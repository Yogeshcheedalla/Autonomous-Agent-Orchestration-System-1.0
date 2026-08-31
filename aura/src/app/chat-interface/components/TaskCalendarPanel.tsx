'use client';

import React, { memo, useEffect, useMemo, useState } from 'react';
import {
  Bell,
  CalendarDays,
  Check,
  CheckSquare,
  Clock3,
  Pencil,
  Plus,
  Trash2,
  X,
} from 'lucide-react';
import { toast } from 'sonner';
import { apiUrl } from '@/lib/apiBase';

interface TaskItem {
  id: string;
  title: string;
  completed: boolean;
  priority: 'high' | 'medium' | 'low';
  dueDate?: string;
  createdAt: string;
  reminderEnabled?: boolean;
  reminderAt?: string;
  notified?: boolean;
}

interface CalendarEvent {
  id: string;
  title: string;
  date: string;
  startTime: string;
  endTime: string;
  type: 'meeting' | 'reminder' | 'focus';
  reminderEnabled: boolean;
  reminderAt?: string;
  notified?: boolean;
}

const TASKS_STORAGE_KEY = 'akansha-planner-tasks';
const EVENTS_STORAGE_KEY = 'akansha-planner-events';
const PLANNER_ACTION_EVENT = 'akansha-planner-action';
const PLANNER_STORAGE_SYNC_EVENT = 'akansha-planner-storage-updated';

const DEFAULT_TASKS: TaskItem[] = [];

const DEFAULT_EVENTS: CalendarEvent[] = [];

const PRIORITY_STYLES = {
  high: 'border-rose-500/25 bg-rose-500/10 text-rose-200',
  medium: 'border-amber-500/25 bg-amber-500/10 text-amber-200',
  low: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-200',
};

const EVENT_STYLES = {
  meeting: 'border-primary/25 bg-primary/10 text-[#c7b8ff]',
  reminder: 'border-sky-500/25 bg-sky-500/10 text-sky-200',
  focus: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-200',
};

function readStorage<T>(key: string, fallback: T): T {
  if (typeof window === 'undefined') return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeStorage<T>(key: string, value: T) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(key, JSON.stringify(value));
  window.dispatchEvent(new CustomEvent(PLANNER_STORAGE_SYNC_EVENT, { detail: { key } }));
}

function samePlannerPayload<T>(left: T, right: T) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function withoutLegacyPlannerSeed<T extends { id: string }>(items: T[]) {
  return items.filter(
    (item) => !['task-001', 'task-002', 'event-001', 'event-002'].includes(item.id)
  );
}

function formatTime12h(time24: string) {
  const [h, m] = time24.split(':');
  const hours = parseInt(h);
  const ampm = hours >= 12 ? 'PM' : 'AM';
  const h12 = hours % 12 || 12;
  return `${h12}:${m} ${ampm}`;
}

function formatEventWindow(event: CalendarEvent) {
  return `${formatTime12h(event.startTime)} - ${formatTime12h(event.endTime)}`;
}

function getEventStartDate(event: CalendarEvent) {
  return new Date(`${event.date}T${event.startTime}:00`);
}

function formatReminderTime(reminderAt?: string) {
  if (!reminderAt) return '';
  return new Date(reminderAt).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
  });
}

function getTodayIso() {
  return new Date().toISOString().slice(0, 10);
}

