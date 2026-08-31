'use client';

import React, { useState, useRef, useCallback, memo } from 'react';
import {
  Paperclip,
  Mic,
  MicOff,
  Send,
  Square,
  BookMarked,
  Image as ImageIcon,
  X,
  FileText,
} from 'lucide-react';
import {
  autoRouteCognitivePrompt,
  expandSlashCommand,
  getSlashCommandSuggestions,
  type SlashCommandDefinition,
} from '@/lib/slashCommands';

interface ChatComposerProps {
  onSend: (content: string, attachments?: File[]) => void;
  onStop?: () => void;
  onOpenPromptLibrary: () => void;
  isStreaming: boolean;
  selectedModel: string;
  onToggleMic?: () => void;
  isListening?: boolean;
}

function stableFileSignature(file: File) {
  const identity = file.name.startsWith('pasted-screenshot-') ? 'clipboard-image' : file.name;
  return `${identity}:${file.type || 'application/octet-stream'}:${file.size}`;
}

function stableClipboardImageSignature(file: File) {
  return `${file.type || 'image/png'}:${file.size}`;
}

/**
 * Memoised, and it took three fixes upstream in ChatThread to make the memo hold.
 *
 * ChatThread keeps `streamingContent` in state, so it re-renders on every token of
 * a reply and re-rendered all 494 lines of this component with it. Three of the
 * seven props were unstable:
 *   - `onSend` (`handleSend`) listed `messages` in its `useCallback` deps for a
 *     single read in the planner-title branch, so it changed identity every time a
 *     message settled. It now reads `messagesRef`.
 *   - `onOpenPromptLibrary` was an inline arrow in the JSX. It is now `useCallback`ed
 *     as `openPromptLibrary`.
 *   - the rest (`onStop`, `onToggleMic`) were already `useCallback`ed over stable
 *     helpers, and `isStreaming` / `isListening` / `selectedModel` are primitives.
 *
 * `selectedModel` is declared in the props interface but never read in here. Left
 * alone deliberately -- it is a string, so it costs the memo nothing, and removing
 * it means touching the call site too.
 */
