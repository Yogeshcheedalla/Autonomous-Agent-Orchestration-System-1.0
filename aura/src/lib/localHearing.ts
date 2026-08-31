/**
 * The browser's recogniser, when the browser will not do it.
 *
 * `useVoice` listens through `webkitSpeechRecognition`, which is the right
 * default: it streams interim words in ~300 ms and costs us nothing. But it is
 * Chrome-only, it is a round trip to Google's speech service, and it refuses --
 * `network`, `service-not-allowed` -- in exactly the situations where a voice
 * assistant is most expected to still work. When that happens the whole
 * assistant goes deaf.
 *
 * This module is the second ear: capture the microphone here, hand the audio to
 * `/api/voice/stt`, and get back faster-whisper's transcript plus Silero's
 * measurement of where speech actually stopped. Slower than the browser -- about
 * 1.2 s for a 3 s clip on this machine, against Chrome's ~300 ms of interim
 * words -- so it is a fallback and a second opinion, never the default.
 *
 * Three rules, matching `backend/voice_local.py`:
 *   - Availability is probed, never assumed. A backend without the models
 *     installed says so, and the caller stays on the browser path.
 *   - The microphone is borrowed, not taken. `useVoice` already holds a
 *     `MediaStream` for its waveform analyser; opening a second one would ask
 *     the user for permission twice and cost another device open.
 *   - Nothing here throws at the caller. Every failure is a `null` result with a
 *     reason attached, because a deaf fallback must not also crash the path that
 *     was working.
 */

import { apiUrl } from '@/lib/apiBase';

/** How long a single fallback capture may run before it is cut off. Whisper's
 *  cost is linear in audio duration, so an open-ended recording is an
 *  open-ended wait; 15 s is longer than any spoken instruction we have seen. */
const MAX_CAPTURE_MS = 15_000;

/** Below this there is nothing to transcribe -- a click, a door, a breath. The
 *  round trip would cost more than the silence is worth. */
const MIN_CAPTURE_BYTES = 2_000;

/** Codec preference. Opus in WebM is what Chrome and Firefox both produce, and
 *  PyAV on the backend decodes it directly. The bare fallbacks are for Safari,
 *  which offers only MP4/AAC. */
const CANDIDATE_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
];

export interface LocalCapability {
  name: string;
  installed: boolean;
  ready: boolean;
  detail: string;
  model: string;
}

export interface LocalCapabilityReport {
  /** Transcription is available: faster-whisper installed and a model present. */
  hearing: boolean;
  /** Waveform turn-taking is available: Silero's ONNX graph is reachable. */
  turnTaking: boolean;
  /** Local speech synthesis is available. Reported for completeness; TTS is
   *  chosen server-side by `/api/voice/tts`, not here. */
  speech: boolean;
  capabilities: LocalCapability[];
  /** Human-readable reason when something is unavailable, for the UI to show
   *  instead of a silent no-op. Empty when everything is usable. */
  detail: string;
}

/** Silero's reading of the clip, as measured on the waveform rather than
 *  inferred from when the recogniser last emitted a word. */
export interface LocalVoiceActivity {
  speaking: boolean;
  speech_ms: number;
  trailing_silence_ms: number;
  leading_silence_ms: number;
  duration_ms: number;
  engine: string;
}

export interface LocalTranscript {
  text: string;
  language: string;
  language_confidence: number;
  latency_ms: number;
  engine: string;
  model: string;
  duration_s: number;
}

export interface LocalHearingResult {
  transcript: LocalTranscript;
  /** `null` when Silero was unavailable. Not the same as "heard nothing": the
   *  backend deliberately distinguishes "I could not listen" from "I listened
   *  and there was silence", and so must anything reading this. */
  voiceActivity: LocalVoiceActivity | null;
  /** Weak-but-real voiceprint comparison against the enrolled owner. Supporting
   *  evidence for `owner_verify`, never a verdict on its own. */
  speaker: Record<string, unknown> | null;
}

