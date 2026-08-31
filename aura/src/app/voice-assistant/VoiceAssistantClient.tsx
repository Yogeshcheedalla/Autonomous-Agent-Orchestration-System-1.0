'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import dynamic from 'next/dynamic';
import AppLayout from '@/components/AppLayout';
import { useVoice } from '@/hooks/useVoice';
import { useReducedMotion } from '@/hooks/useReducedMotion';
import { useElementHeight } from '@/hooks/useElementHeight';
import { apiUrl } from '@/lib/apiBase';
import { isBareWakeCall, matchWakeWord } from '@/lib/wakeWord';
import type { NeuralPresenceHandle } from '@/components/assistant/NeuralPresence';

import MicControl from './components/MicControl';
import StagePresence from './components/StagePresence';
import TranscriptRail, { type Turn } from './components/TranscriptRail';
import VoiceHeader from './components/VoiceHeader';
import { STARTERS, type LoopState, type VoiceLanguageKey } from './voiceDesign';

// Client-only: the rig reads `matchMedia` and drives itself off
// requestAnimationFrame, neither of which exists during prerender, and there is
// nothing about a face at rest worth putting in the server HTML.
//
// `NeuralPresence` is the photoreal face — a MuseTalk worker streaming JPEG
// frames — and it falls back to the CSS rig by itself whenever no worker is
// reachable, which is the normal state until a Colab tunnel is registered. So
// this page renders one component and never has to decide which face is live.
const NeuralPresence = dynamic(() => import('@/components/assistant/NeuralPresence'), {
  ssr: false,
  loading: () => <div className="h-[429px] w-[286px] animate-pulse rounded-[2rem] bg-white/5" />,
});

// The full-body presence, for full screen only. Client-only for the same reason,
// and lazily loaded for a different one: it pulls 1.6 MB of behaviour clips, and
// a user who never opens full screen should never pay for them.
const BodyPresence = dynamic(() => import('@/components/assistant/BodyPresence'), {
  ssr: false,
});

// ── Types ────────────────────────────────────────────────────────────────────

