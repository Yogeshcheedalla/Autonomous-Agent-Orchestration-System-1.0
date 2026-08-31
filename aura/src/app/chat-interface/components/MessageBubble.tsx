'use client';

import React, { useState, memo } from 'react';
import {
  Copy,
  RotateCcw,
  ThumbsUp,
  ThumbsDown,
  Brain,
  Check,
  Code2,
  Pin,
  GitBranch,
} from 'lucide-react';
import { toast } from 'sonner';
import type { Message } from './ChatThread';

interface MessageBubbleProps {
  message: Message;
  onTogglePin?: (message: Message) => void;
  onContinueFrom?: (message: Message) => void;
  isBranchAnchor?: boolean;
}

const MODEL_BADGES: Record<string, { label: string; color: string }> = {
  'GPT-4o': {
    label: 'Akansha',
    color: 'bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/20',
  },
  'Claude 3.5': {
    label: 'Claude 3.5',
    color: 'bg-orange-500/10 text-orange-600 dark:text-orange-400 border-orange-500/20',
  },
  'Gemini 1.5': {
    label: 'Gemini 1.5',
    color: 'bg-blue-500/10 text-blue-600 dark:text-blue-400 border-blue-500/20',
  },
};

function cleanUrl(url: string) {
  return url.replace(/[),.;:!?]+$/, '');
}

function openLinkOnModifier(event: React.MouseEvent<HTMLAnchorElement>, url: string) {
  if (!event.ctrlKey && !event.metaKey) return;
  event.preventDefault();
  window.open(url, '_blank', 'noopener,noreferrer');
}

function renderLink(label: React.ReactNode, url: string, key: string) {
  const href = cleanUrl(url);
  return (
    <a
      key={key}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => openLinkOnModifier(event, href)}
      className="font-medium text-[#8B6CFF] underline underline-offset-2 hover:text-[#A99AFF]"
    >
      {label}
    </a>
  );
}

function renderInlineText(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const urlPattern = '(?:https?:\\/\\/[^)\\s]+|\\/generated\\/[^)\\s]+)';
  const pattern = new RegExp(`(\\[([^\\]]+)\\]\\((${urlPattern})\\)|${urlPattern})`, 'g');
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }

    const raw = match[0];
    const markdownLabel = match[2];
    const markdownUrl = match[3];
    const href = markdownUrl || raw;
    nodes.push(
      renderLink(markdownLabel || cleanUrl(raw), href, `${keyPrefix}-link-${match.index}`)
    );
    cursor = match.index + raw.length;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }

  return nodes;
}

