'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  claimAkanshaAudio,
  hardCancelBrowserSpeech,
  releaseAkanshaAudio,
  settleBrowserSpeechCancel,
} from '@/lib/audioPlaybackGuard';
import { apiUrl } from '@/lib/apiBase';
import {
  LocalCapture,
  localCaptureSupported,
  probeLocalHearing,
  transcribeLocally,
  warmLocalHearing,
  type LocalCapabilityReport,
  type LocalVoiceActivity,
} from '@/lib/localHearing';

export type VoiceGender = 'male' | 'female';
export type VoiceTone = 'friendly' | 'professional' | 'energetic' | 'calm';
export type VoiceLanguagePreference = 'telugu_english' | 'english' | 'hindi';

export interface VoiceFrequencySignature {
  averageFrequencyHz: number;
  spectralCentroidHz: number;
  lowBandEnergy: number;
  midBandEnergy: number;
  highBandEnergy: number;
  rmsLevel: number;
  sampleCount: number;
  capturedAt: string;
}

interface VoiceOption {
  id: string;
  name: string;
  lang: string;
  gender: VoiceGender;
  kind?: 'system' | 'sample';
}

interface SpeakOptions {
  voiceGender?: VoiceGender;
  voiceTone?: VoiceTone;
  voiceLanguage?: VoiceLanguagePreference;
}
interface QueueSpeakOptions extends SpeakOptions {
  queue?: boolean;
  preferBrowser?: boolean;
}

interface QueuedSpeechItem {
  text: string;
  options?: SpeakOptions;
  preparedAudio?: Promise<Blob | null>;
}

type VoiceLanguageMode = 'english' | 'telugu' | 'mixed' | 'hindi';
type VisemeCode = 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;

interface VisemeFrame {
  at: number;
  viseme: VisemeCode;
  intensity: number;
}

interface VoiceState {
  isListening: boolean;
  isSpeaking: boolean;
  transcript: string;
  finalTranscript: string;
  voiceNotice: string;
  voiceNoticeId: number;
  speakingVolume: number;
  viseme: number;
  /** Which ear produced the current transcript. `browser` is Chrome's streaming
   *  recogniser; `local` is faster-whisper via `/api/voice/stt`, used when the
   *  browser has no recogniser or its own has started refusing. Surfaced because
   *  the two have very different latencies and the UI should be able to say so
   *  rather than appear to have stalled. */
  hearingSource: 'browser' | 'local';
  /** True while a local capture is being transcribed. There are no interim
   *  results on this path — the whole utterance resolves at once. */
  localHearingBusy: boolean;
}

const TONE_CONFIG: Record<VoiceTone, { rate: number; pitch: number; volume: number }> = {
  friendly: { rate: 1, pitch: 1.05, volume: 0.95 },
  professional: { rate: 0.98, pitch: 0.96, volume: 0.92 },
  energetic: { rate: 1.08, pitch: 1.12, volume: 1 },
  calm: { rate: 0.9, pitch: 0.94, volume: 0.88 },
};

/**
 * Whether to speak this chunk with the browser's own voice instead of hers.
 *
 * Only when explicitly asked, which no caller currently does — so in practice
 * this is always false and every chunk gets the server voice.
 *
 * It used to also return true for any queued English chunk of 180 characters or
 * fewer, as a latency shortcut. That is what made two different voices audible
 * in one conversation: a short sentence came out in the system voice and a long
 * one in hers, so her identity changed between chunks of the same reply. Two
 * voices is a worse defect than a slower first word, and it was not even a real
 * trade — `speak()` already prefetches each chunk's audio at enqueue time
 * (`preparedAudio`), so by the time the queue reaches a chunk its blob is
 * usually in hand. The old rule additionally *suppressed* that prefetch for
 * exactly the short chunks it was meant to speed up.
 *
 * The browser voice remains the fallback when the server path fails, which is
 * the one case where a different voice beats silence.
 */
function shouldUseFastBrowserSpeech(text: string, options?: QueueSpeakOptions | SpeakOptions) {
  if (!options || !('queue' in options)) {
    return false;
  }

  return Boolean(options.queue && 'preferBrowser' in options && options.preferBrowser);
}

function inferGender(name: string): VoiceGender {
  const normalized = name.toLowerCase();
  if (
    [
      'female',
      'samantha',
      'victoria',
      'karen',
      'zira',
      'aria',
      'sara',
      'jenny',
      'ava',
      'nancy',
      'lisa',
      'hazel',
      'heera',
      'priya',
    ].some((token) => normalized.includes(token))
  ) {
    return 'female';
  }

  if (
    [
      'male',
      'david',
      'mark',
      'daniel',
      'alex',
      'george',
      'james',
      'guy',
      'ryan',
      'adam',
      'aaron',
      'leo',
      'rohan',
      'rahul',
    ].some((token) => normalized.includes(token))
  ) {
    return 'male';
  }

  return 'male';
}

const FEMALE_SAMPLE_VOICE_ID = 'sample-irina-energetic';
const FEMALE_SAMPLE_PATH = '/audio/irina-energetic.mp3';
const FEMALE_SAMPLE_OPTION: VoiceOption = {
  id: FEMALE_SAMPLE_VOICE_ID,
  name: 'Irina energetic e-commerce girl (sample)',
  lang: 'en-US',
  gender: 'female',
  kind: 'sample',
};
const SPEECH_INTERRUPT_PATTERN =
  /\b(stop|wait|pause|hold on|enough|silent|mute|aagu|aapu|ruko|ruk jao|bas)\b|ఆపు|ఆగు|రుకో|रुको|बस/i;

const INTERRUPTION_RESTART_DELAY_MS = 80;
const LISTENING_KEEPALIVE_MS = 650;
const BARGE_IN_RMS_THRESHOLD = 0.026;
const SOFT_SPEECH_RMS_THRESHOLD = 0.014;
const BARGE_IN_HOLD_MS = 120;
const SILENCE_RESTART_MS = 1400;
// How many times to retry a `recognition.start()` the browser refused before
// giving up. `start()` throws `InvalidStateError` while the previous session is
// still releasing, and a refused start produces no session — so no `onend` or
// `onerror` follows to try again. Without a retry here the restart chain dies on
// the first refusal and continuous listening stops for good.
const RECOGNITION_START_RETRIES = 6;
// Consecutive misses before trying the next recognition language. Chrome emits
// `no-speech` for ordinary silence, so a single event is no evidence at all that
// the language is wrong; only a streak is.
const RECOGNITION_LANGUAGE_ROTATE_AFTER = 3;
// Ceiling on the restart backoff. A flat retry re-hit Chrome's remote speech
// service about four times a second for as long as the network stayed down.
const RECOGNITION_RESTART_MAX_MS = 5000;
// Consecutive `network` / `service-not-allowed` errors before handing the turn
// to local Whisper. One is noise -- Chrome's speech service drops a request now
// and then and the next start succeeds. Two in a row, with no successful result
// between them, is the recogniser being unavailable rather than unlucky, and
// that is the case this whole fallback exists for.
const RECOGNITION_FAILURES_BEFORE_LOCAL = 2;

const HINDI_ROMAN_HINTS = new Set([
  'namaste',
  'hindi',
  'kaise',
  'kaisa',
  'kaisi',
  'kya',
  'kyun',
  'kab',
  'kahan',
  'kaun',
  'mujhe',
  'mere',
  'mera',
  'meri',
  'tum',
  'aap',
  'hai',
  'hain',
  'ho',
  'nahi',
  'nahin',
  'batao',
  'batana',
  'samjhao',
  'chalo',
  'ruk',
  'ruko',
  'theek',
  'thik',
  'bas',
  'acha',
  'accha',
  'aaj',
  'kal',
  'karna',
  'karo',
  'chahiye',
  'yaar',
  'bhai',
  'chal',
  'raha',
  'rahe',
  'lagta',
  'lagi',
]);

const TELUGU_ROMAN_HINTS = new Set([
  'telugu',
  'anna',
  'ayya',
  'andi',
  'ra',
  'randi',
  'ledu',
  'kadu',
  'naku',
  'naaku',
  'neeku',
  'meeru',
  'nuvvu',
  'emi',
  'em',
  'enti',
  'ela',
  'unnav',
  'unnaru',
  'cheppu',
  'cheptha',
  'cheppandi',
  'chesa',
  'chesadu',
  'chesindi',
  'chudu',
  'choopu',
  'matladu',
  'matladandi',
  'bagundi',
  'sare',
  'inka',
  'ippudu',
  'eppudu',
  'enduku',
  'ekkada',
  'lo',
  'ki',
  'ga',
  'ante',
  'aithe',
  'kani',
  'undi',
  'ravatledu',
  'avvali',
  'chestunnav',
  'jarigindi',
  'vellali',
]);

function recognitionLanguagesFor(preference: VoiceLanguagePreference) {
  if (preference === 'hindi') return ['hi-IN', 'en-IN'];
  if (preference === 'telugu_english') return ['te-IN', 'en-IN', 'hi-IN'];
  return ['en-IN', 'en-US'];
}

function preferredRecognitionLanguage(preference: VoiceLanguagePreference, index = 0) {
  const languages = recognitionLanguagesFor(preference);
  return languages[index % languages.length] ?? languages[0] ?? 'en-IN';
}

const TELUGU_VISEMES: Record<string, VisemeCode> = {
  '\u0C05': 1,
  '\u0C06': 1,
  '\u0C3E': 1,
  '\u0C07': 2,
  '\u0C08': 2,
  '\u0C0E': 2,
  '\u0C0F': 2,
  '\u0C10': 2,
  '\u0C3F': 2,
  '\u0C40': 2,
  '\u0C46': 2,
  '\u0C47': 2,
  '\u0C48': 2,
  '\u0C09': 3,
  '\u0C0A': 3,
  '\u0C12': 3,
  '\u0C13': 3,
  '\u0C14': 3,
  '\u0C41': 3,
  '\u0C42': 3,
  '\u0C4A': 3,
  '\u0C4B': 3,
  '\u0C4C': 3,
  '\u0C2A': 4,
  '\u0C2B': 4,
  '\u0C2C': 4,
  '\u0C2D': 4,
  '\u0C2E': 4,
  '\u0C35': 5,
  '\u0C24': 6,
  '\u0C25': 6,
  '\u0C26': 6,
  '\u0C27': 6,
  '\u0C1F': 6,
  '\u0C20': 6,
  '\u0C21': 6,
  '\u0C22': 6,
  '\u0C28': 6,
  '\u0C23': 6,
  '\u0C32': 6,
  '\u0C33': 6,
  '\u0C30': 6,
  '\u0C31': 6,
  '\u0C38': 6,
  '\u0C15': 7,
  '\u0C16': 7,
  '\u0C17': 7,
  '\u0C18': 7,
  '\u0C1A': 7,
  '\u0C1B': 7,
  '\u0C1C': 7,
  '\u0C1D': 7,
  '\u0C2F': 7,
  '\u0C36': 7,
  '\u0C37': 7,
  '\u0C39': 7,
};

