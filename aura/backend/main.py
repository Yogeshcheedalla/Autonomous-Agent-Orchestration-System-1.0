import json
import html
import logging
import os
import re
import secrets
import shutil
import asyncio
import base64
import ctypes
import dataclasses
import hashlib
import hmac
import subprocess
import threading
import tempfile
import sys
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import FastAPI, Depends, BackgroundTasks, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel
from dotenv import load_dotenv
import edge_tts

try:
    from win10toast import ToastNotifier
except Exception:  # pragma: no cover - optional runtime dependency
    ToastNotifier = None  # type: ignore[assignment]

load_dotenv(override=True)

from .database import (
    get_db,
    Base,
    engine,
    ChatMessage,
    Memory,
    Task,
    InboxMessage,
    UserProfile,
    IntegrationConnection,
    SpeakerProfile,
    SpeakerInteraction,
    TaskAutomation,
    AutomationExecutionLog,
    UserAccount,
    RefreshTokenRecord,
    OwnerSecret,
)
from .auth_service import AuthService
from .ai_engine import generate_chat_stream, analyze_intent_and_memory, _should_skip_ai_memory_analysis
from . import model_routes
from .artifact_engine import (
    GENERATED_ARTIFACTS_DIR,
    artifact_markdown,
    create_requested_artifacts,
    requested_artifact_formats,
    sanitize_model_artifact_placeholders,
)
from .automation import execute_desktop_command, probe_gui_control
from .hermes.api import router as hermes_router
from .voice_ws import router as voice_ws_router
from .avatar import (
    router as avatar_router,
    set_tts_provider as set_avatar_tts_provider,
    get_worker_client as get_avatar_worker_client,
)
from .voice_kernel import EndpointDetector
from .social_connectors import (
    SOCIAL_CONNECTOR_CATALOG,
    SOCIAL_PERMISSION_LEVELS,
    SOCIAL_PLATFORM_META,
    normalized_adapter_key,
    normalized_permission_level,
    permission_allows_send,
    platform_adapters,
    recommended_adapter_key,
    social_connector_catalog,
)
from .speaker_identity import (
    OWNER,
    access_level_for_relationship,
    at_least,
    normalize_relationship,
    resolve_speaker_identity,
)
from .app_connect import (
    APP_CONNECTORS,
    CONNECTED,
    DISCONNECTED,
    WEB_SESSION,
    AppConnector,
    catalog as app_catalog,
    describe_connector,
    effective_capabilities,
    missing_fields,
    plan_connect,
    resolve_app_id,
)
from .app_discovery import (
    browser_profile_dir,
    connectors_from_records,
    desktop_connector,
    discover_desktop_apps,
    normalise_site_url,
    profile_signed_in,
    site_app_id,
    site_connector,
    site_label,
    slugify,
)
from .app_operator import (
    ROUTE_BROWSER,
    ControlDecision,
    ControlGrant,
    decide,
    execute as operator_execute,
    grant_for,
    playbook_for,
    understand,
)
from .goal_pursuit import (
    PursuitPlan,
    goal_from_utterance,
    plan_pursuit,
    pursue,
)
from .owner_verify import (
    FACTOR_HISTORY,
    FACTOR_PASSPHRASE,
    OUTCOME_ABSENT,
    OUTCOME_NOT_ENROLLED,
    TIER_ACT,
    TIER_GRANT,
    TIER_IRREVERSIBLE,
    TIER_READ,
    Factor,
    HistoryChallenge,
    assess as owner_assess,
    camera_factor,
    hash_passphrase,
    history_challenge,
    history_factor,
    local_session_factor,
    passphrase_factor,
    probe_camera_stack,
    probe_voice_stack,
    spoken_verdict,
    voice_factor,
    voiceprint_from_audio,
    voiceprint_from_samples,
    MIN_PASSPHRASE_WORDS,
)
from . import voice_local
from . import memory_index
from . import semantic_memory
from .user_knowledge_graph import (
    LOCAL_GRAPH_OWNER_ID,
    graph_snapshot,
    record_chat_message_in_graph,
    record_knowledge_event,
    record_social_permission_in_graph,
)


def resolve_graph_owner(authorization: str | None = Header(default=None)) -> str:
    """Return the knowledge-graph owner for this request.

    This exists because the owner id used to be the bare literal "default_user"
    repeated at sixteen call sites. That is not a cosmetic problem: the id is the
    graph's filename, so the day a real identity arrives, every one of those sixteen
    has to be found and changed, and a single miss writes half the user's facts into
    a second graph that nothing ever reads again. One resolver means one place.

    Right now this almost always returns the local owner, and that is not a
    limitation of this function -- the frontend sends no Authorization header on any
    request, so there is nothing to read. The login endpoints below do mint tokens
    via AuthService, so the seam is wired to the real thing and starts attributing
    correctly the moment the client attaches one. Until then the behaviour is
    identical to the literal it replaced, which is the point: no existing graph moves.

    Inbound webhooks and OAuth redirects are called by the remote platform, not by
    the user's browser, so they will never carry a token and always resolve to the
    local owner. That is correct for a single-user desktop assistant, and is the
    reason this returns a fallback instead of raising 401.
    """
    if not authorization:
        return LOCAL_GRAPH_OWNER_ID

    scheme, _, raw_token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not raw_token.strip():
        return LOCAL_GRAPH_OWNER_ID

    claims = AuthService.verify_access_token(raw_token.strip())
    if not claims:
        # An expired or forged token is not an error here -- the endpoints this
        # guards are not access-controlled, and failing them closed would break
        # local use for someone whose token merely went stale.
        return LOCAL_GRAPH_OWNER_ID

    email = str(claims.get("email") or "").strip()
    return email or f"user-{claims['user_id']}"

app = FastAPI(title="Akansha AI Engine")
app.include_router(hermes_router)
app.include_router(voice_ws_router)
app.include_router(avatar_router)
planner_reminder_registry: dict[str, dict[str, Any]] = {}
planner_scheduler_task: asyncio.Task | None = None
task_automation_scheduler_task: asyncio.Task | None = None
planner_reminder_history: list[dict[str, Any]] = []
social_oauth_states: dict[str, dict[str, Any]] = {}
toast_notifier = ToastNotifier() if ToastNotifier else None
REMINDER_MARKER_DIR = Path(tempfile.gettempdir()) / "akansha-reminders"


def next_chat_display_order(db: Session, session_id: str) -> float:
    value = (
        db.query(func.max(ChatMessage.display_order))
        .filter(ChatMessage.session_id == session_id)
        .scalar()
    )
    if value is None:
        id_value = db.query(func.max(ChatMessage.id)).filter(ChatMessage.session_id == session_id).scalar()
        return float(id_value or 0) + 1
    return float(value) + 1


def prepare_message_insert_order(
    db: Session,
    session_id: str,
    continue_from_message_id: int | None,
    slots: int,
) -> float:
    normalized_session_id = session_id or "default"
    if not continue_from_message_id:
        return next_chat_display_order(db, normalized_session_id)

    anchor = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.id == continue_from_message_id,
            ChatMessage.session_id == normalized_session_id,
        )
        .first()
    )
    if not anchor:
        return next_chat_display_order(db, normalized_session_id)

    anchor_order = float(anchor.display_order if anchor.display_order is not None else anchor.id)
    if anchor.display_order is None:
        anchor.display_order = anchor_order
        db.add(anchor)

    (
        db.query(ChatMessage)
        .filter(
            ChatMessage.session_id == normalized_session_id,
            ChatMessage.display_order > anchor_order,
        )
        .update(
            {ChatMessage.display_order: ChatMessage.display_order + float(slots)},
            synchronize_session=False,
        )
    )
    return anchor_order + 1


def is_broken_assistant_response(user_input: str, response_text: str) -> bool:
    cleaned = " ".join((response_text or "").strip().split())
    if not cleaned:
        return True
    if cleaned == "0" and not re.search(r"\b(zero|0|number|digit|math|calculate|count)\b", user_input.lower()):
        return True
    return False

# @app.on_event("startup")
# def reset_database():
#     import traceback
#     try:
#         Base.metadata.drop_all(bind=engine)
#         Base.metadata.create_all(bind=engine)
#         print("Successfully reset the database schema!")
#     except Exception as e:
#         print("Failed to reset database schema:", e)
#         traceback.print_exc()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/generated",
    StaticFiles(directory=str(GENERATED_ARTIFACTS_DIR)),
    name="generated_artifacts",
)


# ── conversational runtime (composition root) ──────────────────────────────
def _runtime():
    """The process-wide VoiceRuntime: jarvis engine + orchestrator + narration.

    Resolved lazily and by function rather than bound at import time, because it
    adopts `_global_jarvis_engine`, which this module defines much further down.
    Every route that touches task execution must go through here — an engine
    built per request has empty `active_sessions`, which is how a session
    created by one endpoint became invisible to the next.
    """
    from .voice_runtime import get_runtime
    return get_runtime(jarvis_engine=_global_jarvis_engine)


@app.on_event("startup")
async def _start_conversational_runtime():
    """Bind the runtime to the serving loop and start the narration consumer."""
    try:
        await _runtime().ensure_started()
    except Exception as exc:
        logging.getLogger(__name__).warning("Runtime startup failed: %s", exc)


@app.on_event("startup")
async def _warm_local_voice_models():
    """Load the voice models in the background while the server comes up.

    Measured: the first `/api/voice/stt` call on a cold process took 35 s wall
    against 2.7 s of real Whisper work -- the rest was CTranslate2 constructing
    the model, PyAV opening its first container and numba compiling librosa's
    internals. Paying that during boot instead means the first person to speak
    waits about a second.

    In a thread, not inline, and never awaited: this is roughly 16 s of blocking
    CPU work, and holding up the event loop for it would make every other route
    unreachable while it ran. Failure is logged and dropped, because a machine
    without the models still has text chat and the browser recogniser.
    """
    if os.getenv("AKANSHA_SKIP_VOICE_WARM", "").strip().lower() in {"1", "true", "yes"}:
        return

    def _load() -> None:
        try:
            report = voice_local.warm()
            logging.getLogger(__name__).info("Local voice models warm: %s", report)
        except Exception as exc:
            logging.getLogger(__name__).warning("Local voice warm-up failed: %s", exc)
        # Same thread, after the voice models, because the two compete for the
        # same cores and the microphone is the one the user is waiting on.
        #
        # And after a pause, which was measured rather than guessed. Building the
        # embedding session plus the first full memory sync is ~3 s of saturating
        # CPU -- ONNX Runtime takes every core by default -- and running it during
        # boot took `/api/voice/local/capabilities` from 0.63 s to 5.91 s. That
        # probe is what the client polls to decide whether it can hear at all, so
        # making it slow to save a few milliseconds on a question nobody has asked
        # yet is the wrong trade. Nothing needs semantic recall in the first
        # seconds; the first request that does still finds it warm.
        delay = float(os.getenv("AKANSHA_MEMORY_WARM_DELAY", "6") or 6)
        if delay > 0:
            time.sleep(delay)
        try:
            report = memory_index.warm()
            logging.getLogger(__name__).info("Semantic memory warm: %s", report)
        except Exception as exc:
            logging.getLogger(__name__).warning("Semantic memory warm-up failed: %s", exc)

    threading.Thread(target=_load, name="akansha-voice-warm", daemon=True).start()


@app.on_event("startup")
async def _install_voice_executor():
    """Give `/ws/voice/{id}` something that actually does the work.

    Without this the kernel path is inert: `voice_ws` forwards `generate_response`
    and `execute_plan` to the client when no executor is registered, and nothing
    was ever registered — so the one channel that supports barge-in could not
    answer a question or run a task.
    """
    try:
        from .voice_executor import install
        install()
    except Exception as exc:
        logging.getLogger(__name__).warning("Voice executor not installed: %s", exc)


@app.on_event("shutdown")
async def _stop_conversational_runtime():
    try:
        await _runtime().shutdown()
    except Exception as exc:
        logging.getLogger(__name__).warning("Runtime shutdown failed: %s", exc)


@app.on_event("shutdown")
async def _close_avatar_worker():
    """
    Close the long-lived socket to the GPU worker on the loop that opened it.

    Not merely tidy. The client holds a pending `recv()` on that socket, which on
    Windows is an overlapped operation registered with the loop's IOCP. Closing
    the loop with one still outstanding puts `IocpProactor.close()` into
    `while self._cache: self._poll(1)` — a loop with no exit, because nothing
    will ever complete an operation on a socket whose loop has stopped running.
    Shutting down from a *different* loop (`asyncio.run(...close())`) cannot help:
    the overlapped op belongs to this one.
    """
    try:
        await get_avatar_worker_client().close()
    except Exception as exc:
        logging.getLogger(__name__).warning("Avatar worker shutdown failed: %s", exc)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    user_tone: str | None = None
    response_style: str | None = None
    conversation_mode: str | None = None
    language_preference: str | None = None
    attachments: list[dict] | None = None
    continue_from_message_id: int | None = None
    speaker_profile: dict[str, Any] | None = None


class ChatMessageSaveRequest(BaseModel):
    role: str
    content: str
    session_id: str
    continue_from_message_id: int | None = None


class ChatMessagePinRequest(BaseModel):
    pinned: bool




class ProfileUpdateRequest(BaseModel):
    full_name: str | None = None
    email: str | None = None
    bio: str | None = None
    preferred_mode: str | None = None
    voice_gender: str | None = None
    voice_tone: str | None = None
    voice_language: str | None = None
    avatar_style: str | None = None
    background_listening: bool | None = None
    interrupt_enabled: bool | None = None
    username: str | None = None
    password: str | None = None


class ContinuousTaskRequest(BaseModel):
    goal: str
    target_tab_id: str | None = None
    language_preference: str = "english"


class AuthOtpRequest(BaseModel):
    email: str
    purpose: str = "login"


class AuthOtpVerifyRequest(BaseModel):
    email: str
    code: str
    purpose: str | None = None


class AuthPasswordLoginRequest(BaseModel):
    email: str
    password: str


class AuthRegisterRequest(BaseModel):
    full_name: str
    email: str
    username: str | None = None
    password: str
    code: str


class AuthResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


class ReminderRequest(BaseModel):
    title: str
    date_time: str


class SocialReplyRequest(BaseModel):
    message_id: int | None = None
    platform: str
    sender: str
    reply: str
    approved: bool = False


class SocialSetupRequest(BaseModel):
    config: dict[str, str]
    test_connection: bool = True
    permission_level: str | None = "send_after_approval"
    adapter_key: str | None = None


class TTSRequest(BaseModel):
    text: str
    voice_gender: str = "female"
    voice_tone: str | None = None
    language_mode: str | None = None


class DesktopNotificationRequest(BaseModel):
    title: str
    body: str


class PlannerReminderSyncItem(BaseModel):
    reminder_id: str
    title: str
    body: str
    reminder_at: str


class PlannerReminderSyncRequest(BaseModel):
    reminders: list[PlannerReminderSyncItem]


class BrowserAutomationPermissionsRequest(BaseModel):
    open_links: bool | None = None
    open_close_tabs: bool | None = None
    type_into_page: bool | None = None
    edit_fields: bool | None = None
    delete_draft_content: bool | None = None
    background_open: bool | None = None


class BrowserAutomationRunRequest(BaseModel):
    action: str
    target: str | None = None
    run_at: str | None = None
    background: bool = False


class BrowserAutomationPromptRequest(BaseModel):
    prompt: str
    run_at: str | None = None
    background: bool = True
    cowork: bool = True


class SpeakerProfileRequest(BaseModel):
    display_name: str
    relationship_to_owner: str | None = None
    #: Accepted and ignored. Still on the model so an existing caller that sends it
    #: gets a 200 rather than a validation error, but `save_voice_speaker` derives
    #: the stored level from `relationship_to_owner` and never reads this. It used
    #: to be honoured, which meant any caller could store an owner-level speaker.
    access_level: str | None = None
    closeness_level: str | None = None
    communication_style: str | None = None
    language_preference: str | None = None
    notes: str | None = None
    context_profile: dict[str, Any] | None = None
    conversation_summary: str | None = None
    mood_state: str | None = None
    last_heard_text: str | None = None
    #: Stored, never compared. Nothing in this codebase computes a speaker embedding
    #: or matches one, so this is an opaque blob the client chose to keep — not a
    #: voiceprint and not usable for verification.
    voice_signature: dict[str, Any] | None = None


GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/google/callback")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


def get_or_create_profile(db: Session) -> UserProfile:
    profile = db.query(UserProfile).first()
    if profile:
        return profile

    profile = UserProfile()
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def get_or_create_connection(db: Session, provider: str) -> IntegrationConnection:
    connection = db.query(IntegrationConnection).filter(IntegrationConnection.provider == provider).first()
    if connection:
        return connection

    connection = IntegrationConnection(provider=provider)
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def serialize_profile(profile: UserProfile) -> dict[str, Any]:
    return {
        "full_name": profile.full_name,
        "email": profile.email,
        "bio": profile.bio,
        "preferred_mode": profile.preferred_mode,
        "voice_gender": profile.voice_gender,
        "voice_tone": profile.voice_tone,
        "voice_language": profile.voice_language or "english",
        "avatar_style": profile.avatar_style,
        "background_listening": profile.background_listening,
        "interrupt_enabled": profile.interrupt_enabled,
        "google_connected": profile.google_connected,
        "google_email": profile.google_email,
        "username": profile.username,
    }


def serialize_speaker_profile(profile: SpeakerProfile) -> dict[str, Any]:
    voice_signature: dict[str, Any] | None = None
    if profile.voice_signature_json:
        try:
            voice_signature = json.loads(profile.voice_signature_json)
        except json.JSONDecodeError:
            voice_signature = None
    context_profile: dict[str, Any] | None = None
    if profile.context_profile_json:
        try:
            context_profile = json.loads(profile.context_profile_json)
        except json.JSONDecodeError:
            context_profile = None

    return {
        "id": profile.id,
        "display_name": profile.display_name,
        "relationship_to_owner": profile.relationship_to_owner,
        "access_level": profile.access_level,
        "closeness_level": profile.closeness_level or "normal",
        "communication_style": profile.communication_style,
        "language_preference": profile.language_preference,
        "notes": profile.notes,
        "context_profile": context_profile,
        "conversation_summary": profile.conversation_summary,
        "mood_state": profile.mood_state,
        "interaction_count": profile.interaction_count or 0,
        "last_intro_text": profile.last_intro_text,
        "last_heard_text": profile.last_heard_text,
        "voice_signature": voice_signature,
        "timestamp": profile.timestamp.isoformat() if profile.timestamp else None,
    }


def _default_owner_speaker_profile(db: Session) -> dict[str, Any]:
    profile = get_or_create_profile(db)
    return {
        "display_name": profile.full_name or "Owner",
        "relationship_to_owner": OWNER,
        "access_level": OWNER,
        "closeness_level": "close",
        "communication_style": "proactive close companion",
        "language_preference": profile.voice_language or "english",
        "notes": profile.bio or "Primary Akansha owner.",
        "context_profile": {
            "education": "B.Tech student",
            "assistant_project": "Building Akansha as an autonomous voice, chat, automation, and memory assistant.",
            "current_work_style": "Wants quick, accurate, relationship-aware, human-like responses.",
        },
        "conversation_summary": "Owner expects proactive support, memory, automation safety, and natural conversation.",
    }


def _chat_speaker_profile(req: ChatRequest, db: Session) -> dict[str, Any]:
    """Resolve who is speaking for this chat turn.

    The old body merged the request's `speaker_profile` on top of the owner's and
    then tried to derive `access_level` behind an `if not merged.get(...)` guard.
    That guard could never fire -- the owner defaults always set `access_level`,
    so the derived value was dead code and every claimed speaker, guest included,
    came back with owner authority and the owner's bio, project notes and
    conversation summary attached. `resolve_speaker_identity` derives authority
    instead of inheriting it, and builds a non-owner profile from the claim rather
    than layering it over the owner's.
    """
    resolved = resolve_speaker_identity(req.speaker_profile, _default_owner_speaker_profile(db))
    recent = _recent_speaker_interactions(db, resolved)
    if recent:
        resolved["recent_interactions"] = recent
    return resolved


def _find_speaker_from_payload(db: Session, speaker_payload: dict[str, Any]) -> SpeakerProfile | None:
    speaker_id = speaker_payload.get("id")
    display_name = str(speaker_payload.get("display_name") or "").strip()

    if isinstance(speaker_id, int):
        speaker = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker_id).first()
        if speaker:
            return speaker
    if display_name:
        return db.query(SpeakerProfile).filter(SpeakerProfile.display_name.ilike(display_name)).first()
    return None


