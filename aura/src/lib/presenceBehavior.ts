/**
 * The decisions behind Akansha's non-verbal behaviour, with no React, no DOM and
 * no video element in sight.
 *
 * `BodyPresence` used to hold all of this inline, which meant none of it could be
 * tested: every rule lived inside a `requestAnimationFrame` closure that needs a
 * browser, five decoding videos and a live audio analyser before it will run
 * once. The rules are the part that was wrong, so they are the part that had to
 * become checkable. Everything here takes plain values and returns plain values,
 * so `backend/test_audit_regressions.py` can drive the real shipped module
 * through Node — the same discipline `src/lib/wakeWord.ts` is held to, and for
 * the same reason: a Python re-implementation would encode what I believe the
 * rules are and pass while the shipped file was broken.
 *
 * Two defects are pinned here rather than described:
 *
 *  1. **Reduced motion used to delete her.** `prefers-reduced-motion: reduce`
 *     replaced the whole performance with a still photograph — no hands, no
 *     mouth, no eyes. That reading is wrong. The preference exists to stop
 *     decorative movement that a person did not ask for; the assistant's face is
 *     not decoration, it is the content of the page. So reduced motion now
 *     removes the parts that *are* decorative — cross-fades, gesture interrupts,
 *     the lean-in — and she keeps breathing, blinking and speaking. There is
 *     deliberately no field in `MotionPlan` that can stop playback, because the
 *     bug was a boolean that could.
 *  2. **One broken clip used to freeze all five.** A single `onError` set one
 *     `failed` flag for the whole component, so one missing or undecodable file
 *     took away every behaviour including the four that had loaded. Health is now
 *     per clip, and `playableClip` substitutes the nearest surviving behaviour;
 *     the still frame is reached only when nothing at all can play.
 */

/** The five cut behaviour loops. Names are the contract with `BodyPresence`. */
export type ClipName = 'idle' | 'listening' | 'speaking' | 'beat' | 'open';

/**
 * What is known about one clip's ability to play.
 *
 * `unknown` is optimistic on purpose: a clip that has not finished loading is
 * still shown, because `preload="auto"` plus a decode is a few hundred
 * milliseconds and swapping her out for a photograph during that window would
 * flash on every page load.
 */
export type ClipHealth = 'unknown' | 'ready' | 'broken';

export type ClipHealthMap = Partial<Record<ClipName, ClipHealth>>;

export interface PresenceInputs {
  /** TTS is playing. */
  speaking: boolean;
  /** The microphone is open. */
  listening: boolean;
  /** The model is working. */
  thinking: boolean;
}

/**
 * Which parts of the performance are allowed to move.
 *
 * Note what is absent: nothing here says "show a still instead". A reduced-motion
 * reader still gets a person, at rest.
 */
export interface MotionPlan {
  /** Cross-fade duration between clips. 0 means swap on the frame. */
  fadeMs: number;
  /** Whether an emphasis peak may interrupt the base behaviour with a gesture. */
  allowGestures: boolean;
  /** Whether she leans in slightly to speak and settles back afterwards. */
  allowProxemics: boolean;
}

/** Cross-fade length. Long enough to hide a pose change, short enough that a
 *  gesture still lands on the word that triggered it. */
export const FADE_MS = 240;

/** Minimum gap between gestures. People gesture in bursts every few seconds,
 *  not on every stressed syllable. */
export const GESTURE_REFRACTORY_MS = 2600;

/** Emphasis must exceed the running baseline by this much to earn a gesture. */
export const EMPHASIS_THRESHOLD = 0.055;

/** A strong peak gets both palms; a moderate one gets the short beat. */
export const STRONG_EMPHASIS = 0.115;

/** Speech has to be under way before she gestures about it. */
export const GESTURE_LEAD_IN_MS = 420;

export function motionPlan(reducedMotion: boolean): MotionPlan {
  return reducedMotion
    ? { fadeMs: 0, allowGestures: false, allowProxemics: false }
    : { fadeMs: FADE_MS, allowGestures: true, allowProxemics: true };
}

/**
 * Whether the film should be running at all.
 *
 * Reported by the person using this: *"the voice interface is continuously
 * moving. When it only speaks, then only it should move"*. They are right, and
 * for a reason beyond taste: the source has her mouth moving through most of it,
 * so a loop that runs while the assistant is silent is a figure mouthing words
 * with no sound. Playing it only while there is audio makes the mouth *mean*
 * something — it is not lipsync, but it is speech-locked, and the difference
 * between "moving now" and "moving always" is the difference between a person who
 * just started talking and a video wallpaper.
 *
 * When it returns false the clip is paused, not swapped: a held frame of the same
 * woman in the same light, which is a person sitting still rather than a
 * photograph substituted for one. `listening` deliberately does not qualify — it
 * used to, and the movement it produced is exactly what was complained about.
 */
export function shouldPlay({ speaking }: PresenceInputs): boolean {
  return speaking;
}