const EMPTY_REPORT: LocalCapabilityReport = {
  hearing: false,
  turnTaking: false,
  speech: false,
  capabilities: [],
  detail: 'Local voice backend unreachable.',
};

let probeInFlight: Promise<LocalCapabilityReport> | null = null;
let cachedReport: LocalCapabilityReport | null = null;

function firstUsableMimeType(): string {
  if (typeof MediaRecorder === 'undefined') return '';
  for (const candidate of CANDIDATE_MIME_TYPES) {
    try {
      if (MediaRecorder.isTypeSupported(candidate)) return candidate;
    } catch {
      // Older implementations throw instead of returning false.
    }
  }
  return '';
}

/** True when this browser can capture audio for us at all. Checked before the
 *  network probe, because a Safari without `MediaRecorder` cannot use the
 *  fallback no matter how well the backend is provisioned. */
export function localCaptureSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof MediaRecorder !== 'undefined' &&
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia
  );
}

/**
 * Ask the backend which local voice models are actually installed.
 *
 * Cached after the first success and de-duplicated while in flight: this is
 * called from a mount effect and from the recogniser's error handler, and two
 * copies of a capability probe would race to the same answer.
 */
export async function probeLocalHearing({
  refresh = false,
}: { refresh?: boolean } = {}): Promise<LocalCapabilityReport> {
  if (cachedReport && !refresh) return cachedReport;
  if (probeInFlight && !refresh) return probeInFlight;

  probeInFlight = (async () => {
    try {
      const res = await fetch(apiUrl('/api/voice/local/capabilities'));
      if (!res.ok) return EMPTY_REPORT;
      const data = (await res.json()) as { capabilities?: LocalCapability[] };
      const capabilities = Array.isArray(data.capabilities) ? data.capabilities : [];
      const find = (name: string) => capabilities.find((cap) => cap.name === name);
      const hearing = find('hearing');
      const turnTaking = find('turn_taking');
      const speech = find('speech');
      const usable = (cap?: LocalCapability) => !!cap?.installed && !!cap?.ready;
      const report: LocalCapabilityReport = {
        hearing: usable(hearing),
        turnTaking: usable(turnTaking),
        speech: usable(speech),
        capabilities,
        // Carry the backend's own fix instructions through to the UI. It already
        // names the exact `pip install` or model download that would fix it, and
        // rewriting that here would let the two drift.
        detail: usable(hearing) ? '' : hearing?.detail || EMPTY_REPORT.detail,
      };
      cachedReport = report;
      return report;
    } catch {
      return EMPTY_REPORT;
    } finally {
      probeInFlight = null;
    }
  })();

  return probeInFlight;
}

/**
 * Ask the backend to load the models now.
 *
 * Fire-and-forget on purpose. The server already warms them in a startup thread,
 * so this only matters when the frontend outlives a backend restart -- and in
 * that case the first fallback turn would otherwise pay ~16 s of one-time model
 * construction while the user waited for an answer.
 */
export function warmLocalHearing(): void {
  void fetch(apiUrl('/api/voice/local/warm?hearing=true&speech=false'), {
    method: 'POST',
  }).catch(() => {
    // Warming is an optimisation. Failing to warm must not surface as an error.
  });
}

/**
 * Record one utterance from an already-open microphone stream.
 *
 * Returns the captured audio, or `null` if the recorder produced nothing worth
 * sending. `stop` is what the caller uses to end the turn -- this module does not
 * decide when speech is over; `useVoice`'s waveform monitor does, and Silero
 * confirms it after the fact.
 */
export class LocalCapture {
  private recorder: MediaRecorder | null = null;
  private chunks: Blob[] = [];
  private settled = false;
  private timer: number | null = null;
  private readonly done: Promise<Blob | null>;
  private resolveDone: (value: Blob | null) => void = () => {};