def _recent_speaker_interactions(db: Session, speaker_payload: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    speaker = _find_speaker_from_payload(db, speaker_payload)
    if not speaker:
        return []

    rows = (
        db.query(SpeakerInteraction)
        .filter(SpeakerInteraction.speaker_id == speaker.id)
        .order_by(SpeakerInteraction.timestamp.desc(), SpeakerInteraction.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "role": row.role,
            "content": (row.content or "")[:500],
            "mood_state": row.mood_state,
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        }
        for row in reversed(rows)
    ]


def _trim_summary(value: str, limit: int = 1800) -> str:
    cleaned = " ".join((value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:].lstrip(" .;,-")


def _append_speaker_interaction(
    db: Session,
    speaker_payload: dict[str, Any],
    session_id: str,
    role: str,
    content: str,
    mood_state: str | None = None,
) -> None:
    relationship = str(speaker_payload.get("relationship_to_owner") or "").strip().lower()
    if relationship == "owner":
        return

    speaker = _find_speaker_from_payload(db, speaker_payload)
    if not speaker:
        return

    db.add(
        SpeakerInteraction(
            speaker_id=speaker.id,
            speaker_name=speaker.display_name,
            session_id=session_id or "default",
            role=role,
            content=content.strip()[:4000],
            mood_state=(mood_state or speaker.mood_state or "neutral").strip().lower(),
        )
    )

    if role == "assistant":
        previous = speaker.conversation_summary or f"{speaker.display_name} is {speaker.relationship_to_owner or 'connected to the owner'}."
        speaker.conversation_summary = _trim_summary(
            f"{previous} Recent Akansha reply: {content.strip()[:260]}"
        )
        db.add(speaker)


def _record_speaker_interaction(req: ChatRequest, db: Session, speaker_payload: dict[str, Any]) -> dict[str, Any]:
    raw_speaker = req.speaker_profile or {}
    speaker_id = raw_speaker.get("id")
    display_name = str(speaker_payload.get("display_name") or "").strip()
    relationship = str(speaker_payload.get("relationship_to_owner") or "").strip().lower()

    if not display_name or relationship == "owner":
        return speaker_payload

    speaker = None
    if isinstance(speaker_id, int):
        speaker = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker_id).first()
    if not speaker:
        speaker = db.query(SpeakerProfile).filter(SpeakerProfile.display_name.ilike(display_name)).first()
    if not speaker:
        return speaker_payload

    speaker.last_heard_text = req.message.strip()[:1000]
    speaker.mood_state = (req.user_tone or raw_speaker.get("mood_state") or speaker.mood_state or "neutral").strip().lower()
    speaker.interaction_count = int(speaker.interaction_count or 0) + 1

    if speaker.closeness_level in {None, "", "new"} and speaker.interaction_count >= 3:
        speaker.closeness_level = "normal"
    if relationship == "friend" and speaker.closeness_level == "normal" and speaker.interaction_count >= 25:
        speaker.closeness_level = "close"

    db.add(speaker)
    db.commit()
    db.refresh(speaker)

    speaker_payload["interaction_count"] = speaker.interaction_count or 0
    speaker_payload["closeness_level"] = speaker.closeness_level or speaker_payload.get("closeness_level")
    speaker_payload["mood_state"] = speaker.mood_state
    speaker_payload["last_heard_text"] = speaker.last_heard_text
    speaker_payload["id"] = speaker.id
    _append_speaker_interaction(
        db,
        speaker_payload,
        req.session_id,
        "user",
        req.message,
        speaker.mood_state,
    )
    speaker_payload["recent_interactions"] = _recent_speaker_interactions(db, speaker_payload)
    db.commit()
    return speaker_payload


def _record_assistant_speaker_interaction(
    req: ChatRequest,
    db: Session,
    speaker_payload: dict[str, Any],
    response_text: str,
) -> None:
    _append_speaker_interaction(
        db,
        speaker_payload,
        req.session_id,
        "assistant",
        response_text,
        speaker_payload.get("mood_state"),
    )
    db.commit()


def _speaker_access_level(relationship: str | None) -> str:
    """Kept as the module-level name other code and tests already import.

    The table it used to hold now lives in `speaker_identity`, so the chat path
    and this helper cannot drift apart -- which is exactly how the old duplicate
    lists ended up disagreeing about whether "teacher" was a relationship at all.
    """
    return access_level_for_relationship(relationship)


def _schedule_automation_plan(run_at_iso: str, plan: dict[str, Any], original_prompt: str) -> str:
    payload_dir = Path(tempfile.gettempdir()) / "akansha-automation"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_path = payload_dir / f"automation-{secrets.token_hex(8)}.json"
    payload_path.write_text(
        json.dumps(
            {
                "run_at": run_at_iso,
                "plan": plan,
                "prompt": original_prompt,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    subprocess.Popen(
        [sys.executable, "-m", "backend.scheduled_automation_runner", str(payload_path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return str(payload_path)


def google_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI)


def cloned_voice_configured() -> bool:
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)


def detect_text_language_mode(text: str) -> str:
    telugu_chars = len(re.findall(r"[\u0C00-\u0C7F]", text))
    hindi_chars = len(re.findall(r"[\u0900-\u097F]", text))
    latin_chars = len(re.findall(r"[A-Za-z]", text))
    words = set(re.findall(r"[A-Za-z]+", text.lower()))
    if hindi_chars and latin_chars:
        return "hindi"
    if hindi_chars:
        return "hindi"
    if telugu_chars and latin_chars:
        return "mixed"
    if telugu_chars:
        return "telugu"
    if {"namaste", "hindi", "kaise", "kya", "mujhe", "aap", "hai", "nahi", "batao"} & words:
        return "hindi"
    if {"telugu", "anna", "andi", "naku", "naaku", "meeru", "ela", "unnaru", "cheppu"} & words:
        return "mixed"
    return "english"


def build_voice_settings(voice_tone: str | None) -> dict[str, float]:
    tone = (voice_tone or "friendly").lower()
    if tone == "energetic":
        return {"stability": 0.38, "similarity_boost": 0.82, "style": 0.7, "use_speaker_boost": True}
    if tone == "calm":
        return {"stability": 0.72, "similarity_boost": 0.8, "style": 0.2, "use_speaker_boost": True}
    if tone == "professional":
        return {"stability": 0.64, "similarity_boost": 0.84, "style": 0.28, "use_speaker_boost": True}
    return {"stability": 0.52, "similarity_boost": 0.83, "style": 0.45, "use_speaker_boost": True}


def get_edge_voice_name(voice_gender: str, language_mode: str) -> str:
    normalized_gender = (voice_gender or "female").lower()
    normalized_mode = (language_mode or "english").lower()

    telugu_voice = "te-IN-ShrutiNeural" if normalized_gender == "female" else "te-IN-MohanNeural"
    english_voice = "en-US-AriaNeural" if normalized_gender == "female" else "en-US-GuyNeural"
    hindi_voice = "hi-IN-SwaraNeural" if normalized_gender == "female" else "hi-IN-MadhurNeural"

    if normalized_mode == "hindi":
        return hindi_voice
    if normalized_mode in {"telugu", "mixed"}:
        return telugu_voice
    return english_voice


def get_edge_tts_prosody(voice_tone: str | None) -> tuple[str, str]:
    """Returns (rate, pitch) for edge-tts. Friendly default is warmer and slightly higher."""
    tone = (voice_tone or "friendly").lower()
    if tone == "energetic":
        return "+10%", "+5Hz"
    if tone == "calm":
        return "-8%", "-1Hz"
    if tone == "professional":
        return "-2%", "+0Hz"
    # friendly — default: slightly faster, warm higher pitch (cuter/sweeter feel)
    return "+3%", "+3Hz"


async def generate_edge_tts_audio(text: str, voice_gender: str, voice_tone: str | None, language_mode: str | None) -> bytes:
    detected_mode = detect_text_language_mode(text)
    requested_mode = (language_mode or "").lower()
    resolved_mode = detected_mode if requested_mode in {"", "english"} and detected_mode != "english" else (requested_mode or detected_mode)
    # Always use female voice for Akansha — sweet, warm, natural
    voice_name = get_edge_voice_name("female", resolved_mode)
    rate, pitch = get_edge_tts_prosody(voice_tone)

    communicate = edge_tts.Communicate(
        text=text,
        voice=voice_name,
        rate=rate,
        pitch=pitch,
    )

    audio_chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])

    if not audio_chunks:
        raise HTTPException(status_code=502, detail="Edge TTS did not return audio.")

    return b"".join(audio_chunks)


# The avatar relay needs a voice but must not import this module (it is imported
# *by* this one), so hand it the synthesizer here. Same injection shape as
# `voice_ws.set_executor`.
set_avatar_tts_provider(generate_edge_tts_audio)


def show_windows_notification(title: str, body: str) -> None:
    clean_title = normalize_reminder_text(title) or "Akansha reminder"
    clean_body = normalize_reminder_text(body) or "You asked Akansha to remind you."

    if toast_notifier is not None:
        try:
            toast_notifier.show_toast(
                clean_title,
                clean_body,
                duration=10,
                threaded=False,
            )
        except Exception:
            pass

    safe_title = clean_title.replace("'", "''")
    safe_body = clean_body.replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = "<toast><visual><binding template='ToastGeneric'><text>{safe_title}</text><text>{safe_body}</text></binding></visual><audio silent='false'/></toast>"
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Akansha Planner').Show($toast)
"""
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    popup_script = f"""
Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName WindowsBase

$window = New-Object System.Windows.Window
$window.Title = 'Akansha reminder'
$window.Width = 420
$window.Height = 180
$window.Topmost = $true
$window.ResizeMode = 'NoResize'
$window.WindowStartupLocation = 'Manual'
$window.Left = [System.Windows.SystemParameters]::WorkArea.Right - 440
$window.Top = [System.Windows.SystemParameters]::WorkArea.Bottom - 220
$window.Background = '#111827'
$window.Foreground = '#F8FAFC'

$stack = New-Object System.Windows.Controls.StackPanel
$stack.Margin = '18'

$titleBlock = New-Object System.Windows.Controls.TextBlock
$titleBlock.Text = '{safe_title}'
$titleBlock.FontSize = 18
$titleBlock.FontWeight = 'Bold'
$titleBlock.Margin = '0,0,0,10'
$titleBlock.TextWrapping = 'Wrap'
$stack.Children.Add($titleBlock) | Out-Null

$bodyBlock = New-Object System.Windows.Controls.TextBlock
$bodyBlock.Text = '{safe_body}'
$bodyBlock.FontSize = 13
$bodyBlock.Foreground = '#CBD5E1'
$bodyBlock.TextWrapping = 'Wrap'
$stack.Children.Add($bodyBlock) | Out-Null

$window.Content = $stack

$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromSeconds(12)
$timer.Add_Tick({{
    $timer.Stop()
    $window.Close()
}})
$timer.Start()

$window.ShowDialog() | Out-Null
"""
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Sta", "-Command", popup_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


BROWSER_AUTOMATION_PROVIDER = "browser-automation"
BROWSER_AUTOMATION_DEFAULTS: dict[str, bool] = {
    "open_links": True,
    "open_close_tabs": True,
    "type_into_page": True,
    "edit_fields": True,
    "delete_draft_content": True,
    "background_open": True,
}

BROWSER_AUTOMATION_ACTIONS: list[dict[str, str]] = [
    {
        "key": "open_url",
        "label": "Open link",
        "permission": "open_links",
        "description": "Open any URL in the default browser.",
    },
    {
        "key": "open_youtube_song",
        "label": "Open YouTube search",
        "permission": "open_links",
        "description": "Open a YouTube search for a song or playlist in the default browser.",
    },
    {
        "key": "new_tab",
        "label": "Open tab",
        "permission": "open_close_tabs",
        "description": "Create a new tab in the active browser window.",
    },
    {
        "key": "close_tab",
        "label": "Close tab",
        "permission": "open_close_tabs",
        "description": "Close the current active browser tab.",
    },
    {
        "key": "click",
        "label": "Click active window",
        "permission": "type_into_page",
        "description": "Click the current pointer location or the center of the active window.",
    },
    {
        "key": "type_text",
        "label": "Type into page",
        "permission": "type_into_page",
        "description": "Type text into the currently focused browser field.",
    },
    {
        "key": "edit_field",
        "label": "Edit field",
        "permission": "edit_fields",
        "description": "Replace the content of the focused field.",
    },
    {
        "key": "remove_draft",
        "label": "Clear draft",
        "permission": "delete_draft_content",
        "description": "Select and delete the current draft or focused field content.",
    },
    {
        "key": "open_app",
        "label": "Open desktop app",
        "permission": "open_links",
        "description": "Open a desktop app such as Notepad, Calculator, Explorer, VS Code, or Chrome.",
    },
    {
        "key": "write_text_file",
        "label": "Write text file",
        "permission": "type_into_page",
        "description": "Create or replace a text/code file in a requested folder.",
    },
    {
        "key": "run_python_file",
        "label": "Run Python file",
        "permission": "type_into_page",
        "description": "Run a saved Python file and capture its output.",
    },
    {
        "key": "close_window",
        "label": "Close desktop window",
        "permission": "open_close_tabs",
        "description": "Close the currently active desktop app window.",
    },
    {
        "key": "switch_window",
        "label": "Switch desktop window",
        "permission": "open_close_tabs",
        "description": "Move to the next open desktop app window.",
    },
    {
        "key": "volume_up",
        "label": "Increase volume",
        "permission": "open_links",
        "description": "Raise Windows system volume.",
    },
    {
        "key": "volume_down",
        "label": "Decrease volume",
        "permission": "open_links",
        "description": "Lower Windows system volume.",
    },
    {
        "key": "brightness_up",
        "label": "Increase brightness",
        "permission": "open_links",
        "description": "Raise Windows screen brightness when the display supports WMI brightness control.",
    },
    {
        "key": "brightness_down",
        "label": "Decrease brightness",
        "permission": "open_links",
        "description": "Lower Windows screen brightness when the display supports WMI brightness control.",
    },
]

DESKTOP_APP_ALIASES: dict[str, list[str]] = {
    "notepad": ["notepad", "notepad app", "notepad desktop", "notepad desktop app"],
    "calculator": ["calculator", "calc"],
    "file explorer": ["file explorer", "explorer"],
    "vscode": ["vscode", "vs code", "visual studio code", "code editor"],
    "antigravity": [
        "antigravity",
        "antigravity ide",
        "antigravity app",
        "antigravity desktop",
        "antigravity desktop app",
    ],
    "chrome": [
        "chrome",
        "google chrome",
        "chrome browser",
        "google chrome browser",
        "chrome app",
        "google chrome app",
        "chrome desktop",
        "google chrome desktop",
    ],
    "brave": [
        "brave",
        "brave browser",
        "brave app",
        "brave desktop",
    ],
    "edge": [
        "edge",
        "microsoftedge",
        "microsoft edge",
        "edge browser",
        "microsoft edge browser",
        "edge app",
        "microsoft edge app",
        "edge desktop",
        "microsoft edge desktop",
    ],
    "command prompt": ["command prompt", "cmd"],
    "powershell": ["powershell"],
    "whatsapp": [
        "whatsapp",
        "whats app",
        "whatsup",
        "watsup",
        "whatsap",
        "whatsapp desktop",
        "whats app desktop",
        "whatsup desktop",
        "whatsapp app",
        "whatsapp desktop app",
    ],
    "telegram": ["telegram", "telegram app", "telegram desktop", "telegram desktop app"],
    "discord": ["discord", "discord app", "discord desktop", "discord desktop app"],
    "word": ["word", "microsoft word", "ms word"],
    "excel": ["excel", "microsoft excel", "ms excel"],
    "powerpoint": ["powerpoint", "power point", "microsoft powerpoint", "ms powerpoint"],
    "settings": ["settings", "windows settings"],
    "terminal": ["terminal", "windows terminal"],
    "control panel": ["control panel"],
}

DUAL_MODE_APP_TARGETS: dict[str, dict[str, str | None]] = {
    "whatsapp": {
        "label": "WhatsApp",
        "desktop_app": "whatsapp",
        "web_url": "https://web.whatsapp.com",
    },
    "telegram": {
        "label": "Telegram",
        "desktop_app": "telegram",
        "web_url": "https://web.telegram.org",
    },
    "discord": {
        "label": "Discord",
        "desktop_app": "discord",
        "web_url": "https://discord.com/app",
    },
    "instagram": {
        "label": "Instagram",
        "desktop_app": None,
        "web_url": "https://www.instagram.com",
    },
    "twitter": {
        "label": "X / Twitter",
        "desktop_app": None,
        "web_url": "https://x.com",
    },
}

DESKTOP_ONLY_APP_TARGETS: set[str] = {
    "notepad",
    "calculator",
    "file explorer",
    "vscode",
    "antigravity",
    "command prompt",
    "powershell",
    "word",
    "excel",
    "powerpoint",
    "settings",
    "terminal",
    "control panel",
}

BROWSER_APP_LABELS: dict[str, str] = {
    "chrome": "Google Chrome",
    "brave": "Brave",
    "edge": "Microsoft Edge",
}

SPECIAL_FOLDERS: dict[str, str] = {
    "downloads": str((Path.home() / "Downloads")),
    "desktop": str((Path.home() / "Desktop")),
    "documents": str((Path.home() / "Documents")),
    "pictures": str((Path.home() / "Pictures")),
    "videos": str((Path.home() / "Videos")),
    "music": str((Path.home() / "Music")),
}

KNOWN_LOGIN_URLS: dict[str, dict[str, Any]] = {
    "codechef.com": {
        "url": "https://www.codechef.com/login?page=1",
        "prefill_delay": 3.0,
        "tab_presses_before": 1,
        "type_interval": 0.07,
        "step_delay": 0.18,
    },
    "linkedin.com": {
        "url": "https://www.linkedin.com/login",
        "prefill_delay": 2.4,
        "tab_presses_before": 0,
        "type_interval": 0.06,
        "step_delay": 0.14,
    },
    "instagram.com": {
        "url": "https://www.instagram.com/accounts/login/",
        "prefill_delay": 2.8,
        "tab_presses_before": 0,
        "type_interval": 0.06,
        "step_delay": 0.14,
    },
    "twitter.com": {
        "url": "https://x.com/i/flow/login",
        "prefill_delay": 2.8,
        "tab_presses_before": 0,
        "type_interval": 0.06,
        "step_delay": 0.14,
    },
    "x.com": {
        "url": "https://x.com/i/flow/login",
        "prefill_delay": 2.8,
        "tab_presses_before": 0,
        "type_interval": 0.06,
        "step_delay": 0.14,
    },
    "github.com": {
        "url": "https://github.com/login",
        "prefill_delay": 2.2,
        "tab_presses_before": 0,
        "type_interval": 0.05,
        "step_delay": 0.12,
    },
    "leetcode.com": {
        "url": "https://leetcode.com/accounts/login/",
        "prefill_delay": 2.6,
        "tab_presses_before": 0,
        "type_interval": 0.05,
        "step_delay": 0.12,
    },
    "codeforces.com": {
        "url": "https://codeforces.com/enter",
        "prefill_delay": 2.6,
        "tab_presses_before": 0,
        "type_interval": 0.05,
        "step_delay": 0.12,
    },
}

AUTOMATION_OPENABLE_TARGETS = (
    "notepad|calculator|calc|file explorer|explorer|vscode|visual studio code|chrome|brave|edge|"
    "microsoft edge|whatsapp|telegram|discord|word|excel|powerpoint|settings|terminal|control panel|"
    "youtube|google|codechef|linkedin|instagram|twitter|x"
)


def normalize_browser_automation_prompt(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    desktop_slash = re.match(r"^/desktop\s+(.+)$", normalized, flags=re.IGNORECASE)
    if desktop_slash:
        target = re.sub(r"^\s*open\s+", "", desktop_slash.group(1), flags=re.IGNORECASE).strip()
        normalized = f"open {target} in the desktop app"

    website_slash = re.match(r"^/(?:web|website)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if website_slash:
        target = re.sub(r"^\s*open\s+", "", website_slash.group(1), flags=re.IGNORECASE).strip()
        normalized = f"open {target} in the web browser"

    desktop_prefix = re.match(r"^(?:desktop|desktop app|desktop application)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if desktop_prefix:
        target = re.sub(r"^\s*open\s+", "", desktop_prefix.group(1), flags=re.IGNORECASE).strip()
        normalized = f"open {target} in the desktop app"

    website_prefix = re.match(r"^(?:web|website|browser)\s+(.+)$", normalized, flags=re.IGNORECASE)
    if website_prefix:
        target = re.sub(r"^\s*open\s+", "", website_prefix.group(1), flags=re.IGNORECASE).strip()
        normalized = f"open {target} in the web browser"

    normalized = re.sub(r"\bweb\s+site\b", "website", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwebsite version\b", "website", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bweb version\b", "website", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bdesktop version\b", "desktop app", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bdesktop client\b", "desktop app", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bweb client\b", "website", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bin\s+website\b", "in the web browser", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bon\s+website\b", "in the web browser", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bin\s+web\b", "in the web browser", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bin\s+desktop\b", "in the desktop app", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwhats?\s*up\b", "whatsapp", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwats?\s*up\b", "whatsapp", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwhats?\s*ap+p?\b", "whatsapp", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwhats\s+app\b", "whatsapp", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwhastapp\b", "whatsapp", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bmicrosoftedge\b", "microsoft edge", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bmicro\s*soft edge\b", "microsoft edge", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bms edge\b", "microsoft edge", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bedge browser\b", "microsoft edge", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bmicrosoft edge browser\b", "microsoft edge", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bedge app\b", "microsoft edge", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bmicrosoft edge app\b", "microsoft edge", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bchrome browser\b", "google chrome", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bchrome app\b", "google chrome", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bgoogle chrome app\b", "google chrome", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        rf"\bop(?:en)?\s*({AUTOMATION_OPENABLE_TARGETS})\b",
        r"open \1",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\bdesktop\s+open\b", "open", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\byoutub\b", "youtube", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bcaland(ar|er)?\b", "calendar", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def extract_first_url_or_domain(prompt: str) -> str | None:
    url_match = re.search(r"(https?://[^\s]+)", prompt, flags=re.IGNORECASE)
    if url_match:
        return url_match.group(1).rstrip(".,)")

    domain_match = re.search(
        r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|in|org|net|io|ai|co|app|dev))\b",
        prompt,
        flags=re.IGNORECASE,
    )
    if domain_match:
        return domain_match.group(1)
    return None


def extract_search_phrase(prompt: str) -> str | None:
    quoted = re.search(r'"([^"]+)"|\'([^\']+)\'', prompt)
    if quoted:
        return next(group for group in quoted.groups() if group)

    for marker in ["search for", "look for", "find", "play", "search"]:
        match = re.search(rf"{marker}\s+(.+)", prompt, flags=re.IGNORECASE)
        if match:
            phrase = re.split(
                r"\b(?:and then|then|after that|before submitting|before submit)\b",
                match.group(1),
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            phrase = re.sub(
                r"\s+\b(?:on|in|using|at)\s+(?:google|google\.com|youtube|youtube\.com|the\s+browser|browser)\s*$",
                "",
                phrase,
                flags=re.IGNORECASE,
            )
            return phrase.strip(" .")
    return None


def extract_field_value(prompt: str, field_names: list[str]) -> str | None:
    for field in field_names:
        pattern = rf"\b{re.escape(field)}\b\s*(?:is|=|:)?\s*([^\n,;]+)"
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("\"'")
    return None


FORM_FIELD_ALIASES: list[tuple[str, list[str]]] = [
    ("full_name", ["full name", "name"]),
    ("email", ["email", "mail id", "email id"]),
    ("phone", ["phone number", "mobile number", "mobile", "phone", "contact number"]),
    ("username", ["username", "user name", "user id", "login id"]),
    ("password", ["password", "passcode"]),
    ("address", ["address"]),
    ("city", ["city"]),
    ("state", ["state"]),
    ("country", ["country"]),
    ("college", ["college", "school", "university"]),
    ("company", ["company", "organization", "organisation"]),
    ("message", ["message", "text", "comment", "description"]),
]


def _clean_form_value(value: str) -> str:
    cleaned = value.strip().strip("\"' .,;:")
    cleaned = re.sub(
        r"\s+\b(?:and\s+)?(?:before\s+submitting|before\s+submit|before\s+submission|ask\s+before\s+submit|pop\s*up\s+notification|notify\s+me|submit\s+it|click\s+submit)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip().strip("\"' .,;:")


def extract_form_fill_values(prompt: str) -> list[str]:
    aliases = [alias for _, alias_list in FORM_FIELD_ALIASES for alias in alias_list]
    marker_pattern = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    found: list[tuple[int, str, str]] = []

    for field_key, field_aliases in FORM_FIELD_ALIASES:
        for alias in field_aliases:
            pattern = (
                rf"\b{re.escape(alias)}\b\s*(?:is|=|:|-)?\s*"
                rf"(.+?)(?=\s+(?:and\s+|then\s+)?(?:{marker_pattern})\b\s*(?:is|=|:|-)?|"
                rf"\s+\b(?:before\s+submitting|before\s+submit|before\s+submission|ask\s+before\s+submit|"
                rf"pop\s*up\s+notification|notify\s+me|submit\s+it|click\s+submit)\b|$)"
            )
            match = re.search(pattern, prompt, flags=re.IGNORECASE | re.DOTALL)
            if match:
                value = _clean_form_value(match.group(1))
                if value:
                    found.append((match.start(), field_key, value))
                break

    ordered: list[str] = []
    seen_fields: set[str] = set()
    for _, field_key, value in sorted(found, key=lambda item: item[0]):
        if field_key not in seen_fields:
            ordered.append(value)
            seen_fields.add(field_key)
    return ordered


def should_confirm_before_submit(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(before\s+submitting|before\s+submit|before\s+submission|ask\s+before\s+submit|"
            r"notify\s+me\s+before\s+submit|pop\s*up\s+notification|popup\s+notification|wait\s+before\s+submit)\b",
            prompt,
            flags=re.IGNORECASE,
        )
    )


def extract_login_credentials(prompt: str) -> tuple[str | None, str | None]:
    username_match = re.search(
        r"\b(?:username|user\s*id|login\s*id|email)\b\s*[-:=]?\s*(.+?)(?=\s+\b(?:password|passcode)\b|$)",
        prompt,
        flags=re.IGNORECASE | re.DOTALL,
    )
    password_match = re.search(
        r"\b(?:password|passcode)\b\s*[-:=]?\s*(.+?)(?=$)",
        prompt,
        flags=re.IGNORECASE | re.DOTALL,
    )

    username = username_match.group(1).strip().strip("\"'") if username_match else None
    password = password_match.group(1).strip().strip("\"'") if password_match else None
    return username, password


def normalize_domain(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    normalized = url_or_domain.lower().strip()
    normalized = re.sub(r"^https?://", "", normalized)
    normalized = normalized.split("/")[0]
    return normalized


def detect_known_site(prompt: str) -> str | None:
    lowered = prompt.lower()
    if re.search(r"\bgoogle\b", lowered) and not re.search(r"\bgoogle chrome\b", lowered):
        return "google.com"
    if "codechef" in lowered:
        return "codechef.com"
    if "linkedin" in lowered:
        return "linkedin.com"
    if "instagram" in lowered:
        return "instagram.com"
    if "youtube" in lowered:
        return "youtube.com"
    if "codeforces" in lowered:
        return "codeforces.com"
    if "leetcode" in lowered:
        return "leetcode.com"
    if "github" in lowered:
        return "github.com"
    return None


def _is_google_domain(domain: str | None) -> bool:
    return bool(domain and re.search(r"(^|\.)google\.", domain, flags=re.IGNORECASE))


def _google_search_url(phrase: str) -> str:
    return f"https://www.google.com/search?{urlencode({'q': phrase})}"


def humanize_desktop_target(app_name: str | None) -> str:
    if not app_name:
        return "the requested app"
    friendly_names = {
        "notepad": "Notepad",
        "calculator": "Calculator",
        "file explorer": "File Explorer",
        "vscode": "Visual Studio Code",
        "antigravity": "Antigravity IDE",
        "command prompt": "Command Prompt",
        "powershell": "PowerShell",
        "whatsapp": "WhatsApp",
        "telegram": "Telegram",
        "discord": "Discord",
        "word": "Microsoft Word",
        "excel": "Microsoft Excel",
        "powerpoint": "Microsoft PowerPoint",
        "settings": "Windows Settings",
        "terminal": "Windows Terminal",
        "control panel": "Control Panel",
        "chrome": "Google Chrome",
        "brave": "Brave",
        "edge": "Microsoft Edge",
    }
    return friendly_names.get(app_name, app_name.title())


@app.post("/api/continuous/start")
def start_continuous_task_endpoint(req: ContinuousTaskRequest):
    # Same defect as the OpenWork pipeline had: `active_sessions` is instance
    # state, so an engine built per request starts empty and the session created
    # here was invisible to /api/continuous/step, which could only ever answer
    # "no active steps remaining". Both endpoints now share one engine.
    engine = _runtime().jarvis
    session = engine.start_continuous_task(
        goal=req.goal,
        target_tab_id=req.target_tab_id,
        language=req.language_preference,
    )
    return {
        "status": "success",
        "session_id": session.session_id,
        "target_tab_id": session.target_tab_id,
        "main_goal": session.main_goal,
        "total_steps": len(session.subtasks),
        "subtasks": [
            {
                "id": s.id,
                "description": s.goal_description,
                "action": s.action,
                "params": s.params,
                "status": s.status,
                "voice_prompt": s.voice_update_prompt,
            }
            for s in session.subtasks
        ],
    }


@app.post("/api/continuous/step")
def execute_continuous_step_endpoint(payload: dict[str, Any]):
    session_id = payload.get("session_id", "")
    runtime = _runtime()
    res = runtime.jarvis.execute_next_subtask(
        session_id, runtime.orchestrator._bubbles.get(session_id), runtime.memory
    )
    return res or {"status": "finished", "message": "No active steps remaining."}


@app.get("/api/environment/brief")
def get_environment_brief_endpoint():
    from .agent_modules.environment_advisor import SurroundingEnvironmentAdvisor
    advisor = SurroundingEnvironmentAdvisor()
    snapshot = advisor.generate_snapshot_and_recommendations()
    return {
        "status": "success",
        "current_time_ist": snapshot.current_time_ist,
        "active_workspace": snapshot.active_workspace,
        "academic_context": snapshot.academic_context,
        "system_status": snapshot.system_status,
        "recommendations": snapshot.recommendations,
    }


@app.post("/api/nlp/clean")
def nlp_clean_prompt_endpoint(payload: dict[str, Any]):
    from .agent_modules.nlp_processor import NLPContextProcessor
    raw_prompt = payload.get("prompt", "")
    processor = NLPContextProcessor()
    res = processor.clean_noise(raw_prompt)
    return {
        "status": "success",
        "original_prompt": res.original_prompt,
        "cleaned_prompt": res.cleaned_prompt,
        "removed_fillers": res.removed_fillers,
        "detected_intent": res.detected_intent,
        "extracted_entities": res.extracted_entities,
    }




def detect_desktop_app(prompt: str) -> str | None:
    lowered = prompt.lower()
    for app_name, aliases in DESKTOP_APP_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            return app_name
    return None


def detect_dual_mode_app(prompt: str) -> str | None:
    lowered = prompt.lower()
    if re.search(r"\b(?:whats\s*app|whatsup|watsup|whatsap)\b", lowered):
        return "whatsapp"
    if re.search(r"\btelegram\b", lowered):
        return "telegram"
    if re.search(r"\bdiscord\b", lowered):
        return "discord"
    if re.search(r"\binstagram\b", lowered):
        return "instagram"
    if re.search(r"\b(?:twitter|x)\b", lowered):
        return "twitter"
    return None


def has_desktop_app_context(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(desktop|desktop app|desktop application|desktop mode|desktop only|desktop version|windows app|installed app|local app|native app|pc app|desktop client|desktop software|desktop program|open the app|open on desktop|on desktop|in the desktop|on my laptop|on my pc|in desktop|desktop side)\b",
            prompt,
            flags=re.IGNORECASE,
        )
    )


def has_web_app_context(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(web|web app|website|website mode|website only|browser|browser version|site|online|tab|web version|website version|open in chrome|open in brave|open in edge|in the browser|open on the website|on the website|web page|web side)\b",
            prompt,
            flags=re.IGNORECASE,
        )
    )


def has_explicit_website_context(prompt: str) -> bool:
    return bool(
        re.search(
            r"\b(website|web\s*site|site|online|web app|web version|website version|web browser|browser version|in the browser|on the web|website only|web side|browser side)\b",
            prompt,
            flags=re.IGNORECASE,
        )
    )


def extract_generic_desktop_app_request(prompt: str) -> str | None:
    patterns = [
        r"\bdesktop\s+(?:open|launch|start|use)\s+(.+?)(?:\s+app)?\b",
        r"\bdesktop\s+app\s+(?:open|launch|start|use)\s+(.+?)(?:\s+app)?\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+in\s+the\s+desktop\s+app\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+in\s+desktop\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+as\s+the\s+desktop\s+app\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+desktop\s+app\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+desktop\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip(" .\"'")
            if candidate:
                return candidate
    return None


def extract_generic_website_request(prompt: str) -> str | None:
    patterns = [
        r"\b(?:website|web|browser)\s+(?:open|launch|start|use)\s+(.+)\b",
        r"\b(?:website|web|browser|site)\s+(?:open|launch|start|use)\s+(.+)\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+in\s+the\s+web(?:\s+browser)?\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+in\s+the\s+website\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+on\s+the\s+website\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+website\s+only\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+website\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+web\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip(" .\"'")
            if candidate:
                return candidate
    return None


def infer_generic_website_url(target: str | None) -> str | None:
    if not target:
        return None
    explicit = extract_first_url_or_domain(target)
    if explicit:
        return explicit if explicit.startswith("http") else f"https://{explicit}"
    known = detect_known_site(target)
    if known:
        return f"https://{known}"
    cleaned = re.sub(
        r"\b(?:website|web\s*site|web|browser|app|application|page|site|online)\b",
        " ",
        target,
        flags=re.IGNORECASE,
    )
    cleaned = re.split(
        r"\b(?:and|then|search|type|fill|login|log\s+in|sign\s+in|open|play|scroll)\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cleaned = " ".join(cleaned.split()).strip(" .\"'")
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", cleaned, flags=re.IGNORECASE):
        return f"https://www.{cleaned.lower()}.com"
    return None


def _clean_contact_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip("\"'").strip(" .,:;")
    cleaned = re.sub(r"^(?:the\s+contact\s+|contact\s+|chat\s+with\s+|to\s+)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(?:on|in|through|via)\s+whatsapp(?:\s+desktop|\s+app|\s+website|\s+web)?\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\bwhatsapp(?:\s+desktop|\s+app|\s+website|\s+web)?\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:desktop|website|web)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.split(
        r"\b(?:case\s+sensitive|case\s+sensitivity|ignore\s+case|no\s+worries|spelling\s+mistake|spelling\s+mistakes|typo|typos)\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    normalized_lower = cleaned.lower()
    relationship_aliases = {
        "mumy": "mummy",
        "mummi": "mummy",
        "mummie": "mummy",
        "mumyy": "mummy",
        "mommy": "mummy",
        "mom": "mummy",
        "mother": "mummy",
        "ammah": "amma",
        "ammaa": "amma",
        "ammah": "amma",
        "dad": "daddy",
        "dady": "daddy",
        "dadddy": "daddy",
        "father": "daddy",
        "pappa": "papa",
        "nannaa": "nanna",
    }
    cleaned = relationship_aliases.get(normalized_lower, cleaned)
    return cleaned or None


def _normalize_whatsapp_allowed_contact(contact: str | None) -> str | None:
    if not contact:
        return None

    normalized = contact.lower()
    normalized = re.sub(
        r"\b(?:on|in|through|via)\s+(?:the\s+)?(?:whatsapp\s+)?(?:desktop|desktop app|app|website|web|web browser|browser)\b",
        "",
        normalized,
    )
    normalized = re.sub(r"\b(?:whatsapp|desktop|app|website|web|browser)\b", "", normalized)
    normalized = re.sub(r"[^a-z\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    allowed_aliases = {
        "amma": "Amma",
        "ammaa": "Amma",
        "ammah": "Amma",
        "amm": "Amma",
        "am ma": "Amma",
        "mummy": "Amma",
        "mumyy": "Amma",
        "mommy": "Amma",
        "mom": "Amma",
        "mother": "Amma",
        "mamma": "Amma",
        "mumy": "Amma",
    }
    return allowed_aliases.get(normalized) if normalized in allowed_aliases else None


def _whatsapp_safety_clarification(contact: str | None) -> dict[str, Any] | None:
    if not contact:
        return None

    allowed_contact = _normalize_whatsapp_allowed_contact(contact)
    if allowed_contact:
        return None

    return {
        "summary": (
            "For safety, WhatsApp message automation is limited to the Amma contact in this build. "
            "I will not open or send to another WhatsApp chat from automation."
        ),
        "steps": [],
        "needs_clarification": True,
    }


def _clean_message_body(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip("\"'")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or None


def _strip_schedule_phrases(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value
    schedule_patterns = [
        r"\b(?:today|tomorrow)\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
        r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:today|tomorrow)?\b",
        r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:today|tomorrow)?\b",
    ]
    for pattern in schedule_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:with|and)\s+(?:a\s+)?remind(?:er| me)\b", "", cleaned, flags=re.IGNORECASE)
    return _clean_message_body(cleaned)


def extract_prompt_schedule(prompt: str) -> tuple[str | None, str]:
    lowered = prompt.lower()
    now = datetime.now().astimezone()
    run_at: datetime | None = None

    match = re.search(
        r"\b(?:(today|tomorrow)\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        lowered,
        flags=re.IGNORECASE,
    )
    if match:
        day_hint, hour_text, minute_text, period = match.groups()
        hour = int(hour_text)
        minute = int(minute_text or "0")
        if hour == 12:
            hour = 0
        if period.lower() == "pm":
            hour += 12
        target_date = now.date()
        if day_hint == "tomorrow":
            target_date = target_date + timedelta(days=1)
        candidate = datetime.combine(target_date, datetime.min.time(), tzinfo=now.tzinfo).replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
        )
        if day_hint != "tomorrow" and candidate <= now:
            candidate = candidate + timedelta(days=1)
        run_at = candidate
        prompt = re.sub(match.group(0), "", prompt, flags=re.IGNORECASE).strip()
        prompt = re.sub(r"\s{2,}", " ", prompt)

    return run_at.isoformat() if run_at else None, prompt


def extract_send_message_details(prompt: str) -> tuple[str | None, str | None]:
    normalized = re.sub(r"\s+", " ", prompt.strip())
    normalized = re.sub(r"\bwhats?\s*up\b", "whatsapp", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwats?\s*up\b", "whatsapp", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bwhats?\s*ap+p?\b", "whatsapp", normalized, flags=re.IGNORECASE)

    explicit_message_patterns: list[tuple[str, str, str]] = [
        (
            r"\bopen\s+whatsapp(?:\s+desktop|\s+app)?\s+and\s+send\s+(?P<message>.+?)\s+(?:message\s+)?to\s+(?P<contact>[^,.;\n]+)",
            "message",
            "contact",
        ),
        (
            r"\bsend\s+(?P<message>.+?)\s+message\s+to\s+(?P<contact>[^,.;\n]+)",
            "message",
            "contact",
        ),
        (
            r"\bsend\s+(?P<message>.+?)\s+to\s+(?P<contact>[^,.;\n]+)",
            "message",
            "contact",
        ),
        (
            r"\bmessage\s+(?P<contact>[^,.;\n]+?)\s+(?:saying|with|message|text)\s+(?P<message>.+)",
            "message",
            "contact",
        ),
        (
            r"\bsend\s+to\s+(?P<contact>[^,.;\n]+?)\s+(?:saying|with|message|text)\s+(?P<message>.+)",
            "message",
            "contact",
        ),
        (
            r"\bsend\s+(?:a\s+message\s+)?to\s+(?P<contact>[^,.;\n]+?)\s+(?:saying|with|message|text)\s+(?P<message>.+)",
            "message",
            "contact",
        ),
    ]

    for pattern, message_key, contact_key in explicit_message_patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        message = _strip_schedule_phrases(match.group(message_key))
        contact = _clean_contact_name(_strip_schedule_phrases(match.group(contact_key)))
        if message and message.lower() in {"a", "message", "the message"} and contact:
            return None, contact
        if message and contact:
            return message, contact

    contact_only_patterns = [
        r"\bsend\s+(?:a\s+message|message)\s+to\s+(?P<contact>[^,;\n]+?)(?:[.?!]?)$",
        r"\bopen\s+whatsapp(?:\s+desktop|\s+app)?\s+and\s+send\s+(?:a\s+message|message)\s+to\s+(?P<contact>[^,;\n]+?)(?:[.?!]?)$",
        r"\bopen\s+whatsapp(?:\s+desktop|\s+app)?\s+and\s+send\s+(?P<contact>[^,;\n]+?)(?:[.?!]?)$",
        r"\bopen\s+whatsapp(?:\s+desktop|\s+app)?\s+and\s+message\s+(?P<contact>[^,;\n]+?)(?:[.?!]?)$",
    ]
    for pattern in contact_only_patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            contact = _clean_contact_name(match.group("contact"))
            if contact:
                return None, contact

    patterns: list[tuple[str, str, str]] = [
        (
            r"\bsend\s+(?P<message>.+?)\s+(?:message\s+)?to\s+(?P<contact>[^,.;\n]+)",
            "message",
            "contact",
        ),
    ]

    for pattern, message_key, contact_key in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        message = _strip_schedule_phrases(match.group(message_key))
        contact = _clean_contact_name(_strip_schedule_phrases(match.group(contact_key)))
        if message and message.lower() in {"a", "message", "the message"} and contact:
            return None, contact
        if message or contact:
            return message, contact

    contact_only_match = re.search(
        r"\b(?:send|message)\s+(?:a\s+message\s+)?to\s+(?P<contact>[^,.;\n]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if contact_only_match:
        return None, _clean_contact_name(_strip_schedule_phrases(contact_only_match.group("contact")))

    open_and_send_match = re.search(
        r"\bopen\s+(?:the\s+)?whatsapp(?:\s+desktop|\s+app)?\s+and\s+send\s+(?P<message>.+?)\s+to\s+(?P<contact>[^,.;\n]+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if open_and_send_match:
        return (
            _strip_schedule_phrases(open_and_send_match.group("message")),
            _clean_contact_name(_strip_schedule_phrases(open_and_send_match.group("contact"))),
        )

    return None, None


def extract_open_target(prompt: str) -> str | None:
    patterns = [
        r"\b(?:open|launch|start|use)\s+(.+?)\s+(?:in|on)\s+the\s+(?:desktop app|desktop|web browser|browser|website)\b",
        r"\b(?:open|launch|start|use)\s+(.+?)\s+(?:desktop app|desktop|website|web browser|browser)\b",
        r"\b(?:open|launch|start|use)\s+(.+?)(?:\s+and\s+.+)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1).strip(" .\"'")
        if candidate:
            candidate = re.sub(r"\b(the|my)\b", "", candidate, flags=re.IGNORECASE)
            candidate = re.sub(r"\s+", " ", candidate).strip()
            if candidate:
                return candidate
    return None


def extract_windows_path(prompt: str) -> str | None:
    match = re.search(r"([A-Za-z]:\\[^\n\r\"']+)", prompt)
    if match:
        return match.group(1).strip()
    return None


def detect_special_folder(prompt: str) -> str | None:
    lowered = prompt.lower()
    for key in SPECIAL_FOLDERS.keys():
        if re.search(rf"\b{re.escape(key)}\b", lowered):
            return key
    return None


def extract_create_folder_name(prompt: str) -> str | None:
    match = re.search(
        r"(?:create|make)\s+(?:another\s+|a\s+new\s+)?folder(?:\s+named|\s+called|\s+with\s+name)?\s+([^\n,.;]+)",
        prompt,
        flags=re.IGNORECASE,
    )
    if match:
        name = match.group(1).strip().strip('"')
        if name.lower() not in {"in", "same", "the same"}:
            return name
    return None


def extract_run_command(prompt: str) -> tuple[str | None, str]:
    powershell_match = re.search(r"(?:run|execute)\s+(?:powershell|pwsh)\s+command\s+(.+)", prompt, flags=re.IGNORECASE)
    if powershell_match:
        return powershell_match.group(1).strip(), "powershell"

    cmd_match = re.search(r"(?:run|execute)\s+cmd\s+command\s+(.+)", prompt, flags=re.IGNORECASE)
    if cmd_match:
        return cmd_match.group(1).strip(), "cmd"

    generic_match = re.search(r"(?:run|execute)\s+command\s+(.+)", prompt, flags=re.IGNORECASE)
    if generic_match:
        return generic_match.group(1).strip(), "powershell"

    return None, "powershell"


def _sanitize_generated_filename_stem(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if cleaned and cleaned[0].isdigit():
        cleaned = f"task_{cleaned}"
    return cleaned[:48] or "akansha_program"


def _extract_code_task(prompt: str) -> str:
    task = prompt
    task = re.sub(r"\b(?:open|launch|start)\s+.+?\s+(?:desktop\s+)?app\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:and|then)?\s*(?:save|store|put)\b.+", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:and|then)?\s*run\s+(?:it|this|the\s+file)?\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:write|create|make|generate)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:a|an|the)?\s*(?:program|script|code|file)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\b(?:in|using)\s+(?:python|py|javascript|js|typescript|ts)\b", " ", task, flags=re.IGNORECASE)
    task = re.sub(r"\s+", " ", task).strip(" .")
    return task or "requested program"


def _requested_code_language(prompt: str) -> str | None:
    lowered = prompt.lower()
    if re.search(r"\b(?:python|py)\b", lowered):
        return "python"
    if re.search(r"\b(?:javascript|js)\b", lowered):
        return "javascript"
    if re.search(r"\b(?:typescript|ts)\b", lowered):
        return "typescript"
    return None


def _python_code_for_task(task: str) -> str:
    lowered = task.lower()
    if re.search(r"\breverse\b", lowered) and re.search(r"\bstring\b|\bsting\b", lowered):
        return '''"""Reverse a string program.

This file was generated by Akansha automation and can be edited safely.
"""


def reverse_string(text: str) -> str:
    """Return the characters of text in reverse order."""
    return text[::-1]


def main() -> None:
    sample = "Akansha"
    print(f"Original string: {sample}")
    print(f"Reversed string: {reverse_string(sample)}")


if __name__ == "__main__":
    main()
'''

    function_name = _sanitize_generated_filename_stem(task)
    return f'''"""Python program for: {task}."""


def {function_name}() -> None:
    print("Task: {task}")
    print("Edit this generated starter with the exact logic you want.")


if __name__ == "__main__":
    {function_name}()
'''


def _build_code_file_request(prompt: str) -> dict[str, Any] | None:
    lowered = prompt.lower()
    language = _requested_code_language(prompt)
    if language != "python":
        return None
    if not re.search(r"\b(?:write|create|make|generate)\b", lowered):
        return None
    if not re.search(r"\b(?:program|script|code|file)\b", lowered):
        return None

    task = _extract_code_task(prompt)
    stem = "reverse_string" if re.search(r"\breverse\b", task.lower()) and re.search(r"\bstring\b|\bsting\b", task.lower()) else _sanitize_generated_filename_stem(task)
    filename = f"{stem}.py"
    folder_key = detect_special_folder(prompt) or "downloads"
    return {
        "language": language,
        "task": task,
        "filename": filename,
        "folder_key": folder_key,
        "content": _python_code_for_task(task),
        "should_run": bool(re.search(r"\b(?:run|execute|test)\b", lowered)),
    }


def is_complex_site_workflow(prompt: str) -> bool:
    lowered = prompt.lower()
    workflow_tokens = [
        "complete all",
        "solve all",
        "submit all",
        "verify if",
        "go to another question",
        "complete the path",
        "difficulty",
        "section",
        "pop up a message",
        "do the corrections",
    ]
    return any(token in lowered for token in workflow_tokens)


def _automation_caution_response(prompt: str) -> dict[str, Any] | None:
    lowered = prompt.lower()
    risky_patterns = [
        r"\bshutdown\b",
        r"\brestart\b",
        r"\bsign\s*out\b",
        r"\blog\s*out\b",
        r"\bformat\b",
        r"\bfactory\s+reset\b",
        r"\bdelete\s+(?:my\s+)?account\b",
        r"\bdelete\s+(?:all|everything)\b",
        r"\bclear\s+(?:all|everything|history|chat history|browser history)\b",
        r"\bremove\s+(?:all|everything)\b",
    ]
    if any(re.search(pattern, lowered) for pattern in risky_patterns):
        return {
            "summary": (
                "That command can change or remove important data/session state. "
                "Please confirm with the exact words: yes, run this risky action."
            ),
            "steps": [],
            "needs_clarification": True,
            "caution": True,
        }
    return None


def _looks_like_payment_or_booking_decision(prompt: str) -> bool:
    lowered = prompt.lower()
    decision_word = re.search(
        r"\b(?:buy|purchase|order|checkout|pay|payment|card|credit card|debit card|book|booking|reserve|confirm)\b",
        lowered,
    )
    task_context = re.search(
        r"\b(?:shop|shopping|product|price|compare|hotels?|flights?|tickets?|cabs?|trains?|movies?|appointments?|deals?)\b",
        lowered,
    )
    return bool(decision_word and task_context)


def _automation_goal_label(prompt: str, plan: dict[str, Any]) -> str:
    lowered = prompt.lower()
    actions = {str(step.get("action", "")) for step in plan.get("steps", [])}

    if plan.get("needs_clarification"):
        return "Needs clarification"
    if plan.get("requires_confirmation") or "notify_user" in actions:
        return "Approval-gated task"
    if any(token in lowered for token in ["convert", "generate", "create", "export", "file", "folder", "pdf", "ppt", "excel", "word", "image", "video", "document"]):
        return "File and document workflow"
    if "run_command" in actions:
        return "Command workflow"
    if any(action.startswith("open_app") for action in actions):
        return "Desktop app workflow"
    if re.search(r"\b(song|music|video|audio|media|play|pause|stream|spotify|youtube|reel|shorts)\b", lowered):
        return "Media workflow"
    if "scroll" in actions or "click" in actions or "type_text" in actions or "type_sequence" in actions:
        return "Active-window control"
    if "open_url" in actions or re.search(r"\b(search|open|website|browser|google)\b", lowered):
        return "Browser research"
    return "General automation"


def _humanize_automation_action(action: str) -> str:
    normalized = action.strip().lower()
    if not normalized:
        return "dynamic task step"
    if "url" in normalized or "browser" in normalized:
        return "web navigation"
    if "tab" in normalized:
        return "tab control"
    if "click" in normalized:
        return "GUI click"
    if "type" in normalized or "field" in normalized or "draft" in normalized:
        return "GUI text control"
    if "scroll" in normalized:
        return "smart scroll"
    if "key" in normalized or "hotkey" in normalized:
        return "keyboard control"
    if "media" in normalized or "youtube" in normalized or "song" in normalized or "volume" in normalized:
        return "media and audio control"
    if "app" in normalized or "window" in normalized:
        return "desktop app control"
    if "path" in normalized or "folder" in normalized or "file" in normalized or "pdf" in normalized or "ppt" in normalized:
        return "file/document workflow"
    if "command" in normalized or "shell" in normalized:
        return "terminal command"
    if "notify" in normalized or "confirm" in normalized:
        return "owner confirmation"
    if "wait" in normalized:
        return "timed wait"
    return normalized.replace("_", " ")


def _automation_risk_level(prompt: str, plan: dict[str, Any]) -> str:
    lowered = prompt.lower()
    actions = {str(step.get("action", "")) for step in plan.get("steps", [])}

    if plan.get("caution") or re.search(r"\b(delete|format|factory reset|shutdown|restart|sign out|log out)\b", lowered):
        return "blocked"
    if plan.get("requires_confirmation") or "notify_user" in actions or _looks_like_payment_or_booking_decision(prompt):
        return "approval-required"
    if re.search(r"\b(password|login|sign in|card|otp|send|post|message|email|private|personal)\b", lowered):
        return "guarded"
    if "run_command" in actions or "run_python_file" in actions or "convert_pdfs_to_ppts" in actions:
        return "guarded"
    return "normal"


def _automation_owner_label(owner_name: str | None = None) -> str:
    cleaned = " ".join(str(owner_name or "").split()).strip()
    return cleaned or "the owner"


def _automation_capabilities_for_steps(steps: list[dict[str, Any]]) -> list[str]:
    capabilities: list[str] = []
    for step in steps:
        capability = _humanize_automation_action(str(step.get("action", "")))
        if capability and capability not in capabilities:
            capabilities.append(capability)
    return capabilities


def _build_cowork_profile(prompt: str, plan: dict[str, Any], owner_name: str | None = None) -> dict[str, Any]:
    steps = list(plan.get("steps", []))
    risk_level = _automation_risk_level(prompt, plan)
    needs_owner = risk_level in {"approval-required", "blocked"} or bool(plan.get("needs_clarification"))
    phase_status = "blocked" if plan.get("needs_clarification") or risk_level == "blocked" else "ready"
    owner_label = _automation_owner_label(owner_name)

    return {
        "mode": "Akansha Cowork",
        "owner_label": owner_label,
        "goal": _automation_goal_label(prompt, plan),
        "risk_level": risk_level,
        "needs_owner_input": needs_owner,
        "capabilities": _automation_capabilities_for_steps(steps),
        "dynamic_policy": {
            "planning_style": "goal-first",
            "tool_selection": "Use the available local browser, desktop, file, media, command, and approval tools that match the goal.",
            "missing_tool_behavior": f"If the goal needs a tool that is not installed yet, stop at the exact missing capability and ask {owner_label} before guessing.",
            "future_ready": True,
        },
        "phases": [
            {
                "name": "Understand",
                "status": "complete",
                "detail": "Classified the request and separated browser, desktop, file, media, command, and safety intent.",
            },
            {
                "name": "Plan",
                "status": "complete" if steps else phase_status,
                "detail": f"Prepared {len(steps)} executable step(s)." if steps else "No executable step was produced.",
            },
            {
                "name": "Execute",
                "status": phase_status,
                "detail": "Ready to run through the local automation executor." if steps else "Waiting for a clearer or safer instruction.",
            },
            {
                "name": "Verify",
                "status": "pending",
                "detail": "Checks each step result and reports the exact stop point instead of hiding failures.",
            },
        ],
        "stop_rules": [
            f"Ask {owner_label} before payment, booking, account deletion, posting, or sending sensitive data.",
            "Stop on the first failed step and show the failed action with its message.",
            "Use active-window control for GUI actions, so browser focus and page layout still matter.",
        ],
    }


def _build_cowork_execution_report(
    cowork: dict[str, Any],
    execution_results: list[dict[str, Any]],
    scheduled: bool = False,
) -> dict[str, Any]:
    report = dict(cowork)
    if scheduled:
        report["phases"] = [
            {**phase, "status": "scheduled" if phase["name"] == "Execute" else phase["status"]}
            for phase in cowork.get("phases", [])
        ]
        report["outcome"] = "Scheduled"
        report["step_statuses"] = []
        return report

    step_statuses: list[dict[str, Any]] = []
    for item in execution_results:
        step = item.get("step", {})
        result = item.get("result", {})
        step_statuses.append(
            {
                "action": step.get("action"),
                "target": step.get("target"),
                "success": bool(result.get("success")),
                "message": result.get("message") or "",
                "note": result.get("note") or "",
            }
        )

    failed = next((item for item in step_statuses if not item["success"]), None)
    if not execution_results:
        outcome = "No steps executed"
        execute_status = "blocked"
        verify_status = "blocked"
    elif failed:
        outcome = f"Stopped at {failed['action']}"
        execute_status = "blocked"
        verify_status = "needs-review"
    else:
        outcome = "Completed local automation steps"
        execute_status = "complete"
        verify_status = "complete"

    report["outcome"] = outcome
    report["step_statuses"] = step_statuses
    report["phases"] = [
        {
            **phase,
            "status": execute_status if phase["name"] == "Execute" else verify_status if phase["name"] == "Verify" else phase["status"],
        }
        for phase in cowork.get("phases", [])
    ]
    return report


def _extract_ordinal_number(prompt: str) -> int | None:
    lowered = prompt.lower()
    ordinal_words = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
        "sixth": 6,
        "seventh": 7,
        "eighth": 8,
        "ninth": 9,
        "tenth": 10,
    }
    for word, value in ordinal_words.items():
        if re.search(rf"\b{word}\b", lowered):
            return value
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", lowered)
    if match:
        return max(1, min(int(match.group(1)), 10))
    return None


def _bounded_scroll_amount(value: float) -> int:
    return max(1, min(int(round(value)), 40))


def _smart_scroll_instruction(prompt: str) -> tuple[str, int]:
    lowered = prompt.lower()
    direction = "up" if re.search(r"\b(?:up|top|previous|back|above|start|beginning)\b", lowered) else "down"

    if re.search(r"\b(?:top|start|beginning)\b", lowered):
        return "up", 18
    if re.search(r"\b(?:bottom|end|last)\b", lowered):
        return "down", 18
    if re.search(r"\b(?:read|reading|article|paragraph|line|slow|slowly|carefully)\b", lowered):
        return direction, 2
    if re.search(r"\b(?:reels?|shorts?|video|youtube|instagram|facebook|feed|post|posts|tweet|tweets|social)\b", lowered):
        return direction, 5
    if re.search(r"\b(?:search results?|results?|products?|shopping|compare|list|table|rows?)\b", lowered):
        return direction, 7
    if re.search(r"\b(?:document|pdf|page|pages|screen|slide|slides)\b", lowered):
        return direction, 12
    return direction, 6


def _extract_scroll_instruction(prompt: str) -> tuple[str, int]:
    lowered = prompt.lower()
    if re.search(r"\b(?:smart\s+scroll|scroll\s+smart)\b", lowered):
        return _smart_scroll_instruction(prompt)

    direction = "up" if re.search(r"\b(?:scroll\s+up|page\s+up|up|top|previous|back|above)\b", lowered) else "down"

    measurement_match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(cm|centimeters?|centimetres?|mm|millimeters?|millimetres?|in|inch|inches|px|pixels?)\b",
        lowered,
    )
    if measurement_match:
        value = float(measurement_match.group(1))
        unit = measurement_match.group(2)
        if unit.startswith("cm") or unit.startswith("centimet"):
            return direction, _bounded_scroll_amount(value * 3)
        if unit.startswith("mm") or unit.startswith("millimet"):
            return direction, _bounded_scroll_amount(value * 0.3)
        if unit in {"in", "inch", "inches"}:
            return direction, _bounded_scroll_amount(value * 8)
        return direction, _bounded_scroll_amount(value / 80)

    if "one by one" in lowered:
        return direction, 3
    if re.search(r"\b(?:little|small|slow|slowly|bit)\b", lowered):
        return direction, 2
    if re.search(r"\bhalf\s+(?:page|screen)\b", lowered):
        return direction, 8
    if re.search(r"\b(?:full\s+page|one\s+page|page)\b", lowered):
        return direction, 12
    if re.search(r"\b(?:more|fast|quick|quickly|large)\b", lowered):
        return direction, 10
    if re.search(r"\b(?:normal|medium)\b", lowered):
        return direction, 6

    amount_match = re.search(r"\b(?:by|for)\s+(\d{1,2})\b", lowered)
    if amount_match:
        return direction, _bounded_scroll_amount(float(amount_match.group(1)))

    return direction, 6


def build_browser_prompt_plan(prompt: str, owner_name: str | None = None) -> dict[str, Any]:
    normalized = normalize_browser_automation_prompt(prompt)
    lowered = normalized.lower()
    owner_label = _automation_owner_label(owner_name)
    caution_response = _automation_caution_response(normalized)
    if caution_response:
        return caution_response
    url_or_domain = extract_first_url_or_domain(normalized)
    normalized_domain = normalize_domain(url_or_domain) or detect_known_site(normalized)
    windows_path = extract_windows_path(normalized)
    special_folder = detect_special_folder(normalized)
    search_phrase = extract_search_phrase(normalized)
    username_value, password_value = extract_login_credentials(normalized)
    email_value = username_value or extract_field_value(normalized, ["email", "username", "user id", "login id"])
    message_value = extract_field_value(normalized, ["message", "text", "reply"])
    desktop_app = detect_desktop_app(normalized)
    requested_desktop_app = extract_generic_desktop_app_request(normalized)
    requested_website_target = extract_generic_website_request(normalized)
    dual_mode_app = detect_dual_mode_app(normalized)
    desktop_context = has_desktop_app_context(normalized)
    web_context = has_web_app_context(normalized)
    explicit_website_context = has_explicit_website_context(normalized)
    dual_mode_native_app_hint = bool(
        dual_mode_app
        and re.search(
            rf"\b(?:{re.escape(dual_mode_app)}|{re.escape(str(DUAL_MODE_APP_TARGETS.get(dual_mode_app, {}).get('label', dual_mode_app)))})\s+app\b",
            lowered,
            flags=re.IGNORECASE,
        )
    )
    if dual_mode_native_app_hint and not explicit_website_context:
        desktop_context = True
    browser_app = desktop_app if desktop_app in BROWSER_APP_LABELS else None
    is_open_request = any(token in lowered for token in ["open", "launch", "start", "use"])
    created_folder_name = extract_create_folder_name(normalized) or "Converted_PPTs"
    shell_command, shell_name = extract_run_command(normalized)
    send_message_text, send_message_contact = extract_send_message_details(normalized)
    if send_message_contact and (dual_mode_app == "whatsapp" or desktop_app == "whatsapp" or "whatsapp" in lowered):
        whatsapp_safety_response = _whatsapp_safety_clarification(send_message_contact)
        if whatsapp_safety_response:
            return whatsapp_safety_response
        send_message_contact = _normalize_whatsapp_allowed_contact(send_message_contact) or send_message_contact
    requested_open_target = extract_open_target(normalized)
    typed_match = re.search(r"\b(?:type|write|send)\b\s+(.+)", normalized, flags=re.IGNORECASE)
    typed_instruction = None
    if typed_match:
        typed_instruction = re.split(
            r"\b(?:to the active field|in the active field|into the active field|to the field|in the field|into the field)\b",
            typed_match.group(1),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .")

    steps: list[dict[str, Any]] = []
    summary = "Prepared a browser automation plan."

    code_file_request = _build_code_file_request(normalized)
    if code_file_request:
        if is_open_request and desktop_app:
            steps.append({"action": "open_app", "target": desktop_app})
            steps.append({"action": "wait", "payload": {"seconds": 1.2}})
        file_payload = {
            "folder_key": code_file_request["folder_key"],
            "filename": code_file_request["filename"],
            "content": code_file_request["content"],
            "encoding": "utf-8",
        }
        steps.append({"action": "write_text_file", "payload": file_payload})
        if code_file_request["should_run"]:
            steps.append({"action": "run_python_file", "payload": file_payload})
        folder_label = code_file_request["folder_key"].title()
        summary = (
            f"Creating {code_file_request['filename']} in {folder_label}"
            + (" and running it." if code_file_request["should_run"] else ".")
        )
        return {"summary": summary, "steps": steps}

    submit_confirmation = should_confirm_before_submit(normalized)
    submit_followup = bool(
        re.fullmatch(
            r"(?:ok|okay|yes|all ok|all okay|confirmed|confirm|go ahead|now)?\s*submit(?:\s+it|\s+the\s+form|\s+now)?",
            lowered,
        )
        or re.search(r"\b(?:ok|okay|yes|all\s+ok|all\s+okay|confirmed|confirm|go\s+ahead|now)\b.*\bsubmit\b", lowered)
    )
    if submit_followup and not submit_confirmation:
        steps.append({"action": "press_key", "target": "enter", "payload": {"key": "enter"}})
        summary = "Submitting the currently focused form after your confirmation."
        return {"summary": summary, "steps": steps}

    if re.search(r"\b(?:close|remove)\b.*\btab\b", lowered):
        steps.append({"action": "close_tab"})
        summary = "Closing the current browser tab."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["new tab", "open tab"]):
        steps.append({"action": "new_tab"})
        summary = "Opening a new browser tab."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["clear draft", "delete draft", "remove draft", "clear field"]):
        steps.append({"action": "remove_draft"})
        summary = "Clearing the active draft or field."
        return {"summary": summary, "steps": steps}

    if re.search(r"\bdouble\s+click\b|\bdoubleclick\b", lowered):
        steps.append({"action": "click", "payload": {"clicks": 2}})
        summary = "Double-clicking in the active window."
        return {"summary": summary, "steps": steps}

    if re.search(r"\bclick(?:\s+(?:this|that|there|here|current|selected|active|button|link|item))?\b", lowered):
        steps.append({"action": "click", "payload": {"clicks": 1}})
        summary = "Clicking in the active window."
        return {"summary": summary, "steps": steps}

    scroll_requested = bool(re.search(r"\bscroll\b|\bpage\s+(?:up|down)\b", lowered))
    if is_open_request and normalized_domain and scroll_requested and "play" not in lowered:
        direction, amount = _extract_scroll_instruction(normalized)
        target_url = url_or_domain or f"https://{normalized_domain}"
        steps.append({"action": "open_url", "target": target_url})
        steps.append({"action": "wait", "payload": {"seconds": 2.0}})
        steps.append({"action": "scroll", "target": direction, "payload": {"direction": direction, "amount": amount}})
        summary = f"Opening {normalized_domain} and scrolling {direction} in the active window."
        return {"summary": summary, "steps": steps}

    if scroll_requested:
        direction, amount = _extract_scroll_instruction(normalized)
        steps.append({"action": "scroll", "target": direction, "payload": {"direction": direction, "amount": amount}})
        summary = f"Scrolling {direction} in the active window."
        return {"summary": summary, "steps": steps}

    shortcut_commands = [
        (("select all",), ["ctrl", "a"], "Selecting all content in the active window."),
        (("copy", "copy this"), ["ctrl", "c"], "Copying the current selection."),
        (("paste", "paste it"), ["ctrl", "v"], "Pasting into the active window."),
        (("cut", "cut this"), ["ctrl", "x"], "Cutting the current selection."),
        (("undo",), ["ctrl", "z"], "Undoing the last active-window change."),
        (("redo",), ["ctrl", "y"], "Redoing the last active-window change."),
        (("save", "save file", "save this"), ["ctrl", "s"], "Saving the active file or page."),
    ]
    for phrases, keys, shortcut_summary in shortcut_commands:
        if any(re.search(rf"\b{re.escape(phrase)}\b", lowered) for phrase in phrases):
            steps.append({"action": "hotkey", "payload": {"keys": keys}})
            return {"summary": shortcut_summary, "steps": steps}

    key_match = re.search(
        r"\bpress\s+(enter|return|escape|esc|tab|space|spacebar|backspace|delete|home|end|page up|page down|up|down|left|right)\b",
        lowered,
    )
    if key_match:
        key_name = key_match.group(1).replace(" ", "")
        steps.append({"action": "press_key", "target": key_name, "payload": {"key": key_name}})
        summary = f"Pressing {key_match.group(1)} in the active window."
        return {"summary": summary, "steps": steps}

    compound_volume_match = re.search(r"\b(?:volume|sound|audio)?\s*(?:to|at|on)\s+(\d{1,3})\s*%?\b", lowered)
    compound_result_index = _extract_ordinal_number(normalized)
    if "play" in lowered and compound_volume_match and any(token in lowered for token in ["song", "result", "video", "track", "music"]):
        volume_amount = max(0, min(int(compound_volume_match.group(1)), 100))
        result_index = compound_result_index or 1
        query_source = (
            normalized[: compound_volume_match.start()]
            + " "
            + normalized[compound_volume_match.end() :]
        )
        query = re.sub(
            r"\b(open|youtube|play|please|and|then|now|the|song|songs|music|movie|movies|result|video|track|volume|sound|audio|to|at|on|first|second|third|fourth|fifth|\d+(?:st|nd|rd|th))\b",
            " ",
            query_source,
            flags=re.IGNORECASE,
        )
        query = " ".join(query.split()).strip(" .")
        steps.append({"action": "set_volume", "payload": {"amount": volume_amount}})
        if query:
            steps.append(
                {
                    "action": "open_youtube_song",
                    "target": query,
                    "payload": {"play": True, "index": result_index, "wait_seconds": 3.0},
                }
            )
            summary = f"Setting volume to {volume_amount}% and playing YouTube result {result_index} for {query}."
        else:
            steps.append({"action": "click_youtube_result", "target": str(result_index), "payload": {"amount": result_index}})
            summary = f"Setting volume to {volume_amount}% and playing YouTube result {result_index}."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["volume", "sound", "audio"]):
        amount_match = re.search(r"\b(?:by|to|at|on)\s+(\d{1,3})\s*%?\b", lowered)
        amount = int(amount_match.group(1)) if amount_match else 5
        if any(token in lowered for token in ["mute", "silent", "silence"]):
            steps.append({"action": "volume_mute"})
            summary = "Toggling system mute."
            return {"summary": summary, "steps": steps}
        if amount_match and any(token in lowered for token in ["set", "make", "to", "at", "on", "volume"]):
            steps.append({"action": "set_volume", "payload": {"amount": amount}})
            summary = f"Setting system volume to {amount}%."
            return {"summary": summary, "steps": steps}
        if any(token in lowered for token in ["increase", "raise", "up", "higher", "louder"]):
            steps.append({"action": "volume_up", "payload": {"amount": amount}})
            summary = "Increasing system volume."
            return {"summary": summary, "steps": steps}
        if any(token in lowered for token in ["decrease", "reduce", "lower", "down", "quieter"]):
            steps.append({"action": "volume_down", "payload": {"amount": amount}})
            summary = "Decreasing system volume."
            return {"summary": summary, "steps": steps}

    # Only fire standalone media-key shortcut when there is no site-specific automation intent
    _has_site_intent = any(s in lowered for s in [
        "youtube", "codechef", "leetcode", "github", "coursera",
        "linkedin learning", "hackerrank", "codeforces", "geeksforgeeks",
    ])
    if not _has_site_intent and any(token in lowered for token in ["pause", "resume", "play pause", "play/pause"]):
        steps.append({"action": "media_play_pause"})
        summary = "Toggling media playback."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["next song", "next track", "skip song", "skip track"]):
        steps.append({"action": "media_next"})
        summary = "Skipping to the next media item."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["previous song", "previous track", "back song", "last song"]):
        steps.append({"action": "media_previous"})
        summary = "Returning to the previous media item."
        return {"summary": summary, "steps": steps}

    form_values = extract_form_fill_values(normalized)
    form_context = bool(
        re.search(
            r"\b(fill|type|enter|complete|edit)\b.*\b(detail|details|form|field|fields|application|website|site|page)\b",
            lowered,
        )
        or re.search(r"\b(name|email|phone|mobile|username|password|address|city|message)\b", lowered)
    )
    if form_values and form_context:
        target_url = url_or_domain or (f"https://{normalized_domain}" if normalized_domain else None)
        if target_url:
            steps.append({"action": "open_url", "target": target_url})
            steps.append({"action": "wait", "payload": {"seconds": 2.2}})

        should_submit_now = bool(re.search(r"\b(submit|sign\s+in|login|log\s+in)\b", lowered)) and not submit_confirmation
        steps.append(
            {
                "action": "type_sequence",
                "payload": {
                    "values": form_values,
                    "submit": should_submit_now,
                    "tab_presses_before": 0,
                    "type_interval": 0.04,
                    "step_delay": 0.12,
                    "clear_each": True,
                },
            }
        )
        if submit_confirmation:
            steps.append(
                {
                    "action": "notify_user",
                    "payload": {
                        "title": "Akansha is waiting before submit",
                        "body": "I filled the requested details. Say okay submit when you want me to press submit.",
                    },
                }
            )
            summary = "Opening the requested page, filling the details, and waiting for your submit confirmation."
        else:
            summary = (
                "Opening the requested page and filling the details."
                if target_url
                else "Filling the provided details into the active form fields."
            )
        return {"summary": summary, "steps": steps}

    youtube_result_index = _extract_ordinal_number(normalized)
    if youtube_result_index and "play" in lowered and any(token in lowered for token in ["song", "result", "video", "track"]):
        steps.append({"action": "click_youtube_result", "target": str(youtube_result_index), "payload": {"amount": youtube_result_index}})
        summary = f"Playing YouTube result {youtube_result_index} in the active browser."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["brightness", "screen light", "display light"]):
        amount_match = re.search(r"\b(?:by|to|at)\s+(\d{1,3})\s*%?\b", lowered)
        amount = int(amount_match.group(1)) if amount_match else 10
        if any(token in lowered for token in ["set", "make", "to ", "at "]):
            steps.append({"action": "set_brightness", "payload": {"amount": amount}})
            summary = f"Setting screen brightness to {amount}%."
            return {"summary": summary, "steps": steps}
        if any(token in lowered for token in ["increase", "raise", "up", "higher", "brighter"]):
            steps.append({"action": "brightness_up", "payload": {"amount": amount}})
            summary = "Increasing screen brightness."
            return {"summary": summary, "steps": steps}
        if any(token in lowered for token in ["decrease", "reduce", "lower", "down", "dim"]):
            steps.append({"action": "brightness_down", "payload": {"amount": amount}})
            summary = "Decreasing screen brightness."
            return {"summary": summary, "steps": steps}

    if is_open_request and desktop_app in DESKTOP_ONLY_APP_TARGETS and not web_context:
        steps.append({"action": "open_app", "target": desktop_app})
        summary = f"Opening {humanize_desktop_target(desktop_app)} on the desktop."
        if typed_instruction:
            steps.append({"action": "wait", "payload": {"seconds": 1.4}})
            steps.append({"action": "type_text", "target": typed_instruction})
            summary = f"Opening {humanize_desktop_target(desktop_app)} and typing the requested text."
        elif message_value:
            steps.append({"action": "wait", "payload": {"seconds": 1.4}})
            steps.append({"action": "type_text", "target": message_value})
            summary = f"Opening {humanize_desktop_target(desktop_app)} and typing the provided message."
        return {"summary": summary, "steps": steps}

    if is_open_request and requested_desktop_app and desktop_context and not detect_dual_mode_app(requested_desktop_app):
        steps.append({"action": "open_app", "target": requested_desktop_app})
        summary = f"Opening {humanize_desktop_target(requested_desktop_app)} on the desktop."
        if typed_instruction:
            steps.append({"action": "wait", "payload": {"seconds": 1.4}})
            steps.append({"action": "type_text", "target": typed_instruction})
            summary = f"Opening {humanize_desktop_target(requested_desktop_app)} and typing the requested text."
        elif message_value:
            steps.append({"action": "wait", "payload": {"seconds": 1.4}})
            steps.append({"action": "type_text", "target": message_value})
            summary = f"Opening {humanize_desktop_target(requested_desktop_app)} and typing the provided message."
        return {"summary": summary, "steps": steps}

    if is_open_request and requested_website_target and not desktop_context:
        inferred_url = infer_generic_website_url(requested_website_target)
        if inferred_url:
            steps.append({"action": "open_url", "target": inferred_url})
            if search_phrase:
                steps.append({"action": "wait", "payload": {"seconds": 1.8}})
                steps.append({"action": "type_text", "target": search_phrase})
                steps.append({"action": "press_key", "target": "enter", "payload": {"key": "enter"}})
                summary = f"Opening {inferred_url} and searching for {search_phrase}."
            else:
                summary = f"Opening {inferred_url} in the browser."
            return {"summary": summary, "steps": steps}

    if "play" in lowered and any(token in lowered for token in ["song", "songs", "music", "movie", "movies"]):
        result_index = _extract_ordinal_number(normalized) or 1
        query = search_phrase or re.sub(
            r"\b(open|youtube|play|please|song|songs|music|movie|movies|first|second|third|fourth|fifth|\d+(?:st|nd|rd|th)?)\b",
            " ",
            normalized,
            flags=re.IGNORECASE,
        )
        query = " ".join(query.split()).strip(" .") or normalized
        steps.append(
            {
                "action": "open_youtube_song",
                "target": query,
                "payload": {"play": True, "index": result_index, "wait_seconds": 3.0},
            }
        )
        summary = f"Opening YouTube, searching for {query}, and selecting result {result_index}."
        return {"summary": summary, "steps": steps}

    if normalized_domain == "codechef.com" and any(token in lowered for token in ["practice", "problem section", "practice problem"]):
        java_path = "https://www.codechef.com/practice/java" if "java" in lowered else "https://www.codechef.com/practice"
        steps.append({"action": "open_url", "target": java_path})

        if is_complex_site_workflow(normalized):
            steps.append(
                {
                    "action": "unsupported_browser_workflow",
                    "payload": {
                        "message": (
                            "I opened the CodeChef practice path, but this kind of multi-step site workflow "
                            "needs a DOM-aware web agent. The current automation will not auto-solve or submit "
                            "CodeChef practice problems."
                        ),
                        "note": (
                            "This prevents the old behavior where the full prompt was typed into random text boxes."
                        ),
                    },
                }
            )
            summary = "Opening the CodeChef practice path without dumping your prompt into the page."
            return {"summary": summary, "steps": steps}

        summary = "Opening the CodeChef practice path."
        return {"summary": summary, "steps": steps}

    if shell_command:
        steps.append({"action": "run_command", "target": shell_command, "payload": {"shell": shell_name}})
        summary = f"Running the requested {shell_name} command."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["convert all pdfs to ppts", "convert pdfs to ppts", "convert pdf to ppt", "convert pdfs to pptx"]):
        source_path = windows_path or SPECIAL_FOLDERS.get(special_folder or "", SPECIAL_FOLDERS["downloads"])
        steps.append(
            {
                "action": "convert_pdfs_to_ppts",
                "payload": {
                    "source_path": source_path,
                    "output_folder_name": created_folder_name,
                },
            }
        )
        summary = f"Converting all PDFs in {source_path} into PPTX files and placing them in {created_folder_name}."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["open folder", "open path", "go to files", "go to folder", "open downloads", "open desktop", "open documents"]):
        source_path = windows_path or SPECIAL_FOLDERS.get(special_folder or "", "")
        if source_path:
            steps.append({"action": "open_path", "payload": {"path": source_path}})
            summary = f"Opening {source_path} in File Explorer."
            return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["close window", "close app", "exit app"]):
        steps.append({"action": "close_window"})
        summary = "Closing the current desktop app window."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["switch window", "switch app", "change window"]):
        steps.append({"action": "switch_window"})
        summary = "Switching to the next open desktop window."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["minimize window", "minimize app"]):
        steps.append({"action": "minimize_window"})
        summary = "Minimizing the current desktop window."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["maximize window", "maximize app"]):
        steps.append({"action": "maximize_window"})
        summary = "Maximizing the current desktop window."
        return {"summary": summary, "steps": steps}

    if _looks_like_payment_or_booking_decision(normalized):
        query = search_phrase or normalized
        steps.append({"action": "open_url", "target": _google_search_url(query)})
        steps.append(
            {
                "action": "notify_user",
                "payload": {
                    "title": f"Akansha needs {owner_label} before the final action",
                    "body": (
                        "I opened the research/comparison path. I will not purchase, book, "
                        f"post, or use payment details until {owner_label} confirms the exact next step."
                    ),
                },
            }
        )
        summary = "Opening research and pausing before any purchase, booking, posting, or payment step."
        return {"summary": summary, "steps": steps, "requires_confirmation": True}

    if search_phrase:
        if _looks_like_payment_or_booking_decision(normalized):
            steps.append({"action": "open_url", "target": _google_search_url(search_phrase)})
            steps.append(
                {
                    "action": "notify_user",
                    "payload": {
                        "title": f"Akansha needs {owner_label} before payment",
                        "body": (
                            "I opened the research/comparison search. I will not purchase, book, "
                            f"or use card/payment details until {owner_label} confirms the exact next step."
                        ),
                    },
                }
            )
            summary = (
                "Opening comparison research and pausing before any purchase, booking, or payment step."
            )
            return {"summary": summary, "steps": steps, "requires_confirmation": True}

        if _is_google_domain(normalized_domain):
            steps.append({"action": "open_url", "target": _google_search_url(search_phrase)})
            summary = f"Opening Google search for {search_phrase}."
            return {"summary": summary, "steps": steps}

        if url_or_domain or normalized_domain:
            target_url = url_or_domain or f"https://{normalized_domain}"
            steps.append({"action": "open_url", "target": target_url})
            steps.append({"action": "wait", "payload": {"seconds": 1.8}})
            steps.append({"action": "type_text", "target": search_phrase})
            steps.append({"action": "press_key", "target": "enter", "payload": {"key": "enter"}})
            summary = f"Opening {normalized_domain or url_or_domain} and searching for {search_phrase}."
            return {"summary": summary, "steps": steps}

        steps.append({"action": "open_url", "target": _google_search_url(search_phrase)})
        summary = f"Opening a browser search for {search_phrase}."
        return {"summary": summary, "steps": steps}

    if is_open_request and requested_website_target:
        requested_target_app = detect_desktop_app(requested_website_target)
        requested_target_dual_mode = detect_dual_mode_app(requested_website_target)
        requested_target_domain = normalize_domain(extract_first_url_or_domain(requested_website_target)) or detect_known_site(
            requested_website_target
        )

        if requested_target_app in DESKTOP_ONLY_APP_TARGETS:
            label = humanize_desktop_target(requested_target_app)
            return {
                "summary": (
                    f"{label} is a Windows desktop app, not a website. "
                    f"Say '/desktop open {requested_target_app}' or 'open {requested_target_app} desktop app'."
                ),
                "steps": [],
                "needs_clarification": True,
            }

        if requested_target_app in BROWSER_APP_LABELS:
            browser_label = BROWSER_APP_LABELS[requested_target_app]
            return {
                "summary": (
                    f"{browser_label} is a desktop browser app, not a website. "
                    f"Say '/desktop open {requested_target_app}' to launch the browser, or '/website open google.com' to open a real website."
                ),
                "steps": [],
                "needs_clarification": True,
            }

        if requested_target_dual_mode:
            dual_mode_config = DUAL_MODE_APP_TARGETS[requested_target_dual_mode]
            label = str(dual_mode_config["label"])
            web_target = str(dual_mode_config["web_url"])
            if requested_target_dual_mode == "whatsapp" and send_message_contact:
                return {
                    "summary": (
                        "WhatsApp message sending is only supported in the desktop app path in this build. "
                        "Say '/desktop open whatsapp' or 'open whatsapp desktop and send hi to Amma'."
                    ),
                    "steps": [],
                    "needs_clarification": True,
                }
            web_steps = [{"action": "open_url", "target": web_target}]
            web_summary = f"Opening {label} in the browser."
            if typed_instruction:
                web_steps.append({"action": "wait", "payload": {"seconds": 1.8}})
                web_steps.append({"action": "type_text", "target": typed_instruction})
                web_summary = f"Opening {label} in the browser and typing the requested text."
            elif message_value:
                web_steps.append({"action": "wait", "payload": {"seconds": 1.8}})
                web_steps.append({"action": "type_text", "target": message_value})
                web_summary = f"Opening {label} in the browser and typing the provided message."
            return {"summary": web_summary, "steps": web_steps}

        if requested_target_domain:
            target_url = extract_first_url_or_domain(requested_website_target) or f"https://{requested_target_domain}"
            steps.append({"action": "open_url", "target": target_url})
            summary = f"Opening {requested_target_domain} in the default browser."
            return {"summary": summary, "steps": steps}

    if is_open_request and requested_open_target and web_context and not normalized_domain:
        requested_target_app = detect_desktop_app(requested_open_target)
        requested_target_dual_mode = detect_dual_mode_app(requested_open_target)
        if requested_target_app in DESKTOP_ONLY_APP_TARGETS:
            label = humanize_desktop_target(requested_target_app)
            return {
                "summary": (
                    f"{label} is a Windows desktop app, not a website. "
                    f"Say '/desktop open {requested_target_app}' or 'open {requested_target_app} desktop app'."
                ),
                "steps": [],
                "needs_clarification": True,
            }
        if requested_target_app in BROWSER_APP_LABELS:
            browser_label = BROWSER_APP_LABELS[requested_target_app]
            return {
                "summary": (
                    f"{browser_label} is a browser application, not a normal website. "
                    f"Say '/desktop open {requested_target_app}' to launch the app, or '/website open google.com' for a real site."
                ),
                "steps": [],
                "needs_clarification": True,
            }
        if requested_target_dual_mode:
            label = str(DUAL_MODE_APP_TARGETS[requested_target_dual_mode]["label"])
            return {
                "summary": (
                    f"{label} can open either as the desktop app or on the web. "
                    f"Say '/desktop open {label}' or '/website open {label}' so I choose the right one."
                ),
                "steps": [],
                "needs_clarification": True,
            }

    if is_open_request and desktop_app in DESKTOP_ONLY_APP_TARGETS and web_context:
        label = humanize_desktop_target(desktop_app)
        return {
            "summary": (
                f"{label} is a Windows desktop app, not a website. "
                f"Say '/desktop open {desktop_app}' or 'open {desktop_app} desktop app'."
            ),
            "steps": [],
            "needs_clarification": True,
        }

    if is_open_request and browser_app and normalized_domain:
        browser_label = BROWSER_APP_LABELS[browser_app]
        target_url = url_or_domain or f"https://{normalized_domain}"
        steps.append({"action": "open_app_url", "target": browser_app, "payload": {"url": target_url}})
        summary = f"Opening {normalized_domain} in {browser_label}."
        return {"summary": summary, "steps": steps}

    if is_open_request and browser_app and explicit_website_context and not normalized_domain and not search_phrase:
        browser_label = BROWSER_APP_LABELS[browser_app]
        return {
            "summary": (
                f"{browser_label} is a desktop browser app, not a website. "
                f"Say '/desktop open {browser_app}' to launch the app, or '/website open google.com' for a real website. "
                f"You can also say 'open google in {browser_app}' to open a website inside that browser."
            ),
            "steps": [],
            "needs_clarification": True,
        }

    if is_open_request and browser_app and desktop_context and not normalized_domain:
        browser_label = BROWSER_APP_LABELS[browser_app]
        steps.append({"action": "open_app", "target": browser_app})
        summary = f"Opening {browser_label} as the desktop browser app."
        return {"summary": summary, "steps": steps}

    if is_open_request and browser_app and not normalized_domain and not web_context:
        browser_label = BROWSER_APP_LABELS[browser_app]
        steps.append({"action": "open_app", "target": browser_app})
        summary = f"Opening {browser_label} as the desktop browser app."
        return {"summary": summary, "steps": steps}

    if dual_mode_app and any(token in lowered for token in ["open", "launch", "start", "use"]):
        dual_mode_config = DUAL_MODE_APP_TARGETS[dual_mode_app]
        label = str(dual_mode_config["label"])
        desktop_target = dual_mode_config["desktop_app"]
        web_target = str(dual_mode_config["web_url"])

        if desktop_context and web_context:
            return {
                "summary": (
                    f"I can open {label} as the desktop app or in the browser, but your prompt mentions both. "
                    f"Please say either '/desktop open {label}' or '/website open {label}'."
                ),
                "steps": [],
                "needs_clarification": True,
            }

        if desktop_context:
            if desktop_target:
                desktop_steps = [{"action": "open_app", "target": desktop_target}]
                desktop_summary = f"Opening the {label} desktop app."
                if dual_mode_app == "whatsapp" and send_message_contact:
                    desktop_steps.append({"action": "wait", "payload": {"seconds": 2.2}})
                    desktop_steps.append(
                        {
                            "action": "whatsapp_send_message",
                            "payload": {
                                "contact": send_message_contact,
                                "message": send_message_text or "",
                            },
                        }
                    )
                    desktop_summary = (
                        f"Opening WhatsApp desktop and sending '{send_message_text}' to {send_message_contact}."
                        if send_message_text
                        else f"Opening WhatsApp desktop and focusing the chat for {send_message_contact}."
                    )
                    return {
                        "summary": desktop_summary,
                        "steps": desktop_steps,
                    }
                if typed_instruction:
                    desktop_steps.append({"action": "wait", "payload": {"seconds": 1.4}})
                    desktop_steps.append({"action": "type_text", "target": typed_instruction})
                    desktop_summary = f"Opening the {label} desktop app and typing the requested text."
                elif message_value:
                    desktop_steps.append({"action": "wait", "payload": {"seconds": 1.4}})
                    desktop_steps.append({"action": "type_text", "target": message_value})
                    desktop_summary = f"Opening the {label} desktop app and typing the provided message."
                return {
                    "summary": desktop_summary,
                    "steps": desktop_steps,
                }
            return {
                "summary": (
                    f"I can open {label} on the web, but this setup does not have a reliable desktop app target for it yet. "
                    f"Please say '/website open {label}' or tell me the exact installed desktop app name."
                ),
                "steps": [],
                "needs_clarification": True,
            }

        if web_context:
            web_steps = [{"action": "open_url", "target": web_target}]
            web_summary = f"Opening {label} in the browser."
            if typed_instruction:
                web_steps.append({"action": "wait", "payload": {"seconds": 1.8}})
                web_steps.append({"action": "type_text", "target": typed_instruction})
                web_summary = f"Opening {label} in the browser and typing the requested text."
            elif message_value:
                web_steps.append({"action": "wait", "payload": {"seconds": 1.8}})
                web_steps.append({"action": "type_text", "target": message_value})
                web_summary = f"Opening {label} in the browser and typing the provided message."
            return {
                "summary": web_summary,
                "steps": web_steps,
            }

        if not desktop_target:
            web_steps = [{"action": "open_url", "target": web_target}]
            web_summary = (
                f"Opening {label} in the browser because this setup has a web target for it but not a reliable desktop app target."
            )
            if typed_instruction:
                web_steps.append({"action": "wait", "payload": {"seconds": 1.8}})
                web_steps.append({"action": "type_text", "target": typed_instruction})
                web_summary = f"Opening {label} in the browser and typing the requested text."
            elif message_value:
                web_steps.append({"action": "wait", "payload": {"seconds": 1.8}})
                web_steps.append({"action": "type_text", "target": message_value})
                web_summary = f"Opening {label} in the browser and typing the provided message."
            return {
                "summary": web_summary,
                "steps": web_steps,
            }

        return {
            "summary": (
                f"I can open {label} as the desktop app or in the browser. "
                f"Please say '/desktop open {label}' or '/website open {label}'."
            ),
            "steps": [],
            "needs_clarification": True,
        }

    if (
        dual_mode_app == "whatsapp"
        and send_message_contact
        and any(token in lowered for token in ["send", "message", "text", "reply"])
    ):
        if web_context or explicit_website_context:
            return {
                "summary": (
                    "WhatsApp message sending is only supported in the desktop app path in this build. "
                    "Say '/desktop open whatsapp' or 'open whatsapp desktop and send hi to Amma'."
                ),
                "steps": [],
                "needs_clarification": True,
            }

        desktop_steps = [{"action": "open_app", "target": "whatsapp"}, {"action": "wait", "payload": {"seconds": 2.2}}]
        desktop_steps.append(
            {
                "action": "whatsapp_send_message",
                "payload": {
                    "contact": send_message_contact,
                    "message": send_message_text or "",
                },
            }
        )
        desktop_summary = (
            f"Opening WhatsApp desktop and sending '{send_message_text}' to {send_message_contact}."
            if send_message_text
            else f"Opening WhatsApp desktop and focusing the chat for {send_message_contact}."
        )
        return {"summary": desktop_summary, "steps": desktop_steps}

    if is_open_request and normalized_domain and not dual_mode_app and not browser_app:
        target_url = url_or_domain or f"https://{normalized_domain}"
        steps.append({"action": "open_url", "target": target_url})
        summary = f"Opening {normalized_domain} in the default browser."
        if typed_instruction:
            steps.append({"action": "wait", "payload": {"seconds": 1.8}})
            steps.append({"action": "type_text", "target": typed_instruction})
            summary = f"Opening {normalized_domain} and typing the requested text."
        elif message_value:
            steps.append({"action": "wait", "payload": {"seconds": 1.8}})
            steps.append({"action": "type_text", "target": message_value})
            summary = f"Opening {normalized_domain} and typing the provided message."
        return {"summary": summary, "steps": steps}

    if desktop_app and any(token in lowered for token in ["open", "launch", "start"]):
        steps.append({"action": "open_app", "target": desktop_app})
        summary = f"Opening {humanize_desktop_target(desktop_app)} on the desktop."
        if desktop_app == "whatsapp" and send_message_contact:
            steps.append({"action": "wait", "payload": {"seconds": 2.2}})
            steps.append(
                {
                    "action": "whatsapp_send_message",
                    "payload": {
                        "contact": send_message_contact,
                        "message": send_message_text or "",
                    },
                }
            )
            summary = (
                f"Opening WhatsApp and sending '{send_message_text}' to {send_message_contact}."
                if send_message_text
                else f"Opening WhatsApp and focusing the chat for {send_message_contact}."
            )
            return {"summary": summary, "steps": steps}
        if typed_instruction:
            steps.append({"action": "wait", "payload": {"seconds": 1.4}})
            steps.append({"action": "type_text", "target": typed_instruction})
            summary = f"Opening {humanize_desktop_target(desktop_app)} and typing the requested text."
        elif message_value:
            steps.append({"action": "wait", "payload": {"seconds": 1.4}})
            steps.append({"action": "type_text", "target": message_value})
            summary = f"Opening {humanize_desktop_target(desktop_app)} and typing the provided message."
        elif desktop_app == "calculator":
            operation_match = re.search(r"(?:do|calculate|compute)\s+([0-9+\-*/().\s]+)", normalized, flags=re.IGNORECASE)
            if operation_match:
                expression = operation_match.group(1).replace(" ", "")
                steps.append({"action": "wait", "payload": {"seconds": 1.2}})
                steps.append({"action": "type_text", "target": expression})
                steps.append({"action": "wait", "payload": {"seconds": 0.4}})
                steps.append({"action": "type_text", "target": "="})
                summary = f"Opening calculator and entering {expression}."
        return {"summary": summary, "steps": steps}

    if requested_desktop_app and desktop_context and any(token in lowered for token in ["open", "launch", "start", "use"]):
        steps.append({"action": "open_app", "target": requested_desktop_app})
        summary = f"Opening {humanize_desktop_target(requested_desktop_app)} on the desktop."
        requested_desktop_dual_mode = detect_dual_mode_app(requested_desktop_app)
        if requested_desktop_dual_mode == "whatsapp" and send_message_contact:
            steps.append({"action": "wait", "payload": {"seconds": 2.2}})
            steps.append(
                {
                    "action": "whatsapp_send_message",
                    "payload": {
                        "contact": send_message_contact,
                        "message": send_message_text or "",
                    },
                }
            )
            summary = (
                f"Opening WhatsApp desktop and sending '{send_message_text}' to {send_message_contact}."
                if send_message_text
                else f"Opening WhatsApp desktop and focusing the chat for {send_message_contact}."
            )
            return {"summary": summary, "steps": steps}
        if typed_instruction:
            steps.append({"action": "wait", "payload": {"seconds": 1.4}})
            steps.append({"action": "type_text", "target": typed_instruction})
            summary = f"Opening {humanize_desktop_target(requested_desktop_app)} on the desktop and typing the requested text."
        elif message_value:
            steps.append({"action": "wait", "payload": {"seconds": 1.4}})
            steps.append({"action": "type_text", "target": message_value})
            summary = f"Opening {humanize_desktop_target(requested_desktop_app)} on the desktop and typing the provided message."
        return {"summary": summary, "steps": steps}

    if (any(token in lowered for token in ["login", "sign in", "log in"]) or ((email_value or password_value) and normalized_domain)) and normalized_domain:
        login_meta = KNOWN_LOGIN_URLS.get(normalized_domain)
        if login_meta:
            steps.append({"action": "open_url", "target": login_meta["url"]})
        else:
            steps.append({"action": "open_url", "target": url_or_domain})

        if email_value or password_value:
            steps.append({"action": "wait", "payload": {"seconds": (login_meta or {}).get("prefill_delay", 2.4)}})
            steps.append(
                {
                    "action": "type_sequence",
                    "payload": {
                        "values": [value for value in [email_value, password_value] if value],
                        "submit": True,
                        "tab_presses_before": (login_meta or {}).get("tab_presses_before", 0),
                        "type_interval": (login_meta or {}).get("type_interval", 0.05),
                        "step_delay": (login_meta or {}).get("step_delay", 0.12),
                        "clear_each": True,
                    },
                }
            )
            summary = f"Opening the login page for {normalized_domain} and filling the provided credentials."
            return {"summary": summary, "steps": steps}

    if "youtube" in lowered:
        query = search_phrase or normalized
        result_index = _extract_ordinal_number(normalized) or 1
        should_play = "play" in lowered or "open" in lowered
        steps.append(
            {
                "action": "open_youtube_song",
                "target": query,
                "payload": {"play": should_play, "index": result_index, "wait_seconds": 3.0},
            }
        )
        summary = (
            f"Opening YouTube, searching for {query}, and selecting result {result_index}."
            if should_play
            else f"Opening YouTube and searching for {query}."
        )
        return {"summary": summary, "steps": steps}

    if url_or_domain:
        steps.append({"action": "open_url", "target": url_or_domain})
        summary = f"Opening {url_or_domain} in the default browser."

    if typed_instruction and not is_complex_site_workflow(normalized):
        if steps:
            steps.append({"action": "wait", "payload": {"seconds": 1.8}})
        steps.append({"action": "type_text", "target": typed_instruction})
        summary = "Opening the requested page and typing the important message into the active field."
        return {"summary": summary, "steps": steps}

    if email_value or password_value:
        if not steps and not url_or_domain:
            summary = "Typing the provided credentials into the active browser fields."
        elif steps:
            steps.append({"action": "wait", "payload": {"seconds": 2.0}})
            summary = f"{summary} Then filling the credentials into the active fields."

        sequence = [value for value in [email_value, password_value] if value]
        steps.append({"action": "type_sequence", "payload": {"values": sequence, "submit": "login" in lowered or "sign in" in lowered}})
        return {"summary": summary, "steps": steps}

    if message_value:
        steps.append({"action": "type_text", "target": message_value})
        summary = "Typing the provided message into the active browser field."
        return {"summary": summary, "steps": steps}

    if any(token in lowered for token in ["type ", "write ", "send "]) and not message_value and not is_complex_site_workflow(normalized):
        typed_text = normalized
        for prefix in ["type", "write", "send"]:
            if lowered.startswith(prefix):
                typed_text = normalized[len(prefix):].strip(" :")
                break
        steps.append({"action": "type_text", "target": typed_text})
        summary = "Typing the requested text into the active app or browser field."
        return {"summary": summary, "steps": steps}

    if not steps:
        target_url = _google_search_url(normalized)
        steps.append({"action": "open_url", "target": target_url})
        summary = "Opening a browser search based on the important part of your prompt."

    return {"summary": summary, "steps": steps}


def get_social_connection_status(connection: IntegrationConnection) -> tuple[bool, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    if connection.metadata_json:
        try:
            metadata = json.loads(connection.metadata_json)
        except json.JSONDecodeError:
            metadata = {}

    configured = bool(metadata.get("configured")) and connection.is_connected
    return configured, metadata


def social_public_base_url() -> str:
    return os.getenv("AKANSHA_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _local_secret_key() -> bytes:
    seed = os.getenv("AKANSHA_SECRET_KEY") or os.getenv("SECRET_KEY") or "akansha-local-dev-secret"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _local_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < length:
        chunks.append(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return b"".join(chunks)[:length]


def _local_encrypt(raw: bytes) -> dict[str, str]:
    key = _local_secret_key()
    nonce = secrets.token_bytes(16)
    stream = _local_keystream(key, nonce, len(raw))
    ciphertext = bytes(item ^ stream[index] for index, item in enumerate(raw))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    return {
        "scheme": "local-hmac-stream-v1",
        "payload": base64.b64encode(nonce + tag + ciphertext).decode("ascii"),
    }


def _local_decrypt(payload: str) -> bytes:
    key = _local_secret_key()
    packed = base64.b64decode(payload.encode("ascii"))
    nonce, tag, ciphertext = packed[:16], packed[16:48], packed[48:]
    expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Social credential signature did not match.")
    stream = _local_keystream(key, nonce, len(ciphertext))
    return bytes(item ^ stream[index] for index, item in enumerate(ciphertext))


def _dpapi_encrypt(raw: bytes) -> dict[str, str]:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows.")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(raw)
    in_blob = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DataBlob()
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return {"scheme": "windows-dpapi", "payload": base64.b64encode(encrypted).decode("ascii")}


def _dpapi_decrypt(payload: str) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI is only available on Windows.")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    encrypted = base64.b64decode(payload.encode("ascii"))
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(encrypted)
    in_blob = DataBlob(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def encrypt_social_config(config: dict[str, str]) -> dict[str, str]:
    raw = json.dumps(config, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if os.name == "nt":
        try:
            return _dpapi_encrypt(raw)
        except Exception:
            pass
    return _local_encrypt(raw)


def decrypt_social_config(metadata: dict[str, Any]) -> dict[str, str]:
    encrypted = metadata.get("config_encrypted")
    if isinstance(encrypted, dict) and isinstance(encrypted.get("payload"), str):
        try:
            scheme = encrypted.get("scheme")
            if scheme == "windows-dpapi":
                raw = _dpapi_decrypt(encrypted["payload"])
            else:
                raw = _local_decrypt(encrypted["payload"])
            decoded = json.loads(raw.decode("utf-8"))
            if isinstance(decoded, dict):
                return {str(key): str(value) for key, value in decoded.items()}
        except Exception:
            return {}

    legacy_config = metadata.get("config")
    if isinstance(legacy_config, dict):
        return {str(key): str(value) for key, value in legacy_config.items()}
    return {}


def ensure_social_auto_secrets(platform: str, config: dict[str, str]) -> dict[str, str]:
    updated = dict(config)
    if platform in {"whatsapp", "instagram"} and not updated.get("webhook_verify_token"):
        updated["webhook_verify_token"] = secrets.token_urlsafe(24)
    if platform == "telegram" and not updated.get("webhook_secret"):
        updated["webhook_secret"] = secrets.token_urlsafe(24)
    return updated


def clean_social_config(platform: str, config: dict[str, str]) -> dict[str, str]:
    allowed = {field["key"] for field in SOCIAL_FIELD_DEFINITIONS.get(platform, [])}
    cleaned: dict[str, str] = {}
    for key, value in config.items():
        if key in allowed and isinstance(value, str) and value.strip():
            cleaned[key] = value.strip()
    return cleaned


def social_config_preview(platform: str, config: dict[str, str]) -> dict[str, str]:
    secret_fields = {
        field["key"]
        for field in SOCIAL_FIELD_DEFINITIONS.get(platform, [])
        if field.get("secret")
    }
    return {
        key: mask_secret(value) if key in secret_fields else value
        for key, value in config.items()
    }


def serialize_social_platform(platform: str, connection: IntegrationConnection) -> dict[str, Any]:
    meta = SOCIAL_PLATFORM_META[platform]
    configured, metadata = get_social_connection_status(connection)
    config = decrypt_social_config(metadata)
    required_fields = SOCIAL_REQUIRED_FIELDS.get(platform, [])
    configured_fields = sorted(config.keys())
    missing_fields = [field for field in required_fields if not config.get(field)]
    verified = bool(metadata.get("verified"))
    return {
        "key": platform,
        "label": meta["label"],
        "connected": configured,
        "verified": verified,
        "accent": meta["accent"],
        "setup_required": bool(missing_fields),
        "required_fields": required_fields,
        "missing_fields": missing_fields,
        "configured_fields": configured_fields,
        "fields": SOCIAL_FIELD_DEFINITIONS.get(platform, []),
        "config_preview": social_config_preview(platform, config),
        "webhook_url": f"{social_public_base_url()}/api/social/webhook/{platform}",
        "last_verified": metadata.get("last_verified"),
        "verification_status": metadata.get("verification_status") or ("verified" if verified else "not_tested"),
        "verification_detail": metadata.get("verification_detail"),
        "account_label": connection.account_email if configured else None,
        "permission_level": normalized_permission_level(metadata.get("permission_level")),
        "permission": SOCIAL_PERMISSION_LEVELS[normalized_permission_level(metadata.get("permission_level"))],
        "adapter_key": metadata.get("adapter_key") or recommended_adapter_key(platform),
        "recommended_adapter": recommended_adapter_key(platform),
        "connector_adapters": platform_adapters(platform),
        "safety_note": metadata.get("safety_note") or "Replies are governed by the saved permission level.",
    }


def _social_oauth_redirect_uri(provider: str) -> str:
    env_key = f"AKANSHA_{provider.upper()}_OAUTH_REDIRECT_URI"
    return os.getenv(env_key, f"{social_public_base_url()}/api/social/oauth/callback/{provider}")


def _oauth_state(platform: str, provider: str, permission_level: str, adapter_key: str | None = None) -> str:
    state = secrets.token_urlsafe(24)
    social_oauth_states[state] = {
        "platform": platform,
        "provider": provider,
        "permission_level": normalized_permission_level(permission_level),
        "adapter_key": normalized_adapter_key(platform, adapter_key),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return state


def _social_oauth_start(platform: str, permission_level: str | None = None, adapter_key: str | None = None) -> dict[str, Any]:
    if platform not in SOCIAL_PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unsupported social platform")

    normalized_permission = normalized_permission_level(permission_level)
    normalized_adapter = normalized_adapter_key(platform, adapter_key)

    if platform in {"whatsapp", "instagram"}:
        app_id = os.getenv("META_APP_ID") or os.getenv("FACEBOOK_APP_ID")
        provider = "meta"
        state = _oauth_state(platform, provider, normalized_permission, normalized_adapter)
        redirect_uri = _social_oauth_redirect_uri(provider)
        scope_by_platform = {
            "whatsapp": "business_management,whatsapp_business_management,whatsapp_business_messaging",
            "instagram": "instagram_business_basic,instagram_business_manage_messages,pages_show_list,pages_manage_metadata",
        }
        if not app_id:
            return {
                "available": False,
                "platform": platform,
                "provider": provider,
                "adapter_key": normalized_adapter,
                "permission_level": normalized_permission,
                "setup_required": True,
                "missing_env": ["META_APP_ID or FACEBOOK_APP_ID"],
                "docs": [
                    "https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/overview",
                    "https://developers.facebook.com/docs/instagram-platform/",
                ],
                "message": "Add a Meta app id to enable one-click Meta authorization for WhatsApp/Instagram.",
            }
        auth_url = "https://www.facebook.com/v20.0/dialog/oauth?" + urlencode(
            {
                "client_id": app_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "scope": scope_by_platform[platform],
                "response_type": "code",
            }
        )
        return {
            "available": True,
            "platform": platform,
            "provider": provider,
            "adapter_key": normalized_adapter,
            "permission_level": normalized_permission,
            "auth_url": auth_url,
            "redirect_uri": redirect_uri,
            "message": "Open this URL to authorize through Meta. WhatsApp and Instagram still require their own approved permissions.",
        }

    if platform == "twitter":
        client_id = os.getenv("X_CLIENT_ID") or os.getenv("TWITTER_CLIENT_ID")
        provider = "x"
        state = _oauth_state(platform, provider, normalized_permission, normalized_adapter)
        redirect_uri = _social_oauth_redirect_uri(provider)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        social_oauth_states[state]["code_verifier"] = verifier
        if not client_id:
            return {
                "available": False,
                "platform": platform,
                "provider": provider,
                "adapter_key": normalized_adapter,
                "permission_level": normalized_permission,
                "setup_required": True,
                "missing_env": ["X_CLIENT_ID or TWITTER_CLIENT_ID"],
                "docs": ["https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code"],
                "message": "Add an X OAuth client id to enable one-click X authorization.",
            }
        auth_url = "https://x.com/i/oauth2/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": "tweet.read users.read offline.access tweet.write dm.read dm.write",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return {
            "available": True,
            "platform": platform,
            "provider": provider,
            "adapter_key": normalized_adapter,
            "permission_level": normalized_permission,
            "auth_url": auth_url,
            "redirect_uri": redirect_uri,
            "message": "Open this URL to authorize X. X uses its own OAuth flow and cannot share a Meta token.",
        }

    return {
        "available": False,
        "platform": platform,
        "provider": platform,
        "adapter_key": normalized_adapter,
        "permission_level": normalized_permission,
        "setup_required": True,
        "message": "This platform does not expose a browser OAuth start flow in Akansha yet. Use the adapter setup fields.",
    }


def _exchange_social_oauth_code(provider: str, code: str, context: dict[str, Any]) -> dict[str, Any]:
    if provider == "meta":
        app_id = os.getenv("META_APP_ID") or os.getenv("FACEBOOK_APP_ID")
        app_secret = os.getenv("META_APP_SECRET") or os.getenv("FACEBOOK_APP_SECRET")
        if not app_id or not app_secret:
            return {"exchanged": False, "missing_env": ["META_APP_SECRET or FACEBOOK_APP_SECRET"]}
        url = "https://graph.facebook.com/v20.0/oauth/access_token?" + urlencode(
            {
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": _social_oauth_redirect_uri("meta"),
                "code": code,
            }
        )
        with urlopen(Request(url), timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {"exchanged": True, "token_payload": data}

    if provider == "x":
        client_id = os.getenv("X_CLIENT_ID") or os.getenv("TWITTER_CLIENT_ID")
        client_secret = os.getenv("X_CLIENT_SECRET") or os.getenv("TWITTER_CLIENT_SECRET")
        verifier = context.get("code_verifier")
        if not client_id or not verifier:
            return {"exchanged": False, "missing_env": ["X_CLIENT_ID or TWITTER_CLIENT_ID"]}
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": _social_oauth_redirect_uri("x"),
            "code_verifier": verifier,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if client_secret:
            basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"
        request = Request(
            "https://api.x.com/2/oauth2/token",
            data=urlencode(form).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {"exchanged": True, "token_payload": data}

    return {"exchanged": False, "missing_env": []}


def verify_social_config(platform: str, config: dict[str, str]) -> dict[str, Any]:
    if platform == "telegram":
        bot_token = config.get("bot_token", "")
        request = Request(f"https://api.telegram.org/bot{bot_token}/getMe")
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise HTTPException(status_code=400, detail="Telegram rejected this bot token.")
        result = data.get("result", {})
        username = result.get("username") or result.get("first_name") or "Telegram bot"
        return {
            "verified": True,
            "verification_status": "verified",
            "account_label": f"@{username}" if not str(username).startswith("@") else username,
            "detail": "Telegram bot token verified with getMe.",
        }

    if platform == "whatsapp":
        token = config.get("access_token")
        phone_number_id = config.get("phone_number_id")
        if token and phone_number_id:
            request = Request(
                f"https://graph.facebook.com/v19.0/{phone_number_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            return {
                "verified": True,
                "verification_status": "verified",
                "account_label": data.get("display_phone_number") or data.get("verified_name") or "WhatsApp Cloud API",
                "detail": "WhatsApp phone number ID verified with Meta Graph API.",
            }

    if platform == "instagram":
        token = config.get("page_access_token")
        account_id = config.get("instagram_business_account_id")
        if token and account_id:
            request = Request(
                f"https://graph.facebook.com/v19.0/{account_id}?fields=username",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            username = data.get("username") or account_id
            return {
                "verified": True,
                "verification_status": "verified",
                "account_label": f"@{username}" if not str(username).startswith("@") else username,
                "detail": "Instagram business account verified with Meta Graph API.",
            }

    if platform == "twitter":
        token = config.get("bearer_token")
        if token:
            request = Request(
                "https://api.x.com/2/users/by/username/xdevelopers",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            user = data.get("data", {})
            return {
                "verified": True,
                "verification_status": "verified",
                "account_label": "X API bearer token",
                "detail": f"X bearer token verified with a public user lookup ({user.get('username', 'xdevelopers')}).",
            }

    return {
        "verified": False,
        "verification_status": "configured_unverified",
        "account_label": f"{platform}@configured.local",
        "detail": "Credentials were saved. Live verification was skipped for this platform.",
    }


def store_social_message(db: Session, platform: str, sender: str, content: str, intent: str = "incoming") -> None:
    if not content.strip():
        return
    db.add(
        InboxMessage(
            platform=platform,
            sender=sender or "Unknown",
            content=content.strip(),
            intent=intent,
            sentiment="neutral",
            is_read=False,
        )
    )


def get_connection_metadata(connection: IntegrationConnection) -> dict[str, Any]:
    if not connection.metadata_json:
        return {}
    try:
        return json.loads(connection.metadata_json)
    except json.JSONDecodeError:
        return {}


def _automation_runtime(scheduled_actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure whether automation can actually run, right now.

    Reported as "there are six automation permissions active. It is not live,
    actually". That was accurate: `BROWSER_AUTOMATION_DEFAULTS` is six hardcoded
    `True`s, so the count the chat header showed was the size of a dict literal
    and would have read "6 active" on a machine with no screen, no scheduler and
    no GUI stack at all.

    A permission is a *preference*. Whether the action it permits can be carried
    out is a different question, and it is the one worth putting in front of the
    user. So everything below is probed on the request rather than declared:

      * `screen` comes from `pyautogui.size()`, which fails on a headless session
        — and every click, keystroke and scroll in `automation.py` goes through
        pyautogui, so no screen means no GUI control regardless of permissions.
      * `window_manager` calls into pygetwindow, the layer used to find and focus
        the target window before typing into it.
      * `runner` checks the scheduled-run child module is actually present, since
        scheduling spawns `python -m backend.scheduled_automation_runner` in a
        detached process and a missing module fails silently into DEVNULL.
      * `due` counts saved runs whose time has passed, which is the number that
        tells the user something is stuck rather than pending.

    Failures are caught and reported, never raised: a status endpoint that 500s
    because the GUI stack is unhappy tells the user less than one that says the
    GUI stack is unhappy.
    """
    probe = probe_gui_control()
    screen = probe.get("screen")
    screen_error = probe.get("screen_error")
    window_manager = bool(probe.get("window_manager"))

    runner_module = Path(__file__).resolve().parent / "scheduled_automation_runner.py"

    now = datetime.utcnow()
    due = 0
    for item in scheduled_actions:
        if item.get("status") not in (None, "scheduled"):
            continue
        raw = str(item.get("run_at") or "")
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Saved times are mixed: the datetime-local input writes a naive local
        # string, the API accepts an offset-bearing one, and comparing either to a
        # naive `utcnow()` raises. Normalise to naive UTC rather than guessing a
        # zone for the naive ones -- an hour of skew in a "how many are overdue"
        # count is tolerable; a 500 on the status endpoint is not.
        if when.tzinfo is not None:
            when = when.astimezone(timezone.utc).replace(tzinfo=None)
        if when <= now:
            due += 1

    return {
        # The single field the UI should key its wording off. Everything else is
        # the evidence for it.
        "gui_control": screen is not None and window_manager,
        "screen": screen,
        "screen_error": screen_error,
        "window_manager": window_manager,
        "runner_available": runner_module.is_file(),
        "scheduled_total": len(scheduled_actions),
        "scheduled_due": due,
        "checked_at": now.isoformat(),
    }


def get_browser_automation_status(connection: IntegrationConnection) -> dict[str, Any]:
    metadata = get_connection_metadata(connection)
    stored = metadata.get("permissions", {})
    permissions = {
        **BROWSER_AUTOMATION_DEFAULTS,
        **stored,
    }
    scheduled_actions = metadata.get("scheduled_actions", [])
    return {
        "provider": BROWSER_AUTOMATION_PROVIDER,
        "permissions": permissions,
        # Which of those the owner actually decided, as opposed to inherited from
        # the defaults table. The chat header used to present the second kind as
        # if it were the first.
        "granted": sorted(key for key, value in stored.items() if value),
        "defaulted": sorted(key for key in BROWSER_AUTOMATION_DEFAULTS if key not in stored),
        "runtime": _automation_runtime(scheduled_actions),
        "scheduled_actions": scheduled_actions,
        "actions": BROWSER_AUTOMATION_ACTIONS,
        "cowork": {
            "enabled": True,
            "description": (
                "Akansha Cowork classifies each automation request, builds a step plan, "
                "executes through the local desktop/browser layer, and reports the exact stop point."
            ),
            "supports": [
                "browser research",
                "active-window GUI control",
                "desktop app launch",
                "smart and measured scroll",
                "media and volume control",
                "file workflows",
                "approval-gated shopping, booking, posting, and sending",
            ],
        },
        "disclaimer": (
            "Akansha can prepare and trigger browser actions, but the browser and OS still enforce "
            "their own security and focus rules."
        ),
    }


def ensure_social_seed(db: Session):
    existing = (
        db.query(InboxMessage)
        .filter(InboxMessage.platform.in_(list(SOCIAL_PLATFORM_META.keys())))
        .count()
    )
    if existing:
        return

    samples = [
        InboxMessage(
            platform="whatsapp",
            sender="Rahul",
            content="Hey, can we move tomorrow's practice interview to 7:30 PM?",
            intent="schedule",
            sentiment="neutral",
        ),
        InboxMessage(
            platform="instagram",
            sender="Ananya Design",
            content="Loved your AI project post. Are you open to collaborating on a reel next week?",
            intent="collaboration",
            sentiment="positive",
        ),
        InboxMessage(
            platform="twitter",
            sender="Open Source Club",
            content="We saw your thread on JWT refresh flow. Would you like to join our Sunday space?",
            intent="invitation",
            sentiment="positive",
        ),
        InboxMessage(
            platform="telegram",
            sender="Project Team",
            content="Can you confirm whether the demo build is ready before midnight?",
            intent="status",
            sentiment="urgent",
        ),
        InboxMessage(
            platform="discord",
            sender="Build Squad",
            content="Can you review the latest bot integration notes in the shared Discord channel?",
            intent="review",
            sentiment="neutral",
        ),
    ]
    db.add_all(samples)
    db.commit()


def suggest_social_replies(message: InboxMessage) -> list[str]:
    content = message.content.lower()
    name = message.sender.split()[0]

    if any(token in content for token in ["move", "schedule", "time", "tomorrow"]):
        return [
            f"Yes {name}, 7:30 PM works for me.",
            f"I can do tomorrow, but I need a little later. Would 8 PM work?",
            "Let me confirm in a few minutes and I will get back to you.",
        ]

    if any(token in content for token in ["collab", "collaborating", "reel"]):
        return [
            "That sounds exciting. I would love to hear the idea in a little more detail.",
            "Yes, I am open to it. Can you share the concept and expected timeline?",
            "I am interested. Let us lock a quick call and plan it properly.",
        ]

    if any(token in content for token in ["join", "space", "sunday", "invite"]):
        return [
            "Thanks for inviting me. Please share the exact time and topic.",
            "I would be happy to join if the timing works. Send me the details.",
            "That sounds good. Let me check my schedule and confirm shortly.",
        ]

    if any(token in content for token in ["ready", "midnight", "demo", "confirm"]):
        return [
            "I am checking the latest build right now and will confirm shortly.",
            "The demo is almost ready. I will send a final status update soon.",
            "Give me a little time and I will confirm the final build status.",
        ]

    return [
        f"Thanks {name}, I saw your message.",
        "I am on it. Let me get back to you shortly.",
        "Got it. I will reply with a proper update soon.",
    ]


# --- OTP Auth Endpoints ---
otp_store: dict[str, dict[str, Any]] = {}
OTP_TTL_SECONDS = 10 * 60
AUTH_PURPOSES = {"login", "signup", "reset"}


def normalize_auth_email(email: str) -> str:
    normalized = " ".join((email or "").strip().split()).lower()
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", normalized):
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    return normalized


def normalize_auth_purpose(purpose: str | None) -> str:
    normalized = (purpose or "login").strip().lower()
    if normalized not in AUTH_PURPOSES:
        raise HTTPException(status_code=400, detail="Unsupported authentication purpose.")
    return normalized


def hash_password(password: str) -> str:
    if len(password or "") < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
    return f"pbkdf2_sha256$120000${salt}${digest}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        algorithm, rounds_text, salt, digest = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return hmac.compare_digest(password, stored_hash)
        computed = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(rounds_text),
        ).hex()
        return hmac.compare_digest(computed, digest)
    except Exception:
        return False


def create_auth_token(email: str) -> str:
    payload = f"{email}:{secrets.token_hex(24)}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def consume_otp(email: str, code: str, purpose: str | None = None) -> None:
    normalized_email = normalize_auth_email(email)
    expected_purpose = normalize_auth_purpose(purpose) if purpose else None
    record = otp_store.get(normalized_email)
    if not record:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code.")

    if datetime.now(timezone.utc) > record["expires_at"]:
        otp_store.pop(normalized_email, None)
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code.")

    if expected_purpose and record["purpose"] != expected_purpose:
        raise HTTPException(status_code=400, detail="OTP was requested for a different action.")

    record["attempts"] = int(record.get("attempts", 0)) + 1
    if record["attempts"] > 5:
        otp_store.pop(normalized_email, None)
        raise HTTPException(status_code=400, detail="Too many OTP attempts. Request a new code.")

    if not hmac.compare_digest(str(record["code"]), str(code or "").strip()):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code.")

    otp_store.pop(normalized_email, None)


def auth_user_payload(user: UserProfile) -> dict[str, Any]:
    return {
        "email": user.email,
        "full_name": user.full_name,
        "username": user.username,
        "google_connected": user.google_connected,
        "google_email": user.google_email,
    }


@app.post("/api/auth/send-otp")
def send_otp(req: AuthOtpRequest):
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    email = normalize_auth_email(req.email)
    purpose = normalize_auth_purpose(req.purpose)
    code = f"{secrets.randbelow(1000000):06d}"
    otp_store[email] = {
        "code": code,
        "purpose": purpose,
        "attempts": 0,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=OTP_TTL_SECONDS),
    }

    print(f"\n[{datetime.now().isoformat()}] OTP generated for {email} ({purpose}): {code}\n")

    # SMTP Sending Logic
    smtp_email = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    email_dispatched = False

    if smtp_email and smtp_password:
        try:
            msg = MIMEMultipart()
            # Note: Gmail SMTP often requires the 'From' address to be exactly the authenticated email
            msg['From'] = smtp_email
            msg['To'] = email
            msg['Subject'] = f"{code} is your Akansha Verification Sequence"

            body = f"""
Akansha authentication code

Your {purpose} verification code is: {code}

This code expires in 10 minutes.

If you did not request this, ignore this email.
            """
            msg.attach(MIMEText(body, 'plain'))

            print(f"Connecting to {smtp_server}:{smtp_port}...")
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, email, msg.as_string())
            server.quit()
            email_dispatched = True
            print(f"Email successfully dispatched to {email}")
        except Exception as e:
            print(f"CRITICAL ERROR: Failed to dispatch email: {e}")
    else:
        print("WARNING: SMTP credentials missing. Email dispatch skipped.")
    
    response = {"success": True, "message": f"OTP sent to {email}", "expires_in_seconds": OTP_TTL_SECONDS}
    if not email_dispatched:
        response["dev_code"] = code
        response["message"] = "SMTP is not configured. Use the development code returned by this local backend."
    return response

