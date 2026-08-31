'use client';

import React, { useCallback, useEffect, useState } from 'react';
import FolderSidebar from './FolderSidebar';
import ConversationList from './ConversationList';
import ConversationPreview from './ConversationPreview';
import { clearSessionTitles, deleteSessionTitle, getSessionTitle } from '@/hooks/chatSessionTitles';
import {
  clearConversationMetadata,
  DEFAULT_CONVERSATION_FOLDERS,
  readConversationFolders,
  readConversationMetadata,
  slugFolderId,
  writeConversationFolders,
  writeConversationMetadata,
  type ConversationFolder,
  type ConversationMetadata,
  type ConversationStatus,
} from '@/lib/chatHistoryMetadata';
import { toast } from 'sonner';
import { apiUrl } from '@/lib/apiBase';
import type { ChatHistoryMessage, ChatHistoryResponse } from '@/types/chatApi';

export type ConversationMessage = {
  id: number | string;
  role: string;
  content: string;
  timestamp?: string;
};

export type Conversation = {
  id: string;
  title: string;
  summary: string;
  model: string;
  modelColor: string;
  folder: string;
  folderId: string;
  messageCount: number;
  tokenCount: number;
  createdAt: string;
  updatedAt: string;
  starred: boolean;
  shared: boolean;
  shareVisibility?: 'public' | 'private';
  hasMemory: boolean;
  hasAttachments: boolean;
  tags: string[];
  status: ConversationStatus;
  messages: ConversationMessage[];
};

