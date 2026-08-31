"""One path for every model API key.

The ask was: *"any model API key can connect over this internet. Give me that
path, one single path."* This is that path.

Why one path is possible at all
-------------------------------
Because the industry converged. OpenAI's `/v1/chat/completions` became the de
facto wire format, and every provider that matters now serves it -- Anthropic
ships an OpenAI compatibility layer, Google serves one at
`/v1beta/openai`, Groq at `/openai/v1`, and Ollama and LM Studio speak it on
loopback. That is not a small convenience. It means one client, one streaming
loop, one sanitiser and one cascade cover all of them, so "connect any provider"
is a table of base URLs rather than an adapter per vendor.

`model_routes` already proved the shape on two providers. This generalises it:
a model id carries where it lives, `client_for` resolves that to a base URL and
a key, and `wire_name` strips the provenance again before the request.

What the registry is, and what it is not
----------------------------------------
The base URLs below are a *starting guess*, not a verified list. Only OpenRouter
has been exercised against a real key on this machine; the rest are written from
each vendor's published compatibility endpoint and have not been proven here. So
the registry is deliberately not the source of truth -- `connect()` is. Connect
probes the URL with the user's own key, and stores the base URL that actually
answered. A vendor that moves its endpoint costs one clear error message instead
of a silent failure, and `custom` covers anything absent from the table.

That is also why connecting is a live request rather than a form submit. A key
pasted into a text box and saved is a key that fails later, at the worst moment,
inside a voice turn. A key that has just listed the caller's own models is a key
that works, and the list it returned is the model picker.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from openai import OpenAI

#: `.env` is read directly rather than trusted to be in `os.environ`, because it
#: is loaded by `ai_engine` at import and this module must not depend on having
#: been imported after it. Measured consequence of getting that wrong: with
#: `OPENROUTER_API_KEY` sitting in `.env`, `connected_ids()` reported OpenRouter
#: as *not* connected while reporting `openai` as connected off a stray shell
#: variable -- the screen would have told the user their working provider was
#: missing and their unusable one was fine.
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _env_key(name: str) -> str:
    """A key from the process environment, falling back to `.env` on disk."""
    if not name:
        return ""
    live = (os.getenv(name) or "").strip()
    if live:
        return live
    try:
        from dotenv import dotenv_values

        return str((dotenv_values(ENV_PATH) or {}).get(name) or "").strip()
    except Exception:  # noqa: BLE001 - a missing or unreadable .env is just "no key"
        return ""


#: `::` rather than `/` or `:`, because both are already taken. OpenRouter ids
#: contain a slash (`openai/gpt-4o-mini`) and Ollama tags contain a colon
#: (`gemma3:4b`), so either single character would make `provider` ambiguous with
#: the model name. `::` appears in neither.
SEP = "::"

#: Kept because it shipped. `ollama/gemma3:4b` is what `model_routes` wrote into
#: preference files and what its tests assert on, so it stays a valid spelling of
#: `ollama::gemma3:4b` forever rather than becoming a migration.
LEGACY_LOCAL_PREFIX = "ollama/"

#: The provider a bare id belongs to. A model name with no provider is an
#: OpenRouter id, which is what every id in `ai_engine`'s lists is.
DEFAULT_PROVIDER = "openrouter"


@dataclass(frozen=True)
class Provider:
    """Where a provider lives and what a key for it looks like.

    `key_hint` is shown next to the input box, and it earns its place: the single
    most common way a paste fails is a key from the wrong vendor, and every
    vendor prefixes theirs distinctively. Telling someone their OpenRouter key
    should start `sk-or-v1-` is cheaper than a 401 they have to interpret.
    """

    id: str
    label: str
    base_url: str
    docs_url: str
    key_hint: str = ""
    #: `False` only for loopback servers, which authenticate by being unreachable
    #: from anywhere else.
    needs_key: bool = True
    local: bool = False
    #: Env var honoured for backward compatibility, so a key already in `.env`
    #: keeps working without being re-pasted into the new screen.
    key_env: str = ""
    note: str = ""

    @property
    def verified_here(self) -> bool:
        """Whether this app has actually reached this provider on this machine."""
        return self.id == "openrouter"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "base_url": self.base_url,
            "docs_url": self.docs_url,
            "key_hint": self.key_hint,
            "needs_key": self.needs_key,
            "local": self.local,
            "note": self.note,
        }


#: Ordered as the UI shows them: the aggregator first because one key there
#: reaches hundreds of models and is the shortest path to "connected", then the
#: direct vendors, then the loopback servers, then the escape hatch.
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        docs_url="https://openrouter.ai/keys",
        key_hint="sk-or-v1-…",
        key_env="OPENROUTER_API_KEY",
        note="One key, hundreds of models, including free ones. Start here.",
    ),
    Provider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        docs_url="https://platform.openai.com/api-keys",
        key_hint="sk-…",
        key_env="OPENAI_API_KEY",
    ),
    Provider(
        id="anthropic",
        label="Anthropic (Claude)",
        base_url="https://api.anthropic.com/v1",
        docs_url="https://console.anthropic.com/settings/keys",
        key_hint="sk-ant-…",
        key_env="ANTHROPIC_API_KEY",
        note="Uses Anthropic's OpenAI-compatible endpoint.",
    ),
    Provider(
        id="google",
        label="Google (Gemini)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        docs_url="https://aistudio.google.com/apikey",
        key_hint="AIza…",
        key_env="GEMINI_API_KEY",
    ),
    Provider(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        docs_url="https://console.groq.com/keys",
        key_hint="gsk_…",
        key_env="GROQ_API_KEY",
        note="Fastest first token of the hosted providers, which matters for voice.",
    ),
    Provider(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        docs_url="https://platform.deepseek.com/api_keys",
        key_hint="sk-…",
        key_env="DEEPSEEK_API_KEY",
    ),
    Provider(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        docs_url="https://console.mistral.ai/api-keys",
        key_env="MISTRAL_API_KEY",
    ),
    Provider(
        id="xai",
        label="xAI (Grok)",
        base_url="https://api.x.ai/v1",
        docs_url="https://console.x.ai",
        key_hint="xai-…",
        key_env="XAI_API_KEY",
    ),
    Provider(
        id="together",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        docs_url="https://api.together.ai/settings/api-keys",
        key_env="TOGETHER_API_KEY",
        note="Large catalogue of open-weight models, hosted.",
    ),
    Provider(
        id="fireworks",
        label="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        docs_url="https://fireworks.ai/account/api-keys",
        key_env="FIREWORKS_API_KEY",
    ),
    Provider(
        id="cerebras",
        label="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        docs_url="https://cloud.cerebras.ai",
        key_env="CEREBRAS_API_KEY",
    ),
    Provider(
        id="ollama",
        label="Ollama (this machine)",
        base_url="http://127.0.0.1:11434/v1",
        docs_url="https://ollama.com/download",
        needs_key=False,
        local=True,
        note="Runs offline with no key and no quota. The floor under an outage.",
    ),
    Provider(
        id="lmstudio",
        label="LM Studio (this machine)",
        base_url="http://127.0.0.1:1234/v1",
        docs_url="https://lmstudio.ai",
        needs_key=False,
        local=True,
        note="Enable the local server in LM Studio's Developer tab first.",
    ),
    Provider(
        id="llamacpp",
        label="llama.cpp (this machine)",
        base_url="http://127.0.0.1:8080/v1",
        docs_url="https://github.com/ggml-org/llama.cpp",
        needs_key=False,
        local=True,
        note="Whatever `llama-server` is currently serving.",
    ),
    Provider(
        id="custom",
        label="Anything else",
        base_url="",
        docs_url="",
        note=(
            "Any endpoint that speaks the OpenAI chat-completions protocol -- a "
            "corporate gateway, a vLLM box, a provider not listed here. Give the "
            "base URL that ends in /v1."
        ),
    ),
)

PROVIDERS_BY_ID: dict[str, Provider] = {provider.id: provider for provider in PROVIDERS}


def provider(provider_id: str) -> Provider | None:
    return PROVIDERS_BY_ID.get((provider_id or "").strip().lower())


# --- Model ids ---------------------------------------------------------------


def split_model_id(model: str) -> tuple[str, str]:
    """`("groq", "llama-3.3-70b")` from `"groq::llama-3.3-70b"`.

    Three spellings resolve, and the order matters. `provider::name` is the
    current one. `ollama/name` is the legacy one and is kept working rather than
    migrated, because it is already written into preference files on disk. A bare
    name is an OpenRouter id -- `openai/gpt-4o-mini` is a *model* there, not the
    OpenAI provider, which is exactly the ambiguity `::` exists to settle.
    """
    cleaned = (model or "").strip()
    if SEP in cleaned:
        head, _, tail = cleaned.partition(SEP)
        head = head.strip().lower()
        if head in PROVIDERS_BY_ID:
            return head, tail.strip()
        return DEFAULT_PROVIDER, cleaned
    if cleaned.startswith(LEGACY_LOCAL_PREFIX):
        return "ollama", cleaned[len(LEGACY_LOCAL_PREFIX):]
    return DEFAULT_PROVIDER, cleaned


def provider_of(model: str) -> str:
    return split_model_id(model)[0]


def wire_name(model: str) -> str:
    """The name to put in the request body, with the provenance stripped."""
    return split_model_id(model)[1]


def qualify(provider_id: str, name: str) -> str:
    """The id to store and to show. Bare for OpenRouter, prefixed otherwise.

    OpenRouter stays bare so every id already in `ai_engine`'s lists, in the
    cooldown table and in saved preferences keeps its current spelling.
    """
    provider_id = (provider_id or "").strip().lower()
    name = (name or "").strip()
    if not name or provider_id in ("", DEFAULT_PROVIDER):
        return name
    return f"{provider_id}{SEP}{name}"


# --- The key vault -----------------------------------------------------------
#
# A separate file from `.env`, for the same reason the model preference is a
# separate file: this app rewrites the vault whenever a key is added or removed,
# and a rewrite of `.env` puts every other secret in that file -- Google, Meta,
# database URLs -- one bad write away from being lost. `.env` stays hand-edited
# and read-only from here.
#
# The keys are stored in plaintext, and that is worth stating rather than
# implying. It matches what `.env` already does on this machine, the file lives
# under a gitignored directory, and permissions are tightened where the platform
# allows. It is not encryption at rest, and nothing here should be described as
# a secret store.

VAULT_FILE = Path(__file__).resolve().parents[1] / ".akansha" / "providers.json"


def _empty_vault() -> dict[str, Any]:
    return {"version": 1, "providers": {}}


def read_vault(*, path: Path | None = None) -> dict[str, Any]:
    """Never raises. A corrupt vault is an empty vault, because the alternative
    is an assistant that will not start because a JSON file lost a brace."""
    target = path or VAULT_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_vault()
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        return _empty_vault()
    return data


def _write_vault(data: dict[str, Any], *, path: Path | None = None) -> None:
    target = path or VAULT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        # Windows ignores most of the POSIX mode bits. Best effort, and the
        # failure is not worth interrupting a connection over.
        pass


def mask(key: str) -> str:
    """`sk-or-v1-…9f2a`. Enough to tell two keys apart, not enough to use one.

    The screen has to show *something*, or a user with two accounts cannot tell
    which one is connected. This is the smallest thing that answers that.
    """
    cleaned = (key or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "…" * 3
    return f"{cleaned[:7]}…{cleaned[-4:]}"


def api_key_for(provider_id: str, *, path: Path | None = None) -> str:
    """The key to use, vault first, then the environment.

    Vault first because it is what the user last pasted into the screen, and a
    screen that appears to save a key but is silently overridden by a stale `.env`
    line is a bug nobody can see. Environment second so that a key already in
    `.env` -- `OPENROUTER_API_KEY` on this machine -- works with no migration.
    """
    entry = read_vault(path=path)["providers"].get(provider_id) or {}
    stored = str(entry.get("api_key") or "").strip()
    if stored:
        return stored
    spec = provider(provider_id)
    if spec and spec.key_env:
        return _env_key(spec.key_env)
    return ""


def base_url_for(provider_id: str, *, path: Path | None = None) -> str:
    """Where to send the request: whatever answered at connect time, else the
    registry's guess."""
    entry = read_vault(path=path)["providers"].get(provider_id) or {}
    stored = str(entry.get("base_url") or "").strip().rstrip("/")
    if stored:
        return stored
    spec = provider(provider_id)
    return (spec.base_url if spec else "").rstrip("/")


