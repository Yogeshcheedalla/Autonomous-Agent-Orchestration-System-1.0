'use client';

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  CLIP_NAMES,
  GESTURE_LEAD_IN_MS,
  GESTURE_REFRACTORY_MS,
  baseClip,
  entryFractionFor,
  gestureFor,
  motionPlan,
  playableClip,
  shouldPlay,
  stanceFor,
  type ClipHealth,
  type ClipHealthMap,
  type ClipName,
} from '@/lib/presenceBehavior';

/**
 * BodyPresence — the filmed Akansha, whose non-verbal behaviour is driven by the
 * conversation rather than looping decoratively.
 *
 * The source is a single 10s performance of the same woman as the portrait
 * (720x1280, 24fps, black set). It carries real hand movement, real head turns,
 * real eye movement and a real mouth, which is why it is used at both framings
 * now: a warped photograph is phoneme-accurate and costs nothing, but it is a
 * puppet, and the person it is meant to be is available on film.
 *
 * The clip could not be used directly, for three reasons that were measured:
 *
 *   1. Its last frame differs from its first by 11.5x a normal frame step, so
 *      `<video loop>` jump-cuts every ten seconds.
 *   2. Only ~2.4s of it has the arms extended (f163-175 and f201-235). Looping
 *      the whole thing gestures on a ten-second metronome regardless of what is
 *      being said.
 *   3. Her mouth moves for most of it. Playing that while the assistant is silent
 *      is a figure mouthing words with no sound, which reads as broken.
 *
 * So it was cut offline into five behaviour loops, each built forward-then-reverse
 * over a measured frame range with the turnaround frames excluded. That makes
 * every clip seamless by definition and removes all per-frame work from the
 * runtime: no reverse playback, no `currentTime` stepping, no seeking. The
 * browser plays ordinary looping videos and this component only changes which one
 * is opaque. Verified after cutting: seam ratios 1.0x-2.4x against the original's
 * 11.5x, and zero duplicated frames.
 *
 * Reversal is not a trick, it is anatomy: a gesture is out-and-back, so a reach
 * played backwards is a retraction, which is what a hand does next.
 *
 * ── Three defects this file used to have, all of them reported as "she is just a
 * still image" ──
 *
 * **Reduced motion deleted her.** `prefers-reduced-motion: reduce` swapped the
 * whole performance for a photograph. On a machine with Windows animations turned
 * off — which is where this was found — the full-screen presence was a still
 * picture and nothing else, permanently. Motion the viewer did not ask for is
 * what that preference is about; the assistant's face is the content. Reduced
 * motion now takes away the cross-fade, the gesture interrupts and the lean-in,
 * and she keeps talking. The decision lives in `motionPlan`, which has no field
 * that can stop playback.
 *
 * **One broken file froze all five.** A single `onError` set one `failed` flag
 * for the component, so one clip that would not decode took away the four that
 * had loaded. Health is per clip now and `playableClip` substitutes the nearest
 * surviving behaviour; the still is reached only when every clip is broken.
 *
 * **There was one encoding.** The clips are H.264 only, and a Chromium build
 * without the proprietary decoder answers `canPlayType('avc1.42E01E')` with
 * "probably" and then fails the load with `MEDIA_ELEMENT_ERROR: Format error` —
 * measured, in the browser this project is developed against. Every clip now
 * ships as VP9 WebM beside the MP4, offered through two `<source>` children so
 * the browser's own source selection walks past whichever it cannot decode. MP4
 * is listed first because it is the one with hardware decode on this hardware.
 *
 * ── And one defect that came from fixing those ──
 *
 * Once she played, she played *constantly*: reported as "the voice interface is
 * continuously moving". A loop that runs while the assistant is silent is a
 * figure mouthing words with no sound, and it is also just wallpaper — movement
 * carries no information if it never stops. So playback is speech-gated
 * (`shouldPlay`), and what is on screen when she is silent is a held frame of the
 * same woman rather than a substituted photograph. Since a still figure that
 * always resumes from the same frame in the same pose is its own tell, each
 * utterance also takes a new `stanceFor(turn)` and enters the clip at
 * `entryFractionFor(turn)`.
 */