function formatPlannerDate(date?: string) {
  if (!date) return '';
  const parsed = new Date(`${date}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return date;
  return parsed.toLocaleDateString('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

function isTodayDate(date?: string) {
  return Boolean(date && date === getTodayIso());
}

function isOverdueTask(task: TaskItem) {
  return Boolean(task.dueDate && !task.completed && task.dueDate < getTodayIso());
}

function toTwelveHourParts(time24: string) {
  const [hourRaw = '09', minute = '00'] = (time24 || '09:00').split(':');
  const hour = Number.parseInt(hourRaw, 10);
  const period = hour >= 12 ? 'PM' : 'AM';
  const displayHour = hour % 12 || 12;
  return {
    hour: displayHour.toString(),
    minute,
    period,
  };
}

function fromTwelveHourParts(hour: string, minute: string, period: string) {
  const parsedHour = Number.parseInt(hour, 10);
  const normalizedHour = Number.isNaN(parsedHour) ? 9 : Math.min(Math.max(parsedHour, 1), 12);
  let hour24 = normalizedHour % 12;
  if (period === 'PM') hour24 += 12;
  return `${hour24.toString().padStart(2, '0')}:${minute.padStart(2, '0')}`;
}

function buildIsoDate(date: string, time24: string) {
  return `${date}T${time24}:00`;
}

function extractStoredTime(reminderAt?: string, fallback = '09:00') {
  if (!reminderAt) return fallback;
  const match = reminderAt.match(/T(\d{2}:\d{2})/);
  return match?.[1] || fallback;
}

function getNextQuarterHourTime() {
  const now = new Date();
  const minutes = now.getMinutes();
  const roundedMinutes = Math.ceil((minutes + 1) / 5) * 5;
  now.setMinutes(roundedMinutes, 0, 0);
  const hh = now.getHours().toString().padStart(2, '0');
  const mm = now.getMinutes().toString().padStart(2, '0');
  return `${hh}:${mm}`;
}

function addMinutes(time24: string, minutesToAdd: number) {
  const [hour, minute] = time24.split(':').map(Number);
  const totalMinutes = hour * 60 + minute + minutesToAdd;
  const normalized = ((totalMinutes % (24 * 60)) + 24 * 60) % (24 * 60);
  const nextHour = Math.floor(normalized / 60)
    .toString()
    .padStart(2, '0');
  const nextMinute = (normalized % 60).toString().padStart(2, '0');
  return `${nextHour}:${nextMinute}`;
}

function TimePicker({
  value,
  onChange,
  className = '',
}: {
  value: string;
  onChange: (nextValue: string) => void;
  className?: string;
}) {
  const parts = useMemo(() => toTwelveHourParts(value), [value]);
  const hours = Array.from({ length: 12 }, (_, index) => `${index + 1}`);
  const minutes = Array.from({ length: 60 }, (_, index) => `${index}`.padStart(2, '0'));

  const update = (patch: Partial<typeof parts>) => {
    const next = { ...parts, ...patch };
    onChange(fromTwelveHourParts(next.hour, next.minute, next.period));
  };

  return (
    <div className={`grid grid-cols-[1fr_1fr_1fr] gap-2 ${className}`}>
      <select
        value={parts.hour}
        onChange={(event) => update({ hour: event.target.value })}
        className="rounded-2xl border border-white/10 bg-[#020617]/80 px-3 py-3 text-sm text-white outline-none focus:border-[#8B6CFF]/50"
      >
        {hours.map((hour) => (
          <option key={hour} value={hour}>
            {hour}
          </option>
        ))}
      </select>
      <select
        value={parts.minute}
        onChange={(event) => update({ minute: event.target.value })}
        className="rounded-2xl border border-white/10 bg-[#020617]/80 px-3 py-3 text-sm text-white outline-none focus:border-[#8B6CFF]/50"
      >
        {minutes.map((minute) => (
          <option key={minute} value={minute}>
            {minute}
          </option>
        ))}
      </select>
      <select
        value={parts.period}
        onChange={(event) => update({ period: event.target.value })}
        className="rounded-2xl border border-white/10 bg-[#020617]/80 px-3 py-3 text-sm text-white outline-none focus:border-[#8B6CFF]/50"
      >
        <option value="AM">AM</option>
        <option value="PM">PM</option>
      </select>
    </div>
  );
}

function showPlannerNotification(title: string, body: string) {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  new Notification(title, {
    body,
    icon: '/favicon.ico',
    requireInteraction: true,
  });
}

async function sendDesktopPlannerNotification(title: string, body: string) {
  try {
    await fetch(apiUrl('/api/system/notify'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, body }),
    });
  } catch (error) {
    console.warn('Failed to send desktop notification:', error);
  }
}

async function sendPlannerTestAlert() {
  showPlannerNotification('Akansha planner test', 'Browser reminder channel is active.');
  await sendDesktopPlannerNotification(
    'Akansha planner test',
    'Desktop reminder channel is active.'
  );
}

/**
 * The planner: tasks and calendar, in a tab.
 *
 * Wrapped in `memo` and it takes no props, so this is the strongest form of the
 * optimisation — with no props there is nothing to compare, and the component
 * re-renders only when its own state changes.
 *
 * That matters because of where it is mounted. `ContextPanel` sits inside
 * `ChatWorkspace`, which re-renders on every stats update — and `contextUnits` is
 * `ceil(characters / 4)`, so a streaming reply pushes a new value roughly every
 * four characters typed by the model. Without this, a 1000-character answer
 * re-ran this 1055-line body and its eight `useMemo` comparisons a couple of
 * hundred times while the user watched text appear, for a panel whose contents
 * had not changed at all.
 *
 * The eight `useMemo`s inside are not a substitute for this: they skip
 * recomputing derived lists, but the function body, the hook bookkeeping and the
 * element tree still run on every parent render. `memo` is what stops the call.
 */
function TaskCalendarPanel() {
  const [activeTab, setActiveTab] = useState<'tasks' | 'calendar'>('tasks');
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [plannerLoaded, setPlannerLoaded] = useState(false);
  const [showTaskForm, setShowTaskForm] = useState(false);
  const [showEventForm, setShowEventForm] = useState(false);
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null);
  const [editingEventId, setEditingEventId] = useState<string | null>(null);
  const [taskTitle, setTaskTitle] = useState('');
  const [taskPriority, setTaskPriority] = useState<TaskItem['priority']>('medium');
  const [taskDate, setTaskDate] = useState('');
  const [taskReminder, setTaskReminder] = useState(false);
  const [taskReminderTime, setTaskReminderTime] = useState('09:00');
  const [eventTitle, setEventTitle] = useState('');
  const [eventType, setEventType] = useState<CalendarEvent['type']>('meeting');
  const [eventDate, setEventDate] = useState('');
  const [eventStartTime, setEventStartTime] = useState('09:00');
  const [eventEndTime, setEventEndTime] = useState('10:00');
  const [eventReminder, setEventReminder] = useState(true);
  const [eventReminderTime, setEventReminderTime] = useState('08:45');

  useEffect(() => {
    setTasks(withoutLegacyPlannerSeed(readStorage(TASKS_STORAGE_KEY, DEFAULT_TASKS)));
    setEvents(withoutLegacyPlannerSeed(readStorage(EVENTS_STORAGE_KEY, DEFAULT_EVENTS)));
    setPlannerLoaded(true);
  }, []);

  useEffect(() => {
    if (!plannerLoaded) return;
    writeStorage(TASKS_STORAGE_KEY, tasks);
  }, [plannerLoaded, tasks]);

  useEffect(() => {
    if (!plannerLoaded) return;
    writeStorage(EVENTS_STORAGE_KEY, events);
  }, [events, plannerLoaded]);

  useEffect(() => {
    const syncPlannerState = (event?: Event) => {
      const storageEvent = event instanceof StorageEvent ? event : undefined;
      const customEvent = event instanceof CustomEvent ? event : undefined;
      const key = storageEvent?.key || customEvent?.detail?.key;
      if (key && key !== TASKS_STORAGE_KEY && key !== EVENTS_STORAGE_KEY) {
        return;
      }

      setTasks((previous) => {
        const next = withoutLegacyPlannerSeed(readStorage(TASKS_STORAGE_KEY, DEFAULT_TASKS));
        return samePlannerPayload(previous, next) ? previous : next;
      });
      setEvents((previous) => {
        const next = withoutLegacyPlannerSeed(readStorage(EVENTS_STORAGE_KEY, DEFAULT_EVENTS));
        return samePlannerPayload(previous, next) ? previous : next;
      });
    };

    window.addEventListener('storage', syncPlannerState);
    window.addEventListener(PLANNER_STORAGE_SYNC_EVENT, syncPlannerState);
    return () => {
      window.removeEventListener('storage', syncPlannerState);
      window.removeEventListener(PLANNER_STORAGE_SYNC_EVENT, syncPlannerState);
    };
  }, []);

  useEffect(() => {
    const handlePlannerAction = (event: Event) => {
      const customEvent = event as CustomEvent<{
        type: 'task' | 'calendar';
        title: string;
        dueDate?: string;
        date?: string;
        startTime?: string;
        endTime?: string;
        reminderEnabled?: boolean;
        reminderAt?: string;
      }>;
      const detail = customEvent.detail;
      if (!detail?.title?.trim()) return;

      if (detail.type === 'task') {
        const nextTask: TaskItem = {
          id: `task-${Date.now()}`,
          title: detail.title.trim(),
          completed: false,
          priority: 'medium',
          dueDate: detail.dueDate,
          createdAt: new Date().toISOString(),
          reminderEnabled: Boolean(detail.reminderEnabled),
          reminderAt: detail.reminderAt,
          notified: false,
        };
        setActiveTab('tasks');
        setTasks((previous) => {
          const next = [nextTask, ...previous];
          writeStorage(TASKS_STORAGE_KEY, next);
          return next;
        });
        toast.success('Added to your to-do planner');
        return;
      }

      const nextEvent: CalendarEvent = {
        id: `event-${Date.now()}`,
        title: detail.title.trim(),
        date: detail.date || new Date().toISOString().slice(0, 10),
        startTime: detail.startTime || getNextQuarterHourTime(),
        endTime: detail.endTime || addMinutes(detail.startTime || getNextQuarterHourTime(), 30),
        type: 'reminder',
        reminderEnabled: Boolean(detail.reminderEnabled),
        reminderAt: detail.reminderAt,
        notified: false,
      };
      setActiveTab('calendar');
      setEvents((previous) => {
        const next = [...previous, nextEvent].sort(
          (a, b) => getEventStartDate(a).getTime() - getEventStartDate(b).getTime()
        );
        writeStorage(EVENTS_STORAGE_KEY, next);
        return next;
      });
      toast.success('Added to your calendar planner');
    };

    window.addEventListener(PLANNER_ACTION_EVENT, handlePlannerAction as EventListener);
    return () =>
      window.removeEventListener(PLANNER_ACTION_EVENT, handlePlannerAction as EventListener);
  }, []);

  const requestBrowserNotificationPermission = async () => {
    if (typeof Notification === 'undefined') {
      toast.info('Browser notifications are unavailable here. Desktop alerts will still be used.');
      return true;
    }

    if (Notification.permission === 'granted') return true;
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      toast.info(
        'Browser notifications were not granted. Desktop alerts will still try to appear.'
      );
      return true;
    }
    toast.success('Notifications enabled for planner reminders');
    return true;
  };

  const resetTaskForm = () => {
    setEditingTaskId(null);
    setTaskTitle('');
    setTaskPriority('medium');
    setTaskDate('');
    setTaskReminder(false);
    setTaskReminderTime('09:00');
    setShowTaskForm(false);
  };

  const resetEventForm = () => {
    setEditingEventId(null);
    setEventTitle('');
    setEventDate('');
    setEventStartTime('09:00');
    setEventEndTime('10:00');
    setEventType('meeting');
    setEventReminder(true);
    setEventReminderTime('08:45');
    setShowEventForm(false);
  };

  const editTask = (task: TaskItem) => {
    setEditingTaskId(task.id);
    setTaskTitle(task.title);
    setTaskPriority(task.priority);
    setTaskDate(task.dueDate || '');
    setTaskReminder(Boolean(task.reminderEnabled));
    setTaskReminderTime(task.reminderAt ? extractStoredTime(task.reminderAt, '09:00') : '09:00');
    setShowTaskForm(true);
  };

  const editEvent = (event: CalendarEvent) => {
    setEditingEventId(event.id);
    setEventTitle(event.title);
    setEventDate(event.date);
    setEventStartTime(event.startTime);
    setEventEndTime(event.endTime);
    setEventType(event.type);
    setEventReminder(event.reminderEnabled);
    setEventReminderTime(
      event.reminderAt
        ? extractStoredTime(event.reminderAt, addMinutes(event.startTime, -15))
        : addMinutes(event.startTime, -15)
    );
    setShowEventForm(true);
  };

  const addTask = async () => {
    if (!taskTitle.trim()) return;

    const nextTask: TaskItem = {
      id: editingTaskId || `task-${Date.now()}`,
      title: taskTitle.trim(),
      completed: false,
      priority: taskPriority,
      dueDate: taskDate || undefined,
      createdAt: new Date().toISOString(),
      reminderEnabled: taskReminder,
      reminderAt: taskReminder && taskDate ? buildIsoDate(taskDate, taskReminderTime) : undefined,
      notified: false,
    };

    setTasks((previous) => {
      const next = editingTaskId
        ? previous.map((item) => (item.id === editingTaskId ? { ...item, ...nextTask } : item))
        : [nextTask, ...previous];
      writeStorage(TASKS_STORAGE_KEY, next);
      return next;
    });
    resetTaskForm();
    toast.success(editingTaskId ? 'Task updated' : 'Task added to your planner');
  };

  const addEvent = async () => {
    if (!eventTitle.trim()) return;
    if (!eventDate || !eventStartTime || !eventEndTime) {
      toast.error('Calendar events need a date, start time, and end time.');
      return;
    }

    if (eventEndTime <= eventStartTime) {
      toast.error('End time must be later than start time.');
      return;
    }

    const nextEvent: CalendarEvent = {
      id: editingEventId || `event-${Date.now()}`,
      title: eventTitle.trim(),
      date: eventDate,
      startTime: eventStartTime,
      endTime: eventEndTime,
      type: eventType,
      reminderEnabled: eventReminder,
      reminderAt:
        eventReminder && eventDate ? buildIsoDate(eventDate, eventReminderTime) : undefined,
      notified: false,
    };

    setEvents((previous) => {
      const next = [...previous.filter((item) => item.id !== editingEventId), nextEvent].sort(
        (a, b) => getEventStartDate(a).getTime() - getEventStartDate(b).getTime()
      );
      writeStorage(EVENTS_STORAGE_KEY, next);
      return next;
    });
    resetEventForm();
    toast.success(editingEventId ? 'Calendar reminder updated' : 'Calendar event scheduled');
  };

  const pendingTasks = useMemo(() => tasks.filter((task) => !task.completed).length, [tasks]);
  const doneTasks = useMemo(() => tasks.filter((task) => task.completed).length, [tasks]);
  const overdueTasks = useMemo(() => tasks.filter(isOverdueTask).length, [tasks]);
  const dueTodayTasks = useMemo(
    () => tasks.filter((task) => !task.completed && isTodayDate(task.dueDate)).length,
    [tasks]
  );
  const upcomingEvents = useMemo(
    () =>
      [...events].sort((a, b) => getEventStartDate(a).getTime() - getEventStartDate(b).getTime()),
    [events]
  );
  const todayEvents = useMemo(
    () => upcomingEvents.filter((event) => isTodayDate(event.date)).length,
    [upcomingEvents]
  );
  const nextEvent = upcomingEvents[0];

  return (
    <div className="flex h-full flex-col text-white">
      <div className="border-b border-white/10 bg-[#050b14]/80 px-4 py-4 sm:px-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-xs font-medium uppercase tracking-[0.18em] text-slate-500">
              Planner cockpit
            </p>
            <p className="mt-1 text-sm text-slate-300">
              Tasks, reminders, alarms, and exact calendar windows.
            </p>
          </div>
          <div className="grid w-full grid-cols-2 gap-2 sm:w-auto">
            {[
              { key: 'tasks', label: 'To-do', icon: CheckSquare, badge: pendingTasks },
              {
                key: 'calendar',
                label: 'Calendar',
                icon: CalendarDays,
                badge: upcomingEvents.length,
              },
            ].map(({ key, label, icon: Icon, badge }) => (
              <button
                type="button"
                key={key}
                onClick={() => setActiveTab(key as 'tasks' | 'calendar')}
                className={`flex items-center justify-center gap-2 rounded-2xl border px-4 py-3 text-sm font-semibold transition ${
                  activeTab === key
                    ? 'border-[#8B6CFF]/45 bg-primary/18 text-white shadow-[0_12px_28px_rgba(108,71,255,0.18)]'
                    : 'border-white/10 bg-white/[0.045] text-slate-300 hover:bg-white/[0.075]'
                }`}
              >
                <Icon size={15} />
                {label}
                <span className="rounded-full bg-white/10 px-2 py-0.5 text-[11px] font-mono tabular-nums text-slate-200">
                  {badge}
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-5 sm:px-5">
        {activeTab === 'tasks' && (
          <div className="space-y-5">
            <div className="grid gap-3 md:grid-cols-4">
              <div className="rounded-3xl border border-primary/20 bg-primary/12 p-4">
                <p className="text-3xl font-semibold text-[#c7b8ff]">{pendingTasks}</p>
                <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">Pending</p>
              </div>
              <div className="rounded-3xl border border-emerald-500/20 bg-emerald-500/10 p-4">
                <p className="text-3xl font-semibold text-emerald-200">{doneTasks}</p>
                <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">Completed</p>
              </div>
              <div className="rounded-3xl border border-amber-500/20 bg-amber-500/10 p-4">
                <p className="text-3xl font-semibold text-amber-200">{dueTodayTasks}</p>
                <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">Due today</p>
              </div>
              <div className="rounded-3xl border border-rose-500/20 bg-rose-500/10 p-4">
                <p className="text-3xl font-semibold text-rose-200">{overdueTasks}</p>
                <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">Overdue</p>
              </div>
            </div>

            {showTaskForm ? (
              <div className="rounded-[28px] border border-[#8B6CFF]/25 bg-primary/10 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
                <div className="space-y-3">
                  <input
                    value={taskTitle}
                    onChange={(event) => setTaskTitle(event.target.value)}
                    placeholder="What do you need to get done?"
                    className="w-full rounded-2xl border border-white/10 bg-[#020617]/80 px-4 py-3 text-sm text-white outline-none placeholder:text-slate-500 focus:border-[#8B6CFF]/60"
                  />

                  <div className="grid gap-3 sm:grid-cols-2">
                    <select
                      value={taskPriority}
                      onChange={(event) =>
                        setTaskPriority(event.target.value as TaskItem['priority'])
                      }
                      className="rounded-2xl border border-white/10 bg-[#020617]/80 px-3 py-3 text-sm text-white outline-none focus:border-[#8B6CFF]/50"
                    >
                      <option value="high">High priority</option>
                      <option value="medium">Medium priority</option>
                      <option value="low">Low priority</option>
                    </select>
                    <input
                      type="date"
                      value={taskDate}
                      onChange={(event) => setTaskDate(event.target.value)}
                      className="rounded-2xl border border-white/10 bg-[#020617]/80 px-3 py-3 text-sm text-white outline-none focus:border-[#8B6CFF]/50"
                    />
                  </div>

                  <div
                    onClick={async () => {
                      if (!taskReminder) {
                        await requestBrowserNotificationPermission();
                        setTaskReminder(true);
                        if (!taskReminderTime) setTaskReminderTime('09:00');
                      } else {
                        setTaskReminder(false);
                      }
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-2xl border border-white/10 bg-[#020617]/70 px-3 py-3 text-sm text-white transition hover:bg-white/[0.05]"
                  >
                    Reminder notification
                    <div
                      className={`relative h-6 w-11 flex-shrink-0 cursor-pointer rounded-full transition-colors duration-200 ease-in-out focus:outline-none ${
                        taskReminder ? 'bg-emerald-500' : 'bg-muted'
                      }`}
                    >
                      <span
                        className={`pointer-events-none absolute left-1 top-1 h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                          taskReminder ? 'translate-x-5' : 'translate-x-0'
                        }`}
                      />
                    </div>
                  </div>

                  {taskReminder && taskDate && (
                    <label className="block">
                      <span className="mb-2 block text-xs uppercase tracking-[0.18em] text-muted-foreground">
                        Custom reminder time
                      </span>
                      <TimePicker value={taskReminderTime} onChange={setTaskReminderTime} />
                    </label>
                  )}

                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={addTask}
                      className="inline-flex items-center gap-2 rounded-2xl bg-primary px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-primary-hover"
                    >
                      <Plus size={14} />
                      {editingTaskId ? 'Update task' : 'Save task'}
                    </button>
                    <button
                      type="button"
                      onClick={resetTaskForm}
                      className="rounded-2xl border border-white/10 px-4 py-2.5 text-sm text-slate-300 hover:bg-white/10"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setShowTaskForm(true)}
                className="flex w-full items-center justify-center gap-2 rounded-3xl border border-dashed border-[#8B6CFF]/35 bg-primary/10 px-4 py-4 text-sm font-semibold text-[#c7b8ff] transition hover:border-[#8B6CFF]/60 hover:bg-primary/16"
              >
                <Plus size={14} />
                Add to-do
              </button>
            )}

            <div className="space-y-2">
              {tasks.length ? (
                tasks.map((task) => (
                  <div
                    key={task.id}
                    className={`group rounded-3xl border p-4 transition ${
                      task.completed
                        ? 'border-white/5 bg-white/[0.035] opacity-70'
                        : 'border-white/10 bg-white/[0.06] hover:-translate-y-0.5 hover:border-[#8B6CFF]/35 hover:bg-white/[0.085]'
                    }`}
                  >
                    <div className="flex items-start gap-3">
                      <button
                        type="button"
                        onClick={() =>
                          setTasks((previous) => {
                            const next = previous.map((item) =>
                              item.id === task.id ? { ...item, completed: !item.completed } : item
                            );
                            writeStorage(TASKS_STORAGE_KEY, next);
                            return next;
                          })
                        }
                        className={`mt-0.5 flex h-5 w-5 items-center justify-center rounded-full border transition-colors ${
                          task.completed
                            ? 'border-emerald-500 bg-emerald-500 text-white'
                            : 'border-white/20 hover:border-[#8B6CFF]'
                        }`}
                      >
                        {task.completed && <Check size={11} />}
                      </button>

                      <div className="min-w-0 flex-1">
                        <p
                          className={`text-sm font-medium leading-6 ${
                            task.completed
                              ? 'text-muted-foreground line-through'
                              : 'text-foreground'
                          }`}
                        >
                          {task.title}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          <span
                            className={`rounded-full border px-2.5 py-1 text-[11px] font-medium uppercase tracking-[0.18em] ${
                              PRIORITY_STYLES[task.priority]
                            }`}
                          >
                            {task.priority}
                          </span>
                          {task.dueDate && (
                            <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2.5 py-1 text-[11px] text-muted-foreground">
                              <Clock3 size={11} />
                              {formatPlannerDate(task.dueDate)}
                            </span>
                          )}
                          {task.reminderEnabled && (
                            <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] text-amber-200">
                              <Bell size={11} />
                              {task.reminderAt
                                ? `Notify ${formatReminderTime(task.reminderAt)}`
                                : 'Notify'}
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            editTask(task);
                          }}
                          className="rounded-xl p-2 text-slate-400 hover:bg-white/10 hover:text-white"
                        >
                          <Pencil size={14} />
                        </button>
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            setTasks((previous) => {
                              const next = previous.filter((item) => item.id !== task.id);
                              writeStorage(TASKS_STORAGE_KEY, next);
                              return next;
                            });
                            toast.success('Task removed');
                          }}
                          className="rounded-xl p-2 text-slate-400 hover:bg-white/10 hover:text-red-300"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  </div>
                ))
              ) : (
                <div className="rounded-3xl border border-dashed border-white/10 bg-white/[0.035] px-4 py-12 text-center">
                  <CheckSquare size={28} className="mx-auto text-slate-500" />
                  <p className="mt-3 text-sm font-semibold text-slate-300">No to-do items yet.</p>
                  <p className="mt-1 text-xs text-slate-500">
                    Create one here or ask Akansha in chat to add it.
                  </p>
                </div>
              )}
            </div>
          </div>
        )}

        {activeTab === 'calendar' && (
          <div className="space-y-5">
            <div className="grid gap-3 md:grid-cols-[1fr_1fr_2fr]">
              <div className="rounded-3xl border border-sky-500/20 bg-sky-500/10 p-4">
                <p className="text-3xl font-semibold text-sky-200">{upcomingEvents.length}</p>
                <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">Scheduled</p>
              </div>
              <div className="rounded-3xl border border-emerald-500/20 bg-emerald-500/10 p-4">
                <p className="text-3xl font-semibold text-emerald-200">{todayEvents}</p>
                <p className="mt-1 text-xs uppercase tracking-[0.18em] text-slate-500">Today</p>
              </div>
              <div className="rounded-3xl border border-white/10 bg-white/[0.055] p-4">
                <p className="text-xs uppercase tracking-[0.18em] text-slate-500">Next window</p>
                <p className="mt-2 truncate text-sm font-semibold text-white">
                  {nextEvent ? nextEvent.title : 'No calendar slots yet'}
                </p>
                <p className="mt-1 text-xs text-slate-400">
                  {nextEvent
                    ? `${formatPlannerDate(nextEvent.date)} - ${formatEventWindow(nextEvent)}`
                    : 'Add an event or ask Akansha to schedule one.'}
                </p>
              </div>
            </div>

            <div className="rounded-3xl border border-sky-500/20 bg-gradient-to-br from-sky-500/10 via-primary/8 to-transparent p-4">
              <div className="flex items-center gap-2">
                <Bell size={15} className="text-sky-300" />
                <p className="text-sm font-medium text-white">Reminder channel</p>
                <button
                  type="button"
                  onClick={() => {
                    void sendPlannerTestAlert();
                    toast.success('Test alert sent');
                  }}
                  className="ml-auto inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.06] px-3 py-1.5 text-xs font-medium text-white hover:bg-white/[0.1]"
                >
                  <Bell size={12} />
                  Send test alert
                </button>
              </div>
              <p className="mt-2 text-sm leading-6 text-slate-300">
                Calendar events use a proper <span className="text-white">start time</span> and{' '}
                <span className="text-white">end time</span>. Notifications trigger before the start
                time without changing your to-do section.
              </p>
            </div>

            {showEventForm ? (
              <div className="rounded-[28px] border border-sky-500/20 bg-sky-500/10 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]">
                <div className="space-y-3">
                  <input
                    value={eventTitle}
                    onChange={(event) => setEventTitle(event.target.value)}
                    placeholder="Calendar event title"
                    className="w-full rounded-2xl border border-white/10 bg-[#020617]/80 px-4 py-3 text-sm text-white outline-none placeholder:text-slate-500 focus:border-sky-400/50"
                  />

                  <div className="grid gap-3 xl:grid-cols-[0.8fr_1fr_1fr]">
                    <input
                      type="date"
                      value={eventDate}
                      onChange={(event) => setEventDate(event.target.value)}
                      className="rounded-2xl border border-white/10 bg-[#020617]/80 px-3 py-3 text-sm text-white outline-none focus:border-sky-400/50"
                    />
                    <TimePicker value={eventStartTime} onChange={setEventStartTime} />
                    <TimePicker value={eventEndTime} onChange={setEventEndTime} />
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <select
                      value={eventType}
                      onChange={(event) =>
                        setEventType(event.target.value as CalendarEvent['type'])
                      }
                      className="rounded-2xl border border-white/10 bg-[#020617]/80 px-3 py-3 text-sm text-white outline-none focus:border-sky-400/50"
                    >
                      <option value="meeting">Meeting</option>
                      <option value="reminder">Reminder</option>
                      <option value="focus">Focus block</option>
                    </select>

                    <div
                      onClick={async () => {
                        if (!eventReminder) {
                          await requestBrowserNotificationPermission();
                          setEventReminder(true);
                          if (!eventReminderTime && eventStartTime) {
                            const [h, m] = eventStartTime.split(':').map(Number);
                            const reminderMins = (h * 60 + m - 15 + 24 * 60) % (24 * 60);
                            const reminderH = Math.floor(reminderMins / 60);
                            const reminderM = reminderMins % 60;
                            setEventReminderTime(
                              `${reminderH.toString().padStart(2, '0')}:${reminderM.toString().padStart(2, '0')}`
                            );
                          }
                        } else {
                          setEventReminder(false);
                        }
                      }}
                      className="flex cursor-pointer items-center justify-between rounded-2xl border border-white/10 bg-[#020617]/70 px-3 py-3 text-sm text-white transition hover:bg-white/[0.05]"
                    >
                      Notify me
                      <div
                        className={`relative h-6 w-11 flex-shrink-0 cursor-pointer rounded-full transition-colors duration-200 ease-in-out focus:outline-none ${
                          eventReminder ? 'bg-emerald-500' : 'bg-muted'
                        }`}
                      >
                        <span
                          className={`pointer-events-none absolute left-1 top-1 h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                            eventReminder ? 'translate-x-5' : 'translate-x-0'
                          }`}
                        />
                      </div>
                    </div>
                  </div>

                  {eventReminder && eventDate && (
                    <label className="block">
                      <span className="mb-2 block text-xs uppercase tracking-[0.18em] text-muted-foreground">
                        Custom reminder time
                      </span>
                      <TimePicker value={eventReminderTime} onChange={setEventReminderTime} />
                    </label>
                  )}

                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={addEvent}
                      className="inline-flex items-center gap-2 rounded-2xl bg-sky-400 px-4 py-2.5 text-sm font-semibold text-slate-950 transition-colors hover:bg-sky-300"
                    >
                      <Plus size={14} />
                      {editingEventId ? 'Update event' : 'Save event'}
                    </button>
                    <button
                      type="button"
                      onClick={resetEventForm}
                      className="rounded-2xl border border-white/10 px-4 py-2.5 text-sm text-slate-300 hover:bg-white/10"
                    >
                      <X size={14} className="inline" />
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setShowEventForm(true)}
                className="flex w-full items-center justify-center gap-2 rounded-3xl border border-dashed border-sky-400/35 bg-sky-500/10 px-4 py-4 text-sm font-semibold text-sky-200 transition hover:border-sky-400/60 hover:bg-sky-500/16"
              >
                <Plus size={14} />
                Add calendar slot
              </button>
            )}

            <div className="space-y-2">
              {upcomingEvents.length ? (
                upcomingEvents.map((event) => (
                  <div
                    key={event.id}
                    className="group rounded-3xl border border-white/10 bg-white/[0.06] p-4 transition hover:-translate-y-0.5 hover:border-sky-400/35 hover:bg-white/[0.085]"
                  >
                    <div className="flex items-start gap-3">
                      <div
                        className={`rounded-2xl border px-3 py-2 text-[11px] font-medium uppercase tracking-[0.2em] ${
                          EVENT_STYLES[event.type]
                        }`}
                      >
                        {event.type}
                      </div>

                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-semibold text-white">{event.title}</p>
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2.5 py-1 text-[11px] text-muted-foreground">
                            <CalendarDays size={11} />
                            {formatPlannerDate(event.date)}
                          </span>
                          <span className="inline-flex items-center gap-1 rounded-full bg-slate-900/70 px-2.5 py-1 text-[11px] text-slate-200">
                            <Clock3 size={11} />
                            {formatEventWindow(event)}
                          </span>
                          {event.reminderEnabled && (
                            <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] text-amber-200">
                              <Bell size={11} />
                              {event.reminderAt
                                ? `Notify ${formatReminderTime(event.reminderAt)}`
                                : 'Notification'}
                            </span>
                          )}
                        </div>
                      </div>

                      <div className="flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                        <button
                          type="button"
                          onClick={(clickEvent) => {
                            clickEvent.stopPropagation();
                            editEvent(event);
                          }}
                          className="rounded-xl p-2 text-slate-400 hover:bg-white/10 hover:text-white"
                        >
                          <Pencil size={14} />
                        </button>
                        <button
                          type="button"
                          onClick={(clickEvent) => {
                            clickEvent.stopPropagation();
                            setEvents((previous) => {
                              const next = previous.filter((item) => item.id !== event.id);
                              writeStorage(EVENTS_STORAGE_KEY, next);
                              return next;
                            });
                            toast.success('Calendar event removed');
                          }}
                          className="rounded-xl p-2 text-slate-400 hover:bg-white/10 hover:text-red-300"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  </div>
                ))
              ) : (
                <div className="rounded-3xl border border-dashed border-white/10 bg-white/[0.035] px-4 py-12 text-center">
                  <CalendarDays size={28} className="mx-auto text-slate-500" />
                  <p className="mt-3 text-sm font-semibold text-slate-300">
                    No calendar slots yet.
                  </p>
                  <p className="mt-1 text-xs text-slate-500">
                    Add a time window here or ask Akansha to schedule one.
                  </p>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * The `displayName` is for the React DevTools profiler: a bare `memo(fn)` shows
 * up as "Anonymous", which makes the one thing this wrapper exists to let you
 * verify -- that it stopped re-rendering -- the hardest thing to find.
 */
TaskCalendarPanel.displayName = 'TaskCalendarPanel';

export default memo(TaskCalendarPanel);
