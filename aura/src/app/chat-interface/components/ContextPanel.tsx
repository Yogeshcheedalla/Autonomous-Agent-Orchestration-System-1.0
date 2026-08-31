'use client';

import React, { memo, useState } from 'react';
import {
  Brain,
  FileText,
  Clock,
  X,
  ChevronDown,
  ChevronUp,
  Layers,
  Plus,
  Trash2,
  Pin,
  Search,
  Edit3,
  Check,
  CalendarDays,
} from 'lucide-react';
import { toast } from 'sonner';
import TaskCalendarPanel from './TaskCalendarPanel';
import { apiUrl } from '@/lib/apiBase';
import type { MemoriesResponse } from '@/types/chatApi';

interface ContextPanelProps {
  onClose: () => void;
  messageCount?: number;
  contextUnits?: number;
}

const RAG_DOCS = [
  { id: 'doc-001', name: 'auth.middleware.ts', type: 'TypeScript', size: '3.2 KB', indexed: true },
  { id: 'doc-002', name: 'API_design_spec.md', type: 'Markdown', size: '18.4 KB', indexed: true },
  { id: 'doc-003', name: 'system_architecture.pdf', type: 'PDF', size: '2.1 MB', indexed: false },
];

interface MemoryItem {
  id: string;
  content: string;
  category: string;
  timestamp: string;
  pinned: boolean;
}

/**
 * Memoized at the bottom of this file. Partial win, deliberately.
 *
 * `messageCount` and `contextUnits` genuinely change while a reply streams, so
 * the wrapper cannot stop re-renders during a reply -- it stops them the rest of
 * the time, when the parent re-renders for a reason this drawer does not care
 * about. The large cost underneath (TaskCalendarPanel) is memoized separately and
 * takes no props, so it stays parked even while these two numbers tick.
 */
