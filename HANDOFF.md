# Akansha Voice Engine — handoff

Paste this whole file into a new chat and say "continue from the handoff".

Working dir: `C:\MY-AI` · app: `C:\MY-AI\aura` · branch `main` (do not push; commit only when asked).
Shell cwd resets between turns — prefix with `cd /c/MY-AI/aura &&`.

Full prior transcripts, if a detail is missing:
`C:\Users\LENOVO\.claude\projects\C--MY-AI\5847d88e-9190-48f8-aa12-3d69c09cbb25.jsonl`

---

## The task

Two parts, from the original request (verbatim): *"please check graphify band understand code
structure and please undeerstand code directory and mistakes and beaking points build sime thing like
jarvis+electron+friday+siri+alexa+ai agent autonomous voice agent orchestrator"*

- **A.** Audit the graphify output / code structure / directory for mistakes and breaking points.
- **B.** Build the "AKANSHA ADVANCED VOICE + AUTONOMOUS OPERATIONS ENGINE" per a 40-section spec whose
  defining principle is `VOICE UNDERSTANDING + CONTEXT + REASONING + CORRECT OPERATIONS`, not
  `Voice → Text → Answer`. Success = **correct completion of the user's intended operation**, not a
  fluent response.

Governing instruction for the current phase, verbatim: *"after all building and removing errors and
break points registered and listed all ,please test by voice whether is replying like and human and
doing and automating tasks continuously learning improving memory actions and all desktop and web
complete control and also improve complete styiling of web clean styling improve styling whole
performance of website"*

Order of remaining work: **finish the registered items → voice end-to-end test → styling +
performance pass.**

The user's embedded §26 requirement, verbatim: *"with real women beautiful women and pleasing voice to
listen focus on correct lip sync facial expressions phrases and all english , telugu both
understanding agent complete like speaking realizing if doubts then it should ask and do tasks
complete web and desktop control over all"*

### The 40 sections, condensed

§1 continuous pipeline (Mic→VAD→streaming STT→state→intent→turn-taking→reasoning→tools→observation→
response→streaming TTS→continuous listening); §2 ChatGPT-like natural voice UX; §3 exactly 15 voice
states (IDLE, LISTENING, SPEECH_DETECTED, TRANSCRIBING, UNDERSTANDING, THINKING, RESPONDING, SPEAKING,
INTERRUPTED, EXECUTING, WAITING, CONFIRMING, PAUSED, STOPPED, ERROR); §4 true barge-in, no Stop button;
§5 VAD classifying USER_SPEECH/ASSISTANT_SPEECH/BACKGROUND_NOISE/SILENCE/MUSIC/KEYBOARD/SYSTEM_AUDIO;
§6 end-of-turn via `turn_complete_probability` (canonical example: "Open my project and…" [pause]
"…find the failing tests"); §7 backchannels ("yeah", "okay", "mm-hmm") must not become tasks;
§8 Conversation Situation Model; §9 "Should I respond?" engine; §10 14 intent categories; §11 layered
memory (short_term, working, task, summary, long_term); §12 rolling context compression; §13 never
lose task context (CURRENT_TASK, LAST_FILE, PENDING_ACTION…); §14 talk while executing; §15
hierarchical task graph, 7 statuses; §16 voice+automation as ONE system; §17 live commentary; §18
speech priorities P0–P6, **never speak P6**; §19 streaming TTS at sentence boundaries; §20
interruption during tool execution via cancellation tokens; §21 environment awareness; §22 multimodal;
§23 premium futuristic dark-glassmorphism UI (explicitly NOT an admin dashboard); §24 state-reactive 3D
Akansha core; §25 Three.js/R3F/drei/WebGL with 2D fallback; §26 real-time audio visualisation + the
user requirement above; §27 live task graph visualisation; §28 advanced chat UI; §29 context indicator
(`Context ████████░░ 78%`); §30 confidence gating (execute / confirm / clarify); §31 human-like turn
taking without pretending to be human; §32 six conversation modes; §33 **optional** wake word; §34
conversation_end ≠ task_end; §35 corrections re-plan ("Deploy to production" → "Actually, staging");
§36 low STT confidence + destructive ⇒ read back verbatim; §37 continuous autonomous work across
restarts; §38 checkpointing; §39 model-tool contract (LLM → schema → permission validator → executor);
§40 Listen/Understand/Remember/Reason/Plan/Speak/Act/Observe/Verify/Learn across COMPUTER (Windows),
BROWSER, CLOUD.

