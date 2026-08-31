/**
 * The voice interface's own small design system.
 *
 * Every visual that carries loop state — the halo behind her, the caption under
 * her, the dot in the header, the ring on the mic button — reads from this one
 * table. They drifted apart when each of them held its own ternary: the header
 * dot went green while the caption still said "Thinking", which is a lie about
 * what the assistant is doing and the fastest way to make a face feel fake.
 *
 * Colours are `oklch` triples rather than Tailwind class names because most of
 * them end up inside a `radial-gradient`, where a class name cannot go. Stated
 * as bare components so callers can set their own alpha.
 */

export type LoopState = 'idle' | 'listening' | 'processing' | 'speaking' | 'waiting';

export interface StateTone {
  /** Bare `oklch` components, for interpolation into gradients and shadows. */
  glow: string;
  /** Tailwind text colour for the caption and header dot. */
  text: string;
  /** Tailwind border colour for the mic button ring. */
  ring: string;
  /**
   * What she is doing, in her voice, sentence case. Not a status code: the
   * caption sits under a human face, and `PROCESSING` under a human face reads
   * as a machine wearing one.
   */
  caption: string;
  /** Terser form for the header, where the caption would crowd the controls. */
  badge: string;
}

/**
 * Warm amber for speaking, cool green for listening, violet for thinking.
 *
 * The pairing is deliberate: listening and speaking are the two states the user
 * must never confuse, so they sit at opposite ends of the hue circle rather than
 * being two shades of the same cyan the old design used for both.
 */
export const STATE_TONES: Record<LoopState, StateTone> = {
  idle: {
    glow: '0.62 0.02 260',
    text: 'text-neutral-400',
    ring: 'border-white/15',
    caption: 'Tap to start talking',
    badge: 'Asleep',
  },
  listening: {
    glow: '0.78 0.16 155',
    text: 'text-emerald-300',
    ring: 'border-emerald-400/50',
    caption: 'Listening…',
    badge: 'Listening',
  },
  processing: {
    glow: '0.66 0.19 295',
    text: 'text-violet-300',
    ring: 'border-violet-400/50',
    caption: 'Thinking it through…',
    badge: 'Thinking',
  },
  speaking: {
    glow: '0.80 0.14 70',
    text: 'text-amber-200',
    ring: 'border-amber-300/50',
    caption: 'Speaking',
    badge: 'Speaking',
  },
  waiting: {
    glow: '0.72 0.06 230',
    text: 'text-sky-200/80',
    ring: 'border-sky-300/40',
    caption: 'Go ahead, I’m here',
    badge: 'Ready',
  },
};

export const LANGUAGES = [
  { key: 'english' as const, short: 'EN', label: 'English' },
  { key: 'telugu_english' as const, short: 'TE', label: 'Telugu' },
  { key: 'hindi' as const, short: 'HI', label: 'Hindi' },
];

export type VoiceLanguageKey = (typeof LANGUAGES)[number]['key'];

/**
 * Openers for the idle state, phrased as things you would actually say out loud.
 *
 * The old set — "Open YouTube", "What time is it?" — read like a syntax
 * reference, and taught the user to speak in commands to something built to
 * understand sentences.
 */
export const STARTERS = [
  'Open my GitHub notifications',
  'What’s on my calendar today?',
  'Summarise my unread email',
  'Find the failing tests in this project',
] as const;
