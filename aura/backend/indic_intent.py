"""A Latin view of Telugu and Devanagari text, for intent matching only.

Why this exists
---------------
Every intent detector in `ai_engine` is a Latin-script regex --
`\\b(?:open|send|play|show|create|...)\\b` -- and the app offers EN / TE / HI. So
`"ఓపెన్ యూట్యూబ్"` ("open youtube") matched none of them. It is two words, has no
question mark, and cannot match `\\bopen\\b`, so `_is_brief_ack_or_fragment`
classified a perfectly clear command as a meaningless fragment and answered
"tell me exactly what to do". The user had to repeat it in English before
anything happened. That is the whole failure mode this module removes, and it was
not one bad regex: it was every regex, because the text never reached them in a
shape they could read.

What it is, and what it is not
------------------------------
This produces a *matching view*, never display text and never model input. The
user's own words go to the model and to the screen unchanged -- transliterating
what someone said and showing it back is its own insult. Only the classifiers see
the Latin view.

It is also not a transliteration library. `transliterate` is a deliberately plain
character walk, good enough to turn an English loanword written in Telugu into a
recognisable ASCII skeleton and nothing more. Sanskrit retroflex/dental
distinctions are collapsed on purpose: `ట` and `త` both become `t`, because the
goal is that `టైమ్` reaches `\\btime\\b`, not that a scholar could read it back.

The three-step lookup is the useful part:

1. the Indic word itself, in `WORDS` -- native commands like `తెరువు` (open) and
   `खोलो` (open), where a phonetic skeleton would be unrecognisable;
2. its skeleton, in `SKELETONS` -- English loanwords, where speakers and STT
   engines disagree about the spelling (`ఓపెన్`, `ఒపెన్`, `ఓపన్` all arrive);
3. the squeezed skeleton as-is, which already lands on the English word often
   enough to matter: `ఓపెన్` -> `oopen` -> `open`.

An unrecognised word still becomes an ASCII token rather than disappearing, so
word counts and "is this a fragment" stay honest.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

#: The tables live in `aura/shared/indic_intent_tables.json`, not in this file.
#:
#: They used to be Python literals here, which was fine until the *frontend* needed
#: the same view: `src/lib/automationCommands.ts` decides which endpoint a message
#: goes to, and its patterns are Latin too, so `"ఓపెన్ యూట్యూబ్"` was classified as
#: ordinary chat and never reached the automation route at all. Fixing that in the
#: browser meant either an extra round trip per Indic message or a second copy of
#: these tables in TypeScript -- and a second copy of 97 words and 94 skeletons is a
#: copy that drifts, silently, in the direction of whichever language someone edited
#: last. One JSON file read by both is the only version of this that stays true.
#:
#: Load failure is deliberately fatal: without the tables every classifier in
#: `ai_engine` silently reverts to English-only, which is the outage this module
#: exists to remove, and a backend that boots pretending otherwise is worse than one
#: that does not boot.
_TABLES_PATH = Path(__file__).resolve().parent.parent / "shared" / "indic_intent_tables.json"


@lru_cache(maxsize=1)
def _tables() -> dict:
    with _TABLES_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


_T = _tables()

TELUGU_RANGE = "-".join(_T["teluguRange"])
DEVANAGARI_RANGE = "-".join(_T["devanagariRange"])
_INDIC = re.compile(f"[{TELUGU_RANGE}{DEVANAGARI_RANGE}]")
_INDIC_WORD = re.compile(f"[{TELUGU_RANGE}{DEVANAGARI_RANGE}]+")


def has_indic(text: str) -> bool:
    return bool(_INDIC.search(text or ""))


# --- Character tables --------------------------------------------------------
#
# Retroflex and dental collapse to the same Latin letter, and the three Telugu
# sibilants all become `s`. That is a feature: the target is an English keyword,
# and a speaker saying "start" writes `స్టార్ట్` or `ష్టార్ట్` interchangeably.
#
# Telugu and Devanagari are merged into one table per class. Nothing needs to know
# which script a character came from -- the point of the walk below is to leave the
# script behind.

_CONSONANTS: dict[str, str] = _T["consonants"]
_VOWELS: dict[str, str] = _T["vowels"]
_MATRAS: dict[str, str] = _T["matras"]
_VIRAMAS: set[str] = set(_T["viramas"])
#: Nasal and aspirate marks, and the Devanagari nukta, which modifies the letter
#: before it and contributes no sound of its own.
_MARKS: dict[str, str] = _T["marks"]


def transliterate(word: str) -> str:
    """A phonetic ASCII skeleton for one Indic word.

    The inherent vowel is the only subtlety. A bare consonant carries an `a`
    (`ప` is "pa"), which a matra replaces and a virama cancels -- so `ఓపెన్` walks
    out as `oopen` rather than `oopena`, and that is what makes step 3 of the
    lookup work as often as it does.
    """
    out: list[str] = []
    owe_a = False
    for char in word or "":
        if char in _CONSONANTS:
            if owe_a:
                out.append("a")
            out.append(_CONSONANTS[char])
            owe_a = True
        elif char in _MATRAS:
            out.append(_MATRAS[char])
            owe_a = False
        elif char in _VIRAMAS:
            owe_a = False
        elif char in _VOWELS:
            if owe_a:
                out.append("a")
                owe_a = False
            out.append(_VOWELS[char])
        elif char in _MARKS:
            if owe_a:
                out.append("a")
                owe_a = False
            out.append(_MARKS[char])
        elif char.isdigit():
            if owe_a:
                out.append("a")
                owe_a = False
            out.append(char)
        # Anything else in the block -- ZWJ, avagraha, editorial marks -- carries
        # no sound and is dropped rather than guessed at.
    if owe_a:
        out.append("a")
    return "".join(out)


def squeeze(skeleton: str) -> str:
    """`yuutyuub` -> `yutyub`, `oopen` -> `open`.

    Doubled vowels are an artefact of length marks that English spelling does not
    have, so collapsing runs is what closes the gap between a skeleton and the
    word it is trying to be.
    """
    return re.sub(r"(.)\1+", r"\1", skeleton or "")


# --- Step 1: native words ----------------------------------------------------
#
# Keyed by the Indic spelling rather than by a skeleton, because these are real
# Telugu and Hindi words and `చూపించు -> cuupimcu` is not something anyone should
# have to maintain. The English value is chosen to be the exact token the
# classifiers already look for. Greetings and question words earn their place
# beside the commands: a Telugu greeting that matched nothing fell through to the
# *fragment* branch and was answered "tell me exactly what to do".

WORDS: dict[str, str] = _T["words"]


# --- Step 2: loanword skeletons ----------------------------------------------
#
# English words written in Indic script, keyed by the *squeezed* skeleton so that
# one entry covers every spelling a speaker or an STT engine might produce:
# `ఓపెన్`, `ఒపెన్` and `ఓపన్` all squeeze to `open`. Only words whose skeleton
# does not already equal the English spelling need to be here -- `open` and
# `send` arrive correct on their own, `youtube` never does.

SKELETONS: dict[str, str] = _T["skeletons"]


# --- The view ----------------------------------------------------------------


def latin_word(word: str) -> str:
    """One Indic word as the closest English token, by the three-step lookup."""
    if word in WORDS:
        return WORDS[word]
    skeleton = transliterate(word)
    squeezed = squeeze(skeleton)
    # The trailing inherent vowel is the Devanagari case: `यूट्यूब` carries no
    # final virama, so it walks out as `yutyuba` where Telugu's `యూట్యూబ్` gives
    # `yutyub`. Both must reach the same entry.
    for key in (skeleton, squeezed, squeezed.rstrip("a") or squeezed):
        if key in SKELETONS:
            return SKELETONS[key]
    # Unknown word. The squeezed skeleton is returned rather than nothing, so it
    # still counts as a word and a sentence does not shrink into a "fragment".
    return squeezed


def intent_view(text: str) -> str:
    """`"ఓపెన్ యూట్యూబ్"` -> `"open youtube"`. Latin runs are left alone.

    This is what the classifiers should match against. It is never shown, never
    stored and never sent to a model -- mixed input like `"Chrome లో ఓపెన్ చేయి"`
    keeps its English half exactly as typed and gains a readable Telugu half, so
    one regex covers all three languages instead of three sets of regexes drifting
    apart.
    """
    if not text or not _INDIC.search(text):
        return text or ""
    return _INDIC_WORD.sub(lambda match: latin_word(match.group(0)), text)


def both_views(text: str) -> tuple[str, str]:
    """The original and the Latin view, for callers that need to try each.

    Returned as a pair rather than concatenated: joining them doubles the word
    count, which is exactly the signal `_is_brief_ack_or_fragment` reads.
    """
    return (text or ""), intent_view(text)