export const ALL_CONVERSATIONS: Conversation[] = [
  {
    id: 'conv-h001',
    title: 'Refactor auth middleware',
    summary:
      'Discussed async/await patterns for Express.js auth middleware, JWT error handling, and rate limiting with Redis store for distributed deployments.',
    model: 'GPT-4o',
    modelColor: 'bg-green-500',
    folder: 'Work',
    folderId: 'folder-work',
    messageCount: 14,
    tokenCount: 4218,
    createdAt: '2026-04-24T08:32:00Z',
    updatedAt: '2026-04-24T09:15:00Z',
    starred: true,
    shared: false,
    hasMemory: true,
    hasAttachments: true,
    tags: ['typescript', 'backend', 'security'],
    status: 'active',
    messages: [],
  },
  {
    id: 'conv-h002',
    title: 'Explain RLHF in simple terms',
    summary:
      'Explained Reinforcement Learning from Human Feedback using real-world analogies — training loop, reward models, and why RLHF matters for alignment.',
    model: 'Claude 3.5 Sonnet',
    modelColor: 'bg-orange-400',
    folder: 'Research',
    folderId: 'folder-research',
    messageCount: 7,
    tokenCount: 2841,
    createdAt: '2026-04-24T07:10:00Z',
    updatedAt: '2026-04-24T07:48:00Z',
    starred: true,
    shared: true,
    shareVisibility: 'public',
    hasMemory: false,
    hasAttachments: false,
    tags: ['ai', 'ml', 'research'],
    status: 'active',
    messages: [],
  },
  {
    id: 'conv-h003',
    title: 'Draft Q2 product roadmap',
    summary:
      'Drafted a comprehensive Q2 product roadmap with feature prioritization, resource allocation, and OKR alignment for a B2B SaaS product team.',
    model: 'Gemini 1.5 Pro',
    modelColor: 'bg-blue-400',
    folder: 'Work',
    folderId: 'folder-work',
    messageCount: 22,
    tokenCount: 8932,
    createdAt: '2026-04-23T14:20:00Z',
    updatedAt: '2026-04-23T16:05:00Z',
    starred: false,
    shared: true,
    shareVisibility: 'private',
    hasMemory: true,
    hasAttachments: true,
    tags: ['product', 'strategy', 'planning'],
    status: 'summarized',
    messages: [],
  },
  {
    id: 'conv-h004',
    title: 'SQL query optimization tips',
    summary:
      'Covered index strategies, query execution plans, EXPLAIN ANALYZE in PostgreSQL, N+1 problem solutions, and connection pooling best practices.',
    model: 'GPT-4o',
    modelColor: 'bg-green-500',
    folder: 'Work',
    folderId: 'folder-work',
    messageCount: 9,
    tokenCount: 3140,
    createdAt: '2026-04-23T11:00:00Z',
    updatedAt: '2026-04-23T11:52:00Z',
    starred: true,
    shared: false,
    hasMemory: true,
    hasAttachments: false,
    tags: ['database', 'postgresql', 'performance'],
    status: 'active',
    messages: [],
  },
  {
    id: 'conv-h005',
    title: 'Kubernetes pod scheduling',
    summary:
      'Deep dive into Kubernetes scheduling algorithms, node affinity/anti-affinity rules, taints and tolerations, and resource request/limit tuning.',
    model: 'Claude 3.5 Sonnet',
    modelColor: 'bg-orange-400',
    folder: 'Work',
    folderId: 'folder-work',
    messageCount: 16,
    tokenCount: 5677,
    createdAt: '2026-04-23T09:30:00Z',
    updatedAt: '2026-04-23T10:41:00Z',
    starred: false,
    shared: false,
    hasMemory: false,
    hasAttachments: true,
    tags: ['devops', 'kubernetes', 'infrastructure'],
    status: 'active',
    messages: [],
  },
  {
    id: 'conv-h006',
    title: 'RAG pipeline architecture',
    summary:
      'Designed a production RAG pipeline with chunking strategies, embedding models, vector database selection (Pinecone vs Weaviate), and re-ranking approaches.',
    model: 'GPT-4o',
    modelColor: 'bg-green-500',
    folder: 'Research',
    folderId: 'folder-research',
    messageCount: 31,
    tokenCount: 12480,
    createdAt: '2026-04-22T15:00:00Z',
    updatedAt: '2026-04-22T17:30:00Z',
    starred: false,
    shared: false,
    hasMemory: true,
    hasAttachments: true,
    tags: ['rag', 'vector-db', 'embeddings'],
    status: 'summarized',
    messages: [],
  },
  {
    id: 'conv-h007',
    title: 'React Server Components deep dive',
    summary:
      'Explored RSC architecture, server/client component boundaries, data fetching patterns, streaming with Suspense, and migration strategies from Pages Router.',
    model: 'Claude 3.5 Sonnet',
    modelColor: 'bg-orange-400',
    folder: 'Work',
    folderId: 'folder-work',
    messageCount: 18,
    tokenCount: 6823,
    createdAt: '2026-04-22T10:00:00Z',
    updatedAt: '2026-04-22T11:45:00Z',
    starred: true,
    shared: true,
    shareVisibility: 'public',
    hasMemory: false,
    hasAttachments: false,
    tags: ['react', 'nextjs', 'frontend'],
    status: 'active',
    messages: [],
  },
  {
    id: 'conv-h008',
    title: 'Pricing strategy for SaaS',
    summary:
      'Analyzed freemium vs free trial models, seat-based vs usage-based pricing, value metric selection, and competitive pricing analysis for a developer tool.',
    model: 'Gemini 1.5 Pro',
    modelColor: 'bg-blue-400',
    folder: 'Personal',
    folderId: 'folder-personal',
    messageCount: 12,
    tokenCount: 4102,
    createdAt: '2026-04-21T16:00:00Z',
    updatedAt: '2026-04-21T17:10:00Z',
    starred: false,
    shared: false,
    hasMemory: false,
    hasAttachments: false,
    tags: ['business', 'pricing', 'strategy'],
    status: 'active',
    messages: [],
  },
  {
    id: 'conv-h009',
    title: 'WebSocket vs SSE for streaming',
    summary:
      'Compared WebSocket and Server-Sent Events for real-time AI streaming, covering latency, reconnection, proxy compatibility, and implementation complexity.',
    model: 'GPT-4o',
    modelColor: 'bg-green-500',
    folder: 'Research',
    folderId: 'folder-research',
    messageCount: 8,
    tokenCount: 2910,
    createdAt: '2026-04-21T09:00:00Z',
    updatedAt: '2026-04-21T09:44:00Z',
    starred: false,
    shared: false,
    hasMemory: false,
    hasAttachments: false,
    tags: ['networking', 'streaming', 'backend'],
    status: 'archived',
    messages: [],
  },
  {
    id: 'conv-h010',
    title: 'Write onboarding email sequence',
    summary:
      'Created a 5-email onboarding sequence for a developer tool — welcome, feature discovery, first success moment, social proof, and upgrade nudge.',
    model: 'Claude 3.5 Sonnet',
    modelColor: 'bg-orange-400',
    folder: 'Personal',
    folderId: 'folder-personal',
    messageCount: 11,
    tokenCount: 3560,
    createdAt: '2026-04-20T14:00:00Z',
    updatedAt: '2026-04-20T15:20:00Z',
    starred: false,
    shared: false,
    hasMemory: false,
    hasAttachments: false,
    tags: ['writing', 'marketing', 'email'],
    status: 'active',
    messages: [],
  },
  {
    id: 'conv-h011',
    title: 'Terraform module for ECS',
    summary:
      'Built a reusable Terraform module for AWS ECS Fargate with ALB, auto-scaling, CloudWatch logging, secrets management, and IAM role patterns.',
    model: 'GPT-4o',
    modelColor: 'bg-green-500',
    folder: 'Work',
    folderId: 'folder-work',
    messageCount: 24,
    tokenCount: 9140,
    createdAt: '2026-04-19T11:00:00Z',
    updatedAt: '2026-04-19T13:30:00Z',
    starred: false,
    shared: false,
    hasMemory: true,
    hasAttachments: true,
    tags: ['terraform', 'aws', 'devops'],
    status: 'summarized',
    messages: [],
  },
  {
    id: 'conv-h012',
    title: 'Explain attention mechanism',
    summary:
      'Detailed explanation of self-attention, multi-head attention, positional encoding, and how transformers process sequential data without recurrence.',
    model: 'Gemini 1.5 Pro',
    modelColor: 'bg-blue-400',
    folder: 'Research',
    folderId: 'folder-research',
    messageCount: 15,
    tokenCount: 5230,
    createdAt: '2026-04-18T10:00:00Z',
    updatedAt: '2026-04-18T11:20:00Z',
    starred: false,
    shared: true,
    shareVisibility: 'public',
    hasMemory: false,
    hasAttachments: false,
    tags: ['ai', 'transformers', 'research'],
    status: 'active',
    messages: [],
  },
];

