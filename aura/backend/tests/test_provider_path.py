"""
Tests for the one provider path -- `backend/providers.py`.
==========================================================

The premise of that module is a single sentence: *a stored key is a proven key.*
Everything downstream leans on it. `client_for` hands a key to the streaming loop
without re-checking it, the model picker is populated from whatever the last
successful connect discovered, and the screen says "Connected". If the premise is
false, the failure does not surface on the settings page where it could be fixed
-- it surfaces mid-sentence in a voice turn.

It was false. `connect` proved keys by listing models, and `GET /v1/models` is
public on OpenRouter: a key of `sk-or-v1-definitely-not-a-real-key` came back
with 387 models, `ok=True`, and overwrote the working key. The first test here is
that exact scenario, and it is first because it is the one that shipped.

The rest guard the claims the module makes about itself in prose, since prose does
not fail a build: that ids already written to disk keep resolving, that a key is
never in a response, that "out of credit" is a working key and "cannot tell" is
neither a pass nor a failure.
"""

from __future__ import annotations

import json

import pytest

from backend import providers


@pytest.fixture()
def vault(tmp_path):
    """A vault file of its own, so a test can never read or write the real keys."""
    return tmp_path / "providers.json"


def _catalogue(*names: str):
    """A stand-in for `_probe_models` -- the injectable `probe` exists for this."""

    def probe(base_url: str, api_key: str, timeout: float):
        return list(names), ""

    return probe


def _verifier(result: tuple[bool, str, str]):
    """A stand-in for `_verify_key`, which is the only part that spends a request."""
    return lambda *args, **kwargs: result


GOOD = (True, "", "")


def test_a_public_catalogue_is_not_an_authentication_check(vault, monkeypatch):
    """The shipped bug, pinned.

    A catalogue that answers without credentials says nothing about the key. Both
    halves matter: the bad key must be refused, *and* the good one must still be
    there afterwards, because the original failure was not "a bad key was
    accepted" but "a bad key replaced a working one".
    """
    monkeypatch.setattr(providers, "_verify_key", _verifier(GOOD))
    first = providers.connect(
        "openrouter", "sk-or-v1-real", path=vault, probe=_catalogue("openai/gpt-4o-mini")
    )
    assert first["ok"] and first["verified"]

    # Same 387-models-and-no-error probe as before; only the auth answer differs.
    monkeypatch.setattr(
        providers, "_verify_key", _verifier((False, "That key was rejected by the provider.", ""))
    )
    refused = providers.connect(
        "openrouter", "sk-or-v1-definitely-not-a-real-key", path=vault, probe=_catalogue("a", "b")
    )

    assert refused["ok"] is False
    assert "rejected" in refused["error"]
    assert providers.api_key_for("openrouter", path=vault) == "sk-or-v1-real"


def test_out_of_credit_is_a_working_key(vault, monkeypatch):
    """402 is the account's problem, not the key's.

    This account is on the free tier and 402s on paid routes, so treating a 402 as
    a bad key would refuse the correct key -- and refusing the correct key is
    unrecoverable from the screen, because there is nothing else to paste.
    """
    monkeypatch.setattr(
        providers, "_verify_key", _verifier((True, "", "out of credit, so paid models will refuse"))
    )
    result = providers.connect("openrouter", "sk-or-v1-real", path=vault, probe=_catalogue("x"))

    assert result["ok"] and result["verified"] is True
    assert "credit" in result["note"]
    row = _row(providers.describe(path=vault), "openrouter")
    assert row["proven"] is True and "credit" in row["note"]


def _row(described: dict, provider_id: str) -> dict:
    return next(row for row in described["providers"] if row["id"] == provider_id)


def test_a_key_that_cannot_be_confirmed_is_stored_but_not_proven(vault, monkeypatch):
    """The third outcome, kept distinct from both of the other two.

    A gateway that answers 500 to the verifying request has told us nothing. The
    key is kept -- refusing it would lock someone out of a provider that may be
    perfectly fine -- but `proven` stays false and the reason is carried to the
    screen, so a later failure is expected rather than mysterious.
    """
    monkeypatch.setattr(
        providers, "_verify_key", _verifier((False, "", "answered 500. Stored unverified."))
    )
    result = providers.connect("groq", "gsk_live", path=vault, probe=_catalogue("llama-3.3-70b"))

    assert result["ok"] is True
    assert result["verified"] is False
    assert "unverified" in result["note"]
    assert providers.api_key_for("groq", path=vault) == "gsk_live"
    assert _row(providers.describe(path=vault), "groq")["proven"] is False


def test_a_local_endpoint_is_connected_without_claiming_it_was_authenticated(vault):
    """Ollama and LM Studio have no credential, so there is nothing to prove.

    `verified=False` here is not a warning. The note is what stops it reading as
    one, and it is the reason `_verify_key` returns a note alongside the flag
    rather than a bare boolean.
    """
    result = providers.connect("ollama", path=vault, probe=_catalogue("gemma3:4b"))

    assert result["ok"] is True
    assert result["verified"] is False
    assert "local" in result["note"].lower()
    assert result["models"] == ["ollama::gemma3:4b"]


def test_verify_reads_the_status_code_and_nothing_else(monkeypatch):
    """`_verify_key` itself, with the one request it makes stubbed out.

    Checked here rather than through `connect` because the mapping from status
    code to verdict *is* the fix, and every one of these five lines was a wrong
    answer at some point in the design.
    """
    seen: dict = {}

    class _Response:
        def __init__(self, code):
            self.status_code = code
            self.text = "body"

    def _post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, body=json)
        return _Response(_post.code)

    import requests

    monkeypatch.setattr(requests, "post", _post)

    for code, expected in ((200, True), (402, True), (429, True), (401, False), (500, False)):
        _post.code = code
        verified, error, _note = providers._verify_key("http://x/v1", "k", ["m"], 1.0)
        assert verified is expected, code
        # Only a rejected credential may refuse the write. A 500 must not.
        assert bool(error) is (code == 401), code

    assert seen["url"] == "http://x/v1/chat/completions"
    assert seen["body"]["max_tokens"] == 1, "the verifying request must stay free"


