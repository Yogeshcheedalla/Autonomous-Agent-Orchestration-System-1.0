/**
 * Web Speech API — recognition half.
 *
 * TypeScript's DOM lib does not ship these: `SpeechRecognition` is still
 * vendor-prefixed in Chromium and absent from the standard lib, so every call
 * site was reaching for `(window as any).webkitSpeechRecognition` and typing its
 * event handlers `(event: any)`. That is not laziness — there was no type to use.
 *
 * This file is the type, so the `any`s can go. It declares exactly what the app
 * touches rather than the whole spec: an over-complete declaration invented from
 * the spec text would claim members Chromium does not actually implement, which
 * is worse than no type at all because it typechecks and then throws at runtime.
 *
 * No imports or exports here on purpose — that keeps it a global script file, so
 * the `Window` augmentation below merges with the real `Window` instead of
 * declaring a new unrelated interface inside a module.
 */

interface SpeechRecognitionAlternative {
  readonly transcript: string;
  readonly confidence: number;
}

interface SpeechRecognitionResult {
  readonly isFinal: boolean;
  readonly length: number;
  item(index: number): SpeechRecognitionAlternative;
  [index: number]: SpeechRecognitionAlternative;
}

interface SpeechRecognitionResultList {
  readonly length: number;
  item(index: number): SpeechRecognitionResult;
  [index: number]: SpeechRecognitionResult;
}

interface SpeechRecognitionEvent extends Event {
  readonly resultIndex: number;
  readonly results: SpeechRecognitionResultList;
}

/**
 * The documented `error` codes, as a union rather than `string`.
 *
 * Every consumer in this app branches on this value — `no-speech` restarts,
 * `not-allowed` disables the mic for the session — so a typo in one of these
 * literals silently disables a recovery path. The union turns that into a
 * compile error. `(string & {})` keeps an unrecognised code from being rejected,
 * because the list is not closed and Chromium has shipped codes not in the spec.
 */
type SpeechRecognitionErrorCode =
  | 'aborted'
  | 'audio-capture'
  | 'bad-grammar'
  | 'language-not-supported'
  | 'network'
  | 'no-speech'
  | 'not-allowed'
  | 'service-not-allowed'
  | (string & {});

interface SpeechRecognitionErrorEvent extends Event {
  readonly error: SpeechRecognitionErrorCode;
  readonly message: string;
}

interface SpeechRecognition extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  maxAlternatives: number;
  onresult: ((event: SpeechRecognitionEvent) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  /**
   * Optional on purpose. `stop()` finishes the current utterance and still fires
   * `onresult`; `abort()` discards it. Both call sites that use it guard with
   * `?.()` because it is missing on some older Chromium builds, and the type
   * should not tempt anyone into dropping that guard.
   */
  abort?(): void;
}

interface SpeechRecognitionConstructor {
  new (): SpeechRecognition;
}

interface Window {
  SpeechRecognition?: SpeechRecognitionConstructor;
  webkitSpeechRecognition?: SpeechRecognitionConstructor;
}