interface TaskStatus {
  active: boolean;
  site: string;
  step: string;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const NEXT_PROMPTS = [
  'What else can I help with?',
  'Anything else?',
  'What would you like next?',
  'Go ahead, I am listening.',
  'What is next?',
];

function nextPrompt() {
  return NEXT_PROMPTS[Math.floor(Math.random() * NEXT_PROMPTS.length)];
}

// ── Telugu unicode filter for live text ──────────────────────────────────────

function filterLiveText(text: string, lang: string): string {
  const hasTeluguUnicode = /[\u0C00-\u0C7F]/.test(text);
  if (hasTeluguUnicode || lang === 'telugu_english') {
    if (hasTeluguUnicode) return '🎤 Listening in Telugu...';
  }
  return text;
}

// ── End-of-turn detection (§6) ────────────────────────────────────────────────
// These mirror `EndpointDetector`'s floor and ceiling in the kernel. They are
// duplicated here on purpose and only here: the client has to be able to fire on
// its own if the backend stops answering, and it must not ask before the floor
// (the answer can only be "below minimum silence"). Everything *judgemental* —
// dangling tails, continuation markers, punctuation, thresholds — stays server
// side, because two copies of a rule are two rules.

/** Detector's `min_silence_ms`: the earliest an answer can be anything but "no". */
const FIRST_CHECK_MS = 240;
/** Detector's `max_silence_ms`: fire regardless, even if we cannot reach it. */
const MAX_SILENCE_MS = 2600;
/** Floor on re-check spacing. The detector will happily say "60ms"; that would
 *  be ~30 requests for one hesitant sentence. */
const MIN_RECHECK_MS = 200;
/** Offline fallback, replaced by the backend's own `recommended_silence_ms` after
 *  the first turn of a session. */
const DEFAULT_SILENCE_MS = 1200;

interface TurnBoundary {
  should_finalize: boolean;
  recheck_in_ms: number;
  turn_complete_probability: number;
}

/** Ask the kernel whether the turn is over. `null` means "could not reach it". */
async function askTurnBoundary(body: {
  transcript: string;
  silence_ms: number;
  pending_question: boolean;
  executing: boolean;
  holds_floor: boolean;
  recognizer_final: boolean;
}): Promise<TurnBoundary | null> {
  try {
    const res = await fetch(apiUrl('/api/voice/turn-boundary'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as Partial<TurnBoundary>;
    if (typeof data.should_finalize !== 'boolean') return null;
    return {
      should_finalize: data.should_finalize,
      recheck_in_ms: typeof data.recheck_in_ms === 'number' ? data.recheck_in_ms : MIN_RECHECK_MS,
      turn_complete_probability: data.turn_complete_probability ?? 0,
    };
  } catch {
    return null;
  }
}

// ── Main component ────────────────────────────────────────────────────────────

export default function VoiceAssistantClient() {
  const {
    isListening,
    isSpeaking,
    transcript,
    finalTranscript,
    speakingVolume,
    voiceLanguage,
    setVoiceLanguage,
    startListening,
    stopListening,
    speak,
    stopSpeaking,
    clearTranscript,
    onSpeechAudio,
    setBackgroundListening,
    lastSpeechEnergyAt,
    localVoiceActivity,
  } = useVoice();

  const [active, setActive] = useState(false);
  /**
   * Whether the microphone stays open while idle, waiting for "hey Akansha".
   *
   * Off by default and remembered, because arming it takes a microphone
   * permission prompt and an always-on recogniser -- neither of which should
   * happen because someone opened a page. Once the owner turns it on, it stays
   * on across reloads, which is the whole point of a wake word.
   */
  const [wakeWordEnabled, setWakeWordEnabled] = useState(false);
  /**
   * Full-screen presence: her, the halo, and two icon buttons. Nothing else.
   *
   * The normal layout is a working surface -- header, transcript rail, starter
   * chips, live caption -- and all of it competes with her for attention. This
   * mode is for talking rather than reading, so every word on the page goes away
   * except the ones she says out loud.
   *
   * Deliberately not a route. A separate page would remount `NeuralPresence`,
   * which means tearing down the MuseTalk socket and dropping frames mid-
   * sentence; a state flag keeps one face alive for the life of the tab.
   */
  const [immersive, setImmersive] = useState(false);
  const [loopState, setLoopState] = useState<LoopState>('idle');
  const [liveText, setLiveText] = useState('');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [taskStatus, setTaskStatus] = useState<TaskStatus>({ active: false, site: '', step: '' });
  const [loginPrompt, setLoginPrompt] = useState<{ site: string; msg: string } | null>(null);
  const [awaitingLogin, setAwaitingLogin] = useState(false);
  const [showHistory, setShowHistory] = useState(true);
  const [mounted, setMounted] = useState(false);
  const reducedMotion = useReducedMotion();

  // Her size is a consequence of the layout, not a constant. The stage is a
  // `flex-1` box, so its height is whatever the microphone, caption and starter
  // chips left over; 286×429 is her intrinsic box, so height ÷ 1.5 is the widest
  // she can be without any of her being cropped. Never above 286 — upscaling a
  // photograph past its natural size only softens it — and never below 120, past
  // which she stops reading as a face at all and the layout should scroll instead.
  const [stageRef, stageHeight] = useElementHeight<HTMLDivElement>();
  const presenceWidth = stageHeight
    ? Math.max(120, Math.min(286, Math.round(stageHeight / 1.5)))
    : 286;

  useEffect(() => {
    setMounted(true);
  }, []);

  const processingRef = useRef(false);
  const activeRef = useRef(false);
  const speakingRef = useRef(false);
  const silenceTimerRef = useRef<number | null>(null);
  // Read from inside the silence loop, which is not re-created when these change
  // — it is keyed on the transcript, and rebuilding it mid-pause would restart
  // the silence clock and make the turn feel like it never ends.
  const awaitingLoginRef = useRef(false);
  const taskActiveRef = useRef(false);
  // Last `recommended_silence_ms` the backend gave us, used only when
  // `/api/voice/turn-boundary` is unreachable.
  const silenceBudgetRef = useRef(DEFAULT_SILENCE_MS);
  const turnIdRef = useRef(0);
  // Conversation session ID, regenerated on each start. The "voice_" prefix is
  // load-bearing: the backend keys off it to skip live web fetches, which is what
  // keeps a spoken turn from waiting on a page crawl.
  const sessionIdRef = useRef('voice_pending');

  // The photoreal face. Held once for the page: reconnecting the avatar socket
  // whenever the conversation session id is regenerated would drop frames
  // mid-sentence for no benefit, since the socket is per-tab, not per-turn.
  const presenceRef = useRef<NeuralPresenceHandle>(null);
  const [avatarSessionId] = useState(() => `voice_${Math.random().toString(36).slice(2, 10)}`);

  // Hand every synthesized utterance to the face, with the playing element as
  // its clock. `useVoice` owns the audio; this only borrows `currentTime`, so
  // the face and the sound can never be two different waveforms.
  useEffect(() => {
    return onSpeechAudio(({ id, blob, audio }) => {
      presenceRef.current?.speak(blob, { id, getElapsedSeconds: () => audio.currentTime });
    });
  }, [onSpeechAudio]);

  // Sync refs
  useEffect(() => {
    activeRef.current = active;
  }, [active]);
  useEffect(() => {
    speakingRef.current = isSpeaking;
  }, [isSpeaking]);
  useEffect(() => {
    awaitingLoginRef.current = awaitingLogin || loginPrompt !== null;
  }, [awaitingLogin, loginPrompt]);
  useEffect(() => {
    taskActiveRef.current = taskStatus.active;
  }, [taskStatus.active]);

  // Autoscroll lives in `TranscriptRail`, which owns the scroll container and can
  // therefore tell whether the user has scrolled up to read back. This effect
  // scrolled unconditionally from out here, which yanked the panel to the bottom
  // every time a reply streamed a chunk.

  // Mirror live transcript — filter Telugu unicode
  useEffect(() => {
    const raw = transcript || finalTranscript || '';
    setLiveText(filterLiveText(raw, voiceLanguage));
  }, [transcript, finalTranscript, voiceLanguage]);

  // Derive loopState from hook state
  useEffect(() => {
    if (!active) {
      setLoopState('idle');
      return;
    }
    if (isSpeaking) {
      setLoopState('speaking');
      return;
    }
    if (isListening) {
      setLoopState('listening');
      return;
    }
    if (processingRef.current) {
      setLoopState('processing');
      return;
    }
    setLoopState('waiting');
  }, [active, isSpeaking, isListening]);

  // Add a turn to the conversation log
  const addTurn = useCallback((role: 'user' | 'ai', text: string, extra?: Partial<Turn>) => {
    const id = ++turnIdRef.current;
    setTurns((prev) => [...prev, { id, role, text, at: new Date().toISOString(), ...extra }]);
  }, []);

  // Restart listening after response finishes
  const restartListening = useCallback(() => {
    if (!activeRef.current) return;
    clearTranscript();
    setLiveText('');
    setTimeout(() => {
      if (activeRef.current) startListening();
    }, 600);
  }, [clearTranscript, startListening]);

  // Wait until TTS finishes — polls speakingRef every 150ms
  const waitForSpeech = useCallback(
    () =>
      new Promise<void>((resolve) => {
        // Give edge-tts ~400ms to start before polling
        setTimeout(() => {
          const check = setInterval(() => {
            if (!speakingRef.current) {
              clearInterval(check);
              resolve();
            }
          }, 150);
          // Hard timeout: 25s max
          setTimeout(() => {
            clearInterval(check);
            resolve();
          }, 25_000);
        }, 400);
      }),
    []
  );

  // ── Silence detection: adaptive end-of-turn, decided by the kernel (§6) ───
  // This used to be `setTimeout(..., 1200)`, which is wrong in both directions.
  // "Open my project and… [pause] …find the failing tests" was cut in half at
  // the pause, and "stop" waited 1.2s for nothing. So instead of guessing a
  // duration, poll `/api/voice/turn-boundary` while the user is silent and let
  // the same `EndpointDetector` the WebSocket transport uses make the call.
  //
  // No requests are made while speech is arriving: every transcript update
  // re-runs this effect, which cancels the pending check and restarts the clock.
  useEffect(() => {
    if (!isListening || !active) return;
    const text = finalTranscript.trim() || transcript.trim();
    if (!text) return;
    if (silenceTimerRef.current) clearTimeout(silenceTimerRef.current);

    // Silence starts when the *microphone* stopped hearing speech, not when the
    // transcript stopped changing. Those are different moments and the difference
    // is not small: Chrome's recogniser is a network round trip, so its last
    // interim word can land a few hundred milliseconds after the user actually
    // went quiet -- and while the recogniser is stalled, the transcript stops
    // changing for reasons that have nothing to do with the user at all. The
    // waveform monitor in `useVoice` already tracks real speech energy for
    // barge-in, against an adaptive noise floor; this reads that instead of
    // guessing. Falls back to "now" when the analyser has not seen speech yet,
    // which is the old behaviour and the only safe default.
    const energyAt = lastSpeechEnergyAt();
    const now = Date.now();
    // Clamped to the ceiling: a stale energy timestamp from an earlier turn must
    // not make this turn look like it has already been silent for a minute and
    // fire instantly.
    const silenceStartedAt = energyAt > 0 && now - energyAt < MAX_SILENCE_MS ? energyAt : now;
    const recognizerFinal = finalTranscript.trim().length > 0;
    let cancelled = false;

    const fire = () => {
      if (cancelled || processingRef.current || !activeRef.current) return;
      stopListening();
      void processInput(text);
    };

    const check = async () => {
      if (cancelled || processingRef.current || !activeRef.current) return;
      const silenceMs = Date.now() - silenceStartedAt;
      // Silero's figure wins when there is one. It is the same class of
      // measurement -- trailing silence on the waveform -- but computed by the
      // VAD the backend's own `EndpointDetector` was tuned against, on the exact
      // audio that produced this transcript. Only available on the local-Whisper
      // path, because that is the only path where the audio reaches the server.
      const measured = localVoiceActivity();
      const effectiveSilenceMs =
        measured && measured.trailing_silence_ms > 0
          ? Math.max(measured.trailing_silence_ms, silenceMs)
          : silenceMs;
      // Hard ceiling, enforced locally: a dangling "and" must not hold the turn
      // open forever, and neither must an unreachable backend.
      if (effectiveSilenceMs >= MAX_SILENCE_MS) {
        fire();
        return;
      }

      const decision = await askTurnBoundary({
        transcript: text,
        silence_ms: effectiveSilenceMs,
        pending_question: awaitingLoginRef.current,
        executing: taskActiveRef.current,
        holds_floor: speakingRef.current,
        recognizer_final: recognizerFinal,
      });
      if (cancelled || processingRef.current || !activeRef.current) return;

      if (!decision) {
        // Backend unreachable. Fall back to the last budget it recommended,
        // which is at least this session's own calibration rather than a
        // constant baked into the bundle.
        if (effectiveSilenceMs >= silenceBudgetRef.current) fire();
        else scheduleIn(silenceBudgetRef.current - effectiveSilenceMs);
        return;
      }
      if (decision.should_finalize) {
        fire();
        return;
      }
      scheduleIn(decision.recheck_in_ms);
    };

    const scheduleIn = (ms: number) => {
      const wait = Math.min(Math.max(ms, MIN_RECHECK_MS), MAX_SILENCE_MS);
      silenceTimerRef.current = window.setTimeout(() => {
        void check();
      }, wait);
    };

    // First look once we are past the detector's hair-trigger floor; asking
    // earlier can only ever get "below minimum silence" back.
    scheduleIn(FIRST_CHECK_MS);
    return () => {
      cancelled = true;
      if (silenceTimerRef.current) clearTimeout(silenceTimerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finalTranscript, transcript, isListening, active]);

  // ── Core: process one voice turn via unified /api/voice/chat SSE ─────────
  const processInput = useCallback(
    async (text: string) => {
      if (!text.trim() || processingRef.current) return;
      processingRef.current = true;
      setLoopState('processing');
      clearTranscript();
      setLiveText('');

      addTurn('user', text);

      // Local stop-command fast path — no server round trip needed
      const lower = text.toLowerCase();
      if (/\b(stop|cancel|abort|halt|quit|never mind)\b/.test(lower)) {
        stopSpeaking();
        setActive(false);
        activeRef.current = false;
        addTurn('ai', 'Okay, stopping. Tap the orb to start again.', { intent: 'CONTROL' });
        processingRef.current = false;
        setLoopState('idle');
        return;
      }

      // Login confirmation across turns
      if (awaitingLogin && /\b(yes|yeah|yep|ok|okay|logged in|sure)\b/.test(lower)) {
        setAwaitingLogin(false);
        if (loginPrompt) {
          await fetch(apiUrl('/api/voice/confirm-login'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ site: loginPrompt.site }),
          });
          addTurn('ai', `Got it! Proceeding with ${loginPrompt.site} now.`);
          setLoginPrompt(null);
        }
        processingRef.current = false;
        restartListening();
        return;
      }

      try {
        // ── Single unified SSE call: NLP + LLM stream in one request ──────────
        // Backend emits: {type:'intent',...} → {type:'chunk',content} → {type:'done',content}
        const res = await fetch(apiUrl('/api/voice/chat'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            transcript: text,
            session_id: sessionIdRef.current,
          }),
        });

        if (!res.ok || !res.body) throw new Error(`Voice chat failed: ${res.status}`);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();

        let fullResponse = '';
        let spokenUpTo = 0;
        let aiTurnId: number | null = null;
        let buffer = '';

        const flushSentence = (accumulated: string) => {
          const rem = accumulated.slice(spokenUpTo);
          const end = rem.search(/[.!?\n]/);
          if (end > 15) {
            const sentence = rem.slice(0, end + 1);
            spokenUpTo += sentence.length;
            speakingRef.current = true;
            void speak(sentence, { voiceGender: 'female', voiceTone: 'friendly', queue: true });
          }
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() ?? '';

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            let event: Record<string, unknown>;
            try {
              event = JSON.parse(line.slice(6));
            } catch {
              continue;
            }

            // ── Intent event: first event from server ──────────────────────
            if (event.type === 'intent') {
              // The NLP layer's own view of how long to wait on the next pause.
              // Only used if `/api/voice/turn-boundary` goes unreachable, but it
              // beats a hardcoded constant as a fallback.
              const recommended = event.recommended_silence_ms;
              if (typeof recommended === 'number' && recommended > 0) {
                silenceBudgetRef.current = Math.min(
                  Math.max(recommended, FIRST_CHECK_MS),
                  MAX_SILENCE_MS
                );
              }

              // Login check: speak prompt and wait for user to confirm
              if (event.requires_login_check && event.target_site) {
                const msg =
                  (event.login_prompt as string) ||
                  `Are you logged into ${event.target_site} on Chrome?`;
                setLoginPrompt({ site: event.target_site as string, msg });
                setAwaitingLogin(true);
                setLoopState('speaking');
                speakingRef.current = true;
                await speak(msg, { voiceGender: 'female', voiceTone: 'friendly' });
                addTurn('ai', msg, { intent: 'LOGIN_CHECK', site: event.target_site as string });
                processingRef.current = false;
                restartListening();
                return;
              }

              // Automation: speak start phrase, then fire automation in background
              if (event.trigger_automation || event.requires_automation) {
                const site = (event.target_site as string) || '';
                const startMsg =
                  (event.emotion_phrase as string) ||
                  `Starting ${site} automation in the background.`;
                setLoopState('speaking');
                speakingRef.current = true;
                await speak(startMsg, { voiceGender: 'female', voiceTone: 'friendly' });
                addTurn('ai', startMsg, { intent: 'TASK', site, isAuto: true });

                setTaskStatus({ active: true, site, step: 'Launching...' });

                // If OpenWork session was created, poll its status
                const owSessionId = event.openwork_session_id as string | undefined;
                if (owSessionId) {
                  const pollOW = setInterval(async () => {
                    try {
                      const owRes = await fetch(apiUrl(`/api/openwork/status/${owSessionId}`));
                      const owData = (await owRes.json()) as Record<string, unknown>;
                      if (owData.found) {
                        setTaskStatus({
                          active: owData.status !== 'completed',
                          site: (owData.site_domain as string) || site,
                          step: owData.requires_login
                            ? `🔐 ${(owData.login_prompt as string) || 'Please log in'}`
                            : `Step ${owData.current_step as number}/${owData.total_steps as number}`,
                        });
                        if (owData.status === 'completed' || owData.status === 'failed') {
                          clearInterval(pollOW);
                        }
                      }
                    } catch {
                      /* silent */
                    }
                  }, 2000);
                  // Auto-clear after 5 minutes
                  setTimeout(() => clearInterval(pollOW), 300_000);
                }

                fetch(apiUrl('/api/automation/browser/prompt'), {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ prompt: text, cowork: true, background: false }),
                })
                  .then((r) => r.json())
                  .then((result) => {
                    const msg =
                      ((result as Record<string, unknown>).message as string) ||
                      `${site} task completed.`;
                    setTaskStatus({ active: false, site: '', step: '' });
                    speakingRef.current = true;
                    void speak(msg, { voiceGender: 'female', voiceTone: 'friendly' });
                    addTurn('ai', msg, { site, isAuto: true });
                  })
                  .catch(() => setTaskStatus({ active: false, site: '', step: '' }));

                processingRef.current = false;
                restartListening();
                return;
              }

              // Show plan summary if the agentic engine built one
              const planSummary = event.plan_summary as string | undefined;
              if (planSummary) {
                const planId = ++turnIdRef.current;
                setTurns((prev) => [
                  ...prev,
                  {
                    id: planId,
                    role: 'ai',
                    text: `Research complete.\n${planSummary}`,
                    at: new Date().toISOString(),
                    intent: 'PLAN',
                  },
                ]);
              }

              // Otherwise: LLM chunks are coming next — prepare AI turn row
              setLoopState('speaking');
              aiTurnId = ++turnIdRef.current;
              setTurns((prev) => [
                ...prev,
                {
                  id: aiTurnId!,
                  role: 'ai',
                  text: '',
                  at: new Date().toISOString(),
                  intent: event.intent_category as string,
                },
              ]);
            }

            // ── Chunk event: accumulate text and stream TTS ─────────────────
            if (event.type === 'chunk') {
              const chunk = (event.content as string) || '';
              if (chunk) {
                fullResponse += chunk;
                if (aiTurnId !== null) {
                  const capturedId = aiTurnId;
                  setTurns((prev) =>
                    prev.map((t) => (t.id === capturedId ? { ...t, text: fullResponse } : t))
                  );
                }
                flushSentence(fullResponse);
              }
            }

            // ── Done event: speak tail, handle blocking questions, stop ─────
            if (event.type === 'done') {
              const blockingQ = event.blocking_question as string | undefined;
              if (blockingQ) {
                const questionText = (event.content as string) || blockingQ;
                if (aiTurnId === null) {
                  addTurn('ai', questionText, { intent: 'RESEARCH_QUESTION' });
                }
                speakingRef.current = true;
                await speak(questionText, { voiceGender: 'female', voiceTone: 'friendly' });
                break;
              }
              const final = (event.content as string) || fullResponse;
              if (!fullResponse && final) {
                fullResponse = final;
                if (aiTurnId !== null) {
                  const capturedId = aiTurnId;
                  setTurns((prev) =>
                    prev.map((t) => (t.id === capturedId ? { ...t, text: fullResponse } : t))
                  );
                }
              }
              const tail = fullResponse.slice(spokenUpTo).trim();
              if (tail) {
                speakingRef.current = true;
                void speak(tail, { voiceGender: 'female', voiceTone: 'friendly', queue: true });
              }
              break;
            }

            // ── Error event ─────────────────────────────────────────────────
            if (event.type === 'error') {
              throw new Error((event.message as string) || 'Server error');
            }
          }
        }
      } catch {
        const errMsg = 'Sorry, something went wrong. Try again.';
        addTurn('ai', errMsg);
        speakingRef.current = true;
        await speak(errMsg, { voiceGender: 'female', voiceTone: 'friendly' });
      } finally {
        processingRef.current = false;

        // Wait for all queued TTS to finish
        await waitForSpeech();

        // Guarded rather than an early `return`. A `return` inside `finally`
        // overrides whatever the try/catch was producing, which lint is right to
        // call unsafe — and nothing below is cleanup in the first place, it is the
        // start of the next turn.
        if (activeRef.current) {
          // Speak "what next?" prompt then restart listening
          const prompt = nextPrompt();
          speakingRef.current = true;
          await speak(prompt, { voiceGender: 'female', voiceTone: 'friendly' });

          await new Promise((r) => setTimeout(r, 800));
          if (activeRef.current) restartListening();
        }
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [
      awaitingLogin,
      loginPrompt,
      addTurn,
      speak,
      stopSpeaking,
      clearTranscript,
      restartListening,
      waitForSpeech,
    ]
  );

