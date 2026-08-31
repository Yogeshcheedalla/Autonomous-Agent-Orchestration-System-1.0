'use client';

export const PLANNER_ACTION_EVENT = 'akansha-planner-action';
export const TASKS_STORAGE_KEY = 'akansha-planner-tasks';
export const EVENTS_STORAGE_KEY = 'akansha-planner-events';
export const PLANNER_STORAGE_SYNC_EVENT = 'akansha-planner-storage-updated';

export type PlannerActionKind = 'task' | 'calendar';
export type PlannerCommandMode = 'create' | 'update' | 'delete';

export type PlannerCommand = {
  mode: PlannerCommandMode;
  kind: PlannerActionKind;
  title: string;
  date?: string;
  startTime?: string;
  endTime?: string;
  reminderEnabled?: boolean;
  reminderAt?: string;
  completed?: boolean;
};

const REMINDER_INTENT_PATTERN =
  /\b(remind me|set a reminder|reminder|remainder|notification|notify me|notify|alert|alarm|pop\s*up|popup)\b/i;

const PLANNER_PREPARATION_PATTERNS = [
  /\bi will give\b/i,
  /\bi'll give\b/i,
  /\bgive the details\b/i,
  /\bgive the list\b/i,
  /\bi will give the details\b/i,
  /\bi will give the list\b/i,
  /\bi will tell\b/i,
  /\bi'll tell\b/i,
  /\bcan you please add the details\b/i,
  /\badd the details\b/i,
  /\bdetails? i will give\b/i,
  /\bin the next (message|chat)\b/i,
  /\bnext (message|chat)\b/i,
  /\bafter this\b/i,
  /\blater\b/i,
  /\bgoing to (the )?market\b/i,
  /\bi need to buy\b/i,
  /\bi have a list\b/i,
  /\bdetails? (later|next)\b/i,
];

type PlannerTask = {
  id: string;
  title: string;
  completed: boolean;
  priority: 'high' | 'medium' | 'low';
  dueDate?: string;
  createdAt: string;
  reminderEnabled?: boolean;
  reminderAt?: string;
  notified?: boolean;
};

type PlannerEvent = {
  id: string;
  title: string;
  date: string;
  startTime: string;
  endTime: string;
  type: 'meeting' | 'reminder' | 'focus';
  reminderEnabled: boolean;
  reminderAt?: string;
  notified?: boolean;
};

export function normalizePlannerText(text: string) {
  return text
    .toLowerCase()
    .replace(/calen+d+a?r/gi, 'calendar')
    .replace(/calander/gi, 'calendar')
    .replace(/calendndar/gi, 'calendar')
    .replace(/calandar/gi, 'calendar')
    .replace(/calender/gi, 'calendar')
    .replace(/calenadar/gi, 'calendar')
    .replace(/todolist/gi, 'todo list')
    .replace(/to do list/gi, 'todo list')
    .replace(/do[-\s]?to list/gi, 'todo list')
    .replace(/\s+/g, ' ')
    .trim();
}

function to24Hour(hour: string, minute: string | undefined, period: string) {
  const numericHour = Number.parseInt(hour, 10);
  const safeMinute = (minute || '00').padStart(2, '0');
  const normalizedPeriod = period.replace(/\./g, '').toLowerCase();
  let normalizedHour = numericHour % 12;
  if (normalizedPeriod === 'pm') normalizedHour += 12;
  return `${normalizedHour.toString().padStart(2, '0')}:${safeMinute}`;
}

export function extractDateValue(text: string) {
  const lowered = normalizePlannerText(text);
  const today = new Date();
  if (lowered.includes('day after tomorrow')) {
    const next = new Date(today);
    next.setDate(today.getDate() + 2);
    return next.toISOString().slice(0, 10);
  }
  if (lowered.includes('tomorrow')) {
    const next = new Date(today);
    next.setDate(today.getDate() + 1);
    return next.toISOString().slice(0, 10);
  }
  if (lowered.includes('today')) {
    return today.toISOString().slice(0, 10);
  }

  const isoMatch = text.match(/\b(\d{4}-\d{2}-\d{2})\b/);
  return isoMatch?.[1];
}

