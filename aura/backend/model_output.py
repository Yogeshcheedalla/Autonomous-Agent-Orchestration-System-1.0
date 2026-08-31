"""
model_output — keep the model's scaffolding out of the user's ears.
==================================================================

Free and open-weight models leak their internals into `delta.content`. Measured
against this account's actual OpenRouter routes:

  * `liquid/lfm-2.5-2.6b:free`, asked to open a project, replied with the literal
    string ``<|tool_call_start|>[bash(command='ls -la')]<|tool_call_end|>``.
  * `nvidia/nemotron-3.5-lightning:free` replied with 1721 characters beginning
    ``Here's a thinking process:\\n\\n1. **Analyze User Input:**`` — its entire
    chain of thought, in the content channel, for a request that wanted one
    sentence.

In a text chat that is ugly. In a voice product it is *spoken aloud*, and there
was nothing anywhere in the backend that removed it: no `<think>` handling, no
special-token handling, no markdown flattening before TTS. So the first thing the
user would have heard from a fallback model is Akansha reading out her own
prompt-analysis notes.

Two kinds of marker, one mechanism:

  * **Blocks** — an opener and a closer, with everything between them dropped.
    `<think>`, `<|tool_call_start|>`, gpt-oss's harmony `analysis` channel.
  * **Drops** — a bare token deleted wherever it appears. Stray chat-template
    tokens, and (in speech mode) the markdown that a TTS engine has no way to
    pronounce.

The hard part is that this runs on a *stream*. `<|tool_call_start|>` is 21
characters and arrives split across chunks; a naive per-chunk `replace` sees
`<|tool_` and `call_start|>` separately and passes both through. So the sanitiser
holds back exactly the longest suffix of its buffer that could still become a
marker, and nothing more — ordinary prose streams with zero added latency, which
matters because §19 wants sentence one spoken while sentence three is generating.

What this deliberately does *not* do is guess at unmarked prose. There is no
reliable way to tell "Here's a thinking process:" from a legitimate reply about
thinking processes, and a heuristic that strips the first paragraph of every
answer would be worse than the leak. Models that dump reasoning without markers
are excluded from the voice model list instead — see `ai_engine.VOICE_MODELS`.
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

#: (opener, closer) pairs. Everything between them is dropped, along with both
#: markers. An opener with no closer suppresses to the end of the stream — that is
#: intentional: a truncated reasoning dump is still a reasoning dump, and callers
#: treat "produced nothing speakable" as a failed candidate and try the next model.
_BLOCKS: Tuple[Tuple[str, str], ...] = (
    ("<think>", "</think>"),
    ("<thinking>", "</thinking>"),
    ("<|thinking|>", "<|/thinking|>"),
    ("<|tool_call_start|>", "<|tool_call_end|>"),
    ("<tool_call>", "</tool_call>"),
    ("[TOOL_CALLS]", "[/TOOL_CALLS]"),
    # gpt-oss "harmony" format: the analysis channel runs until the next message
    # marker, so `<|message|>` acts as its closer.
    ("<|channel|>analysis", "<|message|>"),
)

#: Bare tokens deleted wherever they appear. Chat-template residue that some
#: routes forward verbatim.
_DROPS: Tuple[str, ...] = (
    "<|im_start|>",
    "<|im_end|>",
    "<|start|>",
    "<|end|>",
    "<|eot_id|>",
    "<|endoftext|>",
    "<|assistant|>",
    "<|user|>",
    "<|system|>",
)

#: Additional drops for speech only. A TTS engine cannot pronounce emphasis, and
#: depending on the voice it either reads the asterisks out or swallows the word
#: with them. The text UI keeps its markdown — this is why `for_speech` is a flag
#: rather than the default.
_SPEECH_DROPS: Tuple[str, ...] = ("```", "**", "__", "`", "#")


class StreamSanitizer:
    """Incremental scaffolding filter. One instance per model attempt.

    Not reusable across streams: it carries partial-marker state, so feeding a
    second response into a used instance would let the first one's dangling
    suppression swallow it.
    """

    def __init__(self, for_speech: bool = False) -> None:
        self._buf = ""
        self._closer: str | None = None
        self._drops: Sequence[str] = _DROPS + (_SPEECH_DROPS if for_speech else ())
        markers = [m for pair in _BLOCKS for m in pair] + list(self._drops)
        self._max_marker = max(len(m) for m in markers)
        #: Openers only — what we scan for when not already suppressing.
        self._openers = tuple(open_ for open_, _ in _BLOCKS)
        #: Every token whose *prefix* could be sitting at the end of the buffer.
        self._holdable = self._openers + tuple(self._drops)

    def feed(self, chunk: str) -> str:
        """Absorb a raw delta, return whatever is now safe to emit."""
        if not chunk:
            return ""
        self._buf += chunk
        return self._drain(final=False)

    def finish(self) -> str:
        """Flush at end of stream. Anything still held back is released.

        A held-back tail at this point was a partial marker that never completed —
        so it was ordinary text that merely looked suspicious, and dropping it
        would truncate the last word of the reply.
        """
        if self._closer is not None:
            # Unterminated reasoning block. Discard rather than speak it.
            self._buf = ""
            return ""
        out = self._drain(final=True)
        tail, self._buf = self._buf, ""
        return out + tail

    # ── internals ────────────────────────────────────────────────────────────
    def _drain(self, final: bool) -> str:
        out: List[str] = []
        while True:
            if self._closer is not None:
                index = self._buf.find(self._closer)
                if index == -1:
                    # Still inside the block. Keep only enough to recognise a
                    # closer that is arriving split across chunks.
                    self._buf = self._keep_tail(self._buf, (self._closer,))
                    break
                self._buf = self._buf[index + len(self._closer) :]
                self._closer = None
                continue

            cut, marker = self._earliest(self._buf, self._openers + tuple(self._drops))
            if marker is None:
                if final:
                    out.append(self._buf)
                    self._buf = ""
                else:
                    kept = self._keep_tail(self._buf, self._holdable)
                    out.append(self._buf[: len(self._buf) - len(kept)])
                    self._buf = kept
                break

            out.append(self._buf[:cut])
            self._buf = self._buf[cut + len(marker) :]
            if marker in self._openers:
                self._closer = dict(_BLOCKS)[marker]
            # A drop needs nothing further — the marker is simply gone.
        return "".join(out)

    @staticmethod
    def _earliest(text: str, markers: Sequence[str]) -> Tuple[int, str | None]:
        """Leftmost complete marker in `text`, longest wins on a tie.

        Longest-wins matters: `**` and `#` are substrings of nothing here, but
        `<|start|>` would otherwise shadow nothing while `<think>` and
        `<thinking>` share a prefix and must not be confused.
        """
        best_at, best = len(text), None
        for marker in markers:
            at = text.find(marker)
            if at == -1:
                continue
            if at < best_at or (at == best_at and best and len(marker) > len(best)):
                best_at, best = at, marker
        return (best_at, best) if best else (len(text), None)

    def _keep_tail(self, text: str, markers: Sequence[str]) -> str:
        """Longest suffix of `text` that is a proper prefix of some marker.

        This is the whole reason ordinary prose is not delayed: "Paris." holds
        back nothing, while "...the file <|too" holds back exactly five
        characters until the next chunk resolves them.
        """
        limit = min(len(text), self._max_marker - 1)
        for size in range(limit, 0, -1):
            suffix = text[-size:]
            if any(m.startswith(suffix) for m in markers):
                return suffix
        return ""


#: Markdown links read terribly aloud — "see docs at h-t-t-p-s colon slash slash".
_LINK = re.compile(r"\[([^\]\n]+)\]\((?:[^)\s]+)\)")
#: A list marker at the start of a line is punctuation, not a word.
_BULLET = re.compile(r"(?m)^[ \t]*[-*+][ \t]+")
_WHITESPACE = re.compile(r"[ \t]{2,}")


def polish_for_speech(text: str) -> str:
    """Final pass over a *complete* reply, for the things a stream cannot do.

    Link syntax and line-leading bullets need lookahead across a whole line, so
    they are handled here rather than in `StreamSanitizer`. Callers that stream to
    TTS still get the token-level cleanup live; this tidies the transcript and any
    reply spoken in one piece.
    """
    if not text:
        return ""
    cleaned = _LINK.sub(r"\1", text)
    cleaned = _BULLET.sub("", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    return cleaned.strip()


def sanitize(text: str, for_speech: bool = False) -> str:
    """One-shot convenience for text that is already complete."""
    sanitizer = StreamSanitizer(for_speech=for_speech)
    out = sanitizer.feed(text) + sanitizer.finish()
    return polish_for_speech(out) if for_speech else out.strip()
