'use client';

import React, { memo } from 'react';
import { Mic, MicOff, Volume2, VolumeX, Minimize2, Maximize2 } from 'lucide-react';

import JarvisCore, {
  coreStateFor,
  presenceLabel,
  EMOTION_COLORS,
  type Emotion,
} from '@/components/assistant/JarvisCore';

/**
 * Re-exported, not redeclared. `ChatThread` imports the type from here and the
 * vocabulary now lives with the object that renders it, in JarvisCore.
 */
export type { Emotion };

interface AvatarPanelProps {
  emotion: Emotion;
  isSpeaking: boolean;
  isListening: boolean;
  onToggleMic: () => void;
  onToggleVoice: () => void;
  voiceEnabled: boolean;
  minimized?: boolean;
  onToggleMinimize?: () => void;
  /**
   * Live speech amplitude, 0..1, for §26.
   *
   * Optional because nothing measures it yet. `JarvisCore` falls back to a small
   * randomised flutter, which reads as speech but is not synchronised to the audio;
   * wiring an AnalyserNode on the TTS output into this prop is what makes it real,
   * and is still the only change needed here to get it.
   */
  audioLevel?: number;
}

/**
 * The panel is a frame around `JarvisCore` now, not a face.
 *
 * It used to draw one: two white ellipses, two brows, a quadratic mouth path, and
 * five timers driving blink, gaze, brow tilt and a mouth flutter -- all inside a
 * flat SVG disc. It was reported as *"remove this Akansha image and set a
 * three-dimensional jarvis-like structure"*, and the timers went with it, because a
 * timer whose `setState` no element reads is exactly the bug an earlier pass here
 * had already fixed once. `JarvisCore` owns every animation it needs; this file
 * owns the frame, the state badge and the controls.
 *
 * Memoized at the bottom. Every prop is a primitive except the three callbacks, and
 * all three are `useCallback`'d at the single call site in ChatThread -- which
 * matters, because this sits inside a component that re-renders on every streamed
 * token, and unstable callbacks would make the wrapper compare unequal on each one.
 */

// Kept as literal hex, not `var(--violet-primary)`: these are handed to `JarvisCore`
// which composes them with alpha suffixes (`${accent}cc`), and `var()` cannot be
// concatenated that way. The table itself lives in JarvisCore, beside the states it
// colours; see EMOTION_COLORS there.

