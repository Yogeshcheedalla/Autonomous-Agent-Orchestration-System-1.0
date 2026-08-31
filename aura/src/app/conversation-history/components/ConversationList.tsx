'use client';

import React, { useState, useMemo } from 'react';
import Link from 'next/link';
import {
  Search,
  Star,
  Share2,
  Brain,
  Paperclip,
  MessageSquare,
  Check,
  X,
  Trash2,
  FolderInput,
  Download,
  ArrowUpDown,
  Filter,
  Archive,
} from 'lucide-react';
import type { Conversation } from './ConversationHistoryScreen';
import type { ConversationFolder } from '@/lib/chatHistoryMetadata';
import { toast } from 'sonner';

interface ConversationListProps {
  conversations: Conversation[];
  selectedFolder: string | null;
  selectedConversation: Conversation | null;
  onSelectConversation: (conv: Conversation) => void;
  selectedIds: Set<string>;
  onSelectionChange: (ids: Set<string>) => void;
  loading?: boolean;
  onDeleteConversations?: (ids: string[]) => Promise<void> | void;
  onClearHistory?: () => Promise<void> | void;
  folders: ConversationFolder[];
  onMoveConversations?: (ids: string[], folderId: string) => void;
  onArchiveConversations?: (ids: string[]) => void;
  onExportConversations?: (ids: string[]) => void;
}

const SORT_OPTIONS = [
  { key: 'sort-updated', label: 'Last updated', value: 'updated' },
  { key: 'sort-created', label: 'Date created', value: 'created' },
  { key: 'sort-messages', label: 'Message count', value: 'messages' },
  { key: 'sort-tokens', label: 'Context size', value: 'tokens' },
];

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = Math.max(0, now.getTime() - date.getTime());
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

const STATUS_BADGES: Record<string, { label: string; className: string }> = {
  active: {
    label: 'Active',
    className: 'bg-green-500/10 text-green-600 dark:text-green-400 border-green-500/20',
  },
  summarized: {
    label: 'Summarized',
    className: 'bg-primary/10 text-primary dark:text-[#9B7FFF] border-primary/20',
  },
  archived: { label: 'Archived', className: 'bg-muted text-muted-foreground border-border' },
};

