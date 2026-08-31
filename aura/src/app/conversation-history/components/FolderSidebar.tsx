'use client';

import React, { useState } from 'react';
import {
  Folder,
  FolderOpen,
  Star,
  Archive,
  Share2,
  Plus,
  MessageSquare,
  Trash2,
} from 'lucide-react';
import type { Conversation } from './ConversationHistoryScreen';
import type { ConversationFolder } from '@/lib/chatHistoryMetadata';
import { toast } from 'sonner';

interface FolderSidebarProps {
  selectedFolder: string | null;
  onSelectFolder: (folder: string | null) => void;
  conversations: Conversation[];
  folders: ConversationFolder[];
  onCreateFolder: (name: string) => void;
  onEmptyTrash: () => void;
}

const SYSTEM_FOLDERS = [
  { key: 'all', label: 'All Conversations', icon: MessageSquare },
  { key: 'starred', label: 'Starred', icon: Star },
  { key: 'shared', label: 'Shared', icon: Share2 },
  { key: 'archived', label: 'Archived', icon: Archive },
];

export default function FolderSidebar({
  selectedFolder,
  onSelectFolder,
  conversations,
  folders,
  onCreateFolder,
  onEmptyTrash,
}: FolderSidebarProps) {
  const [newFolderMode, setNewFolderMode] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');

  const getCount = (key: string) => {
    if (key === 'all') return conversations.length;
    if (key === 'starred') return conversations.filter((c) => c.starred).length;
    if (key === 'shared') return conversations.filter((c) => c.shared).length;
    if (key === 'archived') return conversations.filter((c) => c.status === 'archived').length;
    return conversations.filter((c) => c.folderId === key).length;
  };

  const handleCreateFolder = () => {
    const name = newFolderName.trim();
    if (!name) return;

    if (folders.some((folder) => folder.name.toLowerCase() === name.toLowerCase())) {
      toast.error(`Folder "${name}" already exists`);
      return;
    }

    onCreateFolder(name);
    setNewFolderName('');
    setNewFolderMode(false);
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-3 border-b border-border">
        <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
          Library
        </p>
      </div>

      <div className="flex-1 overflow-y-auto scrollbar-thin p-2">
        {/* System folders */}
        <div className="mb-3">
          {SYSTEM_FOLDERS.map(({ key, label, icon: Icon }) => {
            const count = getCount(key);
            const isActive = selectedFolder === key || (key === 'all' && selectedFolder === null);
            return (
              <button
                key={`sys-folder-${key}`}
                onClick={() => onSelectFolder(key === 'all' ? null : key)}
                className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-sm transition-colors mb-0.5 ${
                  isActive
                    ? 'bg-primary/10 text-primary dark:text-[#9B7FFF]'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                }`}
              >
                <Icon size={15} className="shrink-0" />
                <span className="flex-1 text-left text-xs font-medium">{label}</span>
                <span className="text-xs font-mono tabular-nums text-muted-foreground/60">
                  {count}
                </span>
              </button>
            );
          })}
        </div>

        {/* User folders */}
        <div>
          <div className="flex items-center justify-between px-2.5 mb-1">
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
              Folders
            </p>
            <button
              onClick={() => setNewFolderMode(true)}
              className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              title="New folder"
            >
              <Plus size={13} />
            </button>
          </div>

          {folders.map((folder) => {
            const count = getCount(folder.id);
            const isActive = selectedFolder === folder.id;
            return (
              <button
                key={folder.id}
                onClick={() => onSelectFolder(folder.id)}
                className={`w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-sm transition-colors mb-0.5 group ${
                  isActive
                    ? 'bg-primary/10 text-primary dark:text-[#9B7FFF]'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                }`}
              >
                {isActive ? (
                  <FolderOpen size={15} className="shrink-0" />
                ) : (
                  <Folder size={15} className="shrink-0" />
                )}
                <span className="flex-1 text-left text-xs font-medium">{folder.name}</span>
                <span className="text-xs font-mono tabular-nums text-muted-foreground/60">
                  {count}
                </span>
              </button>
            );
          })}

          {/* New folder input */}
          {newFolderMode && (
            <div className="px-2 mt-1">
              <input
                type="text"
                autoFocus
                value={newFolderName}
                onChange={(e) => setNewFolderName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleCreateFolder();
                  if (e.key === 'Escape') {
                    setNewFolderMode(false);
                    setNewFolderName('');
                  }
                }}
                onBlur={() => {
                  if (!newFolderName.trim()) {
                    setNewFolderMode(false);
                  }
                }}
                placeholder="Folder name..."
                className="w-full bg-muted rounded-lg text-xs px-2.5 py-1.5 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/40 border-0"
              />
              <p className="text-xs text-muted-foreground/60 mt-1 px-0.5">
                Enter to create, Esc to cancel
              </p>
            </div>
          )}
        </div>
      </div>

      <div className="p-2 border-t border-border">
        <button
          onClick={onEmptyTrash}
          className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs text-muted-foreground hover:bg-muted hover:text-red-500 transition-colors"
        >
          <Trash2 size={13} />
          Empty trash
        </button>
      </div>
    </div>
  );
}