  // ── Start / stop ─────────────────────────────────────────────────────────
  /**
   * Open a fresh conversation. Extracted because there are now three ways in --
   * tapping the orb, picking a starter chip, and saying "hey Akansha" -- and all
   * three have to regenerate the session id. The `voice_` prefix is load-bearing
   * (see `sessionIdRef`), so a path that forgets it silently loses the fast path.
   */
  const openSession = useCallback(() => {
    sessionIdRef.current = `voice_${Date.now()}`;
    setActive(true);
    activeRef.current = true;
    setTurns([]);
  }, []);

  /**
   * `useCallback` because `MicControl` is memoised and this page re-renders on
   * every recognition result -- an inline arrow here would re-render the control
   * several times a second while the user is talking, which is exactly when the
   * volume bloom is animating.
   */
  const toggleWakeWord = useCallback(() => {
    setWakeWordEnabled((enabled) => !enabled);
  }, []);

  const toggleImmersive = useCallback(() => {
    setImmersive((on) => !on);
  }, []);

  // Escape leaves full screen. Without this the only way out is a 40px icon in
  // the corner of an otherwise chromeless page, and a mode with no obvious exit
  // is a trap -- the browser's own full-screen affordances are not in play here,
  // since this is a fixed overlay rather than the Fullscreen API.
  useEffect(() => {
    if (!immersive) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setImmersive(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [immersive]);

  const handleToggle = useCallback(() => {
    if (!active) {
      openSession();
      startListening();
    } else {
      stopSpeaking();
      stopListening();
      setActive(false);
      activeRef.current = false;
      processingRef.current = false;
      setLoopState('idle');
      clearTranscript();
      setLiveText('');
    }
  }, [active, openSession, startListening, stopListening, stopSpeaking, clearTranscript]);

  const runStarter = useCallback(
    (phrase: string) => {
      openSession();
      void processInput(phrase);
    },
    [openSession, processInput]
  );

  // ── Wake word: "hey Akansha" ───────────────────────────────────────────────
  // Keep the recogniser alive while idle. `backgroundListening` is what makes
  // `useVoice` restart recognition after every `onend`, and without it the mic
  // closes after the first utterance -- so a wake word would work exactly once.
  useEffect(() => {
    if (!wakeWordEnabled) return;
    setBackgroundListening(true);
    startListening();
    return () => setBackgroundListening(false);
  }, [wakeWordEnabled, setBackgroundListening, startListening]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    setWakeWordEnabled(window.localStorage.getItem('akansha_wake_word') === 'on');
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem('akansha_wake_word', wakeWordEnabled ? 'on' : 'off');
  }, [wakeWordEnabled]);

  /**
   * Listen for the wake phrase while idle, and hand the rest of the sentence
   * straight to the turn pipeline.
   *
   * Reads the interim transcript as well as the final one on purpose: waiting
   * for `isFinal` costs the better part of a second, and a wake word that lags
   * behind the user feels broken even when it eventually fires. The wake phrase
   * is a fixed target, so an interim hypothesis is enough evidence for it --
   * unlike a command, where acting on interim text would act on half a sentence.
   *
   * Guarded on `!active` so this cannot fire mid-conversation: once a session is
   * open the silence detector owns the transcript, and a second consumer would
   * submit the same words twice.
   */
  useEffect(() => {
    if (!wakeWordEnabled || active || processingRef.current) return;
    const heard = `${finalTranscript} ${transcript}`.trim();
    if (!heard) return;

    const match = matchWakeWord(heard);
    if (!match.matched) return;

    const bare = isBareWakeCall(heard);
    openSession();
    clearTranscript();
    setLiveText('');

    if (bare) {
      // Called and nothing more. Answer, then hand the floor straight back --
      // the alternative is executing an empty command, which reads as the
      // assistant ignoring its own name.
      void speak('Yes?', { queue: false });
      startListening();
      return;
    }

    void processInput(match.remainder);
  }, [
    wakeWordEnabled,
    active,
    finalTranscript,
    transcript,
    openSession,
    clearTranscript,
    speak,
    startListening,
    processInput,
  ]);

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <AppLayout activePath="/voice-assistant">
      {/* `h-full`, not `min-h-full`. AppLayout's <main> is a 692px scroll box, and
          a *minimum* height leaves this column flex sized by its content — so
          `flex-1 min-h-0` below had no definite height to shrink against, the row
          took its full content height, and the transcript rail was pushed off the
          bottom of the viewport entirely. A definite height is what makes the
          shrink instruction mean anything. */}
      <div
        className="relative flex h-full flex-col overflow-hidden bg-neutral-950"
        // The panel view stays mounted under the full-screen overlay rather than
        // being swapped out, because unmounting `NeuralPresence` would close the
        // MuseTalk socket and drop frames mid-sentence. `inert` is what makes
        // that safe: covered by an opaque overlay it is invisible, but without
        // this its buttons would still be tabbable and its live regions would
        // still be read out from behind the thing on screen.
        inert={immersive}
      >
        {/* Ambient wash. Two offset radial gradients rather than the 60px cyan
            grid that used to sit here: a technical grid behind a photograph of a
            person makes the person look like a readout. */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              'radial-gradient(ellipse 80% 60% at 50% -10%, oklch(0.4 0.09 285 / 0.28), transparent 70%), radial-gradient(ellipse 60% 50% at 85% 100%, oklch(0.42 0.07 230 / 0.2), transparent 70%)',
          }}
        />