export default function ConversationList({
  conversations,
  selectedFolder,
  selectedConversation,
  onSelectConversation,
  selectedIds,
  onSelectionChange,
  loading = false,
  onDeleteConversations,
  onClearHistory,
  folders,
  onMoveConversations,
  onArchiveConversations,
  onExportConversations,
}: ConversationListProps) {
  const [search, setSearch] = useState('');
  const [selectedModel, setSelectedModel] = useState('All models');
  const [sortBy, setSortBy] = useState('updated');
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const [sortMenuOpen, setSortMenuOpen] = useState(false);
  const [showStarredOnly, setShowStarredOnly] = useState(false);
  const [showSharedOnly, setShowSharedOnly] = useState(false);
  const modelOptions = useMemo(
    () => [
      'All models',
      ...Array.from(new Set(conversations.map((conversation) => conversation.model))).sort(),
    ],
    [conversations]
  );

  const filtered = useMemo(() => {
    let result = [...conversations];

    // Folder filter
    if (selectedFolder === 'starred') result = result.filter((c) => c.starred);
    else if (selectedFolder === 'shared') result = result.filter((c) => c.shared);
    else if (selectedFolder === 'archived') result = result.filter((c) => c.status === 'archived');
    else if (selectedFolder) result = result.filter((c) => c.folderId === selectedFolder);

    // Search
    if (search) {
      const q = search.toLowerCase();
      result = result.filter(
        (c) =>
          c.title.toLowerCase().includes(q) ||
          c.summary.toLowerCase().includes(q) ||
          c.tags.some((t) => t.toLowerCase().includes(q))
      );
    }

    // Model filter
    if (selectedModel !== 'All models') {
      result = result.filter((c) => c.model === selectedModel);
    }

    // Extra filters
    if (showStarredOnly) result = result.filter((c) => c.starred);
    if (showSharedOnly) result = result.filter((c) => c.shared);

    // Sort
    result.sort((a, b) => {
      if (sortBy === 'updated')
        return new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime();
      if (sortBy === 'created')
        return new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime();
      if (sortBy === 'messages') return b.messageCount - a.messageCount;
      if (sortBy === 'tokens') return b.tokenCount - a.tokenCount;
      return 0;
    });

    return result;
  }, [
    conversations,
    selectedFolder,
    search,
    selectedModel,
    sortBy,
    showStarredOnly,
    showSharedOnly,
  ]);

  const toggleSelect = (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelectionChange(next);
  };

  const selectAll = () => {
    if (selectedIds.size === filtered.length) {
      onSelectionChange(new Set());
    } else {
      onSelectionChange(new Set(filtered.map((c) => c.id)));
    }
  };

  const handleBulkDelete = async () => {
    if (!selectedIds.size) return;
    const confirmed = window.confirm(
      `Delete ${selectedIds.size} selected conversation${selectedIds.size > 1 ? 's' : ''}?`
    );
    if (!confirmed) return;

    await onDeleteConversations?.(Array.from(selectedIds));
    onSelectionChange(new Set());
  };

  const handleBulkMove = () => {
    if (!selectedIds.size) return;

    const folderNames = folders.map((folder) => folder.name).join(', ');
    const folderName = window.prompt(`Move to folder: ${folderNames}`, folders[0]?.name || '');
    if (!folderName) return;

    const folder = folders.find(
      (item) => item.name.toLowerCase() === folderName.trim().toLowerCase()
    );
    if (!folder) {
      toast.error('Folder not found');
      return;
    }

    onMoveConversations?.(Array.from(selectedIds), folder.id);
    onSelectionChange(new Set());
  };

  const handleBulkArchive = () => {
    if (!selectedIds.size) return;
    onArchiveConversations?.(Array.from(selectedIds));
    onSelectionChange(new Set());
  };

  const handleBulkExport = () => {
    if (!selectedIds.size) return;
    onExportConversations?.(Array.from(selectedIds));
  };

  const activeFilterCount = [
    selectedModel !== 'All models',
    showStarredOnly,
    showSharedOnly,
  ].filter(Boolean).length;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-4 py-3 border-b border-border">
        <div className="flex items-center justify-between mb-2.5">
          <h1 className="text-base font-semibold text-foreground">Conversations</h1>
          <span className="text-xs text-muted-foreground font-mono tabular-nums">
            {filtered.length} results
          </span>
        </div>

        {/* Search */}
        <div className="relative mb-2">
          <Search
            size={14}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
          />
          <input
            type="text"
            placeholder="Search conversations, tags..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full bg-muted rounded-xl text-sm pl-9 pr-9 py-2 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/40 border-0"
          />
          {search && (
            <button
              onClick={() => setSearch('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              <X size={13} />
            </button>
          )}
        </div>

        {/* Filter + Sort row */}
        <div className="flex items-center gap-2">
          {/* Filter */}
          <div className="relative">
            <button
              onClick={() => {
                setFilterMenuOpen(!filterMenuOpen);
                setSortMenuOpen(false);
              }}
              className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs font-medium transition-colors ${
                activeFilterCount > 0
                  ? 'border-primary/40 bg-primary/10 text-primary'
                  : 'border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground'
              }`}
            >
              <Filter size={12} />
              Filter
              {activeFilterCount > 0 && (
                <span className="w-4 h-4 rounded-full bg-primary text-white text-xs flex items-center justify-center font-mono">
                  {activeFilterCount}
                </span>
              )}
            </button>

            {filterMenuOpen && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setFilterMenuOpen(false)} />
                <div className="absolute left-0 top-full mt-1 z-50 bg-card border border-border rounded-xl shadow-lg py-3 w-56 animate-fade-in">
                  <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider px-3 mb-2">
                    Model
                  </p>
                  {modelOptions.map((model) => (
                    <button
                      key={`model-filter-${model}`}
                      onClick={() => setSelectedModel(model)}
                      className={`flex items-center justify-between w-full px-3 py-2 text-sm transition-colors ${
                        selectedModel === model
                          ? 'text-primary bg-primary/5'
                          : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                      }`}
                    >
                      {model}
                      {selectedModel === model && <Check size={13} />}
                    </button>
                  ))}
                  <div className="h-px bg-border my-2" />
                  <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider px-3 mb-2">
                    Show only
                  </p>
                  {[
                    {
                      key: 'filter-starred',
                      label: 'Starred',
                      value: showStarredOnly,
                      toggle: () => setShowStarredOnly(!showStarredOnly),
                    },
                    {
                      key: 'filter-shared',
                      label: 'Shared',
                      value: showSharedOnly,
                      toggle: () => setShowSharedOnly(!showSharedOnly),
                    },
                  ].map(({ key, label, value, toggle }) => (
                    <button
                      key={key}
                      onClick={toggle}
                      className="flex items-center justify-between w-full px-3 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
                    >
                      {label}
                      <div
                        className={`w-8 h-4 rounded-full transition-colors relative ${value ? 'bg-primary' : 'bg-muted-foreground/30'}`}
                      >
                        <div
                          className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-transform ${value ? 'translate-x-4' : 'translate-x-0.5'}`}
                        />
                      </div>
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>

          {/* Sort */}
          <div className="relative">
            <button
              onClick={() => {
                setSortMenuOpen(!sortMenuOpen);
                setFilterMenuOpen(false);
              }}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-border bg-card text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            >
              <ArrowUpDown size={12} />
              {SORT_OPTIONS.find((s) => s.value === sortBy)?.label}
            </button>

            {sortMenuOpen && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setSortMenuOpen(false)} />
                <div className="absolute left-0 top-full mt-1 z-50 bg-card border border-border rounded-xl shadow-lg py-1 w-44 animate-fade-in">
                  {SORT_OPTIONS.map((opt) => (
                    <button
                      key={opt.key}
                      onClick={() => {
                        setSortBy(opt.value);
                        setSortMenuOpen(false);
                      }}
                      className={`flex items-center justify-between w-full px-3 py-2 text-sm transition-colors ${
                        sortBy === opt.value
                          ? 'text-primary bg-primary/5'
                          : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                      }`}
                    >
                      {opt.label}
                      {sortBy === opt.value && <Check size={13} />}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>

          <div className="flex-1" />

          {conversations.length > 0 && (
            <button
              onClick={() => void onClearHistory?.()}
              className="flex items-center gap-1.5 rounded-lg border border-red-500/20 bg-red-500/5 px-2.5 py-1.5 text-xs font-medium text-red-500 transition-colors hover:bg-red-500/10"
            >
              <Trash2 size={12} />
              Clear history
            </button>
          )}

          {/* Select all */}
          <button
            onClick={selectAll}
            className="text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            {selectedIds.size === filtered.length && filtered.length > 0
              ? 'Deselect all'
              : 'Select all'}
          </button>
        </div>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        {filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-center px-6">
            <MessageSquare size={32} className="text-muted-foreground/30 mb-3" />
            <p className="text-sm font-medium text-muted-foreground">
              {loading ? 'Loading conversations...' : 'No conversations found'}
            </p>
            <p className="text-xs text-muted-foreground/60 mt-1">
              Try adjusting your search or filters
            </p>
            <Link
              href="/chat-interface"
              className="mt-4 text-xs text-primary hover:text-primary-hover transition-colors"
            >
              Start a new conversation
            </Link>
          </div>
        ) : (
          filtered.map((conv) => {
            const isSelected = selectedIds.has(conv.id);
            const isActive = selectedConversation?.id === conv.id;
            const statusBadge = STATUS_BADGES[conv.status];

            return (
              <div
                key={conv.id}
                onClick={() => onSelectConversation(conv)}
                className={`
                  group relative flex gap-3 px-4 py-3.5 border-b border-border cursor-pointer transition-all
                  ${isActive ? 'bg-primary/5 border-l-2 border-l-primary' : 'hover:bg-muted/50 border-l-2 border-l-transparent'}
                `}
              >
                {/* Checkbox */}
                <div
                  onClick={(e) => toggleSelect(conv.id, e)}
                  className={`
                    shrink-0 w-4 h-4 rounded border mt-1 flex items-center justify-center transition-all cursor-pointer
                    ${isSelected ? 'bg-primary border-primary' : 'border-border group-hover:border-muted-foreground/50'}
                  `}
                >
                  {isSelected && <Check size={10} className="text-white" />}
                </div>

                <div className="flex-1 min-w-0">
                  {/* Title row */}
                  <div className="flex items-start justify-between gap-2 mb-1">
                    <h3
                      className={`text-sm font-medium truncate ${isActive ? 'text-primary dark:text-[#9B7FFF]' : 'text-foreground'}`}
                    >
                      {conv.title}
                    </h3>
                    <span className="text-xs text-muted-foreground/60 shrink-0 font-mono tabular-nums">
                      {formatRelativeTime(conv.updatedAt)}
                    </span>
                  </div>

                  {/* Summary */}
                  <p className="text-xs text-muted-foreground leading-relaxed line-clamp-2 mb-2">
                    {conv.summary}
                  </p>

                  {/* Meta row */}
                  <div className="flex items-center gap-2 flex-wrap">
                    {/* Model badge */}
                    <div className="flex items-center gap-1">
                      <div className={`w-1.5 h-1.5 rounded-full ${conv.modelColor}`} />
                      <span className="text-xs text-muted-foreground">{conv.model}</span>
                    </div>

                    <span className="text-muted-foreground/30">-</span>

                    {/* Message count */}
                    <span className="flex items-center gap-1 text-xs text-muted-foreground">
                      <MessageSquare size={10} />
                      <span className="font-mono tabular-nums">{conv.messageCount}</span>
                    </span>

                    <span className="text-muted-foreground/30">-</span>

                    {/* Context size */}
                    <span className="text-xs text-muted-foreground font-mono tabular-nums">
                      {(conv.tokenCount / 1000).toFixed(1)}k context
                    </span>

                    {/* Indicators */}
                    {conv.hasMemory && (
                      <span className="flex items-center gap-0.5 text-accent">
                        <Brain size={10} />
                      </span>
                    )}
                    {conv.hasAttachments && (
                      <span className="text-muted-foreground">
                        <Paperclip size={10} />
                      </span>
                    )}
                    {conv.starred && (
                      <Star size={10} className="text-amber-400" fill="currentColor" />
                    )}
                    {conv.shared && <Share2 size={10} className="text-muted-foreground" />}

                    {/* Status badge */}
                    {conv.status !== 'active' && (
                      <span
                        className={`text-xs px-1.5 py-0.5 rounded border font-medium ${statusBadge.className}`}
                      >
                        {statusBadge.label}
                      </span>
                    )}
                  </div>

                  {/* Tags */}
                  {conv.tags.length > 0 && (
                    <div className="flex items-center gap-1.5 mt-2 flex-wrap">
                      {conv.tags.map((tag) => (
                        <span
                          key={`tag-${conv.id}-${tag}`}
                          className="text-[10px] px-2 py-0.5 rounded-full bg-primary/5 border border-primary/10 text-primary/70"
                        >
                          #{tag}
                        </span>
                      ))}
                    </div>
                  )}

                  {/* Memory Preview (Added to History) */}
                  {conv.hasMemory && (
                    <div className="mt-3 p-2 rounded-lg bg-accent/5 border border-accent/10 flex items-start gap-2">
                      <Brain size={12} className="text-accent shrink-0 mt-0.5" />
                      <p className="text-[10px] text-muted-foreground italic line-clamp-1">
                        Remembers your preference for TypeScript and Node.js...
                      </p>
                    </div>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Bulk action bar */}
      {selectedIds.size > 0 && (
        <div className="border-t border-border bg-card px-4 py-3 flex items-center gap-3 animate-slide-up">
          <span className="text-sm font-medium text-foreground">{selectedIds.size} selected</span>
          <div className="flex-1" />
          <button
            onClick={handleBulkMove}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            <FolderInput size={13} />
            Move to folder
          </button>
          <button
            onClick={handleBulkArchive}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            <Archive size={13} />
            Archive
          </button>
          <button
            onClick={handleBulkExport}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            <Download size={13} />
            Export
          </button>
          <button
            onClick={handleBulkDelete}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-red-500/20 bg-red-500/5 text-xs font-medium text-red-500 hover:bg-red-500/10 transition-colors"
          >
            <Trash2 size={13} />
            Delete
          </button>
          <button
            onClick={() => onSelectionChange(new Set())}
            className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground transition-colors"
          >
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  );
}
