'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';

/**
 * A photographic face that is actually driven by the voice pipeline.
 *
 * `useVoice` already builds a real, language-aware viseme timeline — English,
 * Telugu and Hindi, with per-phoneme durations and intensities — and publishes
 * it as `viseme` on every frame of playback. Until now nothing rendered it:
 * `AssistantAvatarStage` accepted the prop and drew an arc reactor, and the live
 * `/voice-assistant` page never even destructured it. This component is the
 * consumer, so the mouth on screen is the mouth the timeline describes.
 *
 * It is a 2D puppet rig over one still photograph, not a 3D model:
 *
 *  - Each moving part is a *window* (a soft-edged mask over a landmark) holding
 *    a *copy of the portrait* which is then transformed. Because the pixels that
 *    move are the woman's own pixels, the result reads as a face rather than as
 *    a drawing pasted onto a face. The mask feathers the seam into the
 *    surrounding skin, which is why the warp is invisible at the edges.
 *  - A window cannot be its own content: `clip-path`/`mask` travel with an
 *    element's `transform`, so a single masked-and-translated div would move its
 *    own aperture and reveal nothing. Hence every part is two nested divs — the
 *    outer one masks and never moves, the inner one moves and is never masked.
 *  - All geometry is expressed as a fraction of the container, and every layer
 *    is `inset-0`, so a landmark measured once on the 1024x1536 source stays
 *    correct at every rendered size. The numbers in LANDMARKS were read off the
 *    source image directly, not guessed.
 *
 * Motion is written straight to the DOM from one requestAnimationFrame loop
 * rather than through React state. Lip sync is a ~10 Hz signal and there are a
 * dozen layers; re-rendering the tree per frame would cost more than the
 * animation is worth, and this page is also running speech recognition.
 */

const PORTRAIT = '/assets/images/akansha-presence.webp';

/** 1024x1536 source, so the box is 2:3. */
const ASPECT = 1024 / 1536;

/**
 * Facial landmarks as fractions of the container.
 *
 * Measured off the source image rather than estimated: a grid was composited
 * over it and the features read off, then the mouth and eye regions were
 * re-cropped at 4x to fix the corners to three decimal places. `L`/`R` are the
 * viewer's left and right, which is her right and left.
 */
const LANDMARKS = {
  eyeL: { cx: 0.419, cy: 0.3315 },
  eyeR: { cx: 0.573, cy: 0.3315 },
  browL: { cx: 0.417, cy: 0.3065 },
  browR: { cx: 0.577, cy: 0.305 },
  mouth: { cx: 0.498, cy: 0.4395, rx: 0.072, ry: 0.015 },
  /** Where the upper and lower lip meet — the axis the mouth opens about. */
  lipLine: 0.4372,
  /** Centre of the jaw/chin mass that drops when the mouth opens. */
  jaw: { cx: 0.5, cy: 0.462 },
} as const;

/** A mouth pose. Every field is normalised; the rig scales them to pixels. */
interface Pose {
  /** Jaw separation, 0 = closed, 1 = fully open. */
  open: number;
  /** Corner spread: +1 fully spread (a smile-like /i/), -1 fully rounded (/u/). */
  wide: number;
  /** Lips compressed against each other, for /p/ /b/ /m/. */
  press: number;
  /** Lower lip drawn back against the upper teeth, for /f/ /v/. */
  tuck: number;
}

/**
 * Viseme code -> mouth pose.
 *
 * The codes are `useVoice`'s, and they are shared by all three languages: its
 * Telugu and Hindi tables map those scripts' consonants and vowel signs onto
 * the same 0-8 range, so this one table serves every language the assistant
 * speaks. Anything the timeline classifies as silence or punctuation is 0.
 */
const POSES: Record<number, Pose> = {
  // silence, and the rest pose between words
  0: { open: 0.0, wide: 0.0, press: 0.0, tuck: 0 },
  // /a/ — the widest vowel
  1: { open: 1.0, wide: 0.1, press: 0, tuck: 0 },
  // /e/ /i/ /y/ — spread and shallow
  2: { open: 0.34, wide: 0.55, press: 0, tuck: 0 },
  // /o/ /u/ /w/ — rounded, so the corners pull in as it opens
  3: { open: 0.52, wide: -0.62, press: 0, tuck: 0 },
  // /p/ /b/ /m/ — the one viseme that must read as *shut*, not merely small
  4: { open: 0.0, wide: -0.06, press: 1.0, tuck: 0 },
  // /f/ /v/
  5: { open: 0.16, wide: 0.16, press: 0.3, tuck: 1 },
  // /t/ /d/ /n/ /l/ /r/ /s/ /z/ and th
  6: { open: 0.26, wide: 0.24, press: 0, tuck: 0 },
  // /k/ /g/ and the ch/sh/jh/gy group — open at the back
  7: { open: 0.56, wide: 0.02, press: 0, tuck: 0 },
  // everything else
  8: { open: 0.22, wide: 0.08, press: 0, tuck: 0 },
};