# --- One client per provider -------------------------------------------------


class ProviderNotConnected(RuntimeError):
    """Asked to reach a provider that has no key or no address."""


#: Local generation is slow in a way that is not a failure: the first request
#: after a model is loaded pages several gigabytes off disk. The cloud timeout
#: would abort a model that was going to answer.
LOCAL_TIMEOUT_S = 120.0

_client_cache: dict[str, OpenAI] = {}


def client_for(model: str, *, timeout: float | None = None, path: Path | None = None) -> OpenAI:
    """The client that can reach `model`.

    Raises `ProviderNotConnected` rather than returning a client that is certain
    to 401, because a 401 inside the cascade is spent as a real attempt and looks
    to the user like the model refused them.
    """
    provider_id = provider_of(model)
    spec = provider(provider_id)
    base = base_url_for(provider_id, path=path)
    if not base:
        raise ProviderNotConnected(
            f"'{provider_id}' has no base URL. Connect it in Settings > Models."
        )
    key = api_key_for(provider_id, path=path)
    if spec and spec.needs_key and not key:
        raise ProviderNotConnected(
            f"No API key for {spec.label}. Add one in Settings > Models."
        )
    if timeout is None:
        timeout = LOCAL_TIMEOUT_S if (spec and spec.local) else 60.0

    cache_key = f"{base}|{timeout}|{key[-6:]}"
    client = _client_cache.get(cache_key)
    if client is None:
        # `api_key` is required by the SDK even where the server ignores it, which
        # is why a loopback server gets a placeholder rather than an empty string.
        client = OpenAI(base_url=base, api_key=key or "not-needed", timeout=timeout, max_retries=0)
        _client_cache[cache_key] = client
    return client