        <VoiceHeader
          loopState={loopState}
          active={active}
          voiceLanguage={voiceLanguage}
          onLanguageChange={(key: VoiceLanguageKey) => setVoiceLanguage(key)}
          showTranscript={showHistory}
          onToggleTranscript={() => setShowHistory((value) => !value)}
          mounted={mounted}
          reducedMotion={reducedMotion}
        />

        {/* Stacks until `xl`, not `lg`. AppLayout's sidebar takes ~16rem out of the
            viewport before this component sees any of it, so at a 1024px window
            the "side-by-side" branch was really 768px of content trying to hold a
            286px portrait and a 22rem rail. `xl` is the first width where both
            actually fit. */}
        <div className="relative z-10 flex min-h-0 flex-1 flex-col xl:flex-row">
          {/* No `justify-center`. In a scroll container, `justify-content: center`
              overflows in *both* directions and the top half becomes unreachable —
              scrollTop cannot go negative, so on a short screen her face would be
              cut off with no way to scroll up to it. The stage below is `flex-1`
              instead: it absorbs the free space, which centres her when there is
              room and shrinks her when there is not. */}
          <main className="flex min-h-0 min-w-0 flex-1 flex-col items-center gap-5 overflow-y-auto px-4 py-6 sm:px-8">
            <div
              ref={stageRef}
              className="relative flex min-h-[8rem] w-full min-w-0 flex-1 items-center justify-center"
            >
              {/* Sized to her, not to the stage: `StagePresence` works in
                  percentages, so its halo has to share a box with the portrait or
                  it would bloom to the full width of the page. */}
              <div className="relative" style={{ width: presenceWidth }}>
                <StagePresence
                  loopState={loopState}
                  volume={speakingVolume ?? 0}
                  reducedMotion={reducedMotion}
                />
                {/* Not a button. Clicking her face to toggle the microphone gave a
                    286×429 accidental-hit target and made the control's accessible
                    name the alt text of a photograph. `MicControl` is the control. */}
                <div className="relative z-10 overflow-hidden rounded-[2rem]">
                  <NeuralPresence
                    ref={presenceRef}
                    sessionId={avatarSessionId}
                    isListening={loopState === 'listening'}
                    isSpeaking={loopState === 'speaking'}
                    isThinking={loopState === 'processing'}
                    speakingVolume={speakingVolume ?? 0}
                    width={presenceWidth}
                  />
                </div>
              </div>
            </div>

