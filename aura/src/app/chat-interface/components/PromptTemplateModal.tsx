'use client';

import React, { memo, useState } from 'react';
import {
  X,
  Search,
  BookMarked,
  Code2,
  FileText,
  Lightbulb,
  PenTool,
  Star,
  Plus,
  ChevronRight,
} from 'lucide-react';

interface PromptTemplateModalProps {
  open: boolean;
  onClose: () => void;
  onSelect: (prompt: string) => void;
}

const CATEGORIES = [
  { key: 'cat-all', label: 'All', icon: BookMarked },
  { key: 'cat-code', label: 'Code', icon: Code2 },
  { key: 'cat-writing', label: 'Writing', icon: PenTool },
  { key: 'cat-analysis', label: 'Analysis', icon: FileText },
  { key: 'cat-ideas', label: 'Ideas', icon: Lightbulb },
];

const TEMPLATES = [
  {
    id: 'tmpl-000',
    title: 'Continuous Conversational Automation (OpenWork 2026)',
    category: 'Analysis',
    prompt:
      '[CONTINUOUS_AUTOMATION] Execute continuous multi-turn autonomous goal loop isolated within a new dedicated tab. Resolve compact DOM refs ({e1}, {e2}), track subtasks, and support voice barge-in with zero interference to other tabs.',
    starred: true,
    uses: 142,
  },
  {
    id: 'tmpl-001',
    title: 'Code Review',
    category: 'Code',
    prompt:
      'Please review the following code for bugs, security issues, performance problems, and adherence to best practices. Provide specific, actionable feedback with code examples where appropriate.',
    starred: true,
    uses: 47,
  },
  {
    id: 'tmpl-002',
    title: "Explain Like I'm 5",
    category: 'Analysis',
    prompt:
      'Explain the following concept in the simplest possible terms, using analogies and real-world examples that a non-technical person could understand.',
    starred: true,
    uses: 31,
  },
  {
    id: 'tmpl-003',
    title: 'Write Unit Tests',
    category: 'Code',
    prompt:
      'Write comprehensive unit tests for the following code using Jest and TypeScript. Include edge cases, error scenarios, and mock external dependencies appropriately.',
    starred: false,
    uses: 28,
  },
  {
    id: 'tmpl-004',
    title: 'Technical Blog Post',
    category: 'Writing',
    prompt:
      'Write a technical blog post about the following topic. Include an introduction, main sections with code examples, best practices, common pitfalls, and a conclusion. Target audience: senior developers.',
    starred: false,
    uses: 19,
  },
  {
    id: 'tmpl-005',
    title: 'Architecture Review',
    category: 'Analysis',
    prompt:
      'Review the following system architecture and provide feedback on scalability, reliability, security, and maintainability. Suggest specific improvements with trade-off analysis.',
    starred: false,
    uses: 15,
  },
  {
    id: 'tmpl-006',
    title: 'Brainstorm Ideas',
    category: 'Ideas',
    prompt:
      'Generate 10 creative and practical ideas for the following problem or topic. For each idea, provide a brief description, potential benefits, and implementation challenges.',
    starred: false,
    uses: 12,
  },
  {
    id: 'tmpl-007',
    title: 'Refactor Code',
    category: 'Code',
    prompt:
      'Refactor the following code to improve readability, maintainability, and performance. Apply SOLID principles, clean code practices, and modern language features. Explain each change you make.',
    starred: true,
    uses: 38,
  },
  {
    id: 'tmpl-008',
    title: 'API Documentation',
    category: 'Writing',
    prompt:
      'Generate comprehensive API documentation for the following endpoint or function. Include description, parameters, request/response examples, error codes, and usage notes.',
    starred: false,
    uses: 22,
  },
];

/**
 * Memoized at the bottom of this file. Smallest of the five wins, kept for
 * consistency rather than for measured savings: both call sites gate the mount on
 * their open flag, so this is only ever rendering while it is actually on screen.
 * What the wrapper buys is that a reply streaming behind an open library no longer
 * re-renders the template grid on every token.
 */
function PromptTemplateModal({ open, onClose, onSelect }: PromptTemplateModalProps) {
  const [search, setSearch] = useState('');
  const [activeCategory, setActiveCategory] = useState('cat-all');

  if (!open) return null;

  const filtered = TEMPLATES.filter((t) => {
    const matchesSearch =
      !search ||
      t.title.toLowerCase().includes(search.toLowerCase()) ||
      t.prompt.toLowerCase().includes(search.toLowerCase());
    const matchesCategory =
      activeCategory === 'cat-all' ||
      t.category === CATEGORIES.find((c) => c.key === activeCategory)?.label;
    return matchesSearch && matchesCategory;
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className="relative bg-card border border-border rounded-2xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col animate-slide-up">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-border">
          <div className="flex items-center gap-2">
            <BookMarked size={17} className="text-primary" />
            <h2 className="text-base font-semibold text-foreground">Prompt Templates</h2>
            <span className="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded-full font-mono">
              {TEMPLATES.length}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 transition-colors">
              <Plus size={12} />
              New Template
            </button>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
            >
              <X size={15} />
            </button>
          </div>
        </div>

        {/* Search */}
        <div className="px-5 py-3 border-b border-border">
          <div className="relative">
            <Search
              size={14}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
            />
            <input
              type="text"
              placeholder="Search templates..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-full bg-muted rounded-xl text-sm pl-9 pr-4 py-2 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/40 border-0"
              autoFocus
            />
          </div>
        </div>

        {/* Categories */}
        <div className="flex items-center gap-2 px-5 py-2.5 border-b border-border overflow-x-auto scrollbar-thin">
          {CATEGORIES.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setActiveCategory(key)}
              className={`shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-all ${
                activeCategory === key
                  ? 'bg-primary text-white'
                  : 'bg-muted text-muted-foreground hover:bg-muted hover:text-foreground'
              }`}
            >
              <Icon size={12} />
              {label}
            </button>
          ))}
        </div>

        {/* Templates */}
        <div className="flex-1 overflow-y-auto scrollbar-thin p-4 space-y-2">
          {filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <BookMarked size={32} className="text-muted-foreground/30 mb-3" />
              <p className="text-sm font-medium text-muted-foreground">No templates found</p>
              <p className="text-xs text-muted-foreground/60 mt-1">
                Try a different search term or category
              </p>
            </div>
          ) : (
            filtered.map((template) => (
              <div
                key={template.id}
                className="group flex gap-3 p-3.5 rounded-xl border border-border hover:border-primary/30 hover:bg-primary/3 transition-all cursor-pointer"
                onClick={() => onSelect(template.prompt)}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-sm font-medium text-foreground">{template.title}</span>
                    {template.starred && (
                      <Star size={12} className="text-amber-400" fill="currentColor" />
                    )}
                    <span className="text-xs px-2 py-0.5 rounded-full bg-muted text-muted-foreground">
                      {template.category}
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground leading-relaxed line-clamp-2">
                    {template.prompt}
                  </p>
                  <p className="text-xs text-muted-foreground/50 mt-1.5">
                    Used {template.uses} times
                  </p>
                </div>
                <ChevronRight
                  size={16}
                  className="text-muted-foreground/40 group-hover:text-primary transition-colors shrink-0 mt-1"
                />
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

PromptTemplateModal.displayName = 'PromptTemplateModal';

export default memo(PromptTemplateModal);