---

## Stack and environment

- Next.js 15.1.11 (App Router) + React 19.0.3 + TypeScript on **:4030**; FastAPI + Uvicorn on
  **:8000**; SQLAlchemy + SQLite (`aura/akansha.db`); Python 3.11 on Windows.
- Frontend: three ^0.184.0, @react-three/fiber ^9.6.0, @react-three/drei ^10.7.7,
  framer-motion 12.38.0, lucide-react, recharts, sonner, tailwindcss 3.4.6. Alias `"@/*": ["./src/*"]`;
  helpers in `src/lib/` (`apiBase.ts` has `apiUrl()` / `wsUrl()`).
- npm scripts: `dev` (concurrently), `dev:frontend` = `next dev -p 4030`, `dev:backend` =
  `python -m uvicorn backend.main:app --reload --reload-dir backend --port 8000`, `build`, `lint`,
  `type-check` = `tsc --noEmit`.
- Backend libs verified present: websockets 15.0.1, starlette 1.0.0, uvicorn 0.46.0, edge_tts 7.2.8,
  fastapi, openai (OpenRouter base_url), sqlalchemy, pydantic, aiohttp, pyautogui/pygetwindow/pywinauto,
  pymupdf, python-pptx, pillow, win10toast, playwright, pytest-html.
- **Not installed:** any VAD or streaming-STT package; `pytest-timeout`; **`pytest-asyncio`** — async
  tests must use `asyncio.run(...)` inside sync test functions.
- `backend/conftest.py` exists (puts `backend/` on `sys.path` so `agent_modules` imports).
  `backend/scripts/` exists. There is **no** `pytest.ini` / `pyproject.toml` / `setup.cfg` at `aura/`.
- Windows asyncio timer resolution ≈15.6 ms, so `await asyncio.sleep(0.005)` **does not sleep**, it
  yields. Tests that wait need a wall-clock deadline in units of `TICK = 0.02`.

---

## Architecture

- `aura/backend/voice_kernel/` — pure, I/O-free, event-driven decision kernel. `Event` in →
  `Directive` out. No network, no audio, no DB. Modules: `context.py`, `endpointing.py`, `events.py`,
  `intent.py`, `session.py`, `situation.py`, `speech_policy.py`, `states.py`, `task_graph.py`.
- `aura/backend/voice_ws.py` — the transport. `/ws/voice/{session_id}`, the backend's only
  `@app.websocket` route. Owns the socket, the writer queue, the heartbeat and the cancellation registry.
- `aura/backend/voice_executor.py` — the side-effecting half (model streaming, automation bridge,
  §12 compression). Registered via `voice_ws.set_executor` from a startup hook.
- `aura/backend/model_output.py` — pure streaming sanitiser (`StreamSanitizer`, `polish_for_speech`,
  `sanitize`). Strips reasoning blocks / chat-template residue before TTS.
- `aura/backend/voice_engine.py` — the **legacy** path (browser Web Speech API STT, HTTP + SSE).
  Still what the shipped UI uses.
- `aura/backend/voice_runtime.py` — composition root + autonomous runner.
- `aura/backend/scripts/voice_drive.py` + `voice_scenarios.py` — the live end-to-end harness (below).

### Wire protocol (`voice_ws`)

Client→server frames: `{"event": "<EventKind>", ...}`. Server→client: `Directive.as_dict()`.
`TICK_INTERVAL_S = 0.12` endpointing heartbeat; `KEEPALIVE_S = 1.0` (a `tick` liveness frame only fires
after 1 s of *outbound quiet*); `MAX_MESSAGE_BYTES = 64_000`;
`ACTIONABLE = (EXECUTE_PLAN, GENERATE_RESPONSE, COMPRESS_CONTEXT)`; `DRAIN_TIMEOUT_S = 2.0`.
Auth via `_authorised()`: `AKANSHA_VOICE_WS_TOKEN` if set, else loopback-only
(`_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient", None, ""}`). On connect it sends
`session_ready` (with `reconnected`), then `resume_available` if a plan is outstanding. A
`WebSocketDisconnect` is **not** a `session_close` — the plan survives for §37 resume.

