'use client';

import React from 'react';
import { Bell, CalendarDays, CheckSquare } from 'lucide-react';
import TaskCalendarPanel from '@/app/chat-interface/components/TaskCalendarPanel';

export function PlannerServiceView() {
  return (
    <section className="relative overflow-hidden rounded-[32px] border border-white/10 bg-[#07111f]/95 text-white shadow-[0_28px_90px_rgba(0,0,0,0.38)]">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_20%_0%,rgba(108,71,255,0.22),transparent_32%),radial-gradient(circle_at_88%_12%,rgba(0,201,167,0.14),transparent_28%)]" />
      <div className="relative flex flex-wrap items-start justify-between gap-4 border-b border-white/10 px-6 py-6">
        <div className="max-w-4xl">
          <p className="inline-flex items-center gap-2 rounded-full border border-primary/25 bg-primary/12 px-3 py-1 text-xs font-medium uppercase tracking-[0.18em] text-[#c7b8ff]">
            <Bell size={13} />
            Scheduling service
          </p>
          <h1 className="mt-4 text-3xl font-semibold tracking-tight text-white sm:text-4xl">
            Planner, alarms, to-do lists, and calendar timing in one calm workspace.
          </h1>
          <p className="mt-4 max-w-3xl text-sm leading-6 text-slate-300">
            Keep planning separate from live voice when you want focus. Tasks stay simple, reminders
            keep their alert time, and calendar entries preserve exact start and end times.
          </p>
        </div>

        <div className="grid gap-2 sm:grid-cols-3">
          <div className="rounded-2xl border border-sky-400/20 bg-sky-400/10 px-3 py-2 text-xs text-sky-100">
            <Bell size={13} className="mr-1 inline" />
            Alerts
          </div>
          <div className="rounded-2xl border border-white/10 bg-white/[0.06] px-3 py-2 text-xs text-slate-200">
            <CalendarDays size={13} className="mr-1 inline" />
            Calendar
          </div>
          <div className="rounded-2xl border border-white/10 bg-white/[0.06] px-3 py-2 text-xs text-slate-200">
            <CheckSquare size={13} className="mr-1 inline" />
            To-do
          </div>
        </div>
      </div>

      <div className="relative min-h-[720px] bg-[#020617]/45">
        <TaskCalendarPanel />
      </div>
    </section>
  );
}