            <MicControl
              loopState={loopState}
              active={active}
              volume={speakingVolume ?? 0}
              onToggle={handleToggle}
              reducedMotion={reducedMotion}
              wakeWordEnabled={wakeWordEnabled}
              onToggleWakeWord={toggleWakeWord}
            />

            {/* Into full screen. Placed with the mic rather than in the header
                because it is part of the conversation, not part of the page
                furniture: it is the difference between reading about her and
                talking to her. */}
            <button
              type="button"
              onClick={toggleImmersive}
              className="flex items-center gap-2 rounded-full border border-white/[0.08] bg-white/[0.03] px-3.5 py-2 text-[0.8125rem] text-neutral-400 transition-colors duration-150 hover:border-white/20 hover:bg-white/[0.08] hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
              style={{ touchAction: 'manipulation' }}
            >
              <svg
                viewBox="0 0 24 24"
                className="size-4"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15" />
              </svg>
              Full screen
            </button>

            {/* Live transcript. Fixed min-height so the layout does not jump on
                every partial result — the box is there from the moment she starts
                listening, and only its text changes. */}
            {active && (
              <div
                className={`w-full max-w-xl rounded-2xl border border-white/[0.07] bg-white/[0.03] px-5 py-4 text-center backdrop-blur-sm ${
                  reducedMotion ? '' : 'animate-rise'
                }`}
              >
                <p className="min-h-[2.75rem] break-words text-[0.9375rem] leading-relaxed text-neutral-100 text-pretty">
                  {liveText || <span className="text-neutral-500">Say something…</span>}
                  {liveText && (
                    <span
                      aria-hidden="true"
                      className={`ml-1 inline-block h-[1.1em] w-px translate-y-[0.15em] bg-current align-middle ${
                        reducedMotion ? '' : 'animate-pulse'
                      }`}
                    />
                  )}
                </p>
              </div>
            )}

