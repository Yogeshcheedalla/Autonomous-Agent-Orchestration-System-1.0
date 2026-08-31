'use client';

import React, { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import ModelSelector from './ModelSelector';
import MessageBubble from './MessageBubble';
import ChatComposer from './ChatComposer';
import dynamic from 'next/dynamic';
import { type Emotion } from './AvatarPanel';

/**
 * Static, unlike `AvatarPanel`, and deliberately so: the collapsed avatar strip is
 * the default view, so the core is on screen for everybody at first paint. Deferring
 * it would only buy a hole where the presence should be. `AvatarPanel` stays dynamic
 * and now imports this from the shared route chunk rather than dragging its own copy.
 */
import JarvisCore, {
  coreStateFor,
  presenceLabel,
  EMOTION_COLORS,
} from '@/components/assistant/JarvisCore';

/**
 * Dynamic here for the same reason it is dynamic in ChatWorkspace -- and it has to
 * be dynamic in *both* places to buy anything.
 *
 * There are two independent prompt-library modals in this tree: this one, opened by
 * the composer's button, and ChatWorkspace's, opened by the sidebar. ChatWorkspace
 * already imported it lazily, but this file imported the same module statically and
 * ChatWorkspace imports this file statically, so webpack put the module in the
 * route chunk anyway and emitted no separate chunk at all. The split was
 * decorative: measurably so -- no PromptTemplateModal chunk ever appeared in the
 * network log when the modal opened.
 */
const PromptTemplateModal = dynamic(() => import('./PromptTemplateModal'));

/**
 * Loaded on demand. `avatarExpanded` starts false, so the expanded avatar -- the
 * only thing this component renders -- is not on screen for anybody until they
 * click to expand it, yet a static import shipped all of it in the initial
 * /chat-interface chunk. The collapsed row beside it is plain inline markup and
 * stays static, so nothing about the default view changes.
 *
 * The `Emotion` type is still imported statically: it is erased at compile time
 * and carries no runtime weight.
 */
const AvatarPanel = dynamic(() => import('./AvatarPanel'));
import {
  AlertTriangle,
  Brain,
  Share2,
  MoreHorizontal,
  Star,
  Trash2,
  Mic,
  MicOff,
  ChevronDown,
  ChevronUp,
  CheckCheck,
  Pin,
  GitBranch,
  X,
} from 'lucide-react';
import { toast } from 'sonner';
import {
  addMinutes,
  applyPlannerCommand,
  applyPlannerReminderFollowUp,
  cleanPlannerTitle,
  extractDateValue,
  extractTimeWindow,
  inferPlannerCommand,
  isLikelyTaskDetails,
  isPlannerPreparationPrompt,
  isReminderOnlyPlannerFollowUp,
  isWeakPlannerTitle,
  type PlannerCommand,
} from '@/lib/plannerCommands';
import { isAutomationIntent, normalizeAutomationPrompt } from '@/lib/automationCommands';
import { deleteSessionTitle } from '@/hooks/chatSessionTitles';
import {
  claimAkanshaAudio,
  hardCancelBrowserSpeech,
  releaseAkanshaAudio,
  settleBrowserSpeechCancel,
} from '@/lib/audioPlaybackGuard';
import { fetchAkanshaSpeech, languageModeFromTag } from '@/lib/akanshaSpeech';
import { apiUrl } from '@/lib/apiBase';
import { summariseAutomation, type AutomationStatus } from '@/lib/automationStatus';
import type { ChatHistoryResponse } from '@/types/chatApi';

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  sessionId?: string;
  model?: string;
  timestamp: Date;
  attachments?: Array<{
    id: string;
    name: string;
    type: string;
    size: string;
    previewUrl?: string;
  }>;
  memoryRefs?: string[];
  isStreaming?: boolean;
  tokenCount?: number;
  emotion?: Emotion;
  pinned?: boolean;
  displayOrder?: number | null;
  branchFromId?: number | null;
}

const EMOTION_RESPONSES: Record<string, Emotion> = {
  sad: 'sad',
  happy: 'happy',
  excited: 'happy',
  confused: 'thinking',
  help: 'thinking',
  wow: 'surprised',
  amazing: 'surprised',
  thanks: 'happy',
  error: 'thinking',
  default: 'neutral',
};

const CHAT_INTERRUPT_PATTERN =
  /\b(stop|wait|pause|hold on|enough|silent|mute|aagu|aapu|ruko|ruk jao)\b|ఆపు|ఆగు|रुको|बस/i;

type ChatWorkMode = 'quick' | 'research' | 'agent' | 'skill';

const CHAT_WORK_MODES: Array<{ id: ChatWorkMode; label: string; hint: string }> = [
  { id: 'quick', label: 'Quick', hint: 'Fast local/chat answers for normal conversation' },
  {
    id: 'research',
    label: 'Research',
    hint: 'Use for live sources, citations, and deeper verification',
  },
  { id: 'agent', label: 'Agent', hint: 'Use for multi-step autonomous workflows' },
  {
    id: 'skill',
    label: 'Skill',
    hint: 'Use for generated files, coding workflows, and reusable skills',
  },
];

const TRANSIENT_SPEECH_ERRORS = new Set(['no-speech', 'aborted', 'audio-capture', 'network']);

const TIMESTAMP_WITH_ZONE_PATTERN = /(?:Z|[+-]\d{2}:?\d{2})$/i;

function parseChatTimestamp(value: string | null | undefined): Date {
  if (!value) return new Date();
  const normalized = TIMESTAMP_WITH_ZONE_PATTERN.test(value) ? value : `${value}Z`;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? new Date() : parsed;
}

function normalizeAssistantEchoText(text: string) {
  return (text || '')
    .toLowerCase()
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[^\p{L}\p{N}\s]+/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function looksLikeAssistantSpeechEcho(heardText: string, spokenText: string, activeUntil: number) {
  if (!heardText || !spokenText || Date.now() > activeUntil) return false;
  const heard = normalizeAssistantEchoText(heardText);
  const spoken = normalizeAssistantEchoText(spokenText);
  if (heard.length < 3 || spoken.length < 3) return false;
  if (spoken.includes(heard)) return true;

  const heardWords = heard.split(' ').filter((word) => word.length > 1);
  if (!heardWords.length) return false;
  const spokenWords = new Set(spoken.split(' ').filter(Boolean));
  const matched = heardWords.filter((word) => spokenWords.has(word)).length;
  return matched / heardWords.length >= 0.8;
}

function detectEmotion(text: string): Emotion {
  const lower = text.toLowerCase();
  for (const [keyword, emotion] of Object.entries(EMOTION_RESPONSES)) {
    if (lower.includes(keyword)) return emotion;
  }
  return 'neutral';
}

type ChatAttachmentPayload = {
  name: string;
  type: string;
  size: number;
  data_url?: string;
  text?: string;
};

function chatLanguagePreference() {
  if (typeof window === 'undefined') return 'telugu_english';
  const stored =
    window.localStorage.getItem('akansha_voice_language') ||
    window.localStorage.getItem('akansha_app_language');
  if (stored === 'hindi') return 'hindi';
  if (stored === 'english') return 'english';
  return 'telugu_english';
}

function detectSpeechLang(text: string) {
  const preference = chatLanguagePreference();
  const hasTelugu = /[\u0C00-\u0C7F]/.test(text);
  const hasHindi = /[\u0900-\u097F]/.test(text);
  const words = new Set(text.toLowerCase().match(/[a-z]+/g) ?? []);

  if (hasHindi || preference === 'hindi') return 'hi-IN';
  if (hasTelugu || preference === 'telugu_english') return 'te-IN';
  if (
    ['namaste', 'namaskar', 'hindi', 'kaise', 'kya', 'mujhe', 'aap', 'hai', 'nahi', 'batao'].some(
      (word) => words.has(word)
    )
  ) {
    return 'hi-IN';
  }
  if (
    ['telugu', 'anna', 'andi', 'naku', 'naaku', 'meeru', 'ela', 'unnaru', 'cheppu'].some((word) =>
      words.has(word)
    )
  ) {
    return 'te-IN';
  }
  return 'en-IN';
}

function getMessageNumericId(message: Message): number | null {
  if (/^\d+$/.test(message.id)) return Number(message.id);
  return null;
}

function estimateTokenCount(content: string): number | undefined {
  const compact = content.trim();
  if (!compact) return undefined;
  return Math.max(1, Math.ceil(compact.length / 4));
}

function isBrokenAssistantHistoryMessage(message: { role?: string; content?: string }) {
  if (message.role !== 'assistant') return false;
  const compact = (message.content || '').trim();
  return !compact || compact === '0';
}

function _compactTextForVoice(text: string) {
  return (text || '').replace(/\s+/g, ' ').trim();
}

function formatZonedDateTime(timeZone: string) {
  const now = new Date();
  const dateText = new Intl.DateTimeFormat('en-IN', {
    timeZone,
    weekday: 'long',
    day: '2-digit',
    month: 'long',
    year: 'numeric',
  }).format(now);
  const timeText = new Intl.DateTimeFormat('en-IN', {
    timeZone,
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  }).format(now);
  return { dateText, timeText };
}

function timeZoneForQuickQuery(text: string) {
  if (/\b(london|uk|britain|england)\b/i.test(text)) {
    return { label: 'London', timeZone: 'Europe/London' };
  }
  return { label: 'IST', timeZone: 'Asia/Kolkata' };
}

function fastLocalChatReply(content: string, languagePreference: string): string | null {
  const cleaned = _compactTextForVoice(content);
  const preference = languagePreference.toLowerCase();
  if (!cleaned || cleaned.length > 180) {
    return null;
  }

  if (
    /^(?:what(?:'s| is)?|tell me|show me|give me|exact|current|present)?\s*(?:the\s+)?(?:exact\s+|current\s+|present\s+)?(?:(?:ist|india|london|uk|britain|england)\s+)?(?:time|date|day|today(?:'s)? date|today(?:'s)? day|now)(?:\s+(?:in\s+)?(?:ist|india|london|uk|britain|england))?[?.!]*$/i.test(
      cleaned
    )
  ) {
    const { label, timeZone } = timeZoneForQuickQuery(cleaned);
    const { dateText, timeText } = formatZonedDateTime(timeZone);
    const wantsDateOnly = /\b(date|day|today)\b/i.test(cleaned) && !/\btime|now\b/i.test(cleaned);
    if (preference.includes('hindi')) {
      return wantsDateOnly
        ? `Aaj ${dateText} hai, ${label} ke according.`
        : `Abhi ${timeText} ${label} hai, ${dateText}.`;
    }
    if (preference.includes('telugu')) {
      return wantsDateOnly
        ? `Ivvala ${dateText}, ${label} prakaram.`
        : `Ippudu ${timeText} ${label}, ${dateText}.`;
    }
    return wantsDateOnly
      ? `Today is ${dateText} in ${label}.`
      : `${label} time is ${timeText} on ${dateText}.`;
  }

  return null;
}

function insertMessageAfter(
  messages: Message[],
  anchorId: string | null,
  message: Message
): Message[] {
  if (!anchorId) return [...messages, message];
  const anchorIndex = messages.findIndex((item) => item.id === anchorId);
  if (anchorIndex < 0) return [...messages, message];
  return [...messages.slice(0, anchorIndex + 1), message, ...messages.slice(anchorIndex + 1)];
}

function isAlertReminderIntent(text: string) {
  return /\b(alert|alarm|reminder|remainder|notify|notification|pop\s*up|popup|remind me)\b/i.test(
    text
  );
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

function readFileAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error(`Could not read ${file.name}`));
    reader.readAsText(file);
  });
}