class ProviderNotConnected(RuntimeError):
    """Asked to reach a provider that has no key or no address."""


# --- Connecting --------------------------------------------------------------

#: Short, because this runs while someone watches a spinner. A provider that
#: cannot list models in fifteen seconds is a provider they should hear about.
_CONNECT_TIMEOUT_S = 15.0


def connect(
    provider_id: str,
    api_key: str = "",
    *,
    base_url: str = "",
    path: Path | None = None,
    probe: Any = None,
) -> dict[str, Any]:
    """Prove a key works, discover what it can reach, then store it.

    In that order, and the order is the design. Storing first and validating
    later is how a key that was mistyped becomes a voice turn that fails in front
    of the user; the model list that comes back from a successful probe is also
    exactly what the picker needs, so the validating request pays for itself.

    Two requests, not one, because they answer different questions.
    `_probe_models` asks *what can this endpoint serve* and `_verify_key` asks
    *are these credentials real*. Collapsing them is a bug I shipped and had to
    take back: `/models` is public on OpenRouter, so a catalogue of 387 models
    came back for `sk-or-v1-definitely-not-a-real-key` and the fake key was
    written straight over the working one.

    Nothing is written unless both succeed. A refused connect leaves the previous
    working key in place. `verified=False` in the result is a third answer --
    reachable, stored, but the provider gave no way to be sure of the key -- and
    it is reported rather than rounded up to success.
    """
    spec = provider(provider_id)
    if spec is None:
        return {"ok": False, "error": f"Unknown provider '{provider_id}'."}

    target = (base_url or spec.base_url or "").strip().rstrip("/")
    if not target:
        return {"ok": False, "error": "Give the base URL for this endpoint (it usually ends in /v1)."}

    key = (api_key or "").strip()
    if not key and spec.needs_key:
        # Fall back to whatever is already in the environment, so "Connect" works
        # for a provider whose key is already in `.env` without asking the user to
        # find and re-paste it.
        key = api_key_for(provider_id, path=path)
    if spec.needs_key and not key:
        return {"ok": False, "error": f"{spec.label} needs an API key ({spec.key_hint or 'from its dashboard'})."}

    models, error = (probe or _probe_models)(target, key, _CONNECT_TIMEOUT_S)
    if error:
        return {"ok": False, "error": error, "base_url": target}

    verified, verify_error, note = _verify_key(target, key, models, _CONNECT_TIMEOUT_S)
    if verify_error:
        return {"ok": False, "error": verify_error, "base_url": target}

    vault = read_vault(path=path)
    vault["providers"][provider_id] = {
        "api_key": key,
        "base_url": target,
        "models": models,
        "connected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "label": spec.label,
        "verified": verified,
        "verified_note": note,
    }
    _write_vault(vault, path=path)
    _client_cache.clear()
    return {
        "ok": True,
        "provider": provider_id,
        "label": spec.label,
        "base_url": target,
        "key_masked": mask(key),
        "models": [qualify(provider_id, name) for name in models],
        "count": len(models),
        "verified": verified,
        "note": note,
    }


