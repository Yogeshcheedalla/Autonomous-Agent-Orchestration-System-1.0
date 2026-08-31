'use client';

/**
 * JarvisCore — the assistant's presence in the chat header, as an object in space.
 *
 * What this replaces, and why
 * ---------------------------
 * The header used to show a face: two white ellipses, two brows and a quadratic
 * mouth path drawn inside a flat SVG disc, with five timers driving blink, gaze,
 * brow tilt and a mouth flutter. It was reported as "remove this Akansha image and
 * set a three-dimensional jarvis-like structure", and the request is the right one
 * for a reason worth writing down: a cartoon face makes a promise about
 * *understanding* that a chat header cannot keep, and it makes it in the least
 * convincing register available. A reactor core promises attention and state, which
 * is exactly what this component can actually report.
 *
 * So there is no face here, and no photograph either. Nothing is loaded over the
 * network; the whole thing is transforms, gradients and shadows.
 *
 * Why it is genuinely 3D
 * ----------------------
 * The rings are flat circles rotated out of the screen plane inside a single
 * `preserve-3d` scene with `perspective` on the parent. That is the difference
 * between this and the usual "3D-looking" spirograph: the far half of each ring
 * really is behind the core, so it is occluded by it, and the near half really is
 * in front. Nothing between the perspective parent and the rings may set
 * `overflow`, `filter` or `opacity`, because each of those flattens the scene and
 * the illusion dies instantly -- which is why the glow is `box-shadow` rather than
 * a blurred wrapper.
 *
 * State is the only reason it moves
 * ---------------------------------
 * Four states, each visually distinct at a glance across the room:
 *
 *   idle       slow drift, breathing core -- present, not working
 *   listening  a cyan iris opens, emitted shells pulse outward, rings speed up
 *   thinking   an amber sweep crosses the core and the rings counter-rotate
 *   speaking   the core's brightness and the ring scale follow `level`
 *
 * `level` is the live speech amplitude, 0..1. When it is supplied the core is
 * driven by the actual waveform; when it is not, a small fallback flutter keeps the
 * object alive rather than freezing it mid-sentence. That distinction is inherited
 * from the panel this replaces and is worth keeping: an amplitude nobody measured
 * must not be presented as one that was.
 */

import { memo, useEffect, useRef, useState } from 'react';

export type CoreState = 'idle' | 'listening' | 'thinking' | 'speaking';

/**
 * The chat's six emotions, and the mapping onto the four states above.
 *
 * These live here rather than in `AvatarPanel` because there are two presences on
 * the page -- the expanded panel and the collapsed strip in `ChatThread` -- and they
 * were drifting: the strip had its own inline label ternary and its own hardcoded
 * violet, so "Surprised!" and the amber of `thinking` existed in one of them only.
 * One module owning the vocabulary is what keeps the collapsed and expanded views
 * the same object rather than two things that resemble each other.
 */
export type Emotion = 'neutral' | 'happy' | 'thinking' | 'surprised' | 'sad' | 'speaking';

// Literal hex, not `var(--violet-primary)`: these are composed with alpha suffixes
// (`${accent}cc`) below, and `var()` cannot be concatenated that way. They must track
// --violet-primary / --teal-accent in src/styles/tailwind.css.
export const EMOTION_COLORS: Record<Emotion, string> = {
  neutral: '#6C47FF',
  happy: '#00C9A7',
  thinking: '#F59E0B',
  surprised: '#EF4444',
  sad: '#6B7280',
  speaking: '#6C47FF',
};

export const EMOTION_LABELS: Record<Emotion, string> = {
  neutral: 'Ready',
  happy: 'Happy',
  thinking: 'Thinking...',
  surprised: 'Surprised!',
  sad: 'Empathetic',
  speaking: 'Speaking',
};

/**
 * Six emotions collapse into four core states, and the order of these tests is the
 * design.
 *
 * Listening wins outright: while the microphone is open, "I am taking this in" is
 * the only thing the user needs to know, and it is the one state where showing the
 * wrong thing has a cost -- someone who cannot tell whether they are being heard
 * stops talking mid-sentence. Speaking beats thinking for the same reason in
 * reverse: audio is already playing, so claiming to think would contradict what the
 * room can hear.
 */
export function coreStateFor(
  emotion: Emotion,
  isSpeaking: boolean,
  isListening: boolean
): CoreState {
  if (isListening) return 'listening';
  if (isSpeaking || emotion === 'speaking') return 'speaking';
  if (emotion === 'thinking') return 'thinking';
  return 'idle';
}

/**
 * What the badge says. `listening` is not an emotion -- it is a state of the
 * microphone -- so it cannot come out of `EMOTION_LABELS`, and it has to win for the
 * same reason it wins in `coreStateFor`.
 */