Events: `session_open, session_close, speech_start, speech_frame, speech_end, silence_tick,
partial_transcript, final_transcript, stt_error, response_token, response_complete, tts_started,
tts_finished, task_started, step_started, step_completed, step_failed, task_completed, task_failed,
user_stop, user_pause, user_resume, user_confirm, user_reject, environment_update, error`.
Directives: `start_listening, stop_listening, start_transcribing, finalize_turn, keep_listening,
generate_response, speak, stop_tts, execute_plan, cancel_execution, pause_execution, resume_execution,
ask_clarification, request_confirmation, state_changed, compress_context, situation_changed, noop`.

Languages: English, Telugu (script + romanised), Hindi (Devanagari + romanised).

### Design rules established (keep these)

1. **Playback state ("am I making sound") is separate from conversational state ("whose turn is it").**
   The FSM cannot answer both — that is what `_tts_active` on `VoiceSession` is for.
2. `execute_next_subtask` is **blocking** (real browser/desktop automation), so it runs via
   `asyncio.to_thread`; `ContinuousVoiceJarvisEngine._notify` therefore fires from a worker thread.
3. A pause must not kill the runner. `execute_next_subtask` returns `None` for BOTH "no such session"
   AND "session not active".
4. **Rule duplication is the enemy.** Items 11/12/13 all followed from refusing to reimplement kernel
   rules client-side. Only the silence floor (240 ms) and ceiling (2600 ms) are duplicated in
   TypeScript, because a browser must be able to fire without the server.
5. `Task.cancel()` cannot reach a thread already inside pyautogui, so cancellation of blocking
   automation is cooperative via `threading.Event`, honest only at step boundaries.
6. **Sanitiser placement is load-bearing.** It lives *inside* `_stream_into`'s per-candidate loop so
   `produced` means "produced something **speakable**". A model whose entire reply was a reasoning
   block has told us nothing and the next candidate must get a turn.
7. **Streaming marker detection needs a held-back tail.** `<|tool_call_start|>` is 21 chars and arrives
   split across chunks. `_keep_tail` holds back exactly the longest suffix that is a proper prefix of
   some marker, so ordinary prose streams with zero added latency (matters for §19).
8. **Unmarked chain-of-thought is unfixable in code.** No reliable way to tell "Here's a thinking
   process:" from a legitimate reply about thinking processes. Models that dump reasoning without
   markers are excluded from `VOICE_MODELS` rather than heuristically stripped.
9. **Real uvicorn beats `TestClient`** for this harness: `TestClient` drives the ASGI app through a
   portal thread, serialising the very concurrency under test. Also
   `starlette.testclient.WebSocketTestSession.receive` has signature `(self) -> 'Message'` — no
   timeout parameter.

---

## Audit ledger — 20 items