const ChatComposer = memo(function ChatComposer({
  onSend,
  onStop,
  onOpenPromptLibrary,
  isStreaming,
  onToggleMic,
  isListening,
}: ChatComposerProps) {
  const [content, setContent] = useState('');
  const redoStackRef = useRef<string[]>([]);
  const [attachments, setAttachments] = useState<File[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [selectedSlashIndex, setSelectedSlashIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const shouldRefocusAfterStreamRef = useRef(false);
  const lastClipboardImagePasteRef = useRef<{ signature: string; time: number } | null>(null);
  const slashSuggestions = React.useMemo(
    () => getSlashCommandSuggestions(content).slice(0, 8),
    [content]
  );
  const showSlashMenu = slashSuggestions.length > 0 && content.trimStart().startsWith('/');

  const resizeComposer = useCallback(() => {
    if (!textareaRef.current) return;
    textareaRef.current.style.height = 'auto';
    textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 200) + 'px';
  }, []);

  const setComposerContent = useCallback(
    (value: string, options?: { clearRedo?: boolean }) => {
      setContent(value);
      if (options?.clearRedo !== false) {
        redoStackRef.current = [];
      }
      requestAnimationFrame(resizeComposer);
    },
    [resizeComposer]
  );

  const removeLastWord = useCallback((value: string) => {
    const withoutTrailingSpace = value.replace(/\s+$/, '');
    if (!withoutTrailingSpace) return '';
    const lastWord = withoutTrailingSpace.match(/\S+$/);
    if (!lastWord || lastWord.index === undefined) return '';
    return withoutTrailingSpace.slice(0, lastWord.index);
  }, []);

  React.useEffect(() => {
    const handleApplyPrompt = (e: WindowEventMap['akansha-apply-prompt']) => {
      const promptText = e.detail;
      setComposerContent(promptText);

      if (textareaRef.current) {
        textareaRef.current.focus();
      }
    };
    window.addEventListener('akansha-apply-prompt', handleApplyPrompt);
    return () => window.removeEventListener('akansha-apply-prompt', handleApplyPrompt);
  }, [setComposerContent]);

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const newValue = e.target.value;
    setComposerContent(newValue);
    setSelectedSlashIndex(0);
  };

  const focusAndResize = useCallback(() => {
    requestAnimationFrame(() => {
      if (!textareaRef.current) return;
      textareaRef.current.focus();
      resizeComposer();
    });
  }, [resizeComposer]);

  React.useEffect(() => {
    if (isStreaming || !shouldRefocusAfterStreamRef.current) return;
    shouldRefocusAfterStreamRef.current = false;
    focusAndResize();
  }, [focusAndResize, isStreaming]);

  const applySlashSuggestion = useCallback(
    (command: SlashCommandDefinition) => {
      const trimmed = content.trimStart();
      const remainder = trimmed.replace(/^\/\S*\s*/, '').trim();
      const newValue = `/${command.name}${remainder ? ` ${remainder}` : ' '}`;
      setComposerContent(newValue);
      setSelectedSlashIndex(0);
      focusAndResize();
    },
    [content, focusAndResize, setComposerContent]
  );

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      if (e.shiftKey) {
        const restored = redoStackRef.current.pop();
        if (restored !== undefined) {
          setComposerContent(restored, { clearRedo: false });
        }
      } else {
        const nextValue = removeLastWord(content);
        if (nextValue !== content) {
          redoStackRef.current.push(content);
          setComposerContent(nextValue, { clearRedo: false });
        }
      }
      return;
    }

    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
      e.preventDefault();
      const restored = redoStackRef.current.pop();
      if (restored !== undefined) {
        setComposerContent(restored, { clearRedo: false });
      }
      return;
    }

    if (showSlashMenu) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedSlashIndex((index) => (index + 1) % slashSuggestions.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedSlashIndex(
          (index) => (index - 1 + slashSuggestions.length) % slashSuggestions.length
        );
        return;
      }
      if (e.key === 'Tab') {
        e.preventDefault();
        applySlashSuggestion(slashSuggestions[selectedSlashIndex] || slashSuggestions[0]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setContent('');
        return;
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const canSend = Boolean(content.trim() || attachments.length > 0);

  const handleSend = () => {
    if (!canSend) return;
    shouldRefocusAfterStreamRef.current = true;
    const messageText = content.trim()
      ? autoRouteCognitivePrompt(expandSlashCommand(content))
      : 'Analyze the attached image or screenshot carefully. Read visible text, inspect the UI, and explain what you see with reasoning.';
    onSend(messageText, attachments.length > 0 ? attachments : undefined);
    setComposerContent('');
    setAttachments([]);
    focusAndResize();
  };

  const addFiles = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      setAttachments((prev) => {
        const seen = new Set(prev.map(stableFileSignature));
        const uniqueFiles = files.filter((file) => {
          const signature = stableFileSignature(file);
          if (seen.has(signature)) return false;
          seen.add(signature);
          return true;
        });
        if (uniqueFiles.length === 0) return prev;
        return [...prev, ...uniqueFiles].slice(0, 5);
      });
      focusAndResize();
    },
    [focusAndResize]
  );

  const handleFileSelect = (files: FileList | null) => {
    if (!files) return;
    addFiles(Array.from(files).slice(0, 5));
  };

  const extractClipboardImages = useCallback((clipboardData: DataTransfer | null | undefined) => {
    const clipboardItems = Array.from(clipboardData?.items || []);
    const seenClipboardImages = new Set<string>();
    const pastedImages = clipboardItems
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item, index) => {
        const file = item.getAsFile();
        if (!file) return null;
        const sourceSignature = stableClipboardImageSignature(file);
        if (seenClipboardImages.has(sourceSignature)) return null;
        seenClipboardImages.add(sourceSignature);
        const extension = file.type.split('/')[1] || 'png';
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        return new File([file], `pasted-screenshot-${timestamp}-${index + 1}.${extension}`, {
          type: file.type,
          lastModified: file.lastModified || 0,
        });
      })
      .filter((file): file is File => Boolean(file));

    return pastedImages;
  }, []);

  const attachClipboardImages = useCallback(
    (clipboardData: DataTransfer | null | undefined) => {
      const pastedImages = extractClipboardImages(clipboardData);
      if (pastedImages.length === 0) return false;

      const signature = pastedImages.map(stableFileSignature).join('|');
      const previous = lastClipboardImagePasteRef.current;
      const now = Date.now();
      if (previous?.signature === signature && now - previous.time < 750) {
        return true;
      }

      lastClipboardImagePasteRef.current = { signature, time: now };
      addFiles(pastedImages);
      return true;
    },
    [addFiles, extractClipboardImages]
  );

  const handlePaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      if (!attachClipboardImages(e.clipboardData)) return;
      e.preventDefault();
      e.stopPropagation();
      e.nativeEvent.stopImmediatePropagation();
    },
    [attachClipboardImages]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragging(false);
      addFiles(Array.from(e.dataTransfer.files));
    },
    [addFiles]
  );

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => setIsDragging(false);

  const removeAttachment = (index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  };

  const getFileIcon = (file: File) => {
    if (file.type.startsWith('image/')) return <ImageIcon size={12} className="text-blue-400" />;
    return <FileText size={12} className="text-muted-foreground" />;
  };

  return (
    <div className="px-4 pb-4 pt-2" data-akansha-chat-composer="true">
      {/* Padded outer, capped inner -- the same two-element structure the message
          list uses, and it has to be two elements. With `max-w-3xl` and `px-4` on
          one div the cap measured the padding too, so the bordered input box came
          out 736px against the bubbles' 768px and sat inset 17px from them: at
          1440px the box visibly stepped in from the column above it. Measured, not
          guessed -- message column left 614, textarea left 631. */}
      <div className="mx-auto w-full max-w-3xl">
        {isDragging && (
          <div className="absolute inset-4 rounded-xl border-2 border-dashed border-primary bg-primary/5 z-10 flex items-center justify-center pointer-events-none">
            <p className="text-sm font-medium text-primary">Drop files to attach</p>
          </div>
        )}

        <div
          className={`
          relative border rounded-2xl bg-card transition-all duration-150
          ${isDragging ? 'border-primary' : 'border-border'}
          focus-within:border-primary/50 focus-within:ring-1 focus-within:ring-primary/20
        `}
          onDrop={handleDrop}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
        >
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-2 px-4 pt-3">
              {attachments.map((file, i) => (
                <div
                  key={`attachment-${i}-${file.name}`}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-muted border border-border text-xs group"
                >
                  {getFileIcon(file)}
                  <span className="font-mono text-foreground max-w-[120px] truncate">
                    {file.name}
                  </span>
                  <span className="text-muted-foreground">{(file.size / 1024).toFixed(0)}KB</span>
                  <button
                    type="button"
                    onClick={() => removeAttachment(i)}
                    className="text-muted-foreground hover:text-red-500 transition-colors ml-1"
                  >
                    <X size={11} />
                  </button>
                </div>
              ))}
            </div>
          )}

          {isListening && (
            <div className="flex items-center gap-3 px-4 pt-3">
              <div className="flex items-center gap-1">
                {[0, 1, 2, 3, 4].map((i) => (
                  <div
                    key={`wave-${i}`}
                    className="w-1 bg-red-500 rounded-full waveform-bar"
                    style={{ animationDelay: `${i * 0.1}s` }}
                  />
                ))}
              </div>
              <span className="text-xs text-red-500 font-medium animate-pulse">Listening...</span>
            </div>
          )}

          {showSlashMenu && (
            <div className="mx-3 mt-3 rounded-2xl border border-primary/30 bg-background/95 shadow-2xl shadow-primary/10 overflow-hidden animate-fade-in">
              <div className="flex items-center justify-between px-3 py-2 border-b border-border/70">
                <span className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
                  Slash commands
                </span>
                <span className="text-[11px] text-muted-foreground">Use arrows, Tab, or click</span>
              </div>
              <div className="max-h-72 overflow-y-auto scrollbar-thin p-1.5">
                {slashSuggestions.map((command, index) => (
                  <button
                    key={`slash-${command.name}`}
                    type="button"
                    onMouseDown={(event) => {
                      event.preventDefault();
                      applySlashSuggestion(command);
                    }}
                    className={`w-full text-left rounded-xl px-3 py-2.5 transition-colors ${
                      index === selectedSlashIndex
                        ? 'bg-primary/15 text-foreground'
                        : 'text-muted-foreground hover:bg-muted hover:text-foreground'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="font-mono text-sm text-[#8F72FF]">/{command.name}</span>
                      <span className="rounded-full border border-border px-2 py-0.5 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                        {command.category}
                      </span>
                    </div>
                    <p className="mt-1 text-xs leading-relaxed">{command.description}</p>
                  </button>
                ))}
              </div>
            </div>
          )}

          <textarea
            ref={textareaRef}
            value={content}
            onChange={handleInput}
            onPaste={handlePaste}
            onKeyDown={handleKeyDown}
            placeholder="Message Akansha... Type / for commands"
            rows={1}
            className="w-full bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground resize-none focus:outline-none leading-relaxed min-h-[52px]"
          />

          <div className="flex items-center justify-between px-3 pb-3">
            <div className="flex items-center gap-1">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="image/*,.pdf,.txt,.md,.ts,.tsx,.js,.jsx,.py,.json,.csv"
                className="hidden"
                onChange={(e) => handleFileSelect(e.target.files)}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                title="Attach files"
              >
                <Paperclip size={16} />
              </button>
              <button
                type="button"
                onClick={onOpenPromptLibrary}
                className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                title="Prompt templates"
              >
                <BookMarked size={16} />
              </button>
              {onToggleMic && (
                <button
                  type="button"
                  onClick={onToggleMic}
                  className={`p-2 rounded-lg transition-colors ${
                    isListening
                      ? 'bg-red-500/10 text-red-500 hover:bg-red-500/20'
                      : 'hover:bg-muted text-muted-foreground hover:text-foreground'
                  }`}
                  title={isListening ? 'Stop listening' : 'Voice input'}
                >
                  {isListening ? <MicOff size={16} /> : <Mic size={16} />}
                </button>
              )}
            </div>

            <div className="flex items-center gap-2">
              <span className="text-xs text-muted-foreground/60 hidden sm:block">
                {content.length > 0 && `${content.length} chars`}
              </span>
              {isStreaming && (
                <button
                  type="button"
                  onClick={onStop}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-red-500/10 border border-red-500/20 text-red-500 text-xs font-medium hover:bg-red-500/20 transition-colors"
                >
                  <Square size={12} fill="currentColor" />
                  Stop
                </button>
              )}
              {!isStreaming || canSend ? (
                <button
                  type="button"
                  onClick={handleSend}
                  disabled={!canSend}
                  className={`
                  flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-medium transition-all duration-150 active:scale-95
                  ${
                    canSend
                      ? 'bg-primary hover:bg-primary-hover text-white shadow-sm shadow-primary/20'
                      : 'bg-muted text-muted-foreground cursor-not-allowed'
                  }
                `}
                >
                  <Send size={13} />
                  Send
                </button>
              ) : null}
            </div>
          </div>
        </div>

        <p className="text-center text-xs text-muted-foreground/40 mt-2">
          Akansha can make mistakes. Verify important information.
        </p>
      </div>
    </div>
  );
});

ChatComposer.displayName = 'ChatComposer';

export default ChatComposer;