export function presenceLabel(emotion: Emotion, isListening: boolean): string {
  return isListening ? 'Listening...' : EMOTION_LABELS[emotion];
}

export interface JarvisCoreProps {
  state: CoreState;
  /** Hex accent, e.g. `#6C47FF`. Hex specifically: the `${c}33` alpha suffix below. */
  accent: string;
  /** Live speech amplitude 0..1. Omit when nothing is measuring it. */
  level?: number;
  /** Outer diameter in px. The whole assembly is proportional to this. */
  size?: number;
  className?: string;
}

/** The four rings: axis, tilt out of plane, period, thickness, inset, colour role. */
const RINGS = [
  { jx: 1, jy: 0.15, jz: 0, tilt: 74, period: 9, width: 1.5, inset: 0, hue: 'accent' },
  { jx: 0.2, jy: 1, jz: 0, tilt: 68, period: 13, width: 1, inset: 0.07, hue: 'cyan' },
  { jx: 1, jy: 0.6, jz: 0.2, tilt: 58, period: 7, width: 1, inset: 0.15, hue: 'accent' },
  { jx: 0.4, jy: 1, jz: 0.3, tilt: 82, period: 17, width: 2, inset: 0.22, hue: 'cyan' },
] as const;

const CYAN = '#22d3ee';

/** Motes on the equator. Deterministic: a random phase would differ per render. */
const MOTES = [
  { phase: 0, radius: 0.46, period: 6, size: 3 },
  { phase: 72, radius: 0.5, period: 8, size: 2 },
  { phase: 144, radius: 0.42, period: 5, size: 2.5 },
  { phase: 216, radius: 0.52, period: 11, size: 2 },
  { phase: 288, radius: 0.44, period: 7, size: 3 },
] as const;

