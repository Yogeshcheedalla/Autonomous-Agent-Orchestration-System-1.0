'use client';

import React, { useEffect } from 'react';
import { RefreshCw, Sparkles } from 'lucide-react';

export default function VoiceAssistantError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.warn('Voice assistant route error:', error);
  }, [error]);

  return (
    <div className="flex min-h-[70vh] items-center justify-center bg-[radial-gradient(circle_at_top,_rgba(108,71,255,0.22),_transparent_28%),linear-gradient(180deg,_#020617_0%,_#0f172a_100%)] px-6 py-12">
      <div className="w-full max-w-xl rounded-[32px] border border-white/10 bg-slate-950/85 p-8 text-center shadow-[0_32px_120px_rgba(15,23,42,0.45)]">
        <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-primary/20 text-[#c7b8ff]">
          <Sparkles size={24} />
        </div>
        <h1 className="mt-5 text-2xl font-semibold text-white">Akansha hit a temporary issue</h1>
        <p className="mt-3 text-sm leading-7 text-slate-300">
          We kept the route from dropping into a raw browser error screen. Please retry the voice
          assistant now, and if it still breaks we will have a cleaner trace to fix.
        </p>
        <button
          onClick={reset}
          className="mt-6 inline-flex items-center gap-2 rounded-full bg-primary px-5 py-3 text-sm font-medium text-white transition-colors hover:bg-primary-hover"
        >
          <RefreshCw size={16} />
          Reload voice assistant
        </button>
      </div>
    </div>
  );
}