            {/* Cold-start affordance only. Once there is a conversation the user
                has clearly worked out how to talk to her, and four suggestion
                chips are then just 130px taken away from her face on a stacked
                layout. */}
            {!active && turns.length === 0 && (
              <div className="w-full max-w-xl">
                <p className="mb-3 text-center text-xs text-neutral-500">Or try one of these</p>
                <div className="flex flex-wrap justify-center gap-2">
                  {STARTERS.map((phrase) => (
                    <button
                      key={phrase}
                      type="button"
                      onClick={() => runStarter(phrase)}
                      className="rounded-full border border-white/[0.08] bg-white/[0.03] px-3.5 py-2 text-[0.8125rem] text-neutral-300 transition-colors duration-150 hover:border-white/20 hover:bg-white/[0.08] hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
                      style={{ touchAction: 'manipulation' }}
                    >
                      {phrase}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {loginPrompt && (
              <div
                role="alertdialog"
                aria-label="Sign-in check"
                className="w-full max-w-xl rounded-2xl border border-sky-400/25 bg-sky-500/[0.08] px-5 py-4 backdrop-blur-sm"
              >
                <p className="text-sm leading-relaxed text-sky-100 text-pretty">
                  {loginPrompt.msg}
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={async () => {
                      await fetch(apiUrl('/api/voice/confirm-login'), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ site: loginPrompt.site }),
                      });
                      setAwaitingLogin(false);
                      setLoginPrompt(null);
                      restartListening();
                    }}
                    className="rounded-lg bg-sky-400 px-3.5 py-1.5 text-[0.8125rem] font-medium text-sky-950 transition-colors hover:bg-sky-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
                  >
                    Yes, I&rsquo;m Signed In
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setLoginPrompt(null);
                      setAwaitingLogin(false);
                      restartListening();
                    }}
                    className="rounded-lg border border-white/15 px-3.5 py-1.5 text-[0.8125rem] text-neutral-300 transition-colors hover:bg-white/[0.06] hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
                  >
                    Not Yet
                  </button>
                </div>
              </div>
            )}
          </main>

