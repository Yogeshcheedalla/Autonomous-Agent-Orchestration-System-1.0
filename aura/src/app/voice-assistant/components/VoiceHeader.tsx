'use client';

import { memo } from 'react';

import { LANGUAGES, STATE_TONES, type LoopState, type VoiceLanguageKey } from '../voiceDesign';

interface Props {
  loopState: LoopState;
  active: boolean;
  voiceLanguage: string;
  onLanguageChange: (key: VoiceLanguageKey) => void;
  showTranscript: boolean;
  onToggleTranscript: () => void;
  /** False until mounted; gates anything read from client-only state. */
  mounted: boolean;
  reducedMotion: boolean;
}

/**
 * Top bar: who she is, what she is doing, what language she is hearing.
 *
 * The language control is a `radiogroup` rather than three loose buttons. Three
 * buttons where exactly one is always on is a radio group whether or not it is
 * marked as one — and unmarked, a screen reader user gets three unrelated
 * toggles with no indication that picking one drops another.
 */
function VoiceHeader({
  loopState,
  active,
  voiceLanguage,
  onLanguageChange,
  showTranscript,
  onToggleTranscript,
  mounted,
  reducedMotion,
}: Props) {
  const tone = STATE_TONES[loopState];

  return (
    <header className="relative z-10 flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.06] px-4 py-3 sm:px-6">
      <div className="flex min-w-0 items-center gap-2.5">
        <span
          aria-hidden="true"
          className={`size-2 shrink-0 rounded-full transition-colors duration-300 ${
            active && !reducedMotion ? 'animate-pulse' : ''
          }`}
          style={{ background: `oklch(${tone.glow} / ${active ? 1 : 0.35})` }}
        />
        <h1 className="truncate text-sm font-medium tracking-tight text-neutral-100">
          <span translate="no">Akansha</span>
        </h1>
        <span
          className={`shrink-0 text-xs transition-colors duration-300 ${tone.text}`}
          suppressHydrationWarning
        >
          {mounted ? tone.badge : ''}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <div
          role="radiogroup"
          aria-label="Recognition language"
          className="flex items-center gap-0.5 rounded-full border border-white/[0.08] bg-white/[0.03] p-0.5"
        >
          {LANGUAGES.map(({ key, short, label }) => {
            // `mounted &&` because `voiceLanguage` is restored from client
            // storage: rendering the selected pill on the server would guarantee
            // a mismatch on any preference other than the default.
            const selected = mounted && voiceLanguage === key;
            return (
              <button
                key={key}
                type="button"
                role="radio"
                aria-checked={selected}
                aria-label={label}
                onClick={() => onLanguageChange(key)}
                suppressHydrationWarning
                className={`rounded-full px-2.5 py-1 text-xs font-medium tabular-nums transition-colors duration-150 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40 ${
                  selected
                    ? 'bg-white text-neutral-900'
                    : 'text-neutral-400 hover:bg-white/[0.06] hover:text-neutral-100'
                }`}
              >
                {short}
              </button>
            );
          })}
        </div>

        <button
          type="button"
          onClick={onToggleTranscript}
          aria-pressed={showTranscript}
          className="rounded-full border border-white/[0.08] px-3 py-1.5 text-xs text-neutral-400 transition-colors duration-150 hover:bg-white/[0.06] hover:text-neutral-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
        >
          {showTranscript ? 'Hide transcript' : 'Show transcript'}
        </button>
      </div>
    </header>
  );
}

export default memo(VoiceHeader);