function buildLiveConversations(
  messages: ChatHistoryMessage[],
  folders: ConversationFolder[],
  metadata: ReturnType<typeof readConversationMetadata>
): Conversation[] {
  const folderById = new Map(folders.map((folder) => [folder.id, folder.name]));
  const sessions = new Map<
    string,
    {
      id: string;
      first: Date;
      latest: Date;
      title: string;
      summary: string;
      messageCount: number;
      tokenCount: number;
      hasAttachments: boolean;
      messages: ConversationMessage[];
    }
  >();

  messages.forEach((message) => {
    const sessionId = message.session_id || 'default';
    const timestamp = message.timestamp ? new Date(message.timestamp) : new Date();
    const content = String(message.content || '').trim();
    const existing = sessions.get(sessionId);
    const fallbackTitle = message.role === 'user' && content ? content : 'New chat';
    // The wire row's `timestamp` is nullable (the handler emits None for rows that
    // predate the column) while ConversationMessage treats it as optional, so it
    // has to be normalised rather than passed through as a literal null.
    const conversationMessage: ConversationMessage = {
      ...message,
      timestamp: message.timestamp ?? undefined,
    };

    if (!existing) {
      sessions.set(sessionId, {
        id: sessionId,
        first: timestamp,
        latest: timestamp,
        title: getSessionTitle(sessionId, fallbackTitle),
        summary: content || 'No message content saved yet.',
        messageCount: 1,
        tokenCount: Math.max(1, Math.floor(content.length / 4)),
        // Always false, and honestly so. This used to read
        // `Boolean(message.attachments?.length)`, but GET /api/chat returns no
        // `attachments` field and `chat_messages` has no column for one -- so the
        // read was undefined on every row and the indicator could never light up.
        // Persisting attachments is a schema change, not a cleanup; until then the
        // flag says what is actually known.
        hasAttachments: false,
        messages: [conversationMessage],
      });
      return;
    }

    existing.messageCount += 1;
    existing.tokenCount += Math.max(1, Math.floor(content.length / 4));
    existing.messages.push(conversationMessage);

    if (timestamp < existing.first) existing.first = timestamp;
    if (timestamp >= existing.latest) {
      existing.latest = timestamp;
      if (content) existing.summary = content;
    }

    if (message.role === 'user' && existing.title === 'New chat' && content) {
      existing.title = getSessionTitle(sessionId, content);
    }
  });

  return Array.from(sessions.values())
    .map((session) => {
      const sessionMeta = metadata[session.id] ?? {};
      const folderId = sessionMeta.folderId || DEFAULT_CONVERSATION_FOLDERS[0].id;
      return {
        id: session.id,
        title: getSessionTitle(session.id, session.title || 'New chat'),
        summary:
          session.summary.length > 180
            ? `${session.summary.slice(0, 177).trim()}...`
            : session.summary,
        model: 'Akansha',
        modelColor: 'bg-green-500',
        folder: folderById.get(folderId) || 'Chats',
        folderId,
        messageCount: session.messageCount,
        tokenCount: session.tokenCount,
        createdAt: session.first.toISOString(),
        updatedAt: session.latest.toISOString(),
        starred: Boolean(sessionMeta.starred),
        shared: Boolean(sessionMeta.shared),
        hasMemory: true,
        hasAttachments: session.hasAttachments,
        tags: ['chat'],
        status: sessionMeta.status || 'active',
        messages: session.messages.sort(
          (a, b) => new Date(a.timestamp || 0).getTime() - new Date(b.timestamp || 0).getTime()
        ),
      };
    })
    .sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
}