@pytest.mark.parametrize(
    "model, expected_provider, expected_wire",
    [
        # A bare id is an OpenRouter id, and this one is the reason `::` exists:
        # `openai/` here names a *model family on OpenRouter*, not the OpenAI
        # provider. Reading the `/` as provenance would silently route every one
        # of `ai_engine`'s ids to the wrong endpoint.
        ("openai/gpt-4o-mini", "openrouter", "openai/gpt-4o-mini"),
        ("minimax/minimax-m3:free", "openrouter", "minimax/minimax-m3:free"),
        # The legacy spelling, already written into `.akansha/model_route.json`.
        ("ollama/gemma3:4b", "ollama", "gemma3:4b"),
        # The current spelling. The tag keeps its own colon.
        ("ollama::gemma3:4b", "ollama", "gemma3:4b"),
        ("groq::llama-3.3-70b-versatile", "groq", "llama-3.3-70b-versatile"),
        # An unknown prefix is not a provider, so the whole string stays a model
        # name rather than becoming a request to nowhere.
        ("nope::whatever", "openrouter", "nope::whatever"),
    ],
)
def test_ids_already_written_to_disk_keep_resolving(model, expected_provider, expected_wire):
    assert providers.provider_of(model) == expected_provider
    assert providers.wire_name(model) == expected_wire


def test_nothing_that_leaves_this_module_carries_a_key(vault, monkeypatch):
    """Masking is the whole contract with the frontend.

    `describe` is what the settings screen renders, so a raw key appearing
    anywhere in it is a key in a browser -- and in a screen recording, and in a
    support screenshot. Asserted against the serialised JSON rather than field by
    field, because the leak would come from a field nobody thought to check.
    """
    secret = "sk-or-v1-0123456789abcdefghijklmnop"
    monkeypatch.setattr(providers, "_verify_key", _verifier(GOOD))
    result = providers.connect("openrouter", secret, path=vault, probe=_catalogue("x"))

    assert secret not in json.dumps(result)
    assert secret not in json.dumps(providers.describe(path=vault))
    assert result["key_masked"].startswith("sk-or-v") and result["key_masked"].endswith("mnop")
    # The vault, by contrast, holds the real thing -- that is its job, and the
    # reason `.akansha/` is in .gitignore.
    assert providers.api_key_for("openrouter", path=vault) == secret


def test_a_corrupt_vault_reads_as_empty_rather_than_raising(vault, monkeypatch):
    """A half-written JSON file must not take the app down.

    `read_vault` is called on the request path, so an exception here is a settings
    page that cannot load -- which is the one page that could fix the problem.
    Losing the entries is bad; being unable to re-enter them is worse.
    """
    vault.write_text('{"providers": {"openrouter":', encoding="utf-8")
    monkeypatch.setattr(providers, "_env_key", lambda name: "")

    assert providers.read_vault(path=vault)["providers"] == {}
    # `connected_ids` counts `.env` keys too, so the environment is stubbed away
    # here; that it does count them is the subject of the forget test above.
    assert providers.connected_ids(path=vault) == []


def test_forget_does_not_pretend_to_remove_a_key_it_does_not_own(vault, monkeypatch):
    """`.env` is hand-edited and read-only from here.

    So "Disconnect" on a provider whose key is also in `.env` leaves it working,
    and the response has to say that out loud. Silently showing it as
    disconnected while it keeps answering is a lie the user would only discover by
    accident.
    """
    monkeypatch.setattr(providers, "_verify_key", _verifier(GOOD))
    providers.connect("openrouter", "sk-or-v1-real", path=vault, probe=_catalogue("x"))
    monkeypatch.setattr(providers, "_env_key", lambda name: "sk-or-v1-from-dotenv")

    result = providers.forget("openrouter", path=vault)

    assert result["removed"] is True
    assert result["still_in_env"] is True
    assert "OPENROUTER_API_KEY" in result["note"]
    # Still reachable, and honestly reported as coming from the environment now.
    assert providers.api_key_for("openrouter", path=vault) == "sk-or-v1-from-dotenv"
    assert _row(providers.describe(path=vault), "openrouter")["source"] == "env"
    assert _row(providers.describe(path=vault), "openrouter")["proven"] is False


def test_the_refusals_say_what_to_do_next(vault, monkeypatch):
    """Error strings are the whole interface when a connect fails."""
    unknown = providers.connect("nope", "k", path=vault)
    assert unknown["ok"] is False and "nope" in unknown["error"]

    # `custom` ships with no base URL on purpose: it is the escape hatch for a
    # provider absent from the table, so the URL is the one thing it must be asked.
    no_url = providers.connect("custom", "k", path=vault)
    assert no_url["ok"] is False and "/v1" in no_url["error"]

    # An empty key falls back to `.env` before it fails, so that "Connect" works
    # for a provider already configured there. Stub the environment away to reach
    # the refusal itself rather than whatever this machine happens to have set.
    monkeypatch.setattr(providers, "_env_key", lambda name: "")
    missing_key = providers.connect("groq", "", path=vault, probe=_catalogue("x"))
    assert missing_key["ok"] is False and "API key" in missing_key["error"]
