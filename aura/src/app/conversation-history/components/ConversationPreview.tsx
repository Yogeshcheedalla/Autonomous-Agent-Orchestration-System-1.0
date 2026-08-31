'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import {
  X,
  Star,
  Share2,
  Trash2,
  ExternalLink,
  Brain,
  MessageSquare,
  Check,
  MoreHorizontal,
  Archive,
  Download,
  Zap,
} from 'lucide-react';
import type { Conversation } from './ConversationHistoryScreen';
import { toast } from 'sonner';

interface ConversationPreviewProps {
  conversation: Conversation;
  onClose: () => void;
  onDeleteConversation?: (id: string) => Promise<void> | void;
  onArchiveConversation?: (id: string) => void;
  onToggleStar?: (id: string) => void;
  onExportConversation?: (id: string) => void;
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function ConversationPreview({
  conversation,
  onClose,
  onDeleteConversation,
  onArchiveConversation,
  onToggleStar,
  onExportConversation,
}: ConversationPreviewProps) {
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  const [linkCopied, setLinkCopied] = useState(false);
  const previewMessages = conversation.messages.slice(-8);

  const handleCopyLink = () => {
    // Backend integration point: generate shareable link
    navigator.clipboard.writeText(`https://akansha.ai/share/${conversation.id}`);
    setLinkCopied(true);
    setTimeout(() => setLinkCopied(false), 2000);
    toast.success('Share link copied to clipboard');
  };

  const handleDelete = async () => {
    const confirmed = window.confirm(`Delete "${conversation.title}" from chat history?`);
    if (!confirmed) return;
    await onDeleteConversation?.(conversation.id);
    onClose();
  };

  const handleArchive = () => {
    onArchiveConversation?.(conversation.id);
  };

  const modelColorMap: Record<string, string> = {
    'GPT-4o': 'bg-green-500',
    'Claude 3.5 Sonnet': 'bg-orange-400',
    'Gemini 1.5 Pro': 'bg-blue-400',
  };

  return (
    <div className="flex flex-col h-full w-full">
      {/* Header */}
      <div className="flex items-start gap-3 px-5 py-4 border-b border-border">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <div
              className={`w-2 h-2 rounded-full shrink-0 ${modelColorMap[conversation.model] || 'bg-muted-foreground'}`}
            />
            <h2 className="text-base font-semibold text-foreground truncate">
              {conversation.title}
            </h2>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-muted-foreground">{conversation.model}</span>
            <span className="text-muted-foreground/40">·</span>
            <span className="text-xs text-muted-foreground">{conversation.folder}</span>
            <span className="text-muted-foreground/40">·</span>
            <span className="text-xs text-muted-foreground">
              {formatDate(conversation.updatedAt)}
            </span>
          </div>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => onToggleStar?.(conversation.id)}
            className={`p-2 rounded-lg transition-colors ${conversation.starred ? 'text-amber-400 hover:bg-amber-400/10' : 'text-muted-foreground hover:bg-muted hover:text-foreground'}`}
            title="Star conversation"
          >
            <Star size={15} fill={conversation.starred ? 'currentColor' : 'none'} />
          </button>

          <button
            onClick={handleCopyLink}
            className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
            title="Share conversation"
          >
            {linkCopied ? <Check size={15} className="text-green-500" /> : <Share2 size={15} />}
          </button>

          <Link
            href="/chat-interface"
            onClick={() => sessionStorage.setItem('akansha-active-session', conversation.id)}
            className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
            title="Open in chat"
          >
            <ExternalLink size={15} />
          </Link>

          <div className="relative">
            <button
              onClick={() => setMoreMenuOpen(!moreMenuOpen)}
              className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
            >
              <MoreHorizontal size={15} />
            </button>
            {moreMenuOpen && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setMoreMenuOpen(false)} />
                <div className="absolute right-0 top-full mt-1 z-50 bg-card border border-border rounded-xl shadow-lg py-1 min-w-[160px] animate-fade-in">
                  {[
                    {
                      key: 'action-archive',
                      icon: Archive,
                      label: 'Archive',
                      action: handleArchive,
                    },
                    {
                      key: 'action-export',
                      icon: Download,
                      label: 'Export as JSON',
                      action: () => onExportConversation?.(conversation.id),
                    },
                    {
                      key: 'action-delete',
                      icon: Trash2,
                      label: 'Delete',
                      action: handleDelete,
                      danger: true,
                    },
                  ].map(({ key, icon: Icon, label, action, danger }) => (
                    <button
                      key={key}
                      onClick={() => {
                        action();
                        setMoreMenuOpen(false);
                      }}
                      className={`flex items-center gap-2 w-full px-3 py-2 text-sm transition-colors ${
                        danger
                          ? 'text-red-500 hover:bg-red-500/5'
                          : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                      }`}
                    >
                      <Icon size={14} />
                      {label}
                    </button>
                  ))}
                </div>
              </>
            )}
          </div>

          <button
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors lg:hidden"
          >
            <X size={15} />
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto scrollbar-thin p-5">
        <div className="space-y-6">
          {/* Summary */}
          <div>
            <div className="flex items-center gap-2 mb-3">
              <Brain size={14} className="text-primary" />
              <h3 className="text-sm font-semibold text-foreground">Conversation Summary</h3>
            </div>
            <div className="p-4 rounded-2xl bg-muted/30 border border-border">
              <p className="text-sm text-foreground leading-relaxed">{conversation.summary}</p>
            </div>
          </div>

          {/* Messages */}
          <div className="space-y-4">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2">
                <MessageSquare size={14} className="text-accent" />
                <h3 className="text-sm font-semibold text-foreground">Messages</h3>
              </div>
              <span className="text-xs text-muted-foreground font-mono">
                {conversation.messageCount} total
              </span>
            </div>

            <div className="space-y-3">
              {previewMessages.length ? (
                previewMessages.map((msg) => (
                  <div
                    key={msg.id}
                    className={`flex gap-3 ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}
                  >
                    <div
                      className={`w-8 h-8 rounded-full shrink-0 flex items-center justify-center text-[10px] font-bold mt-1 ${
                        msg.role === 'user'
                          ? 'bg-primary text-white'
                          : 'bg-muted border border-border text-muted-foreground'
                      }`}
                    >
                      {msg.role === 'user' ? 'U' : 'AI'}
                    </div>
                    <div
                      className={`max-w-[85%] px-4 py-3 rounded-2xl text-xs leading-relaxed ${
                        msg.role === 'user'
                          ? 'bg-primary text-white rounded-tr-sm'
                          : 'bg-muted border border-border text-foreground rounded-tl-sm'
                      }`}
                    >
                      {msg.content}
                    </div>
                  </div>
                ))
              ) : (
                <div className="rounded-2xl border border-border bg-muted/30 px-4 py-6 text-center text-xs text-muted-foreground">
                  No saved messages are available for this conversation yet.
                </div>
              )}
            </div>

            <div className="pt-4">
              <Link
                href="/chat-interface"
                onClick={() => sessionStorage.setItem('akansha-active-session', conversation.id)}
                className="flex items-center justify-center gap-2 w-full px-4 py-3 rounded-2xl bg-primary hover:bg-primary-hover text-white text-sm font-medium transition-all duration-150 active:scale-[0.98] shadow-sm shadow-primary/20"
              >
                <Zap size={15} />
                Continue this Conversation
              </Link>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