function AvatarPanel({
  emotion,
  isSpeaking,
  isListening,
  onToggleMic,
  onToggleVoice,
  voiceEnabled,
  minimized = false,
  onToggleMinimize,
  audioLevel,
}: AvatarPanelProps) {
  const accentColor = EMOTION_COLORS[emotion];
  const coreState = coreStateFor(emotion, isSpeaking, isListening);
  const label = presenceLabel(emotion, isListening);

  if (minimized) {
    return (
      <div className="flex items-center gap-2.5 px-3 py-2 rounded-xl border border-border bg-card/80 backdrop-blur-sm">
        {/* The same object at 34px rather than a letter in a circle: one presence
            that shrinks, so collapsing the bar does not change who is on screen. */}
        <JarvisCore state={coreState} accent={accentColor} level={audioLevel} size={34} />
        <div className="flex flex-col leading-tight">
          <span className="text-xs font-semibold text-foreground tracking-wide">Akansha</span>
          <span className="text-xs text-muted-foreground">{label}</span>
        </div>
        <button
          onClick={onToggleMinimize}
          className="p-1 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors ml-1"
          title="Expand"
        >
          <Maximize2 size={12} />
        </button>
      </div>
    );
  }

  return (
    <div
      className="relative flex flex-col items-center gap-3 px-6 py-5 rounded-2xl border overflow-hidden"
      style={{
        // A console housing rather than a card: a vertical dark gradient, a hairline
        // in the accent, and an inner top highlight so the panel has an edge the
        // core can sit inside instead of floating on a flat rectangle.
        borderColor: `${accentColor}33`,
        background: 'linear-gradient(180deg, rgba(16,18,34,0.92) 0%, rgba(10,12,22,0.96) 100%)',
        boxShadow: `inset 0 1px 0 ${accentColor}22, 0 8px 28px -12px ${accentColor}55`,
      }}
    >
      {/* HUD grid. Purely decorative, and behind everything -- `pointer-events-none`
          so it cannot eat a click meant for the mic button. */}
      <div
        aria-hidden
        className="absolute inset-0 pointer-events-none opacity-[0.07]"
        style={{
          backgroundImage:
            'linear-gradient(rgba(34,211,238,0.6) 1px, transparent 1px), linear-gradient(90deg, rgba(34,211,238,0.6) 1px, transparent 1px)',
          backgroundSize: '14px 14px',
          maskImage: 'radial-gradient(circle at 50% 40%, #000 30%, transparent 78%)',
          WebkitMaskImage: 'radial-gradient(circle at 50% 40%, #000 30%, transparent 78%)',
        }}
      />

      <JarvisCore
        state={coreState}
        accent={accentColor}
        level={audioLevel}
        size={132}
        className="relative z-10"
      />

      {/* Name + state badge */}
      <div className="relative z-10 text-center">
        <p className="text-sm font-semibold text-foreground tracking-[0.18em] uppercase">Akansha</p>
        <div
          className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium mt-1 border"
          style={{
            background: `${accentColor}18`,
            borderColor: `${accentColor}44`,
            color: accentColor,
          }}
        >
          <span
            className="w-1.5 h-1.5 rounded-full"
            style={{
              background: accentColor,
              boxShadow: `0 0 6px ${accentColor}`,
              animation: isSpeaking ? 'pulse 0.8s infinite' : 'none',
            }}
          />
          {label}
        </div>
      </div>

      {/* Voice waveform when speaking */}
      {isSpeaking && (
        <div className="relative z-10 flex items-end gap-0.5 h-6">
          {[0, 1, 2, 3, 4, 5, 6].map((i) => (
            <div
              key={`wave-${i}`}
              className="w-1 rounded-full"
              style={{
                background: `linear-gradient(to top, ${accentColor}, #22d3ee)`,
                // Deterministic: the CSS animation owns the motion, so the height is
                // only a starting pose. Reading `Date.now()` during render differs
                // between server and client and makes the first paint a hydration
                // mismatch for no visual gain.
                height: `${8 + Math.sin(i) * 6}px`,
                animation: `waveform ${0.4 + i * 0.05}s ease-in-out infinite alternate`,
                animationDelay: `${i * 0.07}s`,
              }}
            />
          ))}
        </div>
      )}

      {/* Controls */}
      <div className="relative z-10 flex items-center gap-2">
        <button
          onClick={onToggleMic}
          className={`p-2 rounded-xl transition-all border ${
            isListening
              ? 'bg-red-500/15 text-red-500 border-red-500/40'
              : 'bg-white/5 text-muted-foreground hover:text-foreground border-white/10 hover:border-white/25'
          }`}
          style={isListening ? { boxShadow: '0 0 14px rgba(239,68,68,0.35)' } : undefined}
          title={isListening ? 'Stop listening' : 'Start voice input'}
        >
          {isListening ? <MicOff size={14} /> : <Mic size={14} />}
        </button>
        <button
          onClick={onToggleVoice}
          className={`p-2 rounded-xl transition-all border border-white/10 bg-white/5 ${
            voiceEnabled
              ? 'text-foreground hover:border-white/25'
              : 'text-muted-foreground opacity-50'
          }`}
          title={voiceEnabled ? 'Mute voice' : 'Enable voice'}
        >
          {voiceEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
        </button>
        {onToggleMinimize && (
          <button
            onClick={onToggleMinimize}
            className="p-2 rounded-xl bg-white/5 text-muted-foreground hover:text-foreground border border-white/10 hover:border-white/25 transition-all"
            title="Minimize avatar"
          >
            <Minimize2 size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

AvatarPanel.displayName = 'AvatarPanel';

export default memo(AvatarPanel);