const HINDI_VISEMES: Record<string, VisemeCode> = {
  '\u0905': 1,
  '\u0906': 1,
  '\u093E': 1,
  '\u0907': 2,
  '\u0908': 2,
  '\u090F': 2,
  '\u0910': 2,
  '\u093F': 2,
  '\u0940': 2,
  '\u0947': 2,
  '\u0948': 2,
  '\u0909': 3,
  '\u090A': 3,
  '\u0913': 3,
  '\u0914': 3,
  '\u0941': 3,
  '\u0942': 3,
  '\u094B': 3,
  '\u094C': 3,
  '\u092A': 4,
  '\u092B': 4,
  '\u092C': 4,
  '\u092D': 4,
  '\u092E': 4,
  '\u0935': 5,
  '\u0924': 6,
  '\u0925': 6,
  '\u0926': 6,
  '\u0927': 6,
  '\u091F': 6,
  '\u0920': 6,
  '\u0921': 6,
  '\u0922': 6,
  '\u0928': 6,
  '\u0923': 6,
  '\u0932': 6,
  '\u0930': 6,
  '\u0938': 6,
  '\u0915': 7,
  '\u0916': 7,
  '\u0917': 7,
  '\u0918': 7,
  '\u091A': 7,
  '\u091B': 7,
  '\u091C': 7,
  '\u091D': 7,
  '\u092F': 7,
  '\u0936': 7,
  '\u0937': 7,
  '\u0939': 7,
};

const TELUGU_MATRA_VISEMES: Record<string, VisemeCode> = {
  '\u0C3E': 1,
  '\u0C3F': 2,
  '\u0C40': 2,
  '\u0C46': 2,
  '\u0C47': 2,
  '\u0C48': 2,
  '\u0C41': 3,
  '\u0C42': 3,
  '\u0C4A': 3,
  '\u0C4B': 3,
  '\u0C4C': 3,
};

const HINDI_MATRA_VISEMES: Record<string, VisemeCode> = {
  '\u093E': 1,
  '\u093F': 2,
  '\u0940': 2,
  '\u0947': 2,
  '\u0948': 2,
  '\u0941': 3,
  '\u0942': 3,
  '\u094B': 3,
  '\u094C': 3,
};

function isTeluguChar(char: string) {
  return /[\u0C00-\u0C7F]/.test(char);
}

function isHindiChar(char: string) {
  return /[\u0900-\u097F]/.test(char);
}

function isIndicMark(char: string) {
  return /[\u0C01-\u0C04\u0C3E-\u0C56\u0C62-\u0C63\u0901-\u0903\u093C-\u094D\u0951-\u0957\u0962-\u0963]/.test(
    char
  );
}

function latinVisemeAt(text: string, index: number): VisemeCode {
  const pair = text.slice(index, index + 2).toLowerCase();
  const char = text[index]?.toLowerCase() ?? '';
  if (!char.trim()) return 0;
  if (/[.!?,;:]/.test(char)) return 0;
  if (['ch', 'sh', 'jh', 'gy'].includes(pair)) return 7;
  if (pair === 'th') return 6;
  if (char === 'a') return 1;
  if ('eiy'.includes(char)) return 2;
  if ('ouw'.includes(char)) return 3;
  if ('pbm'.includes(char)) return 4;
  if ('fv'.includes(char)) return 5;
  if ('tdnlrsz'.includes(char)) return 6;
  if ('kgqcjx'.includes(char)) return 7;
  return 8;
}

function consumeSpeechCluster(text: string, index: number) {
  const char = text[index] ?? '';

  if (!char.trim() || /[.!?,;:]/.test(char)) {
    return { cluster: char, nextIndex: index + 1, kind: 'pause' as const };
  }

  if (isTeluguChar(char) || isHindiChar(char)) {
    let nextIndex = index + 1;
    while (nextIndex < text.length && isIndicMark(text[nextIndex] ?? '')) {
      nextIndex += 1;
    }

    return {
      cluster: text.slice(index, nextIndex),
      nextIndex,
      kind: isTeluguChar(char) ? ('telugu' as const) : ('hindi' as const),
    };
  }

  const pair = text.slice(index, index + 2).toLowerCase();
  if (['ch', 'sh', 'jh', 'gy', 'th'].includes(pair)) {
    return { cluster: text.slice(index, index + 2), nextIndex: index + 2, kind: 'latin' as const };
  }

  return { cluster: char, nextIndex: index + 1, kind: 'latin' as const };
}

function indicClusterVisemes(
  cluster: string,
  baseMap: Record<string, VisemeCode>,
  matraMap: Record<string, VisemeCode>
) {
  const base = baseMap[cluster[0] ?? ''] ?? 8;
  const matra = Array.from(cluster).find((char) => matraMap[char]);
  const vowel = matra
    ? matraMap[matra]
    : base === 4 || base === 5 || base === 6 || base === 7
      ? 1
      : base;

  if (base === vowel || base === 1 || base === 2 || base === 3) {
    return [{ viseme: vowel, weight: 1, intensity: vowel === 1 ? 0.95 : 0.72 }];
  }

  return [
    { viseme: base, weight: 0.38, intensity: base === 4 ? 0.32 : 0.55 },
    { viseme: vowel, weight: 0.82, intensity: vowel === 1 ? 0.95 : 0.74 },
  ];
}

function clusterToVisemeParts(text: string, index: number) {
  const token = consumeSpeechCluster(text, index);

  if (token.kind === 'pause') {
    const isPunctuation = /[.!?,;:]/.test(token.cluster);
    return {
      nextIndex: token.nextIndex,
      parts: [{ viseme: 0 as VisemeCode, weight: isPunctuation ? 2.5 : 0.95, intensity: 0 }],
    };
  }

  if (token.kind === 'telugu') {
    return {
      nextIndex: token.nextIndex,
      parts: indicClusterVisemes(token.cluster, TELUGU_VISEMES, TELUGU_MATRA_VISEMES),
    };
  }

  if (token.kind === 'hindi') {
    return {
      nextIndex: token.nextIndex,
      parts: indicClusterVisemes(token.cluster, HINDI_VISEMES, HINDI_MATRA_VISEMES),
    };
  }

  const viseme = latinVisemeAt(text, index);
  return {
    nextIndex: token.nextIndex,
    parts: [
      {
        viseme,
        weight: token.cluster.length > 1 ? 1.18 : 1,
        intensity: viseme === 4 ? 0.34 : viseme === 1 ? 0.92 : 0.7,
      },
    ],
  };
}

function buildVisemeTimeline(text: string, mode: VoiceLanguageMode, rate: number): VisemeFrame[] {
  const clean = text.replace(/\s+/g, ' ').trim();
  if (!clean) return [{ at: 0, viseme: 0, intensity: 0 }];

  const frames: VisemeFrame[] = [];
  let cursor = 0;
  const baseMs = mode === 'telugu' || mode === 'hindi' ? 118 : mode === 'mixed' ? 104 : 82;
  const msPerUnit = baseMs / Math.max(0.72, rate);

  let index = 0;
  while (index < clean.length) {
    const { nextIndex, parts } = clusterToVisemeParts(clean, index);

    for (const part of parts) {
      const previous = frames[frames.length - 1];
      if (!previous || previous.viseme !== part.viseme || part.viseme === 0) {
        frames.push({ at: cursor / 1000, viseme: part.viseme, intensity: part.intensity });
      }
      cursor += msPerUnit * part.weight;
    }

    index = Math.max(nextIndex, index + 1);
  }

  frames.push({ at: cursor / 1000, viseme: 0, intensity: 0 });
  return frames;
}

function getVisemeFrameAt(frames: VisemeFrame[], currentTime: number, actualDuration?: number) {
  if (!frames.length) {
    return { at: 0, viseme: 0 as VisemeCode, intensity: 0 };
  }

  const predictedDuration = frames[frames.length - 1]?.at ?? 0;
  const scaledTime =
    actualDuration && Number.isFinite(actualDuration) && actualDuration > 0 && predictedDuration > 0
      ? Math.min(predictedDuration, currentTime * (predictedDuration / actualDuration))
      : currentTime;

  let start = 0;
  let end = frames.length - 1;
  while (start < end) {
    const middle = Math.ceil((start + end) / 2);
    if ((frames[middle]?.at ?? 0) <= scaledTime) {
      start = middle;
    } else {
      end = middle - 1;
    }
  }

  return frames[start] ?? frames[0];
}

function detectVoiceLanguageMode(
  text: string,
  preference: VoiceLanguagePreference
): VoiceLanguageMode {
  const teluguChars = (text.match(/[\u0C00-\u0C7F]/g) ?? []).length;
  const hindiChars = (text.match(/[\u0900-\u097F]/g) ?? []).length;
  const latinChars = (text.match(/[A-Za-z]/g) ?? []).length;
  const words = new Set(text.toLowerCase().match(/[a-z]+/g) ?? []);
  const teluguScore = [...words].filter((word) => TELUGU_ROMAN_HINTS.has(word)).length;
  const hindiScore = [...words].filter((word) => HINDI_ROMAN_HINTS.has(word)).length;

  if (hindiChars > 0) {
    return 'hindi';
  }
  if (teluguChars > 0 && latinChars > 0) {
    return 'mixed';
  }
  if (teluguChars > 0) {
    return 'telugu';
  }
  if (hindiScore >= 2 && hindiScore > teluguScore) {
    return 'hindi';
  }
  if (teluguScore >= 2 && teluguScore >= hindiScore) {
    return 'mixed';
  }
  if (preference === 'hindi' && hindiScore >= 1) return 'hindi';
  if (preference === 'telugu_english' && teluguScore >= 1) return 'mixed';
  return 'english';
}

function preferredLanguageMode(preference: VoiceLanguagePreference): VoiceLanguageMode {
  if (preference === 'hindi') return 'hindi';
  if (preference === 'telugu_english') return 'mixed';
  return 'english';
}

function languageMatches(lang: string, mode: VoiceLanguageMode) {
  const normalized = lang.toLowerCase();
  if (mode === 'hindi') {
    return normalized.startsWith('hi');
  }
  if (mode === 'telugu') {
    return normalized.startsWith('te');
  }
  if (mode === 'mixed') {
    return (
      normalized.startsWith('te') || normalized.startsWith('en-in') || normalized.includes('india')
    );
  }
  return normalized.startsWith('en');
}