function renderInlineSegment(segment: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  segment.split(/(`[^`]+`|\*\*.*?\*\*)/g).forEach((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (!part) return;
    if (part.startsWith('`') && part.endsWith('`')) {
      nodes.push(
        <code
          key={key}
          className="px-1.5 py-0.5 rounded bg-muted font-mono text-xs text-primary dark:text-[#9B7FFF]"
        >
          {part.slice(1, -1)}
        </code>
      );
      return;
    }
    if (part.startsWith('**') && part.endsWith('**')) {
      nodes.push(
        <strong key={key} className="font-semibold text-foreground">
          {renderInlineText(part.slice(2, -2), `${key}-bold`)}
        </strong>
      );
      return;
    }
    nodes.push(...renderInlineText(part, key));
  });
  return nodes;
}

function isMarkdownTableStart(lines: string[], index: number): boolean {
  const current = lines[index]?.trim() || '';
  const next = lines[index + 1]?.trim() || '';
  return (
    current.startsWith('|') &&
    current.endsWith('|') &&
    next.startsWith('|') &&
    next.endsWith('|') &&
    next
      .slice(1, -1)
      .split('|')
      .every((cell) => /^:?-{3,}:?$/.test(cell.trim()))
  );
}

function parseMarkdownTableLine(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function renderMarkdownTable(tableLines: string[], key: string) {
  const header = parseMarkdownTableLine(tableLines[0]);
  const body = tableLines.slice(2).map(parseMarkdownTableLine);

  return (
    <div
      key={key}
      className="my-3 w-full overflow-x-auto rounded-xl border border-primary/20 bg-[#080914]/40"
    >
      <table className="min-w-full border-collapse text-left text-xs">
        <thead className="bg-primary/12 text-[#D8D0FF]">
          <tr>
            {header.map((cell, index) => (
              <th
                key={`${key}-head-${index}`}
                className="border-b border-primary/20 px-3 py-2 font-semibold"
              >
                {renderInlineSegment(cell, `${key}-head-${index}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, rowIndex) => (
            <tr key={`${key}-row-${rowIndex}`} className="odd:bg-white/[0.025]">
              {header.map((_, cellIndex) => (
                <td
                  key={`${key}-cell-${rowIndex}-${cellIndex}`}
                  className="border-b border-white/5 px-3 py-2 align-top text-foreground/90"
                >
                  {renderInlineSegment(
                    row[cellIndex] || '',
                    `${key}-cell-${rowIndex}-${cellIndex}`
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function renderTextWithTables(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  const lines = text.split('\n');

  for (let index = 0; index < lines.length; index += 1) {
    if (isMarkdownTableStart(lines, index)) {
      const tableLines = [lines[index], lines[index + 1]];
      index += 2;
      while (
        index < lines.length &&
        lines[index].trim().startsWith('|') &&
        lines[index].trim().endsWith('|')
      ) {
        tableLines.push(lines[index]);
        index += 1;
      }
      index -= 1;
      nodes.push(renderMarkdownTable(tableLines, `${keyPrefix}-table-${index}`));
      continue;
    }

    nodes.push(
      <span key={`${keyPrefix}-line-${index}`}>
        {renderInlineSegment(lines[index], `${keyPrefix}-line-${index}`)}
        {index < lines.length - 1 && <br />}
      </span>
    );
  }

  return nodes;
}

function formatContent(content: string): React.ReactNode[] {
  const parts = content.split(/(```[\s\S]*?```)/g);
  return parts.map((part, i) => {
    if (part.startsWith('```')) {
      const lines = part.split('\n');
      const lang = lines[0].replace('```', '').trim() || 'code';
      const code = lines.slice(1, -1).join('\n');
      return (
        <div
          key={`code-block-${i}`}
          className="my-3 rounded-xl overflow-hidden border border-border bg-zinc-950 dark:bg-zinc-900"
        >
          <div className="flex items-center justify-between px-4 py-2 bg-zinc-900 dark:bg-zinc-800 border-b border-border">
            <div className="flex items-center gap-2">
              <Code2 size={13} className="text-muted-foreground" />
              <span className="text-xs font-mono text-muted-foreground">{lang}</span>
            </div>
            <CopyCodeButton code={code} />
          </div>
          <pre className="p-4 overflow-x-auto scrollbar-thin text-xs font-mono text-zinc-200 leading-relaxed">
            <code>{code}</code>
          </pre>
        </div>
      );
    }
    return (
      <React.Fragment key={`text-${i}`}>{renderTextWithTables(part, `line-${i}`)}</React.Fragment>
    );
  });
}

function CopyCodeButton({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <button
      onClick={handleCopy}
      className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
    >
      {copied ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
      <span>{copied ? 'Copied' : 'Copy'}</span>
    </button>
  );
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  });
}

function formatMessageDateTime(date: Date): string {
  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  });
}

/**
 * Memoised, and this is the single largest render win in the chat.
 *
 * ChatThread holds `streamingContent` in state, so it re-renders on every token of
 * a streaming reply. Without a memo boundary here, every token re-rendered every
 * bubble already on screen: a 500-token answer in a 100-message thread meant
 * ~50,000 renders of this 480-line component, all of them producing identical
 * output. That is the stutter that reads as "the chat is slow to respond" even
 * when the backend has already sent the text.
 *
 * The memo is sound because all four props are stable between tokens:
 *   - `message` objects come from the messages array in state and keep their
 *     identity until that message itself changes;
 *   - `onTogglePin` and `onContinueFrom` are useCallback'd with `[]` deps in
 *     ChatThread, so they are created once;
 *   - `isBranchAnchor` is a boolean.
 * The streaming bubble is a separate instance built from a fresh object literal
 * each render, so it keeps updating on every token exactly as before -- the memo
 * skips the settled messages, not the one that is actually changing.
 */
const MessageBubble = memo(function MessageBubble({
  message,
  onTogglePin,
  onContinueFrom,
  isBranchAnchor,
}: MessageBubbleProps) {
  const [liked, setLiked] = useState<boolean | null>(null);
  const isUser = message.role === 'user';
  const badge = message.model ? MODEL_BADGES[message.model] : null;

  const handleCopy = () => {
    navigator.clipboard.writeText(message.content);
    toast.success('Message copied to clipboard');
  };

  return (
    <div
      id={`chat-message-${message.id}`}
      className={`flex gap-3 py-3 group scroll-mt-32 ${isUser ? 'flex-row-reverse' : 'flex-row'} ${
        isBranchAnchor ? 'rounded-2xl bg-primary/5 ring-1 ring-primary/20 px-2' : ''
      }`}
    >
      {/* Avatar */}
      <div
        className={`w-8 h-8 rounded-full shrink-0 flex items-center justify-center text-xs font-semibold mt-0.5 ${
          isUser
            ? 'bg-gradient-to-br from-primary to-accent text-white'
            : 'bg-gradient-to-br from-zinc-700 to-zinc-600 text-zinc-200 border border-border'
        }`}
      >
        {isUser ? 'A' : 'AI'}
      </div>

      <div className={`flex flex-col gap-1 max-w-[75%] ${isUser ? 'items-end' : 'items-start'}`}>
        {/* Header row */}
        <div className={`flex items-center gap-2 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
          <span
            className={`text-xs font-medium ${isUser ? 'text-foreground' : 'text-green-600 dark:text-green-400'}`}
          >
            {isUser ? 'You' : 'Akansha'}
          </span>
          {badge && (
            <span className={`text-xs px-2 py-0.5 rounded-full border font-medium ${badge.color}`}>
              {badge.label}
            </span>
          )}
          {message.memoryRefs && message.memoryRefs.length > 0 && (
            <span className="flex items-center gap-1 text-xs text-accent px-2 py-0.5 rounded-full bg-accent/10 border border-accent/20">
              <Brain size={10} />
              Memory
            </span>
          )}
          <span
            className="text-xs text-muted-foreground/60"
            title={formatMessageDateTime(message.timestamp)}
          >
            {formatTime(message.timestamp)}
          </span>
          {message.pinned && (
            <span className="flex items-center gap-1 text-xs text-amber-400 px-2 py-0.5 rounded-full bg-amber-400/10 border border-amber-400/20">
              <Pin size={10} fill="currentColor" />
              Pinned
            </span>
          )}
        </div>

        {/* Attachments */}
        {message.attachments && message.attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-1">
            {message.attachments.map((att) => (
              <div
                key={att.id}
                className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-primary/10 border border-primary/20 text-xs"
              >
                {att.previewUrl ? (
                  // Deliberately a raw <img>: previewUrl is a `blob:` URL from
                  // URL.createObjectURL on a locally-picked file, which the
                  // next/image optimizer cannot fetch. There is also nothing to
                  // optimize — the source is already in memory and this renders
                  // at 56x40.
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={att.previewUrl}
                    alt={att.name}
                    className="h-10 w-14 rounded-md object-cover border border-primary/20"
                  />
                ) : (
                  <Code2 size={12} className="text-primary" />
                )}
                <div className="min-w-0">
                  <span className="block font-mono text-primary dark:text-[#9B7FFF] max-w-44 truncate">
                    {att.name}
                  </span>
                  <span className="block text-muted-foreground">{att.size}</span>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Content bubble */}
        <div
          className={`rounded-2xl px-4 py-3 text-sm leading-relaxed ${
            isUser
              ? 'bg-primary text-white rounded-tr-sm'
              : 'bg-card border border-border text-foreground rounded-tl-sm'
          }`}
        >
          {isUser ? (
            <p>{message.content}</p>
          ) : (
            <div className="prose-sm">
              {formatContent(message.content)}
              {message.isStreaming && <span className="streaming-cursor" />}
            </div>
          )}
        </div>

        {/* Actions (AI messages only) */}
        {!message.isStreaming && (
          <div
            className={`flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity ${isUser ? 'flex-row-reverse' : ''}`}
          >
            <button
              onClick={handleCopy}
              className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
              title="Copy message"
            >
              <Copy size={13} />
            </button>
            {onTogglePin && (
              <button
                onClick={() => onTogglePin(message)}
                className={`p-1.5 rounded-lg transition-colors ${
                  message.pinned
                    ? 'text-amber-400 bg-amber-400/10'
                    : 'hover:bg-muted text-muted-foreground hover:text-foreground'
                }`}
                title={message.pinned ? 'Unpin message' : 'Pin message'}
              >
                <Pin size={13} fill={message.pinned ? 'currentColor' : 'none'} />
              </button>
            )}
            {onContinueFrom && (
              <button
                onClick={() => onContinueFrom(message)}
                className={`p-1.5 rounded-lg transition-colors ${
                  isBranchAnchor
                    ? 'text-primary bg-primary/10'
                    : 'hover:bg-muted text-muted-foreground hover:text-foreground'
                }`}
                title="Continue conversation from this message"
              >
                <GitBranch size={13} />
              </button>
            )}
            {!isUser && (
              <>
                <button
                  className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                  title="Regenerate"
                >
                  <RotateCcw size={13} />
                </button>
                <button
                  onClick={() => setLiked(true)}
                  className={`p-1.5 rounded-lg transition-colors ${liked === true ? 'text-green-500 bg-green-500/10' : 'hover:bg-muted text-muted-foreground hover:text-foreground'}`}
                  title="Good response"
                >
                  <ThumbsUp size={13} />
                </button>
                <button
                  onClick={() => setLiked(false)}
                  className={`p-1.5 rounded-lg transition-colors ${liked === false ? 'text-red-500 bg-red-500/10' : 'hover:bg-muted text-muted-foreground hover:text-foreground'}`}
                  title="Bad response"
                >
                  <ThumbsDown size={13} />
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
});

MessageBubble.displayName = 'MessageBubble';

export default MessageBubble;
