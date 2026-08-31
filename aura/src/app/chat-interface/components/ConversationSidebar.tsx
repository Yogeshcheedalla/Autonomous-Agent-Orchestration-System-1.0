'use client';

import React, { memo, useState, useEffect } from 'react';
import Link from 'next/link';
import {
  Plus,
  Search,
  MessageSquare,
  ChevronDown,
  ChevronRight,
  Pencil,
  Check,
  X,
  Trash2,
} from 'lucide-react';
import {
  CHAT_SESSION_TITLES_UPDATED,
  deleteSessionTitle,
  getSessionTitle,
  writeSessionTitle,
} from '@/hooks/chatSessionTitles';
import { toast } from 'sonner';
import { apiUrl } from '@/lib/apiBase';
import type { ChatHistoryResponse } from '@/types/chatApi';

interface HistoryItem {
  id: string;
  title: string;
  timestamp: Date;
}

interface ConversationSidebarProps {
  activeSessionId: string;
  onNewChat: (sessionId?: string) => void;
  onSessionChange: (id: string) => void;
}

const TIMESTAMP_WITH_ZONE_PATTERN = /(?:Z|[+-]\d{2}:?\d{2})$/i;

function parseConversationTimestamp(value: string | null | undefined): Date {
  if (!value) return new Date();
  const normalized = TIMESTAMP_WITH_ZONE_PATTERN.test(value) ? value : `${value}Z`;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? new Date() : parsed;
}

function startOfDay(date: Date) {
  const next = new Date(date);
  next.setHours(0, 0, 0, 0);
  return next;
}

function formatSidebarDate(date: Date) {
  return date.toLocaleDateString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

function formatConversationTime(date: Date) {
  return date.toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  });
}

function formatConversationMeta(date: Date) {
  return `${formatSidebarDate(date)} · ${formatConversationTime(date)}`;
}

function getConversationGroups(items: HistoryItem[]) {
  const today = startOfDay(new Date());
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const groups = new Map<string, HistoryItem[]>();

  items.forEach((item) => {
    const itemDay = startOfDay(item.timestamp);
    const label =
      itemDay.getTime() === today.getTime()
        ? `Today · ${formatSidebarDate(item.timestamp)}`
        : itemDay.getTime() === yesterday.getTime()
          ? `Yesterday · ${formatSidebarDate(item.timestamp)}`
          : formatSidebarDate(item.timestamp);

    groups.set(label, [...(groups.get(label) ?? []), item]);
  });

  return Array.from(groups.entries()).map(([label, groupedItems]) => ({
    label,
    items: groupedItems,
  }));
}

/**
 * Memoized at the bottom of this file.
 *
 * The sidebar is a sibling of `ChatThread` under `ChatWorkspace`, and
 * `ChatWorkspace` re-renders on every `onStatsChange` -- which fires roughly once
 * per four characters of a streaming reply, because contextUnits is
 * ceil(characters / 4). Without the wrapper, answering one question re-ran this
 * entire list, its date grouping and its fetch bookkeeping a few hundred times
 * for a component whose contents did not change at all.
 *
 * All three props are stable: `activeSessionId` is a string, `onNewChat` is
 * useCallback'd in the parent, and `onSessionChange` is a setState function.
 */
