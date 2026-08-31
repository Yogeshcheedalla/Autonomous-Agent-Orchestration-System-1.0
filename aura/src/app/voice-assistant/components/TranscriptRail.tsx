'use client';

import { memo, useEffect, useRef } from 'react';

export interface Turn {
  id: number;
  role: 'user' | 'ai';
  text: string;
  /** ISO timestamp. Formatted at render so the locale is the reader's. */
  at: string;
  intent?: string;
  site?: string | null;
  isAuto?: boolean;
}

interface Props {
  turns: Turn[];
  onClear: () => void;
  reducedMotion: boolean;
}

/**
 * Time formatted through `Intl`, not `toLocaleTimeString` with a literal option
 * bag. The old version produced `04:07 PM` for everyone including people whose
 * locale writes `16:07`, because it hardcoded `hour: '2-digit'`.
 *
 * Built once at module scope rather than per row: constructing a `DateTimeFormat`
 * is the expensive part, and a 200-turn conversation was building 200 of them.
 */
const TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  hour: 'numeric',
  minute: '2-digit',
});

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : TIME_FORMAT.format(date);
}

/**
 * The conversation, alongside her.
 *
 * Two things here are not styling choices:
 *
 * `aria-live="polite"` — a voice interface is the one case where the screen is
 * optional, so a user who cannot see it still needs her replies announced. It is
 * `polite` rather than `assertive` because interrupting a screen reader mid-word
 * to deliver a partial sentence is worse than waiting for the pause.
 *
 * Autoscroll only when already at the bottom. Unconditional `scrollIntoView`
 * yanked the panel back down the instant a reply streamed in, which made reading
 * back through a long conversation impossible while she was still talking.
 */
function TranscriptRail({ turns, onClear, reducedMotion }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    const onScroll = () => {
      // 64px of slack: "close enough to the bottom that the user is following
      // along" rather than "exactly at the bottom", which no trackpad ever hits.
      pinnedRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 64;
    };
    node.addEventListener('scroll', onScroll, { passive: true });
    return () => node.removeEventListener('scroll', onScroll);
  }, []);

  useEffect(() => {
    if (!pinnedRef.current) return;
    endRef.current?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth' });
  }, [turns, reducedMotion]);

  return (
    <aside
      // Stacked, this sits under her, so its cap is a share of the viewport rather
      // than `40vh` — at 40 a full conversation squeezed the portrait down to its
      // floor. Uncapped from `xl` up, where it is a column beside her instead.
      className="flex max-h-[34vh] w-full shrink-0 flex-col border-white/[0.06] bg-black/25 backdrop-blur-xl xl:max-h-none xl:w-[22rem] xl:border-l"
      aria-label="Conversation transcript"
    >
      <header className="flex shrink-0 items-center justify-between border-b border-white/[0.06] px-5 py-3.5">
        <h2 className="text-[0.8125rem] font-medium text-neutral-300">Conversation</h2>
        {turns.length > 0 && (
          <button
            type="button"
            onClick={onClear}
            className="rounded-md px-2 py-1 text-xs text-neutral-500 transition-colors hover:bg-white/5 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/30"
          >
            Clear
          </button>
        )}
      </header>

      <div
        ref={scrollRef}
        className="flex-1 space-y-3 overflow-y-auto overscroll-contain px-4 py-4"
        aria-live="polite"
        aria-atomic="false"
      >
        {turns.length === 0 ? (
          <p className="px-1 pt-6 text-sm leading-relaxed text-neutral-500 text-pretty">
            Nothing yet. Start talking and both sides of the conversation show up here.
          </p>
        ) : (
          turns.map((turn) => (
            <article
              key={turn.id}
              className={`flex ${turn.role === 'user' ? 'justify-end' : 'justify-start'} ${
                reducedMotion ? '' : 'animate-rise'
              }`}
            >
              <div
                className={`min-w-0 max-w-[88%] rounded-2xl px-3.5 py-2.5 ${
                  turn.role === 'user'
                    ? 'rounded-br-md bg-white/[0.07] text-neutral-100'
                    : 'rounded-bl-md bg-gradient-to-br from-violet-500/[0.14] to-sky-500/[0.08] text-neutral-100 ring-1 ring-inset ring-white/[0.06]'
                }`}
              >
                <p className="whitespace-pre-wrap break-words text-[0.8125rem] leading-relaxed text-pretty">
                  {turn.text || '…'}
                </p>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.6875rem] text-neutral-500">
                  <time dateTime={turn.at} className="tabular-nums">
                    {formatTime(turn.at)}
                  </time>
                  {turn.site && (
                    <span className="truncate text-amber-300/70" translate="no">
                      {turn.site}
                    </span>
                  )}
                  {turn.isAuto && <span className="text-violet-300/70">automated</span>}
                </div>
              </div>
            </article>
          ))
        )}
        <div ref={endRef} />
      </div>
    </aside>
  );
}

export default memo(TranscriptRail);