function loadImageElement(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('Could not prepare screenshot for analysis'));
    image.src = dataUrl;
  });
}

async function readImageForVision(
  file: File
): Promise<{ dataUrl: string; type: string; size: number }> {
  const originalDataUrl = await readFileAsDataUrl(file);
  if (file.size <= 350_000 || typeof document === 'undefined') {
    return { dataUrl: originalDataUrl, type: file.type || 'image/png', size: file.size };
  }

  try {
    const image = await loadImageElement(originalDataUrl);
    const maxDimension = 1280;
    const scale = Math.min(1, maxDimension / Math.max(image.naturalWidth, image.naturalHeight));
    const width = Math.max(1, Math.round(image.naturalWidth * scale));
    const height = Math.max(1, Math.round(image.naturalHeight * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context)
      return { dataUrl: originalDataUrl, type: file.type || 'image/png', size: file.size };
    context.drawImage(image, 0, 0, width, height);
    const compressedDataUrl = canvas.toDataURL('image/jpeg', 0.82);
    const compressedSize = Math.round((compressedDataUrl.length * 3) / 4);
    if (compressedDataUrl.length >= originalDataUrl.length) {
      return { dataUrl: originalDataUrl, type: file.type || 'image/png', size: file.size };
    }
    return { dataUrl: compressedDataUrl, type: 'image/jpeg', size: compressedSize };
  } catch {
    return { dataUrl: originalDataUrl, type: file.type || 'image/png', size: file.size };
  }
}

function formatChatError(error: unknown): string {
  const rawMessage = error instanceof Error ? error.message : String(error || '');
  const lower = rawMessage.toLowerCase();

  if (
    lower.includes('402') ||
    lower.includes('credit') ||
    lower.includes('budget') ||
    lower.includes('insufficient') ||
    lower.includes('quota')
  ) {
    return 'OpenRouter did not return a model answer for this request. Akansha kept the chat alive; local tools and source fallback remain available.';
  }

  if (
    lower.includes('401') ||
    lower.includes('missing authentication header') ||
    lower.includes('unauthorized') ||
    lower.includes('openrouter is not configured')
  ) {
    return 'The OpenRouter API key is not active in the running backend session. Check the key in C:\\MY-AI\\aura\\.env and restart the backend.';
  }

  if (lower.includes('413') || lower.includes('too large') || lower.includes('payload')) {
    return 'That screenshot is too large to send as-is. Crop it to the important area or paste a smaller image, then I can analyze it.';
  }

  if (
    lower.includes('request timed out') ||
    lower.includes('timeout') ||
    lower.includes('timed out')
  ) {
    return 'That answer took too long, so I stopped it instead of leaving the chat hanging. Akansha kept the chat route alive; use a shorter prompt or current-source mode for faster results.';
  }

  if (lower.includes('image') || lower.includes('vision') || lower.includes('unsupported')) {
    return 'The screenshot upload is working, but detailed image analysis did not return a usable result. Try a smaller crop or ask about one specific visible area.';
  }

  if (
    lower.includes('failed to fetch') ||
    lower.includes('networkerror') ||
    lower.includes('network')
  ) {
    return 'I could not reach the local chat service. Make sure the FastAPI backend is running on port 8000, then send it again.';
  }

  return rawMessage
    ? `I could not finish that request: ${rawMessage.slice(0, 260)}`
    : 'I could not finish that request. Please try once more.';
}

async function buildChatAttachments(files?: File[]): Promise<ChatAttachmentPayload[] | undefined> {
  if (!files?.length) return undefined;
  const supported = files.slice(0, 5);
  const payloads = await Promise.all(
    supported.map(async (file) => {
      const base = {
        name: file.name,
        type: file.type || 'application/octet-stream',
        size: file.size,
      };

      if (file.type.startsWith('image/')) {
        const visionImage = await readImageForVision(file);
        return {
          ...base,
          type: visionImage.type,
          size: visionImage.size,
          data_url: visionImage.dataUrl,
        };
      }

      if (
        file.type.startsWith('text/') ||
        /\.(md|txt|json|csv|ts|tsx|js|jsx|py)$/i.test(file.name)
      ) {
        const text = await readFileAsText(file);
        return {
          ...base,
          text: text.slice(0, 400000),
        };
      }

      return base;
    })
  );
  return payloads;
}

/**
 * Shown when a thread has no messages yet.
 *
 * There was nothing here before: a new chat rendered the header, the avatar row,
 * the chip row and the composer around a tall empty black rectangle, which reads
 * as a page that failed to load rather than one waiting for input. Four starter
 * cards double as a statement of what the assistant can actually do, which is the
 * one thing a blank chat cannot communicate.
 *
 * Module scope on purpose -- declared inside ChatThread it would be a new
 * component type on every render, so React would unmount and remount it per
 * streaming token.
 */
function EmptyThread({ onPick }: { onPick: (prompt: string) => void }) {
  const starters = [
    { title: 'Automate my desktop', body: 'Open apps, click, type and scroll for me' },
    { title: 'Research and summarise', body: 'Search the web and give me the short version' },
    { title: 'Build me a file', body: 'A slide deck, a spreadsheet, an image or a doc' },
    { title: 'Remember this', body: 'Keep context across sessions and recall it later' },
  ];

  return (
    <div className="flex flex-col items-center justify-center py-16 px-2 text-center">
      <div className="w-12 h-12 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mb-4">
        <Brain size={22} className="text-primary" />
      </div>
      <h2 className="text-xl font-semibold text-foreground">How can I help?</h2>
      <p className="mt-1.5 text-sm text-muted-foreground max-w-md">
        Ask in English or Telugu, type or talk. I can act on your desktop and the web, not just
        answer.
      </p>

      <div className="mt-8 grid w-full grid-cols-1 gap-2 sm:grid-cols-2">
        {starters.map((starter) => (
          <button
            key={starter.title}
            type="button"
            onClick={() => onPick(starter.title)}
            className="group rounded-xl border border-border bg-card/60 px-4 py-3 text-left transition-colors hover:border-primary/30 hover:bg-muted"
          >
            <span className="block text-sm font-medium text-foreground">{starter.title}</span>
            <span className="mt-0.5 block text-xs text-muted-foreground">{starter.body}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export default function ChatThread({
  sessionId,
  onStatsChange,
}: {
  sessionId: string;
  onStatsChange?: (messages: number, contextUnits: number) => void;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  /**
   * A mirror of `messages` for callbacks that need to *read* the thread without
   * being rebuilt whenever it changes.
   *
   * `handleSend` reads the array once, inside the planner-title branch, to find the
   * previous user message. Listing `messages` as a dependency for that one read
   * gave `handleSend` a new identity after every settled message -- and since it is
   * passed to ChatComposer as `onSend`, that identity churn is what stops the
   * composer being memoisable at all. A ref reads the same value with a stable
   * callback.
   */
  const messagesRef = useRef<Message[]>([]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  const [selectedModel, setSelectedModel] = useState('Akansha');
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingContent, setStreamingContent] = useState('');
  const [streamingAfterMessageId, setStreamingAfterMessageId] = useState<string | null>(null);
  const [promptModalOpen, setPromptModalOpen] = useState(false);
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  const [activeBranchFromId, setActiveBranchFromId] = useState<number | null>(null);
  const [chatWorkMode, setChatWorkMode] = useState<ChatWorkMode>('quick');

  // Image attachments get a `blob:` URL so the composer can show a thumbnail, and
  // a blob URL pins the file's decoded bytes for the lifetime of the *document* —
  // dropping the last reference to it frees nothing. Nothing revoked these, so a
  // session spent attaching screenshots leaked every one of them until the tab
  // closed. Previews belong to the conversation they were sent in, so the set is
  // released when the session changes, the conversation is deleted, or the view
  // unmounts.
  const previewUrlsRef = useRef<string[]>([]);
  const revokePreviewUrls = useCallback(() => {
    for (const url of previewUrlsRef.current) URL.revokeObjectURL(url);
    previewUrlsRef.current = [];
  }, []);

  useEffect(() => {
    setMessages([]);
    fetch(apiUrl(`/api/chat?session_id=${sessionId}`))
      .then((res) => res.json() as Promise<ChatHistoryResponse>)
      .then((data) => {
        if (data.messages) {
          setMessages(
            data.messages
              .filter((m) => !isBrokenAssistantHistoryMessage(m))
              .map((m) => ({
                id: m.id.toString(),
                // `?? undefined` because the wire field is nullable (the column
                // carries a default, not a NOT NULL) while `Message.sessionId` is
                // optional. Passing a literal null through, as the `any` did, put a
                // value in state that every `sessionId ===` comparison downstream
                // silently fails against.
                sessionId: m.session_id ?? undefined,
                // `chat_messages.role` is an unconstrained String column, so the
                // wire type is `string` and this narrowing has to be explicit. It
                // matches what the renderer already does -- MessageBubble branches
                // on `role === 'user'` and treats everything else as assistant --
                // so behaviour is unchanged; the difference is that a stray
                // 'system' or 'tool' row can no longer be smuggled into a field
                // typed as a two-value union.
                role: m.role === 'user' ? ('user' as const) : ('assistant' as const),
                content: m.content,
                timestamp: parseChatTimestamp(m.timestamp),
                pinned: Boolean(m.pinned),
                displayOrder: typeof m.display_order === 'number' ? m.display_order : null,
                branchFromId: typeof m.branch_from_id === 'number' ? m.branch_from_id : null,
                tokenCount: estimateTokenCount(m.content || ''),
              }))
          );
        }
      })
      .catch((err) => console.warn('Failed to load chat history:', err));
    return revokePreviewUrls;
  }, [sessionId, revokePreviewUrls]);
  const [currentEmotion, setCurrentEmotion] = useState<Emotion>('neutral');
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [voiceEnabled, setVoiceEnabled] = useState(false);
  const [automationStatus, setAutomationStatus] = useState<AutomationStatus | null>(null);
  // Whether the avatar shows its full face (§26 expressions + lip sync) or the
  // compact inline strip. Defaults to compact so the chat keeps its vertical
  // space; the face is one click away.
  //
  // This was `avatarMinimized`, and the ternary below was inverted: the false
  // branch drew a hand-rolled compact strip and the true branch drew
  // `<AvatarPanel minimized={true} />`, which is *also* compact. Both states
  // were small, `minimized` was hardcoded true at the only call site, and the
  // full panel — face, brows, gaze, lip sync, HUD rings — had no reachable code
  // path in either direction.
  const [avatarExpanded, setAvatarExpanded] = useState(false);
  const [showAvatarBar] = useState(true);
  const [owTaskStatus, setOwTaskStatus] = useState<{
    session_id: string;
    site_domain: string;
    status: string;
    current_step: number;
    total_steps: number;
    requires_login: boolean;
    login_prompt: string | null;
  } | null>(null);
  const [owStatusExpanded, setOwStatusExpanded] = useState(false);
  const activeSessionIdRef = useRef(sessionId);
  const bottomRef = useRef<HTMLDivElement>(null);
  const speechRef = useRef<SpeechSynthesisUtterance | null>(null);
  // Her actual voice, when the server can produce it. The `SpeechSynthesisUtterance`
  // above is now only the fallback; see `speak`. One element reused across turns
  // rather than one per utterance, so `stopChatSpeech` always has something
  // concrete to pause — a per-utterance element would leave nothing to stop once
  // the reference had moved on.
  const chatTtsAudioRef = useRef<HTMLAudioElement | null>(null);
  const chatTtsUrlRef = useRef<string | null>(null);
  const chatAudioOwnerIdRef = useRef(`chat-${Math.random().toString(36).slice(2)}`);
  const chatSpeechGenerationRef = useRef(0);
  const recognitionRef = useRef<SpeechRecognition | null>(null);
  const micStoppedManuallyRef = useRef(false);
  const voiceEnabledRef = useRef(false);
  const isListeningRef = useRef(false);
  const isSpeakingRef = useRef(false);
  const isStreamingRef = useRef(false);
  const recognitionStartingRef = useRef(false);
  const voiceRestartTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const voiceAutoStartAttemptedRef = useRef(false);
  const voiceListeningToastShownRef = useRef(false);
  const assistantSpeechEchoUntilRef = useRef(0);
  const lastAssistantSpeechTextRef = useRef('');
  const activeChatAbortRef = useRef<AbortController | null>(null);
  const sendFromVoiceRef = useRef<(content: string) => void>(() => undefined);
  const pendingTranscriptRef = useRef('');
  const voiceFinalFlushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastVoiceSendRef = useRef<{ text: string; at: number } | null>(null);
  const pendingPlannerRef = useRef<PlannerCommand | null>(null);
  const lastReportedStatsRef = useRef({ messages: -1, contextUnits: -1 });

  useEffect(() => {
    activeSessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    voiceEnabledRef.current = voiceEnabled;
    if (!voiceEnabled && voiceRestartTimerRef.current) {
      clearTimeout(voiceRestartTimerRef.current);
      voiceRestartTimerRef.current = null;
    }
  }, [voiceEnabled]);

  useEffect(() => {
    isListeningRef.current = isListening;
  }, [isListening]);

  useEffect(() => {
    isSpeakingRef.current = isSpeaking;
  }, [isSpeaking]);

  // Hoisted out of the stats effect below. That effect lists `streamingContent` in
  // its deps, so it fires on every token of a reply -- and it used to re-run this
  // reduce over the whole thread each time, even though nothing in `messages`
  // changes while a token streams. Keyed on `messages` alone, the per-token cost
  // drops from O(thread length) to adding one number.
  const settledCharacterCount = useMemo(
    () => messages.reduce((acc, msg) => acc + msg.content.length, 0),
    [messages]
  );

  useEffect(() => {
    isStreamingRef.current = isStreaming;
  }, [isStreaming]);

  useEffect(() => {
    const totalCharacters = settledCharacterCount + streamingContent.length;
    const contextUnits = totalCharacters ? Math.max(1, Math.ceil(totalCharacters / 4)) : 0;
    const messageCount = messages.length + (streamingContent ? 1 : 0);
    if (
      lastReportedStatsRef.current.messages === messageCount &&
      lastReportedStatsRef.current.contextUnits === contextUnits
    ) {
      return;
    }
    lastReportedStatsRef.current = { messages: messageCount, contextUnits };
    onStatsChange?.(messageCount, contextUnits);
  }, [messages, onStatsChange, streamingContent, settledCharacterCount]);

  useEffect(() => {
    if (streamingAfterMessageId) {
      document
        .getElementById(`chat-message-${streamingAfterMessageId}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    if (activeBranchFromId) {
      document
        .getElementById(`chat-message-${activeBranchFromId}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [activeBranchFromId, messages, streamingAfterMessageId, streamingContent]);

  useEffect(() => {
    // Re-probed, not read once. The endpoint measures the GUI stack per request,
    // so a screen that goes away mid-session shows up here within a minute
    // instead of the header claiming six live permissions for the rest of the day.
    let cancelled = false;

    const poll = () => {
      fetch(apiUrl('/api/automation/browser/status'))
        .then((res) => (res.ok ? res.json() : null))
        .then((status: AutomationStatus | null) => {
          if (!cancelled) setAutomationStatus(status);
        })
        .catch(() => {
          if (!cancelled) setAutomationStatus(null);
        });
    };

    poll();
    const timer = setInterval(poll, 60_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  // Text-to-speech
  const releaseChatTtsUrl = useCallback(() => {
    if (chatTtsUrlRef.current) {
      URL.revokeObjectURL(chatTtsUrlRef.current);
      chatTtsUrlRef.current = null;
    }
  }, []);

  // Stop whatever she is currently saying, whichever engine is saying it.
  //
  // This is the function registered with `claimAkanshaAudio`, so it is also what
  // the voice page calls to take the microphone from under the chat page. That
  // makes its completeness load-bearing: `hardCancelBrowserSpeech()` cancels
  // `speechSynthesis` and nothing else, so before the `<audio>` pause below, a
  // server-voiced reply on the chat page would have kept playing straight through
  // a voice-page takeover — which is exactly the "two voices" symptom, arriving
  // by a different route than the one already fixed in `useVoice`.
  const stopChatSpeech = useCallback(() => {
    chatSpeechGenerationRef.current += 1;
    speechRef.current = null;
    assistantSpeechEchoUntilRef.current = Date.now() + 900;
    hardCancelBrowserSpeech();
    const audio = chatTtsAudioRef.current;
    if (audio) {
      audio.pause();
      try {
        audio.currentTime = 0;
      } catch {
        // Seeking a media element with no loaded source throws in some browsers.
        // Nothing to rewind in that case, which is the desired end state anyway.
      }
    }
    releaseChatTtsUrl();
    setIsSpeaking(false);
    setCurrentEmotion('neutral');
  }, [releaseChatTtsUrl]);

  const claimChatAudio = useCallback(() => {
    claimAkanshaAudio(chatAudioOwnerIdRef.current, stopChatSpeech);
  }, [stopChatSpeech]);

  /**
   * Say something, in her voice.
   *
   * Two engines, in strict preference order, and never both for the same text:
   *
   *   1. `/api/voice/tts` — the same route the voice page uses, so she sounds
   *      like one person across the app instead of like the local Windows voice
   *      registry on this page and like herself on the other one.
   *   2. `SpeechSynthesisUtterance` — only if the server produced no audio at
   *      all. Guarded by `serverAudioStarted`, because a decode failure *after*
   *      playback has begun would otherwise re-read the whole reply in a second
   *      voice on top of the first.
   */
  const speak = useCallback(
    async (text: string) => {
      if (!voiceEnabled || typeof window === 'undefined') return;
      claimChatAudio();
      stopChatSpeech();
      await settleBrowserSpeechCancel();
      const speechGeneration = ++chatSpeechGenerationRef.current;
      const plainText = text
        .replace(/```[\s\S]*?```/g, 'code block')
        .replace(/\*\*/g, '')
        .replace(/`/g, '');
      const speechLang = detectSpeechLang(plainText);
      lastAssistantSpeechTextRef.current = plainText;
      assistantSpeechEchoUntilRef.current =
        Date.now() + Math.min(18_000, Math.max(2_500, plainText.length * 55));

      // A newer turn has taken over, or this one was stopped. Either way this
      // utterance is stale and must not touch shared state.
      const isCurrent = () => speechGeneration === chatSpeechGenerationRef.current;

      const markSpeaking = () => {
        assistantSpeechEchoUntilRef.current =
          Date.now() + Math.min(18_000, Math.max(2_500, plainText.length * 55));
        setIsSpeaking(true);
        setCurrentEmotion('speaking');
      };
      const markSilent = (echoTailMs: number) => {
        assistantSpeechEchoUntilRef.current = Date.now() + echoTailMs;
        setIsSpeaking(false);
        setCurrentEmotion('neutral');
      };

      let serverAudioStarted = false;
      // 1200 rather than the fallback's 500: the cap exists to bound synthesis
      // latency, and the server does not pay per-character the way the local
      // engine's startup does. Long replies still get truncated, which is a
      // pre-existing limit of speaking a written answer aloud, not a new one.
      const blob = await fetchAkanshaSpeech(plainText.slice(0, 1200), {
        languageMode: languageModeFromTag(speechLang),
      });
      if (!isCurrent()) return;

      if (blob) {
        const audio = chatTtsAudioRef.current ?? new Audio();
        chatTtsAudioRef.current = audio;
        releaseChatTtsUrl();
        const url = URL.createObjectURL(blob);
        chatTtsUrlRef.current = url;
        audio.src = url;
        audio.volume = 0.9;
        audio.onplay = () => {
          if (!isCurrent()) return;
          serverAudioStarted = true;
          markSpeaking();
        };
        audio.onended = () => {
          if (!isCurrent()) return;
          releaseChatTtsUrl();
          markSilent(1500);
        };
        audio.onerror = () => {
          if (!isCurrent()) return;
          markSilent(900);
        };
        try {
          await audio.play();
          // Playing. It ends on `onended`, or a newer turn stops it.
          return;
        } catch {
          // Autoplay refusal, or a blob the decoder rejected outright. Neither
          // made a sound, so falling through to the local voice is safe.
          if (serverAudioStarted) return;
          releaseChatTtsUrl();
        }
      }

      const utterance = new SpeechSynthesisUtterance(plainText.slice(0, 500));
      utterance.lang = speechLang;
      utterance.rate = 1.0;
      utterance.pitch = 1.1;
      utterance.volume = 0.9;
      const voices = window.speechSynthesis.getVoices();
      const femaleVoice =
        voices.find(
          (v) =>
            v.lang.toLowerCase().startsWith(speechLang.slice(0, 2).toLowerCase()) &&
            (v.name.toLowerCase().includes('female') ||
              v.name.includes('Samantha') ||
              v.name.includes('Victoria') ||
              v.name.includes('Karen') ||
              v.name.includes('Heera') ||
              v.name.includes('Swara') ||
              v.name.includes('Shruti') ||
              v.name.includes('Neerja'))
        ) ??
        voices.find(
          (v) =>
            v.name.toLowerCase().includes('female') ||
            v.name.includes('Samantha') ||
            v.name.includes('Victoria') ||
            v.name.includes('Karen') ||
            v.name.includes('Heera') ||
            v.name.includes('Swara') ||
            v.name.includes('Shruti') ||
            v.name.includes('Neerja')
        );
      if (femaleVoice) utterance.voice = femaleVoice;
      utterance.onstart = () => {
        if (!isCurrent() || speechRef.current !== utterance) return;
        markSpeaking();
      };
      utterance.onend = () => {
        if (!isCurrent() || speechRef.current !== utterance) return;
        markSilent(1500);
      };
      utterance.onerror = () => {
        if (!isCurrent() || speechRef.current !== utterance) return;
        markSilent(900);
      };
      speechRef.current = utterance;
      window.speechSynthesis.speak(utterance);
    },
    [claimChatAudio, releaseChatTtsUrl, stopChatSpeech, voiceEnabled]
  );

  const clearVoiceFinalFlushTimer = useCallback(() => {
    if (voiceFinalFlushTimerRef.current) {
      clearTimeout(voiceFinalFlushTimerRef.current);
      voiceFinalFlushTimerRef.current = null;
    }
  }, []);

  const submitVoiceTranscript = useCallback(
    (rawTranscript: string) => {
      const spokenText = _compactTextForVoice(rawTranscript);
      if (!spokenText) return;

      const previous = lastVoiceSendRef.current;
      const now = Date.now();
      if (
        previous &&
        previous.text.toLowerCase() === spokenText.toLowerCase() &&
        now - previous.at < 2500
      ) {
        return;
      }

      clearVoiceFinalFlushTimer();
      pendingTranscriptRef.current = '';
      lastVoiceSendRef.current = { text: spokenText, at: now };
      sendFromVoiceRef.current(spokenText);
    },
    [clearVoiceFinalFlushTimer]
  );

  // Speech-to-text
  const toggleListening = useCallback(() => {
    if (typeof window === 'undefined') return;
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      toast.error('Speech recognition not supported in this browser');
      return;
    }

    if (isListeningRef.current || recognitionStartingRef.current) {
      micStoppedManuallyRef.current = true;
      recognitionStartingRef.current = false;
      submitVoiceTranscript(pendingTranscriptRef.current);
      if (voiceRestartTimerRef.current) {
        clearTimeout(voiceRestartTimerRef.current);
        voiceRestartTimerRef.current = null;
      }
      clearVoiceFinalFlushTimer();
      const activeRecognition = recognitionRef.current;
      recognitionRef.current = null;
      try {
        activeRecognition?.abort?.();
      } catch {
        try {
          activeRecognition?.stop();
        } catch {
          // Browser speech recognition can throw if it already ended.
        }
      }
      setIsListening(false);
      voiceEnabledRef.current = false;
      setVoiceEnabled(false);
      voiceListeningToastShownRef.current = false;
      return;
    }

    const startRecognition = async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach((track) => track.stop());
      } catch (error) {
        console.warn('Microphone permission denied:', error);
        micStoppedManuallyRef.current = true;
        recognitionStartingRef.current = false;
        recognitionRef.current = null;
        setIsListening(false);
        voiceEnabledRef.current = false;
        setVoiceEnabled(false);
        toast.error('Microphone permission is blocked. Allow mic access in your browser.');
        return;
      }

      const previousRecognition = recognitionRef.current;
      if (previousRecognition) {
        try {
          previousRecognition.abort?.();
        } catch {
          try {
            previousRecognition.stop();
          } catch {
            // Ignore stale speech-recognition cleanup errors.
          }
        }
        recognitionRef.current = null;
      }

      const recognition = new SpeechRecognition();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.maxAlternatives = 3;
      const preference = chatLanguagePreference();
      recognition.lang =
        preference === 'hindi' ? 'hi-IN' : preference === 'telugu_english' ? 'te-IN' : 'en-IN';

      pendingTranscriptRef.current = '';
      micStoppedManuallyRef.current = false;
      voiceEnabledRef.current = true;
      setVoiceEnabled(true);

      recognition.onstart = () => {
        recognitionStartingRef.current = false;
        setIsListening(true);
        if (!voiceListeningToastShownRef.current) {
          voiceListeningToastShownRef.current = true;
          toast.info('Listening... speak now');
        }
      };

      recognition.onresult = (event: SpeechRecognitionEvent) => {
        let interimTranscript = '';
        let finalTranscript = '';

        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const alternatives = Array.from(event.results[i]).map((alternative) =>
            alternative.transcript.trim()
          );
          const chunk = alternatives
            .filter(Boolean)
            .sort((left, right) => right.length - left.length)[0];
          if (event.results[i].isFinal) {
            finalTranscript += `${chunk} `;
          } else {
            interimTranscript += `${chunk} `;
          }
        }

        const heardText = `${finalTranscript} ${interimTranscript}`.trim();
        const isInterruptCommand = CHAT_INTERRUPT_PATTERN.test(heardText);
        const isAssistantEcho =
          heardText &&
          !isInterruptCommand &&
          looksLikeAssistantSpeechEcho(
            heardText,
            lastAssistantSpeechTextRef.current,
            assistantSpeechEchoUntilRef.current
          );

        if (isAssistantEcho) {
          pendingTranscriptRef.current = '';
          clearVoiceFinalFlushTimer();
          return;
        }

        if (heardText) {
          pendingTranscriptRef.current = heardText;
        }

        if (heardText && (isSpeakingRef.current || isStreamingRef.current)) {
          if (isInterruptCommand && !finalTranscript.trim()) {
            stopChatSpeech();
            activeChatAbortRef.current?.abort();
            activeChatAbortRef.current = null;
            setIsStreaming(false);
            setStreamingContent('');
            setCurrentEmotion('thinking');
            pendingTranscriptRef.current = '';
            clearVoiceFinalFlushTimer();
            return;
          }
          stopChatSpeech();
          activeChatAbortRef.current?.abort();
          activeChatAbortRef.current = null;
          setIsStreaming(false);
          setStreamingContent('');
          setCurrentEmotion('thinking');
        }

        if (finalTranscript.trim()) {
          submitVoiceTranscript(finalTranscript.trim());
          return;
        }

        if (interimTranscript.trim()) {
          clearVoiceFinalFlushTimer();
          voiceFinalFlushTimerRef.current = setTimeout(() => {
            if (micStoppedManuallyRef.current || isSpeakingRef.current || isStreamingRef.current)
              return;
            submitVoiceTranscript(pendingTranscriptRef.current);
          }, 1400);
        }
      };

      recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
        recognitionStartingRef.current = false;
        setIsListening(false);
        const errorCode = event?.error;

        if (
          errorCode === 'not-allowed' ||
          errorCode === 'service-not-allowed' ||
          errorCode === 'audio-capture'
        ) {
          micStoppedManuallyRef.current = true;
          voiceEnabledRef.current = false;
          recognitionRef.current = null;
          setVoiceEnabled(false);
          setIsListening(false);
          voiceListeningToastShownRef.current = false;
          toast.error(
            errorCode === 'audio-capture'
              ? 'No microphone input was detected. Check your mic device and try again.'
              : 'Browser blocked voice input. Allow microphone and speech access.'
          );
          return;
        }

        if (TRANSIENT_SPEECH_ERRORS.has(errorCode)) {
          if (
            !micStoppedManuallyRef.current &&
            voiceEnabledRef.current &&
            !voiceRestartTimerRef.current
          ) {
            voiceRestartTimerRef.current = setTimeout(() => {
              voiceRestartTimerRef.current = null;
              if (
                isListeningRef.current ||
                recognitionStartingRef.current ||
                !voiceEnabledRef.current
              )
                return;
              recognitionRef.current = null;
              toggleListening();
            }, 500);
          }
          return;
        }

        toast.error(
          `Voice input paused (${errorCode || 'unknown error'}). I will keep trying while voice mode is on.`
        );
      };

      recognition.onend = () => {
        recognitionStartingRef.current = false;
        setIsListening(false);
        if (recognitionRef.current === recognition) {
          recognitionRef.current = null;
        }
        const spokenText = pendingTranscriptRef.current;
        clearVoiceFinalFlushTimer();
        if (!micStoppedManuallyRef.current && spokenText.trim()) {
          submitVoiceTranscript(spokenText);
        }

        if (!micStoppedManuallyRef.current && voiceEnabledRef.current) {
          voiceRestartTimerRef.current = setTimeout(() => {
            voiceRestartTimerRef.current = null;
            if (isListeningRef.current || recognitionStartingRef.current) return;
            recognitionRef.current = null;
            toggleListening();
          }, 350);
        }
        pendingTranscriptRef.current = '';
      };

      recognitionRef.current = recognition;

      try {
        recognitionStartingRef.current = true;
        recognition.start();
      } catch (error) {
        recognitionStartingRef.current = false;
        recognitionRef.current = null;
        setIsListening(false);
        console.warn('Speech recognition start failed:', error);
        if (!micStoppedManuallyRef.current && voiceEnabledRef.current) {
          voiceRestartTimerRef.current = setTimeout(() => {
            voiceRestartTimerRef.current = null;
            if (
              isListeningRef.current ||
              recognitionStartingRef.current ||
              !voiceEnabledRef.current
            )
              return;
            recognitionRef.current = null;
            toggleListening();
          }, 600);
          return;
        }
        toast.error('Could not start voice input. Refresh once and try again.');
      }
    };

    void startRecognition();
  }, [clearVoiceFinalFlushTimer, stopChatSpeech, submitVoiceTranscript]);

  // Both of these exist so `AvatarPanel` can be memoized without the wrapper being
  // inert. It used to receive two inline arrows, which are a new object identity on
  // every render of this 2200-line component -- so a memo on the panel would have
  // compared unequal every single time and skipped nothing.
  //
  // The updater form is what keeps the dep list empty: reading `voiceEnabled`
  // directly would put it in the deps and hand the panel a new function every time
  // the mic is toggled. Clearing the two refs inside the updater is safe because
  // both assignments are idempotent, so React calling it twice in dev changes
  // nothing.
  const toggleVoiceEnabled = useCallback(() => {
    setVoiceEnabled((previous) => {
      const next = !previous;
      if (next) {
        micStoppedManuallyRef.current = false;
        voiceAutoStartAttemptedRef.current = false;
      }
      return next;
    });
  }, []);

  const collapseAvatar = useCallback(() => setAvatarExpanded(false), []);

  const simulateStreaming = useCallback(
    (content: string, emotion: Emotion = 'neutral', shouldSpeak = false) => {
      setIsStreaming(true);
      setStreamingContent('');
      setCurrentEmotion('thinking');
      let index = 0;
      const words = content.split(' ');
      const interval = setInterval(() => {
        if (index < words.length) {
          setStreamingContent((prev) => prev + (prev ? ' ' : '') + words[index]);
          index++;
        } else {
          clearInterval(interval);
          const newMsg: Message = {
            id: `msg-${Date.now()}`,
            role: 'assistant',
            content,
            model: selectedModel,
            timestamp: new Date(),
            tokenCount: estimateTokenCount(content),
            memoryRefs: Math.random() > 0.5 ? ['Previous context'] : undefined,
            emotion,
          };
          setMessages((prev) => [...prev, newMsg]);
          setIsStreaming(false);
          setStreamingContent('');
          setCurrentEmotion(emotion);
          if (shouldSpeak) {
            speak(content);
          }
        }
      }, 35);
    },
    [selectedModel, speak]
  );

  const addAssistantMessage = useCallback(
    (content: string, emotion: Emotion = 'neutral') => {
      const nextMessage: Message = {
        id: `msg-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        role: 'assistant',
        content,
        model: selectedModel,
        timestamp: new Date(),
        tokenCount: estimateTokenCount(content),
        emotion,
      };
      setMessages((previous) => [...previous, nextMessage]);
      fetch(apiUrl('/api/chat/message'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          role: 'assistant',
          content,
          session_id: activeSessionIdRef.current,
        }),
      })
        .then(() => {
          window.dispatchEvent(new CustomEvent('akansha-history-updated'));
        })
        .catch((error) => console.warn('Failed to persist planner assistant message:', error));
    },
    [selectedModel]
  );

  const handleSend = useCallback(
    async (content: string, attachments?: File[], source: 'text' | 'voice' = 'text') => {
      if (!content.trim() && !attachments?.length) return;
      const hasAttachments = Boolean(attachments?.length);
      const continueFromId = activeBranchFromId;
      const continueFromLocalId = continueFromId ? String(continueFromId) : null;
      const shouldSpeakReply = source === 'voice';
      activeChatAbortRef.current?.abort();
      activeChatAbortRef.current = null;
      stopChatSpeech();
      setStreamingContent('');
      setIsStreaming(false);
      const detectedEmotion = detectEmotion(content);
      const localUserMessageId = `msg-${Date.now()}`;
      const userMsg: Message = {
        id: localUserMessageId,
        role: 'user',
        content,
        timestamp: new Date(),
        attachments: attachments?.map((f, i) => {
          const previewUrl = f.type.startsWith('image/') ? URL.createObjectURL(f) : undefined;
          if (previewUrl) previewUrlsRef.current.push(previewUrl);
          return {
            id: `att-${i}`,
            name: f.name,
            type: f.type,
            size: `${(f.size / 1024).toFixed(1)} KB`,
            previewUrl,
          };
        }),
        branchFromId: continueFromId,
      };
      setMessages((prev) => insertMessageAfter(prev, continueFromLocalId, userMsg));
      setStreamingAfterMessageId(localUserMessageId);
      setCurrentEmotion('thinking');

      const persistPlannerSideMessage = (role: 'user' | 'assistant', messageContent: string) => {
        fetch(apiUrl('/api/chat/message'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            role,
            content: messageContent,
            session_id: activeSessionIdRef.current,
          }),
        })
          .then(() => {
            window.dispatchEvent(new CustomEvent('akansha-history-updated'));
          })
          .catch((error) => console.warn(`Failed to persist ${role} planner message:`, error));
      };

      const isQuickMode = chatWorkMode === 'quick';
      const fastReply =
        isQuickMode && !hasAttachments
          ? fastLocalChatReply(content, chatLanguagePreference())
          : null;
      if (fastReply) {
        persistPlannerSideMessage('user', content);
        setStreamingAfterMessageId(null);
        setCurrentEmotion('happy');
        addAssistantMessage(fastReply, 'happy');
        if (shouldSpeakReply) {
          speak(fastReply);
        }
        return;
      }
      const resolvePlannerTitle = (draft: PlannerCommand) => {
        if (draft.mode === 'delete') return draft.title;
        if (!isWeakPlannerTitle(draft.title)) return draft.title;

        const previousUserMessage = [...messagesRef.current]
          .reverse()
          .find(
            (message) =>
              message.role === 'user' &&
              message.content.trim().toLowerCase() !== content.trim().toLowerCase() &&
              !inferPlannerCommand(message.content)
          );

        if (!previousUserMessage) return draft.title;
        const nextTitle = cleanPlannerTitle(previousUserMessage.content);
        return isWeakPlannerTitle(nextTitle) ? draft.title : nextTitle;
      };

      const finalizePlannerAction = (draft: PlannerCommand) => {
        const resolvedTitle = resolvePlannerTitle(draft).trim();
        return applyPlannerCommand(
          {
            ...draft,
            title: resolvedTitle,
          },
          resolvedTitle
        );
      };

      const pendingPlanner = pendingPlannerRef.current;
      if (!hasAttachments && pendingPlanner) {
        if (isPlannerPreparationPrompt(content)) {
          addAssistantMessage(
            pendingPlanner.kind === 'calendar'
              ? 'I am still waiting for the real calendar details. Tell me the event title, date, time, and whether you want a reminder.'
              : 'I am still waiting for the real to-do details. Tell me the actual items you want me to save.',
            'thinking'
          );
          return;
        }

        persistPlannerSideMessage('user', content);
        const replyLower = content.toLowerCase();
        const replyDate = extractDateValue(content);
        const replyTimes = extractTimeWindow(content);
        const treatAsTaskDetails =
          pendingPlanner.kind === 'task' &&
          !replyDate &&
          !replyTimes.startTime &&
          isLikelyTaskDetails(content);
        const reminderEnabled = /\b(no|without)\b/.test(replyLower)
          ? false
          : pendingPlanner.reminderEnabled ||
            /\b(yes|remind|notification|notify)\b/.test(replyLower);

        const resolvedDraft: PlannerCommand = {
          ...pendingPlanner,
          title: treatAsTaskDetails ? content.trim() : pendingPlanner.title,
          date: replyDate || pendingPlanner.date || new Date().toISOString().slice(0, 10),
          startTime: replyTimes.startTime || pendingPlanner.startTime,
          endTime:
            replyTimes.endTime ||
            pendingPlanner.endTime ||
            (replyTimes.startTime ? addMinutes(replyTimes.startTime, 30) : undefined),
          reminderEnabled,
          reminderAt:
            reminderEnabled &&
            (replyDate || pendingPlanner.date || new Date().toISOString().slice(0, 10))
              ? `${replyDate || pendingPlanner.date || new Date().toISOString().slice(0, 10)}T${
                  replyTimes.startTime || pendingPlanner.startTime || '09:00'
                }:00`
              : undefined,
        };

        if (resolvedDraft.kind === 'calendar' && !resolvedDraft.startTime) {
          addAssistantMessage(
            'Got it. Tell me the reminder or event time in AM/PM format, like 6:30 PM or 9:15 AM.',
            'thinking'
          );
          pendingPlannerRef.current = resolvedDraft;
          return;
        }

        const plannerResult = finalizePlannerAction(resolvedDraft);
        addAssistantMessage(plannerResult.message, plannerResult.success ? 'happy' : 'thinking');
        pendingPlannerRef.current = null;
        return;
      }

      if (!hasAttachments && isReminderOnlyPlannerFollowUp(content)) {
        persistPlannerSideMessage('user', content);
        const plannerResult = applyPlannerReminderFollowUp(content);
        addAssistantMessage(plannerResult.message, plannerResult.success ? 'happy' : 'thinking');
        pendingPlannerRef.current = null;
        return;
      }

      if (!hasAttachments && !isAlertReminderIntent(content) && isAutomationIntent(content)) {
        fetch(apiUrl('/api/automation/browser/prompt'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            prompt: normalizeAutomationPrompt(content),
            background: true,
          }),
        })
          .then((res) => res.json())
          .then((payload) => {
            const messageText =
              payload?.message ||
              payload?.detail ||
              'I tried to run that automation command, but I could not confirm the result.';
            const noteText = payload?.note ? ` ${payload.note}` : '';
            addAssistantMessage(
              `${messageText}${noteText}`.trim(),
              payload?.success ? 'happy' : 'thinking'
            );

            // If an OpenWork session was initiated, start polling its status
            const owId = payload?.openwork_session_id as string | undefined;
            if (owId) {
              const pollOW = setInterval(async () => {
                try {
                  const owRes = await fetch(apiUrl(`/api/openwork/status/${owId}`));
                  const owData = (await owRes.json()) as Record<string, unknown>;
                  if (owData.found) {
                    setOwTaskStatus({
                      session_id: owId,
                      site_domain: (owData.site_domain as string) || '',
                      status: (owData.status as string) || 'active',
                      current_step: (owData.current_step as number) || 0,
                      total_steps: (owData.total_steps as number) || 0,
                      requires_login: Boolean(owData.requires_login),
                      login_prompt: (owData.login_prompt as string) || null,
                    });
                    if (owData.status === 'completed' || owData.status === 'failed') {
                      clearInterval(pollOW);
                    }
                  }
                } catch {
                  /* silent */
                }
              }, 2000);
              // Auto-clear after 5 minutes
              setTimeout(() => clearInterval(pollOW), 300_000);
            }
          })
          .catch((error) => {
            console.warn('Chat automation failed:', error);
            addAssistantMessage(
              'I tried to run that automation command, but the automation service was unavailable.',
              'thinking'
            );
          })
          .finally(() => {
            setIsStreaming(false);
          });
        return;
      }

      const plannerIntent = hasAttachments ? null : inferPlannerCommand(content);
      if (plannerIntent) {
        persistPlannerSideMessage('user', content);
        if (isPlannerPreparationPrompt(content)) {
          pendingPlannerRef.current = {
            ...plannerIntent,
            title: 'Planner item',
          };
          addAssistantMessage(
            plannerIntent.kind === 'calendar'
              ? 'Sure — tell me the actual calendar details in your next message, like the event title, date, time, and whether you want a reminder. I will wait instead of saving this setup sentence.'
              : 'Sure — send me the actual to-do items in your next message, and I will add them properly instead of saving this setup sentence.',
            'thinking'
          );
          return;
        }

        const needsReminderFollowUp =
          plannerIntent.mode === 'create' &&
          !plannerIntent.reminderEnabled &&
          !plannerIntent.startTime &&
          !plannerIntent.reminderAt;
        const needsCalendarTime =
          plannerIntent.kind === 'calendar' &&
          plannerIntent.mode === 'create' &&
          (!plannerIntent.startTime || !plannerIntent.date);

        if (needsReminderFollowUp || needsCalendarTime) {
          pendingPlannerRef.current = plannerIntent;
          addAssistantMessage(
            plannerIntent.kind === 'calendar'
              ? `I can add "${resolvePlannerTitle(plannerIntent)}" to your calendar. Do you want a reminder too? If yes, tell me the date and time in AM/PM, like tomorrow 6:30 PM.`
              : `I can add "${resolvePlannerTitle(plannerIntent)}" to your to-do list. Do you want a reminder too? If yes, tell me the date and time in AM/PM, like today 8:45 PM.`,
            'thinking'
          );
          return;
        }

        const plannerResult = finalizePlannerAction({
          ...plannerIntent,
          title: resolvePlannerTitle(plannerIntent),
          date: plannerIntent.date || new Date().toISOString().slice(0, 10),
          endTime:
            plannerIntent.kind === 'calendar'
              ? plannerIntent.endTime || addMinutes(plannerIntent.startTime || '09:00', 30)
              : plannerIntent.endTime,
          reminderAt:
            plannerIntent.reminderEnabled &&
            (plannerIntent.date || new Date().toISOString().slice(0, 10))
              ? `${plannerIntent.date || new Date().toISOString().slice(0, 10)}T${
                  plannerIntent.startTime || '09:00'
                }:00`
              : undefined,
        });
        addAssistantMessage(plannerResult.message, plannerResult.success ? 'happy' : 'thinking');
        return;
      }

      const streamResponse = async () => {
        const controller = new AbortController();
        activeChatAbortRef.current = controller;
        setIsStreaming(true);
        setStreamingContent('');
        setCurrentEmotion('thinking');
        let accumulated = '';
        let serverUserMessageId: string | null = null;
        let serverAssistantMessageId: string | null = null;

        try {
          const attachmentPayloads = await buildChatAttachments(attachments);
          const response = await fetch(apiUrl('/api/chat/stream'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            signal: controller.signal,
            body: JSON.stringify({
              message: content,
              session_id: activeSessionIdRef.current,
              conversation_mode: shouldSpeakReply ? 'voice' : chatWorkMode,
              language_preference: chatLanguagePreference(),
              attachments: attachmentPayloads,
              continue_from_message_id: continueFromId,
            }),
          });

          if (!response.body) {
            throw new Error('Streaming response was not available');
          }

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = '';

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const events = buffer.split('\n\n');
            buffer = events.pop() ?? '';

            for (const event of events) {
              const payloadLine = event.split('\n').find((line) => line.startsWith('data: '));
              if (!payloadLine) continue;

              const payload = JSON.parse(payloadLine.slice(6));
              if (payload.type === 'chunk') {
                accumulated += payload.content;
                setStreamingContent(accumulated);
              }
              if (payload.type === 'done') {
                accumulated = payload.content;
                setStreamingContent(accumulated);
                if (payload.user_message_id) {
                  serverUserMessageId = String(payload.user_message_id);
                }
                if (payload.assistant_message_id) {
                  serverAssistantMessageId = String(payload.assistant_message_id);
                }
              }
              if (payload.type === 'error') {
                throw new Error(payload.message);
              }
            }
          }

          if (controller.signal.aborted) return;
          const responseEmotion: Emotion = detectedEmotion === 'sad' ? 'sad' : 'happy';
          const newMsg: Message = {
            id:
              serverAssistantMessageId ||
              `msg-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            role: 'assistant',
            content: accumulated,
            model: selectedModel,
            timestamp: new Date(),
            tokenCount: estimateTokenCount(accumulated),
            emotion: responseEmotion,
            branchFromId: continueFromId,
          };
          setMessages((prev) => {
            const savedUserMessageId = serverUserMessageId;
            const next = savedUserMessageId
              ? prev.map((message) =>
                  message.id === localUserMessageId
                    ? { ...message, id: savedUserMessageId }
                    : message
                )
              : prev;
            return insertMessageAfter(next, savedUserMessageId || localUserMessageId, newMsg);
          });
          setStreamingAfterMessageId(null);
          const assistantNumericId = serverAssistantMessageId
            ? Number(serverAssistantMessageId)
            : null;
          if (continueFromId && assistantNumericId) {
            setActiveBranchFromId(assistantNumericId);
          }
          setCurrentEmotion(responseEmotion);
          window.dispatchEvent(new CustomEvent('akansha-history-updated'));
          if (shouldSpeakReply) {
            speak(accumulated);
          }
        } catch (err) {
          if (controller.signal.aborted) return;
          console.warn('[Akansha chat] recovered request failure:', err);
          simulateStreaming(formatChatError(err), 'sad', shouldSpeakReply);
        } finally {
          if (activeChatAbortRef.current === controller) {
            activeChatAbortRef.current = null;
          }
          if (!controller.signal.aborted) {
            setIsStreaming(false);
            setStreamingContent('');
            setStreamingAfterMessageId(null);
          }
        }
      };

      void streamResponse();
    },
    [
      activeBranchFromId,
      addAssistantMessage,
      chatWorkMode,
      selectedModel,
      simulateStreaming,
      speak,
      stopChatSpeech,
    ]
  );

  /**
   * Stable so ChatComposer's memo holds. As an inline arrow in the JSX this was a
   * new function on every render, which on its own was enough to re-render the
   * 494-line composer on every streaming token.
   */
  const openPromptLibrary = useCallback(() => {
    setPromptModalOpen(true);
  }, []);

  // Same reasoning, for `PromptTemplateModal`. Not stable in the absolute sense --
  // `handleSend` changes when the model, work mode or branch point changes -- but it
  // does not change per streamed token, which is the render storm the memo exists to
  // absorb.
  const closePromptLibrary = useCallback(() => {
    setPromptModalOpen(false);
  }, []);

  const applyPromptFromLibrary = useCallback(
    (prompt: string) => {
      handleSend(prompt);
      setPromptModalOpen(false);
    },
    [handleSend]
  );

  const stopActiveResponse = useCallback(() => {
    activeChatAbortRef.current?.abort();
    activeChatAbortRef.current = null;
    stopChatSpeech();
    setIsStreaming(false);
    setStreamingContent('');
    setStreamingAfterMessageId(null);
    setCurrentEmotion('neutral');
    toast.info('Stopped. You can type or attach a screenshot now.');
  }, [stopChatSpeech]);

  useEffect(() => {
    sendFromVoiceRef.current = (content: string) => handleSend(content, undefined, 'voice');
  }, [handleSend]);

  useEffect(() => {
    if (!voiceEnabled) return;

    const keepAlive = setInterval(() => {
      if (
        voiceEnabledRef.current &&
        !micStoppedManuallyRef.current &&
        !isListeningRef.current &&
        !recognitionStartingRef.current
      ) {
        toggleListening();
      }
    }, 1600);

    return () => clearInterval(keepAlive);
  }, [toggleListening, voiceEnabled]);

  useEffect(() => {
    // Copied out rather than read in the cleanup. The ref is write-once (assigned
    // its random id at line 568 and never reassigned), so this is equivalent --
    // but the lint rule cannot know that, and the pattern it warns about is a real
    // bug elsewhere, so it is worth keeping the rule loud rather than suppressed.
    const audioOwnerId = chatAudioOwnerIdRef.current;
    return () => {
      clearVoiceFinalFlushTimer();
      if (voiceRestartTimerRef.current) {
        clearTimeout(voiceRestartTimerRef.current);
        voiceRestartTimerRef.current = null;
      }
      releaseAkanshaAudio(audioOwnerId);
      activeChatAbortRef.current?.abort();
      stopChatSpeech();
    };
  }, [clearVoiceFinalFlushTimer, stopChatSpeech]);

  const handleShare = () => {
    navigator.clipboard.writeText('https://akansha.ai/share/conv-abc123');
    toast.success('Shareable link copied to clipboard');
  };

  const handleDeleteConversation = async () => {
    const confirmed = window.confirm('Delete this conversation from chat history?');
    if (!confirmed) return;

    try {
      const response = await fetch(apiUrl(`/api/chat/session/${encodeURIComponent(sessionId)}`), {
        method: 'DELETE',
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Could not delete this conversation.');
      }

      deleteSessionTitle(sessionId);
      setMessages([]);
      revokePreviewUrls();
      setStreamingContent('');
      setIsStreaming(false);
      window.dispatchEvent(new CustomEvent('akansha-history-updated'));
      window.dispatchEvent(new CustomEvent('akansha-new-chat'));
      toast.success('Conversation deleted');
    } catch (error) {
      console.warn('[Akansha chat] recovered delete failure:', error);
      toast.error(error instanceof Error ? error.message : 'Could not delete this conversation.');
    }
  };

  const toggleMessagePin = useCallback((message: Message) => {
    const numericId = getMessageNumericId(message);
    if (!numericId) {
      toast.info('This message can be pinned after it finishes saving.');
      return;
    }

    const nextPinned = !message.pinned;
    setMessages((previous) =>
      previous.map((item) => (item.id === message.id ? { ...item, pinned: nextPinned } : item))
    );

    fetch(apiUrl(`/api/chat/message/${numericId}/pin`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pinned: nextPinned }),
    })
      .then((response) => {
        if (!response.ok) throw new Error('Could not update the pin.');
        toast.success(nextPinned ? 'Message pinned' : 'Message unpinned');
      })
      .catch((error) => {
        console.warn('[Akansha chat] recovered pin failure:', error);
        setMessages((previous) =>
          previous.map((item) =>
            item.id === message.id ? { ...item, pinned: message.pinned } : item
          )
        );
        toast.error(error instanceof Error ? error.message : 'Could not update the pin.');
      });
  }, []);

  const continueFromMessage = useCallback((message: Message) => {
    const numericId = getMessageNumericId(message);
    if (!numericId) {
      toast.info('Wait for this message to finish saving, then continue from it.');
      return;
    }

    setActiveBranchFromId(numericId);
    setTimeout(() => {
      document
        .querySelector<HTMLTextAreaElement>('textarea[placeholder^="Message Akansha"]')
        ?.focus();
    }, 0);
    toast.success('Continue mode is active. Your next message will be inserted here.');
  }, []);

  const clearContinueMode = useCallback(() => {
    setActiveBranchFromId(null);
    toast.info('Continue mode cleared. New replies will go to the bottom.');
  }, []);

  // Both of these used to run bare in the render body, so they walked the whole
  // messages array on every render -- and this component re-renders on every token
  // of a streaming reply. Two full O(n) passes per token, producing the same answer
  // every time until a pin or a branch anchor actually changes.
  const pinnedMessages = useMemo(() => messages.filter((message) => message.pinned), [messages]);
  // Same reasoning: the header re-renders per streamed token, and the wording
  // rules only change when a probe comes back.
  const automationSummary = useMemo(
    () => summariseAutomation(automationStatus),
    [automationStatus]
  );
  const activeBranchMessage = useMemo(
    () =>
      activeBranchFromId
        ? messages.find((message) => getMessageNumericId(message) === activeBranchFromId)
        : null,
    [messages, activeBranchFromId]
  );

  return (
    <div className="flex flex-col h-full">
      {/* Chat header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-border bg-card/80 backdrop-blur-md shrink-0 z-10">
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-bold text-foreground tracking-tight truncate">
            Akansha Chat
          </h2>
          <div className="flex items-center gap-2 mt-0.5 text-xs">
            <span className="text-muted-foreground font-medium">{messages.length} messages</span>
            <span className="text-muted-foreground/50">·</span>
            <span className="text-accent font-medium flex items-center gap-1">
              <Brain size={11} />
              Memory active
            </span>
            <span className="text-muted-foreground/50">·</span>
            {/* Was `${count} automation permissions active`, unconditionally, from
                a dict of six hardcoded server-side `true`s -- so it read the same
                on a machine that could not deliver a single click. Now it reports
                what the last probe measured, and opens the panel that can change
                it. */}
            <button
              type="button"
              onClick={() => window.dispatchEvent(new CustomEvent('akansha-toggle-automation'))}
              title={automationSummary.detail}
              className={`flex items-center gap-1 font-medium transition-colors hover:underline ${
                automationSummary.tone === 'live'
                  ? 'text-[#38bdf8]'
                  : automationSummary.tone === 'degraded'
                    ? 'text-amber-400'
                    : automationSummary.tone === 'offline'
                      ? 'text-rose-400'
                      : 'text-muted-foreground'
              }`}
            >
              {automationSummary.tone === 'live' ? (
                <CheckCheck size={11} />
              ) : (
                <AlertTriangle size={11} />
              )}
              {automationSummary.label}
            </button>
          </div>
        </div>

        <div
          className="hidden xl:flex items-center rounded-xl border border-border bg-background/60 p-1"
          role="group"
          aria-label="Chat work mode"
        >
          {CHAT_WORK_MODES.map((mode) => (
            <button
              key={mode.id}
              type="button"
              onClick={() => setChatWorkMode(mode.id)}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${
                chatWorkMode === mode.id
                  ? 'bg-primary text-white shadow-sm'
                  : 'text-muted-foreground hover:text-foreground hover:bg-muted'
              }`}
              title={mode.hint}
            >
              {mode.label}
            </button>
          ))}
        </div>

        <ModelSelector selected={selectedModel} onChange={setSelectedModel} />

        <div className="flex items-center gap-1">
          <button
            onClick={handleShare}
            className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
            title="Share conversation"
          >
            <Share2 size={15} />
          </button>

          <div className="relative">
            <button
              onClick={() => setMoreMenuOpen(!moreMenuOpen)}
              className="p-2 rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
              title="More options"
            >
              <MoreHorizontal size={15} />
            </button>
            {moreMenuOpen && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setMoreMenuOpen(false)} />
                <div className="absolute right-0 top-full mt-1 z-50 bg-card border border-border rounded-xl shadow-lg py-1 min-w-[160px] animate-fade-in">
                  {[
                    { key: 'menu-star', icon: Star, label: 'Star conversation' },
                    { key: 'menu-share', icon: Share2, label: 'Share publicly' },
                    {
                      key: 'menu-delete',
                      icon: Trash2,
                      label: 'Delete conversation',
                      danger: true,
                    },
                  ].map(({ key, icon: Icon, label, danger }) => (
                    <button
                      key={key}
                      onClick={() => {
                        setMoreMenuOpen(false);
                        if (key === 'menu-delete') {
                          void handleDeleteConversation();
                          return;
                        }
                        toast.success(`${label} action triggered`);
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
        </div>
      </div>

      {pinnedMessages.length > 0 && (
        <div className="px-4 py-2 border-b border-border bg-amber-400/[0.03] shrink-0">
          <div className="flex items-center gap-2 overflow-x-auto scrollbar-thin">
            <span className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-amber-400 shrink-0">
              <Pin size={12} fill="currentColor" />
              Pinned
            </span>
            {pinnedMessages.slice(0, 6).map((message) => (
              <button
                key={`pinned-${message.id}`}
                type="button"
                onClick={() =>
                  document
                    .getElementById(`chat-message-${message.id}`)
                    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
                }
                className="max-w-72 truncate rounded-full border border-amber-400/20 bg-amber-400/10 px-3 py-1.5 text-xs text-foreground hover:bg-amber-400/15 transition-colors"
                title={message.content}
              >
                {message.role === 'user' ? 'You: ' : 'Akansha: '}
                {message.content || 'Attachment message'}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Presence bar — the full console when expanded, a compact strip otherwise */}
      {showAvatarBar && (
        <div
          className={`border-b border-border bg-card/30 shrink-0 ${
            avatarExpanded ? 'px-4 py-3 flex justify-center' : 'px-4 py-2 flex items-center gap-3'
          }`}
        >
          {avatarExpanded ? (
            <AvatarPanel
              emotion={currentEmotion}
              isSpeaking={isSpeaking}
              isListening={isListening}
              onToggleMic={toggleListening}
              onToggleVoice={toggleVoiceEnabled}
              voiceEnabled={voiceEnabled}
              minimized={false}
              onToggleMinimize={collapseAvatar}
            />
          ) : (
            <div className="flex items-center gap-3 w-full">
              <div className="flex items-center gap-2">
                {/* The same core at 40px, not a letter in a gradient circle. The two
                    ping/pulse rings that used to sit on top of that circle are gone
                    with it: the core reports listening and speaking itself, and a
                    second ring around it said the same thing in a different visual
                    language. */}
                <JarvisCore
                  state={coreStateFor(currentEmotion, isSpeaking, isListening)}
                  accent={EMOTION_COLORS[currentEmotion]}
                  size={40}
                />
                <div>
                  <p className="text-xs font-semibold text-foreground">Akansha</p>
                  <p className="text-xs text-muted-foreground">
                    {presenceLabel(currentEmotion, isListening)}
                  </p>
                </div>
              </div>

              {/* Voice waveform */}
              {(isSpeaking || isListening) && (
                <div className="flex items-center gap-0.5 h-5">
                  {[0, 1, 2, 3, 4].map((i) => (
                    <div
                      key={`wave-${i}`}
                      className="w-1 rounded-full"
                      style={{
                        background: isListening ? '#EF4444' : 'var(--violet-primary)',
                        animation: `waveform ${0.4 + i * 0.06}s ease-in-out infinite alternate`,
                        animationDelay: `${i * 0.08}s`,
                      }}
                    />
                  ))}
                </div>
              )}

              <div className="flex items-center gap-1 ml-auto">
                <button
                  onClick={toggleListening}
                  className={`p-1.5 rounded-lg transition-all text-xs ${
                    isListening
                      ? 'bg-red-500/15 text-red-500 border border-red-500/30'
                      : 'bg-muted text-muted-foreground hover:text-foreground border border-border'
                  }`}
                  title={isListening ? 'Stop listening' : 'Voice input'}
                >
                  {isListening ? <MicOff size={13} /> : <Mic size={13} />}
                </button>
                <button
                  onClick={() => setAvatarExpanded(true)}
                  className="p-1.5 rounded-lg bg-muted text-muted-foreground hover:text-foreground border border-border transition-all"
                  title="Expand presence"
                >
                  <ChevronUp size={13} />
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto scrollbar-thin px-4 py-4">
        {/* The reading column. The scroll container stays full-width so its
            scrollbar sits at the pane edge, but the content is capped and centred.
            Before this there was no max-width container anywhere on the page: at
            1440px a bubble stretched the full ~1100px pane, roughly 190 characters
            per line, about three times the width at which prose stays readable.
            `space-y-1` moves here with the children so spacing is unchanged. */}
        <div className="mx-auto w-full max-w-3xl space-y-1">
          {messages.length === 0 && !isStreaming && <EmptyThread onPick={handleSend} />}
          {messages.map((msg) => (
            <React.Fragment key={msg.id}>
              <MessageBubble
                message={msg}
                onTogglePin={toggleMessagePin}
                onContinueFrom={continueFromMessage}
                isBranchAnchor={activeBranchFromId === getMessageNumericId(msg)}
              />
              {isStreaming && streamingAfterMessageId === msg.id && (
                <div className="message-enter">
                  <MessageBubble
                    message={{
                      id: 'streaming',
                      role: 'assistant',
                      content: streamingContent,
                      model: selectedModel,
                      timestamp: new Date(),
                      isStreaming: true,
                    }}
                  />
                </div>
              )}
            </React.Fragment>
          ))}

          {isStreaming && !streamingAfterMessageId && (
            <div className="message-enter">
              <MessageBubble
                message={{
                  id: 'streaming',
                  role: 'assistant',
                  content: streamingContent,
                  model: selectedModel,
                  timestamp: new Date(),
                  isStreaming: true,
                }}
              />
            </div>
          )}

          <div ref={bottomRef} />
        </div>
      </div>

      {/* Prompt suggestions */}
      <div className="px-4 py-2 shrink-0">
        {/* Aligned to the same reading column as the bubbles and the composer, and
            the scrollbar is hidden rather than styled: a horizontal scrollbar
            rendered permanently under the chips, which read as a broken row rather
            than a scrollable one. The chips still scroll by drag/wheel. */}
        <div className="mx-auto w-full max-w-3xl flex items-center gap-2 overflow-x-auto no-scrollbar">
          {[
            'Add unit tests',
            'Explain the JWT flow',
            'Add TypeScript generics',
            'How to handle refresh tokens?',
            'What can OpenWork do?',
          ].map((suggestion) => (
            <button
              key={`suggestion-${suggestion}`}
              onClick={() => handleSend(suggestion)}
              className="shrink-0 text-xs px-3 py-1.5 rounded-full border border-border bg-card hover:bg-muted hover:border-primary/30 text-muted-foreground hover:text-foreground transition-colors"
            >
              {suggestion}
            </button>
          ))}
        </div>
      </div>

      {activeBranchMessage && (
        <div className="mx-4 mb-2 rounded-2xl border border-primary/25 bg-primary/10 px-3 py-2 text-xs text-foreground flex items-center gap-3 shrink-0">
          <GitBranch size={14} className="text-[#9B7FFF] shrink-0" />
          <span className="min-w-0 flex-1 truncate">
            Continuing from {activeBranchMessage.role === 'user' ? 'your' : 'Akansha'} message:{' '}
            <span className="text-muted-foreground">
              {activeBranchMessage.content || 'attachment message'}
            </span>
          </span>
          <button
            type="button"
            onClick={clearContinueMode}
            className="p-1 rounded-lg hover:bg-background/50 text-muted-foreground hover:text-foreground transition-colors"
            title="Clear continue mode"
          >
            <X size={13} />
          </button>
        </div>
      )}

      {/* OpenWork task status panel */}
      {owTaskStatus && owTaskStatus.status !== 'completed' && (
        <div className="mx-4 mb-2 shrink-0">
          <button
            type="button"
            onClick={() => setOwStatusExpanded((v) => !v)}
            className="w-full flex items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-950/40 px-3 py-2 text-xs text-amber-300 hover:bg-amber-950/60 transition-colors"
          >
            <div className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
            <span className="flex-1 text-left font-mono">
              OpenWork · {owTaskStatus.site_domain}
              {owTaskStatus.requires_login
                ? ' · 🔐 Waiting for login'
                : ` · Step ${owTaskStatus.current_step}/${owTaskStatus.total_steps}`}
            </span>
            <ChevronDown
              size={12}
              className={`transition-transform ${owStatusExpanded ? 'rotate-180' : ''}`}
            />
          </button>
          {owStatusExpanded && (
            <div className="mt-1 rounded-xl border border-amber-500/20 bg-amber-950/30 px-3 py-2 text-xs font-mono space-y-1">
              <div className="flex justify-between text-amber-200/70">
                <span>Session</span>
                <span className="truncate max-w-[180px]">{owTaskStatus.session_id}</span>
              </div>
              <div className="flex justify-between text-amber-200/70">
                <span>Status</span>
                <span className="capitalize">{owTaskStatus.status}</span>
              </div>
              {owTaskStatus.requires_login && owTaskStatus.login_prompt && (
                <div className="mt-2 text-amber-300 leading-relaxed">
                  {owTaskStatus.login_prompt}
                </div>
              )}
              {owTaskStatus.status === 'completed' || owTaskStatus.status === 'failed' ? (
                <button
                  type="button"
                  onClick={() => setOwTaskStatus(null)}
                  className="mt-1 text-amber-500 hover:text-amber-300 transition-colors"
                >
                  Dismiss
                </button>
              ) : null}
            </div>
          )}
        </div>
      )}

      {/* Composer */}
      <ChatComposer
        onSend={handleSend}
        onStop={stopActiveResponse}
        onOpenPromptLibrary={openPromptLibrary}
        isStreaming={isStreaming}
        selectedModel={selectedModel}
        onToggleMic={toggleListening}
        isListening={isListening}
      />

      {/* Gated rather than always mounted. The component early-returns null when
          `open` is false, so an unconditional mount looked free -- but with a lazy
          import the mount itself is what fetches the chunk, so leaving it mounted
          would download the modal on first paint and undo the split. */}
      {promptModalOpen && (
        <PromptTemplateModal
          open={promptModalOpen}
          onClose={closePromptLibrary}
          onSelect={applyPromptFromLibrary}
        />
      )}
    </div>
  );
}