function JarvisCore({ state, accent, level, size = 132, className = '' }: JarvisCoreProps) {
  const [flutter, setFlutter] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  // The fallback amplitude, and only the fallback. A measured `level` is the
  // animation -- the prop changing is the frame -- so the timer must not run
  // alongside it, or the two fight and the core stutters.
  useEffect(() => {
    if (state !== 'speaking' || typeof level === 'number') {
      if (timer.current) clearInterval(timer.current);
      setFlutter(0);
      return;
    }
    timer.current = setInterval(() => setFlutter(Math.random()), 110);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [state, level]);

  const amplitude =
    state === 'speaking'
      ? Math.max(0, Math.min(1, typeof level === 'number' ? level : flutter))
      : 0;

  const active = state !== 'idle';
  // Rings speed up when there is something to do. One multiplier rather than four
  // periods, so their relative rhythm -- which is what stops it looking mechanical
  // -- survives the state change.
  const rate = state === 'thinking' ? 0.45 : state === 'listening' ? 0.7 : active ? 0.6 : 1;
  const coreScale = 1 + amplitude * 0.16;
  const glow = 0.35 + amplitude * 0.5 + (active ? 0.15 : 0);

  return (
    <div
      className={`relative select-none ${className}`}
      style={{ width: size, height: size, perspective: size * 3.2 }}
      // The face this replaces was decorative and unlabelled. The core reports
      // state, so it is worth exposing: a screen reader user gets the same
      // information a sighted user gets from the colour.
      role="img"
      aria-label={`Assistant ${state}`}
    >
      {/* Ambient bloom. Sits *behind* the scene and outside it, so its blur cannot
          flatten the 3D context. */}
      <div
        aria-hidden
        className="absolute rounded-full"
        style={{
          inset: -size * 0.18,
          background: `radial-gradient(circle at 50% 50%, ${accent}55 0%, ${CYAN}22 42%, transparent 72%)`,
          filter: `blur(${size * 0.09}px)`,
          opacity: glow,
          transition: 'opacity 160ms linear',
        }}
      />

      {/* Emitted shells — energy leaving the object while it listens or speaks. */}
      {active && state !== 'thinking' && (
        <>
          {[0, 1].map((index) => (
            <div
              key={`shell-${index}`}
              aria-hidden
              className="absolute inset-0 rounded-full border"
              style={{
                borderColor: state === 'listening' ? CYAN : accent,
                animation: `jarvisEmit ${state === 'listening' ? 1.8 : 1.2}s ease-out infinite`,
                animationDelay: `${index * (state === 'listening' ? 0.9 : 0.6)}s`,
              }}
            />
          ))}
        </>
      )}

      {/* The 3D scene. Everything below here shares one coordinate space. */}
      <div className="jarvis-scene absolute inset-0">
        {RINGS.map((ring, index) => (
          <div
            key={`ring-${index}`}
            aria-hidden
            className="jarvis-ring"
            style={{
              inset: `${ring.inset * size}px`,
              borderStyle: 'solid',
              borderWidth: ring.width,
              borderColor: ring.hue === 'cyan' ? `${CYAN}cc` : `${accent}cc`,
              boxShadow: `0 0 ${8 + amplitude * 14}px ${
                ring.hue === 'cyan' ? `${CYAN}66` : `${accent}66`
              }`,
              // Dashed on the outer two only: all four dashed reads as noise, all
              // four solid reads as a gyroscope toy.
              borderTopColor: index < 2 ? 'transparent' : undefined,
              animationName: index % 2 ? 'jarvisSpinReverse' : 'jarvisSpin',
              animationDuration: `${ring.period * rate}s`,
              ['--jx' as string]: ring.jx,
              ['--jy' as string]: ring.jy,
              ['--jz' as string]: ring.jz,
              ['--jtilt' as string]: `${ring.tilt}deg`,
            }}
          />
        ))}

        {/* Motes, on the scene's equator so they cross in front of and behind. */}
        {MOTES.map((mote, index) => (
          <div
            key={`mote-${index}`}
            aria-hidden
            // `jarvis-mote` carries no styling of its own -- it exists so the
            // reduced-motion block in tailwind.css can restore the orbit's first
            // frame. Cancelling `jarvisOrbit` also cancels the `translateX(radius)`
            // that puts the mote in orbit, and five motes stacked at dead centre is
            // not the same object holding still.
            className="jarvis-mote absolute rounded-full"
            style={{
              left: '50%',
              top: '50%',
              width: mote.size,
              height: mote.size,
              marginLeft: -mote.size / 2,
              marginTop: -mote.size / 2,
              background: index % 2 ? CYAN : accent,
              boxShadow: `0 0 6px ${index % 2 ? CYAN : accent}`,
              animation: `jarvisOrbit ${mote.period * rate}s linear infinite`,
              ['--phase' as string]: `${mote.phase}deg`,
              ['--radius' as string]: `${mote.radius * size}px`,
            }}
          />
        ))}

        {/* The core. A sphere, not a disc: the off-centre highlight and the inset
            shadow on the opposite side are what make it read as volume. */}
        <div
          aria-hidden
          className="absolute rounded-full overflow-hidden"
          style={{
            inset: size * 0.29,
            transform: `translateZ(${size * 0.04}px) scale(${coreScale})`,
            transition: 'transform 90ms linear',
            background: `radial-gradient(circle at 34% 30%, #ffffff 0%, ${CYAN} 18%, ${accent} 58%, #0b1020 100%)`,
            boxShadow: `inset -${size * 0.05}px -${size * 0.06}px ${size * 0.1}px #0b102099,
                        inset ${size * 0.02}px ${size * 0.02}px ${size * 0.05}px ${CYAN}55,
                        0 0 ${14 + amplitude * 26}px ${accent}aa`,
            animation: state === 'idle' ? 'jarvisBreathe 3.6s ease-in-out infinite' : undefined,
          }}
        >
          {/* Thinking sweep — a scanline crossing the core, clipped by it. This is
              why the core carries `overflow-hidden`: it is a leaf, so flattening
              here costs nothing. */}
          {state === 'thinking' && (
            <div
              className="absolute left-0 h-1/3 w-full"
              style={{
                top: '33%',
                background: `linear-gradient(to bottom, transparent, ${accent}, transparent)`,
                animation: 'jarvisScan 1.5s ease-in-out infinite',
              }}
            />
          )}
          {/* Listening iris — an aperture, so "I am taking this in" is a shape and
              not just a colour change. */}
          {state === 'listening' && (
            <div
              className="absolute rounded-full border-2"
              style={{
                inset: '22%',
                borderColor: `${CYAN}dd`,
                boxShadow: `0 0 10px ${CYAN}, inset 0 0 12px ${CYAN}88`,
                animation: 'jarvisBreathe 1.4s ease-in-out infinite',
              }}
            />
          )}
        </div>

        {/* Reticle ticks on the outer shell — the HUD register. Four marks, at the
            cardinal points, in the scene so they tilt with it. */}
        {[0, 90, 180, 270].map((angle) => (
          <div
            key={`tick-${angle}`}
            aria-hidden
            className="absolute left-1/2 top-1/2"
            style={{
              width: 1.5,
              height: size * 0.07,
              marginLeft: -0.75,
              background: CYAN,
              opacity: 0.75,
              boxShadow: `0 0 5px ${CYAN}`,
              transformOrigin: '50% 0',
              transform: `rotateZ(${angle}deg) translateY(${size * 0.4}px)`,
            }}
          />
        ))}
      </div>
    </div>
  );
}

JarvisCore.displayName = 'JarvisCore';

export default memo(JarvisCore);