def forget(provider_id: str, *, path: Path | None = None) -> dict[str, Any]:
    """Remove a stored key.

    The environment is *not* cleared, and the response says so, because this app
    does not own `.env` and silently appearing to disconnect a provider that still
    works would be worse than refusing to.
    """
    vault = read_vault(path=path)
    removed = vault["providers"].pop(provider_id, None) is not None
    if removed:
        _write_vault(vault, path=path)
        _client_cache.clear()
    spec = provider(provider_id)
    still_in_env = bool(spec and spec.key_env and _env_key(spec.key_env))
    return {
        "ok": True,
        "removed": removed,
        "still_in_env": still_in_env,
        "note": (
            f"A key is still set in {spec.key_env} in .env, so this provider stays reachable. "
            "Remove that line to disconnect it fully."
            if still_in_env and spec
            else ""
        ),
    }


def _probe_models(base_url: str, api_key: str, timeout: float) -> tuple[list[str], str]:
    """`GET {base}/models` -- the catalogue, and *only* the catalogue.

    This deliberately does not decide whether the key is valid, and that is a
    correction rather than a design preference. It used to be the whole of
    `connect`, and it passed a key of `sk-or-v1-definitely-not-a-real-key`:
    OpenRouter serves `/models` unauthenticated, so 387 models came back and the
    fake key was written over the working one. Any endpoint that answers without
    credentials cannot be an authentication check. `_verify_key` is.
    """
    import requests  # local import: the probe is the only thing here that needs it

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = requests.get(f"{base_url}/models", headers=headers, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - any transport failure is "not reachable"
        return [], _explain_transport(exc, base_url)

    if response.status_code in (401, 403):
        return [], "That key was rejected. Check it was copied whole, and from this provider."
    if response.status_code == 404:
        # Some gateways serve completions but not the catalogue. Not fatal, and
        # not something to pretend we discovered models from.
        return [], ""
    if response.status_code >= 400:
        return [], f"{base_url} answered {response.status_code}: {response.text[:160]}"

    try:
        payload = response.json()
    except ValueError:
        return [], f"{base_url}/models did not return JSON. Is this an OpenAI-compatible endpoint?"

    entries = payload.get("data") if isinstance(payload, dict) else payload
    names: list[str] = []
    for entry in entries or []:
        name = entry.get("id") if isinstance(entry, dict) else str(entry)
        if name and name not in names:
            names.append(str(name))
    names.sort()
    return names, ""


#: Tried in order for the verifying request. Small, cheap and widely present; the
#: reply is thrown away, only the status code is read. A miss is not a failure --
#: `_verify_key` falls through to the catalogue's own first entry.
_VERIFY_MODEL_HINTS = ("gpt-4o-mini", "gpt-3.5-turbo", "claude-3-5-haiku-latest")


def _verify_key(
    base_url: str,
    api_key: str,
    models: list[str],
    timeout: float,
) -> tuple[bool, str, str]:
    """One `max_tokens=1` completion, read for its status code alone.

    Returns `(verified, error, note)`. A non-empty `error` means refuse the key
    and keep whatever was stored before. `verified=False` with no error means the
    provider would not give a straight answer either way, which is a real third
    outcome and is carried through to the screen rather than smoothed over.

    The status codes are the whole point:

    * 200          -- the key is real and the account can serve. Proven.
    * 401 / 403    -- the key is wrong. This is the case the old `/models`-only
                      check could not see, and the only one that refuses a write.
    * 402 / 429    -- the key is real; the *account* is out of credit or over its
                      rate limit. Counting this as a bad key would refuse the
                      correct key on the free tier, which is exactly the state
                      this machine is in.
    * anything else -- unproven, stored, said out loud.

    A completion is used rather than a provider-specific credential endpoint
    (OpenRouter has `/key`, most do not) because the whole premise here is one
    path for every OpenAI-compatible provider. `/chat/completions` is the one
    route all fifteen are guaranteed to implement -- it is what they are *for* --
    and it is the same route a voice turn will use, so a pass here means the pass
    was measured on the path that matters.
    """
    if not api_key:
        # Local endpoints (Ollama, LM Studio, llama.cpp). There is no credential,
        # so there is nothing to prove; the catalogue already showed it is up.
        return False, "", "No key needed for a local endpoint, so nothing was authenticated."

    import requests  # local import, as in _probe_models

    candidate = ""
    for hint in _VERIFY_MODEL_HINTS:
        candidate = next((name for name in models if hint in name), "")
        if candidate:
            break
    if not candidate:
        candidate = models[0] if models else ""
    if not candidate:
        return False, "", "This endpoint listed no models, so the key could not be checked."

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": candidate,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - transport failures read the same as above
        return False, _explain_transport(exc, base_url), ""

    code = response.status_code
    if code < 300:
        return True, "", ""
    if code in (401, 403):
        return False, "That key was rejected by the provider. Check it was copied whole, and from this provider.", ""
    if code == 402:
        return True, "", "The key is valid but the account is out of credit, so paid models will refuse."
    if code == 429:
        return True, "", "The key is valid; the provider is rate-limiting it right now."
    return False, "", f"The key could not be confirmed: {candidate} answered {code}. Stored unverified."


def _explain_transport(exc: Exception, target: str) -> str:
    text = str(exc).lower()
    if "refused" in text or "failed to establish" in text:
        return f"Nothing is listening on {target}. Is the server running?"
    if "timed out" in text or "timeout" in text:
        return f"{target} did not answer in time."
    if "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text:
        return f"Could not resolve the host in {target}. Check the URL."
    if "certificate" in text or "ssl" in text:
        return f"The TLS certificate for {target} was rejected."
    return f"Could not reach {target}: {exc}"


# --- What the screen renders -------------------------------------------------


def connected_ids(*, path: Path | None = None) -> list[str]:
    """Providers that can be reached right now, by vault entry or by env var."""
    vault = read_vault(path=path)["providers"]
    live: list[str] = []
    for spec in PROVIDERS:
        # A loopback server is not asserted here: "reachable" for those is what
        # `model_routes` discovery decides by probing, and claiming otherwise
        # would put an unreachable local model in the cascade.
        if spec.id in vault or (spec.key_env and _env_key(spec.key_env)):
            live.append(spec.id)
    return live


def available_models(*, path: Path | None = None) -> list[str]:
    """Every model any connected provider reported, already provider-qualified."""
    vault = read_vault(path=path)["providers"]
    out: list[str] = []
    for provider_id, entry in vault.items():
        for name in entry.get("models") or []:
            qualified = qualify(provider_id, str(name))
            if qualified not in out:
                out.append(qualified)
    return out


def describe(*, path: Path | None = None) -> dict[str, Any]:
    """The provider half of the Models screen, in one shape.

    Deliberately returns every provider, not just the connected ones: the screen
    is a place to *connect* something, so the ones that are not connected are the
    important rows. Keys are masked here and nowhere else -- no endpoint in this
    app returns a usable key, including to the user who pasted it.
    """
    vault = read_vault(path=path)["providers"]
    rows: list[dict[str, Any]] = []
    for spec in PROVIDERS:
        entry = vault.get(spec.id) or {}
        env_key = _env_key(spec.key_env) if spec.key_env else ""
        stored = str(entry.get("api_key") or "")
        row = spec.as_dict()
        row.update(
            {
                "connected": bool(stored or env_key or not spec.needs_key),
                "source": "vault" if stored else ("env" if env_key else ""),
                "key_masked": mask(stored or env_key),
                "base_url_in_use": base_url_for(spec.id, path=path),
                "models": [qualify(spec.id, str(name)) for name in (entry.get("models") or [])],
                "connected_at": entry.get("connected_at", ""),
                "verified_here": spec.verified_here,
                # "There is a key", "the key reached the provider" and "the key
                # was accepted" are three different claims, and the screen must
                # not merge them. A key picked up from `.env` has never been
                # tried at all. A vault entry means `connect` got an answer --
                # but `/models` answers OpenRouter without any key, so only the
                # authenticated completion in `_verify_key` earns `proven`.
                # Showing all three as "Connected" is how a stale key becomes a
                # failure in the middle of a voice turn instead of on the screen
                # where it can be fixed.
                "proven": bool(stored and entry.get("verified")),
                "note": entry.get("verified_note", ""),
            }
        )
        rows.append(row)
    return {"providers": rows, "separator": SEP}