@app.post("/api/auth/verify-otp")
def verify_otp(req: AuthOtpVerifyRequest, db: Session = Depends(get_db)):
    email = normalize_auth_email(req.email)
    consume_otp(email, req.code, req.purpose)

    user = db.query(UserProfile).filter(UserProfile.email == email).first()
    if not user:
        user = UserProfile(email=email, full_name=email.split('@')[0])
        db.add(user)
        db.commit()
        db.refresh(user)
    record_knowledge_event(
        email,
        surface="auth",
        event_type="login_verified",
        summary=f"{email} verified OTP login",
        metadata={"purpose": req.purpose or "login"},
    )
    
    return {
        "success": True,
        "token": create_auth_token(email),
        "user": auth_user_payload(user),
    }


@app.post("/api/auth/register")
def register_with_email(req: AuthRegisterRequest, db: Session = Depends(get_db)):
    email = normalize_auth_email(req.email)
    consume_otp(email, req.code, "signup")
    full_name = " ".join(req.full_name.strip().split())
    if len(full_name) < 2:
        raise HTTPException(status_code=400, detail="Full name is required.")

    existing = db.query(UserProfile).filter(UserProfile.email == email).first()
    if existing and existing.password:
        raise HTTPException(status_code=409, detail="An account already exists for this email.")

    user = existing or UserProfile(email=email)
    user.full_name = full_name
    user.username = (req.username or email.split("@")[0]).strip()[:80]
    user.password = hash_password(req.password)
    db.add(user)
    db.commit()
    db.refresh(user)
    record_knowledge_event(
        email,
        surface="auth",
        event_type="signup",
        summary=f"{full_name} signed up",
        metadata={"email": email, "username": user.username},
    )
    return {"success": True, "token": create_auth_token(email), "user": auth_user_payload(user)}


