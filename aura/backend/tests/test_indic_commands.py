"""
Telugu and Hindi commands reach the same intent regexes English does.
====================================================================

From a real session, verbatim:

    హే హాయ్                -> "Ha, continue cheyyi. Nenu context hold chesthunnanu."
    ఓపెన్ యూట్యూబ్         -> "Sare, ardham ayyindi. Ippudu exact ga em cheyyalo cheppu."
    I said to open the YouTube -> "Sure, let me do that!"  ✅ Opened YouTube

Two clear utterances -- a greeting and an open-YouTube command -- both answered
with canned filler, and the command only ran once it was repeated in English. The
cause was not a wrong regex. It was that every intent regex in `ai_engine` is
Latin (`\\b(?:open|send|show|...)\\b`), so an Indic-script command matched none of
them, and `_is_brief_ack_or_fragment` -- which says yes to anything under four
words with no recognisable verb -- claimed it.

These tests are written against that transcript. The first three are the three
lines above.
"""

from __future__ import annotations

import pytest

from backend import indic_intent
from backend.ai_engine import _intent_text, _is_brief_ack_or_fragment, _is_brief_greeting


def test_the_command_that_was_thrown_away_is_now_a_command():
    """`ఓపెన్ యూట్యూబ్` is "open youtube", and must not read as a fragment."""
    assert indic_intent.intent_view("ఓపెన్ యూట్యూబ్") == "open youtube"
    assert _is_brief_ack_or_fragment("ఓపెన్ యూట్యూబ్") is False


def test_the_greeting_that_was_answered_as_a_fragment_is_now_a_greeting():
    """`హే హాయ్` is "hey hi".

    Note the plural: the old pattern was a `fullmatch` on a *single* greeting
    token, so even the folded `hey hi` would have missed. People say "hey hi" out
    loud far more often than they type it.
    """
    assert indic_intent.intent_view("హే హాయ్") == "hey hi"
    assert _is_brief_greeting("హే హాయ్") is True
    assert _is_brief_greeting("hey hi") is True
    assert _is_brief_greeting("hi") is True


def test_a_real_greeting_is_still_not_a_command():
    """The run must stay short, or "hey open youtube" becomes a greeting."""
    assert _is_brief_greeting("hey hi hello") is True
    assert _is_brief_greeting("hey open youtube") is False
    assert _is_brief_greeting("hello can you open youtube") is False


@pytest.mark.parametrize(
    "said, view",
    [
        # Telugu: English loanwords in Telugu script, which is what STT returns
        # for app names.
        ("ఓపెన్ యూట్యూబ్", "open youtube"),
        ("వాట్సాప్ ఓపెన్ చేయి", "whatsapp open do"),
        ("క్రోమ్ లో గూగుల్ తెరువు", "chrome lo google open"),
        ("పాట ప్లే చేయి", "song play do"),
        # Telugu: native verbs, where a phonetic skeleton would be useless.
        ("వాట్సాప్ తెరువు", "whatsapp open"),
        ("నమస్కారం", "hello"),
        # Hindi. `यूट्यूब` has no closing virama, so its skeleton keeps a trailing
        # inherent vowel that Telugu's `యూట్యూబ్` does not -- both must land here.
        ("यूट्यूब खोलो", "youtube open"),
        ("व्हाट्सएप्प भेजो", "whatsapp send"),
        ("सर्च करो", "search do"),
        # Mixed input keeps its English half exactly as typed, case included.
        ("Chrome లో ఓపెన్ చేయి", "Chrome lo open do"),
        # Pure Latin is returned untouched, which is what keeps the English path
        # free: `intent_view` short-circuits before it looks at anything.
        ("open youtube", "open youtube"),
        ("", ""),
    ],
)
def test_the_latin_view_of_what_was_said(said, view):
    assert indic_intent.intent_view(said) == view


def test_an_unknown_indic_word_becomes_a_token_rather_than_vanishing():
    """A word count is a signal, so dropping words would corrupt it.

    `ఇప్పుడు` ("now") is in no lexicon here and does not need to be. What matters
    is that it still occupies a word, so a four-word sentence is not silently
    reduced to the three-word shape that `_is_brief_ack_or_fragment` claims.
    """
    view = indic_intent.intent_view("ఇప్పుడు సమయం ఎంత")
    assert view == "ipudu time how much"
    assert len(view.split()) >= 3
    assert _is_brief_ack_or_fragment("ఇప్పుడు సమయం ఎంత") is False


def test_the_automation_interceptor_gate_opens_for_indic_commands():
    """The gate only -- deliberately not the action.

    `_handle_task_automation_intent` launches real browsers and real apps, so what
    is asserted here is the condition it uses to decide, evaluated on the same
    Latin view it now reads. That the gate opens is the fix; that the launch works
    was already true for English.
    """
    action_keywords = ["open ", "check ", "launch ", "go to ", "visit ", "show ", "ping "]
    sites = ["twitter", "x.com", "codechef", "github", "whatsapp", "youtube", "gmail", "google"]

    def gate(said: str) -> bool:
        text = _intent_text(said).lower()
        return any(text.startswith(k) or f" {k}" in text for k in action_keywords) or any(
            site in text for site in sites
        )

    assert gate("ఓపెన్ యూట్యూబ్") is True
    assert gate("యూట్యూబ్ ఓపెన్ చేయి") is True
    assert gate("वाट्सएप्प खोलो") is True
    assert gate("వాట్సాప్ తెరువు") is True
    assert gate("hello there") is False


def test_what_the_user_said_is_never_rewritten():
    """The view is for matching. It is not what gets answered or shown.

    Folding someone's Telugu into ASCII and reading it back would be its own
    insult, quite apart from what it would do to the model's sense of which
    language the conversation is in.
    """
    said = "ఓపెన్ యూట్యూబ్"
    assert _intent_text(said) != said, "the view exists, or none of the above works"
    assert indic_intent.has_indic(said) is True
    assert indic_intent.has_indic("open youtube") is False