const REST: Pose = POSES[0];

export type PresenceEmotion = 'neutral' | 'happy' | 'thinking' | 'concerned' | 'surprised';

/** Brow shaping per emotion: lift is upward, tilt raises the inner ends. */
const BROWS: Record<PresenceEmotion, { lift: number; tilt: number }> = {
  neutral: { lift: 0, tilt: 0 },
  happy: { lift: 0.12, tilt: 0 },
  thinking: { lift: -0.1, tilt: -0.55 },
  concerned: { lift: 0.05, tilt: 0.6 },
  surprised: { lift: 0.55, tilt: 0.1 },
};

export interface HumanPresenceProps {
  /** Microphone is open. Drives attentive blinking and the listening ring. */
  isListening: boolean;
  /** TTS is playing. Gates the whole mouth rig. */
  isSpeaking: boolean;
  /** Model is working. Shows in the brows and the gaze, not just in a spinner. */
  isThinking?: boolean;
  /** Current viseme code, 0-8, from `useVoice`. */
  viseme?: number;
  /** Speech amplitude 0..1, from `useVoice`. Scales how far the mouth opens. */
  speakingVolume?: number;
  emotion?: PresenceEmotion;
  /** Rendered width. The height follows from the 2:3 source aspect. */
  width?: number;
  className?: string;
}

const clamp = (v: number, lo = 0, hi = 1) => (v < lo ? lo : v > hi ? hi : v);
const pct = (v: number) => `${(v * 100).toFixed(3)}%`;

/**
 * A feathered elliptical aperture.
 *
 * `rx`/`ry` are the radii at which the mask is still fully opaque; it fades to
 * nothing by `1 / feather` of that again. Soft edges are the whole trick — a
 * hard-edged window would show a moving rectangle of skin.
 */
function aperture(cx: number, cy: number, rx: number, ry: number, solid = 0.55): string {
  return `radial-gradient(ellipse ${pct(rx)} ${pct(ry)} at ${pct(cx)} ${pct(cy)}, #000 0%, #000 ${(solid * 100).toFixed(0)}%, transparent 100%)`;
}

