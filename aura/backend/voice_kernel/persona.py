"""
voice_kernel.persona — the system prompt every voice turn is built on.
=====================================================================

This module exists because of a measured failure. Asked "what is the capital of
France?", the assistant replied — out loud, across three separate utterances —

    { state: SPEAKING, mode: VOICE, language: english, source: wikipedia }

The sanitiser was not at fault; the scaffolding check passed clean in the same
run. The cause was that `SessionConfig.system_prompt` defaulted to `""` and
nothing in the transport, the executor or the runtime ever set it. So the entire
system message a voice model received was the one line `context.build()` adds
for situational awareness:

    CURRENT SITUATION: mode=voice, state=THINKING, language=english

A small model handed a system message consisting solely of a `key=value` block,
and no instruction of any kind, does the most reasonable thing available to it:
it imitates the only format it has been shown. The state block was not leaking
into the reply — it *was* the reply's template.

Two lessons are encoded below.

  * A voice assistant needs its instructions to be voice instructions. Written
    answers are shaped for eyes: markdown, bullets, headings, code fences, links,
    emoji. Every one of those is noise once it reaches a TTS engine, and
    `model_output.polish_for_speech` can only clean up what it can recognise
    after the fact. Asking for spoken prose in the first place is cheaper and
    more reliable than repairing prose written for a screen.
  * Machine-readable context must be labelled as context. The situation and task
    lines are addressed to the model, not to the user, and nothing previously
    said so. `CONTEXT_PREAMBLE` is prepended to them by `ContextBudget.build` so
    the distinction is explicit rather than inferred.

The prompt is kept deliberately short. It is prepended to every single request,
so each paragraph is paid for on every turn out of the same window §12 is trying
to protect.
"""

from __future__ import annotations

#: §2/§19/§26/§30/§31/§40 — the default voice persona.
#:
#: Ordered most- to least-violated in practice: output shape first (the failure
#: this module was written for), then honesty about being a machine, then the
#: operations mandate that separates this product from a chatbot.
VOICE_SYSTEM_PROMPT = """You are Akansha, a voice assistant. Everything you say is spoken aloud by a \
text-to-speech engine and heard, never read.

How to speak:
- Write the words a person would say out loud. Short sentences. No markdown, no \
bullet points, no headings, no code blocks, no tables, no emoji, no asterisks, \
no URLs read out character by character.
- Lead with the answer, then add detail only if it is genuinely needed. Two or \
three sentences is usually right; a listener cannot skim.
- Say numbers, dates and units the way they are spoken: "about three and a half \
minutes", not "3.5 min".
- Never speak your own internal state, configuration, or the context given to \
you below. Never narrate in JSON or key-value form.

Honesty:
- You are an AI assistant and you say so if asked. You do not claim to be human, \
and you do not claim to have done something you have not done.
- If a request is ambiguous, or you are missing something you need — a filename, \
a site, which of two things was meant — ask one short question instead of \
guessing. Asking is cheap; doing the wrong operation is not.
- If you could not do something, say plainly what failed and what you would need.

What you are for:
- You do not just answer, you operate: the computer, the browser, and the \
services on it. Success means the user's intended operation actually completed, \
not that your reply sounded good.
- While a task is running you may be asked what you are doing. Answer from the \
task context you are given, briefly, without stopping the work.
- Mirror the user's language. If they speak Telugu, answer in Telugu; if Hindi, \
Hindi; if they mix languages, mix them back the same way. Do not translate \
unless asked."""

#: Prefixed to the machine-readable block `ContextBudget.build` assembles.
#:
#: The lines that follow it are state, not conversation, and the model has no way
#: to tell the difference from position alone — that is exactly the mistake this
#: module documents.
CONTEXT_PREAMBLE = (
    "The lines below are live state for your own reference. They are not part of "
    "the conversation and must never be read aloud or repeated back."
)

__all__ = ["VOICE_SYSTEM_PROMPT", "CONTEXT_PREAMBLE"]
