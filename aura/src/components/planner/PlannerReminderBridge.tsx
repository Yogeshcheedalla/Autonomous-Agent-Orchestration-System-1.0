'use client';

import { useEffect, useRef, useState } from 'react';
import { apiUrl } from '@/lib/apiBase';

type PlannerTask = {
  id: string;
  title: string;
  dueDate?: string;
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
  reminderEnabled: boolean;
  reminderAt?: string;
  notified?: boolean;
};

const TASKS_STORAGE_KEY = 'akansha-planner-tasks';
const EVENTS_STORAGE_KEY = 'akansha-planner-events';
const PLANNER_STORAGE_SYNC_EVENT = 'akansha-planner-storage-updated';

/** Heartbeat gap, and the base the failure backoff doubles from. */
const SYNC_BASE_INTERVAL_MS = 15000;
/** Longest gap the backoff will grow to, so it always recovers on its own. */
const SYNC_BACKOFF_CEILING_MS = 300000;
/** Coalesces a burst of planner edits into a single POST. */
const SYNC_DEBOUNCE_MS = 800;

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

function formatTime12h(time24: string) {
  const [h, m] = time24.split(':');
  const hours = Number.parseInt(h, 10);
  const ampm = hours >= 12 ? 'PM' : 'AM';
  const h12 = hours % 12 || 12;
  return `${h12}:${m} ${ampm}`;
}

function formatEventWindow(event: PlannerEvent) {
  return `${formatTime12h(event.startTime)} - ${formatTime12h(event.endTime)}`;
}

function buildReminderSyncPayload(tasks: PlannerTask[], events: PlannerEvent[]) {
  return {
    reminders: [
      ...tasks
        .filter((task) => task.reminderEnabled && task.reminderAt && !task.notified)
        .map((task) => ({
          reminder_id: `task:${task.id}`,
          title: `Task reminder: ${task.title}`,
          body: `Due: ${task.dueDate || 'Today'}`,
          reminder_at: task.reminderAt as string,
        })),
      ...events
        .filter((event) => event.reminderEnabled && event.reminderAt && !event.notified)
        .map((event) => ({
          reminder_id: `event:${event.id}`,
          title: `Reminder: ${event.title}`,
          body: `Scheduled for ${event.date} · ${formatEventWindow(event)}`,
          reminder_at: event.reminderAt as string,
        })),
    ],
  };
}

function showBrowserNotification(title: string, body: string) {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  new Notification(title, {
    body,
    icon: '/favicon.ico',
    requireInteraction: true,
  });
}

async function sendDesktopNotification(title: string, body: string) {
  try {
    await fetch(apiUrl('/api/system/notify'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, body }),
    });
  } catch (error) {
    console.warn('[Akansha planner] recovered desktop notification failure:', error);
  }
}