export function extractTimeWindow(text: string) {
  const rangeMatch = text.match(
    /(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\s*(?:-|to)\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)/i
  );
  if (rangeMatch) {
    return {
      startTime: to24Hour(rangeMatch[1], rangeMatch[2], rangeMatch[3]),
      endTime: to24Hour(rangeMatch[4], rangeMatch[5], rangeMatch[6]),
    };
  }

  const singleMatch = text.match(/(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)/i);
  if (singleMatch) {
    return {
      startTime: to24Hour(singleMatch[1], singleMatch[2], singleMatch[3]),
      endTime: undefined,
    };
  }

  return {
    startTime: undefined,
    endTime: undefined,
  };
}

export function extractReminderTime(text: string) {
  const reminderMatch = text.match(
    /\b(?:remind me|reminder|remainder|notify me|notify|notification|alert|alarm|pop\s*up|popup|custom reminder(?: time)?)\b.*?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b/i
  );
  if (reminderMatch) {
    return to24Hour(reminderMatch[1], reminderMatch[2], reminderMatch[3]);
  }

  const leadingReminderMatch = text.match(
    /\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b.*?\b(?:remind me|reminder|notify me|notify|notification|alert|alarm|pop\s*up|popup)\b/i
  );
  if (leadingReminderMatch) {
    return to24Hour(leadingReminderMatch[1], leadingReminderMatch[2], leadingReminderMatch[3]);
  }

  return undefined;
}

export function addMinutes(time24: string, minutesToAdd: number) {
  const [hour = 0, minute = 0] = time24.split(':').map(Number);
  const totalMinutes = hour * 60 + minute + minutesToAdd;
  const normalized = ((totalMinutes % (24 * 60)) + 24 * 60) % (24 * 60);
  const nextHour = Math.floor(normalized / 60)
    .toString()
    .padStart(2, '0');
  const nextMinute = (normalized % 60).toString().padStart(2, '0');
  return `${nextHour}:${nextMinute}`;
}

export function formatTime12h(time24?: string) {
  if (!time24) return '';
  const [hourRaw = '09', minuteRaw = '00'] = time24.split(':');
  const hour = Number.parseInt(hourRaw, 10);
  if (Number.isNaN(hour)) return time24;
  const period = hour >= 12 ? 'PM' : 'AM';
  const displayHour = hour % 12 || 12;
  return `${displayHour}:${minuteRaw} ${period}`;
}