export default function ConversationHistoryScreen() {
  const [selectedFolder, setSelectedFolder] = useState<string | null>(null);
  const [folders, setFolders] = useState<ConversationFolder[]>(DEFAULT_CONVERSATION_FOLDERS);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedConversation, setSelectedConversation] = useState<Conversation | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);

  const loadConversations = useCallback(async () => {
    setLoading(true);
    try {
      const storedFolders = readConversationFolders();
      const metadata = readConversationMetadata();
      setFolders(storedFolders);
      const response = await fetch(apiUrl('/api/chat'));
      const payload = (await response.json()) as ChatHistoryResponse;
      const liveConversations = buildLiveConversations(
        payload.messages ?? [],
        storedFolders,
        metadata
      );
      setConversations(liveConversations);
      setSelectedConversation((current) => {
        if (!current) return liveConversations[0] ?? null;
        return (
          liveConversations.find((conversation) => conversation.id === current.id) ??
          liveConversations[0] ??
          null
        );
      });
    } catch (error) {
      console.warn('Failed to load conversation history:', error);
      setConversations([]);
      setSelectedConversation(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const updateConversationMetadata = useCallback(
    (ids: string[], update: ConversationMetadata) => {
      const metadata = readConversationMetadata();
      ids.forEach((id) => {
        metadata[id] = { ...(metadata[id] ?? {}), ...update };
      });
      writeConversationMetadata(metadata);
      void loadConversations();
    },
    [loadConversations]
  );

  useEffect(() => {
    void loadConversations();
    window.addEventListener('akansha-history-updated', loadConversations);
    return () => window.removeEventListener('akansha-history-updated', loadConversations);
  }, [loadConversations]);

  const deleteConversations = useCallback(
    async (ids: string[]) => {
      if (!ids.length) return;

      try {
        await Promise.all(
          ids.map(async (id) => {
            const response = await fetch(apiUrl(`/api/chat/session/${encodeURIComponent(id)}`), {
              method: 'DELETE',
            });
            if (!response.ok) {
              const payload = await response.json().catch(() => ({}));
              throw new Error(payload.detail || `Could not delete ${id}.`);
            }
          })
        );

        ids.forEach(deleteSessionTitle);
        setConversations((current) =>
          current.filter((conversation) => !ids.includes(conversation.id))
        );
        setSelectedIds(new Set());
        setSelectedConversation((current) =>
          current && ids.includes(current.id)
            ? (conversations.find((conversation) => !ids.includes(conversation.id)) ?? null)
            : current
        );
        window.dispatchEvent(new CustomEvent('akansha-history-updated'));
        toast.success(`${ids.length} conversation${ids.length > 1 ? 's' : ''} deleted`);
      } catch (error) {
        console.warn('Failed to delete history:', error);
        toast.error(error instanceof Error ? error.message : 'Could not delete history.');
      }
    },
    [conversations]
  );

  const clearHistory = useCallback(async () => {
    const confirmed = window.confirm('Delete all saved chat history? This cannot be undone.');
    if (!confirmed) return;

    try {
      const response = await fetch(apiUrl('/api/chat/history'), {
        method: 'DELETE',
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Could not clear chat history.');
      }

      clearSessionTitles();
      clearConversationMetadata();
      setConversations([]);
      setSelectedConversation(null);
      setSelectedIds(new Set());
      window.dispatchEvent(new CustomEvent('akansha-history-updated'));
      toast.success('Chat history cleared');
    } catch (error) {
      console.warn('Failed to clear chat history:', error);
      toast.error(error instanceof Error ? error.message : 'Could not clear chat history.');
    }
  }, []);

  const createFolder = useCallback(
    (name: string) => {
      const nextFolder = { id: slugFolderId(name), name };
      const nextFolders = [...folders, nextFolder];
      writeConversationFolders(nextFolders);
      setFolders(nextFolders);
      setSelectedFolder(nextFolder.id);
      toast.success(`Folder "${name}" created`);
    },
    [folders]
  );

  const moveConversations = useCallback(
    (ids: string[], folderId: string) => {
      const folder = folders.find((item) => item.id === folderId);
      if (!folder) {
        toast.error('Choose a valid folder');
        return;
      }

      updateConversationMetadata(ids, { folderId });
      toast.success(
        `${ids.length} conversation${ids.length > 1 ? 's' : ''} moved to ${folder.name}`
      );
    },
    [folders, updateConversationMetadata]
  );

  const archiveConversations = useCallback(
    (ids: string[]) => {
      updateConversationMetadata(ids, { status: 'archived' });
      toast.success(`${ids.length} conversation${ids.length > 1 ? 's' : ''} archived`);
    },
    [updateConversationMetadata]
  );

  const toggleStar = useCallback(
    (id: string) => {
      const current = conversations.find((conversation) => conversation.id === id);
      updateConversationMetadata([id], { starred: !current?.starred });
      toast.success(current?.starred ? 'Removed from starred' : 'Added to starred');
    },
    [conversations, updateConversationMetadata]
  );

  const exportConversations = useCallback(
    (ids: string[]) => {
      const selected = conversations.filter((conversation) => ids.includes(conversation.id));
      if (!selected.length) return;

      const blob = new Blob([JSON.stringify(selected, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `akansha-conversations-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
      toast.success(`${selected.length} conversation${selected.length > 1 ? 's' : ''} exported`);
    },
    [conversations]
  );

  const emptyTrash = useCallback(() => {
    const archivedIds = conversations
      .filter((conversation) => conversation.status === 'archived')
      .map((conversation) => conversation.id);

    if (!archivedIds.length) {
      toast.info('No archived conversations to delete');
      return;
    }

    const confirmed = window.confirm(
      `Delete ${archivedIds.length} archived conversation${archivedIds.length > 1 ? 's' : ''}?`
    );
    if (!confirmed) return;
    void deleteConversations(archivedIds);
  }, [conversations, deleteConversations]);

  return (
    <div className="flex h-full overflow-hidden">
      {/* Folder sidebar */}
      <div className="hidden md:flex w-52 shrink-0 border-r border-border flex-col bg-card/50">
        <FolderSidebar
          selectedFolder={selectedFolder}
          onSelectFolder={setSelectedFolder}
          conversations={conversations}
          folders={folders}
          onCreateFolder={createFolder}
          onEmptyTrash={emptyTrash}
        />
      </div>

      {/* Conversation list */}
      <div
        className={`flex flex-col min-w-0 border-r border-border bg-background ${selectedConversation ? 'hidden lg:flex lg:w-[420px] xl:w-[480px] shrink-0' : 'flex-1'}`}
      >
        <ConversationList
          conversations={conversations}
          selectedFolder={selectedFolder}
          selectedConversation={selectedConversation}
          onSelectConversation={setSelectedConversation}
          selectedIds={selectedIds}
          onSelectionChange={setSelectedIds}
          loading={loading}
          onDeleteConversations={deleteConversations}
          onClearHistory={clearHistory}
          folders={folders}
          onMoveConversations={moveConversations}
          onArchiveConversations={archiveConversations}
          onExportConversations={exportConversations}
        />
      </div>

      {/* Preview panel */}
      <div className={`flex-1 min-w-0 ${selectedConversation ? 'flex' : 'hidden lg:flex'}`}>
        {selectedConversation ? (
          <ConversationPreview
            conversation={selectedConversation}
            onClose={() => setSelectedConversation(null)}
            onDeleteConversation={(id) => deleteConversations([id])}
            onArchiveConversation={(id) => archiveConversations([id])}
            onToggleStar={toggleStar}
            onExportConversation={(id) => exportConversations([id])}
          />
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center text-center p-8">
            <div className="w-16 h-16 rounded-2xl bg-muted flex items-center justify-center mb-4">
              <svg
                width="28"
                height="28"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                className="text-muted-foreground"
              >
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              </svg>
            </div>
            <p className="text-sm font-medium text-muted-foreground">
              Select a conversation to preview
            </p>
            <p className="text-xs text-muted-foreground/60 mt-1">
              Click any conversation from the list
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
