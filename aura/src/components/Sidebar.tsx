'use client';

import React, { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import AppLogo from './ui/AppLogo';
import {
  MessageSquare,
  History,
  BookMarked,
  Settings,
  ChevronLeft,
  ChevronRight,
  Plus,
  Search,
  Star,
  X,
  Cpu,
  MemoryStick,
  Mic,
  Zap,
  CalendarDays,
  PlugZap,
  Trash2,
  LogOut,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  CHAT_SESSION_TITLES_UPDATED,
  deleteSessionTitle,
  getSessionTitle,
} from '@/hooks/chatSessionTitles';
import { apiUrl } from '@/lib/apiBase';
import type { ChatHistoryResponse } from '@/types/chatApi';

interface SidebarProps {
  collapsed: boolean;
  onToggleCollapse: () => void;
  activePath?: string;
  onClose?: () => void;
}

interface RecentConversation {
  id: string;
  title: string;
  model: string;
  starred: boolean;
  timestamp: Date;
}

const navItems = [
  {
    key: 'nav-chat',
    href: '/chat-interface',
    icon: MessageSquare,
    label: 'Chat',
    badge: null,
  },
  {
    key: 'nav-voice',
    href: '/voice-assistant',
    icon: Mic,
    label: 'Voice Agent',
    badge: 'New',
  },
  {
    key: 'nav-history',
    href: '/conversation-history',
    icon: History,
    label: 'History',
    badge: '24',
  },
  {
    key: 'nav-planner',
    href: '/planner-service',
    icon: CalendarDays,
    label: 'Planner',
    badge: null,
  },
  {
    key: 'nav-integrations',
    href: '/connections',
    icon: PlugZap,
    // Not a count. It said "27 apps", which was the length of a hardcoded registry
    // and is now wrong in both directions: the catalog declares 29, this machine has
    // 63 installed applications the scan can adopt, and any website can be connected
    // besides. A number here would have to be re-typed every time the page grows.
    label: 'Connections',
    badge: 'Any app',
  },
  {
    key: 'nav-task-automations',
    href: '/task-automations',
    icon: Zap,
    label: 'Task Studio',
    badge: 'Canvas',
  },
  {
    key: 'nav-cognitive-dashboard',
    href: '/cognitive-dashboard',
    icon: Cpu,
    label: 'Observatory',
    badge: null,
  },
  {
    key: 'nav-prompts',
    href: '/chat-interface',
    icon: BookMarked,
    label: 'Prompts',
    badge: null,
  },
];

const modelColorMap: Record<string, string> = {
  Akansha: 'bg-green-500',
  'GPT-4o': 'bg-green-500',
  'Claude 3.5': 'bg-orange-400',
  'Gemini 1.5': 'bg-blue-400',
};

function formatRecentConversationDate(date: Date) {
  return date.toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
  });
}