function cleanExtractedPlannerSubject(value: string) {
  return value
    .replace(/^[\s:,-]+/, '')
    .replace(/\b(?:please|kindly|now|just|only)\b/gi, '')
    .replace(/\b(?:set|create|add|save|put|schedule|plan|make)\b\s+(?:a|an|the)?\s*/gi, '')
    .replace(/\b(?:reminder|remainder|notification|alert|alarm|pop\s*up|popup)\b/gi, '')
    .replace(/\b(?:at|by|around|near)\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b/gi, '')
    .replace(/\b(?:for|on)\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b/gi, '')
    .replace(
      /\b(?:with|and)\s+(?:a\s+)?(?:reminder|remainder|notification|alert|alarm|pop\s*up|popup)\b.*$/gi,
      ''
    )
    .replace(/[.!?]+$/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function extractPlannerSubjectPhrase(text: string) {
  const normalized = text.replace(/\s+/g, ' ').trim();
  const subjectPatterns = [
    /\b(?:which|that|this|it)\s+(?:is|was)?\s*(?:about|regarding|for)\s+["']?(.+?)["']?\s*$/i,
    /\b(?:about|regarding)\s+["']?(.+?)["']?\s*$/i,
    /\b(?:title|name|subject)\s+(?:is|as|should\s+be|:)\s*["']?(.+?)["']?\s*$/i,
    /\b(?:remind me|notify me)\s+(?:to|about)\s+["']?(.+?)(?:\s+\b(?:at|by|around|near|on)\b\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\s*$)/i,
  ];

  for (const pattern of subjectPatterns) {
    const match = normalized.match(pattern);
    const subject = match?.[1] ? cleanExtractedPlannerSubject(match[1]) : '';
    if (subject && !isWeakPlannerTitle(subject)) return subject;
  }

  return '';
}

export function cleanPlannerTitle(text: string) {
  const explicitAlertMessage = text.match(
    /\b(?:alert|alarm|reminder|notification|pop\s*up|popup)(?:\s+message)?\s*(?:should\s+be|message\s+is|as|saying|to\s+say|:)\s*["']?(.+?)["']?\s*$/i
  );
  if (explicitAlertMessage?.[1]) {
    return explicitAlertMessage[1]
      .replace(/^[\s:,-]+/, '')
      .replace(/[.]+$/, '')
      .trim();
  }

  const extractedSubject = extractPlannerSubjectPhrase(text);
  if (extractedSubject) return extractedSubject;

  const trimmed = text
    .replace(/please\s+/gi, '')
    .replace(/\b(can you|could you|would you)\b/gi, '')
    .replace(
      /\b(?:also\s+)?(?:add\s+)?(?:the\s+)?(?:reminder|remainder|notification|alert|alarm|pop\s*up|popup|notify me|notify|remind me)\b.*$/gi,
      ''
    )
    .replace(
      /\b(add|create|save|put|schedule|plan|set|edit|update|change|modify|move|reschedule|shift|rename|mark|delete|remove|clear|cancel|erase)\b/gi,
      ''
    )
    .replace(
      /\b(notify me|notify|notification|alert|alarm|pop\s*up|popup|remind me|reminder|remainder|custom reminder(?: time)?|set a reminder)\b.*?(\d{1,2})(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b/gi,
      ''
    )
    .replace(
      /\b(\d{1,2})(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b\s*(?:-|to)\s*\b(\d{1,2})(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b/gi,
      ''
    )
    .replace(/\b(today|tomorrow|day after tomorrow)\b/gi, '')
    .replace(/\b\d{4}-\d{2}-\d{2}\b/gi, '')
    .replace(/\b\d{1,2}(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b/gi, '')
    .replace(/(this|it|that)\s+(to|into)\s+(my\s+)?(calendar|todo list|to-?do list|tasks?)/gi, '')
    .replace(/(to|into)\s+(my\s+|the\s+)?(calendar|todo list|to-?do list|tasks?)/gi, '')
    .replace(/\b(which is|that is)\s+(in|from|inside)\b/gi, '')
    .replace(
      /\b(in|from|inside)\s+(my\s+|the\s+)?(calendar|schedule|event|todo list|to-?do list|do-to list|tasks?)\b/gi,
      ''
    )
    .replace(
      /\b(remind me|set a reminder|notification|alert|alarm|pop\s*up|popup|notify me|with reminder|with a reminder)\b/gi,
      ''
    )
    .replace(/\b(calendar|schedule|event|todo list|to-?do list|do-to list|todo|tasks?)\b/gi, '')
    .replace(
      /\b(at|for|on)\s+(today|tomorrow|day after tomorrow|\d{4}-\d{2}-\d{2}|(\d{1,2})(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?))/gi,
      ''
    )
    .replace(/\b(also|the|my)\b/gi, '')
    .replace(/\b(to|into|for|at|on|with|and)\b\s*$/gi, '')
    .replace(/\s+/g, ' ')
    .trim();

  if (trimmed) return trimmed.replace(/[.,!?]+$/, '');
  if (text.toLowerCase().includes('exam')) return 'Exam';
  return 'Planner item';
}

export function isPlannerPreparationPrompt(text: string) {
  return PLANNER_PREPARATION_PATTERNS.some((pattern) => pattern.test(text));
}

export function isLikelyTaskDetails(text: string) {
  const normalized = text
    .replace(/\r/g, '\n')
    .replace(/\s*(?:-|\u2022|\u00b7|,)\s*/g, '\n')
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean);

  if (normalized.length >= 2) return true;

  return /\b\d+\s*(kg|g|gram|grams|litre|liter|ml|pcs|pieces?)\b/i.test(text);
}

export function isWeakPlannerTitle(title: string) {
  const lowered = normalizePlannerText(title);
  return (
    !lowered ||
    lowered === 'planner item' ||
    lowered === 'exam' ||
    lowered === 'also' ||
    lowered === 'yes' ||
    lowered === 'ok' ||
    lowered === 'okay' ||
    lowered.length < 5 ||
    /\b(ok|okay|nice|please|add|it|that|this|calendar|todo|to-do|task|tasks|notify|remind|reminder|remainder|alert|alarm|popup)\b/.test(
      lowered
    )
  );
}

export function isReminderOnlyPlannerFollowUp(text: string) {
  const lowered = normalizePlannerText(text);
  const { startTime } = extractTimeWindow(text);
  const reminderTime = extractReminderTime(text);
  const hasTime = Boolean(reminderTime || startTime);
  const hasReminderLanguage =
    REMINDER_INTENT_PATTERN.test(text) || /\b(yes|yeah|yep|ok|okay|also)\b/.test(lowered);
  const cleanedTitle = cleanPlannerTitle(text);

  return hasTime && hasReminderLanguage && isWeakPlannerTitle(cleanedTitle);
}

export function inferPlannerCommand(text: string): PlannerCommand | null {
  const lowered = normalizePlannerText(text);
  const wantsCalendar =
    /\b(calendar|schedule|event|remind me|reminder|remainder|notification|alert|alarm|pop up|popup)\b/.test(
      lowered
    ) || /\badd\b.*\bcalendar\b/.test(lowered);
  const wantsTask =
    /\b(todo list|to-?do list|todo|task list|tasks?|checklist)\b/.test(lowered) ||
    /\badd\b.*\btask\b/.test(lowered);
  const hasPlannerReference = wantsCalendar || wantsTask;
  const hasAction =
    /\b(add|create|save|put|schedule|plan|edit|update|change|modify|move|reschedule|shift|rename|mark|complete|done|delete|remove|clear|cancel|erase)\b/.test(
      lowered
    );
  const followUpReference =
    /\b(add|put|move|schedule|plan|edit|update|change|modify)\b/.test(lowered) &&
    /\b(it|this|that)\b/.test(lowered) &&
    /\b(my\b.*\b(calendar|todo list|task|tasks)\b|\b(calendar|todo list|task|tasks)\b)/.test(
      lowered
    );

  const hasReminderIntent = REMINDER_INTENT_PATTERN.test(lowered);
  if (!hasAction && !followUpReference && !hasReminderIntent) return null;
  if (!hasPlannerReference && !followUpReference && !hasReminderIntent) return null;

  const mode: PlannerCommandMode = /\b(delete|remove|clear|cancel|erase)\b/.test(lowered)
    ? 'delete'
    : /\b(edit|update|change|modify|move|reschedule|shift|rename|mark|complete|done)\b/.test(
          lowered
        )
      ? 'update'
      : 'create';

  const { startTime, endTime } = extractTimeWindow(text);
  const date = extractDateValue(text);
  const reminderTime = extractReminderTime(text);
  const reminderEnabled =
    /\b(remind|reminder|remainder|notify|notification|alert|alarm|pop up|popup)\b/.test(lowered) ||
    Boolean(reminderTime);
  const completed =
    /\b(mark|set)\b.*\b(done|complete|completed)\b/.test(lowered) || /\bcompleted\b/.test(lowered);

  return {
    mode,
    kind:
      wantsTask && !wantsCalendar
        ? 'task'
        : /\b(todo list|task|tasks)\b/.test(lowered) && !/\bcalendar\b/.test(lowered)
          ? 'task'
          : 'calendar',
    title: cleanPlannerTitle(text),
    date,
    startTime,
    endTime,
    reminderEnabled,
    reminderAt:
      reminderEnabled && date && (reminderTime || startTime)
        ? `${date}T${reminderTime || startTime}:00`
        : undefined,
    completed: completed || undefined,
  };
}

export function readPlannerStorage<T>(key: string, fallback: T): T {
  if (typeof window === 'undefined') return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writePlannerStorage<T>(key: string, value: T) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(key, JSON.stringify(value));
  window.dispatchEvent(new CustomEvent(PLANNER_STORAGE_SYNC_EVENT, { detail: { key } }));
}

function normalizeMatch(text: string) {
  return normalizePlannerText(text)
    .replace(/[^a-z0-9\s]/g, '')
    .trim();
}

function splitTaskItems(text: string) {
  const commandFreeText = text
    .replace(/\b(add|create|save|put|schedule|plan|set)\b/gi, '')
    .replace(/\b(to|into)\s+(my\s+)?(todo list|to-?do list|tasks?|checklist)\b/gi, '')
    .replace(/\b(today|tomorrow|day after tomorrow)\b/gi, '')
    .replace(/\b\d{4}-\d{2}-\d{2}\b/gi, '')
    .replace(
      /\b(notify me|notify|notification|alert|alarm|pop\s*up|popup|remind me|reminder|custom reminder(?: time)?|set a reminder)\b.*?(\d{1,2})(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b/gi,
      ''
    )
    .replace(/\b\d{1,2}(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b/gi, '')
    .replace(/\s+/g, ' ')
    .trim();

  const normalized = commandFreeText
    .replace(/\r/g, '\n')
    .replace(/\s*(?:-|\u2022|\u00b7)\s*/g, '\n')
    .replace(/\s*,\s*/g, '\n')
    .replace(
      /\s+(?=(?:mangoes?|tomatoes?|cucumbers?|onions?|potatoes?|apples?|bananas?)\b\s+\d)/gi,
      '\n'
    )
    .split('\n')
    .map((item) => item.trim())
    .filter(Boolean);

  return Array.from(
    new Set(
      normalized
        .map((item) =>
          item
            .replace(/^(and|also)\s+/i, '')
            .replace(/^(in|buy|get|take)\s+/i, '')
            .replace(/^(i am going to .*?\b(list|todo list|to do list)\b[:-]?\s*)/i, '')
            .replace(/^(actually\s+)/i, '')
            .replace(/[.]+$/, '')
            .trim()
        )
        .filter((item) => item.length >= 2 && !/^(buy|get|take|in)$/i.test(item))
    )
  );
}

function findMatchingTask(tasks: PlannerTask[], title: string) {
  const matchTitle = normalizeMatch(title);
  if (matchTitle) {
    const exact = tasks.find((item) => normalizeMatch(item.title) === matchTitle);
    if (exact) return exact;
    const partial = tasks.find((item) => normalizeMatch(item.title).includes(matchTitle));
    if (partial) return partial;
  }
  return tasks[0];
}

function findStrictMatchingTask(tasks: PlannerTask[], title: string) {
  const matchTitle = normalizeMatch(title);
  if (!matchTitle) return undefined;

  return (
    tasks.find((item) => normalizeMatch(item.title) === matchTitle) ||
    tasks.find((item) => normalizeMatch(item.title).includes(matchTitle)) ||
    tasks.find((item) => {
      const itemTitle = normalizeMatch(item.title);
      return Boolean(itemTitle) && matchTitle.includes(itemTitle);
    })
  );
}

function findMatchingEvent(events: PlannerEvent[], title: string) {
  const matchTitle = normalizeMatch(title);
  if (matchTitle) {
    const exact = events.find((item) => normalizeMatch(item.title) === matchTitle);
    if (exact) return exact;
    const partial = events.find((item) => normalizeMatch(item.title).includes(matchTitle));
    if (partial) return partial;
  }
  return events[events.length - 1];
}

function findStrictMatchingEvent(events: PlannerEvent[], title: string) {
  const matchTitle = normalizeMatch(title);
  if (!matchTitle) return undefined;

  return (
    events.find((item) => normalizeMatch(item.title) === matchTitle) ||
    events.find((item) => normalizeMatch(item.title).includes(matchTitle)) ||
    events.find((item) => {
      const itemTitle = normalizeMatch(item.title);
      return Boolean(itemTitle) && matchTitle.includes(itemTitle);
    })
  );
}

function getTimestampFromId(id: string) {
  const match = id.match(/-(\d{10,})/);
  return match ? Number.parseInt(match[1], 10) : 0;
}

function findLatestTask(tasks: PlannerTask[]) {
  return [...tasks].sort((a, b) => {
    const createdDelta =
      new Date(b.createdAt || 0).getTime() - new Date(a.createdAt || 0).getTime();
    if (createdDelta !== 0) return createdDelta;
    return getTimestampFromId(b.id) - getTimestampFromId(a.id);
  })[0];
}

function findLatestEvent(events: PlannerEvent[]) {
  return [...events].sort((a, b) => getTimestampFromId(b.id) - getTimestampFromId(a.id))[0];
}

function getTaskSortTime(task?: PlannerTask) {
  if (!task) return 0;
  return Math.max(new Date(task.createdAt || 0).getTime() || 0, getTimestampFromId(task.id));
}

function getEventSortTime(event?: PlannerEvent) {
  if (!event) return 0;
  return getTimestampFromId(event.id);
}

function resolveReminderFollowUpKind(
  text: string,
  preferredKind: PlannerActionKind | undefined,
  tasks: PlannerTask[],
  events: PlannerEvent[]
): PlannerActionKind {
  const wantsTask = /\b(todo|to-do|task|tasks)\b/i.test(text);
  const wantsCalendar = /\b(calendar|event|meeting|schedule)\b/i.test(text);
  if (wantsTask) return 'task';
  if (wantsCalendar) return 'calendar';
  if (preferredKind) return preferredKind;

  const latestTask = findLatestTask(tasks);
  const latestEvent = findLatestEvent(events);
  if (getTaskSortTime(latestTask) > getEventSortTime(latestEvent)) {
    return 'task';
  }
  return 'calendar';
}

export function applyPlannerReminderFollowUp(text: string, preferredKind?: PlannerActionKind) {
  const reminderTime = extractReminderTime(text) || extractTimeWindow(text).startTime;
  if (!reminderTime) {
    return {
      success: false,
      message: 'Tell me the reminder time in AM/PM format, like 2:02 PM.',
    };
  }

  const existingTasks = readPlannerStorage<PlannerTask[]>(TASKS_STORAGE_KEY, []);
  const existingEvents = readPlannerStorage<PlannerEvent[]>(EVENTS_STORAGE_KEY, []);
  const kind = resolveReminderFollowUpKind(text, preferredKind, existingTasks, existingEvents);

  if (kind === 'task') {
    const target = findLatestTask(existingTasks);
    if (!target) {
      return {
        success: false,
        message: 'I could not find a recent to-do item to attach that reminder to.',
      };
    }

    const reminderDate =
      extractDateValue(text) || target.dueDate || new Date().toISOString().slice(0, 10);
    const updated = existingTasks.map((item) =>
      item.id === target.id
        ? {
            ...item,
            dueDate: reminderDate,
            reminderEnabled: true,
            reminderAt: `${reminderDate}T${reminderTime}:00`,
            notified: false,
          }
        : item
    );
    writePlannerStorage(TASKS_STORAGE_KEY, updated);
    return {
      success: true,
      message: `Done - I updated "${target.title}" with a reminder for ${formatTime12h(reminderTime)}.`,
    };
  }

  const target = findLatestEvent(existingEvents);
  if (!target) {
    return {
      success: false,
      message: 'I could not find a recent calendar entry to attach that reminder to.',
    };
  }

  const reminderDate =
    extractDateValue(text) || target.date || new Date().toISOString().slice(0, 10);
  const updated = existingEvents.map((item) =>
    item.id === target.id
      ? {
          ...item,
          date: item.date || reminderDate,
          reminderEnabled: true,
          reminderAt: `${reminderDate}T${reminderTime}:00`,
          notified: false,
        }
      : item
  );
  writePlannerStorage(
    EVENTS_STORAGE_KEY,
    [...updated].sort(
      (a, b) =>
        new Date(`${a.date}T${a.startTime}:00`).getTime() -
        new Date(`${b.date}T${b.startTime}:00`).getTime()
    )
  );
  return {
    success: true,
    message: `Done - I updated "${target.title}" with a reminder for ${formatTime12h(reminderTime)}.`,
  };
}

export function applyPlannerCommand(command: PlannerCommand, fallbackTitle?: string) {
  const resolvedTitle = isWeakPlannerTitle(command.title)
    ? fallbackTitle?.trim() || command.title
    : command.title.trim();

  if (command.kind === 'task') {
    const existingTasks = readPlannerStorage<PlannerTask[]>(TASKS_STORAGE_KEY, []);

    if (command.mode === 'delete') {
      const target = findStrictMatchingTask(existingTasks, resolvedTitle);
      if (!target) {
        return {
          success: false,
          message: `I could not find "${resolvedTitle}" in your to-do list to delete.`,
        };
      }

      writePlannerStorage(
        TASKS_STORAGE_KEY,
        existingTasks.filter((item) => item.id !== target.id)
      );
      return {
        success: true,
        message: `Done - I deleted "${target.title}" from your to-do list.`,
      };
    }

    if (command.mode === 'update') {
      const target = findMatchingTask(existingTasks, resolvedTitle);
      if (!target) {
        return {
          success: false,
          message: 'I could not find that to-do item to edit yet.',
        };
      }

      const updated = existingTasks.map((item) =>
        item.id === target.id
          ? {
              ...item,
              title: isWeakPlannerTitle(command.title) ? item.title : resolvedTitle,
              dueDate: command.date || item.dueDate,
              reminderEnabled:
                command.reminderEnabled !== undefined
                  ? command.reminderEnabled
                  : item.reminderEnabled,
              reminderAt:
                command.reminderEnabled === false
                  ? undefined
                  : command.reminderAt || item.reminderAt,
              completed: command.completed !== undefined ? command.completed : item.completed,
              notified: false,
            }
          : item
      );
      writePlannerStorage(TASKS_STORAGE_KEY, updated);
      return {
        success: true,
        message: `Done — I updated "${target.title}" in your to-do list.`,
      };
    }

    const taskItems = splitTaskItems(resolvedTitle);
    const tasksToInsert: PlannerTask[] = (taskItems.length > 1 ? taskItems : [resolvedTitle]).map(
      (title, index) => ({
        id: `task-${Date.now()}-${index}`,
        title,
        completed: false,
        priority: 'medium',
        dueDate: command.date,
        createdAt: new Date().toISOString(),
        reminderEnabled: Boolean(command.reminderEnabled),
        reminderAt: command.reminderAt,
        notified: false,
      })
    );
    writePlannerStorage(TASKS_STORAGE_KEY, [...tasksToInsert, ...existingTasks]);
    return {
      success: true,
      message:
        tasksToInsert.length > 1
          ? `Done — I added ${tasksToInsert.length} items to your to-do list${command.reminderEnabled ? ' with the reminder time you gave.' : '.'}`
          : `Done — I added "${resolvedTitle}" to your to-do list${command.reminderEnabled ? ' with a reminder.' : '.'}`,
    };
  }

  const existingEvents = readPlannerStorage<PlannerEvent[]>(EVENTS_STORAGE_KEY, []);
  const resolvedDate = command.date || new Date().toISOString().slice(0, 10);
  const resolvedStartTime = command.startTime || '09:00';
  const resolvedEndTime = command.endTime || addMinutes(resolvedStartTime, 30);

  if (command.mode === 'delete') {
    const target = findStrictMatchingEvent(existingEvents, resolvedTitle);
    if (!target) {
      return {
        success: false,
        message: `I could not find "${resolvedTitle}" in your calendar to delete.`,
      };
    }

    writePlannerStorage(
      EVENTS_STORAGE_KEY,
      existingEvents.filter((item) => item.id !== target.id)
    );
    return {
      success: true,
      message: `Done - I deleted "${target.title}" from your calendar.`,
    };
  }

  if (command.mode === 'update') {
    const target = findMatchingEvent(existingEvents, resolvedTitle);
    if (!target) {
      return {
        success: false,
        message: 'I could not find that calendar entry to edit yet.',
      };
    }

    const updated = existingEvents.map((item) =>
      item.id === target.id
        ? {
            ...item,
            title: isWeakPlannerTitle(command.title) ? item.title : resolvedTitle,
            date: command.date || item.date,
            startTime: command.startTime || item.startTime,
            endTime: command.endTime || item.endTime,
            reminderEnabled:
              command.reminderEnabled !== undefined
                ? command.reminderEnabled
                : item.reminderEnabled,
            reminderAt:
              command.reminderEnabled === false
                ? undefined
                : command.reminderAt ||
                  (command.date || command.startTime
                    ? `${command.date || item.date}T${command.startTime || item.startTime}:00`
                    : item.reminderAt),
            notified: false,
          }
        : item
    );
    writePlannerStorage(
      EVENTS_STORAGE_KEY,
      [...updated].sort(
        (a, b) =>
          new Date(`${a.date}T${a.startTime}:00`).getTime() -
          new Date(`${b.date}T${b.startTime}:00`).getTime()
      )
    );
    return {
      success: true,
      message: `Done — I updated "${target.title}" in your calendar.`,
    };
  }

  const nextEvent: PlannerEvent = {
    id: `event-${Date.now()}`,
    title: resolvedTitle,
    date: resolvedDate,
    startTime: resolvedStartTime,
    endTime: resolvedEndTime,
    type: 'reminder',
    reminderEnabled: Boolean(command.reminderEnabled),
    reminderAt: command.reminderEnabled
      ? command.reminderAt || `${resolvedDate}T${resolvedStartTime}:00`
      : undefined,
    notified: false,
  };

  writePlannerStorage(
    EVENTS_STORAGE_KEY,
    [...existingEvents, nextEvent].sort(
      (a, b) =>
        new Date(`${a.date}T${a.startTime}:00`).getTime() -
        new Date(`${b.date}T${b.startTime}:00`).getTime()
    )
  );

  return {
    success: true,
    message: `Done — I added "${resolvedTitle}" to your calendar${command.startTime ? ` for ${formatTime12h(command.startTime)}` : ''}${command.reminderEnabled ? ' with a reminder.' : '.'}`,
  };
}