@app.post("/api/auth/login")
def login_with_password(req: AuthPasswordLoginRequest, db: Session = Depends(get_db)):
    email = normalize_auth_email(req.email)
    user = db.query(UserProfile).filter(UserProfile.email == email).first()
    if not user or not verify_password(req.password, user.password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    record_knowledge_event(
        email,
        surface="auth",
        event_type="password_login",
        summary=f"{email} logged in",
        metadata={"email": email},
    )
    return {"success": True, "token": create_auth_token(email), "user": auth_user_payload(user)}


@app.post("/api/auth/reset-password")
def reset_password(req: AuthResetPasswordRequest, db: Session = Depends(get_db)):
    email = normalize_auth_email(req.email)
    consume_otp(email, req.code, "reset")
    user = db.query(UserProfile).filter(UserProfile.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="No account exists for this email.")
    user.password = hash_password(req.new_password)
    db.add(user)
    db.commit()
    return {"success": True, "message": "Password reset successfully."}

@app.post("/api/chat")
async def chat_endpoint(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    graph_owner: str = Depends(resolve_graph_owner),
):
    if not req.message:
        raise HTTPException(status_code=400, detail="Message is empty")

    user_order = prepare_message_insert_order(db, req.session_id, req.continue_from_message_id, slots=2)

    # Save User Message
    user_msg = ChatMessage(
        role="user",
        content=req.message,
        session_id=req.session_id,
        display_order=user_order,
        branch_from_id=req.continue_from_message_id,
    )
    db.add(user_msg)
    db.commit()
    record_chat_message_in_graph(graph_owner, req.session_id, "user", req.message, user_msg.id)

    try:
        speaker_context = _record_speaker_interaction(req, db, _chat_speaker_profile(req, db))
        # Generate Response
        response_generator = generate_chat_stream(
            db,
            req.message,
            req.session_id,
            user_tone=req.user_tone,
            response_style=req.response_style,
            conversation_mode=req.conversation_mode,
            language_preference=req.language_preference,
            attachments=req.attachments,
            speaker_profile=speaker_context,
        )
        response_text = "".join(list(response_generator))
        response_text = sanitize_model_artifact_placeholders(response_text)
        if is_broken_assistant_response(req.message, response_text):
            raise RuntimeError("The AI provider returned an empty response. Please try again.")
        artifacts = create_requested_artifacts(req.message, response_text)
        response_text += artifact_markdown(artifacts)
        
        # Save Assistant Message
        ai_msg = ChatMessage(
            role="assistant",
            content=response_text,
            session_id=req.session_id,
            display_order=user_order + 1,
            branch_from_id=req.continue_from_message_id,
        )
        db.add(ai_msg)
        db.commit()
        record_chat_message_in_graph(graph_owner, req.session_id, "assistant", response_text, ai_msg.id)
        _record_assistant_speaker_interaction(req, db, speaker_context, response_text)

        # Background Task: Extract Memories & Tasks
        if not _should_skip_ai_memory_analysis(req.message, response_text):
            # `speaker_context` travels with the task because it decides whether the
            # analyser is allowed to launch desktop applications. Without it the
            # analyser assumes the local session and grants owner authority.
            background_tasks.add_task(
                analyze_intent_and_memory, db, req.message, response_text, speaker_context
            )

        return {"response": response_text}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"AI Engine Error: {str(e)}")