function formatRecentConversationTime(date: Date) {
  return date.toLocaleTimeString('en-IN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  });
}

function createSessionId() {
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export default function Sidebar({
  collapsed,
  onToggleCollapse,
  activePath,
  onClose,
}: SidebarProps) {
  const router = useRouter();
  const pathname = usePathname();
  const currentPath = activePath || pathname || '';
  const navigationFallbackRef = useRef<number | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [recentConversations, setRecentConversations] = useState<RecentConversation[]>([]);
  const [profile, setProfile] = useState<{ full_name: string; username?: string } | null>(null);

  useEffect(() => {
    const fetchProfile = async () => {
      try {
        const res = await fetch(apiUrl('/api/profile'));
        if (res.ok) {
          const data = await res.json();
          setProfile(data.profile);
        }
      } catch (err) {
        console.warn('Failed to fetch profile:', err);
      }
    };
    fetchProfile();
  }, []);

  useEffect(() => {
    const loadRecentConversations = () => {
      fetch(apiUrl('/api/chat'))
        .then((res) => res.json() as Promise<ChatHistoryResponse>)
        .then((data) => {
          if (!data.messages) {
            setRecentConversations([]);
            return;
          }

          const sessionsMap: Record<string, RecentConversation> = {};

          data.messages.forEach((message) => {
            const sessionId = message.session_id || 'default';
            const messageTimestamp = message.timestamp ? new Date(message.timestamp) : new Date();
            const fallbackTitle =
              message.role === 'user' && message.content
                ? message.content
                : sessionsMap[sessionId]?.title || 'New chat';

            const nextTitle = getSessionTitle(sessionId, fallbackTitle);

            if (!sessionsMap[sessionId]) {
              sessionsMap[sessionId] = {
                id: sessionId,
                title: nextTitle,
                // Not `message.model || 'Akansha'`. GET /api/chat does not return a
                // `model` field and `chat_messages` has no such column, so that
                // read was always undefined and the fallback always won -- the
                // `any` on this row was the only reason it looked live. Surfacing
                // a real per-message model needs a column and a migration, which
                // is a feature, not this cleanup.
                model: 'Akansha',
                starred: false,
                timestamp: messageTimestamp,
              };
              return;
            }

            if (
              message.role === 'user' &&
              !getSessionTitle(sessionId, '').trim() &&
              sessionsMap[sessionId].title === 'New chat'
            ) {
              sessionsMap[sessionId].title = message.content;
            }

            if (messageTimestamp > sessionsMap[sessionId].timestamp) {
              sessionsMap[sessionId].timestamp = messageTimestamp;
            }
          });

          const items = Object.values(sessionsMap)
            .map((item) => ({
              ...item,
              title: getSessionTitle(item.id, item.title || 'New chat'),
            }))
            .sort((a, b) => b.timestamp.getTime() - a.timestamp.getTime())
            .slice(0, 8);

          setRecentConversations(items);
        })
        .catch((error) => {
          console.warn('Failed to load recent conversations:', error);
          setRecentConversations([]);
        });
    };

    loadRecentConversations();
    window.addEventListener('akansha-history-updated', loadRecentConversations);
    window.addEventListener(CHAT_SESSION_TITLES_UPDATED, loadRecentConversations);
    return () => {
      window.removeEventListener('akansha-history-updated', loadRecentConversations);
      window.removeEventListener(CHAT_SESSION_TITLES_UPDATED, loadRecentConversations);
    };
  }, []);

  useEffect(() => {
    if (!navigationFallbackRef.current) return;
    window.clearTimeout(navigationFallbackRef.current);
    navigationFallbackRef.current = null;
  }, [currentPath]);

  useEffect(() => {
    return () => {
      if (navigationFallbackRef.current) {
        window.clearTimeout(navigationFallbackRef.current);
      }
    };
  }, []);

  const filtered = recentConversations.filter((conv) => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) return true;

    return conv.title.toLowerCase().includes(query) || conv.model.toLowerCase().includes(query);
  });

  const reliableNavigate = (href: string) => {
    if (!href || currentPath === href) return false;
    router.push(href);
    return true;
  };

  const deleteRecentConversation = async (
    event: React.MouseEvent<HTMLElement>,
    conversation: RecentConversation
  ) => {
    event.preventDefault();
    event.stopPropagation();
    const confirmed = window.confirm(`Delete "${conversation.title}" from chat history?`);
    if (!confirmed) return;

    const previousConversations = recentConversations;
    deleteSessionTitle(conversation.id);
    setRecentConversations((items) => items.filter((item) => item.id !== conversation.id));
    window.dispatchEvent(new CustomEvent('akansha-history-updated'));

    try {
      const response = await fetch(
        apiUrl(`/api/chat/session/${encodeURIComponent(conversation.id)}`),
        { method: 'DELETE' }
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Could not delete this conversation.');
      }

      window.dispatchEvent(new CustomEvent('akansha-history-updated'));
      toast.success('Conversation deleted');
    } catch (error) {
      console.warn('[Akansha sidebar] recovered delete failure:', error);
      setRecentConversations(previousConversations);
      toast.error(error instanceof Error ? error.message : 'Could not delete this conversation.');
    }
  };

  const openChat = (options?: { newChat?: boolean; promptLibrary?: boolean }) => {
    if (options?.newChat) {
      const nextSessionId = createSessionId();
      if (currentPath === '/chat-interface') {
        window.dispatchEvent(new CustomEvent('akansha-new-chat', { detail: nextSessionId }));
      } else {
        sessionStorage.setItem('akansha-active-session', nextSessionId);
        reliableNavigate('/chat-interface');
      }
      toast.success('Started a new conversation');
      onClose?.();
      return;
    }

    if (options?.promptLibrary) {
      if (currentPath === '/chat-interface') {
        window.dispatchEvent(new CustomEvent('akansha-toggle-prompts'));
        toast.success('Opened Prompt Library');
      } else {
        sessionStorage.setItem('akansha-open-prompts', 'true');
        reliableNavigate('/chat-interface');
      }
      onClose?.();
      return;
    }

    if (currentPath !== '/chat-interface') {
      reliableNavigate('/chat-interface');
    }
    onClose?.();
  };

  const handleNavClick = (event: React.MouseEvent<HTMLElement>, label: string, href: string) => {
    // Modified clicks belong to the browser: ctrl/cmd-click opens a background
    // tab, shift-click a new window. The old handler called preventDefault()
    // unconditionally, so none of those worked on any sidebar link.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
      return;
    }

    // These three do not go to their own href — they reset the thread or open a
    // panel — so they keep the interception.
    if (label === 'New Chat') {
      event.preventDefault();
      event.stopPropagation();
      openChat({ newChat: true });
      return;
    }

    if (label === 'Chat') {
      event.preventDefault();
      event.stopPropagation();
      openChat();
      return;
    }

    if (label === 'Prompts') {
      event.preventDefault();
      event.stopPropagation();
      openChat({ promptLibrary: true });
      return;
    }

    // Already here: there is nothing to navigate to, so swallow the click.
    if (currentPath === href) {
      event.preventDefault();
      event.stopPropagation();
      toast.success(`${label} is already open`, { duration: 900 });
      onClose?.();
      return;
    }

    // Everything else navigates, and `<Link href>` is what does it. This used to
    // be preventDefault() + router.push(href), which made an imperative push the
    // *only* way out of the current route — and when that push silently no-ops
    // the sidebar just stops working, with nothing to fall back on. Measured on a
    // freshly loaded page: clicking Settings from /chat-interface left the path
    // at /chat-interface, with the click handled by React (no reload, no
    // navigation) and both the HTML and RSC responses for /settings returning
    // 200. Link performs the same prefetched client transition without that
    // single point of failure.
    onClose?.();
  };

  const handleFeatureClick = (label: string) => {
    if (label === 'Memory') {
      if (currentPath === '/chat-interface') {
        window.dispatchEvent(new CustomEvent('akansha-toggle-panel', { detail: 'context' }));
        toast.success('Opened Memory panel');
      } else {
        sessionStorage.setItem('akansha-open-memory', 'true');
        reliableNavigate('/chat-interface');
      }
      onClose?.();
    } else {
      toast('Feature Coming Soon', {
        description: `${label} is currently being optimized.`,
        icon: <Zap size={14} className="text-amber-500" />,
      });
    }
  };

  return (
    <aside
      className={`
        flex flex-col h-full bg-[hsl(var(--sidebar-bg))] border-r border-[hsl(var(--sidebar-border))]
        transition-all duration-300 ease-in-out
        ${collapsed ? 'w-16' : 'w-64'}
      `}
    >
      {/* Header */}
      <div
        className={`flex items-center h-14 px-3 border-b border-[hsl(var(--sidebar-border))] ${collapsed ? 'justify-center' : 'justify-between'}`}
      >
        {!collapsed && (
          <div className="flex items-center gap-2 min-w-0">
            <AppLogo size={28} />
            <span className="font-semibold text-base tracking-tight text-foreground truncate">
              Akansha
            </span>
          </div>
        )}
        {collapsed && <AppLogo size={28} />}
        <div className="flex items-center gap-1">
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="lg:hidden p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
              aria-label="Close sidebar"
            >
              <X size={16} />
            </button>
          )}
          <button
            type="button"
            onClick={onToggleCollapse}
            className="hidden lg:flex p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
          </button>
        </div>
      </div>

      {/* New Chat Button */}
      <div className={`p-2 border-b border-[hsl(var(--sidebar-border))]`}>
        <button
          type="button"
          onClick={(event) => handleNavClick(event, 'New Chat', '/chat-interface')}
          className={`
            w-full
            flex items-center gap-2 rounded-lg transition-all duration-150
            bg-primary hover:bg-primary-hover text-white font-medium text-sm
            active:scale-95
            ${collapsed ? 'justify-center p-2' : 'px-3 py-2'}
          `}
          title="New Chat"
        >
          <Plus size={16} />
          {!collapsed && <span>New Chat</span>}
        </button>
      </div>

      {/* Voice Quick-Launch Button */}
      <div className={`px-2 pb-2 border-b border-[hsl(var(--sidebar-border))]`}>
        <Link
          href="/voice-assistant"
          prefetch={true}
          onClick={() => {
            onClose?.();
          }}
          className={`
            w-full flex items-center gap-2 rounded-lg transition-all duration-150
            bg-gradient-to-r from-cyan-600/20 to-indigo-600/20
            border border-cyan-700/40 hover:border-cyan-400/70
            hover:from-cyan-600/30 hover:to-indigo-600/30
            text-cyan-400 hover:text-cyan-300 font-medium text-sm
            active:scale-95
            ${collapsed ? 'justify-center p-2' : 'px-3 py-2'}
          `}
          title="Voice Agent"
        >
          <Mic size={16} className="shrink-0 drop-shadow-[0_0_6px_rgba(34,211,238,0.8)]" />
          {!collapsed && <span className="flex-1 truncate tracking-wide">Voice Agent</span>}
          {!collapsed && (
            <span className="text-[10px] font-mono text-cyan-600 animate-pulse">●</span>
          )}
        </Link>
      </div>

      {/* Nav Items */}
      <nav className="p-2 border-b border-[hsl(var(--sidebar-border))]">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = item.label !== 'Prompts' && currentPath === item.href;
          const navClassName = `
            w-full
            flex items-center gap-2.5 rounded-lg px-2.5 py-2 mb-0.5 text-sm font-medium
            transition-all duration-150
            ${
              isActive
                ? 'bg-primary/10 text-primary dark:bg-primary/15 dark:text-[#9B7FFF]'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground'
            }
            ${collapsed ? 'justify-center' : ''}
          `;
          const content = (
            <>
              <Icon size={17} className="shrink-0" />
              {!collapsed && (
                <>
                  <span className="flex-1 truncate">{item.label}</span>
                  {item.badge && (
                    <span className="text-xs bg-muted text-muted-foreground px-1.5 py-0.5 rounded-full font-mono tabular-nums">
                      {item.badge}
                    </span>
                  )}
                </>
              )}
            </>
          );

          if (item.label === 'Prompts') {
            return (
              <button
                type="button"
                key={item.key}
                onClick={(event) => handleNavClick(event, item.label, item.href)}
                title={collapsed ? item.label : undefined}
                className={navClassName}
              >
                {content}
              </button>
            );
          }

          return (
            <Link
              key={item.key}
              href={item.href}
              prefetch={true}
              onClick={(event) => handleNavClick(event, item.label, item.href)}
              title={collapsed ? item.label : undefined}
              className={navClassName}
            >
              {content}
            </Link>
          );
        })}
      </nav>

      {/* Recent Conversations */}
      {!collapsed && (
        <div className="flex-1 overflow-hidden flex flex-col">
          <div className="p-2 pt-3">
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider px-2 mb-2">
              Recent
            </p>
            <div className="relative mb-2">
              <Search
                size={13}
                className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
              />
              <input
                type="text"
                placeholder="Search chats..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full bg-muted border-0 rounded-md text-xs pl-7 pr-3 py-1.5 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/50"
              />
            </div>
          </div>
          <div className="flex-1 overflow-y-auto scrollbar-thin px-2 pb-2">
            {filtered.map((conv) => (
              <div
                role="button"
                tabIndex={0}
                key={conv.id}
                onClick={() => {
                  if (currentPath === '/chat-interface') {
                    window.dispatchEvent(
                      new CustomEvent('akansha-select-session', { detail: conv.id })
                    );
                  } else {
                    sessionStorage.setItem('akansha-active-session', conv.id);
                    reliableNavigate('/chat-interface');
                  }
                  onClose?.();
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    if (currentPath === '/chat-interface') {
                      window.dispatchEvent(
                        new CustomEvent('akansha-select-session', { detail: conv.id })
                      );
                    } else {
                      sessionStorage.setItem('akansha-active-session', conv.id);
                      reliableNavigate('/chat-interface');
                    }
                    onClose?.();
                  }
                }}
                className="group flex w-full items-start gap-2 px-2 py-2 rounded-lg hover:bg-muted transition-colors cursor-pointer text-left"
              >
                <div
                  className={`w-2 h-2 rounded-full mt-1.5 shrink-0 ${modelColorMap[conv.model] || 'bg-muted-foreground'}`}
                />
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-foreground truncate">{conv.title}</p>
                  <p className="text-xs text-muted-foreground truncate">
                    {conv.model} · {formatRecentConversationDate(conv.timestamp)} ·{' '}
                    {formatRecentConversationTime(conv.timestamp)}
                  </p>
                </div>
                {conv.starred && (
                  <Star size={11} className="text-amber-400 shrink-0 mt-1" fill="currentColor" />
                )}
                <button
                  type="button"
                  onClick={(event) => deleteRecentConversation(event, conv)}
                  className="opacity-0 group-hover:opacity-100 rounded-md p-1 text-muted-foreground transition-all hover:bg-red-500/10 hover:text-red-500"
                  title="Delete chat"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
            {filtered.length === 0 && (
              <p className="text-xs text-muted-foreground text-center py-4">No chats found</p>
            )}
          </div>
        </div>
      )}

      {/* Footer */}
      <div className={`border-t border-[hsl(var(--sidebar-border))] p-2 space-y-0.5`}>
        {[
          { key: 'nav-memory', icon: MemoryStick, label: 'Memory' },
          { key: 'nav-settings', icon: Settings, label: 'Settings', href: '/settings' },
        ].map(({ key, icon: Icon, label, href }) =>
          href ? (
            <Link
              key={key}
              href={href}
              prefetch={false}
              onClick={(event) => {
                event.preventDefault();
                reliableNavigate(href);
                onClose?.();
              }}
              title={collapsed ? label : undefined}
              className={`flex items-center gap-2.5 w-full rounded-lg px-2.5 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground transition-colors ${collapsed ? 'justify-center' : ''}`}
            >
              <Icon size={16} className="shrink-0" />
              {!collapsed && <span>{label}</span>}
            </Link>
          ) : (
            <button
              type="button"
              key={key}
              onClick={() => handleFeatureClick(label)}
              title={collapsed ? label : undefined}
              className={`flex items-center gap-2.5 w-full rounded-lg px-2.5 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground transition-colors ${collapsed ? 'justify-center' : ''}`}
            >
              <Icon size={16} className="shrink-0" />
              {!collapsed && <span>{label}</span>}
            </button>
          )
        )}
        {/* User avatar */}
        <div
          className={`flex items-center justify-between px-2.5 py-2 mt-1 rounded-lg hover:bg-muted transition-colors ${collapsed ? 'flex-col gap-2' : ''}`}
        >
          <div className="w-7 h-7 rounded-full bg-gradient-to-br from-primary to-accent flex items-center justify-center text-white text-xs font-semibold shrink-0">
            A
          </div>
          {!collapsed && (
            <div className="flex-1 min-w-0">
              <p className="text-xs font-medium text-foreground truncate">
                {profile?.username || profile?.full_name || 'Arjun Mehta'}
              </p>
              <p className="text-xs text-muted-foreground truncate">Pro Plan</p>
            </div>
          )}
          <button
            type="button"
            onClick={() => {
              sessionStorage.clear();
              localStorage.clear();
              toast.success('System override: Terminating session...');
              setTimeout(() => {
                window.location.assign('/sign-up-login-screen');
              }, 800);
            }}
            className="p-1.5 text-muted-foreground hover:text-red-500 hover:bg-red-500/10 rounded-md transition-colors"
            title="Log out"
          >
            <LogOut size={16} />
          </button>
        </div>
      </div>
    </aside>
  );
}