  constructor(stream: MediaStream) {
    this.done = new Promise<Blob | null>((resolve) => {
      this.resolveDone = resolve;
    });

    const mimeType = firstUsableMimeType();
    try {
      this.recorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
    } catch {
      this.finish(null);
      return;
    }

    this.recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) this.chunks.push(event.data);
    };
    this.recorder.onerror = () => this.finish(null);
    this.recorder.onstop = () => {
      const type = this.recorder?.mimeType || mimeType || 'audio/webm';
      const blob = new Blob(this.chunks, { type });
      this.finish(blob.size >= MIN_CAPTURE_BYTES ? blob : null);
    };

    try {
      // One blob per second rather than one at the end: a recorder that is
      // stopped before its first `ondataavailable` fires yields an empty
      // capture, and a hesitant speaker can easily be quiet that long.
      this.recorder.start(1_000);
    } catch {
      this.finish(null);
      return;
    }

    this.timer = window.setTimeout(() => this.stop(), MAX_CAPTURE_MS);
  }

  private finish(blob: Blob | null) {
    if (this.settled) return;
    this.settled = true;
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
    this.resolveDone(blob);
  }

  /** End the capture. Safe to call twice, and safe to call on a recorder that
   *  never started -- both resolve the same promise once. */
  stop(): Promise<Blob | null> {
    if (this.recorder && this.recorder.state !== 'inactive') {
      try {
        this.recorder.stop();
      } catch {
        this.finish(null);
      }
    } else {
      this.finish(null);
    }
    return this.done;
  }

  /** Throw the capture away without transcribing it. Used when the browser
   *  recogniser recovers mid-turn and the fallback is no longer needed. */
  abort(): void {
    try {
      if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    } catch {
      // Already stopped.
    }
    this.chunks = [];
    this.finish(null);
  }

  get audio(): Promise<Blob | null> {
    return this.done;
  }
}

/**
 * Send captured audio to faster-whisper and return what it heard.
 *
 * `hint` is not decoration. On this machine the `base` model without a hint
 * produced "Open what's happened message ammo", and the same model with the
 * speaker names from the database as its `initial_prompt` produced "Open
 * WhatsApp and message Amma that I will be late tonight" -- in less time than
 * the `small` model took to get it wrong. The backend builds that hint from its
 * own tables; the caller may add to it.
 */
export async function transcribeLocally(
  audio: Blob,
  { language, hint, signal }: { language?: string; hint?: string; signal?: AbortSignal } = {}
): Promise<LocalHearingResult | null> {
  if (!audio || audio.size < MIN_CAPTURE_BYTES) return null;

  const form = new FormData();
  // A filename with a real extension: the backend hands the bytes to PyAV,
  // which is happier when the container is not a mystery.
  form.append('audio', audio, audio.type.includes('mp4') ? 'turn.mp4' : 'turn.webm');
  if (language) form.append('language', language);
  if (hint) form.append('hint', hint);

  try {
    const res = await fetch(apiUrl('/api/voice/stt'), { method: 'POST', body: form, signal });
    if (!res.ok) {
      // 503 means the models are not installed after all -- the capability probe
      // was stale. Drop the cache so the next probe re-reads the truth instead
      // of the frontend confidently retrying a path that cannot work.
      if (res.status === 503) cachedReport = null;
      return null;
    }
    const data = (await res.json()) as {
      transcript?: LocalTranscript;
      voice_activity?: LocalVoiceActivity | null;
      speaker?: Record<string, unknown> | null;
    };
    if (!data.transcript || typeof data.transcript.text !== 'string') return null;
    return {
      transcript: data.transcript,
      voiceActivity: data.voice_activity ?? null,
      speaker: data.speaker ?? null,
    };
  } catch {
    return null;
  }
}

/** Convenience for the common case: record until `stop()`, then transcribe. */
export async function captureAndTranscribe(
  stream: MediaStream,
  options: { language?: string; hint?: string } = {}
): Promise<{ capture: LocalCapture; result: Promise<LocalHearingResult | null> }> {
  const capture = new LocalCapture(stream);
  const result = capture.audio.then((blob) =>
    blob ? transcribeLocally(blob, options) : Promise.resolve(null)
  );
  return { capture, result };
}

/** Test seam: forget the cached capability probe. */
export function resetLocalHearingCache(): void {
  cachedReport = null;
  probeInFlight = null;
}