export default function HumanPresence({
  isListening,
  isSpeaking,
  isThinking = false,
  viseme = 0,
  speakingVolume = 0,
  emotion = 'neutral',
  width = 300,
  className = '',
}: HumanPresenceProps) {
  const [ready, setReady] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(false);

  // Layers the animation loop writes to. Nothing here is React state: these are
  // mutated up to 60 times a second and none of them affect the tree.
  const rootRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const jawRef = useRef<HTMLDivElement>(null);
  const lipsRef = useRef<HTMLDivElement>(null);
  const cavityRef = useRef<HTMLDivElement>(null);
  const teethRef = useRef<HTMLDivElement>(null);
  const lidLRef = useRef<HTMLDivElement>(null);
  const lidRRef = useRef<HTMLDivElement>(null);
  const gazeLRef = useRef<HTMLDivElement>(null);
  const gazeRRef = useRef<HTMLDivElement>(null);
  const browLRef = useRef<HTMLDivElement>(null);
  const browRRef = useRef<HTMLDivElement>(null);

  // Inputs the loop reads. Refs, not deps: the loop must not be torn down and
  // rebuilt every time a viseme changes, or the smoothing state resets ~10
  // times a second and the mouth snaps instead of moving.
  const targetRef = useRef<Pose>(REST);
  const currentRef = useRef<Pose>({ ...REST });
  const volumeRef = useRef(0);
  const speakingRef = useRef(false);
  const listeningRef = useRef(false);
  const thinkingRef = useRef(false);
  const browRef = useRef(BROWS.neutral);

  useEffect(() => {
    targetRef.current = isSpeaking ? (POSES[viseme] ?? POSES[8]) : REST;
  }, [viseme, isSpeaking]);

  useEffect(() => {
    volumeRef.current = clamp(speakingVolume);
    speakingRef.current = isSpeaking;
    listeningRef.current = isListening;
    thinkingRef.current = isThinking;
  }, [speakingVolume, isSpeaking, isListening, isThinking]);

  useEffect(() => {
    browRef.current = BROWS[emotion] ?? BROWS.neutral;
  }, [emotion]);

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReducedMotion(query.matches);
    const onChange = (event: MediaQueryListEvent) => setReducedMotion(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  // ── The rig ────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!ready) return;

    let raf = 0;
    let t = 0;

    // Blink and gaze are scheduled rather than periodic: a face that blinks on
    // a fixed beat looks mechanical, and one that never blinks looks dead.
    let blinkPhase = 0; // 0..1..0 over a blink
    let blinkAt = 900;
    let blinking = false;
    let blinkStart = 0;

    let gazeX = 0;
    let gazeY = 0;
    let gazeToX = 0;
    let gazeToY = 0;
    let gazeAt = 1200;

    const BLINK_DOWN = 90;
    const BLINK_UP = 110;

    let prev = performance.now();
    let skip = false;

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);

      const dt = Math.min(64, now - prev);
      prev = now;
      t += dt;

      // Idle costs half the frames. While she is silent and not listening the
      // only motion is breathing, which nobody can see at 60 Hz that they
      // cannot see at 30.
      if (!speakingRef.current && !listeningRef.current) {
        skip = !skip;
        if (skip) return;
      }

      // — mouth ---------------------------------------------------------------
      // Critically damped approach rather than a jump. The timeline changes
      // viseme every 80-120 ms; snapping to each one chatters, and easing too
      // slowly smears consonants into each other. `k` here settles in ~50 ms,
      // which lands between the two.
      const target = targetRef.current;
      const cur = currentRef.current;
      const k = 1 - Math.exp(-dt / 46);
      cur.open += (target.open - cur.open) * k;
      cur.wide += (target.wide - cur.wide) * k;
      cur.press += (target.press - cur.press) * k;
      cur.tuck += (target.tuck - cur.tuck) * k;

      // Loudness modulates how far a given viseme actually opens, so a shouted
      // /a/ is wider than a muttered one instead of every /a/ being identical.
      const gain = 0.68 + 0.44 * volumeRef.current;
      const open = clamp(cur.open * gain) * (1 - cur.press * 0.9);
      const wide = cur.wide;

      if (jawRef.current) {
        // The chin dropping is the strongest cue that a mouth is open, and it
        // is the one a mouth-only rig always misses.
        const drop = Math.pow(open, 1.25);
        jawRef.current.style.transform = `translateY(${(drop * 1.85).toFixed(3)}%) scaleY(${(1 + drop * 0.032).toFixed(4)})`;
      }
      if (lipsRef.current) {
        lipsRef.current.style.transform =
          `scaleX(${(1 + wide * 0.085 - open * 0.03).toFixed(4)}) ` +
          `scaleY(${(1 + open * 0.34 - cur.press * 0.2 - cur.tuck * 0.1).toFixed(4)}) ` +
          `translateY(${(open * 0.28 - cur.tuck * 0.12).toFixed(3)}%)`;
      }
      if (cavityRef.current) {
        // Drawn over the stretched lips, not under them: scaling one lip layer
        // about the lip line covers the gap it is supposed to open, so the dark
        // interior has to be laid back on top between the two lips.
        //
        // Height is superlinear in `open`, which is the whole difference between
        // a mouth and a mail slot. Linear height gave every consonant a visible
        // dark bar (a letterbox) while still leaving /a/ too shut, because the
        // useful range is compressed at the bottom. The exponent keeps /t/ and
        // /e/ nearly closed and lets /a/ open properly.
        //
        // Width stays narrower than the lips throughout: real lips keep touching
        // at the corners and part only in the middle.
        // Capped so the gap stays inside the lip silhouette. At 0.055 a full /a/
        // grew a cavity taller than the lips had actually parted, so its lower
        // edge painted over the lower lip instead of being framed by it.
        const h = Math.pow(open, 1.45) * 0.046;
        const w = LANDMARKS.mouth.rx * 2 * (0.6 + wide * 0.14 + open * 0.1);
        const c = cavityRef.current.style;
        c.height = pct(h);
        c.width = pct(w);
        c.opacity = clamp(open * 2.4 - 0.1, 0, 0.94).toFixed(3);
      }
      if (teethRef.current) {
        // Upper teeth only once the mouth is genuinely open, and never at full
        // brightness — a hard white band across the gap looks like dentures.
        // Ramped in a little earlier than the cavity cap allows for, because a
        // gap that is pure void reads as a smudge; a hint of enamel at the top
        // edge is what makes it read as a mouth.
        teethRef.current.style.opacity = clamp((open - 0.42) * 1.2, 0, 0.5).toFixed(3);
      }

      // — blink ---------------------------------------------------------------
      // Listening blinks a little faster than idle: attentive, not sleepy.
      if (!blinking && t > blinkAt) {
        blinking = true;
        blinkStart = t;
      }
      if (blinking) {
        const e = t - blinkStart;
        if (e < BLINK_DOWN) blinkPhase = e / BLINK_DOWN;
        else if (e < BLINK_DOWN + BLINK_UP) blinkPhase = 1 - (e - BLINK_DOWN) / BLINK_UP;
        else {
          blinking = false;
          blinkPhase = 0;
          const base = listeningRef.current ? 2000 : 2600;
          blinkAt = t + base + Math.random() * (listeningRef.current ? 2600 : 3800);
        }
      }
      // Eased so the lid accelerates shut and decelerates open.
      const lid = blinkPhase * blinkPhase * (3 - 2 * blinkPhase);
      const lidTransform = `translateY(${(lid * 1.85).toFixed(3)}%)`;
      if (lidLRef.current) lidLRef.current.style.transform = lidTransform;
      if (lidRRef.current) lidRRef.current.style.transform = lidTransform;

      // — gaze ----------------------------------------------------------------
      // Saccades are small and frequent while she is speaking to you, wider and
      // slower while she is idle or thinking — thinking looks away.
      if (t > gazeAt) {
        const spread = thinkingRef.current ? 1 : speakingRef.current ? 0.42 : 0.75;
        gazeToX = (Math.random() - 0.5) * 2 * spread;
        gazeToY = (Math.random() - 0.5) * 2 * spread * (thinkingRef.current ? 1 : 0.5);
        gazeAt = t + (speakingRef.current ? 900 : 1500) + Math.random() * 2200;
      }
      const gk = 1 - Math.exp(-dt / 70);
      gazeX += (gazeToX - gazeX) * gk;
      gazeY += (gazeToY - gazeY) * gk;
      const gazeTransform = `translate(${(gazeX * 0.85).toFixed(3)}%, ${(gazeY * 0.42).toFixed(3)}%)`;
      if (gazeLRef.current) gazeLRef.current.style.transform = gazeTransform;
      if (gazeRRef.current) gazeRRef.current.style.transform = gazeTransform;

      // — brows ---------------------------------------------------------------
      const { lift, tilt } = browRef.current;
      const think = thinkingRef.current ? 1 : 0;
      const browLift = lift - think * 0.12;
      const browTilt = tilt - think * 0.4;
      if (browLRef.current) {
        browLRef.current.style.transform = `translateY(${(-browLift * 0.5).toFixed(3)}%) rotate(${(browTilt * 1.6).toFixed(2)}deg)`;
      }
      if (browRRef.current) {
        browRRef.current.style.transform = `translateY(${(-browLift * 0.5).toFixed(3)}%) rotate(${(-browTilt * 1.6).toFixed(2)}deg)`;
      }

      // — breathing and sway --------------------------------------------------
      // Two incommensurate periods, so the idle loop never visibly repeats.
      if (bodyRef.current && !reducedMotion) {
        const breathe = Math.sin(t / 668);
        const swayX = Math.sin(t / 1750) * 0.22 + Math.sin(t / 1130) * 0.1;
        const swayR = Math.sin(t / 2210) * 0.3;
        bodyRef.current.style.transform =
          `translate(${swayX.toFixed(3)}%, ${(breathe * 0.11).toFixed(3)}%) ` +
          `rotate(${swayR.toFixed(3)}deg) scale(${(1 + breathe * 0.0022).toFixed(5)})`;
      }

      // One custom property carries the level to every HUD bar, instead of
      // writing a transform per bar per frame.
      if (rootRef.current) {
        rootRef.current.style.setProperty(
          '--vol',
          (speakingRef.current ? volumeRef.current : 0).toFixed(3)
        );
      }
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [ready, reducedMotion]);

  const height = width / ASPECT;

  const state = isSpeaking
    ? 'speaking'
    : isThinking
      ? 'thinking'
      : isListening
        ? 'listening'
        : 'idle';

  const RING: Record<string, string> = {
    speaking: 'shadow-[0_0_90px_-18px_rgba(0,201,167,0.55)] ring-accent/40',
    listening: 'shadow-[0_0_90px_-18px_rgba(108,71,255,0.55)] ring-primary/40',
    thinking: 'shadow-[0_0_90px_-18px_rgba(245,158,11,0.45)] ring-amber-400/40',
    idle: 'shadow-[0_0_70px_-30px_rgba(108,71,255,0.35)] ring-white/10',
  };

  // The portrait is the background of every layer, so it is fetched and decoded
  // once and every window is guaranteed to be in register with the base.
  const portrait: React.CSSProperties = {
    position: 'absolute',
    inset: 0,
    backgroundImage: `url(${PORTRAIT})`,
    backgroundSize: '100% 100%',
    backgroundRepeat: 'no-repeat',
  };

  const bars = useMemo(() => Array.from({ length: 28 }, (_, i) => i), []);

  return (
    <div
      ref={rootRef}
      className={`relative select-none ${className}`}
      style={{ width, height }}
      role="img"
      aria-label={
        {
          speaking: 'Akansha is speaking',
          listening: 'Akansha is listening',
          thinking: 'Akansha is thinking',
          idle: 'Akansha is idle',
        }[state]
      }
    >
      {/* Preloads and decodes the portrait. The rig stays parked until this
          resolves, so the first frame is never a half-painted face. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={PORTRAIT}
        alt=""
        aria-hidden
        className="hidden"
        onLoad={() => setReady(true)}
        onError={() => setReady(true)}
      />

      <div
        className={`absolute inset-0 overflow-hidden rounded-[2rem] ring-1 transition-shadow duration-700 ${RING[state]}`}
        style={{ background: 'var(--surface-deep)', contain: 'paint' }}
      >
        <div
          ref={bodyRef}
          aria-hidden
          className="absolute inset-0"
          style={{ willChange: 'transform', transformOrigin: '50% 90%' }}
        >
          {/* 0 — the photograph */}
          <div style={portrait} />

          {/* 1 — jaw. A soft ellipse over the chin and jawline that drops as the
                  mouth opens. */}
          <div
            style={{
              position: 'absolute',
              inset: 0,
              maskImage: aperture(LANDMARKS.jaw.cx, LANDMARKS.jaw.cy, 0.175, 0.085, 0.35),
              WebkitMaskImage: aperture(LANDMARKS.jaw.cx, LANDMARKS.jaw.cy, 0.175, 0.085, 0.35),
            }}
          >
            <div
              ref={jawRef}
              style={{
                ...portrait,
                transformOrigin: `${pct(LANDMARKS.jaw.cx)} ${pct(0.4)}`,
                willChange: 'transform',
              }}
            />
          </div>

          {/* 2 — lips. Stretched about the lip line, so both lips move apart
                  from the same axis a real mouth opens about. */}
          <div
            style={{
              position: 'absolute',
              inset: 0,
              maskImage: aperture(LANDMARKS.mouth.cx, LANDMARKS.mouth.cy, 0.115, 0.036, 0.5),
              WebkitMaskImage: aperture(LANDMARKS.mouth.cx, LANDMARKS.mouth.cy, 0.115, 0.036, 0.5),
            }}
          >
            <div
              ref={lipsRef}
              style={{
                ...portrait,
                transformOrigin: `${pct(LANDMARKS.mouth.cx)} ${pct(LANDMARKS.lipLine)}`,
                willChange: 'transform',
              }}
            />
          </div>

          {/* 3 — mouth interior. Anchored near the lip line but only 15% of its
                  height above it: when a jaw drops the lower lip travels much
                  further than the upper one, and centring the gap on the lip line
                  pushes dark pixels up onto the philtrum as a grey smudge. */}
          <div
            ref={cavityRef}
            style={{
              position: 'absolute',
              left: pct(LANDMARKS.mouth.cx),
              top: pct(LANDMARKS.lipLine + 0.0016),
              width: 0,
              height: 0,
              opacity: 0,
              transform: 'translate(-50%, -15%)',
              borderRadius: '50%',
              background:
                'radial-gradient(ellipse at 50% 26%, #3d1c19 0%, #24100e 52%, #140807 100%)',
              // Tapers to nothing at the corners, so the gap closes the way lips
              // close rather than ending in two blunt points.
              maskImage: 'radial-gradient(ellipse 52% 100% at 50% 50%, #000 38%, transparent 100%)',
              WebkitMaskImage:
                'radial-gradient(ellipse 52% 100% at 50% 50%, #000 38%, transparent 100%)',
              filter: 'blur(0.5px)',
              willChange: 'width, height, opacity',
            }}
          >
            <div
              ref={teethRef}
              style={{
                position: 'absolute',
                left: '15%',
                right: '15%',
                top: 0,
                height: '30%',
                opacity: 0,
                borderRadius: '0 0 45% 45%',
                background: 'linear-gradient(#f7efe3 0%, #e6d9c8 100%)',
                filter: 'blur(0.6px)',
              }}
            />
          </div>

          {/* 4 — gaze. A tight window on each iris, so a saccade moves the eye
                  and not the eyelid or the skin around it. Under the lids. */}
          {(
            [
              [LANDMARKS.eyeL, gazeLRef],
              [LANDMARKS.eyeR, gazeRRef],
            ] as const
          ).map(([eye, ref], i) => (
            <div
              key={`gaze-${i}`}
              style={{
                position: 'absolute',
                inset: 0,
                maskImage: aperture(eye.cx, eye.cy, 0.021, 0.0115, 0.6),
                WebkitMaskImage: aperture(eye.cx, eye.cy, 0.021, 0.0115, 0.6),
              }}
            >
              <div
                ref={ref}
                style={{
                  ...portrait,
                  transformOrigin: `${pct(eye.cx)} ${pct(eye.cy)}`,
                  willChange: 'transform',
                }}
              />
            </div>
          ))}

          {/* 5 — eyelids. Sliding the portrait down inside a window over the eye
                  brings her own brow-line skin across it, which is what makes
                  the blink read as a lid rather than as a shutter. */}
          {(
            [
              [LANDMARKS.eyeL, lidLRef],
              [LANDMARKS.eyeR, lidRRef],
            ] as const
          ).map(([eye, ref], i) => (
            <div
              key={`lid-${i}`}
              style={{
                position: 'absolute',
                inset: 0,
                maskImage: aperture(eye.cx, eye.cy - 0.0015, 0.042, 0.0165, 0.62),
                WebkitMaskImage: aperture(eye.cx, eye.cy - 0.0015, 0.042, 0.0165, 0.62),
              }}
            >
              <div ref={ref} style={{ ...portrait, willChange: 'transform' }} />
            </div>
          ))}

          {/* 6 — brows. */}
          {(
            [
              [LANDMARKS.browL, browLRef],
              [LANDMARKS.browR, browRRef],
            ] as const
          ).map(([brow, ref], i) => (
            <div
              key={`brow-${i}`}
              style={{
                position: 'absolute',
                inset: 0,
                maskImage: aperture(brow.cx, brow.cy, 0.052, 0.014, 0.45),
                WebkitMaskImage: aperture(brow.cx, brow.cy, 0.052, 0.014, 0.45),
              }}
            >
              <div
                ref={ref}
                style={{
                  ...portrait,
                  transformOrigin: `${pct(brow.cx)} ${pct(brow.cy)}`,
                  willChange: 'transform',
                }}
              />
            </div>
          ))}
        </div>

        {/* Vignette, so she sits in the page instead of on it. */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              'radial-gradient(ellipse 78% 62% at 50% 38%, transparent 45%, rgba(2,6,23,0.72) 100%)',
          }}
        />

        {/* HUD, deliberately subordinate: a level meter along the bottom edge
            and nothing across her face. */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 bottom-0 flex h-16 items-end justify-center gap-[2px] px-6 pb-4"
        >
          {bars.map((i) => {
            const k = 0.35 + Math.sin(i * 1.7) * 0.3 + (i % 3) * 0.12;
            return (
              <span
                key={i}
                className={`w-[3px] rounded-full transition-colors duration-500 ${
                  state === 'speaking'
                    ? 'bg-accent/80'
                    : state === 'listening'
                      ? 'bg-primary/70'
                      : 'bg-white/15'
                }`}
                style={{
                  height: `calc(3px + var(--vol, 0) * ${(k * 34).toFixed(1)}px + ${state === 'listening' ? 2 : 0}px)`,
                  transition: 'height 90ms linear',
                }}
              />
            );
          })}
        </div>
      </div>

      {!ready && (
        <div className="absolute inset-0 animate-pulse rounded-[2rem] bg-white/5" aria-hidden />
      )}
    </div>
  );
}