function ConversationSidebar({
  activeSessionId,
  onNewChat,
  onSessionChange,
}: ConversationSidebarProps) {
  const [search, setSearch] = useState('');
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [historyItems, setHistoryItems] = useState<HistoryItem[]>([]);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');

  const loadHistory = React.useCallback(() => {
    fetch(apiUrl('/api/chat'))
      .then((res) => res.json() as Promise<ChatHistoryResponse>)
      .then((data) => {
        if (!data.messages) {
          setHistoryItems([]);
          return;
        }

        const sessionsMap: Record<string, HistoryItem> = {};
        data.messages.forEach((m) => {
          const sid = m.session_id || 'default';
          const messageTimestamp = parseConversationTimestamp(m.timestamp);
          if (!sessionsMap[sid]) {
            sessionsMap[sid] = {
              id: sid,
              title: getSessionTitle(sid, m.role === 'user' ? m.content : 'New chat'),
              timestamp: messageTimestamp,
            };
            return;
          }

          if (m.role === 'user' && !sessionsMap[sid].title) {
            sessionsMap[sid].title = m.content;
          }

          if (messageTimestamp > sessionsMap[sid].timestamp) {
            sessionsMap[sid].timestamp = messageTimestamp;
          }
        });

        const items = Object.values(sessionsMap)
          .map((item) => ({
            ...item,
            title: getSessionTitle(item.id, item.title || 'New chat'),
          }))
          .sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime());

        setHistoryItems(items);
      })
      .catch((err) => console.warn('Failed to load sidebar history:', err));
  }, []);

  useEffect(() => {
    loadHistory();
    window.addEventListener('akansha-history-updated', loadHistory);
    window.addEventListener(CHAT_SESSION_TITLES_UPDATED, loadHistory);
    return () => {
      window.removeEventListener('akansha-history-updated', loadHistory);
      window.removeEventListener(CHAT_SESSION_TITLES_UPDATED, loadHistory);
    };
  }, [loadHistory]);

  const startRenaming = (event: React.MouseEvent, conversation: HistoryItem) => {
    event.stopPropagation();
    setEditingSessionId(conversation.id);
    setDraftTitle(conversation.title);
  };

  const submitRename = (event?: React.MouseEvent | React.FormEvent) => {
    event?.preventDefault();
    event?.stopPropagation();
    if (!editingSessionId) return;

    writeSessionTitle(editingSessionId, draftTitle);
    setEditingSessionId(null);
    setDraftTitle('');
  };

  const cancelRename = (event?: React.MouseEvent) => {
    event?.stopPropagation();
    setEditingSessionId(null);
    setDraftTitle('');
  };

  const deleteConversation = async (event: React.MouseEvent, conversation: HistoryItem) => {
    event.stopPropagation();
    const confirmed = window.confirm(`Delete "${conversation.title}" from chat history?`);
    if (!confirmed) return;

    try {
      const response = await fetch(
        apiUrl(`/api/chat/session/${encodeURIComponent(conversation.id)}`),
        {
          method: 'DELETE',
        }
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Could not delete this conversation.');
      }

      deleteSessionTitle(conversation.id);
      setHistoryItems((items) => items.filter((item) => item.id !== conversation.id));
      window.dispatchEvent(new CustomEvent('akansha-history-updated'));
      if (conversation.id === activeSessionId) {
        onNewChat();
      }
      toast.success('Conversation deleted');
    } catch (error) {
      console.warn('[Akansha conversations] recovered delete failure:', error);
      toast.error(error instanceof Error ? error.message : 'Could not delete this conversation.');
    }
  };

  const toggleGroup = (id: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Group history items
  const visibleHistoryItems = historyItems.some((item) => item.id === activeSessionId)
    ? historyItems
    : [{ id: activeSessionId, title: 'New chat', timestamp: new Date() }, ...historyItems];
  const groups = getConversationGroups(visibleHistoryItems);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="p-3 border-b border-border">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
            Conversations
          </span>
          <button
            onClick={() => onNewChat()}
            className="p-1 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
            title="New conversation"
          >
            <Plus size={15} />
          </button>
        </div>
        <div className="relative">
          <Search
            size={13}
            className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
          />
          <input
            type="text"
            placeholder="Search..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full bg-muted rounded-lg text-xs pl-7 pr-3 py-1.5 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/40 border-0"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto scrollbar-thin p-2">
        {/* Conversation groups */}
        {groups.map((group) => {
          const filtered = group.items.filter(
            (i) => !search || i.title.toLowerCase().includes(search.toLowerCase())
          );
          if (filtered.length === 0) return null;

          const isExpanded = !collapsedGroups.has(group.label);

          return (
            <div key={`group-${group.label}`} className="mb-3">
              <button
                onClick={() => toggleGroup(group.label)}
                className="flex items-center gap-1 w-full text-xs font-semibold text-muted-foreground uppercase tracking-wider px-1 mb-1 hover:text-foreground transition-colors"
              >
                {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                {group.label}
              </button>

              {isExpanded &&
                filtered.map((conv) => (
                  <div
                    key={conv.id}
                    onClick={() => onSessionChange(conv.id)}
                    className={`group flex items-start gap-2 px-2 py-2 rounded-lg cursor-pointer transition-colors mb-0.5 ${
                      activeSessionId === conv.id
                        ? 'bg-primary/10 text-primary border border-primary/20'
                        : 'hover:bg-muted text-muted-foreground hover:text-foreground border border-transparent'
                    }`}
                  >
                    <MessageSquare
                      size={12}
                      className={`mt-1 shrink-0 ${activeSessionId === conv.id ? 'text-primary' : 'text-primary/70'}`}
                    />
                    <div className="flex-1 min-w-0">
                      {editingSessionId === conv.id ? (
                        <form onSubmit={submitRename} className="flex items-center gap-1.5">
                          <input
                            autoFocus
                            value={draftTitle}
                            onChange={(event) => setDraftTitle(event.target.value)}
                            onClick={(event) => event.stopPropagation()}
                            onKeyDown={(event) => {
                              if (event.key === 'Escape') {
                                cancelRename();
                              }
                            }}
                            className="w-full bg-muted rounded-md px-2 py-1 text-xs text-foreground border border-primary/30 focus:outline-none focus:ring-1 focus:ring-primary/50"
                          />
                          <button
                            type="submit"
                            onClick={submitRename}
                            className="p-1 rounded-md text-emerald-400 hover:bg-emerald-500/10 transition-colors"
                            title="Save name"
                          >
                            <Check size={12} />
                          </button>
                          <button
                            type="button"
                            onClick={cancelRename}
                            className="p-1 rounded-md text-muted-foreground hover:bg-muted transition-colors"
                            title="Cancel rename"
                          >
                            <X size={12} />
                          </button>
                        </form>
                      ) : (
                        <div className="flex items-start gap-2">
                          <div className="min-w-0 flex-1">
                            <p
                              className={`text-xs font-medium truncate ${activeSessionId === conv.id ? 'text-foreground' : ''}`}
                            >
                              {conv.title}
                            </p>
                            <p className="mt-0.5 text-[10px] text-muted-foreground/70">
                              {formatConversationMeta(conv.timestamp)}
                            </p>
                          </div>
                          <button
                            type="button"
                            onClick={(event) => startRenaming(event, conv)}
                            className="opacity-0 group-hover:opacity-100 p-1 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-all"
                            title="Rename chat"
                          >
                            <Pencil size={12} />
                          </button>
                          <button
                            type="button"
                            onClick={(event) => deleteConversation(event, conv)}
                            className="opacity-0 group-hover:opacity-100 p-1 rounded-md text-muted-foreground hover:bg-red-500/10 hover:text-red-500 transition-all"
                            title="Delete chat"
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
            </div>
          );
        })}
      </div>

      <div className="p-2 border-t border-border">
        <Link
          href="/conversation-history"
          className="flex items-center gap-2 px-2 py-2 rounded-lg text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
        >
          <MessageSquare size={13} />
          View all history
        </Link>
      </div>
    </div>
  );
}

ConversationSidebar.displayName = 'ConversationSidebar';

export default memo(ConversationSidebar);
