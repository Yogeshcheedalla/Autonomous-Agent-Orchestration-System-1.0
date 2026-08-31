'use client';

import React, { useState } from 'react';
import dynamic from 'next/dynamic';
import ConversationSidebar from './ConversationSidebar';
import ChatThread from './ChatThread';
import { PanelLeft } from 'lucide-react';

/**
 * Both of these are loaded on demand rather than bundled into the route.
 *
 * `contextPanelOpen` and `isPromptModalOpen` both start false, so neither
 * component renders on first paint -- yet a static import put both of them in
 * /chat-interface's initial chunk, and ContextPanel drags TaskCalendarPanel (the
 * second-largest component in the app) in behind it. That is a drawer and a modal
 * being paid for by every visitor who never opens either.
 *
 * SSR stays on: these are client components either way, and disabling it would
 * only trade bundle weight for a hydration flash once the drawer is open.
 */
const ContextPanel = dynamic(() => import('./ContextPanel'), {
  loading: () => <PanelSkeleton />,
});
const PromptTemplateModal = dynamic(() => import('./PromptTemplateModal'));
/**
 * Same treatment, for the same reason: this is the old `/browser-automation`
 * page's working parts, and a visitor who never opens it should not download it.
 */
const AutomationPanel = dynamic(() => import('./AutomationPanel'), {
  loading: () => <PanelSkeleton />,
});

function PanelSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4" aria-hidden>
      <div className="h-4 w-2/3 animate-pulse rounded bg-muted" />
      <div className="h-3 w-full animate-pulse rounded bg-muted/70" />
      <div className="h-3 w-5/6 animate-pulse rounded bg-muted/70" />
      <div className="mt-2 h-24 w-full animate-pulse rounded-lg bg-muted/50" />
    </div>
  );
}