export default function PlannerReminderBridge() {
  const [tasks, setTasks] = useState<PlannerTask[]>([]);
  const [events, setEvents] = useState<PlannerEvent[]>([]);
  const timerMapRef = useRef<Map<string, number>>(new Map());
  const syncBackendRef = useRef<(() => void) | null>(null);
  const lastSyncedPayloadRef = useRef<string | null>(null);
  const consecutiveFailuresRef = useRef(0);
  const retryNotBeforeRef = useRef(0);
  const inFlightRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const sync = (event?: Event) => {
      const storageEvent = event instanceof StorageEvent ? event : undefined;
      const customEvent = event instanceof CustomEvent ? event : undefined;
      const key = storageEvent?.key || customEvent?.detail?.key;
      if (key && key !== TASKS_STORAGE_KEY && key !== EVENTS_STORAGE_KEY) {
        return;
      }
      setTasks((previous) => {
        const next = readStorage<PlannerTask[]>(TASKS_STORAGE_KEY, []);
        return samePlannerPayload(previous, next) ? previous : next;
      });
      setEvents((previous) => {
        const next = readStorage<PlannerEvent[]>(EVENTS_STORAGE_KEY, []);
        return samePlannerPayload(previous, next) ? previous : next;
      });
    };

    sync();
    window.addEventListener('storage', sync);
    window.addEventListener(PLANNER_STORAGE_SYNC_EVENT, sync);
    window.addEventListener('focus', sync);
    return () => {
      window.removeEventListener('storage', sync);
      window.removeEventListener(PLANNER_STORAGE_SYNC_EVENT, sync);
      window.removeEventListener('focus', sync);
    };
  }, []);

  useEffect(() => {
    const nextTimers = new Map<string, number>();
    const now = Date.now();

    const queueNotification = (
      key: string,
      title: string,
      body: string,
      reminderAt: string,
      markNotified: () => void
    ) => {
      const triggerAt = new Date(reminderAt).getTime();
      if (Number.isNaN(triggerAt)) return;
      const delay = triggerAt - now;

      if (delay <= 0) {
        showBrowserNotification(title, body);
        void sendDesktopNotification(title, body);
        markNotified();
        return;
      }

      const timer = window.setTimeout(() => {
        showBrowserNotification(title, body);
        void sendDesktopNotification(title, body);
        markNotified();
      }, delay);
      nextTimers.set(key, timer);
    };

    tasks.forEach((task) => {
      if (!task.reminderEnabled || !task.reminderAt || task.notified) return;
      queueNotification(
        `task:${task.id}`,
        `Task reminder: ${task.title}`,
        `Due: ${task.dueDate || 'Today'}`,
        task.reminderAt,
        () => {
          const updated = readStorage<PlannerTask[]>(TASKS_STORAGE_KEY, []).map((item) =>
            item.id === task.id ? { ...item, notified: true } : item
          );
          writeStorage(TASKS_STORAGE_KEY, updated);
          setTasks(updated);
        }
      );
    });

    events.forEach((event) => {
      if (!event.reminderEnabled || !event.reminderAt || event.notified) return;
      queueNotification(
        `event:${event.id}`,
        `Reminder: ${event.title}`,
        `Scheduled for ${event.date} · ${formatEventWindow(event)}`,
        event.reminderAt,
        () => {
          const updated = readStorage<PlannerEvent[]>(EVENTS_STORAGE_KEY, []).map((item) =>
            item.id === event.id ? { ...item, notified: true } : item
          );
          writeStorage(EVENTS_STORAGE_KEY, updated);
          setEvents(updated);
        }
      );
    });

    timerMapRef.current.forEach((timer) => window.clearTimeout(timer));
    timerMapRef.current = nextTimers;

    return () => {
      nextTimers.forEach((timer) => window.clearTimeout(timer));
    };
  }, [events, tasks]);

  useEffect(() => {
    syncBackendRef.current = () => {
      const payload = buildReminderSyncPayload(tasks, events);
      const serialized = JSON.stringify(payload);

      // Nothing changed and the last attempt already landed: sending it again
      // teaches the backend nothing. The 15s heartbeat below exists to recover
      // from a *failed* sync, not to re-post an identical payload forever.
      if (serialized === lastSyncedPayloadRef.current) return;

      // A failed backend gets exponentially longer gaps instead of a fixed 15s
      // retry. Measured with the backend down: one failed POST plus one
      // console.warn every 15 seconds, indefinitely -- ~240 per hour, which is
      // the ERR_CONNECTION_REFUSED flood in the network log. Backoff turns that
      // into ~10 attempts an hour while still recovering on its own.
      const now = Date.now();
      if (now < retryNotBeforeRef.current) return;

      if (inFlightRef.current) inFlightRef.current.abort();
      const controller = new AbortController();
      inFlightRef.current = controller;

      fetch(apiUrl('/api/planner/reminders/sync'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: serialized,
        signal: controller.signal,
      })
        .then((response) => {
          if (!response.ok) throw new Error(`sync rejected with ${response.status}`);
          lastSyncedPayloadRef.current = serialized;
          consecutiveFailuresRef.current = 0;
          retryNotBeforeRef.current = 0;
        })
        .catch((error) => {
          if (controller.signal.aborted) return;

          const failures = consecutiveFailuresRef.current + 1;
          consecutiveFailuresRef.current = failures;
          retryNotBeforeRef.current =
            Date.now() + Math.min(SYNC_BACKOFF_CEILING_MS, SYNC_BASE_INTERVAL_MS * 2 ** failures);

          // Only the first failure and then every tenth: an unreachable backend
          // is one fact, not one fact per retry, and drowning the console hides
          // the errors that do need reading.
          if (failures === 1 || failures % 10 === 0) {
            console.warn(
              `[Akansha planner] reminder sync unavailable (attempt ${failures}), retrying with backoff:`,
              error
            );
          }
        })
        .finally(() => {
          if (inFlightRef.current === controller) inFlightRef.current = null;
        });
    };

    // Debounced: editing a task fires this effect on every keystroke that
    // reaches planner state, and each one used to be its own POST.
    const debounce = window.setTimeout(() => syncBackendRef.current?.(), SYNC_DEBOUNCE_MS);
    return () => window.clearTimeout(debounce);
  }, [events, tasks]);

  useEffect(() => {
    const resync = () => syncBackendRef.current?.();
    const interval = window.setInterval(resync, SYNC_BASE_INTERVAL_MS);
    window.addEventListener('focus', resync);
    document.addEventListener('visibilitychange', resync);

    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', resync);
      document.removeEventListener('visibilitychange', resync);
      inFlightRef.current?.abort();
    };
  }, []);

  return null;
}
