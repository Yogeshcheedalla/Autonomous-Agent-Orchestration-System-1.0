'use client';

import { memo } from 'react';

import { STATE_TONES, type LoopState } from '../voiceDesign';

interface Props {
  loopState: LoopState;
  active: boolean;
  volume: number;
  onToggle: () => void;
  reducedMotion: boolean;
  /** Whether the mic stays open while idle, waiting for "hey Akansha". */
  wakeWordEnabled: boolean;
  onToggleWakeWord: () => void;
  /**
   * Full-screen presence. Drops the written caption and the wake-word label so
   * the only things over her are two icons.
   *
   * The state still has to be legible without that text, which is why the mic
   * button grows and the wake-word control keeps its coloured pulsing dot: in
   * the panel the words carry the state and the colour supports them, and here
   * that has to invert.
   */
  minimal?: boolean;
}

/**
 * The one control that starts and stops her.
 *
 * It exists because the old design made *her face* the button. That was wrong for
 * three reasons, only one of them cosmetic: a portrait gives no hint that it is
 * clickable, a 286×429 hit target that toggles the microphone is trivially easy
 * to hit by accident while reading, and wrapping a live `<canvas>` in a `<button>`
 * makes the accessible name of the control the alt text of a photograph.
 *
 * So the face is a face, and this is the button.
 */
function MicControl({
  loopState,
  active,
  volume,
  onToggle,
  reducedMotion,
  wakeWordEnabled,
  onToggleWakeWord,
  minimal = false,
}: Props) {
  const tone = STATE_TONES[loopState];
  const swell = loopState === 'speaking' && !reducedMotion ? volume : 0;

  return (
    <div className="flex flex-col items-center gap-3">
      <button
        type="button"
        onClick={onToggle}
        aria-label={active ? 'Stop listening' : 'Start listening'}
        aria-pressed={active}
        className={`group relative grid place-items-center rounded-full border-2 bg-white/[0.04] backdrop-blur-sm transition-[border-color,background-color,transform] duration-200 hover:scale-[1.04] hover:bg-white/[0.09] active:scale-[0.97] focus:outline-none focus-visible:ring-2 focus-visible:ring-white/60 focus-visible:ring-offset-2 focus-visible:ring-offset-black ${minimal ? 'size-20' : 'size-16'} ${tone.ring}`}
        style={{ touchAction: 'manipulation' }}
      >
        {/* Volume bloom. A sibling rather than a box-shadow on the button so the
            audio envelope drives `transform` and never a paint of the border. */}
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 rounded-full will-change-transform"
          style={{
            transform: `scale(${1 + swell * 0.55})`,
            opacity: swell * 0.5,
            background: `radial-gradient(circle, oklch(${tone.glow} / 0.5) 0%, transparent 70%)`,
            transition: 'transform 140ms ease-out, opacity 140ms ease-out',
          }}
        />
        {active ? (
          // Stop: a square, because a second microphone glyph with a slash
          // through it reads as "microphone is broken" rather than "tap to stop".
          <svg
            viewBox="0 0 24 24"
            className={`text-neutral-100 ${minimal ? 'size-6' : 'size-5'}`}
            aria-hidden="true"
          >
            <rect x="7" y="7" width="10" height="10" rx="2.5" fill="currentColor" />
          </svg>
        ) : (
          <svg
            viewBox="0 0 24 24"
            className={`text-neutral-200 transition-colors group-hover:text-white ${minimal ? 'size-7' : 'size-6'}`}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.7"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M12 4.5a2.75 2.75 0 0 1 2.75 2.75v4.25a2.75 2.75 0 0 1-5.5 0V7.25A2.75 2.75 0 0 1 12 4.5Z" />
            <path d="M6.5 11.5a5.5 5.5 0 0 0 11 0M12 17v2.5" />
          </svg>
        )}
      </button>

      {/* The caption is the only place the loop state is stated in words, so a
          user who cannot see the halo still hears it change. `polite` so it
          waits for a pause instead of cutting across her own speech.

          Kept in the tree in full screen rather than removed: the request there
          was no *visible* text over her, and deleting the live region would take
          the state away from a screen reader too. `sr-only` satisfies both. */}
      <p
        className={minimal ? 'sr-only' : `text-sm transition-colors duration-300 ${tone.text}`}
        aria-live="polite"
      >
        {tone.caption}
      </p>

      {/* Hands-free arming.
          A separate control from the mic button, not a mode of it, because the
          two answer different questions: the button is "listen now", this is
          "listen for your name from now on". Folding them together would mean
          every tap-to-talk also left an always-on recogniser running, which is
          both a battery cost and a microphone-permission surprise.

          `aria-pressed` rather than a checkbox: it toggles a behaviour that is
          already in effect, so it is a button with a state, not a form field. */}
      <button
        type="button"
        onClick={onToggleWakeWord}
        aria-pressed={wakeWordEnabled}
        aria-label={
          wakeWordEnabled ? 'Turn off "Hey Akansha" wake word' : 'Turn on "Hey Akansha" wake word'
        }
        title={wakeWordEnabled ? '"Hey Akansha" is on' : '"Hey Akansha" is off'}
        className={`flex items-center gap-2 rounded-full border text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-white/60 focus-visible:ring-offset-2 focus-visible:ring-offset-black ${
          minimal ? 'size-9 justify-center' : 'px-3 py-1.5'
        } ${
          wakeWordEnabled
            ? 'border-emerald-400/40 bg-emerald-400/10 text-emerald-200 hover:bg-emerald-400/20'
            : 'border-white/15 bg-white/[0.04] text-neutral-400 hover:bg-white/[0.09] hover:text-neutral-200'
        }`}
      >
        <span
          aria-hidden="true"
          className={`rounded-full ${minimal ? 'size-2' : 'size-1.5'} ${
            wakeWordEnabled ? 'animate-pulse bg-emerald-400' : 'bg-neutral-600'
          }`}
        />
        {!minimal && (
          <>
            &ldquo;Hey Akansha&rdquo;
            <span className="text-neutral-500">{wakeWordEnabled ? 'on' : 'off'}</span>
          </>
        )}
      </button>
    </div>
  );
}

export default memo(MicControl);