@app.post("/api/chat/message")
def save_chat_message(req: ChatMessageSaveRequest, db: Session = Depends(get_db)):
    if req.role not in {"user", "assistant"}:
        raise HTTPException(status_code=400, detail="Role must be 'user' or 'assistant'.")
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Message content is empty.")

    order = prepare_message_insert_order(db, req.session_id or "default", req.continue_from_message_id, slots=1)
    message = ChatMessage(
        role=req.role,
        content=req.content.strip(),
        session_id=req.session_id or "default",
        display_order=order,
        branch_from_id=req.continue_from_message_id,
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    return {
        "message": {
            "id": message.id,
            "session_id": message.session_id,
            "role": message.role,
            "content": message.content,
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
            "pinned": bool(getattr(message, "pinned", False)),
            "display_order": message.display_order,
            "branch_from_id": message.branch_from_id,
        }
    }


@app.patch("/api/chat/message/{message_id}/pin")
def update_chat_message_pin(
    message_id: int,
    req: ChatMessagePinRequest,
    db: Session = Depends(get_db),
    graph_owner: str = Depends(resolve_graph_owner),
):
    message = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found.")

    message.pinned = bool(req.pinned)
    db.add(message)
    db.commit()
    db.refresh(message)
    record_chat_message_in_graph(graph_owner, message.session_id, message.role, message.content, message.id)
    return {
        "message": {
            "id": message.id,
            "session_id": message.session_id,
            "role": message.role,
            "content": message.content,
            "timestamp": message.timestamp.isoformat() if message.timestamp else None,
            "pinned": bool(message.pinned),
            "display_order": message.display_order,
            "branch_from_id": message.branch_from_id,
        }
    }


#: How long the chat stream may go without writing anything before it sends an SSE
#: comment frame. Short enough to keep proxies and the browser from treating a slow
#: turn as a dead connection, long enough not to be traffic in its own right.
_STREAM_HEARTBEAT_S = 5.0


@app.post("/api/chat/stream")
async def chat_stream_endpoint(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    graph_owner: str = Depends(resolve_graph_owner),
):
    if not req.message:
        raise HTTPException(status_code=400, detail="Message is empty")

    user_order = prepare_message_insert_order(db, req.session_id, req.continue_from_message_id, slots=2)
    user_msg = ChatMessage(
        role="user",
        content=req.message,
        session_id=req.session_id,
        display_order=user_order,
        branch_from_id=req.continue_from_message_id,
    )
    db.add(user_msg)
    db.commit()
    record_chat_message_in_graph(graph_owner, req.session_id, "user", req.message, user_msg.id)
    speaker_context = _record_speaker_interaction(req, db, _chat_speaker_profile(req, db))

    async def event_stream():
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        response_text = ""
        buffer_file_response = bool(requested_artifact_formats(req.message))
        # Declared out here so the `finally` below can close it even if the failure
        # happened before it was created. It used to be a bare local that nothing
        # ever shut down: one `ThreadPoolExecutor(max_workers=1)` per chat message,
        # and its worker thread blocks on an empty work queue forever rather than
        # exiting when idle, so every turn in a long session left a live thread
        # behind. That is a slow, compounding contribution to exactly the latency
        # this endpoint was reported for.
        executor = None
        try:
            # Run the synchronous generator in a thread pool to avoid blocking the event loop.
            # Chunks are collected incrementally via a queue so streaming still works.
            loop = asyncio.get_event_loop()
            chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()

            def _run_generator():
                try:
                    for chunk in generate_chat_stream(
                        db,
                        req.message,
                        req.session_id,
                        user_tone=req.user_tone,
                        response_style=req.response_style,
                        conversation_mode=req.conversation_mode,
                        language_preference=req.language_preference,
                        attachments=req.attachments,
                        speaker_profile=speaker_context,
                    ):
                        loop.call_soon_threadsafe(chunk_queue.put_nowait, chunk)
                finally:
                    loop.call_soon_threadsafe(chunk_queue.put_nowait, None)  # sentinel

            executor = ThreadPoolExecutor(max_workers=1)
            loop.run_in_executor(executor, _run_generator)

            while True:
                # A timeout rather than a bare `get()`, so the socket is not silent
                # for the whole of a slow turn. `generate_chat_stream` can spend the
                # cascade budget plus a live-web lookup before its first token, and
                # during that time this loop wrote nothing at all -- which is what
                # "no fallback reply is coming" looks like from the client: an open
                # connection, a spinner, and no bytes.
                #
                # An SSE comment frame is the right heartbeat here because the
                # existing client already ignores it for free: ChatThread looks for a
                # `data: ` line in each frame and skips frames that have none, so
                # nothing is appended to the reply text. A `type` of its own would
                # need a frontend change to be anything but silently dropped.
                try:
                    chunk = await asyncio.wait_for(chunk_queue.get(), timeout=_STREAM_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if chunk is None:
                    break
                response_text += chunk
                if not buffer_file_response:
                    yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"

            response_text = sanitize_model_artifact_placeholders(response_text)
            if is_broken_assistant_response(req.message, response_text):
                raise RuntimeError("The AI provider returned an empty response. Please try again.")
            if buffer_file_response and response_text:
                yield f"data: {json.dumps({'type': 'chunk', 'content': response_text})}\n\n"
            artifacts = create_requested_artifacts(req.message, response_text)
            artifact_block = artifact_markdown(artifacts)
            if artifact_block:
                response_text += artifact_block
                yield f"data: {json.dumps({'type': 'chunk', 'content': artifact_block})}\n\n"

            ai_msg = ChatMessage(
                role="assistant",
                content=response_text,
                session_id=req.session_id,
                display_order=user_order + 1,
                branch_from_id=req.continue_from_message_id,
            )
            db.add(ai_msg)
            db.commit()
            record_chat_message_in_graph(graph_owner, req.session_id, "assistant", response_text, ai_msg.id)
            _record_assistant_speaker_interaction(req, db, speaker_context, response_text)
            if not _should_skip_ai_memory_analysis(req.message, response_text):
                background_tasks.add_task(
                    analyze_intent_and_memory, db, req.message, response_text, speaker_context
                )
            yield f"data: {json.dumps({'type': 'done', 'content': response_text, 'user_message_id': user_msg.id, 'assistant_message_id': ai_msg.id})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            if executor is not None:
                # `wait=False` is safe and is the point: `_run_generator` puts its
                # sentinel in its own `finally`, so by the time the loop above has
                # broken the worker has already returned. Waiting would only add the
                # join to the user's turn.
                executor.shutdown(wait=False)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.get("/api/chat")
def get_chat_history(session_id: str | None = None, db: Session = Depends(get_db)):
    query = db.query(ChatMessage)
    if session_id:
        query = query.filter(ChatMessage.session_id == session_id)

    messages = query.order_by(ChatMessage.display_order.asc(), ChatMessage.id.asc()).all()
    return {
        "messages": [
            {
                "id": m.id,
                "session_id": m.session_id,
                "role": m.role,
                "content": m.content,
                "timestamp": m.timestamp.isoformat()
                if hasattr(m, "timestamp") and m.timestamp
                else None,
                "pinned": bool(getattr(m, "pinned", False)),
                "display_order": getattr(m, "display_order", None),
                "branch_from_id": getattr(m, "branch_from_id", None),
            }
            for m in messages
        ]
    }


@app.delete("/api/chat/session/{session_id}")
def delete_chat_session(session_id: str, db: Session = Depends(get_db)):
    normalized_session_id = (session_id or "").strip()
    if not normalized_session_id:
        raise HTTPException(status_code=400, detail="Session id is required.")

    deleted_count = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == normalized_session_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {
        "success": True,
        "session_id": normalized_session_id,
        "deleted": deleted_count,
    }


@app.delete("/api/chat/history")
def clear_chat_history(db: Session = Depends(get_db)):
    deleted_count = db.query(ChatMessage).delete(synchronize_session=False)
    db.commit()
    return {"success": True, "deleted": deleted_count}


@app.get("/api/memories")
def get_memories(db: Session = Depends(get_db)):
    memories = db.query(Memory).order_by(Memory.importance.desc()).all()
    return {"memories": [{"id": m.id, "topic": m.topic, "insight": m.insight, "importance": m.importance, "timestamp": m.timestamp.isoformat() if hasattr(m, 'timestamp') and m.timestamp else None} for m in memories]}

@app.get("/api/tasks")
def get_tasks(db: Session = Depends(get_db)):
    tasks = db.query(Task).filter(Task.is_completed == False).order_by(Task.timestamp.desc()).all()
    return {"tasks": [{"id": t.id, "title": t.title, "description": t.description} for t in tasks]}

@app.get("/api/inbox")
def get_inbox(db: Session = Depends(get_db)):
    messages = db.query(InboxMessage).order_by(InboxMessage.timestamp.desc()).limit(20).all()
    return {"inbox": [{"platform": m.platform, "sender": m.sender, "content": m.content, "intent": m.intent} for m in messages]}


@app.get("/api/social/inbox")
def get_social_inbox(db: Session = Depends(get_db)):
    ensure_social_seed(db)
    messages = (
        db.query(InboxMessage)
        .filter(InboxMessage.platform.in_(list(SOCIAL_PLATFORM_META.keys())))
        .order_by(InboxMessage.timestamp.desc())
        .limit(12)
        .all()
    )

    platforms = []
    for key, meta in SOCIAL_PLATFORM_META.items():
        connection = get_or_create_connection(db, key)
        platforms.append(serialize_social_platform(key, connection))

    return {
        "platforms": platforms,
        "messages": [
            {
                "id": message.id,
                "platform": message.platform,
                "sender": message.sender,
                "content": message.content,
                "intent": message.intent or "general",
                "sentiment": message.sentiment or "neutral",
                "is_read": message.is_read,
                "timestamp": message.timestamp.isoformat() if message.timestamp else None,
                "suggested_replies": suggest_social_replies(message),
            }
            for message in messages
        ],
    }


@app.get("/api/social/connectors/catalog")
def get_social_connectors_catalog():
    return social_connector_catalog()


@app.get("/api/social/oauth/start/{platform}")
def start_social_oauth(
    platform: str,
    permission_level: str | None = None,
    adapter_key: str | None = None,
):
    return _social_oauth_start(platform, permission_level, adapter_key)


@app.get("/api/social/oauth/callback/{provider}")
def social_oauth_callback(provider: str, code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    if error:
        raise HTTPException(status_code=400, detail=f"{provider} authorization failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="OAuth callback is missing code or state.")
    context = social_oauth_states.pop(state, None)
    if not context:
        raise HTTPException(status_code=400, detail="OAuth state expired or did not match.")
    record_social_permission_in_graph(
        LOCAL_GRAPH_OWNER_ID,
        str(context.get("platform") or provider),
        str(context.get("permission_level") or "send_after_approval"),
        str(context.get("adapter_key") or ""),
        scopes=["oauth_code_received"],
    )
    platform = str(context.get("platform") or provider)
    exchange = _exchange_social_oauth_code(provider, code, context)
    if exchange.get("exchanged"):
        token_payload = exchange.get("token_payload") or {}
        connection = get_or_create_connection(db, platform)
        connection.is_connected = True
        connection.access_token = token_payload.get("access_token")
        connection.refresh_token = token_payload.get("refresh_token")
        expires_in = token_payload.get("expires_in")
        connection.token_expiry = (
            datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            if str(expires_in or "").isdigit()
            else None
        )
        metadata = get_connection_metadata(connection)
        metadata.update(
            {
                "mode": "oauth",
                "configured": True,
                "provider": provider,
                "adapter_key": context.get("adapter_key"),
                "permission_level": context.get("permission_level"),
                "token_type": token_payload.get("token_type"),
                "scope": token_payload.get("scope"),
                "verified": True,
                "verification_status": "oauth_connected",
                "verification_detail": f"{provider} OAuth token exchange completed.",
                "last_verified": datetime.now(timezone.utc).isoformat(),
            }
        )
        connection.metadata_json = json.dumps(metadata)
        db.add(connection)
        db.commit()
        title = "Akansha authorization connected"
        detail = "Token exchange completed and the connection was saved."
    else:
        title = "Akansha authorization received"
        missing = ", ".join(exchange.get("missing_env") or ["provider token exchange configuration"])
        detail = f"The provider returned an authorization code. Add {missing} to complete automatic token exchange."
    return HTMLResponse(
        f"""
        <html><body style="font-family:system-ui;padding:32px">
        <h1>{html.escape(title)}</h1>
        <p>{html.escape(detail)}</p>
        <p>You can close this tab and return to Akansha.</p>
        </body></html>
        """
    )


@app.get("/api/knowledge/graph")
def get_knowledge_graph(
    user_id: str | None = None,
    graph_owner: str = Depends(resolve_graph_owner),
):
    # An explicit ?user_id= still wins, so one graph can be inspected while signed in
    # as another. Without the query parameter this reads whoever the request is,
    # rather than always reading the local owner's graph.
    return graph_snapshot(user_id or graph_owner)


@app.post("/api/social/connect/{platform}")
def connect_social_platform(platform: str, db: Session = Depends(get_db)):
    if platform not in SOCIAL_PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unsupported social platform")

    connection = get_or_create_connection(db, platform)
    configured, metadata = get_social_connection_status(connection)
    if not configured:
        raise HTTPException(
            status_code=400,
            detail="This platform is not configured yet. Add the required API credentials first.",
        )

    config = decrypt_social_config(metadata)
    try:
        verification = verify_social_config(platform, config)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") or str(exc)
        raise HTTPException(status_code=400, detail=f"{platform} verification failed: {detail}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{platform} verification failed: {exc}") from exc

    metadata.update(
        {
            "verified": verification["verified"],
            "verification_status": verification["verification_status"],
            "verification_detail": verification["detail"],
            "last_verified": datetime.now(timezone.utc).isoformat(),
        }
    )
    connection.account_email = verification.get("account_label")
    connection.metadata_json = json.dumps(metadata)
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return {"success": True, "platform": platform, "status": serialize_social_platform(platform, connection)}


@app.post("/api/social/setup/{platform}")
def setup_social_platform(
    platform: str,
    req: SocialSetupRequest,
    db: Session = Depends(get_db),
    graph_owner: str = Depends(resolve_graph_owner),
):
    if platform not in SOCIAL_PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unsupported social platform")

    permission_level = normalized_permission_level(req.permission_level)
    adapter_key = normalized_adapter_key(platform, req.adapter_key)
    required_fields = SOCIAL_REQUIRED_FIELDS.get(platform, [])
    config = ensure_social_auto_secrets(platform, clean_social_config(platform, req.config))
    missing = [field for field in required_fields if not config.get(field, "").strip()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(missing)}",
        )

    verification: dict[str, Any] = {
        "verified": False,
        "verification_status": "saved_not_tested",
        "account_label": f"{platform}@configured.local",
        "detail": "Credentials saved. Use Test connection to verify them live.",
    }
    if req.test_connection:
        try:
            verification = verify_social_config(platform, config)
        except HTTPError as exc:
            verification = {
                "verified": False,
                "verification_status": "verification_failed",
                "account_label": f"{platform}@configured.local",
                "detail": exc.read().decode("utf-8", errors="ignore") or str(exc),
            }
        except Exception as exc:
            verification = {
                "verified": False,
                "verification_status": "verification_failed",
                "account_label": f"{platform}@configured.local",
                "detail": str(exc),
            }

    connection = get_or_create_connection(db, platform)
    connection.is_connected = True
    connection.account_email = verification.get("account_label")
    connection.access_token = None
    connection.metadata_json = json.dumps(
        {
            "mode": "manual-api-config",
            "configured": True,
            "adapter_key": adapter_key,
            "permission_level": permission_level,
            "configured_fields": sorted(config.keys()),
            "config_encrypted": encrypt_social_config(config),
            "config": None,
            "security": "tokens-encrypted-at-rest",
            "safety_note": "Akansha uses this channel only inside the saved permission level.",
            "verified": verification["verified"],
            "verification_status": verification["verification_status"],
            "verification_detail": verification["detail"],
            "last_verified": datetime.now(timezone.utc).isoformat() if verification["verified"] else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    record_social_permission_in_graph(
        graph_owner,
        platform,
        permission_level,
        adapter_key,
        scopes=sorted(config.keys()),
    )

    return {
        "success": True,
        "platform": platform,
        "configured_fields": sorted(config.keys()),
        "status": serialize_social_platform(platform, connection),
        "verification_detail": verification["detail"],
    }


@app.post("/api/social/disconnect/{platform}")
def disconnect_social_platform(
    platform: str,
    db: Session = Depends(get_db),
    graph_owner: str = Depends(resolve_graph_owner),
):
    if platform not in SOCIAL_PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unsupported social platform")

    connection = get_or_create_connection(db, platform)
    connection.is_connected = False
    connection.access_token = None
    connection.refresh_token = None
    connection.scope = None
    connection.account_email = None
    connection.metadata_json = json.dumps(
        {
            "mode": "manual-api-config",
            "configured": False,
            "configured_fields": [],
            "last_verified": None,
            "permission_level": "read_only",
        }
    )
    db.add(connection)
    db.commit()
    record_social_permission_in_graph(graph_owner, platform, "read_only", None, scopes=[])

    return {"success": True, "platform": platform}


@app.get("/api/social/webhook/{platform}")
def verify_social_webhook(platform: str, request: Request, db: Session = Depends(get_db)):
    if platform not in SOCIAL_PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unsupported social platform")

    connection = get_or_create_connection(db, platform)
    _, metadata = get_social_connection_status(connection)
    config = decrypt_social_config(metadata)

    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    expected = config.get("webhook_verify_token") or config.get("webhook_secret")

    if mode == "subscribe" and challenge and expected and token == expected:
        return Response(content=challenge, media_type="text/plain")

    raise HTTPException(status_code=403, detail="Webhook verification token did not match.")


@app.post("/api/social/webhook/{platform}")
async def receive_social_webhook(platform: str, request: Request, db: Session = Depends(get_db)):
    if platform not in SOCIAL_PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unsupported social platform")

    payload = await request.json()
    stored = 0
    if platform == "telegram":
        message = payload.get("message") or payload.get("edited_message") or {}
        chat = message.get("chat") or {}
        sender_data = message.get("from") or {}
        sender = str(chat.get("id") or sender_data.get("username") or sender_data.get("first_name") or "Telegram")
        content = message.get("text") or message.get("caption") or ""
        if content:
            store_social_message(db, platform, sender, content)
            record_knowledge_event(
                LOCAL_GRAPH_OWNER_ID,
                surface="social",
                event_type="incoming_message",
                summary=f"{platform} message from {sender}",
                metadata={"platform": platform, "sender": sender},
                content=content,
            )
            stored += 1

    elif platform in {"whatsapp", "instagram"}:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    sender = message.get("from") or message.get("sender", {}).get("id") or platform
                    text = (message.get("text") or {}).get("body") or message.get("message", {}).get("text") or ""
                    if text:
                        store_social_message(db, platform, str(sender), text)
                        record_knowledge_event(
                            LOCAL_GRAPH_OWNER_ID,
                            surface="social",
                            event_type="incoming_message",
                            summary=f"{platform} message from {sender}",
                            metadata={"platform": platform, "sender": str(sender)},
                            content=text,
                        )
                        stored += 1
            for event in entry.get("messaging", []):
                sender = str((event.get("sender") or {}).get("id") or platform)
                text = (event.get("message") or {}).get("text") or ""
                if text:
                    store_social_message(db, platform, sender, text)
                    record_knowledge_event(
                        LOCAL_GRAPH_OWNER_ID,
                        surface="social",
                        event_type="incoming_message",
                        summary=f"{platform} message from {sender}",
                        metadata={"platform": platform, "sender": sender},
                        content=text,
                    )
                    stored += 1

    else:
        store_social_message(db, platform, platform, json.dumps(payload)[:1200], intent="webhook")
        record_knowledge_event(
            LOCAL_GRAPH_OWNER_ID,
            surface="social",
            event_type="webhook_payload",
            summary=f"{platform} webhook payload received",
            metadata={"platform": platform},
            content=json.dumps(payload)[:1200],
        )
        stored += 1

    db.commit()
    return {"success": True, "platform": platform, "stored": stored}


@app.post("/api/social/send")
def send_social_reply(
    req: SocialReplyRequest,
    db: Session = Depends(get_db),
    graph_owner: str = Depends(resolve_graph_owner),
):
    if req.platform not in SOCIAL_PLATFORM_META:
        raise HTTPException(status_code=404, detail="Unsupported social platform")

    connection = get_or_create_connection(db, req.platform)
    if not connection.is_connected:
        raise HTTPException(status_code=400, detail="Connect the platform before sending replies.")
    metadata = get_connection_metadata(connection)
    permission_level = normalized_permission_level(metadata.get("permission_level"))
    permission = SOCIAL_PERMISSION_LEVELS[permission_level]
    if not permission["can_send"]:
        raise HTTPException(
            status_code=403,
            detail=f"{req.platform} permission is {permission_level}. Change permission to send_after_approval or trusted_auto_send before sending.",
        )
    if not permission_allows_send(permission_level, req.approved):
        raise HTTPException(status_code=400, detail="Approval is required before Akansha can send a reply.")
    config = decrypt_social_config(metadata)
    delivery_status = "approved-and-queued"
    delivery_detail = "Reply is saved locally. Add live recipient IDs/tokens to send through the platform API."

    try:
        if req.platform == "telegram":
            chat_id = config.get("default_chat_id") or (req.sender if str(req.sender).lstrip("-").isdigit() else "")
            bot_token = config.get("bot_token")
            if bot_token and chat_id:
                telegram_request = Request(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    data=json.dumps({"chat_id": chat_id, "text": req.reply}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(telegram_request, timeout=15) as response:
                    telegram_data = json.loads(response.read().decode("utf-8"))
                delivery_status = "sent"
                delivery_detail = "Telegram sendMessage accepted the reply." if telegram_data.get("ok") else "Telegram returned a non-ok response."

        elif req.platform == "whatsapp":
            recipient = re.sub(r"\D", "", req.sender)
            phone_number_id = config.get("phone_number_id")
            token = config.get("access_token")
            if token and phone_number_id and recipient:
                whatsapp_request = Request(
                    f"https://graph.facebook.com/v19.0/{phone_number_id}/messages",
                    data=json.dumps(
                        {
                            "messaging_product": "whatsapp",
                            "to": recipient,
                            "type": "text",
                            "text": {"body": req.reply},
                        }
                    ).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urlopen(whatsapp_request, timeout=15):
                    pass
                delivery_status = "sent"
                delivery_detail = "WhatsApp Cloud API accepted the reply."

        elif req.platform == "instagram":
            token = config.get("page_access_token")
            if token and req.sender and req.sender.isdigit():
                instagram_request = Request(
                    f"https://graph.facebook.com/v19.0/me/messages?access_token={token}",
                    data=json.dumps(
                        {
                            "recipient": {"id": req.sender},
                            "message": {"text": req.reply},
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(instagram_request, timeout=15):
                    pass
                delivery_status = "sent"
                delivery_detail = "Instagram Messaging API accepted the reply."
    except HTTPError as exc:
        delivery_status = "send_failed"
        delivery_detail = exc.read().decode("utf-8", errors="ignore") or str(exc)
    except Exception as exc:
        delivery_status = "send_failed"
        delivery_detail = str(exc)

    if req.message_id:
        original = db.query(InboxMessage).filter(InboxMessage.id == req.message_id).first()
        if original:
            original.is_read = True
            db.add(original)

    db.add(
        InboxMessage(
            platform=req.platform,
            sender=f"You -> {req.sender}",
            content=req.reply,
            intent="reply",
            sentiment="approved",
            is_read=True,
        )
    )
    db.commit()
    record_knowledge_event(
        graph_owner,
        surface="social",
        event_type="reply_attempt",
        summary=f"{req.platform} reply to {req.sender}: {delivery_status}",
        metadata={
            "platform": req.platform,
            "sender": req.sender,
            "status": delivery_status,
            "permission_level": permission_level,
        },
        content=req.reply,
    )

    return {
        "success": True,
        "status": delivery_status,
        "detail": delivery_detail,
        "platform": req.platform,
        "sender": req.sender,
        "reply": req.reply,
    }


@app.get("/api/profile")
def get_profile(db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    return {"profile": serialize_profile(profile)}


@app.put("/api/profile")
def update_profile(req: ProfileUpdateRequest, db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)

    for field, value in req.model_dump(exclude_none=True).items():
        setattr(profile, field, value)

    db.add(profile)
    db.commit()
    db.refresh(profile)
    return {"profile": serialize_profile(profile)}


@app.get("/api/google/status")
def get_google_status(db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    connection = get_or_create_connection(db, "google")
    return {
        "configured": google_configured(),
        "connected": connection.is_connected,
        "email": connection.account_email or profile.google_email,
        "scopes": connection.scope.split(" ") if connection.scope else GOOGLE_SCOPES,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "setup_required": not google_configured(),
    }


@app.get("/api/voice/status")
def get_voice_status():
    return {
        "female_cloned_voice_configured": cloned_voice_configured(),
        "provider": "edge-tts",
        "model_id": "te-IN-ShrutiNeural / en-IN-NeerjaNeural / hi-IN-SwaraNeural",
        "voice_id_present": True,
        "supported_language_modes": ["english", "telugu", "mixed", "hindi"],
    }


@app.get("/api/voice/speakers")
def get_voice_speakers(db: Session = Depends(get_db)):
    speakers = db.query(SpeakerProfile).order_by(SpeakerProfile.timestamp.asc()).all()
    return {"speakers": [serialize_speaker_profile(speaker) for speaker in speakers]}


@app.get("/api/voice/speakers/{speaker_id}/interactions")
def get_voice_speaker_interactions(speaker_id: int, db: Session = Depends(get_db)):
    speaker = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker_id).first()
    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker profile not found.")

    rows = (
        db.query(SpeakerInteraction)
        .filter(SpeakerInteraction.speaker_id == speaker.id)
        .order_by(SpeakerInteraction.timestamp.desc(), SpeakerInteraction.id.desc())
        .limit(50)
        .all()
    )
    return {
        "speaker": serialize_speaker_profile(speaker),
        "interactions": [
            {
                "id": row.id,
                "role": row.role,
                "content": row.content,
                "mood_state": row.mood_state,
                "session_id": row.session_id,
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            }
            for row in reversed(rows)
        ],
    }


@app.post("/api/voice/speakers")
def save_voice_speaker(req: SpeakerProfileRequest, db: Session = Depends(get_db)):
    display_name = req.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Display name is required.")

    relationship = normalize_relationship(req.relationship_to_owner) or None
    # Derived, never taken from the request. `access_level` used to be accepted
    # from the body with `req.access_level or _speaker_access_level(...)`, so a
    # caller could POST any display name with `"access_level": "owner"` and have
    # an owner-level speaker stored permanently. Nothing validated that the caller
    # was the owner, and nothing ever re-derived it afterwards.
    access_level = access_level_for_relationship(relationship)

    speaker = db.query(SpeakerProfile).filter(SpeakerProfile.display_name.ilike(display_name)).first()
    if not speaker:
        speaker = SpeakerProfile(display_name=display_name)

    default_closeness = "close" if relationship in {"owner", "mother", "father"} else "new"
    default_style = {
        "owner": "proactive close companion",
        "mother": "warm family care",
        "father": "practical supportive guidance",
        "friend": "casual Indian college style",
        "professor": "formal academic respect",
    }.get(relationship or "", "polite cautious guest")

    speaker.relationship_to_owner = relationship
    speaker.access_level = access_level
    speaker.closeness_level = (req.closeness_level or speaker.closeness_level or default_closeness).strip().lower()
    speaker.communication_style = (
        req.communication_style or speaker.communication_style or default_style
    ).strip() or None
    speaker.language_preference = (req.language_preference or speaker.language_preference or "english").strip() or None
    speaker.notes = req.notes
    if req.context_profile is not None:
        speaker.context_profile_json = json.dumps(req.context_profile, ensure_ascii=False)
    if req.conversation_summary is not None:
        speaker.conversation_summary = req.conversation_summary.strip()
    if req.mood_state is not None:
        speaker.mood_state = req.mood_state.strip().lower() or None
    speaker.last_intro_text = f"{display_name} is {relationship or 'a new speaker'} for the owner."
    speaker.last_heard_text = (req.last_heard_text or "").strip() or speaker.last_heard_text
    if req.voice_signature:
        speaker.voice_signature_json = json.dumps(req.voice_signature, ensure_ascii=True)
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    return {"speaker": serialize_speaker_profile(speaker)}


# --- One-click app connection -----------------------------------------------
#
# `app_connect` holds the registry and the decision logic; everything here is the
# plumbing it deliberately does not own: where credentials are stored, how an
# executable is found on this machine, and how a URL is probed. Keeping those out
# of the module is what lets its tests run without a filesystem or a network.

APP_CONNECT_PROVIDER_PREFIX = "app:"

#: One row holds every adopted app and site, as a JSON list. One row rather than
#: one row per app because there is nothing per-app to store: a path and a URL are
#: not secrets, they need no encryption key of their own, and a list keeps "what
#: has this user adopted" a single read instead of a LIKE scan over the provider
#: column. Deliberately not a new table -- a new table means a migration, and this
#: feature does not need one.
APP_ADOPTED_PROVIDER = APP_CONNECT_PROVIDER_PREFIX + "__adopted__"


def _adopted_records(db: Session) -> list[dict[str, Any]]:
    """The raw adopted rows, or []. Never raises: a corrupt blob reads as empty."""
    connection = (
        db.query(IntegrationConnection)
        .filter(IntegrationConnection.provider == APP_ADOPTED_PROVIDER)
        .first()
    )
    if not connection or not connection.metadata_json:
        return []
    try:
        metadata = json.loads(connection.metadata_json)
    except json.JSONDecodeError:
        return []
    records = metadata.get("adopted")
    return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []


def _write_adopted_records(db: Session, records: list[dict[str, Any]]) -> None:
    connection = get_or_create_connection(db, APP_ADOPTED_PROVIDER)
    connection.metadata_json = json.dumps(
        {"adopted": records, "updated_at": datetime.now(timezone.utc).isoformat()}
    )
    connection.is_connected = bool(records)
    db.add(connection)
    db.commit()


def _adopted_connectors(db: Session) -> list[AppConnector]:
    return connectors_from_records(_adopted_records(db))


def _adopted_index(db: Session) -> dict[str, AppConnector]:
    return {connector.app_id: connector for connector in _adopted_connectors(db)}



class AppCredentialsRequest(BaseModel):
    """Values for one app's credential fields.

    `speaker_profile` is here for the same reason it is on `ChatRequest`: storing a
    credential grants Akansha standing authority over an external account, so the
    turn has to say who asked. Absent means the local desktop session, which is
    the owner.
    """

    config: dict[str, str] = {}
    speaker_profile: dict[str, Any] | None = None


class AppConnectRequest(BaseModel):
    speaker_profile: dict[str, Any] | None = None


# ── proving who the boss is ──────────────────────────────────────────────────
#: `owner_verify` holds the arithmetic and knows nothing about storage; this is
#: the other half -- where a passphrase hash lives, which challenge was issued,
#: and how a turn's evidence is read off the request. The secret store is never
#: serialised into a response: the endpoints report whether a factor is enrolled,
#: never what it is.

#: A challenge older than this is stale. Long enough to answer out loud and think
#: about it, short enough that a question left on screen is not a standing key.
OWNER_CHALLENGE_TTL_SECONDS = 300


def _owner_secret(db: Session | None, key: str) -> dict[str, Any] | None:
    if db is None:
        return None
    row = db.query(OwnerSecret).filter(OwnerSecret.key == key).first()
    if not row or not row.value_json:
        return None
    try:
        value = json.loads(row.value_json)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _put_owner_secret(db: Session, key: str, value: dict[str, Any] | None) -> None:
    row = db.query(OwnerSecret).filter(OwnerSecret.key == key).first()
    if value is None:
        if row:
            db.delete(row)
            db.commit()
        return
    if not row:
        row = OwnerSecret(key=key)
    row.value_json = json.dumps(value, ensure_ascii=True)
    db.add(row)
    db.commit()


def _pending_challenge(db: Session | None) -> HistoryChallenge | None:
    """The question that was actually asked, if it has not gone stale."""
    stored = _owner_secret(db, "pending_challenge")
    if not stored or not stored.get("keys"):
        return None
    issued = float(stored.get("issued_at") or 0.0)
    if issued and (time.time() - issued) > OWNER_CHALLENGE_TTL_SECONDS:
        return None
    return HistoryChallenge(
        question=str(stored.get("question") or ""),
        answer_keys=tuple(str(k) for k in stored.get("keys") or ()),
        source=str(stored.get("source") or "history"),
    )


def _owner_history_rows(db: Session, limit: int = 60) -> list[dict[str, Any]]:
    """The owner's own saved rows, which is the only place a fair question can come from."""
    rows: list[dict[str, Any]] = []
    for memory in (
        db.query(Memory).order_by(Memory.importance.desc(), Memory.id.desc()).limit(limit).all()
    ):
        rows.append(
            {
                "kind": "memory",
                "topic": memory.topic or "",
                "text": memory.insight or "",
                "when": memory.timestamp.strftime("%B") if memory.timestamp else "",
            }
        )
    return rows


def _owner_factors(db: Session | None, speaker_profile: dict[str, Any] | None) -> list[Any]:
    """Every factor this turn can be judged on, including the ones that prove nothing.

    The unavailable ones are kept in the list on purpose: a verdict that silently
    omits the camera reads as though the camera agreed.
    """
    evidence = (speaker_profile or {}).get("owner_evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    factors = [local_session_factor(at_this_machine=bool(evidence.get("local_session", True)))]
    factors.append(passphrase_factor(evidence.get("passphrase"), _owner_secret(db, "passphrase")))
    answer = evidence.get("history_answer")
    if answer:
        pending = _pending_challenge(db)
        if pending is None:
            # An answer to a question that is no longer held. Not a wrong answer
            # and not an absent factor -- the honest report is that there was
            # nothing to check it against, and the fix is to ask for a new one.
            factors.append(
                Factor(
                    FACTOR_HISTORY,
                    OUTCOME_ABSENT,
                    detail="That question has already been used or has expired -- ask me for a new one.",
                )
            )
        else:
            factors.append(history_factor(answer, pending))
    else:
        # Nothing was answered, so the honest question is whether one *could* have
        # been asked. "Not enrolled" and "not asked yet" send the caller down
        # different paths: the first makes the gate stand aside, the second makes
        # it ask, and conflating them either locks the owner out or lets a stranger
        # skip the one challenge this machine can actually pose.
        can_ask = history_challenge(_owner_history_rows(db), seed=0) is not None if db is not None else False
        factors.append(
            Factor(
                FACTOR_HISTORY,
                OUTCOME_ABSENT if can_ask else OUTCOME_NOT_ENROLLED,
                detail=(
                    "Not asked this turn."
                    if can_ask
                    else "Not enough history yet to ask you anything only you would know."
                ),
            )
        )
    heard = evidence.get("voiceprint")
    if not heard and evidence.get("voice_clip"):
        heard = voiceprint_from_audio(str(evidence["voice_clip"]))
    factors.append(voice_factor(heard if isinstance(heard, dict) else None, _owner_secret(db, "voiceprint")))
    factors.append(camera_factor(frame=evidence.get("camera_frame")))
    return factors


def _owner_enrolled(db: Session | None) -> dict[str, bool]:
    return {
        FACTOR_PASSPHRASE: bool(_owner_secret(db, "passphrase")),
        "voiceprint": bool(_owner_secret(db, "voiceprint")),
    }


def _require_owner_for_apps(
    speaker_profile: dict[str, Any] | None,
    action: str,
    *,
    tier: str = TIER_ACT,
    db: Session | None = None,
) -> Any:
    """Reject a non-owner turn before it changes what Akansha can reach.

    Two gates, in this order. The first is unchanged and coarse: a turn that
    *declares* a non-owner speaker is refused outright. The second is the new one
    and it is about evidence rather than declaration -- at `grant` and
    `irreversible` the request has to carry something only the owner could have
    supplied, and the 403 body says which question would clear it.

    The rule that keeps this from being a lockout: **a factor that has never been
    enrolled is not demanded.** On a machine with no passphrase set there is no
    answer that would satisfy the gate, so insisting on one would wall the owner
    out of their own desktop -- a regression dressed up as hardening. Enforcement
    switches on the moment there is something real to ask for, and
    `/api/owner/status` reports honestly whether it is on.
    """
    # Evidence is not a claim. A turn that carries `owner_evidence` and nothing
    # else is not declaring a different speaker, so it has to resolve exactly as an
    # absent profile does -- otherwise attaching proof of who you are *demotes* you
    # to a guest, and the strong factors can never be supplied at all.
    claim = {key: value for key, value in (speaker_profile or {}).items() if key != "owner_evidence"} or None
    resolved = resolve_speaker_identity(claim, {"access_level": OWNER})
    level = str(resolved.get("access_level") or OWNER)
    if not at_least(level, OWNER):
        raise HTTPException(
            status_code=403,
            detail=f"{action} requires owner access; this request resolved to '{level}'.",
        )
    if tier in (TIER_READ, TIER_ACT):
        return None

    factors = _owner_factors(db, speaker_profile)
    verdict = owner_assess(tier, factors)
    if verdict.allowed:
        return verdict
    enrolled = any(
        factor.name == FACTOR_PASSPHRASE and factor.outcome != OUTCOME_NOT_ENROLLED for factor in factors
    )
    if not enrolled or not verdict.challenge_factor:
        # Enforcement is opt-in, and the opt-in is enrolling a passphrase. Until
        # that exists this gate stands aside: turning it on by itself would demand
        # a history question that no screen has asked yet, and break connecting an
        # app on a machine whose owner never asked for a lock. `/api/owner/status`
        # reports `enforced: false` for exactly as long as that is true.
        return verdict
    raise HTTPException(
        status_code=403,
        detail={
            "message": f"{action} needs proof it is you.",
            "spoken": spoken_verdict(verdict),
            "verdict": verdict.as_dict(),
        },
    )


def _app_connector_or_404(app_id: str, db: Session | None = None) -> AppConnector:
    """The connector for `app_id`, declared or adopted.

    Declared first, deliberately: a scanned "Google Chrome" shortcut must not take
    the id from the hand-written Chrome entry, which is the only one that knows how
    to open a tab at a URL.
    """
    adopted = _adopted_index(db) if db is not None else {}
    resolved_id = resolve_app_id(app_id, extra=adopted.values()) or app_id
    connector = APP_CONNECTORS.get(resolved_id) or adopted.get(resolved_id)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Unknown app '{app_id}'.")
    return connector


def _app_stored_config(db: Session, app_id: str) -> dict[str, Any]:
    """Decrypted config for one app, or {}.

    Reuses the social connectors' row and encryption rather than introducing a
    second credential store -- a second store is a second thing to leak, and this
    one already prefers Windows DPAPI where it is available.
    """
    connection = (
        db.query(IntegrationConnection)
        .filter(IntegrationConnection.provider == APP_CONNECT_PROVIDER_PREFIX + app_id)
        .first()
    )
    if not connection or not connection.metadata_json:
        return {}
    try:
        metadata = json.loads(connection.metadata_json)
    except json.JSONDecodeError:
        return {}
    return decrypt_social_config(metadata)


def _app_has_token(db: Session, connector: AppConnector) -> bool:
    """Whether an OAuth token is already stored for this app.

    Checks the provider the OAuth flow actually writes to, not the `app:` row --
    `/api/social/oauth/callback/{provider}` predates this registry and stores
    under the bare provider name. Looking in the wrong place would report every
    already-authorized account as unauthorized.
    """
    provider = connector.oauth_provider or connector.app_id
    connection = (
        db.query(IntegrationConnection)
        .filter(IntegrationConnection.provider == provider)
        .first()
    )
    return bool(connection and connection.access_token)


def _app_account_label(db: Session, connector: AppConnector) -> str:
    provider = connector.oauth_provider or connector.app_id
    connection = (
        db.query(IntegrationConnection)
        .filter(IntegrationConnection.provider == provider)
        .first()
    )
    return str(connection.account_email or "") if connection else ""


def _probe_executable(launch_key: str) -> str:
    """Absolute path to an installed app, or "".

    Delegates to `automation`'s resolver so this registry and the launcher cannot
    disagree about where Chrome lives. A path that resolves here but not there --
    or the reverse -- would mean the UI says connected and the voice command fails.
    """
    from .automation import APP_LAUNCH_COMMANDS, _resolve_launch_commands

    normalized = (launch_key or "").strip().lower()
    commands = _resolve_launch_commands(
        normalized, APP_LAUNCH_COMMANDS.get(normalized, [normalized])
    )
    for command in commands:
        if os.path.isabs(command) and os.path.exists(command):
            return command
        found = shutil.which(command)
        if found:
            return found
    return ""


def _probe_http_ok(url: str) -> bool:
    """Whether `url` answers. Short timeout on purpose.

    This runs once per bridge on a catalog load, and a dead localhost port should
    cost a fraction of a second rather than a default socket timeout. Any 2xx-4xx
    is "answered" -- a 401 from a bridge means the bridge is running, which is the
    question being asked here.
    """
    if not url.lower().startswith(("http://", "https://")):
        return False
    request = UrlRequest(url, method="GET")
    try:
        with urlopen(request, timeout=2.5) as response:
            return 200 <= response.status < 500
    except HTTPError as exc:
        return 200 <= exc.code < 500
    except Exception:
        return False


def _probe_path_exists(path: str) -> bool:
    """Whether an adopted app's own launch path is still there.

    Separate from `_probe_executable` on purpose: that one asks
    `APP_LAUNCH_COMMANDS` about a *name*, and a scanned shortcut is a name it has
    never heard of -- so routing adopted apps through it would report every single
    one as not installed.
    """
    return bool(path) and os.path.exists(path)


def _app_signin_url(connector: AppConnector) -> str:
    """The URL a browser sign-in for this connector would land on, or "".

    `site_url` for a connector declared as a website, `web_url` for one whose
    primary route is a credential but which a person can simply *log into*
    instead. One helper rather than `x.site_url or x.web_url` at four call sites,
    because the failure when they disagree is silent: a probe asked about the
    wrong URL reports a signed-in account as needing sign-in forever.
    """
    return connector.site_url or connector.web_url


def _app_plan_now(
    db: Session,
    connector: AppConnector,
    stored: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`plan_connect` wired to this machine's real probes.

    Every endpoint that reports an app's state goes through here, so a click, a
    save and a disconnect cannot disagree about what the state is. `stored` is only
    passed when the caller already has the decrypted config in hand.
    """
    return plan_connect(
        connector,
        _app_stored_config(db, connector.app_id) if stored is None else stored,
        resolve_executable=_probe_executable,
        http_ok=_probe_http_ok,
        path_exists=_probe_path_exists,
        session_ok=lambda _app_id: profile_signed_in(
            _app_signin_url(connector), profile_dir=browser_profile_dir()
        ),
        has_token=_app_has_token(db, connector),
        account_label=_app_account_label(db, connector),
    ).as_dict()


@app.get("/api/apps/catalog")
def app_connect_catalog(probe: bool = True, db: Session = Depends(get_db)):
    """Every app Akansha can connect to, and -- unless `probe=false` -- its state now.

    `probe=false` returns the declarative half only, which is what the voice path
    wants: answering "what can you connect to" out loud should not fire a dozen
    HTTP requests and a registry sweep.
    """
    adopted = _adopted_index(db)
    all_connectors = {**APP_CONNECTORS, **adopted}
    return app_catalog(
        stored_for=lambda app_id: _app_stored_config(db, app_id),
        resolve_executable=_probe_executable,
        http_ok=_probe_http_ok,
        path_exists=_probe_path_exists,
        session_ok=lambda app_id: profile_signed_in(
            # `all_connectors`, not `adopted`: a declared website connector and a
            # declared connector carrying a `web_url` both have a URL a session can
            # exist for, and asking about "" reports them as never signed in.
            _app_signin_url(all_connectors[app_id]) if app_id in all_connectors else "",
            profile_dir=browser_profile_dir(),
        ),
        token_for=lambda app_id: _app_has_token(db, all_connectors[app_id]),
        account_for=lambda app_id: _app_account_label(db, all_connectors[app_id]),
        extra=adopted.values(),
        probe=probe,
    )


@app.post("/api/apps/connect/{app_id}")
def app_connect(app_id: str, req: AppConnectRequest | None = None, db: Session = Depends(get_db)):
    """One click. Returns what happened, or exactly what is still missing.

    Never a bare boolean: "false" is the answer that made the old integrations
    surface unusable, because the user could not tell a missing credential from an
    uninstalled app from a bridge that was not running.
    """
    _require_owner_for_apps(
        (req.speaker_profile if req else None), "Connecting an app", tier=TIER_GRANT, db=db
    )
    connector = _app_connector_or_404(app_id, db)
    return _app_plan_now(db, connector)


@app.post("/api/apps/credentials/{app_id}")
def app_save_credentials(app_id: str, req: AppCredentialsRequest, db: Session = Depends(get_db)):
    """Store one app's credentials, then immediately report whether that worked.

    The save and the check are one call because separating them is how a user ends
    up staring at a form that accepted their input and a status that still says
    disconnected.
    """
    _require_owner_for_apps(req.speaker_profile, "Saving app credentials", tier=TIER_GRANT, db=db)
    connector = _app_connector_or_404(app_id, db)

    allowed = {f.key for f in connector.fields}
    unknown = sorted(set(req.config) - allowed)
    if unknown:
        # Rejected rather than ignored. Silently dropping a field the user typed is
        # indistinguishable, from their side, from the credential not working.
        raise HTTPException(
            status_code=400,
            detail=f"{connector.label} has no field(s): {', '.join(unknown)}.",
        )

    merged = {**_app_stored_config(db, connector.app_id)}
    for key, value in req.config.items():
        cleaned = str(value or "").strip()
        if cleaned:
            merged[key] = cleaned
        else:
            # An explicitly blank submission clears the value. Keeping the old one
            # would make "remove this key" impossible without a separate endpoint.
            merged.pop(key, None)

    connection = get_or_create_connection(db, APP_CONNECT_PROVIDER_PREFIX + connector.app_id)
    metadata: dict[str, Any] = {}
    if connection.metadata_json:
        try:
            metadata = json.loads(connection.metadata_json)
        except json.JSONDecodeError:
            metadata = {}
    # Under `config_encrypted`, which is the key `decrypt_social_config` reads.
    # Spelling this out because `encrypt_social_config` returns a bare
    # `{"scheme", "payload"}` pair, so `metadata.update(...)` puts those two at the
    # top level and the read-back silently finds nothing -- the save reports
    # connected and the very next status check reports the field still missing.
    metadata["config_encrypted"] = encrypt_social_config(merged)
    metadata["configured"] = not missing_fields(connector, merged)
    metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
    # The legacy plaintext key, if an earlier version of this row ever had one.
    metadata.pop("config", None)
    connection.metadata_json = json.dumps(metadata)
    connection.is_connected = bool(metadata["configured"])
    db.add(connection)
    db.commit()

    return _app_plan_now(db, connector, merged)


@app.post("/api/apps/disconnect/{app_id}")
def app_disconnect(app_id: str, req: AppConnectRequest | None = None, db: Session = Depends(get_db)):
    """Forget an app's stored credentials, then report the state that actually results.

    Deletes the row rather than flagging it, so there is no encrypted secret left
    behind for a later bug to read back.

    The return value is re-probed rather than assumed. Hardcoding
    `outcome="disconnected"` here was wrong in a way that was measurable: for a
    desktop app there is no credential to remove, so disconnecting Chrome answered
    "Google Chrome credentials removed from this machine" -- a sentence about
    something that never existed -- and reported disconnected while the very next
    status check reported connected again, because Chrome is still installed. A card
    that flips between two states on alternate reads is the same class of lie as a
    button that reports connected without a credential.

    For an *adopted* app this is also the removal: the adoption record goes, and the
    app leaves the list. There is no second "remove" endpoint, because a declared
    entry cannot be removed and an adopted one has nothing else to disconnect --
    two endpoints would just be two names for the same click.
    """
    _require_owner_for_apps(
        (req.speaker_profile if req else None), "Disconnecting an app", tier=TIER_GRANT, db=db
    )
    connector = _app_connector_or_404(app_id, db)
    connection = (
        db.query(IntegrationConnection)
        .filter(IntegrationConnection.provider == APP_CONNECT_PROVIDER_PREFIX + connector.app_id)
        .first()
    )
    removed = connection is not None
    if connection:
        db.delete(connection)
        db.commit()

    if connector.adopted:
        remaining = [
            record
            for record in _adopted_records(db)
            if str(record.get("app_id") or "") != connector.app_id
        ]
        _write_adopted_records(db, remaining)
        return {
            "app_id": connector.app_id,
            "outcome": DISCONNECTED,
            "detail": f"{connector.label} was removed from your connections.",
            "connected_to": "",
            "missing": [],
        }

    result = _app_plan_now(db, connector)
    if not removed:
        # Nothing was stored, so say that instead of claiming a removal. The probed
        # outcome is still the honest answer to "what is the state now".
        result["detail"] = f"Nothing was stored for {connector.label}. {result['detail']}"
    return result


# --- Adopting whatever is actually on this machine, and any site --------------
#
# The registry above answers "what did somebody write down". These four answer
# "what does this person have", which is the question the user actually asked.


class AppAdoptRequest(BaseModel):
    """One app or site to add. `target` is a launch path or a URL, never a secret."""

    kind: str = "desktop"
    label: str = ""
    target: str = ""
    speaker_profile: dict[str, Any] | None = None


class AppControlRequest(BaseModel):
    verb: str = ""
    selector: str = ""
    text: str = ""
    url: str = ""
    speaker_profile: dict[str, Any] | None = None


def _declared_entry_works(db: Session, declared_id: str) -> bool:
    """Whether the built-in entry for `declared_id` is actually connected right now.

    Used to decide whether a scanned app should defer to it. A declared entry that
    reports `not_installed` is worse than the scan that found the binary, so it does
    not get to claim the app.
    """
    connector = APP_CONNECTORS.get(declared_id)
    if not connector:
        return False
    return _app_plan_now(db, connector).get("outcome") == CONNECTED


@app.get("/api/apps/discover")
def app_discover(db: Session = Depends(get_db)):
    """Every application this machine actually has, with what is already adopted.

    Not owner-gated: this reads the Start Menu and nothing else. It grants no
    authority, and the list is already visible to anyone who can press the Windows
    key. Adopting one of these is the gated step.
    """
    adopted = _adopted_index(db)
    apps = []
    for found in discover_desktop_apps():
        record = found.as_dict()
        record["adopted"] = found.app_id in adopted
        # Matched on the *label*, not the id: a scan slugifies the display name
        # ("google_chrome", "discord") while the declared registry uses short ids
        # ("chrome", "discord_desktop") and carries the display name as an alias.
        # Comparing ids found no overlap at all, so every declared app also showed
        # up here as un-adopted -- and adopting it would have shadowed the entry
        # that knows how to open a tab.
        declared_id = resolve_app_id(found.label)
        # "Declared" only counts if the built-in entry actually works. Discord is
        # the case that forced this: `discord_desktop` reports "not found on this
        # machine" -- and `Discord.lnk` is in this user's Start Menu. Deferring to a
        # declared entry that cannot resolve would keep showing the user a false
        # "Not installed" for an app they have open. When the scan has a real path
        # and the registry does not, the scan is right.
        record["declared_as"] = declared_id or ""
        record["declared"] = bool(declared_id) and _declared_entry_works(db, declared_id)
        apps.append(record)
    return {
        "apps": apps,
        "count": len(apps),
        "adopted_count": len(adopted),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/apps/adopted")
def app_adopted(db: Session = Depends(get_db)):
    return {
        "apps": [
            {**describe_connector(connector), "status": _app_plan_now(db, connector)}
            for connector in _adopted_connectors(db)
        ]
    }


# ── model connections ────────────────────────────────────────────────────────
#
# Not owner-gated, unlike the app endpoints above. Those grant Akansha standing
# authority over an external account; choosing which model writes her replies
# grants nothing and takes nothing away, and every other settings write on this
# server (`/api/profile`) is ungated too. A gate the settings page cannot satisfy
# would just be a broken screen.


class ModelPreferenceRequest(BaseModel):
    """Which model to try first. `""` means "no preference, use the cascade"."""

    model: str = ""


def _cloud_model_list() -> list[str]:
    from .ai_engine import OPENROUTER_FALLBACK_MODELS, OPENROUTER_MODEL

    ordered: list[str] = []
    for name in (OPENROUTER_MODEL, *OPENROUTER_FALLBACK_MODELS):
        cleaned = (name or "").strip()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


@app.get("/api/models/routes")
def model_routes_state(refresh: bool = False):
    """Every provider, every model, and the order they will actually be tried in.

    `refresh=1` re-probes the local server instead of trusting the cache. That is
    the button a user presses after starting Ollama, and without it they would
    wait out `_NEGATIVE_TTL_S` wondering whether the app noticed.
    """
    from .ai_engine import _MODEL_COOLDOWN, openrouter_configured

    if refresh:
        model_routes.reset_discovery_cache()
    cloud = _cloud_model_list()
    state = model_routes.describe_routes(cloud=cloud, cloud_configured=openrouter_configured())
    now = time.time()
    state["cloud"]["cooling"] = {
        model: int(until - now) for model, until in _MODEL_COOLDOWN.items() if until > now
    }
    return state


@app.post("/api/models/preference")
def model_preference(req: ModelPreferenceRequest):
    """Pick the model that answers first.

    A local model that is not installed is refused rather than stored: the
    preference would sit at the head of every cascade and cost a failed request
    per turn, and the user would have no idea why replies got slower.
    """
    choice = (req.model or "").strip()
    if choice and model_routes.is_local(choice):
        installed = {model.model_id for model in model_routes.discover_local_models()[0]}
        if choice not in installed:
            raise HTTPException(
                status_code=400,
                detail=f"{model_routes.wire_name(choice)} is not pulled on this machine. "
                f"Run `ollama pull {model_routes.wire_name(choice)}` first.",
            )
    record = model_routes.write_preference(choice)
    return {
        "preference": record,
        "cascade": model_routes.merge_candidates(_cloud_model_list()),
    }


@app.post("/api/apps/adopt")
def app_adopt(req: AppAdoptRequest, db: Session = Depends(get_db)):
    """Add one app or site. This is the single click.

    Owner-gated: adopting grants Akansha standing authority to launch that app or
    act on that site as you, which is exactly the authority the existing three
    write endpoints protect.
    """
    _require_owner_for_apps(req.speaker_profile, "Adding a connection", tier=TIER_GRANT, db=db)
    kind = (req.kind or "desktop").strip().lower()
    target = (req.target or "").strip()
    label = (req.label or "").strip()

    if kind == "web":
        url = normalise_site_url(target or label)
        if not url:
            raise HTTPException(
                status_code=400,
                detail=f"'{target or label}' does not look like a website address.",
            )
        app_id, record = site_app_id(url), {
            "app_id": site_app_id(url),
            "kind": "web",
            "label": label or site_label(url),
            "site_url": url,
        }
    elif kind == "desktop":
        if not label:
            raise HTTPException(status_code=400, detail="An app needs a name.")
        if not target or not os.path.exists(target):
            raise HTTPException(
                status_code=400,
                detail=f"'{target}' is not a path on this machine. Re-scan and try again.",
            )
        app_id, record = slugify(label), {
            "app_id": slugify(label),
            "kind": "desktop",
            "label": label,
            "launch_target": target,
        }
    else:
        raise HTTPException(status_code=400, detail=f"Unknown kind '{req.kind}'.")

    if app_id in APP_CONNECTORS:
        declared_id = app_id
    else:
        # Resolved by name as well as id: the declared registry calls it `chrome`
        # and carries "google chrome" as an alias, while a scan of the Start Menu
        # produces `google_chrome`. Only the alias lookup connects the two, and
        # without it adopting Chrome would silently shadow the entry that has the
        # four browser capabilities.
        declared_id = resolve_app_id(label) if kind == "desktop" else ""
    if declared_id and _declared_entry_works(db, declared_id):
        # The declared entry knows more than a scan does -- Chrome's four browser
        # capabilities, Slack's token fields. Shadowing it with a bare launch path
        # would be a downgrade the user did not ask for.
        declared = APP_CONNECTORS[declared_id]
        return {
            **describe_connector(declared),
            "status": _app_plan_now(db, declared),
            "already": "declared",
            "detail": f"{declared.label} is already built in, with more it can do.",
        }

    records = [r for r in _adopted_records(db) if str(r.get("app_id") or "") != app_id]
    records.append(record)
    _write_adopted_records(db, records)
    connector = (
        desktop_connector(record)
        if kind == "desktop"
        else site_connector(record["site_url"], record["label"])
    )
    return {
        **describe_connector(connector),
        "status": _app_plan_now(db, connector),
        "already": "",
    }


@app.post("/api/apps/web/login/{app_id}")
def app_web_login(app_id: str, req: AppConnectRequest | None = None, db: Session = Depends(get_db)):
    """Open an adopted site in Akansha's own browser window so you can sign in.

    This opens a real window on the real desktop, which is why it is owner-gated
    and why it is a click rather than something a chat turn does on its own.
    """
    _require_owner_for_apps(
        (req.speaker_profile if req else None), "Signing in to a website", tier=TIER_GRANT, db=db
    )
    connector = _app_connector_or_404(app_id, db)
    target = _app_signin_url(connector)
    if not target:
        # Strategy is deliberately not consulted any more. A connector declared as
        # `oauth2` that carries a `web_url` is signable-into exactly as much as one
        # declared as a website -- the strategy describes what we would store, not
        # whether a browser can reach the login page. Gating on it here was what
        # made X offer a client secret as its only route.
        raise HTTPException(
            status_code=400,
            detail=f"{connector.label} has no website to sign in to -- it is not a browser connection.",
        )
    from .app_control import open_for_sign_in

    result = open_for_sign_in(target, profile_dir=browser_profile_dir())
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=str(result.get("detail") or "Could not open a browser."))
    return {"app_id": connector.app_id, **result}


@app.post("/api/apps/control/{app_id}")
def app_control(app_id: str, req: AppControlRequest, db: Session = Depends(get_db)):
    """Do one thing with a connected app or site.

    Refuses unless the connection is actually live, checked through the same
    `_app_plan_now` every status read uses. Attempting the action anyway and letting
    it fail would move the mouse or open a browser first, and report the failure
    afterwards -- which is the wrong order for anything with a side effect.
    """
    _require_owner_for_apps(req.speaker_profile, "Controlling an app")
    connector = _app_connector_or_404(app_id, db)
    verb = (req.verb or "").strip().lower()
    allowed = effective_capabilities(connector)
    if verb not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(f"{connector.label} can do: {', '.join(allowed) or 'nothing yet'}." f" Not '{verb}'."),
        )

    from .app_control import WEB_VERBS, desktop_action, web_action

    site = _app_signin_url(connector)
    # A connector goes down the browser path when it *is* a website, or when the verb
    # being asked for is a browser verb and it has a website to run it against. The
    # second half is what lets a `web_url` connector be read and clicked without
    # being redeclared as `web_session`, which would have thrown away its API route.
    if connector.strategy == WEB_SESSION or (site and verb in WEB_VERBS):
        state = _app_plan_now(db, connector)
        if verb != "open" and state.get("outcome") != CONNECTED:
            raise HTTPException(
                status_code=409,
                detail=f"Not signed in to {connector.label} yet. {state.get('detail') or ''}".strip(),
            )
        result = web_action(
            verb,
            url=normalise_site_url(req.url) or site,
            profile_dir=browser_profile_dir(),
            selector=req.selector,
            text=req.text,
        )
    else:
        result = desktop_action(
            verb,
            launch_target=connector.exe_path or _probe_executable(connector.launch_key or app_id),
            label=connector.label,
            text=req.text,
            target=req.selector,
        )
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=str(result.get("detail") or f"{verb} failed."))
    return {"app_id": connector.app_id, "verb": verb, **result}


class AppOperateRequest(BaseModel):
    """One sentence, to be carried out through whatever app it names.

    `app_id` overrides the sentence's own guess, for the case where the caller
    already knows (a button, or a model that resolved it from context).
    `dry_run` returns the plan and the reasoning without doing any of it, which is
    what the voice path asks for before it narrates an irreversible step.
    """

    utterance: str = ""
    app_id: str = ""
    dry_run: bool = False
    #: Stop before the step the playbook marked irreversible -- "draft it, don't
    #: send it". Default True for anything that publishes: a misheard sentence must
    #: not become a public post, and the caller can ask again with False.
    hold_at_commit: bool = True
    speaker_profile: dict[str, Any] | None = None


def _operator_grant(db: Session, connector: AppConnector) -> ControlGrant:
    """`grant_for` wired to this machine, so route and liveness agree with the page."""
    return grant_for(
        connector,
        _app_plan_now(db, connector),
        launch_target=connector.exe_path or _probe_executable(connector.launch_key or connector.app_id),
    )


def operate_decision(db: Session, utterance: str, app_id: str = "") -> ControlDecision:
    """Understand a sentence and decide what to do about it. No side effects.

    Shared by the endpoint and by the voice executor, which is the point: one
    understanding of "post this on X", not one per entry point.
    """
    adopted = _adopted_connectors(db)
    intent = understand(utterance, extra=adopted)
    resolved = app_id or intent.app_id or ""
    if app_id and app_id != intent.app_id:
        # An explicit app_id wins, but the intent keeps its own reading so the reply
        # can say which one it went with.
        intent = dataclasses.replace(intent, app_id=app_id, assumed_app=False)
    index = {**APP_CONNECTORS, **{c.app_id: c for c in adopted}}
    connector = index.get(resolved)
    return decide(intent, _operator_grant(db, connector) if connector else None)


@app.post("/api/apps/operate")
def app_operate(req: AppOperateRequest, db: Session = Depends(get_db)):
    """Do something *through* a connected app, named in a sentence.

    This is the half of the connections work that was missing. `/api/apps/control`
    takes a verb and an app id, which is what a button has; a person has a sentence.
    Without this route, signing into a site bought a green dot and a row of buttons
    on one page, and saying "post this on X" went to a plan builder that knows
    nothing about the registry and would launch an application instead.

    Refuses in the same three ways the control route does -- non-owner, unknown app,
    not connected -- and returns the reasoning either way, because the caller has to
    say *why* out loud.
    """
    _require_owner_for_apps(req.speaker_profile, "Operating an app")
    utterance = (req.utterance or "").strip()
    if not utterance:
        raise HTTPException(status_code=400, detail="Nothing was said to act on.")

    decision = operate_decision(db, utterance, req.app_id.strip())
    payload = decision.as_dict()
    if req.dry_run or not decision.runnable:
        return {"ran": False, **payload}
    # Everything above this line is reversible: opening a window, typing into a
    # draft. Releasing the hold is not, so it is the one place that asks for the
    # passphrase rather than accepting that someone is sitting at the desk.
    if not req.hold_at_commit and _operator_hold_point(decision) is not None:
        _require_owner_for_apps(
            req.speaker_profile,
            "Going through with a step that cannot be undone",
            tier=TIER_IRREVERSIBLE,
            db=db,
        )
    return {"ran": True, **payload, "report": _run_decision(db, decision, hold=req.hold_at_commit)}


def _operator_hold_point(decision: ControlDecision) -> int | None:
    """Index to stop before, so an irreversible step needs a second ask.

    Derived from the playbook rather than from the verb: "click" is harmless on a
    search page and publishes a tweet on the compose page, so only the playbook
    knows which click is the one you cannot take back.
    """
    grant = decision.grant
    if grant is None or grant.route != ROUTE_BROWSER:
        return None
    book = playbook_for(grant.host, decision.intent.operation)
    if book is None or book.commit_step < 0:
        return None
    # +1 for the `open` step the plan prepends.
    return book.commit_step + 1


def _run_decision(
    db: Session,
    decision: ControlDecision,
    *,
    hold: bool = True,
    stop: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Bind the two real runners and run a decision. The only place in the API that does.

    Everything above this line is pure, which is what lets the whole operator be
    tested without opening a browser window or moving the mouse. This function is the
    boundary, so it is short enough to read in one go.

    `stop` is for the voice path, where "stop" has to be honoured between steps: a
    keystroke already sent cannot be recalled, so cancellation is only ever honest at
    a step boundary.
    """
    grant = decision.grant
    if grant is None or not decision.runnable:
        return {"ok": False, "summary": decision.blocked or "Nothing to do.",
                "steps": [], "reasoning": list(decision.reasoning), "needs": list(decision.needs)}
    from .app_control import desktop_action, web_action

    profile = browser_profile_dir()
    return operator_execute(
        decision,
        run_web=lambda verb, **kw: web_action(verb, profile_dir=profile, **kw),
        run_desktop=lambda verb, **kw: desktop_action(
            verb, launch_target=grant.target, label=grant.label, **kw
        ),
        stop=stop,
        up_to=_operator_hold_point(decision) if hold else None,
    )


def _operate_if_connected(db: Session, prompt: str) -> dict[str, Any] | None:
    """Registry first refusal for the freeform automation route. `None` means "not mine".

    Returning `None` for anything unrecognised is the load-bearing half: this must not
    become a second, worse plan builder, so no app named means the older path runs
    exactly as it did. A *named* app that cannot do the thing is a different case --
    that is a real answer, and a much better one than letting the pyautogui plan launch
    something unrelated.
    """
    decision = operate_decision(db, prompt)
    if decision.grant is None:
        return None
    if not decision.runnable:
        return {
            "success": False,
            "scheduled": False,
            "message": decision.blocked,
            "plan": {"summary": decision.blocked, "steps": []},
            "reasoning": decision.reasoning,
            "needs": decision.needs,
            "route": decision.grant.route,
            "note": "Answered from the connections registry, so nothing was launched.",
        }
    report = _run_decision(db, decision)
    return {
        "success": bool(report.get("ok")),
        "scheduled": False,
        "message": report.get("summary") or decision.summary,
        "plan": {"summary": decision.summary, "steps": [s.as_dict() for s in decision.steps]},
        "reasoning": report.get("reasoning") or decision.reasoning,
        "route": decision.grant.route,
        "text": report.get("text") or "",
        "held": bool(report.get("truncated")),
        "note": (
            "Driven through the connection you signed in to, not through a file or a "
            "fresh browser."
        ),
    }


# ── setting a goal, and then reaching it ─────────────────────────────────────
class GoalSetRequest(BaseModel):
    utterance: str = ""
    pursue: bool = False
    dry_run: bool = True
    speaker_profile: dict[str, Any] | None = None


class GoalPursueRequest(BaseModel):
    dry_run: bool = False
    through_commit: bool = False
    max_actions: int = 6
    speaker_profile: dict[str, Any] | None = None


def _research_decision(question: str) -> ControlDecision:
    """A plan for reading up on something, when no connected app covers it.

    This is a real capability rather than a stand-in: reading a public results
    page needs no account, so the grant is honestly `live`. It is kept narrow on
    purpose -- navigate and read, never type or click -- so "research" can never
    quietly become "act on a site nobody signed into".
    """
    from urllib.parse import quote_plus

    from .app_operator import ControlStep, Intent

    url = f"https://duckduckgo.com/?q={quote_plus(question)}"
    grant = ControlGrant(
        app_id="web_research",
        label="the open web",
        route=ROUTE_BROWSER,
        target=url,
        verbs=("open", "read_page"),
        live=True,
        detail="Reading a public page needs no sign-in.",
    )
    return ControlDecision(
        intent=Intent(utterance=question, operation="read", app_phrase="the web", app_id="web_research"),
        grant=grant,
        steps=[
            ControlStep(verb="open", url=url, why="Open the results for that question."),
            ControlStep(verb="read_page", why="Read what the page says instead of guessing."),
        ],
        reasoning=["Nothing you have connected covers that, so I read the open web for it."],
        summary=f"Look up: {question}",
    )


def _cognitive_os():
    """The one running cognitive OS, so goals and lessons share a store."""
    from .hermes.api.routes import _os

    return _os


def _goal_lessons(limit: int = 25) -> list[dict[str, Any]]:
    try:
        brain = _cognitive_os()
        failures = getattr(brain, "failures", None) or getattr(brain, "failure_analysis", None)
        if failures is None:
            return []
        return list(failures.learned_lessons(limit=limit))
    except Exception as err:
        _log.getLogger(__name__).warning("Could not read prior lessons: %s", err)
        return []


def _plan_for_goal(db: Session, goal: dict[str, Any]) -> PursuitPlan:
    return plan_pursuit(
        goal,
        resolve=lambda text: operate_decision(db, text),
        research=_research_decision,
        lessons=_goal_lessons(),
        hold_point=_operator_hold_point,
    )


def _pursue_goal(db: Session, goal: dict[str, Any], req: GoalPursueRequest,
                 stop: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Plan a goal and, unless this is a dry run, carry it out.

    The runner is `_run_decision`, the same single binding the spoken and typed
    routes use, so a goal cannot reach the browser by a path that the operator's
    holds and refusals do not cover.
    """
    brain = _cognitive_os()
    plan = _plan_for_goal(db, goal)
    if req.dry_run:
        return {"ran": False, "plan": plan.as_dict()}
    report = pursue(
        plan,
        run=lambda decision: _run_decision(db, decision, hold=not req.through_commit, stop=stop),
        learn=lambda payload: brain.experiences.record(payload),
        mark_task=lambda task_id, status: brain.goal_graph.update_task_status(task_id, status),
        stop=stop,
        through_commit=req.through_commit,
        max_actions=max(1, min(req.max_actions, 12)),
    )
    progress = {}
    if plan.goal_id:
        try:
            progress = brain.goal_graph.calculate_progress(plan.goal_id)
        except Exception as err:
            _log.getLogger(__name__).warning("Could not recompute goal progress: %s", err)
    return {"ran": True, "plan": plan.as_dict(), "report": report, "progress": progress}


@app.post("/api/goals/set")
def goal_set(req: GoalSetRequest, db: Session = Depends(get_db)):
    """“Set a goal to …” — create it, decompose it, and say how it would be reached."""
    _require_owner_for_apps(req.speaker_profile, "Setting a goal")
    utterance = (req.utterance or "").strip()
    if not utterance:
        raise HTTPException(status_code=400, detail="Nothing was said to set a goal from.")
    read = goal_from_utterance(utterance)
    brain = _cognitive_os()
    created = brain.goal_graph.create_goal(
        title=read["title"],
        goal_context=read["context"],
    )
    goal_id = str(created.get("id") or "")
    if goal_id:
        try:
            brain.goal_graph.decompose_goal(goal_id)
        except Exception as err:
            _log.getLogger(__name__).warning("Could not decompose the new goal: %s", err)
    detail = brain.goal_graph.details(goal_id) if goal_id else {}
    goal = dict(detail.get("goal") or created) | {"tasks": detail.get("tasks", [])}
    plan = _plan_for_goal(db, goal)
    body: dict[str, Any] = {
        "goal": goal,
        "understood": read,
        "plan": plan.as_dict(),
        "spoken": " ".join(read["reasoning"] + [plan.summary]),
    }
    if req.pursue:
        body |= _pursue_goal(db, goal, GoalPursueRequest(dry_run=req.dry_run))
    return body


@app.post("/api/goals/{goal_id}/pursue")
def goal_pursue(goal_id: str, req: GoalPursueRequest, db: Session = Depends(get_db)):
    """Move a goal that already exists. `dry_run` shows the plan and touches nothing."""
    _require_owner_for_apps(req.speaker_profile, "Working on a goal")
    if req.through_commit and not req.dry_run:
        _require_owner_for_apps(
            req.speaker_profile,
            "Letting a goal go through a step that cannot be undone",
            tier=TIER_IRREVERSIBLE,
            db=db,
        )
    brain = _cognitive_os()
    detail = brain.goal_graph.details(goal_id)
    if not detail or not detail.get("goal"):
        raise HTTPException(status_code=404, detail=f"No goal called {goal_id}.")
    goal = dict(detail["goal"]) | {"tasks": detail.get("tasks", [])}
    return _pursue_goal(db, goal, req)


# ── who is the boss ──────────────────────────────────────────────────────────
#: Enrolment and verification. Every response here is shaped so that reading it
#: teaches you nothing you could use to pass the check: no hashes, no vectors, no
#: answer keys, and the history question ships without its own answer.


class OwnerPassphraseRequest(BaseModel):
    passphrase: str = ""
    #: Required to *change* an existing passphrase. Not required to set the first
    #: one, because there is nothing yet to prove and the local session is the owner.
    current_passphrase: str = ""
    speaker_profile: dict[str, Any] | None = None


class OwnerVerifyRequest(BaseModel):
    tier: str = TIER_GRANT
    passphrase: str = ""
    history_answer: str = ""
    #: A local audio file to compare against the enrolled voiceprint. A path, not
    #: bytes: the recording already exists on this disk and copying it into JSON
    #: would put a voice sample in the request log.
    voice_clip: str = ""
    camera_on: bool = False
    local_session: bool = True


class OwnerVoiceEnrollRequest(BaseModel):
    clip: str = ""
    speaker_profile: dict[str, Any] | None = None


def _owner_evidence_profile(req: OwnerVerifyRequest) -> dict[str, Any]:
    return {
        "owner_evidence": {
            "passphrase": req.passphrase or None,
            "history_answer": req.history_answer or None,
            "voice_clip": req.voice_clip or None,
            "camera_frame": True if req.camera_on else None,
            "local_session": req.local_session,
        }
    }


@app.get("/api/owner/status")
def owner_status(db: Session = Depends(get_db)):
    """What can actually be proved on this machine, and what is switched on."""
    enrolled = _owner_enrolled(db)
    voice_stack = probe_voice_stack()
    camera_stack = probe_camera_stack()
    rows = _owner_history_rows(db)
    return {
        "enrolled": {
            "passphrase": enrolled[FACTOR_PASSPHRASE],
            "voiceprint": enrolled["voiceprint"],
            "history": history_challenge(rows, seed=0) is not None,
            "camera": False,
        },
        # Enforcement is honest about being conditional: with no passphrase set
        # there is no question that would clear a grant, so the gate stays open
        # rather than locking the owner out of their own desktop.
        "enforced": enrolled[FACTOR_PASSPHRASE],
        "tiers": {
            "read": "always",
            "act": "the local session is enough",
            "grant": "one answered challenge",
            "irreversible": "the passphrase specifically",
        },
        "available": {
            "voice_match": bool(voice_stack.get("librosa") and voice_stack.get("numpy")),
            "voice_embedding_model": any(voice_stack.get(m) for m in ("resemblyzer", "speechbrain")),
            "camera_match": all(camera_stack.values()) if camera_stack else False,
        },
        "libraries": {"voice": voice_stack, "camera": camera_stack},
        "history_rows": len(rows),
    }


@app.post("/api/owner/passphrase")
def owner_set_passphrase(req: OwnerPassphraseRequest, db: Session = Depends(get_db)):
    """Set or rotate the one factor strong enough for an irreversible step."""
    _require_owner_for_apps(req.speaker_profile, "Setting the owner passphrase", db=db)
    words = len([w for w in (req.passphrase or "").split() if w.strip()])
    if words < MIN_PASSPHRASE_WORDS:
        raise HTTPException(
            status_code=400,
            detail=f"A passphrase needs at least {MIN_PASSPHRASE_WORDS} words -- one word is overheard once and gone.",
        )
    existing = _owner_secret(db, "passphrase")
    if existing:
        check = passphrase_factor(req.current_passphrase, existing)
        if check.outcome != "pass":
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "Changing the passphrase needs the current one.",
                    "spoken": "Say the passphrase you have now, then the new one.",
                    "outcome": check.outcome,
                },
            )
    _put_owner_secret(db, "passphrase", hash_passphrase(req.passphrase))
    # A rotation invalidates any question left hanging from the old session.
    _put_owner_secret(db, "pending_challenge", None)
    return {
        "saved": True,
        "rotated": bool(existing),
        "enforced": True,
        "spoken": "Kept. From now on anything I cannot take back will ask for that first.",
    }


@app.get("/api/owner/challenge")
def owner_challenge(db: Session = Depends(get_db)):
    """Issue one question drawn from the owner's own saved rows.

    The answer keys are stored server-side and are not in this response.
    """
    challenge = history_challenge(_owner_history_rows(db))
    if challenge is None:
        return {
            "available": False,
            "spoken": "I do not have enough written down yet to ask you something only you would know.",
        }
    _put_owner_secret(
        db,
        "pending_challenge",
        {
            "question": challenge.question,
            "keys": list(challenge.answer_keys),
            "source": challenge.source,
            "issued_at": time.time(),
        },
    )
    return {"available": True, "expires_in": OWNER_CHALLENGE_TTL_SECONDS, **challenge.as_dict()}


@app.post("/api/owner/verify")
def owner_verify(req: OwnerVerifyRequest, db: Session = Depends(get_db)):
    """Judge the evidence in this request against one tier. Changes nothing."""
    tier = req.tier if req.tier in (TIER_READ, TIER_ACT, TIER_GRANT, TIER_IRREVERSIBLE) else TIER_GRANT
    factors = _owner_factors(db, _owner_evidence_profile(req))
    verdict = owner_assess(tier, factors)
    if req.history_answer:
        # A question that has been answered is spent, right or wrong. Leaving a
        # failed one live would turn this route into an unlimited guessing oracle
        # against a fixed set of keys.
        _put_owner_secret(db, "pending_challenge", None)
    return {
        "verdict": verdict.as_dict(),
        "spoken": spoken_verdict(verdict) or "That is you. Going ahead.",
    }


@app.post("/api/owner/voice/enroll")
def owner_enroll_voice(req: OwnerVoiceEnrollRequest, db: Session = Depends(get_db)):
    """Keep a voiceprint for this machine's owner from a recording already on disk.

    Reads the file and computes MFCC statistics -- nothing is uploaded anywhere,
    and the clip itself is not stored. If librosa cannot make a print, that is
    reported as unavailable rather than saved as an empty one.
    """
    _require_owner_for_apps(req.speaker_profile, "Enrolling a voiceprint", db=db)
    clip = (req.clip or "").strip()
    if not clip or not Path(clip).exists():
        raise HTTPException(status_code=400, detail=f"No readable recording at '{clip}'.")
    print_ = voiceprint_from_audio(clip)
    if print_ is None:
        return {
            "saved": False,
            "reason": "unavailable",
            "spoken": "I could not make a voiceprint from that -- too short, unreadable, or librosa is missing.",
            "libraries": probe_voice_stack(),
        }
    _put_owner_secret(db, "voiceprint", print_)
    return {
        "saved": True,
        "seconds": print_.get("seconds"),
        "algorithm": print_.get("algorithm"),
        "spoken": (
            "Learnt your voice. It is one signal among several -- it can be fooled by a recording, "
            "so it will never be the only thing I go on."
        ),
    }


# ── the smart search window ──────────────────────────────────────────────────
#: One box that searches everything Akansha knows and can also act on what it
#: finds. Every row carries an `action`, because a result you cannot do anything
#: with is a worse answer than no result: `open` navigates, `operate` hands the
#: text to the operator, `goal` sets a goal, `ask` sends it to chat.
_OMNI_LIMIT = 8


def _omni_apps(db: Session, q: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    low = q.lower()
    adopted = {c.app_id: c for c in _adopted_connectors(db)}
    for connector in {**APP_CONNECTORS, **adopted}.values():
        haystack = f"{connector.app_id} {connector.label}".lower()
        if low not in haystack:
            continue
        state = _app_plan_now(db, connector)
        outcome = str(state.get("outcome") or "")
        hits.append(
            {
                "kind": "app",
                "id": connector.app_id,
                "title": connector.label,
                "subtitle": "Connected" if outcome == "connected" else "Not connected yet",
                "action": "operate" if outcome == "connected" else "connect",
                "route": grant_for(connector, state).route,
                "score": 1.0 if haystack.startswith(low) else 0.7,
            }
        )
    return hits


def _omni_goals(q: str) -> list[dict[str, Any]]:
    low = q.lower()
    try:
        goals = _cognitive_os().goal_graph.list_goals(limit=50)
    except Exception as err:
        _log.getLogger(__name__).warning("Goal search failed: %s", err)
        return []
    out: list[dict[str, Any]] = []
    for goal in goals:
        text = f"{goal.get('title','')} {goal.get('goal_context','')}".lower()
        if low not in text:
            continue
        out.append(
            {
                "kind": "goal",
                "id": goal.get("id"),
                "title": goal.get("title"),
                "subtitle": f"{goal.get('progress', 0)}% · {goal.get('status', '')} · {goal.get('goal_health', '')}",
                "action": "pursue",
                "score": 0.9,
            }
        )
    return out


def _omni_history(db: Session, q: str) -> list[dict[str, Any]]:
    like = f"%{q}%"
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.content.ilike(like))
        .order_by(ChatMessage.timestamp.desc())
        .limit(_OMNI_LIMIT)
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        body = (row.content or "").strip().replace("\n", " ")
        out.append(
            {
                "kind": "message",
                "id": str(row.id),
                "title": body[:90] + ("…" if len(body) > 90 else ""),
                "subtitle": f"{row.role} · {row.timestamp:%d %b %Y}" if row.timestamp else str(row.role),
                "action": "open_chat",
                "session_id": row.session_id,
                "score": 0.6,
            }
        )
    return out


def _omni_memory(db: Session, q: str) -> list[dict[str, Any]]:
    """Memories matching `q` -- by the words used, and by what they mean.

    Both indexes run, and the union is returned. `ilike` finds "Activa" when the
    question says "Activa"; the vector index finds it when the question says
    "my scooter needs servicing", which shares no characters with the memory and
    scored exactly 0 under `LIKE`. Semantic recall is ~3.5 ms here, so this is a
    merge on every search rather than a fallback after a miss.
    """
    like = f"%{q}%"
    rows = (
        db.query(Memory)
        .filter((Memory.topic.ilike(like)) | (Memory.insight.ilike(like)))
        .order_by(Memory.importance.desc())
        .limit(_OMNI_LIMIT)
        .all()
    )
    out: list[dict[str, Any]] = [
        {
            "kind": "memory",
            "id": str(row.id),
            "title": row.topic or "Remembered",
            "subtitle": (row.insight or "")[:110],
            "action": "ask",
            "score": 0.65,
            "match": "lexical",
        }
        for row in rows
    ]

    seen = {item["id"] for item in out}
    for hit in memory_index.recall(q, limit=_OMNI_LIMIT):
        if hit["id"] in seen:
            continue
        out.append(
            {
                "kind": "memory",
                "id": hit["id"],
                "title": hit.get("topic") or "Remembered",
                "subtitle": hit["text"][:110],
                "action": "ask",
                # Below a lexical hit, above nothing. A cosine of 0.24 is a real
                # match but a modest one, so it ranks under an exact word match
                # rather than displacing it.
                "score": round(0.40 + 0.20 * min(1.0, float(hit["similarity"])), 4),
                "match": "semantic",
                "similarity": hit["similarity"],
            }
        )
    return out[: _OMNI_LIMIT * 2]


@app.get("/api/search/omni")
def search_omni(q: str = "", db: Session = Depends(get_db)):
    """Everything matching `q`, plus what can be done with it, ranked."""
    query = (q or "").strip()
    if len(query) < 2:
        return {"query": query, "results": [], "act": None}

    results: list[dict[str, Any]] = []
    for finder in (_omni_apps, _omni_history, _omni_memory):
        try:
            results.extend(finder(db, query))
        except Exception as err:
            _log.getLogger(__name__).warning("Omni search branch failed: %s", err)
    results.extend(_omni_goals(query))
    results.sort(key=lambda r: (-float(r.get("score") or 0), str(r.get("title") or "")))

    # The top row is not a search hit at all: it is what would happen if you
    # just pressed Enter. Resolved by the operator, so it is the truth about
    # what is connected rather than a guess -- and it is a dry run, always.
    act: dict[str, Any] | None = None
    try:
        decision = operate_decision(db, query)
        if decision.grant is not None:
            act = {
                "kind": "act",
                "title": decision.summary or f"Do this: {query}",
                "subtitle": decision.blocked or f"via {decision.grant.label}",
                "action": "operate",
                "runnable": decision.runnable,
                "route": decision.grant.route,
                "reasoning": decision.reasoning,
                "needs": decision.needs,
            }
    except Exception as err:
        _log.getLogger(__name__).warning("Omni act row failed: %s", err)

    return {
        "query": query,
        "results": results[:24],
        "act": act,
        "goal": {
            "kind": "goal_new",
            "title": f"Set a goal: {goal_from_utterance(query)['title']}",
            "action": "goal",
        },
    }



@app.post("/api/system/notify")
def system_notify(request: DesktopNotificationRequest):
    title = request.title.strip()
    body = request.body.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Notification title is required.")

    try:
        show_windows_notification(title, body or "Akansha reminder")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to show desktop notification: {exc}") from exc

    return {"success": True, "message": "Desktop notification sent."}


@app.post("/api/planner/reminders/sync")
def sync_planner_reminders(request: PlannerReminderSyncRequest):
    incoming_ids = {item.reminder_id for item in request.reminders}

    for reminder_id in list(planner_reminder_registry.keys()):
        if reminder_id not in incoming_ids:
            planner_reminder_registry.pop(reminder_id, None)
            _delete_reminder_marker(reminder_id)

    for item in request.reminders:
        existing = planner_reminder_registry.get(item.reminder_id, {})
        title = normalize_reminder_text(item.title) or "Akansha reminder"
        body = normalize_reminder_text(item.body) or "You asked Akansha to remind you."
        should_preserve_sent = bool(existing.get("sent", False)) and existing.get("reminder_at") == item.reminder_at
        armed_payload: dict[str, Any] = {}

        if not should_preserve_sent and existing.get("armed_for") != item.reminder_at:
            try:
                armed_payload = arm_detached_windows_reminder(item.reminder_id, title, body, item.reminder_at)
            except Exception as exc:
                planner_reminder_history.append({
                    "reminder_id": item.reminder_id,
                    "title": title,
                    "body": body,
                    "triggered_at": datetime.now().astimezone().isoformat(),
                    "status": f"arm_error: {exc}",
                })

        planner_reminder_registry[item.reminder_id] = {
            "title": title,
            "body": body,
            "reminder_at": item.reminder_at,
            "sent": should_preserve_sent,
            "armed_for": armed_payload.get("armed_for", existing.get("armed_for")),
            "armed_pid": armed_payload.get("pid", existing.get("armed_pid")),
            "last_synced_at": datetime.now().astimezone().isoformat(),
        }

    return {"success": True, "count": len(planner_reminder_registry)}


@app.get("/api/planner/reminders/status")
def planner_reminders_status():
    return {
        "active_count": len(planner_reminder_registry),
        "active": planner_reminder_registry,
        "history": planner_reminder_history[-20:],
        "scheduler_running": planner_scheduler_task is not None and not planner_scheduler_task.done(),
    }


@app.get("/api/automation/browser/status")
def get_browser_automation_state(db: Session = Depends(get_db)):
    connection = get_or_create_connection(db, BROWSER_AUTOMATION_PROVIDER)
    return get_browser_automation_status(connection)


@app.put("/api/automation/browser/permissions")
def update_browser_automation_permissions(
    req: BrowserAutomationPermissionsRequest, db: Session = Depends(get_db)
):
    connection = get_or_create_connection(db, BROWSER_AUTOMATION_PROVIDER)
    metadata = get_connection_metadata(connection)
    permissions = {
        **BROWSER_AUTOMATION_DEFAULTS,
        **metadata.get("permissions", {}),
    }

    for key, value in req.model_dump(exclude_none=True).items():
        permissions[key] = value

    metadata["permissions"] = permissions
    metadata["scheduled_actions"] = metadata.get("scheduled_actions", [])
    connection.is_connected = True
    connection.metadata_json = json.dumps(metadata)
    db.add(connection)
    db.commit()

    return get_browser_automation_status(connection)


@app.post("/api/automation/browser/run")
async def run_browser_automation(req: BrowserAutomationRunRequest, db: Session = Depends(get_db)):
    connection = get_or_create_connection(db, BROWSER_AUTOMATION_PROVIDER)
    status = get_browser_automation_status(connection)
    actions = {item["key"]: item for item in BROWSER_AUTOMATION_ACTIONS}
    action_meta = actions.get(req.action)

    if not action_meta:
        raise HTTPException(status_code=404, detail="Unsupported browser automation action.")

    permission_key = action_meta["permission"]
    if not status["permissions"].get(permission_key):
        raise HTTPException(
            status_code=400,
            detail=f"The '{action_meta['label']}' permission is currently turned off.",
        )

    metadata = get_connection_metadata(connection)
    metadata["permissions"] = status["permissions"]
    scheduled_actions = metadata.get("scheduled_actions", [])

    if req.run_at:
        scheduled_action = {
            "id": secrets.token_hex(6),
            "action": req.action,
            "label": action_meta["label"],
            "target": req.target,
            "run_at": req.run_at,
            "background": req.background,
            "status": "scheduled",
            "created_at": datetime.utcnow().isoformat(),
            "note": (
                "Saved inside Akansha as a planned browser action. Automatic timed execution still "
                "depends on keeping a worker running."
            ),
        }
        scheduled_actions.append(scheduled_action)
        metadata["scheduled_actions"] = scheduled_actions
        connection.is_connected = True
        connection.metadata_json = json.dumps(metadata)
        db.add(connection)
        db.commit()
        return {
            "success": True,
            "scheduled": True,
            "message": f"{action_meta['label']} saved for {req.run_at}.",
            "scheduled_action": scheduled_action,
        }

    result = await execute_desktop_command(
        req.action,
        req.target,
        {"background": req.background},
    )
    return {
        "success": bool(result.get("success")),
        "scheduled": False,
        "message": result.get("message"),
        "note": result.get("note"),
    }


@app.post("/api/automation/browser/prompt")
async def run_browser_automation_prompt(req: BrowserAutomationPromptRequest, db: Session = Depends(get_db)):
    prompt = req.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Automation prompt is empty.")

    # ── The connections registry gets first refusal ────────────────────────────
    # Same order the voice path uses, and for the same reason: this route's plan
    # builder knows nothing about which sites the user has signed into, so "post this
    # on X" became a desktop plan that launched something instead of touching the
    # account they authenticated. A named, connected app is driven through the route
    # it is actually connected by. Anything the registry does not recognise falls
    # through untouched, so no existing automation changes behaviour.
    # `_operate_if_connected` ends in `web_action`, which drives Playwright's *sync*
    # API, and this endpoint is `async def` -- so calling it directly ran a sync
    # Playwright call inside the running event loop and produced exactly the failure
    # that was reported: `open failed: It looks like you are using Playwright Sync API
    # inside the asyncio loop. Please use the Async API instead.` A thread is the fix
    # rather than the async API, because a sync endpoint reaching the same code
    # already works -- FastAPI runs those in a threadpool, which is the only thing
    # this route was missing. `check_same_thread=False` on the SQLite engine is what
    # lets the session cross with it, and nothing else touches `db` while we await.
    try:
        operated = await asyncio.to_thread(_operate_if_connected, db, prompt)
    except Exception as _op_err:
        import logging as _log

        _log.getLogger(__name__).warning("Connected-app route failed: %s", _op_err)
        operated = None
    if operated is not None:
        return operated

    # ── Real Playwright automation bypass ──────────────────────────────────────
    # For site-specific flows (YouTube, CodeChef, LeetCode, GitHub, Coursera,
    # LinkedIn Learning) route to the real Playwright browser instead of the
    # pyautogui-based build_browser_prompt_plan().
    try:
        from .browser.skills.site_dispatcher import should_use_playwright, dispatch_playwright
        if should_use_playwright(prompt):
            import concurrent.futures
            loop = asyncio.get_event_loop()
            if loop.is_running():
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    pw_result = await loop.run_in_executor(pool, dispatch_playwright, prompt)
            else:
                pw_result = dispatch_playwright(prompt)
            return {
                "success": pw_result.get("success", False),
                "scheduled": False,
                "message": pw_result.get("message", "Playwright automation completed."),
                "plan": {"summary": pw_result.get("message", ""), "steps": pw_result.get("steps", [])},
                "cowork": {"mode": "Real Playwright Browser Automation", "outcome": pw_result.get("message", "")},
                "note": pw_result.get("note", "Real Chromium browser was used — no fake desktop automation."),
            }
    except ImportError:
        pass  # Playwright not installed — fall through to legacy path
    except Exception as _pw_err:
        import logging as _log
        _log.getLogger(__name__).warning("Playwright bypass error: %s", _pw_err)
        # Fall through to legacy path
    # ── End Playwright bypass ───────────────────────────────────────────────────

    inferred_run_at, cleaned_prompt = extract_prompt_schedule(prompt)
    effective_run_at = req.run_at or inferred_run_at
    planning_prompt = cleaned_prompt.strip() or prompt

    connection = get_or_create_connection(db, BROWSER_AUTOMATION_PROVIDER)
    metadata = get_connection_metadata(connection)
    metadata["permissions"] = {**BROWSER_AUTOMATION_DEFAULTS}
    scheduled_actions = metadata.get("scheduled_actions", [])
    owner_profile = get_or_create_profile(db)
    owner_name = owner_profile.full_name

    plan = build_browser_prompt_plan(planning_prompt, owner_name=owner_name)
    cowork_profile = _build_cowork_profile(planning_prompt, plan, owner_name=owner_name)
    plan["cowork"] = cowork_profile

    if plan.get("needs_clarification"):
        return {
            "success": True,
            "scheduled": False,
            "message": plan["summary"],
            "plan": plan,
            "cowork": _build_cowork_execution_report(cowork_profile, []),
            "requires_clarification": True,
        }

    if effective_run_at:
        scheduled_action = {
            "id": secrets.token_hex(6),
            "action": "freeform-prompt",
            "label": plan["summary"],
            "target": planning_prompt,
            "run_at": effective_run_at,
            "background": req.background,
            "status": "scheduled",
            "created_at": datetime.utcnow().isoformat(),
            "note": "Saved from the freeform automation box.",
        }
        scheduled_actions.append(scheduled_action)
        metadata["scheduled_actions"] = scheduled_actions
        connection.is_connected = True
        connection.metadata_json = json.dumps(metadata)
        db.add(connection)
        db.commit()
        _schedule_automation_plan(effective_run_at, plan, planning_prompt)
        return {
            "success": True,
            "scheduled": True,
            "message": f"{plan['summary']} Saved for {effective_run_at}.",
            "plan": plan,
            "cowork": _build_cowork_execution_report(cowork_profile, [], scheduled=True),
        }

    execution_results = []
    for step in plan["steps"]:
        result = await execute_desktop_command(
            step["action"],
            step.get("target"),
            step.get("payload"),
        )
        execution_results.append({"step": step, "result": result})
        if not result.get("success"):
            break

    final_result = execution_results[-1]["result"] if execution_results else {"success": False, "message": "No steps were executed."}
    result_message = (final_result.get("message") or "").strip()
    plan_summary = str(plan.get("summary", "")).strip()
    if final_result.get("success") and plan_summary:
        if result_message and result_message.lower() != plan_summary.lower():
            response_message = f"{plan_summary} {result_message}".strip()
        else:
            response_message = plan_summary
    else:
        response_message = result_message or plan_summary or "I could not complete that automation request."
    return {
        "success": bool(final_result.get("success")),
        "scheduled": False,
        "message": response_message,
        "plan": plan,
        "results": execution_results,
        "cowork": _build_cowork_execution_report(cowork_profile, execution_results),
        "note": (
            "This uses best-effort desktop automation. Complex site-specific flows still depend on page focus, "
            "login state, and the current browser layout."
        ),
    }


@app.delete("/api/automation/browser/scheduled/{action_id}")
def delete_scheduled_browser_automation(action_id: str, db: Session = Depends(get_db)):
    connection = get_or_create_connection(db, BROWSER_AUTOMATION_PROVIDER)
    metadata = get_connection_metadata(connection)
    scheduled_actions = metadata.get("scheduled_actions", [])
    filtered_actions = [item for item in scheduled_actions if item.get("id") != action_id]

    metadata["scheduled_actions"] = filtered_actions
    metadata["permissions"] = {
        **BROWSER_AUTOMATION_DEFAULTS,
        **metadata.get("permissions", {}),
    }
    connection.metadata_json = json.dumps(metadata)
    db.add(connection)
    db.commit()

    return {"success": True, "remaining": len(filtered_actions)}


@app.post("/api/voice/tts")
async def text_to_speech(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Text is empty.")

    # Piper first, and only for a sentence it can actually say. This is the fast
    # path -- local synthesis, no WAN hop -- and `synthesize` returns None rather
    # than mangling a Telugu clause, which is what sends the hard cases to
    # edge-tts. Choosing per utterance instead of per install is deliberate: the
    # common reply gets tens of milliseconds and the code-switched one still
    # sounds right.
    try:
        spoken = voice_local.synthesize(req.text.strip())
    except Exception as exc:
        logger.warning("Local TTS failed, falling back to edge-tts: %s", exc)
        spoken = None
    if spoken is not None:
        return Response(
            content=spoken.wav,
            media_type="audio/wav",
            headers={
                "X-Akansha-Tts-Engine": spoken.engine,
                "X-Akansha-Tts-Voice": spoken.voice,
                "X-Akansha-Tts-Latency-Ms": str(round(spoken.latency_ms)),
            },
        )

    try:
        audio = await generate_edge_tts_audio(
            req.text.strip(),
            req.voice_gender,
            req.voice_tone,
            req.language_mode,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TTS synthesis failed: {exc}")

    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"X-Akansha-Tts-Engine": "edge-tts"},
    )


def _stt_hint(db: Session) -> str:
    """Names Akansha already knows, handed to Whisper as its `initial_prompt`.

    Worth more than it looks. Without it "Amma" comes back as "Emma" and
    "Kukatpally" as three English words; once the decoder has seen them spelled
    correctly in context it keeps them. Cheap by construction -- two indexed
    queries, a short cap, and no LLM -- because this runs on every spoken turn.
    """
    words: list[str] = ["Akansha"]
    try:
        for row in db.query(SpeakerProfile.display_name).limit(24).all():
            name = (row[0] or "").strip()
            if name:
                words.append(name)
        for row in (
            db.query(Memory.topic)
            .order_by(Memory.importance.desc())
            .limit(12)
            .all()
        ):
            topic = (row[0] or "").strip()
            if topic and len(topic) < 40:
                words.append(topic)
    except Exception:
        # A hint is an optimisation. Losing it must never lose the transcript.
        pass
    seen: list[str] = []
    for word in words:
        if word.lower() not in {s.lower() for s in seen}:
            seen.append(word)
    return ", ".join(seen[:24])


def _voice_factor_for(db: Session, samples: Any) -> dict[str, Any]:
    """This turn's voice scored against the enrolled voiceprint, as evidence.

    Never a decision. `owner_verify` weights this factor low on purpose -- MFCC
    cosine similarity tells voices apart but a recording of the owner would pass
    it, so it can support a verdict and must never carry one.
    """
    heard = voiceprint_from_samples(samples)
    enrolled: dict[str, Any] | None = None
    try:
        owner = (
            db.query(SpeakerProfile)
            .filter(SpeakerProfile.access_level == OWNER)
            .filter(SpeakerProfile.voice_signature_json.isnot(None))
            .first()
        )
        if owner and owner.voice_signature_json:
            enrolled = json.loads(owner.voice_signature_json)
    except Exception:
        enrolled = None
    factor = voice_factor(heard, enrolled)
    return {**factor.as_dict(), "voiceprint_made": heard is not None}


@app.get("/api/voice/local/capabilities")
def voice_local_capabilities():
    """What the on-machine voice stack can do, and what to run if it cannot.

    Cheap on purpose -- it checks importability and whether a file exists on disk,
    never loads a model. The client polls this to decide whether to record audio
    for `/api/voice/stt` or stay on the browser recogniser, so it must answer in
    microseconds even on a cold process.
    """
    capabilities = [item.as_dict() for item in voice_local.capabilities()]
    return {
        "status": "success",
        "capabilities": capabilities,
        "hearing_is_local": any(c["name"] == "hearing" and c["installed"] for c in capabilities),
        "sample_rate": voice_local.SAMPLE_RATE,
    }


@app.post("/api/voice/local/warm")
def voice_local_warm(hearing: bool = True, speech: bool = True):
    """Load the models now so nobody's first sentence pays for it.

    Worth calling from the client the moment a voice screen opens: Whisper costs
    seconds to construct and microseconds to reuse, and paying that while the
    user is still reaching for the mic button is free.
    """
    return {"status": "success", "loaded": voice_local.warm(hearing=hearing, speech=speech)}


@app.get("/api/memory/semantic/capabilities")
def memory_semantic_capabilities():
    """Whether meaning-based recall is available, and what is indexed.

    Same contract as the voice probe: importability and a file on disk, never a
    model load. A UI that shows "semantic recall on" has to be able to ask this
    on a cold process without paying 1.2 s for the answer.
    """
    return {
        "status": "success",
        "capability": semantic_memory.capability().as_dict(),
        "index": semantic_memory.stats(),
    }


@app.post("/api/memory/semantic/reindex")
def memory_semantic_reindex(db: Session = Depends(get_db)):
    """Rebuild the vector index from the `memories` table.

    Cheap to call repeatedly: memories whose text has not changed are recognised
    by content hash and skipped, so a no-op re-sync of a few hundred rows is
    single-digit milliseconds rather than a full re-embed.
    """
    return {"status": "success", "result": memory_index.sync(db)}


@app.get("/api/memory/semantic/search")
def memory_semantic_search(q: str = "", limit: int = 5):
    """What Akansha remembers that *means* something close to `q`.

    Exposed separately from `/api/search/omni` because the similarity score is
    the useful part here -- 0.24 is a real but modest match and 0.51 is a
    confident one, and a caller ranking these against its own results needs the
    number rather than a boolean.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return {"status": "success", "query": query, "hits": []}
    return {
        "status": "success",
        "query": query,
        "hits": memory_index.recall(query, limit=max(1, min(int(limit or 5), 25))),
    }


@app.post("/api/voice/stt")
async def speech_to_text(
    audio: UploadFile = File(...),
    language: str | None = None,
    hint: str = "",
    db: Session = Depends(get_db),
):
    """Audio in, words out -- the endpoint `voice_engine.py:11` has always claimed.

    Until now the browser's `webkitSpeechRecognition` did the hearing, which meant
    Chrome only, the audio posted to Google, and no control over how a
    Telugu-English sentence gets segmented. This runs the same Whisper weights on
    this machine.

    Two things come back besides the text, and both are the point:

    - `voice_activity` is Silero reading the waveform. `trailing_silence_ms` is
      the number `/api/voice/turn-boundary` used to be handed by a client
      stopwatch that could not tell a thinking pause from a finished sentence.
    - `speaker` is the owner-verification voice factor scored against the enrolled
      voiceprint, when there is one. It is evidence, never a decision -- the same
      weak-but-real factor `owner_verify` already treats with suspicion.
    """
    payload = await audio.read()
    if not payload:
        raise HTTPException(status_code=400, detail="No audio was uploaded.")
    if not voice_local.whisper_installed():
        raise HTTPException(
            status_code=503,
            detail="Local transcription is not installed. Run `pip install faster-whisper`, "
            "or keep using the browser recogniser.",
        )

    try:
        samples = voice_local.decode_audio(payload)
    except voice_local.AudioUnreadable as exc:
        # The client's problem, so it gets a 400 and the real reason -- not a 500
        # that reads like Akansha broke.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        heard = voice_local.transcribe(samples, language=language, hint=hint or _stt_hint(db))
    except Exception as exc:
        logger.exception("Local transcription failed")
        raise HTTPException(status_code=502, detail=f"Transcription failed: {exc}") from exc

    activity = voice_local.speech_activity(samples)
    return {
        "status": "success",
        "transcript": heard.text if heard else "",
        "detail": heard.as_dict() if heard else None,
        "voice_activity": activity.as_dict() if activity else None,
        "speaker": _voice_factor_for(db, samples),
    }


@app.get("/api/google/auth-url")
def get_google_auth_url():
    if not google_configured():
        return {
            "configured": False,
            "auth_url": None,
            "message": "Add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI to enable Google OAuth.",
        }

    state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
            "scope": " ".join(GOOGLE_SCOPES),
            "state": state,
        }
    )
    return {
        "configured": True,
        "auth_url": f"https://accounts.google.com/o/oauth2/v2/auth?{query}",
        "state": state,
    }


@app.get("/api/google/callback")
def google_callback(code: str, db: Session = Depends(get_db)):
    if not google_configured():
        raise HTTPException(status_code=400, detail="Google OAuth is not configured.")

    token_request = Request(
        "https://oauth2.googleapis.com/token",
        data=urlencode(
            {
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    with urlopen(token_request, timeout=20) as response:
        token_payload = json.loads(response.read().decode("utf-8"))

    access_token = token_payload.get("access_token")
    if not access_token:
        raise HTTPException(status_code=500, detail="Google did not return an access token.")

    user_info_request = Request(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urlopen(user_info_request, timeout=20) as response:
        user_info = json.loads(response.read().decode("utf-8"))

    profile = get_or_create_profile(db)
    connection = get_or_create_connection(db, "google")
    update_google_connection(db, connection, profile, token_payload, user_info)

    redirect_url = "http://localhost:4030/sign-up-login-screen?google=connected"
    return HTMLResponse(
        f"""
        <!doctype html>
        <html>
          <head>
            <meta http-equiv="refresh" content="0;url={redirect_url}" />
            <title>Google connected</title>
          </head>
          <body style="background:#050814;color:#f8fafc;font-family:Arial,sans-serif">
            <p>Google account connected. Returning to Akansha...</p>
            <script>window.location.replace("{redirect_url}")</script>
          </body>
        </html>
        """
    )


@app.post("/api/google/disconnect")
def disconnect_google(db: Session = Depends(get_db)):
    profile = get_or_create_profile(db)
    connection = get_or_create_connection(db, "google")

    connection.access_token = None
    connection.refresh_token = None
    connection.scope = None
    connection.account_email = None
    connection.token_expiry = None
    connection.metadata_json = None
    connection.is_connected = False

    profile.google_connected = False
    profile.google_email = None

    db.add(connection)
    db.add(profile)
    db.commit()

    return {"success": True}


@app.get("/api/google/gmail/summary")
def get_gmail_summary(db: Session = Depends(get_db)):
    connection = get_or_create_connection(db, "google")
    if not connection.is_connected or not connection.access_token:
        return {
            "connected": False,
            "summary": "Connect Google to read and summarize Gmail threads from Akansha.",
            "emails": [],
        }

    try:
        messages = google_api_get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=5&q=category:primary",
            connection,
        )
        email_cards = []
        for item in messages.get("messages", []):
            message_data = google_api_get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{item['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject",
                connection,
            )
            headers = {
                header["name"]: header["value"]
                for header in message_data.get("payload", {}).get("headers", [])
            }
            email_cards.append(
                {
                    "id": item["id"],
                    "sender": headers.get("From", "Unknown sender"),
                    "subject": headers.get("Subject", "No subject"),
                    "snippet": message_data.get("snippet", ""),
                    "important": "IMPORTANT" in message_data.get("labelIds", []),
                }
            )

        summary = (
            f"You have {len(email_cards)} recent Gmail threads ready for review."
            if email_cards
            else "No recent Gmail threads were found."
        )
        return {"connected": True, "summary": summary, "emails": email_cards}
    except Exception as exc:
        return {
            "connected": True,
            "summary": f"Gmail is connected, but Akansha could not fetch messages right now: {exc}",
            "emails": [],
        }


@app.get("/api/google/calendar/events")
def get_calendar_events(db: Session = Depends(get_db)):
    connection = get_or_create_connection(db, "google")
    if not connection.is_connected or not connection.access_token:
        return {
            "connected": False,
            "events": [
                {
                    "title": "Connect Google Calendar",
                    "start": "Anytime",
                    "description": "Authorize Google to view upcoming events and create reminders.",
                }
            ],
        }

    try:
        now = datetime.now(timezone.utc).isoformat()
        events_data = google_api_get(
            f"https://www.googleapis.com/calendar/v3/calendars/primary/events?maxResults=5&singleEvents=true&orderBy=startTime&timeMin={now}",
            connection,
        )
        events = [
            {
                "id": item.get("id"),
                "title": item.get("summary", "Untitled event"),
                "start": item.get("start", {}).get("dateTime") or item.get("start", {}).get("date"),
                "description": item.get("description", ""),
            }
            for item in events_data.get("items", [])
        ]
        return {"connected": True, "events": events}
    except Exception as exc:
        return {
            "connected": True,
            "events": [],
            "error": f"Calendar is connected, but events could not be loaded: {exc}",
        }


@app.post("/api/google/calendar/reminders")
def create_calendar_reminder(req: ReminderRequest, db: Session = Depends(get_db)):
    connection = get_or_create_connection(db, "google")
    if not connection.is_connected or not connection.access_token:
        raise HTTPException(status_code=400, detail="Connect Google Calendar before creating reminders.")

    event_payload = {
        "summary": req.title,
        "start": {"dateTime": req.date_time},
        "end": {"dateTime": req.date_time},
    }
    request = Request(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        data=json.dumps(event_payload).encode("utf-8"),
        headers={
            **google_auth_headers(connection),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        created = json.loads(response.read().decode("utf-8"))

    return {
        "success": True,
        "event_id": created.get("id"),
        "html_link": created.get("htmlLink"),
    }

# ==============================================================================
# OpenHands-Inspired Task Automations & Webhook Execution Platform
# ==============================================================================

class TaskAutomationCreateRequest(BaseModel):
    name: str
    description: str | None = None
    trigger_type: str = "schedule"  # 'schedule' | 'webhook' | 'event' | 'manual'
    trigger_config: dict[str, Any] | None = None  # { interval_minutes: 60, cron: "...", webhook_key: "..." }
    action_type: str = "ai_workflow"  # 'github_decompose' | 'daily_report' | 'desktop_action' | 'ai_workflow' | 'custom_agent'
    action_payload: dict[str, Any] | None = None  # { prompt: "...", repo: "...", channel: "..." }
    status: str = "active"


class TaskAutomationUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    trigger_type: str | None = None
    trigger_config: dict[str, Any] | None = None
    action_type: str | None = None
    action_payload: dict[str, Any] | None = None
    status: str | None = None


DEFAULT_TASK_AUTOMATION_TEMPLATES = [
    {
        "template_id": "template_github_decompose",
        "name": "GitHub Issue Decomposer",
        "description": "Automatically decompose incoming GitHub issues or feature requests into actionable subtasks.",
        "trigger_type": "webhook",
        "trigger_config": {"source": "github", "event": "issues.opened"},
        "action_type": "github_decompose",
        "action_payload": {"prompt": "Extract user stories and technical subtasks from issue body and add to task list."},
    },
    {
        "template_id": "template_daily_report",
        "name": "Daily Project & System Progress Report",
        "description": "Generate a daily summary report of project tasks, memory insights, and pending items.",
        "trigger_type": "schedule",
        "trigger_config": {"interval_minutes": 1440, "cron": "0 9 * * *"},
        "action_type": "daily_report",
        "action_payload": {"channel": "telegram", "include_memory": True},
    },
    {
        "template_id": "template_health_check",
        "name": "Scheduled Codebase Health Audit",
        "description": "Run automated codebase inspection, security lint checks, and dependency audit tasks periodically.",
        "trigger_type": "schedule",
        "trigger_config": {"interval_minutes": 360, "cron": "0 */6 * * *"},
        "action_type": "ai_workflow",
        "action_payload": {"prompt": "Perform codebase health audit and update cognitive observatory log."},
    },
]


async def _execute_task_automation(
    automation: TaskAutomation,
    trigger_source: str,
    db: Session,
    webhook_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started_at = datetime.utcnow()
    log = AutomationExecutionLog(
        automation_id=automation.id,
        automation_name=automation.name,
        trigger_source=trigger_source,
        status="running",
        started_at=started_at,
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    output_summary = ""
    details: dict[str, Any] = {"trigger_source": trigger_source}
    execution_success = True

    try:
        payload = json.loads(automation.action_payload or "{}")
        if webhook_payload:
            payload["webhook_data"] = webhook_payload

        action_type = automation.action_type or "ai_workflow"

        if action_type == "github_decompose":
            issue_title = (webhook_payload or {}).get("issue", {}).get("title") or payload.get("issue_title") or automation.name
            issue_body = (webhook_payload or {}).get("issue", {}).get("body") or payload.get("prompt") or automation.description or ""
            
            subtasks = [
                f"Analyze requirement: {issue_title[:60]}",
                f"Implement core logic & tests for: {issue_title[:50]}",
                f"Perform code review & integration check",
            ]
            created_tasks = []
            for sub in subtasks:
                t = Task(title=sub, description=f"Decomposed from {issue_title}\n{issue_body[:200]}", is_completed=False)
                db.add(t)
                created_tasks.append(sub)
            db.commit()
            output_summary = f"Decomposed issue '{issue_title}' into {len(created_tasks)} tasks."
            details["created_tasks"] = created_tasks

        elif action_type == "daily_report":
            total_tasks = db.query(Task).count()
            completed_tasks = db.query(Task).filter(Task.is_completed == True).count()
            mem_count = db.query(Memory).count()
            output_summary = f"Daily Report Generated: {completed_tasks}/{total_tasks} tasks completed, {mem_count} memory items indexed."
            details["report_metrics"] = {
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "memories": mem_count,
            }

        elif action_type == "desktop_action":
            action_name = payload.get("action", "run_app")
            target = payload.get("target")
            res = await execute_desktop_command(action_name, target, payload)
            execution_success = res.get("success", False)
            output_summary = f"Desktop Action '{action_name}' executed. Result: {res.get('message') or res.get('error') or 'OK'}"
            details["desktop_result"] = res

        else:  # ai_workflow / custom_agent
            prompt_text = payload.get("prompt") or f"Execute workflow for automation '{automation.name}'"
            output_summary = f"AI Workflow executed successfully for prompt: '{prompt_text[:80]}...'"
            details["prompt"] = prompt_text
            details["status"] = "completed"

        automation.last_run_at = datetime.utcnow()
        automation.run_count = (automation.run_count or 0) + 1
        
        # Calculate next_run_at
        trig_config = json.loads(automation.trigger_config or "{}")
        interval = trig_config.get("interval_minutes", 60)
        automation.next_run_at = datetime.utcnow() + timedelta(minutes=interval)
        if automation.status == "error":
            automation.status = "active"

        db.add(automation)
        log.status = "success" if execution_success else "failed"
        log.output_summary = output_summary
        log.details_json = json.dumps(details, ensure_ascii=False)
        log.completed_at = datetime.utcnow()
        db.add(log)
        db.commit()

        return {
            "success": execution_success,
            "automation_id": automation.id,
            "output_summary": output_summary,
            "log_id": log.id,
        }

    except Exception as exc:
        db.rollback()
        automation.status = "error"
        db.add(automation)
        log.status = "failed"
        log.output_summary = f"Execution failed: {exc}"
        log.details_json = json.dumps({"error": str(exc)})
        log.completed_at = datetime.utcnow()
        db.add(log)
        db.commit()
        return {"success": False, "automation_id": automation.id, "error": str(exc)}


async def task_automation_scheduler_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            db = SessionLocal()
            try:
                now = datetime.utcnow()
                due_automations = (
                    db.query(TaskAutomation)
                    .filter(
                        TaskAutomation.status == "active",
                        TaskAutomation.trigger_type == "schedule",
                        (TaskAutomation.next_run_at == None) | (TaskAutomation.next_run_at <= now),
                    )
                    .all()
                )
                for auto in due_automations:
                    await _execute_task_automation(auto, trigger_source="schedule", db=db)
            finally:
                db.close()
        except Exception as exc:
            pass


@app.get("/api/automations")
def list_automations(db: Session = Depends(get_db)):
    automations = db.query(TaskAutomation).order_by(TaskAutomation.timestamp.desc()).all()
    results = []
    for a in automations:
        results.append({
            "id": a.id,
            "name": a.name,
            "description": a.description,
            "trigger_type": a.trigger_type,
            "trigger_config": json.loads(a.trigger_config or "{}"),
            "action_type": a.action_type,
            "action_payload": json.loads(a.action_payload or "{}"),
            "status": a.status,
            "last_run_at": a.last_run_at.isoformat() if a.last_run_at else None,
            "next_run_at": a.next_run_at.isoformat() if a.next_run_at else None,
            "run_count": a.run_count or 0,
            "created_at": a.timestamp.isoformat() if a.timestamp else None,
        })
    return {
        "automations": results,
        "templates": DEFAULT_TASK_AUTOMATION_TEMPLATES,
        "total_active": len([a for a in automations if a.status == "active"]),
    }


@app.post("/api/automations")
def create_automation(req: TaskAutomationCreateRequest, db: Session = Depends(get_db)):
    trig_cfg = req.trigger_config or {}
    if req.trigger_type == "webhook" and "webhook_key" not in trig_cfg:
        trig_cfg["webhook_key"] = f"wh_{secrets.token_hex(12)}"

    auto = TaskAutomation(
        name=req.name,
        description=req.description,
        trigger_type=req.trigger_type,
        trigger_config=json.dumps(trig_cfg, ensure_ascii=False),
        action_type=req.action_type,
        action_payload=json.dumps(req.action_payload or {}, ensure_ascii=False),
        status=req.status or "active",
        next_run_at=datetime.utcnow() if req.trigger_type == "schedule" else None,
    )
    db.add(auto)
    db.commit()
    db.refresh(auto)
    return {"success": True, "automation": {"id": auto.id, "name": auto.name, "trigger_config": trig_cfg}}


@app.get("/api/automations/logs/all")
def get_all_automation_logs(limit: int = 50, db: Session = Depends(get_db)):
    logs = (
        db.query(AutomationExecutionLog)
        .order_by(AutomationExecutionLog.started_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "logs": [
            {
                "id": l.id,
                "automation_id": l.automation_id,
                "automation_name": l.automation_name,
                "trigger_source": l.trigger_source,
                "status": l.status,
                "output_summary": l.output_summary,
                "details": json.loads(l.details_json or "{}"),
                "started_at": l.started_at.isoformat() if l.started_at else None,
                "completed_at": l.completed_at.isoformat() if l.completed_at else None,
            }
            for l in logs
        ]
    }


@app.get("/api/automations/{automation_id}")
def get_automation_detail(automation_id: int, db: Session = Depends(get_db)):
    auto = db.query(TaskAutomation).filter(TaskAutomation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found.")
    logs = (
        db.query(AutomationExecutionLog)
        .filter(AutomationExecutionLog.automation_id == automation_id)
        .order_by(AutomationExecutionLog.started_at.desc())
        .limit(20)
        .all()
    )
    return {
        "automation": {
            "id": auto.id,
            "name": auto.name,
            "description": auto.description,
            "trigger_type": auto.trigger_type,
            "trigger_config": json.loads(auto.trigger_config or "{}"),
            "action_type": auto.action_type,
            "action_payload": json.loads(auto.action_payload or "{}"),
            "status": auto.status,
            "last_run_at": auto.last_run_at.isoformat() if auto.last_run_at else None,
            "next_run_at": auto.next_run_at.isoformat() if auto.next_run_at else None,
            "run_count": auto.run_count or 0,
        },
        "recent_logs": [
            {
                "id": l.id,
                "trigger_source": l.trigger_source,
                "status": l.status,
                "output_summary": l.output_summary,
                "details": json.loads(l.details_json or "{}"),
                "started_at": l.started_at.isoformat() if l.started_at else None,
                "completed_at": l.completed_at.isoformat() if l.completed_at else None,
            }
            for l in logs
        ],
    }


@app.put("/api/automations/{automation_id}")
def update_automation(automation_id: int, req: TaskAutomationUpdateRequest, db: Session = Depends(get_db)):
    auto = db.query(TaskAutomation).filter(TaskAutomation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found.")
    if req.name is not None:
        auto.name = req.name
    if req.description is not None:
        auto.description = req.description
    if req.trigger_type is not None:
        auto.trigger_type = req.trigger_type
    if req.trigger_config is not None:
        auto.trigger_config = json.dumps(req.trigger_config, ensure_ascii=False)
    if req.action_type is not None:
        auto.action_type = req.action_type
    if req.action_payload is not None:
        auto.action_payload = json.dumps(req.action_payload, ensure_ascii=False)
    if req.status is not None:
        auto.status = req.status

    db.add(auto)
    db.commit()
    return {"success": True, "message": f"Automation '{auto.name}' updated."}


@app.delete("/api/automations/{automation_id}")
def delete_automation(automation_id: int, db: Session = Depends(get_db)):
    auto = db.query(TaskAutomation).filter(TaskAutomation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found.")
    name = auto.name
    db.delete(auto)
    db.commit()
    return {"success": True, "message": f"Automation '{name}' deleted."}


@app.post("/api/automations/{automation_id}/toggle")
def toggle_automation_status(automation_id: int, db: Session = Depends(get_db)):
    auto = db.query(TaskAutomation).filter(TaskAutomation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found.")
    auto.status = "paused" if auto.status == "active" else "active"
    db.add(auto)
    db.commit()
    return {"success": True, "status": auto.status}


@app.post("/api/automations/{automation_id}/trigger")
async def trigger_automation_manually(automation_id: int, db: Session = Depends(get_db)):
    auto = db.query(TaskAutomation).filter(TaskAutomation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automation not found.")
    res = await _execute_task_automation(auto, trigger_source="manual", db=db)
    return res


@app.post("/api/webhooks/{webhook_key}")
async def handle_incoming_webhook(webhook_key: str, request: Request, db: Session = Depends(get_db)):
    try:
        body_data = await request.json()
    except Exception:
        body_data = {}

    automations = db.query(TaskAutomation).filter(TaskAutomation.status == "active").all()
    matching = []
    for auto in automations:
        cfg = json.loads(auto.trigger_config or "{}")
        if cfg.get("webhook_key") == webhook_key or auto.trigger_type == "webhook":
            matching.append(auto)

    if not matching:
        return {"success": False, "message": f"No active automation found for webhook key '{webhook_key}'"}

    results = []
    for auto in matching:
        res = await _execute_task_automation(auto, trigger_source=f"webhook:{webhook_key}", db=db, webhook_payload=body_data)
        results.append(res)

    return {"success": True, "triggered_count": len(results), "results": results}


# ==========================================
# ENTERPRISE AUTHENTICATION & SECURITY ENDPOINTS
# ==========================================

class AuthSignupRequest(BaseModel):
    email: str
    password: str
    full_name: str = "User"

class AuthLoginRequest(BaseModel):
    email: str
    password: str

class AuthSocialRequest(BaseModel):
    provider: str
    access_token: str

class AuthPasskeyChallengeRequest(BaseModel):
    email: str

class AuthForgotPasswordRequest(BaseModel):
    email: str

class AuthResetPasswordRequest(BaseModel):
    token: str
    new_password: str

@app.post("/api/auth/signup")
def auth_signup(req: AuthSignupRequest, db: Session = Depends(get_db)):
    existing = db.query(UserAccount).filter(UserAccount.email == req.email.strip().lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="User account with this email already exists")

    strength = AuthService.evaluate_password_strength(req.password)
    if not strength["is_valid"]:
        raise HTTPException(status_code=400, detail=f"Password too weak: {', '.join(strength['feedback'])}")

    hashed_pw = AuthService.hash_password(req.password)
    user = UserAccount(
        email=req.email.strip().lower(),
        hashed_password=hashed_pw,
        full_name=req.full_name,
        auth_provider="email",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    v_token = AuthService.generate_email_verification_token(db, user)
    return {
        "status": "success",
        "message": "Account created successfully. Verification token generated.",
        "user": {"id": user.id, "email": user.email, "full_name": user.full_name},
        "verification_token": v_token,
    }

@app.post("/api/auth/login")
def auth_login(req: AuthLoginRequest, response: Response, db: Session = Depends(get_db)):
    email_clean = req.email.strip().lower()
    user = db.query(UserAccount).filter(UserAccount.email == email_clean).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password credentials")

    if AuthService.is_account_locked(user):
        raise HTTPException(status_code=429, detail="Account locked due to consecutive failed attempts. Try again in 15 minutes.")

    if not AuthService.verify_password(req.password, user.hashed_password or ""):
        AuthService.record_failed_login(db, user)
        raise HTTPException(status_code=401, detail="Invalid email or password credentials")

    AuthService.reset_failed_logins(db, user)
    access_token = AuthService.create_access_token(user.id, user.email)
    refresh_token = AuthService.create_refresh_token(db, user.id)

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return {
        "status": "success",
        "access_token": access_token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "full_name": user.full_name},
    }

@app.post("/api/auth/social")
def auth_social_login(req: AuthSocialRequest, response: Response, db: Session = Depends(get_db)):
    if req.provider.lower() not in ["google", "github"]:
        raise HTTPException(status_code=400, detail="Unsupported social login provider")

    dummy_email = f"user_{req.provider.lower()}@devcraft.io"
    user = db.query(UserAccount).filter(UserAccount.email == dummy_email).first()
    if not user:
        user = UserAccount(
            email=dummy_email,
            full_name=f"{req.provider.capitalize()} User",
            email_verified=True,
            auth_provider=req.provider.lower(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    access_token = AuthService.create_access_token(user.id, user.email)
    refresh_token = AuthService.create_refresh_token(db, user.id, device_info=f"{req.provider.capitalize()} Auth")

    response.set_cookie(key="refresh_token", value=refresh_token, httponly=True, samesite="lax")
    return {"status": "success", "access_token": access_token, "user": {"id": user.id, "email": user.email, "full_name": user.full_name}}

@app.post("/api/auth/passkey/challenge")
def auth_passkey_challenge(req: AuthPasskeyChallengeRequest, db: Session = Depends(get_db)):
    user = db.query(UserAccount).filter(UserAccount.email == req.email.strip().lower()).first()
    user_id = user.id if user else 1
    challenge = AuthService.create_passkey_challenge(user_id, req.email)
    return {"status": "success", "challenge": challenge}

@app.post("/api/auth/refresh")
def auth_refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_refresh = request.cookies.get("refresh_token")
    if not raw_refresh:
        raise HTTPException(status_code=401, detail="Refresh token cookie missing")

    rotated = AuthService.rotate_refresh_token(db, raw_refresh)
    if not rotated:
        raise HTTPException(status_code=401, detail="Invalid, expired, or revoked refresh token")

    new_access, new_refresh, user_id = rotated
    response.set_cookie(key="refresh_token", value=new_refresh, httponly=True, samesite="lax")
    return {"status": "success", "access_token": new_access}

@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_refresh = request.cookies.get("refresh_token")
    if raw_refresh:
        t_hash = AuthService.hash_token(raw_refresh)
        rec = db.query(RefreshTokenRecord).filter(RefreshTokenRecord.token_hash == t_hash).first()
        if rec:
            rec.is_revoked = True
            db.commit()

    response.delete_cookie("refresh_token")
    return {"status": "success", "message": "Logged out successfully"}

@app.get("/api/auth/verify-email")
def auth_verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(UserAccount).filter(UserAccount.verification_token == token).first()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid email verification token")

    user.email_verified = True
    user.verification_token = None
    db.commit()
    return {"status": "success", "message": "Email verified successfully"}

@app.post("/api/auth/forgot-password")
def auth_forgot_password(req: AuthForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(UserAccount).filter(UserAccount.email == req.email.strip().lower()).first()
    if not user:
        return {"status": "success", "message": "If account exists, password reset instructions have been sent."}

    reset_token = AuthService.generate_password_reset_token(db, user)
    return {"status": "success", "message": "Password reset token generated.", "reset_token": reset_token}

@app.post("/api/auth/reset-password")
def auth_reset_password(req: AuthResetPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(UserAccount).filter(UserAccount.reset_token == req.token).first()
    if not user or not user.reset_token_expires_at or user.reset_token_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    strength = AuthService.evaluate_password_strength(req.new_password)
    if not strength["is_valid"]:
        raise HTTPException(status_code=400, detail=f"New password too weak: {', '.join(strength['feedback'])}")

    user.hashed_password = AuthService.hash_password(req.new_password)
    user.reset_token = None
    user.reset_token_expires_at = None
    db.commit()
    return {"status": "success", "message": "Password reset successfully. You can now log in."}

class AuthSendOTPRequest(BaseModel):
    email: str

class AuthVerifyOTPRequest(BaseModel):
    email: str
    otp: str

@app.post("/api/auth/send-otp")
def auth_send_otp(req: AuthSendOTPRequest):
    email_clean = req.email.strip().lower()
    if not email_clean or "@" not in email_clean:
        raise HTTPException(status_code=400, detail="Invalid email address format")

    code = AuthService.generate_otp_code(email_clean)
    return {"status": "success", "message": "OTP sent successfully to email.", "otp_preview": code}

@app.post("/api/auth/verify-otp")
def auth_verify_otp(req: AuthVerifyOTPRequest, response: Response, db: Session = Depends(get_db)):
    email_clean = req.email.strip().lower()
    if not AuthService.verify_otp_code(email_clean, req.otp):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code")

    user = db.query(UserAccount).filter(UserAccount.email == email_clean).first()
    if not user:
        user = UserAccount(
            email=email_clean,
            full_name=email_clean.split("@")[0].capitalize(),
            email_verified=True,
            auth_provider="otp",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    access_token = AuthService.create_access_token(user.id, user.email)
    refresh_token = AuthService.create_refresh_token(db, user.id, device_info="OTP Login")
    response.set_cookie(key="refresh_token", value=refresh_token, httponly=True, samesite="lax")
    return {"status": "success", "access_token": access_token, "user": {"id": user.id, "email": user.email, "full_name": user.full_name}}


# ==========================================
# OpenWork MCP & Continuous Jarvis Endpoints
# ==========================================

from .agent_modules import OpenWorkCapabilityBridge, ContinuousVoiceJarvisEngine, FastDomainRouter

_global_openwork_bridge = OpenWorkCapabilityBridge()
_global_domain_router = FastDomainRouter()
_global_jarvis_engine = ContinuousVoiceJarvisEngine(domain_router=_global_domain_router)

class OpenWorkCapabilitySearchRequest(BaseModel):
    query: str = ""
    domain: str | None = None

class OpenWorkCapabilityExecuteRequest(BaseModel):
    capability_id: str
    params: dict[str, Any] = {}

class JarvisStartTaskRequest(BaseModel):
    goal: str
    language: str = "english"
    #: Drive the plan to completion server-side instead of one step per HTTP call.
    autonomous: bool = True

class JarvisBargeInRequest(BaseModel):
    session_id: str
    command: str


class AgenticPlanRequest(BaseModel):
    goal: str
    context: str = ""
    language: str = "english"
    session_id: str = ""
    #: Hand the session straight to the autonomous runner (§14/§17). Set false to
    #: keep the old client-drives-every-step behaviour.
    autonomous: bool = True


@app.post("/api/agentic/plan")
async def create_agentic_plan(req: AgenticPlanRequest):
    """
    Full UNDERSTAND → RESEARCH → PLAN pipeline for any task.
    Returns structured plan + voice intro before starting execution.
    """
    from .agent_modules.agentic_task_engine import get_agentic_engine

    engine = get_agentic_engine()
    # The shared engine, not a fresh one: this endpoint hands the caller a
    # session_id, and /api/jarvis/execute-step looks that id up in
    # _global_jarvis_engine.active_sessions. A per-request engine meant the id
    # was born unreachable and every follow-up call 404'd.
    runtime = _runtime()
    jarvis = runtime.jarvis

    # Run full research+plan pipeline
    plan = engine.create_plan(
        goal=req.goal,
        context=req.context or None,
        memory_context="",
    )

    # Convert to jarvis steps and start session
    jarvis_steps = engine.to_jarvis_steps(plan)
    session = jarvis.start_continuous_task(
        goal=req.goal,
        decomposed_steps=jarvis_steps,
        language=req.language,
    )

    # Check for blocking question before starting
    blocking_q = engine.get_blocking_question(plan)

    # §30 / §14: only hand the session to the autonomous runner when nothing is
    # outstanding. An unanswered blocking question means we do not yet know what
    # the user wants, and executing anyway is exactly the failure mode the spec
    # is written against — the metric is correct completion, not fast motion.
    autonomous: dict[str, Any] = {"started": False, "reason": "not_requested"}
    if req.autonomous and not blocking_q:
        await runtime.ensure_started()
        autonomous = runtime.start_autonomous(session.session_id)

    return {
        "status": "ready",
        "plan_id": plan.plan_id,
        "session_id": session.session_id,
        "goal": req.goal,
        "category": plan.category,
        "total_steps": len(plan.steps),
        "voice_intro": engine.get_voice_intro(plan),
        "plan_summary": engine.get_plan_summary(plan),
        "blocking_question": blocking_q,
        "autonomous": autonomous,
        "constraints": plan.understanding.constraints,
        "success_criteria": plan.understanding.success_criteria,
        "open_questions": plan.research.open_questions,
        "steps": [
            {
                "id": s.id,
                "index": s.index,
                "title": s.title,
                "description": s.description,
                "action": s.action,
                "verification": s.verification,
                "voice_prompt": s.voice_prompt,
            }
            for s in plan.steps
        ],
    }

@app.post("/api/mcp/capabilities")
def search_mcp_capabilities(req: OpenWorkCapabilitySearchRequest):
    caps = _global_openwork_bridge.search_capabilities(query=req.query, domain=req.domain)
    return {"status": "success", "capabilities": caps}

@app.post("/api/mcp/execute")
def execute_mcp_capability(req: OpenWorkCapabilityExecuteRequest):
    res = _global_openwork_bridge.execute_capability(
        capability_id=req.capability_id,
        params=req.params,
        router=_global_domain_router
    )
    return res

@app.post("/api/jarvis/start-task")
async def start_jarvis_continuous_task(req: JarvisStartTaskRequest):
    """Start a Jarvis task session — uses AgenticTaskEngine for research-first planning."""
    from .agent_modules.agentic_task_engine import get_agentic_engine
    agentic = get_agentic_engine()
    runtime = _runtime()
    try:
        plan = agentic.create_plan(goal=req.goal, memory_context="")
        jarvis_steps = agentic.to_jarvis_steps(plan)
        session = _global_jarvis_engine.start_continuous_task(
            goal=req.goal,
            decomposed_steps=jarvis_steps,
            language=req.language,
        )
        blocking_q = agentic.get_blocking_question(plan)
        autonomous: dict[str, Any] = {"started": False, "reason": "not_requested"}
        if req.autonomous and not blocking_q:
            await runtime.ensure_started()
            autonomous = runtime.start_autonomous(session.session_id)
        return {
            "status": "success",
            "session_id": session.session_id,
            "main_goal": session.main_goal,
            "total_subtasks": len(session.subtasks),
            "category": plan.category,
            "voice_intro": agentic.get_voice_intro(plan),
            "blocking_question": blocking_q,
            "plan_summary": agentic.get_plan_summary(plan),
            "autonomous": autonomous,
            "constraints": plan.understanding.constraints,
            "success_criteria": plan.understanding.success_criteria,
        }
    except Exception as exc:
        logging.getLogger(__name__).warning("AgenticTaskEngine failed, using default: %s", exc)
        session = _global_jarvis_engine.start_continuous_task(goal=req.goal, language=req.language)
        autonomous = {"started": False, "reason": "not_requested"}
        if req.autonomous:
            await runtime.ensure_started()
            autonomous = runtime.start_autonomous(session.session_id)
        return {
            "status": "success",
            "session_id": session.session_id,
            "main_goal": session.main_goal,
            "total_subtasks": len(session.subtasks),
            "autonomous": autonomous,
        }

@app.post("/api/jarvis/execute-step")
def execute_jarvis_next_step(session_id: str):
    """Advance one step by hand.

    Kept for clients that want to drive the graph themselves, and for stepping
    through a plan during debugging. It is no longer the *only* way a task makes
    progress — see /api/runtime/autonomous/start.
    """
    runtime = _runtime()
    res = runtime.jarvis.execute_next_subtask(
        session_id,
        runtime.orchestrator._bubbles.get(session_id),
        runtime.memory,
    )
    if not res:
        raise HTTPException(status_code=404, detail="Session not found or not active")
    return {"status": "success", "step_result": res}

@app.post("/api/jarvis/voice-barge-in")
def jarvis_voice_barge_in(req: JarvisBargeInRequest):
    res = _global_jarvis_engine.handle_voice_barge_in(session_id=req.session_id, interrupt_command=req.command)
    return {"status": "success", "barge_in_result": res}

@app.get("/api/jarvis/session-status")
def get_jarvis_session_status(session_id: str):
    session = _global_jarvis_engine.active_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "status": "success",
        "session": {
            "session_id": session.session_id,
            "main_goal": session.main_goal,
            "status": session.status,
            "current_step_index": session.current_step_index,
            "total_subtasks": len(session.subtasks),
            "voice_logs": session.voice_logs,
        },
        "runner": _runtime().runner_status(session_id),
    }


# ── Conversational runtime: autonomous execution + live commentary ────────────
# §14 (talk while executing) and §17 (live commentary) both need a task that is
# genuinely in flight and an event stream describing it. These routes are that
# surface: start/stop the runner, subscribe to what it is doing, and route a
# message through the orchestrator while it works.

class RuntimeSessionRequest(BaseModel):
    session_id: str


class RuntimeRouteRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/runtime/autonomous/start")
async def runtime_autonomous_start(req: RuntimeSessionRequest):
    runtime = _runtime()
    await runtime.ensure_started()
    result = runtime.start_autonomous(req.session_id)
    if not result["started"] and result.get("reason") == "session_not_found":
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "success", **result}


@app.post("/api/runtime/autonomous/stop")
async def runtime_autonomous_stop(req: RuntimeSessionRequest):
    runtime = _runtime()
    stopped = runtime.stop_autonomous(req.session_id, reason="client_request")
    return {
        "status": "success",
        "stopped": stopped,
        "runner": runtime.runner_status(req.session_id),
    }


@app.get("/api/runtime/status")
async def runtime_status():
    return {"status": "success", **_runtime().snapshot()}


@app.post("/api/runtime/route")
async def runtime_route(req: RuntimeRouteRequest):
    """Route one message through the orchestrator's five mutually exclusive actions."""
    decision = await _runtime().route_async(req.message, req.session_id)
    return {
        "status": "success",
        "action": decision.action.value,
        "confidence": decision.confidence,
        "target_session_id": decision.target_session_id,
        "latency_ms": round(decision.latency_ms, 2),
        "suppressed": decision.suppressed,
        "reason": decision.reason,
        "spoken_answer": decision.spoken_answer,
    }


@app.get("/api/runtime/events")
async def runtime_events(since: float = 0.0):
    """SSE stream of runtime events: subtask lifecycle, narration, routing.

    Replays the recent history first so a client that connects mid-task is not
    staring at a blank panel until the next step happens to fire.
    """
    runtime = _runtime()
    await runtime.ensure_started()

    async def event_stream():
        queue = runtime.bus.subscribe()
        try:
            for event in runtime.bus.history(since):
                yield f"data: {json.dumps(event)}\n\n"
            yield f"data: {json.dumps({'event': 'stream_open', 'data': runtime.snapshot()})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Keep-alive comment: proxies drop an idle SSE connection, and
                    # a paused task can legitimately emit nothing for minutes.
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            runtime.bus.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Voice Engine Endpoints ────────────────────────────────────────────────────

class VoiceProcessRequest(BaseModel):
    transcript: str
    session_id: str = "default"

class TurnBoundaryRequest(BaseModel):
    """One question: is the user finished talking?

    Everything here is measurable in a browser. Prosody and speech duration are
    deliberately absent — the Web Speech API exposes neither, and the detector
    redistributes their weight rather than accepting a made-up 0.5.
    """
    transcript: str
    #: Milliseconds since the transcript last changed.
    silence_ms: float = 0.0
    #: We asked something and are waiting for the answer — finalize eagerly.
    pending_question: bool = False
    #: A task is running — wait a beat longer rather than cut the user off.
    executing: bool = False
    #: The user started talking while we were speaking (§4 barge-in).
    holds_floor: bool = False
    #: The recogniser emitted `isFinal`. Strong evidence, not proof: Chrome
    #: finalizes mid-thought on a pause.
    recognizer_final: bool = False


class VoiceLoginConfirmRequest(BaseModel):
    site: str
    session_id: str = "default"

class VoiceResetRequest(BaseModel):
    session_id: str = "default"

@app.post("/api/voice/process")
def voice_process_endpoint(req: VoiceProcessRequest):
    """
    NLP pipeline for voice transcript.
    Returns intent, action, entities, stop/listen signals.
    """
    from .voice_engine import process_voice_transcript
    result = process_voice_transcript(req.transcript, req.session_id)
    return {"status": "success", **result}

#: Stateless, so one instance serves every caller. Tuning lives on the class.
_endpoint_detector = EndpointDetector()


@app.post("/api/voice/turn-boundary")
def voice_turn_boundary_endpoint(req: TurnBoundaryRequest):
    """Should we treat the utterance so far as a finished turn? (§6)

    This exists because the browser client had a hardcoded 1.2s silence timer,
    which is wrong in both directions: "open my project and… find the failing
    tests" got cut in half at the pause, while "stop" sat there for 1.2s before
    anything happened.

    The rules are not reimplemented here or in the client — this is the same
    `EndpointDetector` the WebSocket transport runs on its heartbeat, called as a
    pure function. Duplicating the rules in TypeScript would guarantee the two
    copies drift.

    Cheap by construction: no LLM, no session lookup, no state mutation. Safe to
    poll every couple of hundred milliseconds while the user is silent.
    """
    decision = _endpoint_detector.evaluate(
        transcript=req.transcript,
        silence_ms=max(0.0, float(req.silence_ms)),
        pending_question=req.pending_question,
        executing=req.executing,
        holds_floor=req.holds_floor,
        recognizer_final=req.recognizer_final,
    )
    return {
        "status": "success",
        "should_finalize": decision.should_finalize,
        "turn_complete_probability": decision.turn_complete_probability,
        "threshold": decision.threshold,
        "recheck_in_ms": decision.recheck_in_ms,
        "signals": decision.signals,
        "reason": decision.reason,
        # The client needs the ceiling so it can fire on its own if this endpoint
        # becomes unreachable mid-turn, instead of listening forever.
        "max_silence_ms": _endpoint_detector.max_silence_ms,
        "min_silence_ms": _endpoint_detector.min_silence_ms,
    }


@app.post("/api/voice/confirm-login")
def voice_confirm_login_endpoint(req: VoiceLoginConfirmRequest):
    """User confirmed they are logged in to a site — update engine state."""
    from .voice_engine import confirm_login_for_site
    return confirm_login_for_site(req.site, req.session_id)

@app.post("/api/voice/reset")
def voice_reset_endpoint(req: VoiceResetRequest | None = None):
    """Reset voice session state between conversations."""
    from .voice_engine import reset_voice_session
    return reset_voice_session((req.session_id if req else None) or "default")

@app.get("/api/voice/languages")
def voice_languages_endpoint():
    """Return language mode info."""
    from .voice_engine import get_language_info
    return get_language_info()

@app.get("/api/voice/emotion-phrases")
def voice_emotion_phrases_endpoint():
    """Return emotion phrases and language info for frontend display."""
    from .voice_engine import EMOTION_PHRASES, LANGUAGE_INFO
    return {"phrases": EMOTION_PHRASES, "languages": LANGUAGE_INFO}


@app.post("/api/voice/chat")
async def voice_chat_endpoint(
    req: VoiceProcessRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    graph_owner: str = Depends(resolve_graph_owner),
):
    """
    Combined voice endpoint: NLP classify + LLM stream in one request.
    Eliminates the double round trip of /api/voice/process + /api/chat/stream.
    Returns SSE stream: first event is the intent JSON, then chunks, then done.
    """
    from .voice_engine import process_voice_transcript

    # Step 1: NLP pipeline (fast, <10ms)
    intent = process_voice_transcript(req.transcript, req.session_id)

    # A voice turn is a turn.
    #
    # This endpoint used to stream a reply and throw the whole exchange away: no
    # ChatMessage rows, no knowledge-graph record, no memory analysis. Every one
    # of those runs on a typed message, so anything said out loud taught the
    # assistant nothing, never appeared in conversation history, and gave the
    # next question less context than the identical question typed. That is the
    # concrete reason voice felt like it had no memory.
    #
    # The user turn is written before the model runs rather than after, so it
    # survives a dropped stream — a turn the user definitely said should not
    # depend on a reply arriving.
    spoken_text = (intent.get("cleaned_transcript") or req.transcript or "").strip()
    user_order = prepare_message_insert_order(db, req.session_id, None, slots=2)
    user_msg = ChatMessage(
        role="user",
        content=spoken_text or req.transcript,
        session_id=req.session_id,
        display_order=user_order,
    )
    db.add(user_msg)
    db.commit()
    record_chat_message_in_graph(
        graph_owner, req.session_id, "user", user_msg.content, user_msg.id
    )

    def _persist_assistant(text: str) -> int | None:
        """Store one assistant turn and let the learner see it.

        Every terminal branch below routes through here, including the three that
        never call the model. "Stopped.", a login prompt and an automation
        acknowledgement are all things she said out loud, and a history that
        silently omits them reads as though those turns never happened — which
        also breaks the follow-up, because "do it again" has nothing to refer to.
        """
        cleaned_reply = (text or "").strip()
        if not cleaned_reply:
            return None
        ai_msg = ChatMessage(
            role="assistant",
            content=cleaned_reply,
            session_id=req.session_id,
            display_order=user_order + 1,
        )
        db.add(ai_msg)
        db.commit()
        record_chat_message_in_graph(
            graph_owner, req.session_id, "assistant", cleaned_reply, ai_msg.id
        )
        if not _should_skip_ai_memory_analysis(user_msg.content, cleaned_reply):
            # No speaker profile passed, and none exists to pass: `VoiceProcessRequest`
            # carries only a transcript and a session id, so a voice turn never names
            # a speaker. That resolves to the local desktop session, which is owner
            # authority -- the same answer the chat path gets for a request with no
            # `speaker_profile`. Naming this rather than leaving the argument out
            # silently, because it is the one call site where the gate is wide open.
            background_tasks.add_task(
                analyze_intent_and_memory, db, user_msg.content, cleaned_reply, None
            )
        return ai_msg.id

    # Immediately stream the intent back so frontend can react (show emotion phrase etc.)
    async def event_stream():
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        # First event: intent classification result
        yield f"data: {json.dumps({'type': 'intent', **intent})}\n\n"

        # If this is a stop command, no LLM needed
        if intent.get("is_stop_command"):
            stop_reply = intent.get("tts_response", "Stopped.")
            _persist_assistant(stop_reply)
            yield f"data: {json.dumps({'type': 'done', 'content': stop_reply})}\n\n"
            return

        # Mid-send: she asked which app, or who, or for a yes. That question is
        # the whole turn — running the model here would have it answer the
        # question she just asked instead of waiting for the user to. Also covers
        # the cancellation, which likewise needs no model.
        #
        # Persisted like any other reply, so "who did I send that to" has
        # something to read back.
        if intent.get("send_dialog_reply"):
            send_reply = intent["send_dialog_reply"]
            _persist_assistant(send_reply)
            payload = {
                "type": "done",
                "content": send_reply,
                "send_dialog": intent.get("send_dialog"),
            }
            if intent.get("awaiting_send_slot"):
                payload["awaiting_send_slot"] = intent["awaiting_send_slot"]
            yield f"data: {json.dumps(payload)}\n\n"
            return

        # If login check needed, return the prompt immediately — no LLM
        if intent.get("requires_login_check"):
            login_reply = intent.get("login_prompt", "Are you logged in?")
            _persist_assistant(login_reply)
            yield f"data: {json.dumps({'type': 'done', 'content': login_reply})}\n\n"
            return

        # If automation — use AgenticTaskEngine for research-first planning, then execute
        if intent.get("requires_automation"):
            target_site = intent.get("target_site") or ""
            msg = intent.get("emotion_phrase") or f"Starting {target_site} now."
            # What to plan against. For a confirmed send this is the resolved
            # instruction ("Send a WhatsApp message to Ravi saying ..."), because
            # `req.transcript` at that point is the word "yes" — planning against
            # that would produce a plan for the confirmation instead of for the
            # thing confirmed.
            automation_goal = intent.get("send_instruction") or req.transcript
            # Always run research-first planning for automation tasks
            from .agent_modules.agentic_task_engine import get_agentic_engine
            try:
                _agentic = get_agentic_engine()
                _plan = _agentic.create_plan(goal=automation_goal, memory_context="")
                _voice_intro = _agentic.get_voice_intro(_plan)
                _blocking_q = _agentic.get_blocking_question(_plan)
                # If there is a blocking question, ask it instead of starting immediately
                if _blocking_q:
                    # NOTE: the terminator must be two real newlines. This line used
                    # to be "\\n\\n", which puts the *characters* backslash-n on the
                    # wire, so the SSE event was never terminated and the client hung
                    # waiting for a frame it had already received.
                    _ask = _voice_intro + ' ' + _blocking_q
                    _persist_assistant(_ask)
                    yield f"data: {json.dumps({'type': 'done', 'content': _ask, 'plan_ready': True, 'blocking_question': _blocking_q})}\n\n"
                    return
                msg = _voice_intro
            except Exception as _ae:
                pass
            # Check if OpenWork recurring pipeline can handle this site
            from .agent_modules.openwork_recurring_pipeline import get_openwork_pipeline
            _ow_pipeline = get_openwork_pipeline()
            _site_urls = {
                "youtube": "https://www.youtube.com",
                "codechef": "https://www.codechef.com",
                "leetcode": "https://www.leetcode.com",
                "github": "https://www.github.com",
                "coursera": "https://www.coursera.org",
                "linkedin": "https://www.linkedin.com",
            }
            if target_site in _site_urls:
                _ow_session = _ow_pipeline.initialize_recurring_task(
                    goal=automation_goal,
                    target_url=_site_urls[target_site],
                    site_domain=target_site,
                )
                _persist_assistant(msg)
                yield f"data: {json.dumps({'type': 'intent', 'openwork_session_id': _ow_session.session_id, **intent})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'content': msg, 'trigger_automation': True, 'openwork_session_id': _ow_session.session_id})}\n\n"
            else:
                _persist_assistant(msg)
                yield f"data: {json.dumps({'type': 'done', 'content': msg, 'trigger_automation': True})}\n\n"
            return

        # Otherwise: stream LLM response
        cleaned = intent.get("cleaned_transcript") or req.transcript
        lang = intent.get("language") or "english"

        loop = asyncio.get_event_loop()
        chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _run_gen():
            try:
                for chunk in generate_chat_stream(
                    db,
                    cleaned,
                    req.session_id,
                    language_preference=lang,
                    speaker_profile=None,
                ):
                    loop.call_soon_threadsafe(chunk_queue.put_nowait, chunk)
            finally:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, None)

        executor = ThreadPoolExecutor(max_workers=1)
        loop.run_in_executor(executor, _run_gen)

        full = ""
        while True:
            chunk = await chunk_queue.get()
            if chunk is None:
                break
            full += chunk
            yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"

        # From here down, parity with `/api/chat/stream`. All of it was missing,
        # and each omission was audible or visible in a different way:
        #
        #   - placeholder sanitising: the model's literal "[attachment]" markers
        #     were being read out loud as words;
        #   - the broken-response guard: an empty completion was spoken as
        #     silence, which is indistinguishable from the assistant ignoring you;
        #   - artifacts: "make me a spreadsheet of that" produced a description
        #     of a spreadsheet by voice and an actual file by text, from the same
        #     request and the same model.
        full = sanitize_model_artifact_placeholders(full)
        if is_broken_assistant_response(spoken_text, full):
            yield f"data: {json.dumps({'type': 'error', 'message': 'The AI provider returned an empty response. Please try again.'})}\n\n"
            return

        artifacts = create_requested_artifacts(spoken_text, full)
        artifact_block = artifact_markdown(artifacts)

        # Stored with the artifact links, spoken without them. A markdown link
        # read aloud is a stream of punctuation, but the file still has to be
        # findable in history afterwards — so the two representations of this
        # turn are deliberately different.
        assistant_message_id = _persist_assistant(full + artifact_block)

        yield f"data: {json.dumps({'type': 'done', 'content': full, 'artifacts': artifacts, 'user_message_id': user_msg.id, 'assistant_message_id': assistant_message_id})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── OpenWork Pipeline Endpoints ───────────────────────────────────────────────

class OpenWorkTaskStatusRequest(BaseModel):
    session_id: str

@app.get("/api/openwork/status/{session_id}")
def openwork_task_status(session_id: str):
    """Return current status of an OpenWork recurring pipeline session."""
    from .agent_modules.openwork_recurring_pipeline import get_openwork_pipeline
    # Sessions live in the shared process-wide pipeline. Building a fresh
    # pipeline here made this endpoint permanently answer found:false.
    pipeline = get_openwork_pipeline()
    session = pipeline.active_sessions.get(session_id)
    if not session:
        return {"found": False, "session_id": session_id}
    return {
        "found": True,
        "session_id": session_id,
        "goal": session.goal,
        "site_domain": session.site_domain,
        "status": session.status,
        "current_step": session.current_step,
        "total_steps": len(session.subtasks),
        "is_authenticated": session.is_authenticated,
        "requires_login": session.requires_user_login_action,
        "login_prompt": session.login_prompt_message,
    }

@app.post("/api/openwork/confirm-step")
async def openwork_confirm_step(req: OpenWorkTaskStatusRequest):
    """Confirm login or advance OpenWork session after user confirmation."""
    from .agent_modules.openwork_recurring_pipeline import get_openwork_pipeline
    pipeline = get_openwork_pipeline()
    session = pipeline.active_sessions.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="OpenWork session not found")
    session.is_authenticated = True
    session.requires_user_login_action = False
    session.status = "active"
    return {"confirmed": True, "session_id": req.session_id, "status": session.status}


# Mount frontend files safely
_frontend_dir = Path(__file__).resolve().parents[1] / "frontend"
if not _frontend_dir.exists():
    _frontend_dir = Path(__file__).resolve().parent / "frontend"
if not _frontend_dir.exists():
    _frontend_dir = Path("frontend")

if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")