function normalizeAssistantEchoText(text: string) {
  return (text || '')
    .toLowerCase()
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[^\p{L}\p{N}\s]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function looksLikeAssistantSpeechEcho(heardText: string, spokenText: string, activeUntil: number) {
  if (!heardText || !spokenText || Date.now() > activeUntil) return false;
  const heard = normalizeAssistantEchoText(heardText);
  const spoken = normalizeAssistantEchoText(spokenText);
  if (heard.length < 3 || spoken.length < 3) return false;
  if (spoken.includes(heard)) return true;

  const heardWords = heard.split(' ').filter((word) => word.length > 1);
  if (!heardWords.length) return false;
  const spokenWords = new Set(spoken.split(' ').filter(Boolean));
  const matched = heardWords.filter((word) => spokenWords.has(word)).length;
  return matched / heardWords.length >= 0.8;
}

/**
 * A synthesized utterance, at the moment it starts playing.
 *
 * Emitted for the photoreal avatar, which needs two things nothing else in the
 * app does: the exact audio bytes, so the rendered face and the audible sound
 * share one waveform rather than two separate syntheses, and the element playing
 * them, so the face seeks by `currentTime` instead of guessing from a start
 * timestamp that drifts whenever playback stalls or re-buffers.
 */
export interface SpeechAudioEvent {
  id: string;
  text: string;
  blob: Blob;
  audio: HTMLAudioElement;
}

export function useVoice() {
  const [state, setState] = useState<VoiceState>({
    isListening: false,
    isSpeaking: false,
    transcript: '',
    finalTranscript: '',
    voiceNotice: '',
    voiceNoticeId: 0,
    speakingVolume: 0,
    viseme: 0,
    hearingSource: 'browser',
    localHearingBusy: false,
  });
  const [availableVoices, setAvailableVoices] = useState<VoiceOption[]>([]);
  const [voiceGender, setVoiceGender] = useState<VoiceGender>('female');
  const [voiceTone, setVoiceTone] = useState<VoiceTone>('friendly');
  const [voiceLanguage, setVoiceLanguage] = useState<VoiceLanguagePreference>(() => {
    if (typeof window !== 'undefined') {
      const saved = window.localStorage.getItem(
        'akansha_voice_language'
      ) as VoiceLanguagePreference | null;
      if (saved && ['telugu_english', 'english', 'hindi'].includes(saved)) return saved;
    }
    return 'english';
  });
  const [backgroundListening, setBackgroundListening] = useState(false);
  const [selectedVoiceId, setSelectedVoiceId] = useState<string | null>(null);
  const [voiceFrequencySignature, setVoiceFrequencySignature] =
    useState<VoiceFrequencySignature | null>(null);

  const recognitionRef = useRef<SpeechRecognition | null>(null);
  const synthRef = useRef<SpeechSynthesis | null>(null);
  const manuallyStoppedRef = useRef(false);
  const voiceRef = useRef<SpeechSynthesisVoice | null>(null);
  const visemeIntervalRef = useRef<number | null>(null);
  const sampleAudioRef = useRef<HTMLAudioElement | null>(null);
  const playbackAudioRef = useRef<HTMLAudioElement | null>(null);
  const playbackUrlRef = useRef<string | null>(null);
  const speechAudioListenersRef = useRef<Set<(event: SpeechAudioEvent) => void>>(new Set());
  const playbackGenerationRef = useRef(0);
  const playbackStopResolveRef = useRef<(() => void) | null>(null);
  const activeUtteranceRef = useRef<SpeechSynthesisUtterance | null>(null);
  const audioOwnerIdRef = useRef(`voice-${Math.random().toString(36).slice(2)}`);
  const speechQueueRunIdRef = useRef(0);
  const speechQueueRef = useRef<QueuedSpeechItem[]>([]);
  const processingQueueRef = useRef(false);
  const stopRequestedRef = useRef(false);
  const isSpeakingRef = useRef(false);
  const isListeningRef = useRef(false);
  const backgroundListeningRef = useRef(false);
  const recognitionStartingRef = useRef(false);
  const microphoneBlockedRef = useRef(false);
  const recognitionRestartTimerRef = useRef<number | null>(null);
  const recognitionLanguageIndexRef = useRef(0);
  // Consecutive `no-speech`/`network` errors with no confirmed transcript in
  // between. Drives both the restart backoff and the decision to try another
  // recognition language, neither of which may key off a single event: silence
  // is not a failure, and `no-speech` is what the browser calls silence.
  const recognitionMissStreakRef = useRef(0);
  // ── Local hearing (faster-whisper) ────────────────────────────────────────
  // Consecutive recogniser failures of the kind that mean "the service is not
  // reachable", as distinct from `no-speech`, which means silence. Reset by any
  // successful result, because a recogniser that produced a transcript is
  // working regardless of what it did a minute ago.
  const recognitionServiceFailuresRef = useRef(0);
  const localHearingReportRef = useRef<LocalCapabilityReport | null>(null);
  const localCaptureRef = useRef<LocalCapture | null>(null);
  // Silero's measurement of the last local turn. The reason the fallback is
  // worth having even when Chrome works: this is trailing silence measured on the
  // waveform, not inferred from when the recogniser last emitted a word.
  const localVoiceActivityRef = useRef<LocalVoiceActivity | null>(null);
  const usingLocalHearingRef = useRef(false);
  // Mirrors of the two settings the recognition handlers read. The effect that
  // builds `SpeechRecognition` used to depend on `selectedVoiceId`, `voiceGender`
  // and `voiceLanguage` — and a sibling effect *sets* `selectedVoiceId` as soon
  // as the browser reports its voices. So the first voice hydration tore the
  // recogniser down and built a new one, and `abort()` mid-utterance throws away
  // whatever the user was saying. Reading these through refs keeps the recogniser
  // alive for the lifetime of the mount; `.lang` is still updated in place.
  const voiceLanguageRef = useRef<VoiceLanguagePreference>(voiceLanguage);
  const voiceGenderRef = useRef<VoiceGender>(voiceGender);
  const assistantSpeechEchoUntilRef = useRef(0);
  const lastAssistantSpeechTextRef = useRef('');
  const lastSpeechEnergyAtRef = useRef(0);
  const bargeInStartedAtRef = useRef<number | null>(null);
  const pendingBargeInRef = useRef(false);
  const bargeInCaptureTimerRef = useRef<number | null>(null);
  const ambientRmsRef = useRef(0.008);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const sourceNodeRef = useRef<MediaElementAudioSourceNode | null>(null);
  const sourceElementRef = useRef<HTMLAudioElement | null>(null);
  const audioFrameRef = useRef<number | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const micAudioContextRef = useRef<AudioContext | null>(null);
  const micSourceNodeRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const micAnalyserRef = useRef<AnalyserNode | null>(null);
  const micFrameRef = useRef<number | null>(null);
  const micSignatureRef = useRef<VoiceFrequencySignature | null>(null);
  const visemeTimelineRef = useRef<VisemeFrame[]>([]);
  const visemeStartTimeRef = useRef(0);

  const stopSpeechNowRef = useRef<(nextState?: Partial<VoiceState>) => void>(() => undefined);
  // The recogniser's event handlers live inside a mount-only effect, so they
  // cannot close over these callbacks directly — the effect would have to depend
  // on them, and re-running it aborts `SpeechRecognition` mid-utterance. Same
  // indirection, and same reason, as `stopSpeechNowRef` above.
  const beginLocalCaptureRef = useRef<() => void>(() => undefined);
  const abandonLocalCaptureRef = useRef<() => void>(() => undefined);
  const localHearingUsableRef = useRef<() => boolean>(() => false);
  const finishLocalCaptureRef = useRef<() => Promise<unknown>>(async () => null);

  const raiseVoiceNotice = useCallback((message: string) => {
    setState((previous) => ({
      ...previous,
      voiceNotice: message,
      voiceNoticeId: previous.voiceNoticeId + 1,
    }));
  }, []);

  const clearVoiceNotice = useCallback(() => {
    setState((previous) => ({ ...previous, voiceNotice: '' }));
  }, []);

  const markAssistantSpeechEchoGuard = useCallback((text: string, minimumMs = 2200) => {
    lastAssistantSpeechTextRef.current = text;
    assistantSpeechEchoUntilRef.current =
      Date.now() + Math.min(24_000, Math.max(minimumMs, text.length * 60));
  }, []);

  const extendAssistantSpeechEchoGuard = useCallback((ms = 1500) => {
    assistantSpeechEchoUntilRef.current = Date.now() + ms;
  }, []);

  const clearBargeInCaptureTimer = useCallback(() => {
    if (typeof window === 'undefined') return;
    if (bargeInCaptureTimerRef.current) {
      window.clearTimeout(bargeInCaptureTimerRef.current);
      bargeInCaptureTimerRef.current = null;
    }
  }, []);

  /**
   * Whether local Whisper can take this turn.
   *
   * Three things have to be true and all three are checked rather than assumed:
   * this browser can record (`MediaRecorder` — Safari before 14.1 cannot), the
   * microphone is already open (we borrow `useVoice`'s stream rather than asking
   * for a second one), and the backend actually has the models installed.
   */
  const localHearingUsable = useCallback(() => {
    if (!localCaptureSupported()) return false;
    if (!micStreamRef.current) return false;
    return !!localHearingReportRef.current?.hearing;
  }, []);

  /**
   * Start recording this turn for local transcription.
   *
   * Idempotent: a capture already in flight is left alone. The recogniser's
   * `onerror` and the "no recogniser at all" path can both ask for a capture
   * within the same tick, and two `MediaRecorder`s on one stream would produce
   * two transcripts of the same sentence.
   */
  const beginLocalCapture = useCallback(() => {
    if (localCaptureRef.current) return;
    if (!localHearingUsable()) return;
    const stream = micStreamRef.current;
    if (!stream) return;
    usingLocalHearingRef.current = true;
    localCaptureRef.current = new LocalCapture(stream);
    setState((previous) =>
      previous.hearingSource === 'local' ? previous : { ...previous, hearingSource: 'local' }
    );
  }, [localHearingUsable]);

  /**
   * Close the capture, transcribe it, and commit the words as a final transcript.
   *
   * There are no interim results on this path — Whisper sees the whole utterance
   * at once — so the UI is told it is busy for the ~1.2 s this takes rather than
   * appearing to have gone deaf. A `null` result is not an error to raise at the
   * user: it is silence, a too-short clip, or a backend that turned out not to
   * have the models, and the recogniser is still trying in parallel.
   */
  const finishLocalCapture = useCallback(async () => {
    const capture = localCaptureRef.current;
    if (!capture) return null;
    localCaptureRef.current = null;
    setState((previous) => ({ ...previous, localHearingBusy: true }));
    try {
      const audio = await capture.stop();
      if (!audio) return null;
      const heard = await transcribeLocally(audio, {
        // Autodetect, deliberately. Pinning `en` is what turns a Telugu-English
        // sentence into English-shaped nonsense, and the whole point of the
        // language rotation elsewhere in this file is that we do not know in
        // advance which one is coming.
        hint: '',
      });
      if (!heard) return null;
      const text = heard.transcript.text.trim();
      localVoiceActivityRef.current = heard.voiceActivity;
      if (!text) return null;
      // Whisper heard something, so whatever the browser recogniser was doing is
      // no longer holding the conversation up. Same reset the browser path does
      // on a confirmed result, for the same reason.
      recognitionMissStreakRef.current = 0;
      lastSpeechEnergyAtRef.current = Date.now();
      clearVoiceNotice();
      setState((previous) => ({
        ...previous,
        transcript: '',
        finalTranscript: `${previous.finalTranscript} ${text}`.trim(),
      }));
      return heard;
    } finally {
      setState((previous) => ({ ...previous, localHearingBusy: false }));
    }
  }, [clearVoiceNotice]);

  /** Drop a capture without transcribing it. Used when the browser recogniser
   *  recovers mid-turn: two ears both committing the same sentence would send it
   *  twice. */
  const abandonLocalCapture = useCallback(() => {
    localCaptureRef.current?.abort();
    localCaptureRef.current = null;
    usingLocalHearingRef.current = false;
    setState((previous) =>
      previous.hearingSource === 'browser' ? previous : { ...previous, hearingSource: 'browser' }
    );
  }, []);

  // Stable ref readers rather than inline arrows in the return object. An inline
  // `() => ref.current` is a new function on every render, which silently defeats
  // `memo` on any component that receives it and forces every effect that lists
  // it as a dependency to re-run. These three exist to be read from effects, so
  // that would be the common case, not the rare one.
  const lastSpeechEnergyAt = useCallback(() => lastSpeechEnergyAtRef.current, []);
  const localVoiceActivity = useCallback(() => localVoiceActivityRef.current, []);
  const localHearingReport = useCallback(() => localHearingReportRef.current, []);

  useEffect(() => {
    beginLocalCaptureRef.current = beginLocalCapture;
    localHearingUsableRef.current = localHearingUsable;
    finishLocalCaptureRef.current = finishLocalCapture;
    abandonLocalCaptureRef.current = abandonLocalCapture;
  }, [beginLocalCapture, abandonLocalCapture, localHearingUsable, finishLocalCapture]);

  /**
   * Find out once, at mount, whether local hearing is available.
   *
   * Cheap by design — the endpoint reports what is installed without loading a
   * model. When the models are there, warm them: the server warms them in its own
   * startup thread, but a frontend that outlives a backend restart would
   * otherwise pay ~16 s of one-time CTranslate2 and numba work on the first
   * fallback turn, which is precisely the turn where the user is already waiting
   * because Chrome just failed.
   */
  useEffect(() => {
    if (typeof window === 'undefined') return;
    let cancelled = false;
    void probeLocalHearing().then((report) => {
      if (cancelled) return;
      localHearingReportRef.current = report;
      if (report.hearing) warmLocalHearing();
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const scheduleBargeInCaptureTimeout = useCallback(() => {
    if (typeof window === 'undefined') return;
    clearBargeInCaptureTimer();
    bargeInCaptureTimerRef.current = window.setTimeout(() => {
      if (!pendingBargeInRef.current) return;
      pendingBargeInRef.current = false;
      raiseVoiceNotice(
        "Sorry, I heard you interrupt, but I didn't catch the words. Can you repeat that?"
      );
    }, 2600);
  }, [clearBargeInCaptureTimer, raiseVoiceNotice]);

  useEffect(() => {
    isSpeakingRef.current = state.isSpeaking;
  }, [state.isSpeaking]);

  useEffect(() => {
    isListeningRef.current = state.isListening;
  }, [state.isListening]);

  useEffect(() => {
    backgroundListeningRef.current = backgroundListening;
  }, [backgroundListening]);

  useEffect(() => {
    voiceLanguageRef.current = voiceLanguage;
  }, [voiceLanguage]);

  useEffect(() => {
    voiceGenderRef.current = voiceGender;
  }, [voiceGender]);

  useEffect(() => {
    if (typeof window === 'undefined') return;

    sampleAudioRef.current = new Audio(FEMALE_SAMPLE_PATH);
    playbackAudioRef.current = new Audio();

    const hydrateVoices = () => {
      const synth = window.speechSynthesis;
      synthRef.current = synth;
      const voices = [
        FEMALE_SAMPLE_OPTION,
        ...synth
          .getVoices()
          .map((voice) => ({
            id: voice.voiceURI,
            name: voice.name,
            lang: voice.lang,
            gender: inferGender(voice.name),
            kind: 'system' as const,
          }))
          .filter((voice) => {
            const normalized = voice.lang.toLowerCase();
            return (
              normalized.startsWith('en') ||
              normalized.startsWith('te') ||
              normalized.startsWith('hi') ||
              normalized.includes('india')
            );
          }),
      ];

      setAvailableVoices(voices);
      if (voices.length) {
        const preferred =
          voices.find((voice) => voice.gender === voiceGenderRef.current) ?? voices[0];
        // Functional update rather than an `if (!selectedVoiceId)` guard: this
        // runs from `onvoiceschanged` too, long after the closure was created.
        setSelectedVoiceId((current) => current || preferred.id);
      }
    };

    hydrateVoices();
    window.speechSynthesis.onvoiceschanged = hydrateVoices;

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

    if (SpeechRecognition) {
      const recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.maxAlternatives = 3;
      recognition.lang = preferredRecognitionLanguage(
        voiceLanguageRef.current,
        recognitionLanguageIndexRef.current
      );

      const restartDelayForStreak = () => {
        if (recognitionMissStreakRef.current === 0) {
          return Date.now() - lastSpeechEnergyAtRef.current < SILENCE_RESTART_MS ? 90 : 220;
        }
        return Math.min(
          240 * 2 ** (recognitionMissStreakRef.current - 1),
          RECOGNITION_RESTART_MAX_MS
        );
      };

      /**
       * Bring recognition back up, and keep trying if the browser refuses.
       *
       * The refusal case is the one that broke continuous listening. `start()`
       * throws `InvalidStateError` while the previous session is still releasing;
       * the old code caught that, cleared the starting flag and scheduled
       * nothing. A refused start creates no session, so no further `onend` or
       * `onerror` could ever arrive to try again — the chain died silently and
       * the microphone stayed dead until the user toggled it by hand. Retrying
       * with a short backoff is what makes "continuous" continuous.
       *
       * The listening-still-wanted check inside the timer matters too: without
       * it a restart already in flight would switch the microphone back on after
       * the user had just switched it off.
       */
      const scheduleRecognitionRestart = (attempt = 0) => {
        if (recognitionRestartTimerRef.current) {
          window.clearTimeout(recognitionRestartTimerRef.current);
        }
        const delay = attempt === 0 ? restartDelayForStreak() : Math.min(150 * 2 ** attempt, 2000);
        recognitionRestartTimerRef.current = window.setTimeout(() => {
          recognitionRestartTimerRef.current = null;
          if (!backgroundListeningRef.current || manuallyStoppedRef.current) return;
          try {
            recognitionStartingRef.current = true;
            recognition.start();
          } catch {
            recognitionStartingRef.current = false;
            if (attempt < RECOGNITION_START_RETRIES) {
              scheduleRecognitionRestart(attempt + 1);
            }
          }
        }, delay);
      };

      recognition.onresult = (event: SpeechRecognitionEvent) => {
        let interim = '';
        let finalResult = '';

        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const alternatives = Array.from(event.results[i] ?? []) as SpeechRecognitionAlternative[];
          const strongestAlternative =
            alternatives.find((alternative) => alternative.confidence >= 0.45) ??
            alternatives[0] ??
            event.results[i][0];
          const value = strongestAlternative.transcript.trim();
          if (event.results[i].isFinal) {
            finalResult += `${value} `;
          } else {
            interim += `${value} `;
          }
        }

        const heardText = `${finalResult} ${interim}`.trim();
        const finalHeardText = finalResult.trim();
        const isInterruptCommand = SPEECH_INTERRUPT_PATTERN.test(heardText);
        const isAssistantEcho =
          heardText &&
          !isInterruptCommand &&
          looksLikeAssistantSpeechEcho(
            heardText,
            lastAssistantSpeechTextRef.current,
            assistantSpeechEchoUntilRef.current
          );

        if (isAssistantEcho) {
          pendingBargeInRef.current = false;
          clearBargeInCaptureTimer();
          setState((previous) => ({ ...previous, transcript: '' }));
          return;
        }

        if (heardText) {
          clearVoiceNotice();
          clearBargeInCaptureTimer();
          lastSpeechEnergyAtRef.current = Date.now();
          const preference = voiceLanguageRef.current;
          const languageMode = detectVoiceLanguageMode(heardText, preference);
          const nextLanguage =
            languageMode === 'hindi'
              ? 'hi-IN'
              : languageMode === 'telugu'
                ? 'te-IN'
                : languageMode === 'mixed'
                  ? preference === 'hindi'
                    ? 'hi-IN'
                    : preference === 'telugu_english'
                      ? 'te-IN'
                      : 'en-IN'
                  : preferredRecognitionLanguage(preference, recognitionLanguageIndexRef.current);
          if (recognition.lang !== nextLanguage) {
            recognition.lang = nextLanguage;
          }
          if (finalHeardText) {
            // A confirmed transcript proves the current language and the current
            // restart cadence both work, so the counters go back to zero. Without
            // this the language index only ever advanced and the backoff only ever
            // grew — one long pause was enough to leave the recogniser listening
            // in a language the user was not speaking, permanently. Reset after
            // the language decision above, so this turn still uses the index that
            // actually produced the result.
            recognitionMissStreakRef.current = 0;
            recognitionLanguageIndexRef.current = 0;
            // The recogniser is demonstrably working, so the local fallback is
            // not needed for this turn. Dropping the capture rather than letting
            // it finish matters: both ears committing the same sentence would
            // send the instruction twice, and "message Amma" executed twice is a
            // real consequence, not a cosmetic one.
            recognitionServiceFailuresRef.current = 0;
            if (localCaptureRef.current) {
              abandonLocalCaptureRef.current();
            }
          }
        }
        if (isSpeakingRef.current && isInterruptCommand) {
          stopSpeechNowRef.current({ transcript: '', finalTranscript: '' });
          return;
        }

        if (
          (isSpeakingRef.current || pendingBargeInRef.current) &&
          heardText.split(/\s+/).filter(Boolean).length >= 1
        ) {
          stopSpeechNowRef.current();
          pendingBargeInRef.current = false;
          clearBargeInCaptureTimer();
          if (!finalHeardText) {
            setState((previous) => ({ ...previous, transcript: interim.trim() }));
            return;
          }
        }

        if (isSpeakingRef.current && finalHeardText) {
          stopSpeechNowRef.current();
          pendingBargeInRef.current = false;
          clearBargeInCaptureTimer();
          setState((previous) => ({
            ...previous,
            transcript: interim.trim(),
            finalTranscript: `${previous.finalTranscript} ${finalHeardText}`.trim(),
          }));
          return;
        }

        setState((previous) => {
          const nextFinal = finalResult.trim()
            ? `${previous.finalTranscript} ${finalResult.trim()}`.trim()
            : previous.finalTranscript;
          return {
            ...previous,
            transcript: interim.trim(),
            finalTranscript: nextFinal,
          };
        });
      };

      recognition.onstart = () => {
        manuallyStoppedRef.current = false;
        recognitionStartingRef.current = false;
        setState((previous) => ({ ...previous, isListening: true }));
      };

      recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
        recognitionStartingRef.current = false;
        setState((previous) => ({ ...previous, isListening: false }));
        if (['not-allowed', 'service-not-allowed'].includes(event?.error)) {
          // These two are not the same failure, and treating them alike is what
          // made a working microphone look like a denied one. `not-allowed` is
          // the user refusing microphone access -- nothing we own can recover
          // from that. `service-not-allowed` is Chrome refusing to use its
          // *remote speech service* while the microphone is still perfectly
          // available, which is exactly the case local Whisper exists for.
          if (event?.error === 'service-not-allowed' && localHearingUsableRef.current()) {
            recognitionServiceFailuresRef.current += 1;
            beginLocalCaptureRef.current();
            scheduleRecognitionRestart();
            return;
          }
          microphoneBlockedRef.current = true;
          manuallyStoppedRef.current = true;
          setBackgroundListening(false);
          return;
        }
        if (
          backgroundListeningRef.current &&
          !manuallyStoppedRef.current &&
          ['no-speech', 'audio-capture', 'network', 'aborted'].includes(event?.error)
        ) {
          // `network` is the one that leaves the assistant deaf: Chrome's
          // recogniser is a round trip to Google's speech service, and when that
          // is unreachable no amount of restarting produces a transcript. Two in
          // a row and the turn goes to local Whisper instead. `no-speech` is
          // explicitly not counted -- that is silence, and silence is not a
          // failure of the recogniser.
          if (event?.error === 'network') {
            recognitionServiceFailuresRef.current += 1;
            if (recognitionServiceFailuresRef.current >= RECOGNITION_FAILURES_BEFORE_LOCAL) {
              beginLocalCaptureRef.current();
            }
          }
          if (
            event?.error === 'no-speech' &&
            (pendingBargeInRef.current ||
              Date.now() - lastSpeechEnergyAtRef.current < SILENCE_RESTART_MS)
          ) {
            pendingBargeInRef.current = false;
            clearBargeInCaptureTimer();
            raiseVoiceNotice("Sorry, I didn't catch that clearly. Can you repeat it once?");
          }
          if (['no-speech', 'network'].includes(event?.error)) {
            recognitionMissStreakRef.current += 1;
            // Only try another language after a streak. Rotating on every event
            // walked a `telugu_english` session te-IN → en-IN → hi-IN during
            // nothing but silence, so a user who paused for a few seconds came
            // back to a recogniser listening in a language they do not speak.
            // That is the "continuous listening breaks" symptom: it half-works,
            // drifts, then mishears everything.
            if (recognitionMissStreakRef.current % RECOGNITION_LANGUAGE_ROTATE_AFTER === 0) {
              recognitionLanguageIndexRef.current += 1;
              recognition.lang = preferredRecognitionLanguage(
                voiceLanguageRef.current,
                recognitionLanguageIndexRef.current
              );
            }
          }
          scheduleRecognitionRestart();
        }
      };

      recognition.onend = () => {
        recognitionStartingRef.current = false;
        setState((previous) => ({ ...previous, isListening: false }));
        if (backgroundListeningRef.current && !manuallyStoppedRef.current) {
          scheduleRecognitionRestart();
        }
      };

      recognitionRef.current = recognition;
    }

    return () => {
      window.speechSynthesis.onvoiceschanged = null;
      try {
        recognitionRef.current?.abort?.();
      } catch {
        // Recognition can throw if the browser already stopped it.
      }
      recognitionRef.current = null;
      if (recognitionRestartTimerRef.current) {
        window.clearTimeout(recognitionRestartTimerRef.current);
        recognitionRestartTimerRef.current = null;
      }
      clearBargeInCaptureTimer();
    };
    // Mount-only by design: every dependency here is a `useCallback` with an
    // empty dependency list. Anything that changes at runtime (language, gender,
    // selected voice) is read through a ref, because re-running this effect
    // aborts and rebuilds `SpeechRecognition` and drops the utterance in flight.
  }, [clearBargeInCaptureTimer, clearVoiceNotice, raiseVoiceNotice]);

  useEffect(() => {
    if (!availableVoices.length) return;

    const mode = preferredLanguageMode(voiceLanguage);
    const current = availableVoices.find((voice) => voice.id === selectedVoiceId);
    const genderMatched = availableVoices.find((voice) => voice.gender === voiceGender);
    const languageMatched = availableVoices.find(
      (voice) => voice.gender === voiceGender && languageMatches(voice.lang, mode)
    );
    const selected =
      current && current.gender === voiceGender && languageMatches(current.lang, mode)
        ? current
        : (languageMatched ?? genderMatched ?? current ?? availableVoices[0]);

    if (selected.id !== selectedVoiceId) {
      setSelectedVoiceId(selected.id);
    }

    if (typeof window !== 'undefined') {
      voiceRef.current =
        window.speechSynthesis.getVoices().find((voice) => voice.voiceURI === selected.id) ?? null;
    }
  }, [availableVoices, selectedVoiceId, voiceGender, voiceLanguage]);

  useEffect(() => {
    if (!recognitionRef.current) return;

    recognitionLanguageIndexRef.current = 0;
    recognitionRef.current.lang = preferredRecognitionLanguage(voiceLanguage);
  }, [voiceLanguage]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem('akansha_voice_language', voiceLanguage);
  }, [voiceLanguage]);

  const voiceChoices = useMemo(() => {
    const filteredVoices = availableVoices.filter(
      (voice) =>
        voice.gender === voiceGender &&
        languageMatches(voice.lang, preferredLanguageMode(voiceLanguage))
    );

    return filteredVoices.length
      ? filteredVoices
      : availableVoices.filter((voice) => voice.gender === voiceGender);
  }, [availableVoices, voiceGender, voiceLanguage]);

  const startVisemeAnimation = useCallback(
    (text?: string, mode: VoiceLanguageMode = 'english', tone: VoiceTone = 'friendly') => {
      if (typeof window === 'undefined') return;
      if (visemeIntervalRef.current) {
        window.clearInterval(visemeIntervalRef.current);
      }

      const frames = text?.trim() ? buildVisemeTimeline(text, mode, TONE_CONFIG[tone].rate) : [];
      visemeTimelineRef.current = frames;
      visemeStartTimeRef.current = window.performance.now();

      if (!frames.length) {
        setState((previous) => ({ ...previous, speakingVolume: 0, viseme: 0 }));
        return;
      }

      visemeIntervalRef.current = window.setInterval(() => {
        if (visemeTimelineRef.current.length) {
          const elapsed = (window.performance.now() - visemeStartTimeRef.current) / 1000;
          const activeFrame = getVisemeFrameAt(visemeTimelineRef.current, elapsed);

          setState((previous) => ({
            ...previous,
            speakingVolume: activeFrame.intensity,
            viseme: activeFrame.viseme,
          }));
          return;
        }

        setState((previous) => ({ ...previous, speakingVolume: 0, viseme: 0 }));
      }, 34);
    },
    []
  );

  const stopVisemeAnimation = useCallback(() => {
    if (typeof window === 'undefined') return;
    if (visemeIntervalRef.current) {
      window.clearInterval(visemeIntervalRef.current);
      visemeIntervalRef.current = null;
    }
    visemeTimelineRef.current = [];
    setState((previous) => ({ ...previous, speakingVolume: 0, viseme: 0 }));
  }, []);

  const stopAudioAnalysis = useCallback(() => {
    if (typeof window === 'undefined') return;
    if (audioFrameRef.current) {
      window.cancelAnimationFrame(audioFrameRef.current);
      audioFrameRef.current = null;
    }
  }, []);

  const stopMicFrequencyCapture = useCallback(() => {
    if (typeof window === 'undefined') return;
    clearBargeInCaptureTimer();
    if (micFrameRef.current) {
      window.cancelAnimationFrame(micFrameRef.current);
      micFrameRef.current = null;
    }
    try {
      micSourceNodeRef.current?.disconnect();
    } catch {
      // The browser may already have disconnected the node.
    }
    micSourceNodeRef.current = null;
    micAnalyserRef.current = null;
    micStreamRef.current?.getTracks().forEach((track) => track.stop());
    micStreamRef.current = null;
    void micAudioContextRef.current?.close().catch(() => undefined);
    micAudioContextRef.current = null;
  }, [clearBargeInCaptureTimer]);

  const startMicFrequencyCapture = useCallback(
    async (stream: MediaStream) => {
      if (typeof window === 'undefined') return;
      stopMicFrequencyCapture();

      const context = new window.AudioContext();
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.74;

      const source = context.createMediaStreamSource(stream);
      source.connect(analyser);

      micStreamRef.current = stream;
      micAudioContextRef.current = context;
      micSourceNodeRef.current = source;
      micAnalyserRef.current = analyser;

      const frequencyData = new Uint8Array(analyser.frequencyBinCount);
      const timeData = new Uint8Array(analyser.fftSize);
      const sampleRate = context.sampleRate || 48000;
      const binHz = sampleRate / analyser.fftSize;

      const tick = () => {
        analyser.getByteFrequencyData(frequencyData);
        analyser.getByteTimeDomainData(timeData);

        let weightedFrequency = 0;
        let totalEnergy = 0;
        let lowEnergy = 0;
        let midEnergy = 0;
        let highEnergy = 0;

        frequencyData.forEach((value, index) => {
          const energy = value / 255;
          const frequency = index * binHz;
          weightedFrequency += frequency * energy;
          totalEnergy += energy;
          if (frequency < 300) lowEnergy += energy;
          else if (frequency < 1800) midEnergy += energy;
          else highEnergy += energy;
        });

        const rms =
          Math.sqrt(
            timeData.reduce((sum, value) => {
              const centered = (value - 128) / 128;
              return sum + centered * centered;
            }, 0) / Math.max(1, timeData.length)
          ) || 0;
        const nowMs = Date.now();
        const speechBandRatio = midEnergy / Math.max(1, lowEnergy + midEnergy + highEnergy);
        const ambientRms = ambientRmsRef.current;
        const adaptiveSoftThreshold = Math.min(
          0.04,
          Math.max(SOFT_SPEECH_RMS_THRESHOLD, ambientRms * 1.9 + 0.004)
        );
        const adaptiveBargeThreshold = Math.min(
          0.065,
          Math.max(BARGE_IN_RMS_THRESHOLD, ambientRms * 2.7 + 0.008)
        );

        if (!isSpeakingRef.current && rms < adaptiveSoftThreshold && speechBandRatio < 0.34) {
          ambientRmsRef.current = ambientRms * 0.94 + rms * 0.06;
        }

        if (rms >= adaptiveSoftThreshold && speechBandRatio > 0.2) {
          lastSpeechEnergyAtRef.current = nowMs;
        }

        if (
          isSpeakingRef.current &&
          rms >= adaptiveBargeThreshold &&
          speechBandRatio > 0.22 &&
          !stopRequestedRef.current
        ) {
          if (bargeInStartedAtRef.current === null) {
            bargeInStartedAtRef.current = nowMs;
          }
          if (nowMs - bargeInStartedAtRef.current >= BARGE_IN_HOLD_MS) {
            pendingBargeInRef.current = true;
            stopSpeechNowRef.current();
            scheduleBargeInCaptureTimeout();
            bargeInStartedAtRef.current = null;
          }
        } else if (rms < adaptiveSoftThreshold) {
          bargeInStartedAtRef.current = null;
        }

        const previous = micSignatureRef.current;
        const sampleCount = (previous?.sampleCount ?? 0) + 1;
        const centroid = totalEnergy
          ? weightedFrequency / totalEnergy
          : (previous?.spectralCentroidHz ?? 0);
        const total = lowEnergy + midEnergy + highEnergy || 1;
        const next: VoiceFrequencySignature = {
          averageFrequencyHz: Math.round(centroid),
          spectralCentroidHz: Math.round(
            previous ? previous.spectralCentroidHz * 0.82 + centroid * 0.18 : centroid
          ),
          lowBandEnergy: Number(
            (previous
              ? previous.lowBandEnergy * 0.82 + (lowEnergy / total) * 0.18
              : lowEnergy / total
            ).toFixed(3)
          ),
          midBandEnergy: Number(
            (previous
              ? previous.midBandEnergy * 0.82 + (midEnergy / total) * 0.18
              : midEnergy / total
            ).toFixed(3)
          ),
          highBandEnergy: Number(
            (previous
              ? previous.highBandEnergy * 0.82 + (highEnergy / total) * 0.18
              : highEnergy / total
            ).toFixed(3)
          ),
          rmsLevel: Number((previous ? previous.rmsLevel * 0.82 + rms * 0.18 : rms).toFixed(4)),
          sampleCount,
          capturedAt: new Date().toISOString(),
        };

        micSignatureRef.current = next;
        if (sampleCount % 10 === 0) {
          setVoiceFrequencySignature(next);
        }
        micFrameRef.current = window.requestAnimationFrame(tick);
      };

      tick();
    },
    [scheduleBargeInCaptureTimeout, stopMicFrequencyCapture]
  );

  const startSampleAudioAnalysis = useCallback(() => {
    if (typeof window === 'undefined' || !sampleAudioRef.current) return;

    const sampleAudio = sampleAudioRef.current;

    try {
      if (!audioContextRef.current) {
        audioContextRef.current = new window.AudioContext();
      }

      const context = audioContextRef.current;

      if (context.state === 'suspended') {
        void context.resume();
      }

      if (!sourceNodeRef.current || sourceElementRef.current !== sampleAudio) {
        sourceNodeRef.current?.disconnect();
        sourceNodeRef.current = context.createMediaElementSource(sampleAudio);
        sourceElementRef.current = sampleAudio;
      }

      if (!analyserRef.current) {
        analyserRef.current = context.createAnalyser();
        analyserRef.current.fftSize = 256;
        analyserRef.current.smoothingTimeConstant = 0.82;
      }

      sourceNodeRef.current.disconnect();
      analyserRef.current.disconnect();
      sourceNodeRef.current.connect(analyserRef.current);
      analyserRef.current.connect(context.destination);

      const analyser = analyserRef.current;
      const dataArray = new Uint8Array(analyser.frequencyBinCount);

      const tick = () => {
        analyser.getByteFrequencyData(dataArray);
        const average =
          dataArray.reduce((sum, value) => sum + value, 0) / Math.max(1, dataArray.length);
        const normalized = Math.min(1, Math.max(0.06, average / 72));

        setState((previous) => ({
          ...previous,
          speakingVolume: normalized,
          viseme: Math.max(0, Math.min(5, Math.round(normalized * 5))),
        }));

        audioFrameRef.current = window.requestAnimationFrame(tick);
      };

      stopAudioAnalysis();
      tick();
    } catch {
      startVisemeAnimation();
    }
  }, [startVisemeAnimation, stopAudioAnalysis]);

  const startPlaybackAudioAnalysis = useCallback(
    (text?: string, mode: VoiceLanguageMode = 'english', tone: VoiceTone = 'friendly') => {
      if (typeof window === 'undefined' || !playbackAudioRef.current) return;

      const playbackAudio = playbackAudioRef.current;
      const textFrames = text?.trim()
        ? buildVisemeTimeline(text, mode, TONE_CONFIG[tone].rate)
        : [];

      try {
        if (!audioContextRef.current) {
          audioContextRef.current = new window.AudioContext();
        }

        const context = audioContextRef.current;

        if (context.state === 'suspended') {
          void context.resume();
        }

        if (!sourceNodeRef.current || sourceElementRef.current !== playbackAudio) {
          sourceNodeRef.current?.disconnect();
          sourceNodeRef.current = context.createMediaElementSource(playbackAudio);
          sourceElementRef.current = playbackAudio;
        }

        if (!analyserRef.current) {
          analyserRef.current = context.createAnalyser();
          analyserRef.current.fftSize = 256;
          analyserRef.current.smoothingTimeConstant = 0.82;
        }

        sourceNodeRef.current.disconnect();
        analyserRef.current.disconnect();
        sourceNodeRef.current.connect(analyserRef.current);
        analyserRef.current.connect(context.destination);

        const analyser = analyserRef.current;
        const dataArray = new Uint8Array(analyser.frequencyBinCount);

        const tick = () => {
          analyser.getByteFrequencyData(dataArray);
          const average =
            dataArray.reduce((sum, value) => sum + value, 0) / Math.max(1, dataArray.length);
          const normalized = Math.min(1, Math.max(0.06, average / 72));
          let activeViseme = Math.max(0, Math.min(5, Math.round(normalized * 5)));
          let activeVolume = normalized;

          if (textFrames.length) {
            const frame = getVisemeFrameAt(
              textFrames,
              playbackAudio.currentTime,
              playbackAudio.duration
            );

            activeViseme = frame.viseme;
            activeVolume =
              frame.viseme === 0 ? 0 : Math.max(normalized * 0.55, frame.intensity * 0.9);
          }

          setState((previous) => ({
            ...previous,
            speakingVolume: activeVolume,
            viseme: activeViseme,
          }));

          audioFrameRef.current = window.requestAnimationFrame(tick);
        };

        stopAudioAnalysis();
        tick();
      } catch {
        startVisemeAnimation(text, mode, tone);
      }
    },
    [startVisemeAnimation, stopAudioAnalysis]
  );

  const stopAllSpeechPlayback = useCallback(
    (nextState?: Partial<VoiceState>) => {
      stopRequestedRef.current = true;
      speechQueueRunIdRef.current += 1;
      playbackGenerationRef.current += 1;
      playbackStopResolveRef.current?.();
      playbackStopResolveRef.current = null;
      activeUtteranceRef.current = null;
      extendAssistantSpeechEchoGuard(900);
      speechQueueRef.current = [];
      processingQueueRef.current = false;
      synthRef.current?.cancel();
      hardCancelBrowserSpeech();

      if (sampleAudioRef.current) {
        sampleAudioRef.current.onended = null;
        sampleAudioRef.current.onerror = null;
        sampleAudioRef.current.pause();
        sampleAudioRef.current.currentTime = 0;
      }

      if (playbackAudioRef.current) {
        playbackAudioRef.current.onplay = null;
        playbackAudioRef.current.onended = null;
        playbackAudioRef.current.onerror = null;
        playbackAudioRef.current.pause();
        playbackAudioRef.current.currentTime = 0;
      }

      if (playbackUrlRef.current) {
        URL.revokeObjectURL(playbackUrlRef.current);
        playbackUrlRef.current = null;
      }

      stopAudioAnalysis();
      stopVisemeAnimation();
      setState((previous) => ({
        ...previous,
        isSpeaking: false,
        speakingVolume: 0,
        viseme: 0,
        ...nextState,
      }));
    },
    [extendAssistantSpeechEchoGuard, stopAudioAnalysis, stopVisemeAnimation]
  );

  const claimAudioPlayback = useCallback(() => {
    if (typeof window === 'undefined') return;
    claimAkanshaAudio(audioOwnerIdRef.current, () => stopAllSpeechPlayback());
  }, [stopAllSpeechPlayback]);

  const startListening = useCallback(() => {
    // Note the missing `!recognitionRef.current` guard. It used to be here, and
    // it is why Firefox and Safari were silently, completely deaf: those browsers
    // ship no `SpeechRecognition` at all, so this callback returned immediately
    // and the microphone button did nothing whatsoever. Local Whisper is a real
    // ear, so the absence of Chrome's recogniser is no longer a reason not to
    // listen -- it just changes which ear gets the turn.
    if (isListeningRef.current || recognitionStartingRef.current || microphoneBlockedRef.current) {
      return;
    }
    if (!recognitionRef.current && !localCaptureSupported()) return;

    const startRecognition = (attempt = 0) => {
      // No recogniser in this browser: local Whisper takes the turn outright.
      // `isListening` still goes true, because from the user's side the assistant
      // is listening -- it just will not show interim words.
      if (!recognitionRef.current) {
        manuallyStoppedRef.current = false;
        beginLocalCaptureRef.current();
        setState((previous) => ({ ...previous, isListening: true }));
        return;
      }
      try {
        manuallyStoppedRef.current = false;
        recognitionStartingRef.current = true;
        if (recognitionRef.current) {
          recognitionRef.current.lang = preferredRecognitionLanguage(
            voiceLanguage,
            recognitionLanguageIndexRef.current
          );
        }
        recognitionRef.current?.start();
      } catch {
        recognitionStartingRef.current = false;
        // The usual cause is a previous session still releasing, which clears on
        // its own — so retry instead of leaving the user looking at a mic button
        // that visibly did nothing. Bounded, because the other cause of a throw
        // (recognition already running) never clears by waiting, and the guard
        // below catches that case anyway.
        if (attempt < RECOGNITION_START_RETRIES) {
          window.setTimeout(
            () => {
              if (isListeningRef.current || manuallyStoppedRef.current) return;
              startRecognition(attempt + 1);
            },
            Math.min(150 * 2 ** attempt, 2000)
          );
        }
      }
    };

    if (navigator.mediaDevices?.getUserMedia) {
      void navigator.mediaDevices
        .getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        })
        .then((stream) => {
          void startMicFrequencyCapture(stream);
          startRecognition();
        })
        .catch(() => {
          microphoneBlockedRef.current = true;
          manuallyStoppedRef.current = true;
          setBackgroundListening(false);
        });
      return;
    }

    startRecognition();
  }, [startMicFrequencyCapture, voiceLanguage]);

  useEffect(() => {
    stopSpeechNowRef.current = (nextState?: Partial<VoiceState>) => {
      stopAllSpeechPlayback(nextState);
      if (backgroundListeningRef.current && !manuallyStoppedRef.current) {
        window.setTimeout(() => startListening(), INTERRUPTION_RESTART_DELAY_MS);
      }
    };
  }, [startListening, stopAllSpeechPlayback]);

  const stopListening = useCallback(() => {
    manuallyStoppedRef.current = true;
    recognitionStartingRef.current = false;
    if (recognitionRestartTimerRef.current) {
      window.clearTimeout(recognitionRestartTimerRef.current);
      recognitionRestartTimerRef.current = null;
    }
    recognitionRef.current?.stop();
    // A capture in flight is transcribed, not discarded. This is the end of the
    // turn, so the words the user just said are the whole point -- throwing them
    // away here is what "it heard me and did nothing" looks like from outside.
    // Deliberately not awaited: `stopListening` is called from event handlers and
    // from the silence detector, and neither can block for Whisper.
    if (localCaptureRef.current) {
      void finishLocalCaptureRef.current();
    }
    // The microphone stays open only while something is listening. Stopping the
    // capture first and the analyser second matters: `stopMicFrequencyCapture`
    // releases the stream `MediaRecorder` is reading from.
    stopMicFrequencyCapture();
    if (micSignatureRef.current) {
      setVoiceFrequencySignature(micSignatureRef.current);
    }
    setState((previous) => (previous.isListening ? { ...previous, isListening: false } : previous));
  }, [stopMicFrequencyCapture]);

  const setContinuousListening = useCallback((enabled: boolean) => {
    if (enabled) {
      microphoneBlockedRef.current = false;
      manuallyStoppedRef.current = false;
    }
    setBackgroundListening(enabled);
  }, []);

  const clearTranscript = useCallback(() => {
    setState((previous) => ({ ...previous, transcript: '', finalTranscript: '' }));
  }, []);

  const fetchSpeechBlob = useCallback(
    async (text: string, options?: SpeakOptions) => {
      const resolvedGender = options?.voiceGender ?? voiceGender;
      const resolvedTone = options?.voiceTone ?? voiceTone;
      const resolvedLanguagePreference = options?.voiceLanguage ?? voiceLanguage;
      const languageMode = detectVoiceLanguageMode(text, resolvedLanguagePreference);

      try {
        const ttsResponse = await fetch(apiUrl('/api/voice/tts'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text,
            voice_gender: resolvedGender,
            voice_tone: resolvedTone,
            language_mode: languageMode,
          }),
        });

        if (!ttsResponse.ok) {
          return null;
        }

        return await ttsResponse.blob();
      } catch {
        return null;
      }
    },
    [voiceGender, voiceLanguage, voiceTone]
  );

  const playSpeechChunk = useCallback(
    async (item: QueuedSpeechItem) => {
      if (typeof window === 'undefined' || !synthRef.current) return;
      const text = item.text;
      const options = item.options;
      const playbackId = ++playbackGenerationRef.current;
      const isCurrentPlayback = () =>
        playbackGenerationRef.current === playbackId && !stopRequestedRef.current;

      claimAudioPlayback();
      markAssistantSpeechEchoGuard(text);
      const resolvedGender = options?.voiceGender ?? voiceGender;
      synthRef.current.cancel();
      await settleBrowserSpeechCancel();
      activeUtteranceRef.current = null;
      if (sampleAudioRef.current) {
        sampleAudioRef.current.pause();
        sampleAudioRef.current.currentTime = 0;
      }
      if (playbackAudioRef.current) {
        playbackAudioRef.current.pause();
        playbackAudioRef.current.currentTime = 0;
      }
      if (playbackUrlRef.current) {
        URL.revokeObjectURL(playbackUrlRef.current);
        playbackUrlRef.current = null;
      }

      const resolvedTone = options?.voiceTone ?? voiceTone;
      const resolvedLanguagePreference = options?.voiceLanguage ?? voiceLanguage;
      const languageMode = detectVoiceLanguageMode(text, resolvedLanguagePreference);

      /**
       * Whether her real voice actually reached the speakers for this chunk.
       *
       * The fallback below exists for a server path that never produced sound.
       * But `audio.onerror` can fire *after* `onplay` — a truncated blob or a
       * decode failure part-way through a sentence — and that rejection lands in
       * the same `catch`, which then spoke the whole text again in the browser
       * voice on top of what had already been heard. This flag is what separates
       * "the server path failed, say it another way" from "the server path was
       * already speaking, so stop".
       */
      let serverAudioStarted = false;

      try {
        const blob = shouldUseFastBrowserSpeech(text, options)
          ? null
          : item.preparedAudio
            ? await item.preparedAudio
            : await fetchSpeechBlob(text, options);
        if (!isCurrentPlayback()) return;
        if (blob && playbackAudioRef.current) {
          const playbackUrl = URL.createObjectURL(blob);
          playbackUrlRef.current = playbackUrl;

          const audio = playbackAudioRef.current;
          audio.src = playbackUrl;
          audio.currentTime = 0;

          audio.onplay = () => {
            if (!isCurrentPlayback()) return;
            serverAudioStarted = true;
            markAssistantSpeechEchoGuard(text);
            setState((previous) => ({ ...previous, isSpeaking: true }));
            startPlaybackAudioAnalysis(text, languageMode, resolvedTone);
            // Announced on `play`, not on fetch: a subscriber that renders a
            // face needs the clock to be running before it starts seeking.
            for (const listener of speechAudioListenersRef.current) {
              try {
                listener({ id: `u${playbackId}`, text, blob, audio });
              } catch {
                /* a broken listener must not silence the assistant */
              }
            }
            if (backgroundListeningRef.current && !manuallyStoppedRef.current) {
              window.setTimeout(() => startListening(), INTERRUPTION_RESTART_DELAY_MS);
            }
          };

          await new Promise<void>((resolve, reject) => {
            playbackStopResolveRef.current = resolve;
            audio.onended = () => {
              playbackStopResolveRef.current = null;
              if (!isCurrentPlayback()) {
                resolve();
                return;
              }
              stopAudioAnalysis();
              stopVisemeAnimation();
              extendAssistantSpeechEchoGuard(1600);
              setState((previous) => ({
                ...previous,
                isSpeaking: false,
                speakingVolume: 0,
                viseme: 0,
              }));
              resolve();
            };

            audio.onerror = () => {
              playbackStopResolveRef.current = null;
              if (!isCurrentPlayback()) {
                resolve();
                return;
              }
              stopAudioAnalysis();
              stopVisemeAnimation();
              extendAssistantSpeechEchoGuard(900);
              setState((previous) => ({
                ...previous,
                isSpeaking: false,
                speakingVolume: 0,
                viseme: 0,
              }));
              reject(new Error('Audio playback failed'));
            };

            if (!isCurrentPlayback()) {
              resolve();
              return;
            }
            void audio.play().catch(reject);
          });
          return;
        }
      } catch {
        // fall back to browser speech synthesis below
      }

      // Three reasons not to fall back, each of which would otherwise be heard
      // as a second voice:
      //   - her voice already started, so this chunk has been said (see above);
      //   - a newer utterance or a stop has superseded this one, so speaking now
      //     would talk over whatever replaced it;
      //   - the user asked for silence.
      if (serverAudioStarted || !isCurrentPlayback()) {
        return;
      }

      const utterance = new SpeechSynthesisUtterance(text);
      activeUtteranceRef.current = utterance;
      utterance.lang =
        languageMode === 'hindi'
          ? 'hi-IN'
          : languageMode === 'telugu' || languageMode === 'mixed'
            ? 'te-IN'
            : 'en-IN';
      const matchingVoice =
        (selectedVoiceId === FEMALE_SAMPLE_VOICE_ID
          ? null
          : window.speechSynthesis
              .getVoices()
              .find(
                (voice) =>
                  voice.voiceURI === selectedVoiceId && languageMatches(voice.lang, languageMode)
              )) ??
        window.speechSynthesis
          .getVoices()
          .find(
            (voice) =>
              inferGender(voice.name) === resolvedGender &&
              languageMatches(voice.lang, languageMode)
          ) ??
        window.speechSynthesis
          .getVoices()
          .find((voice) => inferGender(voice.name) === resolvedGender) ??
        voiceRef.current;

      if (matchingVoice) {
        utterance.voice = matchingVoice;
      }

      utterance.rate = TONE_CONFIG[resolvedTone].rate;
      utterance.pitch = TONE_CONFIG[resolvedTone].pitch;
      utterance.volume = TONE_CONFIG[resolvedTone].volume;

      utterance.onstart = () => {
        if (!isCurrentPlayback() || activeUtteranceRef.current !== utterance) return;
        markAssistantSpeechEchoGuard(text);
        setState((previous) => ({ ...previous, isSpeaking: true }));
        startVisemeAnimation(text, languageMode, resolvedTone);
        if (backgroundListeningRef.current && !manuallyStoppedRef.current) {
          window.setTimeout(() => startListening(), INTERRUPTION_RESTART_DELAY_MS);
        }
      };

      await new Promise<void>((resolve, reject) => {
        playbackStopResolveRef.current = resolve;
        utterance.onend = () => {
          playbackStopResolveRef.current = null;
          if (!isCurrentPlayback() || activeUtteranceRef.current !== utterance) {
            resolve();
            return;
          }
          activeUtteranceRef.current = null;
          extendAssistantSpeechEchoGuard(1600);
          setState((previous) => ({ ...previous, isSpeaking: false }));
          stopVisemeAnimation();
          resolve();
        };

        utterance.onerror = () => {
          playbackStopResolveRef.current = null;
          if (!isCurrentPlayback() || activeUtteranceRef.current !== utterance) {
            resolve();
            return;
          }
          activeUtteranceRef.current = null;
          extendAssistantSpeechEchoGuard(900);
          setState((previous) => ({ ...previous, isSpeaking: false }));
          stopVisemeAnimation();
          reject(new Error('Speech synthesis failed'));
        };

        if (!isCurrentPlayback()) {
          resolve();
          return;
        }
        synthRef.current?.speak(utterance);
      });
    },
    [
      selectedVoiceId,
      claimAudioPlayback,
      extendAssistantSpeechEchoGuard,
      fetchSpeechBlob,
      markAssistantSpeechEchoGuard,
      startPlaybackAudioAnalysis,
      startListening,
      startVisemeAnimation,
      stopAudioAnalysis,
      stopVisemeAnimation,
      voiceGender,
      voiceLanguage,
      voiceTone,
    ]
  );

  const processSpeechQueue = useCallback(async () => {
    if (processingQueueRef.current) return;
    processingQueueRef.current = true;
    stopRequestedRef.current = false;
    const runId = ++speechQueueRunIdRef.current;

    while (
      speechQueueRunIdRef.current === runId &&
      !stopRequestedRef.current &&
      speechQueueRef.current.length > 0
    ) {
      const nextItem = speechQueueRef.current.shift();
      if (!nextItem?.text.trim()) continue;

      try {
        await playSpeechChunk(nextItem);
      } catch {
        // Skip failed chunks and keep the queue moving.
      }
    }

    if (speechQueueRunIdRef.current === runId) {
      processingQueueRef.current = false;
    }
    if (
      speechQueueRunIdRef.current === runId &&
      !stopRequestedRef.current &&
      backgroundListeningRef.current
    ) {
      window.setTimeout(() => startListening(), INTERRUPTION_RESTART_DELAY_MS);
    }
  }, [playSpeechChunk, startListening]);

  const speak = useCallback(
    async (text: string, options?: QueueSpeakOptions) => {
      if (!text.trim()) return;

      if (!options?.queue) {
        stopAllSpeechPlayback();
        claimAudioPlayback();
      }

      stopRequestedRef.current = false;
      if (options?.queue && speechQueueRef.current.length > 0) {
        const lastIndex = speechQueueRef.current.length - 1;
        const lastQueuedItem = speechQueueRef.current[lastIndex];
        if ('preferBrowser' in (lastQueuedItem.options ?? {}) || options.preferBrowser) {
          speechQueueRef.current.push({
            text,
            options,
            preparedAudio:
              options?.queue && !shouldUseFastBrowserSpeech(text, options)
                ? fetchSpeechBlob(text, options)
                : undefined,
          });
          void processSpeechQueue();
          return;
        }
        const currentGender = options.voiceGender ?? voiceGender;
        const currentTone = options.voiceTone ?? voiceTone;
        const currentLanguage = options.voiceLanguage ?? voiceLanguage;
        const lastGender = lastQueuedItem.options?.voiceGender ?? voiceGender;
        const lastTone = lastQueuedItem.options?.voiceTone ?? voiceTone;
        const lastLanguage = lastQueuedItem.options?.voiceLanguage ?? voiceLanguage;

        if (
          lastGender === currentGender &&
          lastTone === currentTone &&
          lastLanguage === currentLanguage
        ) {
          const mergedText = `${lastQueuedItem.text.trim()} ${text.trim()}`.trim();
          speechQueueRef.current[lastIndex] = {
            ...lastQueuedItem,
            text: mergedText,
            preparedAudio: shouldUseFastBrowserSpeech(mergedText, lastQueuedItem.options)
              ? undefined
              : fetchSpeechBlob(mergedText, lastQueuedItem.options),
          };
          void processSpeechQueue();
          return;
        }
      }

      speechQueueRef.current.push({
        text,
        options,
        preparedAudio:
          options?.queue && !shouldUseFastBrowserSpeech(text, options)
            ? fetchSpeechBlob(text, options)
            : undefined,
      });
      void processSpeechQueue();
    },
    [
      fetchSpeechBlob,
      claimAudioPlayback,
      processSpeechQueue,
      stopAllSpeechPlayback,
      voiceGender,
      voiceLanguage,
      voiceTone,
    ]
  );

  const stopSpeaking = useCallback(() => {
    stopAllSpeechPlayback();
    if (backgroundListeningRef.current && !manuallyStoppedRef.current) {
      window.setTimeout(() => startListening(), INTERRUPTION_RESTART_DELAY_MS);
    }
  }, [startListening, stopAllSpeechPlayback]);

  useEffect(() => {
    if (!backgroundListening) return;

    manuallyStoppedRef.current = false;
    const keepAlive = window.setInterval(() => {
      if (
        backgroundListeningRef.current &&
        !manuallyStoppedRef.current &&
        !isListeningRef.current &&
        !recognitionStartingRef.current
      ) {
        startListening();
      }
    }, LISTENING_KEEPALIVE_MS);

    return () => window.clearInterval(keepAlive);
  }, [backgroundListening, startListening]);

  const previewSelectedVoice = useCallback(() => {
    if (selectedVoiceId === FEMALE_SAMPLE_VOICE_ID && sampleAudioRef.current) {
      stopAllSpeechPlayback();
      claimAudioPlayback();
      markAssistantSpeechEchoGuard("Hi, I'm Akansha. I'm ready to talk with you naturally.");
      const sampleAudio = sampleAudioRef.current;
      sampleAudio.pause();
      sampleAudio.currentTime = 0;
      stopVisemeAnimation();
      stopAudioAnalysis();
      setState((previous) => ({ ...previous, isSpeaking: true, speakingVolume: 0, viseme: 0 }));
      startSampleAudioAnalysis();

      sampleAudio.onended = () => {
        stopAudioAnalysis();
        extendAssistantSpeechEchoGuard(1200);
        setState((previous) => ({ ...previous, isSpeaking: false, speakingVolume: 0, viseme: 0 }));
      };

      void sampleAudio.play().catch(() => {
        stopAudioAnalysis();
        extendAssistantSpeechEchoGuard(900);
        setState((previous) => ({ ...previous, isSpeaking: false, speakingVolume: 0, viseme: 0 }));
      });
      return;
    }

    speak("Hi, I'm Akansha. I'm ready to talk with you naturally.", {
      voiceGender,
      voiceTone,
    });
  }, [
    claimAudioPlayback,
    extendAssistantSpeechEchoGuard,
    markAssistantSpeechEchoGuard,
    selectedVoiceId,
    speak,
    startSampleAudioAnalysis,
    stopAllSpeechPlayback,
    stopAudioAnalysis,
    stopVisemeAnimation,
    voiceGender,
    voiceTone,
  ]);

  useEffect(() => {
    // Write-once ref (line 728), so copying it out is equivalent -- see the same
    // note in ChatThread's unmount cleanup. Kept as a copy rather than a lint
    // suppression because the pattern the rule warns about is a real bug elsewhere.
    const audioOwnerId = audioOwnerIdRef.current;
    return () => {
      releaseAkanshaAudio(audioOwnerId);
      stopAudioAnalysis();
      if (sampleAudioRef.current) {
        sampleAudioRef.current.pause();
      }
      if (playbackAudioRef.current) {
        playbackAudioRef.current.pause();
      }
      if (playbackUrlRef.current) {
        URL.revokeObjectURL(playbackUrlRef.current);
      }
      stopMicFrequencyCapture();
      if (recognitionRestartTimerRef.current) {
        window.clearTimeout(recognitionRestartTimerRef.current);
      }
    };
  }, [stopAudioAnalysis, stopMicFrequencyCapture]);

  /** Subscribe to synthesized speech as it starts playing. Returns an unsubscribe. */
  const onSpeechAudio = useCallback((listener: (event: SpeechAudioEvent) => void) => {
    speechAudioListenersRef.current.add(listener);
    return () => {
      speechAudioListenersRef.current.delete(listener);
    };
  }, []);

  return {
    ...state,
    availableVoices,
    voiceChoices,
    voiceGender,
    voiceTone,
    voiceLanguage,
    backgroundListening,
    selectedVoiceId,
    voiceFrequencySignature,
    setVoiceGender,
    setVoiceTone,
    setVoiceLanguage,
    setBackgroundListening: setContinuousListening,
    setSelectedVoiceId,
    startListening,
    stopListening,
    speak,
    stopSpeaking,
    previewSelectedVoice,
    clearTranscript,
    clearVoiceNotice,
    onSpeechAudio,
    /**
     * When the waveform monitor last saw real speech energy, as epoch ms, or 0
     * if it has not seen any.
     *
     * Exposed because the silence detector was measuring the wrong thing. It
     * timed silence from "the transcript stopped changing", which is when
     * Chrome's recogniser stopped *emitting* -- a few hundred milliseconds after
     * the user actually stopped talking, and unrelated to it entirely while the
     * recogniser is stalled. This is the microphone's own answer, from the
     * analyser that already runs for barge-in, and it costs nothing to read.
     */
    lastSpeechEnergyAt,
    /**
     * Silero's reading of the last locally transcribed turn -- trailing silence
     * measured on the waveform by the same VAD the backend uses. `null` when the
     * turn came from the browser recogniser (no audio ever reached us) or when
     * Silero is not installed. The caller must not read `null` as "no silence".
     */
    localVoiceActivity,
    /** What local hearing is capable of on this machine, once probed. */
    localHearingReport,
  };
}