/** The five behaviour loops, with their true durations from the cut. */
const CLIPS: Record<ClipName, { base: string; ms: number }> = {
  /** f96-122. Quietest mouth in the source (2.01) with the body still alive. */
  idle: { base: '/assets/presence/akansha-idle', ms: 2167 },
  /** f30-58. Most body motion of any hands-at-rest window (2.52): attentive. */
  listening: { base: '/assets/presence/akansha-listening', ms: 2333 },
  /** f25-162. The long hands-at-rest talking pass — 5.75s, so 11.4s reversed. */
  speaking: { base: '/assets/presence/akansha-speaking', ms: 11417 },
  /** f155-172. A short emphasis beat: one hand lifts and drops. */
  beat: { base: '/assets/presence/akansha-gesture-beat', ms: 1417 },
  /** f196-234. Both palms open and held — the explaining/presenting gesture. */
  open: { base: '/assets/presence/akansha-gesture-open', ms: 3167 },
};

const POSTER = '/assets/presence/akansha-poster.webp';

/**
 * Head-and-shoulders framing, as a fraction of the 720x1280 source.
 *
 * Measured off the footage rather than guessed: her face occupies x 250-470 and
 * y 55-255, so this box is that plus the shoulders and a little headroom. The
 * region is 340x510, which is 2:3 — the panel's own aspect — so at panel size
 * nothing is stretched.
 */
const PORTRAIT_BOX = { x: 190, y: 40, w: 340, h: 510 } as const;
const SOURCE = { w: 720, h: 1280 } as const;

export interface BodyPresenceProps {
  /** Microphone is open. */
  isListening: boolean;
  /** TTS is playing. */
  isSpeaking: boolean;
  /** Model is working. Held at rest, because thinking is not a performance. */
  isThinking?: boolean;
  /** Speech amplitude 0..1 from `useVoice`, sampled at frame rate. */
  speakingVolume?: number;
  /**
   * `full` shows the whole figure — the framing for a full-screen stage, where
   * posture and hands are the performance. `portrait` crops to head and
   * shoulders for a side panel, where her mouth is large enough on screen to
   * read and her feet are not.
   */
  framing?: 'full' | 'portrait';
  className?: string;
}