| # | Where | Problem | State |
|---|---|---|---|
| 1 | `main.py:6407` | SSE frame written as literal `\\n\\n`; blocking-question branch never terminated its event | **fixed + verified** (`repr()` scan proved it was the only bad frame of 14) |
| 2 | `main.py:6481,6502` | `OpenWorkRecurringPipeline()` per request; `active_sessions` is instance state ⇒ status always `found:false` | **fixed + verified** (`get_openwork_pipeline()` singleton) |
| 3 | `voice_engine.py:436` | stop-words matched by substring ("i am waiting for the build" → stop) | **fixed + verified** (kernel `detect_control_phrase`, 19 cases) |
| 4 | `voice_engine.py:346,556` | `resolve_intent()` called twice per utterance ⇒ two blocking LLM round-trips before a word is spoken | **fixed + verified** (`_resolve_once`, count 2→1) |
| 5 | `voice_engine.py:746` | one global `_voice_engine` / `SessionState`; `session_id` accepted then discarded | **fixed + verified** (per-session LRU, `MAX_VOICE_SESSIONS = 64`) |
| 6 | `voice_engine.py:665` | login `yes/ok` regex tested against any transcript ⇒ "okay open youtube" marks you logged in | **fixed + verified** (`_login_answer()` → True/False/None) |
| 7 | wiring | `ConversationalOrchestrator` / `VoiceFeedbackLoop` never instantiated outside tests | **fixed + verified + regression-tested** |
| 8 | `main.py:6290` | no autonomous runner; graph advanced only on client HTTP calls ⇒ §14/§17 impossible | **fixed + verified + regression-tested** |
| 9 | `useVoice.ts:916` | `SpeechRecognition` effect depended on `selectedVoiceId`, which a sibling effect sets ⇒ `abort()`/rebuild mid-utterance | **fixed + typecheck-verified** |
| 10 | 18 files, 58 sites | `http://localhost:8000` hardcoded | **fixed + typecheck-verified** |
| 11 | `VoiceAssistantClient.tsx:225` | fixed 1200 ms silence timer; backend's `recommended_silence_ms` discarded | **fixed** (new `POST /api/voice/turn-boundary`) |
| 12 | `voice_kernel/endpointing.py` | dangling-tail veto was one weighted vote, not a veto ⇒ §6 canonical example flipped by `recognizer_final` / `pending_question` / `holds_floor` | **fixed + regression-tested** |
| 13 | wiring | `set_executor` never called anywhere ⇒ the whole `/ws/voice/` kernel path was inert | **fixed + regression-tested** (`backend/voice_executor.py` + startup hook; `test_voice_executor.py::TestInstallation` asserts the startup hook, not just `install()`) |
| 14 | `voice_ws.py:73` | executor contract could only return a list ⇒ §19 streaming TTS structurally impossible | **fixed + regression-tested** (async-generator executors; the streaming test blocks the producer until the consumer has seen token 1, so a batching regression deadlocks instead of passing) |
| 15 | `voice_ws.py` | `CANCEL_EXECUTION` never cancelled anything; a new `GENERATE_RESPONSE` did not supersede the old one | **fixed + regression-tested** (`_work`/`_stop_flags` registry; `test_voice_ws_cancellation.py`, 24 cases) |
| 16 | `ai_engine.py:27–34` | **all 5 `OPENROUTER_FALLBACK_MODELS` dead** (3× 402 credits exhausted, 2× 404 "unavailable for free") — a five-deep cascade where every entry fails | **fixed + regression-tested** (`TestVoiceModelCascade`: ≥2 `:free` fallbacks) |
| 17 | `ai_engine.py:5761` | **voice sessions got `[OPENROUTER_MODEL]` — a list of ONE**, so one dead route made every spoken reply the canned `_provider_failure_fallback`, while the text path silently fell through to a working model | **fixed + regression-tested** (`_voice_model_candidates()`; `TestVoiceModelCascade::test_voice_cascade_is_not_a_list_of_one`) |
| 18 | everywhere | **no sanitisation of model scaffolding before TTS.** Measured: `liquid` emits `<\|tool_call_start\|>[bash(command='ls -la')]<\|tool_call_end\|>`; `lightning` emits 1721 chars of `Here's a thinking process:`. Also no markdown flattening for TTS | **fixed + regression-tested** (`backend/model_output.py`; markers in `TestModelOutputSanitiser`, the markerless dumper's exclusion in `TestVoiceModelCascade`) |
| 19 | prompt assembly | **model spoke a state block instead of an answer.** Asked "what is the capital of France?" it said `{ state: SPEAKING, mode: VOICE, language: english, source: wikipedia }` across three P2 utterances | **fixed + proven at runtime** (`voice_drive --scenario reply`) |
| 20 | `session.py:411` (§33) | **wake-word gate silently discarded the first utterance of a cold session** — `_process_turn` returned `KEEP_LISTENING` *before* `add_turn`, so no context, no reply, total silence | **fixed + proven at runtime** (`voice_drive --scenario memory`) |
| 21 | `voice_executor.py:411` (§12) | **rolling compression raised `AttributeError` on every call and deleted the batch it failed to summarise.** It read `Turn.content`; the field is `Turn.text`. The message list was built *outside* the try, so the throw escaped after `take_compression_batch()` had already removed the turns — measured 40 turns → 24, zero summaries. §12 inverted: instead of compressing the oldest turns it silently dropped them | **fixed + regression-tested** (`t.text`, whole transaction inside the try; `test_voice_executor.py::TestCompressContext`) |

### Items 13–15, 21 — test coverage added 2026-08-24

`voice_executor.py` and the `voice_ws` cancellation registry had no dedicated
tests: the suite exercised the socket (a frame in, the right frames out) and the
kernel (decisions), and nothing covered the seam between them. Both defects that
live in that seam are invisible from the wire — a test asserting that a `stop_tts`
frame arrived passes whether or not the generation it names actually stopped. Two
files, 52 cases:

- `backend/tests/test_voice_executor.py` (28) — the registration itself, incremental
  token delivery, barge-in not speaking the superseded answer, stall handling,
  model failover and speakability filtering, plan step events, cooperative
  cancellation, and the §12 compression transaction. `_run_automation` is
  monkeypatched throughout; the real one drives the actual mouse.
- `backend/tests/test_voice_ws_cancellation.py` (24) — work-key naming, the
  generation singleton, what `cancel_execution` / `stop_tts` / `pause_execution`
  each reach, kill-before-start ordering within one directive batch, and `close()`
  setting every cooperative flag before cancelling tasks.

Item 21 was found *by* writing these: the compression test would not go green.
Full suite 268 → **320 passed**.

### Items 16–18 — test coverage added 2026-08-25

These three were marked "fixed" with nothing pinning them, and they are the kind
that come back silently. None of them throws: when the cascade degenerates the
symptom is only that every spoken reply becomes the canned provider fallback, and
the text path keeps working, so the suite stays green while voice is mute. Fifteen
cases in `backend/tests/test_voice_persona_and_gate.py`:

- `TestVoiceModelCascade` (6) — item 17 as `len(_voice_model_candidates()) > 1`,
  because the defect was literally a one-entry list; item 16 as "at least two
  fallbacks end in `:free`", because a cascade of paid routes on a zero-balance key
  is what five-deep-and-all-dead looks like; every entry of `VOICE_MODELS` free;
  candidates deduplicated (the env usually names a model already in the list, and
  retrying one dead route twice per utterance is the cost); the default model
  concrete rather than `openrouter/auto`.
- Item 18's marker half, added to `TestModelOutputSanitiser` (9) — the harmony
  `<|channel|>analysis` … `<|message|>` block, which is the one block whose closer
  is not the mirror of its opener, so a tag-pairing implementation passes it
  through; `<|im_start|>`/`<|im_end|>` as *drops* where the surrounding text must
  survive; and `for_speech` as a flag, since the chat UI renders the markdown that
  the voice path strips.

Item 18's other half is a **configuration** invariant, not a parser one, and that
is why it is asserted as one: `nemotron-3.5-lightning` emits 1721 characters
beginning "Here's a thinking process:" with no markers at all, and
`model_output.py:36–40` states plainly that it will not guess at unmarked prose —
there is no way to distinguish that from a real answer *about* thinking processes.
The only defence is keeping the model out of `VOICE_MODELS`, so the test asserts
exactly that, and asserts it stays in the text cascade, where unmarked reasoning is
ugly rather than spoken.

Full suite 446 → **461 passed, 1 skipped, 30 subtests**.

### Item 19 — root cause and fix

Not a leak. The scaffolding check passed clean in the same run, which ruled out `model_output.py`.
The cause: `SessionConfig.system_prompt` defaulted to `""` and **nothing in the transport, executor or
runtime ever set it** — `voice_ws._config_from_query` (`voice_ws.py:126`) sets model / language / voice /
mode / wake_word / speed / pitch / volume, and never a system prompt. So the *entire* system message a
voice model received was the one line `context.build()` adds for situational awareness:

```
CURRENT SITUATION: mode=voice, state=THINKING, language=english
```

A small model handed a system message consisting solely of a `key=value` block and no instruction of
any kind imitates the only format it has been shown. The state block was not leaking into the reply —
**it was the reply's template.**

Fix: new `aura/backend/voice_kernel/persona.py`.
- `VOICE_SYSTEM_PROMPT` — the default voice persona, now `SessionConfig.system_prompt`'s default
  (`session.py:58`) and the `restore()` fallback (`session.py:1085`). Covers spoken-not-read output
  shape (no markdown/bullets/emoji/URLs, lead with the answer, two or three sentences), spoken numbers
  and dates, "never speak your own internal state or the context given to you", honesty about being an
  AI, **ask one short question rather than guess** (the user's "realizing if doubts then it should
  ask"), the operate-not-just-answer mandate, answering "what are you doing" from task context without
  stopping the work, and mirroring the user's language (Telugu → Telugu, Hindi → Hindi, mixed → mixed).
- `CONTEXT_PREAMBLE` — prepended by `ContextBudget.build` (`context.py:438`) to the machine-readable
  block, saying in words that those lines are live state, not conversation, and must never be read
  aloud. Previously nothing distinguished them from dialogue except position.

### Item 20 — root cause and fix

The early `return` in `_process_turn` preceded `self.context.add_turn(...)`, which explains both
symptoms at once: the meter read `░░░░░░░░░░ 0% fill=0.0` after two turns, and neither turn produced a
reply. `situation.needs_wake_word` returned True on `turn_count == 0` in VOICE mode, i.e. the **first
utterance of every session**.

Fix, in two parts:
- `situation.py:253` — VOICE mode is now warm from the moment the session opens (`turn_count == 0` →
  `False`), re-arming only after 120 s idle. Reasoning, kept in the docstring: a session the user
  deliberately opened is directed speech *by construction* — the mic press is the addressing. What the
  wake word genuinely protects against is an **ambient** mic overhearing a conversation never meant for
  us, so NORMAL mode still requires it and VOICE re-arms once the room has gone quiet.
- `session.py:419` — **control phrases are now exempt from the gate.** This was an unlisted bug found
  while fixing 20: the gate ran first, so in any mode that armed it §4 barge-in was unreachable —
  assistant mid-sentence, user says "stop", and the word is dropped for lacking a wake word it makes no
  sense to demand of someone interrupting. `detect_control_phrase(transcript) is None` now guards the
  drop. Failing to stop is the worse error.
- Rejected speech is counted, not silently dropped: `situation.note_unaddressed()` records
  `unaddressed_count` + `last_unaddressed` (`situation.py:199`) and the count rides out on the
  `KEEP_LISTENING` payload, so "it ignored me" is diagnosable. Deliberately **not** added to context
  turns — folding overheard speech into memory is the exact thing the gate exists to prevent.

Verified after both fixes: `main imports clean`, **210 passed**. What is *not* yet verified is runtime
behaviour against a live model — see the first command below.

### Also observed, not yet acted on

`.env` sets `OPENROUTER_MODEL=openai/gpt-4o-mini`, and `_voice_model_candidates()` puts
`OPENROUTER_MODEL` first — so every voice request currently burns a wasted 402 round trip before
reaching the working free model. Visible in the run log.

---

## Current model configuration (`ai_engine.py`)

All five previous fallbacks were probed dead against this account's key. `openai/gpt-4o-mini`,
`openai/gpt-4o-mini-2024-07-18`, `mistralai/mistral-small-2603` → 402 (still 402 at `max_tokens=64`,
so it is the balance, not the request size); `nex-agi/nex-n2-pro:free`, `openai/gpt-oss-20b:free` →
404 "unavailable for free". Current:

```python
DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3-nano-30b-a3b:free"

VOICE_MODELS = (DEFAULT_OPENROUTER_MODEL, "liquid/lfm-2.5-2.6b:free")

OPENROUTER_FALLBACK_MODELS = (
    DEFAULT_OPENROUTER_MODEL,
    "liquid/lfm-2.5-2.6b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "openai/gpt-4o-mini",
    "mistralai/mistral-small-2603",
)
```

Paid routes are kept *after* the free ones rather than deleted: they are better models and start
working again the moment credits are topped up, and a 402 fails immediately rather than timing out.
`nvidia/nemotron-3.5-lightning:free` is excluded from `VOICE_MODELS` on measured evidence — it streams
its full chain of thought as unmarked prose.

`_voice_model_candidates()` returns a 2–3 entry cascade (`OPENROUTER_MODEL` then `VOICE_MODELS`,
deduped, capped at 3). The original single-entry list had a *latency* rationale that was correct and is
preserved in the docstring; the length was not.

---

## Test results

### Kernel / unit

```bash
cd /c/MY-AI/aura && python -c "import backend.main; print('main imports clean')" && python -m pytest backend/tests/ -q
```

→ `main imports clean`, **210 passed**. Pre-existing and left alone:
`test_cca_properties.py::TestCCAUnits::test_max_lanes_rejection` emits
`RuntimeWarning: coroutine 'Event.wait' was never awaited`.

### Live end-to-end harness

```bash
cd /c/MY-AI/aura && python -m backend.scripts.voice_drive --no-llm
```

**13 passed, 0 failed, 3 informational.**

- **§6 mid-sentence pause: PASS both halves** — turn held open past 1.8 s on "Open my project and",
  then `finalize_turn` fired on the completed sentence. This is exactly the bug the old hardcoded
  1200 ms timer caused, now demonstrably gone end to end.
- **§7 backchannels: 4/4 PASS** — "yeah", "okay", "mm-hmm", "right" produced no `generate_response`
  and no `execute_plan`.
- **§4 barge-in: PASS** — `stop_tts` (reason "user barge-in"), then `stop_tts` (INTERRUPTION),
  `cancel_execution`, and `🔊 AKANSHA [P0] Stopped.`
- **§16 automation planning: PASS** — "open youtube and search for lo-fi beats" → 4 steps
  (`open_url https://youtube.com`, `wait`, `type_text lo-fi beats`, `press_key enter`); "open notepad
  and type hello" → 3 steps. Real desktop execution correctly skipped.
- **§37 resume: PASS all 3** — task active before the drop, `reconnected=True`, `resume_available`
  with `goal: "index the whole r…"`.
- **§26 TTS: PASS** — 36,144 bytes of real edge-tts audio written to
  `backend/scripts/_voice_drive_sample.mp3`.

```bash
cd /c/MY-AI/aura && python -m backend.scripts.voice_drive --scenario reply --scenario memory
```

**2 passed, 2 failed** — the two failures are items 19 and 20 above. `correction`, `concurrent` and
`telugu` **have not been run at all yet**.

### Harness bugs found and fixed (all mine, not the product's)

1. `detach()` cancelled the reader but the `async with` block's `close()` still sent `session_close`,
   genuinely ending the session so the reconnect found nothing → added a `_detached` flag honoured by
   `close()`.
2. `last_tick()` returned `{}` because the keepalive only fires after 1 s of *outbound quiet* and the
   connection had been busy → added `async def tick()` that waits for a fresh tick frame. Three
   spurious resume failures came from this.
3. `UnicodeEncodeError: 'charmap' codec can't encode characters` — Windows console is cp1252 and the
   harness prints box-drawing characters and Telugu → `sys.stdout.reconfigure(encoding="utf-8",
   errors="replace")` guarded by `contextlib.suppress(AttributeError, ValueError)`.
4. A probe f-string formatted a `None` ttft → `TypeError`. Not a product issue, but it did reveal that
   `openrouter/free` streams chunks with no content — the exact case `_stream_into`'s "connected but
   produced nothing" branch handles.

Also: `cat >> ... <<'PYEOF'` heredocs die on the quote characters in scenario code
(`unexpected EOF while looking for matching '`). Use the Write tool for Python files.

---

## Honesty constraints that must survive

- **I cannot speak into a microphone or hear audio.** The harness feeds transcripts as
  `partial_transcript` / `final_transcript` events — the same boundary the browser's Web Speech API
  sits on — so it proves everything from the transcript inward and **nothing** about acoustic accuracy
  or perceived voice quality. This must never be reported as a live spoken test.
- `execute_desktop_command` moves the real mouse and launches real applications, so
  `python -m backend.scripts.voice_drive --desktop` stays off unless the user **explicitly authorises
  real desktop control**. Ask before running it.

---

## Remaining work, in order

1. **Prove items 19 and 20 at runtime.** The code is fixed and 210 tests pass, but the failing
   scenarios have not been re-run against a live model, so the fix is unverified end to end:

   ```bash
   cd /c/MY-AI/aura && python -m backend.scripts.voice_drive --scenario reply --scenario memory
   ```

   Expect: `reply` says something containing "paris" (item 19 — no `{ state: ... }` block), and
   `memory` recalls "nightingale" with the context meter **off 0%** (item 20). If `reply` still
   answers in key=value form, the persona is not reaching the model — check that
   `directive.payload["context"]["messages"][0]["content"]` actually begins with "You are Akansha".
2. **Run the three scenarios that have never run once:**

   ```bash
   cd /c/MY-AI/aura && python -m backend.scripts.voice_drive --scenario correction --scenario concurrent --scenario telugu
   ```
3. **Fix the wasted 402 round trip.** `.env` sets `OPENROUTER_MODEL=openai/gpt-4o-mini`, one of the
   dead paid routes, and `_voice_model_candidates()` puts `OPENROUTER_MODEL` first — so every voice
   request pays a failed request before reaching a working free model. Either change the `.env` value
   to `nvidia/nemotron-3-nano-30b-a3b:free` or have `_voice_model_candidates()` skip a configured model
   already known to be failing.
4. **Styling + performance pass** on the web app — the last clause of the governing instruction.
   `next lint` currently reports **3269 problems, 3135 of them prettier CRLF/formatting**, plus 10 hard
   non-prettier errors in files not touched by this work.
5. **Report the audit narrative in text.** The user has seen the item table but not the wider findings:
   - **Graphify structural imbalance**: Frontend UI & Navigation **5982 nodes** vs Desktop & Browser
     Automation **29 nodes** — the automation surface is ~0.5% of the UI surface, for a product whose
     stated goal is "complete web and desktop control".
   - `main.py` ≈ 6570 lines / 87 routes; `ai_engine.py` 5884 lines.
   - Items 12–20 as findings, of which **13 is the largest**: the entire §1/§4/§6 kernel pipeline was
     complete, tested, and unreachable from the running product.
5. **Regression tests for items 13–18.** `voice_executor.py`, the `voice_ws` cancellation registry and
   `model_output.py` have no dedicated pytest coverage yet — the 210 passing tests include none that
   register an executor or exercise the sanitiser. (The sanitiser *was* verified ad hoc: 8 cases ×
   chunk sizes {1, 3, 7, 1000}, all matching, including the false-positive guard `A < B and C > D` →
   unchanged and `<think>unterminated dump` → empty.)
6. **Phase 2, not started:** §23–§28 UI / 3D work (dark glassmorphism, state-reactive 3D core,
   real-time audio viz, lip sync, English + Telugu), real VAD + streaming STT/TTS, §39 model-tool
   contract, §37/§38 persistence + checkpointing, a frontend client for `/ws/voice/{session_id}`
   (`wsUrl()` in `src/lib/apiBase.ts` exists for this), and an `/api/runtime/events` consumer in the UI.
7. **Housekeeping, offered but not done:** this `HANDOFF.md` is untracked at repo root — offered to move
   it under `aura/docs/` or gitignore it. `backend/scripts/_voice_drive_sample.mp3` is a new untracked
   artifact that should probably be gitignored.

## Spec behaviours confirmed by direct probe (context for why the code looks the way it does)

- **§36** low STT confidence (0.62) + destructive ⇒ `CONFIRMING` + verbatim read-back; the following
  `"yes"` executes the *read-back text*, not the shaky transcript.
- **§35** correction ⇒ `cancel_execution{nodes:[…]}` + `execute_plan{replaces:…}`; graph goes
  `[CANCELLED, PENDING]`.
- **§20** `"stop"` arriving as a *partial* at 300 ms ⇒ `stop_tts` + `cancel_execution` +
  `speak "Stopped." prio 0`.
- **§14** "what are you doing?" injects `CURRENT TASK: …` into the system message without disturbing
  the running node. The task line is at `payload['context']['messages'][0]['content']`, **not**
  `payload['system']` — a probe reading the wrong key produced a false negative once.
- **§5** assistant-sourced speech ⇒ `NOOP` (our own echo is never a barge-in).

### Kernel bugs found and fixed while building it

1. **`peek()` precedence — §4 barge-in was silently dead.** `RESPONSE_TOKEN` sat in `_INERT`, and
   `peek()` checked `_INERT` before `_TABLE`, so the `UNDERSTANDING/THINKING → RESPONDING` rows were
   dead code and the session never took the floor. Order is now `_UNIVERSAL → _TABLE → _INERT → ILLEGAL`.
2. **`_Connection.close()` dropped the final frame.** It cancelled the writer before pushing the
   sentinel, so the queued `stop_listening` died with it. Now: stop accepting frames → push sentinel →
   cancel producers → bounded 2 s drain.
3. **A second, deeper barge-in gap.** `TTS_STARTED` is legal only in `RESPONDING` (→`SPEAKING`),
   `EXECUTING` and `CONFIRMING`, and `holds_floor` is `RESPONDING|SPEAKING` — so while speaking a P1
   read-back from `CONFIRMING`, `holds_floor` was False and barge-in over the safety question emitted
   nothing. Hence `_tts_active`.

`SpeechPolicy.decide()` (`speech_policy.py:100–200`): `progress_min_gap_s = 6.0`,
`max_staleness_s = 12.0`. INTERRUPTION → `INTERRUPT_SELF` + `Utterance("Stopped.", Priority.P0_EMERGENCY,
"stop-ack", interruptible=False)`; BACKCHANNEL → `LISTEN` or `WAIT` when a question is pending;
CORRECTION → `EXECUTE` ("correction — amend the plan"); SOCIAL_CONVERSATION closing with no task →
`END_CONVERSATION`.

---

## Standing constraints

- Do **not** call the Agent tool / subagents unless asked. Do **not** use workflows or deep-research
  unless asked.
- Comply silently with Write/Edit size limits; chunk without commentary; never propose bypassing them.
- Never echo secret values; refer to keys by name. `aura/backend/.env` is **not** tracked by git.
  `OPENROUTER_API_KEY` was confirmed present by length only (73 chars) and never printed. A 402 error
  string did surface an OpenRouter `user_id` in a log line; no key material was exposed.
- Destructive or hard-to-reverse operations need explicit confirmation first.
- Reference files as markdown links relative to the working directory; runnable commands go in their
  own `bash`-fenced block.