function ContextPanel({ onClose, messageCount = 0, contextUnits = 0 }: ContextPanelProps) {
  const [activeTab, setActiveTab] = useState<'memory' | 'planner' | 'rag' | 'context'>('memory');
  const [memoryExpanded, setMemoryExpanded] = useState(true);
  const [memories, setMemories] = useState<MemoryItem[]>([]);

  React.useEffect(() => {
    const fetchMemories = () => {
      fetch(apiUrl('/api/memories'))
        .then((res) => res.json() as Promise<MemoriesResponse>)
        .then((data) => {
          if (data.memories) {
            setMemories(
              data.memories.map((m) => ({
                id: m.id.toString(),
                content: m.insight,
                category: m.topic,
                timestamp: m.timestamp ? new Date(m.timestamp).toLocaleDateString() : 'Just now',
                pinned: m.importance > 3,
              }))
            );
          }
        })
        .catch((err) => console.warn('Failed to load memories:', err));
    };

    // Initial fetch
    fetchMemories();

    // Poll every 3 seconds to catch background updates
    const interval = setInterval(fetchMemories, 3000);
    return () => clearInterval(interval);
  }, []);
  const [searchQuery, setSearchQuery] = useState('');
  const [addingMemory, setAddingMemory] = useState(false);
  const [newMemoryContent, setNewMemoryContent] = useState('');
  const [newMemoryCategory, setNewMemoryCategory] = useState('Preference');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editContent, setEditContent] = useState('');

  const removeMemory = (id: string) => {
    setMemories((prev) => prev.filter((m) => m.id !== id));
    toast.success('Memory removed');
  };

  const togglePin = (id: string) => {
    setMemories((prev) => prev.map((m) => (m.id === id ? { ...m, pinned: !m.pinned } : m)));
  };

  const addMemory = () => {
    if (!newMemoryContent.trim()) return;
    const mem: MemoryItem = {
      id: `mem-${Date.now()}`,
      content: newMemoryContent.trim(),
      category: newMemoryCategory,
      timestamp: 'Just now',
      pinned: false,
    };
    setMemories((prev) => [...prev, mem]);
    setNewMemoryContent('');
    setAddingMemory(false);
    toast.success('Memory saved');
  };

  const startEdit = (mem: MemoryItem) => {
    setEditingId(mem.id);
    setEditContent(mem.content);
  };

  const saveEdit = (id: string) => {
    setMemories((prev) => prev.map((m) => (m.id === id ? { ...m, content: editContent } : m)));
    setEditingId(null);
    toast.success('Memory updated');
  };

  const filteredMemories = memories.filter(
    (m) =>
      !searchQuery ||
      m.content.toLowerCase().includes(searchQuery.toLowerCase()) ||
      m.category.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const maxContextUnits = 128000;
  const usagePercent = (contextUnits / maxContextUnits) * 100;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <div className="flex items-center gap-2">
          <Brain size={15} className="text-accent" />
          <span className="text-sm font-semibold text-foreground">Context</span>
        </div>
        <button
          onClick={onClose}
          className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
        >
          <X size={14} />
        </button>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border shrink-0">
        {[
          { key: 'memory', label: 'Memory', icon: Brain },
          { key: 'planner', label: 'Planner', icon: CalendarDays },
          { key: 'rag', label: 'Docs', icon: FileText },
          { key: 'context', label: 'Window', icon: Layers },
        ].map(({ key, label, icon: Icon }) => (
          <button
            key={`ctx-tab-${key}`}
            onClick={() => setActiveTab(key as typeof activeTab)}
            className={`flex-1 flex items-center justify-center gap-1.5 px-2 py-2.5 text-xs font-medium transition-colors ${
              activeTab === key
                ? 'text-primary border-b-2 border-primary'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <Icon size={13} />
            {label}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto scrollbar-thin p-3">
        {/* Memory tab */}
        {activeTab === 'memory' && (
          <div className="space-y-3">
            {/* Search */}
            <div className="relative">
              <Search
                size={12}
                className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
              />
              <input
                type="text"
                placeholder="Search memories..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full bg-muted rounded-lg text-xs pl-7 pr-3 py-1.5 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/40 border-0"
              />
            </div>

            <div className="flex items-center justify-between">
              <p className="text-xs text-muted-foreground">{filteredMemories.length} memories</p>
              <button
                onClick={() => setAddingMemory(true)}
                className="flex items-center gap-1 text-xs text-primary hover:text-primary-hover transition-colors"
              >
                <Plus size={12} />
                Add
              </button>
            </div>

            {/* Add memory form */}
            {addingMemory && (
              <div className="p-3 rounded-xl border border-primary/30 bg-primary/5 space-y-2">
                <textarea
                  placeholder="What should Akansha remember?"
                  value={newMemoryContent}
                  onChange={(e) => setNewMemoryContent(e.target.value)}
                  autoFocus
                  rows={2}
                  className="w-full bg-transparent text-xs text-foreground placeholder:text-muted-foreground focus:outline-none resize-none"
                />
                <div className="flex items-center gap-2">
                  <select
                    value={newMemoryCategory}
                    onChange={(e) => setNewMemoryCategory(e.target.value)}
                    className="flex-1 bg-card border border-border rounded-lg text-xs px-2 py-1 text-foreground focus:outline-none"
                  >
                    {[
                      'Preference',
                      'Tech stack',
                      'Personal',
                      'Infrastructure',
                      'Goal',
                      'Habit',
                    ].map((cat) => (
                      <option key={cat} value={cat}>
                        {cat}
                      </option>
                    ))}
                  </select>
                  <button
                    onClick={addMemory}
                    className="px-3 py-1 rounded-lg bg-primary text-white text-xs font-medium hover:bg-primary-hover transition-colors"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => setAddingMemory(false)}
                    className="p-1 rounded-lg hover:bg-muted text-muted-foreground transition-colors"
                  >
                    <X size={12} />
                  </button>
                </div>
              </div>
            )}

            {/* Pinned memories */}
            <div>
              <button
                onClick={() => setMemoryExpanded(!memoryExpanded)}
                className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2 hover:text-foreground transition-colors w-full"
              >
                {memoryExpanded ? <ChevronDown size={12} /> : <ChevronUp size={12} />}
                Pinned
              </button>
              {memoryExpanded &&
                filteredMemories
                  .filter((m) => m.pinned)
                  .map((mem) => (
                    <div
                      key={mem.id}
                      className="group flex gap-2 p-2.5 rounded-lg bg-accent/5 border border-accent/15 mb-2"
                    >
                      <Brain size={12} className="text-accent shrink-0 mt-0.5" />
                      <div className="flex-1 min-w-0">
                        {editingId === mem.id ? (
                          <div className="flex gap-1">
                            <input
                              value={editContent}
                              onChange={(e) => setEditContent(e.target.value)}
                              className="flex-1 bg-card border border-border rounded px-2 py-0.5 text-xs text-foreground focus:outline-none"
                              autoFocus
                            />
                            <button onClick={() => saveEdit(mem.id)} className="p-1 text-accent">
                              <Check size={11} />
                            </button>
                          </div>
                        ) : (
                          <p className="text-xs text-foreground leading-relaxed">{mem.content}</p>
                        )}
                        <div className="flex items-center gap-2 mt-1">
                          <span className="text-xs px-1.5 py-0.5 rounded bg-accent/10 text-accent font-medium">
                            {mem.category}
                          </span>
                          <span className="text-xs text-muted-foreground flex items-center gap-1">
                            <Clock size={9} />
                            {mem.timestamp}
                          </span>
                        </div>
                      </div>
                      <div className="flex flex-col gap-1 opacity-0 group-hover:opacity-100 transition-all">
                        <button
                          onClick={() => startEdit(mem)}
                          className="p-1 rounded text-muted-foreground hover:text-primary transition-colors"
                        >
                          <Edit3 size={10} />
                        </button>
                        <button
                          onClick={() => togglePin(mem.id)}
                          className="p-1 rounded text-accent hover:text-muted-foreground transition-colors"
                        >
                          <Pin size={10} />
                        </button>
                        <button
                          onClick={() => removeMemory(mem.id)}
                          className="p-1 rounded text-muted-foreground hover:text-red-500 transition-colors"
                        >
                          <Trash2 size={10} />
                        </button>
                      </div>
                    </div>
                  ))}
            </div>

            {/* All memories */}
            <div>
              <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                All Memories
              </p>
              {filteredMemories
                .filter((m) => !m.pinned)
                .map((mem) => (
                  <div
                    key={mem.id}
                    className="group flex gap-2 p-2.5 rounded-lg hover:bg-muted border border-transparent hover:border-border mb-1.5 transition-all"
                  >
                    <div className="w-1.5 h-1.5 rounded-full bg-muted-foreground/40 shrink-0 mt-1.5" />
                    <div className="flex-1 min-w-0">
                      {editingId === mem.id ? (
                        <div className="flex gap-1">
                          <input
                            value={editContent}
                            onChange={(e) => setEditContent(e.target.value)}
                            className="flex-1 bg-card border border-border rounded px-2 py-0.5 text-xs text-foreground focus:outline-none"
                            autoFocus
                          />
                          <button onClick={() => saveEdit(mem.id)} className="p-1 text-accent">
                            <Check size={11} />
                          </button>
                        </div>
                      ) : (
                        <p className="text-xs text-foreground leading-relaxed">{mem.content}</p>
                      )}
                      <div className="flex items-center gap-2 mt-1">
                        <span className="text-xs text-muted-foreground">{mem.category}</span>
                        <span className="text-muted-foreground/40">·</span>
                        <span className="text-xs text-muted-foreground">{mem.timestamp}</span>
                      </div>
                    </div>
                    <div className="flex flex-col gap-1 opacity-0 group-hover:opacity-100 transition-all">
                      <button
                        onClick={() => startEdit(mem)}
                        className="p-1 rounded text-muted-foreground hover:text-primary transition-colors"
                      >
                        <Edit3 size={10} />
                      </button>
                      <button
                        onClick={() => togglePin(mem.id)}
                        className="p-1 rounded text-muted-foreground hover:text-accent transition-colors"
                      >
                        <Pin size={10} />
                      </button>
                      <button
                        onClick={() => removeMemory(mem.id)}
                        className="p-1 rounded text-muted-foreground hover:text-red-500 transition-colors"
                      >
                        <Trash2 size={10} />
                      </button>
                    </div>
                  </div>
                ))}
              {filteredMemories.filter((m) => !m.pinned).length === 0 && searchQuery && (
                <p className="text-xs text-muted-foreground text-center py-4">
                  No memories match &ldquo;{searchQuery}&rdquo;
                </p>
              )}
            </div>
          </div>
        )}

        {activeTab === 'planner' && (
          <div className="-m-3 h-full min-h-full">
            <TaskCalendarPanel />
          </div>
        )}

        {/* RAG Documents tab */}
        {activeTab === 'rag' && (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-xs text-muted-foreground">{RAG_DOCS.length} documents</p>
              <button className="flex items-center gap-1 text-xs text-primary hover:text-primary-hover transition-colors">
                <Plus size={12} />
                Upload
              </button>
            </div>

            {RAG_DOCS.map((doc) => (
              <div
                key={doc.id}
                className="flex items-start gap-2.5 p-2.5 rounded-lg border border-border hover:bg-muted transition-colors group"
              >
                <FileText size={14} className="text-muted-foreground shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-foreground font-mono truncate">
                    {doc.name}
                  </p>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-xs text-muted-foreground">{doc.type}</span>
                    <span className="text-muted-foreground/40">·</span>
                    <span className="text-xs text-muted-foreground">{doc.size}</span>
                  </div>
                </div>
                <span
                  className={`text-xs px-1.5 py-0.5 rounded font-medium shrink-0 ${
                    doc.indexed
                      ? 'bg-green-500/10 text-green-600 dark:text-green-400'
                      : 'bg-amber-500/10 text-amber-600 dark:text-amber-400'
                  }`}
                >
                  {doc.indexed ? 'Indexed' : 'Processing'}
                </span>
              </div>
            ))}

            <div className="p-3 rounded-xl border border-dashed border-border text-center">
              <p className="text-xs text-muted-foreground">
                Drop PDFs, text files, or code for RAG retrieval
              </p>
            </div>
          </div>
        )}

        {/* Context Window tab */}
        {activeTab === 'context' && (
          <div className="space-y-4">
            <div className="p-3 rounded-xl bg-muted/50 border border-border">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-foreground">Context Load</span>
                <span className="text-xs font-mono tabular-nums text-muted-foreground">
                  {usagePercent.toFixed(1)}%
                </span>
              </div>
              <div className="h-2 bg-muted rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-primary to-accent transition-all"
                  style={{ width: `${usagePercent}%` }}
                />
              </div>
              <p className="text-xs text-muted-foreground mt-1">
                Current conversation context loaded for memory-aware replies
              </p>
            </div>

            <div className="space-y-2">
              {[
                { label: 'Messages in context', value: messageCount, icon: Brain },
                { label: 'RAG chunks retrieved', value: 0, icon: FileText },
                { label: 'Memories loaded', value: memories.length, icon: Layers },
              ].map(({ label, value, icon: Icon }) => (
                <div
                  key={`ctx-stat-${label}`}
                  className="flex items-center justify-between p-2.5 rounded-lg bg-muted/50"
                >
                  <div className="flex items-center gap-2">
                    <Icon size={13} className="text-muted-foreground" />
                    <span className="text-xs text-muted-foreground">{label}</span>
                  </div>
                  <span className="text-xs font-semibold font-mono tabular-nums text-foreground">
                    {value}
                  </span>
                </div>
              ))}
            </div>

            <button
              onClick={() => toast.info('Context cleared')}
              className="w-full px-3 py-2 rounded-lg border border-border text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
            >
              Clear context window
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

ContextPanel.displayName = 'ContextPanel';

export default memo(ContextPanel);