function BodyPresence({
  isListening,
  isSpeaking,
  isThinking = false,
  speakingVolume = 0,
  framing = 'full',
  className = '',
}: BodyPresenceProps) {
  const [active, setActive] = useState<ClipName>('idle');
  const [reducedMotion, setReducedMotion] = useState(false);
  /**
   * Whether the film is running. False holds the current frame — see
   * `shouldPlay`: she moves while there is audio and sits still when there is
   * not, which is what was asked for and also what stops the mouth from miming
   * silence.
   */
  const [running, setRunning] = useState(false);
  /**
   * Utterance counter. Drives the stance and the entry point, so two answers in a
   * row do not open from the identical pose on the identical frame.
   */
  const [turn, setTurn] = useState(0);
  /**
   * Per clip, not per component. `unknown` is optimistic: a clip still loading is
   * shown anyway, because swapping her for a photograph during a few hundred
   * milliseconds of decode would flash on every page load.
   */
  const [health, setHealth] = useState<ClipHealthMap>({});

  const videosRef = useRef<Partial<Record<ClipName, HTMLVideoElement | null>>>({});
  const postureRef = useRef<HTMLDivElement>(null);

  // Inputs the scheduler reads. Refs rather than effect deps: `speakingVolume`
  // changes tens of times a second and the scheduler must not be torn down and
  // rebuilt at that rate, or its envelopes reset before they can detect anything.
  const volumeRef = useRef(0);
  const speakingRef = useRef(false);
  const listeningRef = useRef(false);
  const thinkingRef = useRef(false);
  const activeRef = useRef<ClipName>('idle');
  const runningRef = useRef(false);
  const turnRef = useRef(0);

  useEffect(() => {
    volumeRef.current = speakingVolume;
    speakingRef.current = isSpeaking;
    listeningRef.current = isListening;
    thinkingRef.current = isThinking;
  }, [speakingVolume, isSpeaking, isListening, isThinking]);

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReducedMotion(query.matches);
    const onChange = (event: MediaQueryListEvent) => setReducedMotion(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  const plan = useMemo(() => motionPlan(reducedMotion), [reducedMotion]);

  const mark = useCallback((name: ClipName, state: ClipHealth) => {
    setHealth((previous) => (previous[name] === state ? previous : { ...previous, [name]: state }));
  }, []);

  // ── The scheduler ─────────────────────────────────────────────────────────
  //
  // One rAF loop owns every behaviour decision. It writes React state only when
  // the visible clip actually changes — a handful of times per utterance — so the
  // volume signal never causes a render.
  //
  // Note what it is no longer gated on: `reducedMotion` used to return early here
  // and leave her frozen. Now it only narrows what the loop is allowed to do.
  useEffect(() => {
    let raf = 0;
    let prev = performance.now();
    let t = 0;

    /** Fast and slow envelopes of loudness. Emphasis is the gap between them. */
    let volFast = 0;
    let volSlow = 0;

    /** Wall-clock guards, in the loop's own `t` milliseconds. */
    let gestureUntil = 0;
    let nextGestureAt = 0;
    let speechStartedAt = -1;
    let gestureCount = 0;

    /** 0 at rest, 1 fully leaned in. Proxemics, eased rather than snapped. */
    let posture = 0;

    /** The current stance, eased toward `stanceFor(turn)`. */
    let stanceX = 0;
    let stanceY = 0;
    let stanceScale = 1;

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const dt = Math.min(64, now - prev);
      prev = now;
      t += dt;

      const speaking = speakingRef.current;
      const listening = listeningRef.current;

      if (speaking) {
        if (speechStartedAt < 0) {
          speechStartedAt = t;
          // One new stance and one new entry point per utterance, counted here
          // rather than in an effect on `isSpeaking`: this loop already owns the
          // false→true edge and an effect would fire a second time on any
          // unrelated re-render of the voice page.
          turnRef.current += 1;
          setTurn(turnRef.current);
        }
      } else {
        speechStartedAt = -1;
        gestureCount = 0;
      }

      // — is she moving at all -----------------------------------------------
      const play = shouldPlay({ speaking, listening, thinking: thinkingRef.current });
      if (runningRef.current !== play) {
        runningRef.current = play;
        setRunning(play);
      }

      // — emphasis ----------------------------------------------------------
      // Two time constants over the same signal: the fast one follows syllables,
      // the slow one is the level she has been speaking at. A rise of the first
      // above the second is a stressed syllable, which is where a gesture goes.
      // A fixed threshold instead would gesture constantly in a loud passage and
      // never in a quiet one.
      const vol = speaking ? volumeRef.current : 0;
      volFast += (vol - volFast) * (1 - Math.exp(-dt / 55));
      volSlow += (vol - volSlow) * (1 - Math.exp(-dt / 900));
      const emphasis = volFast - volSlow;

      if (
        plan.allowGestures &&
        speaking &&
        t > nextGestureAt &&
        t > gestureUntil &&
        speechStartedAt >= 0 &&
        t - speechStartedAt > GESTURE_LEAD_IN_MS
      ) {
        const gesture = gestureFor(emphasis, gestureCount);
        if (gesture) {
          gestureCount += 1;
          // Hold for the clip's own length so the hand completes its out-and-back
          // instead of being cut off mid-reach by the next state change.
          gestureUntil = t + CLIPS[gesture].ms;
          nextGestureAt = gestureUntil + GESTURE_REFRACTORY_MS;
          if (activeRef.current !== gesture) {
            activeRef.current = gesture;
            setActive(gesture);
          }
        }
      }

      // — base behaviour ----------------------------------------------------
      const gesturing = t < gestureUntil && speaking;
      if (!gesturing) {
        const base = baseClip({ speaking, listening, thinking: thinkingRef.current });
        if (activeRef.current !== base) {
          activeRef.current = base;
          setActive(base);
        }
      }

      // — proxemics and stance ----------------------------------------------
      // She closes a little distance to speak and settles back when she stops,
      // which is the one non-verbal channel a locked-off camera cannot supply
      // from the footage itself. Small on purpose: a large scale would read as a
      // zoom rather than as a step forward. Listening no longer leans — a lean
      // while the mic is open is movement with nothing behind it, and "it should
      // move only when it speaks" covers this too.
      const wanted = plan.allowProxemics && speaking ? 1 : 0;
      posture += (wanted - posture) * (1 - Math.exp(-dt / 340));
      const stance = plan.allowProxemics ? stanceFor(turnRef.current) : stanceFor(0);
      // Eased toward the turn's stance so a new pose arrives with the first words
      // rather than snapping between answers.
      stanceX += (stance.x - stanceX) * (1 - Math.exp(-dt / 520));
      stanceY += (stance.y - stanceY) * (1 - Math.exp(-dt / 520));
      stanceScale += (stance.scale - stanceScale) * (1 - Math.exp(-dt / 520));
      if (postureRef.current) {
        const scale = stanceScale * (1 + posture * 0.02);
        postureRef.current.style.transform = `scale(${scale.toFixed(5)}) translate(${stanceX.toFixed(3)}%, ${(stanceY - posture * 0.5).toFixed(3)}%)`;
      }
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [plan]);

  /**
   * The clip actually on screen, which is the wanted one unless it is broken.
   * `null` means every clip failed and only then does the still appear.
   */
  const visible = playableClip(active, health);

  // Only the visible clip decodes, and only while she is actually speaking.
  // Pausing matters twice over on this machine: five simultaneous 720x1280
  // decodes with no GPU is a real cost, and a paused element keeps its buffered
  // frames, so both switching clips and resuming are instant.
  useEffect(() => {
    if (!visible) return;
    const current = videosRef.current[visible];
    if (current) {
      if (running) {
        // Autoplay is permitted because every clip was cut with `-an` and carries
        // no audio track at all, so there is nothing for the policy to block.
        const played = current.play();
        if (played && typeof played.catch === 'function') played.catch(() => {});
      } else {
        // Not a swap to a photograph: the element holds the frame it is on, so
        // what stays on screen is this woman sitting still.
        current.pause();
      }
    }
    // Pause the rest only once the fade has finished, or the outgoing clip
    // freezes visibly while it is still half opaque.
    const timer = setTimeout(() => {
      for (const name of CLIP_NAMES) {
        if (name === visible) continue;
        videosRef.current[name]?.pause();
      }
    }, plan.fadeMs + 60);
    return () => clearTimeout(timer);
  }, [visible, running, plan.fadeMs]);

  /**
   * Enter the clip at a different point each utterance.
   *
   * Without this every answer opens on the same frame with the same head
   * movement, which is the clearest tell that it is a video and not a person.
   * `duration` is only known once metadata has loaded, hence the guard; a clip
   * that is not ready simply keeps whatever position it had.
   */
  useEffect(() => {
    if (!visible || turn === 0) return;
    const current = videosRef.current[visible];
    if (!current || !Number.isFinite(current.duration) || current.duration <= 0) return;
    current.currentTime = entryFractionFor(turn) * current.duration;
  }, [turn, visible]);

  const portrait = framing === 'portrait';

  /**
   * Portrait framing as percentages of the container rather than a `transform`,
   * so it holds at any panel size without a second measurement pass.
   */
  const frameStyle: React.CSSProperties = portrait
    ? {
        position: 'absolute',
        width: `${(SOURCE.w / PORTRAIT_BOX.w) * 100}%`,
        height: `${(SOURCE.h / PORTRAIT_BOX.h) * 100}%`,
        left: `${(-PORTRAIT_BOX.x / PORTRAIT_BOX.w) * 100}%`,
        top: `${(-PORTRAIT_BOX.y / PORTRAIT_BOX.h) * 100}%`,
        objectFit: 'fill',
        // Tailwind's preflight sets `video { max-width: 100% }`, which clamped
        // the 212% crop width back to the panel and left the height at 251% —
        // measured as a 120x452 element inside a 120x180 panel, i.e. her face
        // squeezed to half its width. The crop is deliberately larger than its
        // container, so both caps have to go.
        maxWidth: 'none',
        maxHeight: 'none',
      }
    : {
        position: 'absolute',
        inset: 0,
        width: '100%',
        height: '100%',
        // Contain, not cover. Covering a 16:9 viewport from a 9:16 source keeps
        // 32% of the frame height — chest to thigh, head and feet cropped — which
        // throws away the posture and gesture this component exists to show.
        objectFit: 'contain',
      };

  return (
    <div
      className={`relative h-full w-full overflow-hidden ${className}`}
      // Marked hidden rather than labelled: `MicControl`'s caption already
      // announces the loop state through an aria-live region, and a second
      // description of the same state would have a screen reader say it twice.
      aria-hidden="true"
      // Inspectable state. Both attributes exist because the two failures this
      // file had were indistinguishable from the outside — a frozen clip and a
      // still photograph look identical in a screenshot.
      data-presence-clip={visible ?? 'still'}
      data-presence-motion={reducedMotion ? 'reduced' : 'full'}
      // `playing` vs `held`: the difference between a person talking and the same
      // person sitting still. Exposed because "she moves all the time" and "she
      // never moves" are both things that have been reported from this component
      // and neither is visible in a screenshot.
      data-presence-running={running ? 'playing' : 'held'}
      data-presence-turn={turn}
    >
      {/* Ambient spill. The source is 9:16 and a wide viewport is not, so she is
          fitted by height with black either side. Her own backdrop is black, so
          those bands are already invisible against the page — this blurred copy
          just lets the warmth of her hair and blouse reach the edges instead of
          ending on a hard vertical line. A still image, so it costs one decode
          rather than a second video stream. */}
      {!portrait && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={POSTER}
          alt=""
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 h-full w-full select-none"
          style={{
            objectFit: 'cover',
            filter: 'blur(72px) saturate(1.25)',
            opacity: 0.45,
            transform: 'scale(1.18)',
          }}
        />
      )}

      <div ref={postureRef} className="absolute inset-0" style={{ willChange: 'transform' }}>
        {!visible && (
          // Every clip is broken. A sharp still of the same performance is the
          // honest floor: nothing moves, and nothing about it looks like a
          // failure either.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={POSTER}
            alt=""
            className="absolute inset-0 h-full w-full select-none"
            style={portrait ? frameStyle : { objectFit: 'contain' }}
          />
        )}
        {CLIP_NAMES.map((name) => (
          <video
            key={name}
            ref={(element) => {
              videosRef.current[name] = element;
            }}
            poster={POSTER}
            muted
            loop
            playsInline
            preload="auto"
            // Fires once the browser has exhausted both sources.
            onError={() => mark(name, 'broken')}
            onLoadedData={() => mark(name, 'ready')}
            className="select-none"
            style={{
              ...frameStyle,
              opacity: name === visible ? 1 : 0,
              transition: plan.fadeMs ? `opacity ${plan.fadeMs}ms linear` : 'none',
            }}
          >
            {/* Order is the fallback: the browser walks these in turn and uses
                the first it can actually decode. MP4 first for hardware decode,
                WebM for the builds that answer "probably" to H.264 and then fail
                the load. */}
            <source src={`${CLIPS[name].base}.mp4`} type="video/mp4" />
            <source src={`${CLIPS[name].base}.webm`} type="video/webm" />
          </video>
        ))}
      </div>

      {/* Grounding scrim. Also where the generator's watermark used to sit in the
          source; that is painted out in the clips themselves, but the gradient
          still keeps her feet from ending on a hard edge. */}
      {!portrait && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 bottom-0 h-40"
          style={{ background: 'linear-gradient(to top, rgba(2,6,23,0.92), transparent)' }}
        />
      )}
    </div>
  );
}

export default memo(BodyPresence);