          {showHistory && (
            <TranscriptRail
              turns={turns}
              onClear={() => setTurns([])}
              reducedMotion={reducedMotion}
            />
          )}
        </div>

        {/* Task status. `polite` because it fires while she may be mid-sentence,
            and cutting a screen reader off to announce "Step 2/5" loses both. */}
        {taskStatus.active && (
          <div
            role="status"
            aria-live="polite"
            className={`fixed bottom-6 right-6 z-50 flex max-w-[min(20rem,calc(100vw-3rem))] items-center gap-3 rounded-2xl border border-amber-300/20 bg-neutral-900/90 px-4 py-3 shadow-2xl backdrop-blur-xl ${
              reducedMotion ? '' : 'animate-rise'
            }`}
            style={{ paddingBottom: 'max(0.75rem, env(safe-area-inset-bottom))' }}
          >
            <span
              aria-hidden="true"
              className={`size-2 shrink-0 rounded-full bg-amber-300 ${
                reducedMotion ? '' : 'animate-pulse'
              }`}
            />
            <div className="min-w-0">
              <p className="truncate text-[0.8125rem] font-medium text-amber-100" translate="no">
                {taskStatus.site || 'Working'}
              </p>
              <p className="truncate text-xs text-neutral-400">{taskStatus.step}</p>
            </div>
          </div>
        )}
      </div>

