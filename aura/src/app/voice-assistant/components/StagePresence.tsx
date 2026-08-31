'use client';

import { memo } from 'react';

import { STATE_TONES, type LoopState } from '../voiceDesign';

interface Props {
  loopState: LoopState;
  /** 0–1 speech envelope. Only read while speaking. */
  volume: number;
  reducedMotion: boolean;
}

/**
 * The light behind her.
 *
 * Two layers: a soft bloom that carries the state colour, and a thin contour that
 * traces her silhouette. It replaces three concentric rings that were sized for a
 * 128px orb and, at portrait size, either cut across her shoulders or floated
 * clear of her head.
 *
 * Everything animates through `transform` and `opacity` only — `scale` on the
 * bloom rather than `width`/`height`, which is what the old version animated and
 * what forced a layout pass on every audio frame. At 60fps against a canvas
 * that is a visible cost, not a theoretical one.
 */
function StagePresence({ loopState, volume, reducedMotion }: Props) {
  const tone = STATE_TONES[loopState];

  // Only speech breathes. If every state pulsed, "she is talking" and "she is
  // waiting" would look the same, and the halo would stop carrying information.
  const swell = loopState === 'speaking' && !reducedMotion ? volume : 0;
  const alive = !reducedMotion && (loopState === 'listening' || loopState === 'processing');

  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
      {/* Sized relative to the stage rather than in fixed `rem`. The portrait is
          capped in `vh` so it shrinks on a short screen, and a fixed halo would
          detach from her outline exactly when it did. */}
      <div
        aria-hidden="true"
        className="absolute h-[112%] w-[128%] rounded-full blur-[64px] will-change-transform"
        style={{
          transform: `scale(${1 + swell * 0.18})`,
          transition: 'transform 220ms cubic-bezier(0.22, 1, 0.36, 1), opacity 400ms ease',
          opacity: loopState === 'idle' ? 0.45 : 1,
          background: `radial-gradient(ellipse at 50% 40%,
            oklch(${tone.glow} / 0.34) 0%,
            oklch(${tone.glow} / 0.10) 52%,
            transparent 76%)`,
        }}
      />
      <div
        aria-hidden="true"
        className={`absolute h-[105%] w-[112%] rounded-[46%] border will-change-transform ${
          alive ? 'animate-breathe' : ''
        }`}
        style={{
          transform: `scale(${1 + swell * 0.06})`,
          transition: 'transform 220ms cubic-bezier(0.22, 1, 0.36, 1), border-color 400ms ease',
          borderColor: `oklch(${tone.glow} / ${loopState === 'idle' ? 0.14 : 0.4})`,
        }}
      />
    </div>
  );
}

export default memo(StagePresence);