/**
 * The behaviour she holds when nothing is interrupting.
 *
 * `thinking` deliberately rests. A figure gesturing at nothing while it waits on
 * a model reads as stalling, and the amber halo already says it is working.
 */
export function baseClip({ speaking, listening }: PresenceInputs): ClipName {
  if (speaking) return 'speaking';
  if (listening) return 'listening';
  return 'idle';
}

/**
 * Which gesture an emphasis peak earns, or `null` for none.
 *
 * Two rules, and the order matters. A genuinely strong peak always gets both
 * palms, because that is the gesture that carries weight. Everything else walks a
 * fixed cycle, which is what makes a long answer stop looking like a metronome:
 * the same clip on every stressed syllable reads as a tic, and only having two
 * gestures makes that worse, not better. The cycle opens on `open` so the first
 * gesture of a turn is the presenting one — "here is the thing I am about to
 * explain" — and then alternates with a bias toward the short beat, because a
 * held two-palm gesture three times in a row is a lecture.
 */
export const GESTURE_CYCLE: readonly ClipName[] = ['open', 'beat', 'open', 'beat', 'beat'];

export function gestureFor(emphasis: number, gestureCount: number): ClipName | null {
  if (emphasis <= EMPHASIS_THRESHOLD) return null;
  if (emphasis > STRONG_EMPHASIS) return 'open';
  return GESTURE_CYCLE[gestureCount % GESTURE_CYCLE.length];
}

/**
 * Where she sits in the frame for one spoken turn.
 *
 * The camera that shot the source never moved, so every utterance starts from
 * the identical pose unless something varies it. These are small offsets — a
 * fraction of the frame — applied for the length of a turn and eased into, so the
 * effect is that she has shifted in her seat between answers rather than that the
 * shot has changed. Percentages of the container, not pixels, so a 120px panel
 * and a full screen get the same proportion of movement.
 *
 * Deliberately not random. A random walk drifts, and over a long conversation it
 * drifts somewhere wrong; a fixed cycle returns.
 */
export interface Stance {
  /** Horizontal offset, percent of frame width. */
  x: number;
  /** Vertical offset, percent of frame height. */
  y: number;
  /** Scale multiplier. Small: past ~1.05 it reads as a zoom, not a shift. */
  scale: number;
}

export const STANCES: readonly Stance[] = [
  { x: 0, y: 0, scale: 1 },
  { x: -1.4, y: 0.3, scale: 1.016 },
  { x: 1.2, y: -0.4, scale: 1.032 },
  { x: -0.6, y: -0.7, scale: 1.045 },
];

export function stanceFor(turn: number): Stance {
  const index = ((turn % STANCES.length) + STANCES.length) % STANCES.length;
  return STANCES[index];
}

/**
 * Where in the talking clip a turn begins, as a fraction of its length.
 *
 * The speaking loop is 11.4s of one performance. Entering it at 0 every time
 * means every answer opens with the same head movement on the same word, which is
 * the single clearest tell that this is a video. Entering at a different point per
 * turn costs one `currentTime` write and removes it.
 *
 * The fractions avoid the last third: that is where the reversal turnaround sits,
 * and starting inside it means the first thing a viewer sees is a movement playing
 * backwards.
 */
export const ENTRY_POINTS: readonly number[] = [0, 0.22, 0.44, 0.12, 0.33];

export function entryFractionFor(turn: number): number {
  const index = ((turn % ENTRY_POINTS.length) + ENTRY_POINTS.length) % ENTRY_POINTS.length;
  return ENTRY_POINTS[index];
}

/**
 * Nearest surviving behaviour for each clip, most similar first.
 *
 * Similarity is about what the viewer reads, not about file size: a talking pass
 * substitutes for a gesture because both have her mouth moving, and `idle`
 * substitutes for `listening` because both are hands-at-rest.
 */
const SUBSTITUTES: Record<ClipName, readonly ClipName[]> = {
  speaking: ['open', 'beat', 'idle', 'listening'],
  listening: ['idle', 'speaking', 'open', 'beat'],
  idle: ['listening', 'speaking', 'open', 'beat'],
  beat: ['open', 'speaking', 'idle', 'listening'],
  open: ['beat', 'speaking', 'idle', 'listening'],
};

export const CLIP_NAMES = Object.keys(SUBSTITUTES) as ClipName[];

/**
 * The clip to actually show, or `null` when every clip is broken.
 *
 * `null` is the only route to the still frame. Before this existed, one
 * undecodable file meant a photograph and no movement at all — which is exactly
 * what a viewer reported seeing full-screen.
 */
export function playableClip(wanted: ClipName, health: ClipHealthMap): ClipName | null {
  if (health[wanted] !== 'broken') return wanted;
  for (const candidate of SUBSTITUTES[wanted]) {
    if (health[candidate] !== 'broken') return candidate;
  }
  return null;
}

/** Whether any clip at all can still play. */
export function anyClipPlayable(health: ClipHealthMap): boolean {
  return CLIP_NAMES.some((name) => health[name] !== 'broken');
}
