"""Which model answers a request, and which server that model lives on.

Why this exists
---------------
The ask was a "model connection UI for Ollama with model choice, and OpenRouter
with automatic failover, plus Gemma and other open source models". Before this
module there were zero references to Ollama anywhere in the backend, and the
cascade in `ai_engine` was a list of model *names* plus exactly one client:
`_openrouter_client()`. A name was enough while every route lived at the same
base URL. A local model does not, so the cascade has to carry provenance, not
just a name -- which is what `provider_of` and `client_for` add.

The failover it enables is the point. A free OpenRouter route that runs out of
capacity currently ends in `_provider_failure_fallback`'s canned apology; with a
local model in the cascade the same outage ends in an answer, offline, with no
key and no quota. That is the honest reason to prefer local *last* rather than
first: it is the floor under the cloud routes, not a replacement for them.

What is deliberately not here
-----------------------------
No install, no download, no model management. Ollama is a separate program that
this machine does not currently have -- `ollama` is not on PATH, there is no
model store under `~/.ollama`, and nothing answers on port 11434. So discovery
reports "not detected" and the UI says how to get it. Writing a downloader into
an assistant's provider layer would be a large, silent, network-heavy action
taken on the user's behalf, and `pull`ing a 3 GB model is exactly the kind of
thing that should be one deliberate command in a terminal.

Testability
-----------
Discovery takes its base URL and its transport as arguments, so the tests point
it at a stub and assert on the parsing and the ordering rules. Called with no
arguments it is a report about this machine.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from openai import OpenAI

from . import providers

#: Model ids carry their provider, because a bare "gemma3:4b" says nothing about
#: where to send it and a bare "openai/gpt-4o-mini" is already an OpenRouter
#: convention. The prefix is stripped again by `wire_name` before the request --
#: the local server has never heard of it.
LOCAL_PREFIX = "ollama/"

PROVIDER_LOCAL = "ollama"
PROVIDER_CLOUD = "openrouter"

#: The provider ids this module's own `local_client()` can reach -- which is
#: Ollama, and only Ollama, because that client is pinned to `DEFAULT_OLLAMA_BASE`.
#: LM Studio and llama.cpp are equally local but listen on different ports, so
#: they are routed by `providers.client_for` instead; calling them "local" here
#: would send them to 11434.
_LOCAL_PROVIDER_IDS = frozenset({"ollama"})

#: Overridable because Ollama can be told to listen elsewhere, and because the
#: tests need somewhere that is not this machine.
#:
#: `127.0.0.1` rather than `localhost`, and that is a measurement rather than a
#: style preference: `localhost` resolves to both `::1` and `127.0.0.1` here, and
#: a probe that has to give up on a dead port pays the connect timeout once per
#: address. Measured 628 / 623 / 626 ms through `localhost` against a 300 ms
#: connect timeout -- almost exactly double -- and Ollama binds the v4 loopback
#: by default anyway, so the second address could only ever be the slow half.
DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434"


def ollama_base_url() -> str:
    """Where the local server is expected to be listening.

    `OLLAMA_HOST` is Ollama's own variable, so a user who already moved the port
    for their own reasons does not have to tell this app about it separately. It
    is documented as `host:port` and accepted as such, hence the scheme repair.
    """
    raw = (os.getenv("OLLAMA_HOST") or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_OLLAMA_BASE
    if "://" not in raw:
        raw = "http://" + raw
    return raw


@dataclass(frozen=True)
class LocalModel:
    """One model already pulled onto this machine."""

    name: str
    #: Bytes on disk. Used to order the cascade, and shown in the UI because it
    #: is the number that decides whether a model fits in this machine's RAM.
    size_bytes: int = 0
    parameter_size: str = ""
    quantization: str = ""

    @property
    def model_id(self) -> str:
        return LOCAL_PREFIX + self.name

    @property
    def size_label(self) -> str:
        if self.size_bytes <= 0:
            return ""
        gigabytes = self.size_bytes / 1_000_000_000
        return f"{gigabytes:.1f} GB" if gigabytes >= 1 else f"{self.size_bytes / 1_000_000:.0f} MB"

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "size_label": self.size_label,
            "parameter_size": self.parameter_size,
            "quantization": self.quantization,
        }


#: What to suggest when nothing is installed. Small on purpose: these run on a
#: CPU, and a model that swaps is slower than the cloud round trip it was meant
#: to replace. Gemma leads because it was asked for by name.
SUGGESTED_LOCAL_MODELS = (
    {
        "name": "gemma3:4b",
        "why": "Google's open model. Fits comfortably in 8 GB of RAM and handles ordinary conversation.",
    },
    {
        "name": "qwen3:4b",
        "why": "Stronger at following instructions and at non-English, including Telugu.",
    },
    {
        "name": "llama3.2:3b",
        "why": "The smallest of the three. Pick this one if the others feel slow.",
    },
)


# --- Discovery ---------------------------------------------------------------
#
# Asking the server what it has is the only correct answer to "which local models
# can I choose", for the same reason `app_discovery` scans the Start Menu instead
# of shipping a list: a hand-written list of models is wrong the first time the
# user pulls one.


def _default_transport(url: str, timeout: Any) -> Any:
    import requests  # imported here so the module loads without it

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


#: `(connect, read)`. The connect half is short because of what was measured on
#: this machine: a TCP connect to the closed port 11434 does not get refused, it
#: *hangs* -- 2009 ms to 127.0.0.1 and 2062 ms to localhost before the socket
#: gave up. The textbook assumption that a dead local port fails instantly is
#: wrong here (something is dropping rather than rejecting the packet), and with
#: it goes the idea that probing on the request path is free. At 300 ms a machine
#: without Ollama pays a third of a second, once per negative cache window.
_PROBE_TIMEOUT = (0.3, 6.0)

#: Split on purpose, and by two orders of magnitude.
#:
#: A *successful* probe is cheap to repeat and worth repeating: pulling a model
#: should show up in the UI without a backend restart. A *failed* probe is the
#: expensive one -- see `_PROBE_TIMEOUT` -- and it is also the overwhelmingly
#: common case, because most machines will never install Ollama. Re-probing that
#: every 15 s would put a 300 ms stall in front of roughly every fourth reply,
#: which is a latency regression paid by every user who does not use the feature.
#: At five minutes it is 312 ms (measured) per five minutes, and the UI's refresh
#: clears the cache outright so a user who has just started Ollama never waits
#: for the TTL.
_POSITIVE_TTL_S = 15.0
_NEGATIVE_TTL_S = 300.0
_discovery_cache: tuple[float, list[LocalModel], str] | None = None


def discover_local_models(
    *,
    base_url: str | None = None,
    transport: Callable[[str, Any], Any] | None = None,
    timeout: Any = _PROBE_TIMEOUT,
    use_cache: bool = True,
) -> tuple[list[LocalModel], str]:
    """`(models, error)` from the local server. Never raises.

    The error string is returned rather than logged because the UI has to say
    *why* there is nothing to choose from. "Ollama is not running" and "Ollama is
    running but you have not pulled a model" need different next steps from the
    user, and a bare empty list cannot tell them apart.
    """
    global _discovery_cache
    if use_cache and base_url is None and transport is None and _discovery_cache:
        expiry, cached, error = _discovery_cache
        if expiry > time.time():
            return list(cached), error

    target = (base_url or ollama_base_url()).rstrip("/")
    fetch = transport or _default_transport
    models: list[LocalModel] = []
    error = ""
    try:
        payload = fetch(f"{target}/api/tags", timeout)
    except Exception as exc:  # noqa: BLE001 - any failure means "no local models"
        error = _explain_discovery_failure(exc, target)
        payload = None

    if isinstance(payload, dict):
        for entry in payload.get("models") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("model") or entry.get("name") or "").strip()
            if not name:
                continue
            details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
            models.append(
                LocalModel(
                    name=name,
                    size_bytes=int(entry.get("size") or 0),
                    parameter_size=str((details or {}).get("parameter_size") or ""),
                    quantization=str((details or {}).get("quantization_level") or ""),
                )
            )
        if not models and not error:
            error = "Ollama is running but has no models pulled yet."

    models.sort(key=lambda model: (-model.size_bytes, model.name))
    if base_url is None and transport is None:
        ttl = _POSITIVE_TTL_S if models else _NEGATIVE_TTL_S
        _discovery_cache = (time.time() + ttl, list(models), error)
    return models, error


def _explain_discovery_failure(exc: Exception, target: str) -> str:
    """Turn a transport exception into something a person can act on."""
    text = str(exc).lower()
    if "refused" in text or "failed to establish" in text or "connection error" in text:
        return f"Nothing is listening on {target}. Ollama is not running."
    if "timed out" in text or "timeout" in text:
        # Measured on this machine: an unused loopback port drops the connection
        # attempt instead of refusing it, so "not installed" and "installed but
        # wedged" both arrive here. Say the likely thing and name the other.
        return f"{target} did not answer. Ollama is probably not running."
    if "no module named" in text:
        return "The requests library is missing, so the local server cannot be probed."
    return f"Could not read models from {target}: {exc}"


def local_available() -> bool:
    models, _ = discover_local_models()
    return bool(models)


def reset_discovery_cache() -> None:
    """Forget the last probe. Called after the user is told to start Ollama."""
    global _discovery_cache
    _discovery_cache = None


# --- Which server a model lives on -------------------------------------------


def provider_of(model: str) -> str:
    """`local` or `cloud` -- the two servers *this module* knows how to reach.

    Deliberately coarser than `providers.provider_of`, which names the vendor.
    This answers "which client and which timeout", and an id like
    `groq::llama-3.3-70b` is cloud for that purpose even though the vendor is not
    OpenRouter. `_client_for_model` in `ai_engine` makes the finer distinction.
    """
    cleaned = (model or "").strip()
    if cleaned.startswith(LOCAL_PREFIX):
        return PROVIDER_LOCAL
    # `ollama::gemma3:4b` is the same route as `ollama/gemma3:4b`; the newer
    # spelling must not read as a hosted model just because it is newer.
    return PROVIDER_LOCAL if providers.provider_of(cleaned) in _LOCAL_PROVIDER_IDS else PROVIDER_CLOUD


def is_local(model: str) -> bool:
    return provider_of(model) == PROVIDER_LOCAL


def wire_name(model: str) -> str:
    """The name to put in the request body, with the provenance stripped.

    An OpenRouter id like `openai/gpt-4o-mini` keeps its vendor segment, because
    there the slash is part of the real name. A `provider::name` id loses the
    prefix, which is the whole reason `::` was chosen over `/` -- see
    `providers.split_model_id`.
    """
    cleaned = (model or "").strip()
    if providers.SEP in cleaned:
        return providers.wire_name(cleaned)
    return cleaned[len(LOCAL_PREFIX):] if cleaned.startswith(LOCAL_PREFIX) else cleaned


_local_client_cache: dict[str, OpenAI] = {}


def local_client(*, base_url: str | None = None, timeout: float = 120.0) -> OpenAI:
    """An OpenAI-protocol client pointed at Ollama.

    Ollama speaks the OpenAI chat-completions protocol at `/v1`, which is why
    adding it needs no second SDK, no second streaming loop and no second
    sanitiser -- the existing `client.chat.completions.create(..., stream=True)`
    call works unchanged. That compatibility is the whole reason this integration
    is small enough to be worth having.

    `api_key` is required by the SDK and ignored by the server. The timeout is
    long because the first request after a pull loads several gigabytes from disk
    into RAM; the cloud timeout of 12 s would abort a model that was going to
    answer. It is not a latency claim -- this machine has no Ollama installed, so
    nothing here has been measured against a real local generation.
    """
    target = (base_url or ollama_base_url()).rstrip("/")
    key = f"{target}|{timeout}"
    client = _local_client_cache.get(key)
    if client is None:
        client = OpenAI(base_url=f"{target}/v1", api_key="ollama", timeout=timeout, max_retries=0)
        _local_client_cache[key] = client
    return client


# --- The user's choice -------------------------------------------------------
#
# Stored in a file rather than in `.env` or the database. Not `.env`: rewriting
# the file that holds the user's API keys to record a dropdown choice risks their
# credentials for a preference. Not the database: `_openrouter_model_candidates`
# and its callers have no `Session`, and threading one through the whole cascade
# to read a single string would be a large change for a small fact.

PREFERENCE_FILE = Path(__file__).resolve().parents[1] / ".akansha" / "model_route.json"


def read_preference(*, path: Path | None = None) -> dict[str, Any]:
    """`{"preferred": str, "updated_at": str}`. A missing or corrupt file is no
    preference at all, which is the same as never having chosen -- the cascade
    still works, so this must never raise."""
    target = path or PREFERENCE_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"preferred": "", "updated_at": ""}
    if not isinstance(data, dict):
        return {"preferred": "", "updated_at": ""}
    return {
        "preferred": str(data.get("preferred") or "").strip(),
        "updated_at": str(data.get("updated_at") or ""),
    }


def write_preference(model: str, *, path: Path | None = None) -> dict[str, Any]:
    """Record which model to try first. `""` clears it."""
    target = path or PREFERENCE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "preferred": (model or "").strip(),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    target.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def preferred_model(*, path: Path | None = None) -> str:
    return read_preference(path=path)["preferred"]


# --- Building the cascade ----------------------------------------------------


def merge_candidates(
    cloud: Sequence[str],
    *,
    voice: bool = False,
    limit: int | None = None,
    local: Iterable[LocalModel] | None = None,
    preferred: str | None = None,
) -> list[str]:
    """One ordered cascade across both providers.

    Three rules, in order of authority:

    1. **What the user picked goes first.** If they chose a local model in the
       UI, the cloud is their failover; if they chose a cloud model, it keeps the
       position it already had. A preference for something that is no longer
       there -- a model they deleted -- is ignored rather than tried, because a
       404 from the local server would spend a turn proving what discovery
       already knew.

    2. **Cloud before local otherwise.** A 4B model quantised to fit in RAM is
       not as good as the free hosted routes, and defaulting the assistant's
       quality down to protect against an outage that has not happened is the
       wrong trade. Local goes at the back, where it turns "every route failed"
       into an answer.

    3. **Voice keeps its cap**, and local enters the voice cascade only when it
       is what the user asked for or when it is the last thing left. The rule in
       `VOICE_MODELS` is that membership is decided by what a model *emits* --
       a model that streams its reasoning as prose is unusable for speech. No
       local model has been observed emitting anything here, so none of them has
       earned a place in front of a route that has.

    Bigger local models come first among themselves, on the assumption that the
    user pulled a larger model because it answers better. That is an assumption
    about file size, not a benchmark.
    """
    if local is None:
        local, _ = discover_local_models()
    local_ids = [model.model_id for model in local]
    choice = (preferred if preferred is not None else preferred_model()).strip()

    ordered: list[str] = []

    def add(model: str) -> None:
        cleaned = (model or "").strip()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)

    if choice and (not is_local(choice) or choice in local_ids):
        add(choice)

    for model in cloud:
        add(model)

    tail = [model for model in local_ids if model not in ordered]
    if voice:
        # Only as the floor: if any cloud route survived, speech uses it.
        if not [m for m in ordered if not is_local(m)]:
            for model in tail:
                add(model)
    else:
        for model in tail:
            add(model)

    return ordered[:limit] if limit else ordered


def describe_routes(*, cloud: Sequence[str] = (), cloud_configured: bool = False) -> dict[str, Any]:
    """Everything the model-connection UI needs, in one shape.

    Includes the failure string and the suggestions, because "no models" is the
    normal state of a machine that has never installed Ollama and a screen that
    can only render a list has nothing useful to show that user.
    """
    models, error = discover_local_models()
    preference = read_preference()
    return {
        "local": {
            "provider": PROVIDER_LOCAL,
            "base_url": ollama_base_url(),
            "available": bool(models),
            "error": error,
            "models": [model.as_dict() for model in models],
            "suggested": [dict(entry) for entry in SUGGESTED_LOCAL_MODELS],
            "install_url": "https://ollama.com/download",
        },
        "cloud": {
            "provider": PROVIDER_CLOUD,
            "configured": bool(cloud_configured),
            "models": list(cloud),
        },
        "preference": preference,
        "cascade": merge_candidates(cloud, local=models, preferred=preference["preferred"]),
    }