      {/* ── Full screen ────────────────────────────────────────────────────────
          A fixed sibling rather than a route or a replacement for the tree above.
          A route would remount `NeuralPresence` and tear down its socket; a
          conditional swap would do the same. This covers instead of replacing,
          which is also why it is opaque rather than translucent.

          Safe to place inside `<main>`: `position: fixed` escapes an
          `overflow-hidden` ancestor, and AppLayout's only transform is on the
          sidebar, not on any ancestor of this — a transformed ancestor would
          make `inset-0` relative to it instead of to the viewport. */}
      {immersive && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Akansha, full screen"
          className="fixed inset-0 z-[100] bg-black"
        >
          <BodyPresence
            isListening={loopState === 'listening'}
            isSpeaking={loopState === 'speaking'}
            isThinking={loopState === 'processing'}
            speakingVolume={speakingVolume ?? 0}
          />

          {/* Out. Icon-only and in the corner, which is only acceptable because
              Escape does the same thing — a chromeless mode whose single exit is
              a 36px target is a trap. */}
          <button
            type="button"
            onClick={toggleImmersive}
            aria-label="Leave full screen"
            title="Leave full screen (Esc)"
            className="absolute right-5 top-5 grid size-10 place-items-center rounded-full border border-white/10 bg-black/40 text-neutral-400 backdrop-blur-sm transition-colors hover:border-white/25 hover:bg-black/60 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
            style={{ touchAction: 'manipulation' }}
          >
            <svg
              viewBox="0 0 24 24"
              className="size-4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M9 4H5.5A1.5 1.5 0 0 0 4 5.5V9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15" />
              <path d="m9 9 6 6M15 9l-6 6" opacity="0.55" />
            </svg>
          </button>

          {/* Controls float over her. Nothing else does — the header, the rail,
              the starter chips and the state caption are all left behind, which
              was the point of the mode. */}
          <div
            className="absolute inset-x-0 bottom-0 flex flex-col items-center gap-4 px-6 pb-10"
            style={{ paddingBottom: 'max(2.5rem, calc(env(safe-area-inset-bottom) + 1.5rem))' }}
          >
            {/* Subtitles, and only when there is something to show. The one piece
                of text that earns its place here: without it there is no way to
                tell "misheard you" from "did not hear you", and both feel like
                the same failure. Styled as film subtitles rather than as a panel
                so it reads as part of the picture. */}
            {active && liveText && (
              <p className="max-w-3xl text-center text-[1.0625rem] leading-relaxed text-white/90 text-pretty [text-shadow:0_2px_12px_rgba(0,0,0,0.9)]">
                {liveText}
              </p>
            )}

            <MicControl
              loopState={loopState}
              active={active}
              volume={speakingVolume ?? 0}
              onToggle={handleToggle}
              reducedMotion={reducedMotion}
              wakeWordEnabled={wakeWordEnabled}
              onToggleWakeWord={toggleWakeWord}
              minimal
            />
          </div>
        </div>
      )}
    </AppLayout>
  );
}