function createSessionId() {
  return `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export default function ChatWorkspace() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [contextPanelOpen, setContextPanelOpen] = useState(false);
  const [automationPanelOpen, setAutomationPanelOpen] = useState(false);
  const [sessionId, setSessionId] = useState('default');
  const [chatStats, setChatStats] = useState({ messages: 0, contextUnits: 0 });
  const [isPromptModalOpen, setIsPromptModalOpen] = useState(false);

  // Stable, because `ConversationSidebar` is wrapped in `memo` and receives this
  // as a prop. A new function identity on every render would fail the props
  // comparison every time and make that wrapper inert -- the sidebar would keep
  // re-rendering on every streamed token, which is exactly what the memo is there
  // to stop. `setSessionId` and `setIsPromptModalOpen` are setters, so they are
  // stable and the dep list is genuinely empty.
  const startNewChat = React.useCallback((nextSessionId?: string) => {
    setSessionId(nextSessionId ?? createSessionId());
    setIsPromptModalOpen(false);
  }, []);

  const handleStatsChange = React.useCallback((messages: number, contextUnits: number) => {
    setChatStats((previous) => {
      if (previous.messages === messages && previous.contextUnits === contextUnits) {
        return previous;
      }
      return { messages, contextUnits };
    });
  }, []);

  // Same reasoning as `startNewChat`: these are the only props `ContextPanel` and
  // `PromptTemplateModal` receive that could change identity per render, so an
  // inline arrow here would defeat the `memo` on the other side of the boundary.
  const closeContextPanel = React.useCallback(() => setContextPanelOpen(false), []);
  const closePromptModal = React.useCallback(() => setIsPromptModalOpen(false), []);
  const closeAutomationPanel = React.useCallback(() => setAutomationPanelOpen(false), []);

  const applyPromptTemplate = React.useCallback((prompt: string) => {
    if (prompt.includes('[CONTINUOUS_AUTOMATION]')) {
      const continuousTabId = `sess-continuous-${Date.now()}`;
      setSessionId(continuousTabId);
      sessionStorage.setItem('akansha-current-session', continuousTabId);
      setTimeout(() => {
        window.dispatchEvent(new CustomEvent('akansha-apply-prompt', { detail: prompt }));
      }, 100);
    } else {
      window.dispatchEvent(new CustomEvent('akansha-apply-prompt', { detail: prompt }));
    }
    setIsPromptModalOpen(false);
  }, []);

  React.useEffect(() => {
    if (sessionId !== 'default') {
      sessionStorage.setItem('akansha-current-session', sessionId);
    }
  }, [sessionId]);

  React.useEffect(() => {
    const handleTogglePanel = () => {
      setContextPanelOpen((open) => !open);
    };
    const handleTogglePrompts = () => {
      setIsPromptModalOpen(true);
    };
    const handleToggleAutomation = () => {
      setAutomationPanelOpen((open) => !open);
    };
    const handleSelectSession = (event: WindowEventMap['akansha-select-session']) => {
      const session = event.detail;
      if (session) setSessionId(session);
    };

    const handleNewChatWithDetail = (event: WindowEventMap['akansha-new-chat']) => {
      const nextSessionId = event.detail;
      startNewChat(nextSessionId);
    };

    window.addEventListener('akansha-new-chat', handleNewChatWithDetail);
    window.addEventListener('akansha-toggle-panel', handleTogglePanel);
    window.addEventListener('akansha-toggle-prompts', handleTogglePrompts);
    window.addEventListener('akansha-toggle-automation', handleToggleAutomation);
    window.addEventListener('akansha-select-session', handleSelectSession);

    const pendingSession = sessionStorage.getItem('akansha-active-session');
    if (pendingSession) {
      setSessionId(pendingSession);
      sessionStorage.removeItem('akansha-active-session');
    } else {
      setSessionId(sessionStorage.getItem('akansha-current-session') || createSessionId());
    }

    if (sessionStorage.getItem('akansha-open-memory') === 'true') {
      setContextPanelOpen(true);
      sessionStorage.removeItem('akansha-open-memory');
    }

    if (sessionStorage.getItem('akansha-open-prompts') === 'true') {
      setIsPromptModalOpen(true);
      sessionStorage.removeItem('akansha-open-prompts');
    }

    return () => {
      window.removeEventListener('akansha-new-chat', handleNewChatWithDetail);
      window.removeEventListener('akansha-toggle-panel', handleTogglePanel);
      window.removeEventListener('akansha-toggle-prompts', handleTogglePrompts);
      window.removeEventListener('akansha-toggle-automation', handleToggleAutomation);
      window.removeEventListener('akansha-select-session', handleSelectSession);
    };
    // `startNewChat` is useCallback'd with an empty dep list, so listing it here
    // does not make this a re-subscribing effect -- it stays mount-only.
  }, [startNewChat]);

  const isContinuousTab = sessionId.startsWith('sess-continuous-');

  return (
    <div className="flex h-full overflow-hidden">
      {/* Conversation sidebar */}
      {sidebarOpen && (
        <div className="hidden xl:flex w-64 shrink-0 border-r border-border flex-col bg-card/50">
          <ConversationSidebar
            activeSessionId={sessionId}
            onNewChat={startNewChat}
            onSessionChange={setSessionId}
          />
        </div>
      )}

      <div className="hidden xl:flex w-11 shrink-0 border-r border-border bg-card/30 items-start justify-center pt-3">
        <button
          onClick={() => setSidebarOpen((open) => !open)}
          className="flex items-center justify-center w-8 h-8 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors bg-card/80 backdrop-blur"
          title={sidebarOpen ? 'Hide conversations' : 'Show conversations'}
          aria-label={sidebarOpen ? 'Hide conversations' : 'Show conversations'}
        >
          <PanelLeft
            size={16}
            className={`transition-transform duration-200 ${sidebarOpen ? '' : 'rotate-180'}`}
          />
        </button>
      </div>

      {/* Left memory/context drawer */}
      {contextPanelOpen && (
        <div className="hidden lg:flex w-80 shrink-0 border-r border-border flex-col bg-card/50">
          <ContextPanel
            onClose={closeContextPanel}
            messageCount={chatStats.messages}
            contextUnits={chatStats.contextUnits}
          />
        </div>
      )}

      {/* Main chat area */}
      <div className="flex-1 flex flex-col min-w-0 relative">
        {isContinuousTab && (
          <div className="bg-emerald-500/10 border-b border-emerald-500/20 px-4 py-2 flex items-center justify-between text-xs text-emerald-400 font-mono backdrop-blur shrink-0 z-10">
            <div className="flex items-center gap-2">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
              </span>
              <span>🔒 Isolated Continuous Automation Tab Active ({sessionId})</span>
            </div>
            <span className="text-emerald-500/70 text-[10px]">
              Zero Interference · Target Tab Scope Locked
            </span>
          </div>
        )}
        <ChatThread key={sessionId} sessionId={sessionId} onStatsChange={handleStatsChange} />
      </div>

      {/* Automation drawer, on the right so it reads as a status surface for the
          conversation rather than another place to start one. Gated rather than
          hidden with CSS: mounting is what fetches the chunk. */}
      {automationPanelOpen && (
        <div className="hidden lg:flex w-80 shrink-0 flex-col border-l border-border bg-card/50">
          <AutomationPanel onClose={closeAutomationPanel} />
        </div>
      )}
      {/* Modals. Gated on `isPromptModalOpen` rather than always mounted: the
          component already early-returns null when closed, so rendering it
          unconditionally cost nothing visually -- but with a dynamic import the
          mount is what triggers the chunk fetch, so an always-mounted modal would
          download itself immediately and undo the split. */}
      {isPromptModalOpen && (
        <PromptTemplateModal
          open={isPromptModalOpen}
          onClose={closePromptModal}
          onSelect={applyPromptTemplate}
        />
      )}
    </div>
  );
}
