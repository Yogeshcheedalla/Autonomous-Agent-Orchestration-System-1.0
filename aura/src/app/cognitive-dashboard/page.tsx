'use client';

import React, { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  Bot,
  Brain,
  CheckCircle2,
  Clock,
  Gauge,
  GitCompare,
  Network,
  Radar,
  ShieldCheck,
  ShoppingBag,
  TicketCheck,
  TrendingUp,
  Wrench,
} from 'lucide-react';
import { API_BASE_URL } from '@/lib/apiBase';

type DashboardMetrics = Record<string, number>;

type ActionPlatformSnapshot = {
  autonomous_shopping?: Record<string, unknown>;
  autonomous_booking?: Record<string, unknown>;
  execution_bus?: Record<string, unknown>;
  verification?: Record<string, unknown>;
  dashboard_metrics?: DashboardMetrics;
  created_at?: string;
};

type ObservatorySnapshot = {
  active_goals?: Array<Record<string, unknown>>;
  active_agents?: Array<Record<string, unknown>>;
  skills_triggered?: Array<Record<string, unknown>>;
  memory_usage?: Record<string, unknown>;
  system_health?: Record<string, unknown>;
  token_usage?: Record<string, unknown>;
  learning_progress?: Record<string, unknown>;
  action_platform?: ActionPlatformSnapshot;
  universal_execution?: {
    recent?: Array<Record<string, unknown>>;
    pending_collaboration?: Array<Record<string, unknown>>;
    recovery_actions?: Array<Record<string, unknown>>;
    proactive_events?: Array<Record<string, unknown>>;
    automation_plans?: Array<Record<string, unknown>>;
    cognitive_health?: Record<string, unknown>;
  };
  digital_twin?: {
    profile?: Record<string, unknown>;
    future_predictions?: Array<Record<string, unknown>>;
    risk_heatmaps?: Array<unknown>;
    goal_forecasts?: Array<Record<string, unknown>>;
    decision_comparisons?: Array<unknown>;
    timeline_projections?: Array<Record<string, unknown>>;
    behavior_trends?: Array<Record<string, unknown>>;
    recommendations?: Array<Record<string, unknown>>;
  };
  created_at?: string;
};

type LoadState = {
  loading: boolean;
  error: string;
  observatory: ObservatorySnapshot | null;
  action: ActionPlatformSnapshot | null;
};

const API_BASE = API_BASE_URL;

function asNumber(value: unknown, fallback = 0) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function percent(value: unknown) {
  return `${Math.round(asNumber(value) * 100)}%`;
}

function integer(value: unknown) {
  return Math.round(asNumber(value)).toLocaleString('en-IN');
}

function MetricCard({
  label,
  value,
  caption,
  icon: Icon,
}: {
  label: string;
  value: string;
  caption: string;
  icon: React.ComponentType<{ size?: number; className?: string }>;
}) {
  return (
    <section className="rounded-lg border border-white/10 bg-[#101522]/90 p-4 shadow-[0_18px_60px_rgba(0,0,0,0.25)]">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-[0.22em] text-slate-500">{label}</p>
          <p className="mt-2 text-2xl font-semibold text-white">{value}</p>
        </div>
        <span className="flex h-10 w-10 items-center justify-center rounded-md bg-primary/15 text-[#8f73ff]">
          <Icon size={19} />
        </span>
      </div>
      <p className="mt-3 text-sm leading-5 text-slate-400">{caption}</p>
    </section>
  );
}

function JsonPreview({ title, data }: { title: string; data: unknown }) {
  return (
    <section className="rounded-lg border border-white/10 bg-[#0b0f1a] p-4">
      <h2 className="text-sm font-semibold text-white">{title}</h2>
      <pre className="mt-3 max-h-72 overflow-auto rounded-md border border-white/10 bg-black/40 p-3 text-xs leading-5 text-slate-300">
        {JSON.stringify(data ?? {}, null, 2)}
      </pre>
    </section>
  );
}

export default function CognitiveDashboardPage() {
  const [state, setState] = useState<LoadState>({
    loading: true,
    error: '',
    observatory: null,
    action: null,
  });

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setState((previous) => ({ ...previous, loading: true, error: '' }));
      try {
        const [observatoryResponse, actionResponse] = await Promise.all([
          fetch(`${API_BASE}/api/cognitive/observatory/snapshot`, { cache: 'no-store' }),
          fetch(`${API_BASE}/api/cognitive/action-platform/metrics`, { cache: 'no-store' }),
        ]);
        if (!observatoryResponse.ok || !actionResponse.ok) {
          throw new Error('Hermes dashboard API is not reachable');
        }
        const observatory = (await observatoryResponse.json()) as ObservatorySnapshot;
        const action = (await actionResponse.json()) as ActionPlatformSnapshot;
        if (!cancelled) {
          setState({ loading: false, error: '', observatory, action });
        }
      } catch (error) {
        if (!cancelled) {
          setState({
            loading: false,
            error: error instanceof Error ? error.message : 'Unable to load cognitive dashboard',
            observatory: null,
            action: null,
          });
        }
      }
    }
    void load();
    const timer = window.setInterval(load, 30000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const action = state.action ?? state.observatory?.action_platform ?? {};
  const healthScore = state.observatory?.system_health?.score;
  const contextLoad = state.observatory?.token_usage?.estimated_current_tokens;
  const learningScore = state.observatory?.learning_progress?.score;
  const universal = state.observatory?.universal_execution ?? {};
  const twin = state.observatory?.digital_twin ?? {};
  const cognitiveHealth = universal.cognitive_health?.payload as
    | Record<string, unknown>
    | undefined;
  const twinProfile = twin.profile ?? {};
  const latestPrediction = twin.future_predictions?.[0] ?? {};
  const latestBestScenario = latestPrediction.best_scenario as Record<string, unknown> | undefined;

  // `dashboard_metrics` is read inside the callback rather than above it. Hoisting
  // it as `action.dashboard_metrics ?? {}` produced a fresh object literal on every
  // render whenever the backend had not sent metrics yet, so the dependency was
  // never referentially equal and this memo recomputed every render -- it looked
  // like caching while doing none. The dependency is now the state-owned reference
  // (or a stable `undefined`).
  const rawMetrics = action.dashboard_metrics;
  const metricRows = useMemo(() => {
    const metrics = rawMetrics ?? {};
    return [
      [
        'Shopping success',
        percent(metrics.shopping_success_rate),
        'Verified shopping plans ready for approval',
      ],
      [
        'Booking accuracy',
        percent(metrics.booking_accuracy),
        'Booking flows that passed verification gates',
      ],
      [
        'Average savings',
        percent(metrics.average_savings),
        'Estimated savings from ranked comparisons',
      ],
      [
        'Task completion',
        percent(metrics.task_completion_rate),
        'Action platform plans reaching approval-ready state',
      ],
      [
        'Failure rate',
        percent(metrics.failure_rate),
        'Verification audits with blocker-level conflicts',
      ],
      [
        'Recommendation confidence',
        percent(metrics.recommendation_confidence),
        'Combined recommendation confidence',
      ],
    ];
  }, [rawMetrics]);

  return (
    <main className="min-h-screen bg-[#070b16] px-5 py-6 text-slate-200 md:px-8">
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <header className="rounded-lg border border-white/10 bg-[#0e1321] p-5">
          <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.28em] text-[#9aa8ff]">
                Akansha Cognitive Observatory
              </p>
              <h1 className="mt-2 text-2xl font-semibold text-white">Autonomous Action Platform</h1>
              <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-400">
                Commerce, booking, verification, life automation, concierge planning, execution bus,
                safety, memory, agents, and learning metrics in one governed view.
              </p>
            </div>
            <div className="rounded-md border border-emerald-400/20 bg-emerald-400/10 px-3 py-2 text-sm text-emerald-200">
              {state.loading ? 'Syncing Hermes' : state.error ? 'Backend offline' : 'Live snapshot'}
            </div>
          </div>
        </header>

        {state.error ? (
          <section className="rounded-lg border border-red-400/20 bg-red-500/10 p-4 text-sm text-red-100">
            {state.error}
          </section>
        ) : null}

        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <MetricCard
            label="System Health"
            value={percent(healthScore)}
            caption="Overall health from goals, failures, tests, and action checks."
            icon={ShieldCheck}
          />
          <MetricCard
            label="Learning"
            value={percent(learningScore)}
            caption="Stable skills, useful memory, and low active failure pressure."
            icon={Brain}
          />
          <MetricCard
            label="Context Load"
            value={integer(contextLoad)}
            caption="Estimated dashboard context footprint."
            icon={Gauge}
          />
          <MetricCard
            label="Active Agents"
            value={integer(state.observatory?.active_agents?.length)}
            caption="Persistent Hermes agents currently registered."
            icon={Bot}
          />
          <MetricCard
            label="Execution Health"
            value={percent(cognitiveHealth?.system_health)}
            caption="Cognitive health across memory, tools, latency, learning, and hallucination signals."
            icon={Network}
          />
          <MetricCard
            label="Blocked Questions"
            value={integer(universal.pending_collaboration?.length)}
            caption="Places where Akansha paused instead of guessing."
            icon={AlertTriangle}
          />
          <MetricCard
            label="Recovery Plans"
            value={integer(universal.recovery_actions?.length)}
            caption="Self-healing plans for blocked or failed workflows."
            icon={Wrench}
          />
          <MetricCard
            label="Proactive Events"
            value={integer(universal.proactive_events?.length)}
            caption="Risks, deadline alerts, and workflow suggestions detected early."
            icon={Activity}
          />
          <MetricCard
            label="Twin Confidence"
            value={percent(twinProfile.confidence)}
            caption="How much useful personal pattern evidence is available."
            icon={Radar}
          />
          <MetricCard
            label="Future Simulations"
            value={integer(twin.future_predictions?.length)}
            caption="Recent outcome simulations from the digital twin."
            icon={TrendingUp}
          />
          <MetricCard
            label="Decision Rank"
            value={percent(latestBestScenario?.decision_rank)}
            caption="Best path strength from the latest future simulation."
            icon={GitCompare}
          />
          <MetricCard
            label="Timeline Pressure"
            value={percent(
              (latestPrediction.timeline_projection as Record<string, unknown> | undefined)
                ?.relative_estimate
            )}
            caption="Projected pacing pressure for the current future path."
            icon={Clock}
          />
        </section>

        <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {metricRows.map(([label, value, caption]) => (
            <MetricCard
              key={label}
              label={label}
              value={value}
              caption={caption}
              icon={
                label.includes('Booking')
                  ? TicketCheck
                  : label.includes('Shopping')
                    ? ShoppingBag
                    : CheckCircle2
              }
            />
          ))}
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
          <section className="rounded-lg border border-white/10 bg-[#101522]/90 p-4">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-sm font-semibold text-white">Action Platform Metrics</h2>
              <Activity size={18} className="text-[#8f73ff]" />
            </div>
            <div className="mt-4 overflow-hidden rounded-lg border border-white/10">
              <table className="w-full table-fixed border-collapse text-sm">
                <thead className="bg-white/5 text-left text-xs uppercase tracking-[0.16em] text-slate-500">
                  <tr>
                    <th className="px-3 py-3">Metric</th>
                    <th className="px-3 py-3">Value</th>
                    <th className="px-3 py-3">Meaning</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/10">
                  {metricRows.map(([label, value, caption]) => (
                    <tr key={label} className="text-slate-300">
                      <td className="px-3 py-3 font-medium text-white">{label}</td>
                      <td className="px-3 py-3 text-[#9aa8ff]">{value}</td>
                      <td className="px-3 py-3 text-slate-400">{caption}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="rounded-lg border border-white/10 bg-[#101522]/90 p-4">
            <h2 className="text-sm font-semibold text-white">Safety Contract</h2>
            <ul className="mt-4 space-y-3 text-sm leading-6 text-slate-400">
              <li>
                Purchases, payments, bookings, account changes, and private-data sharing stay
                blocked until explicit owner approval.
              </li>
              <li>
                Verification checks compare price changes, availability, duplicates, schedule
                conflicts, expired links, and assumptions.
              </li>
              <li>
                Execution bus plans service routing, authentication scope, monitoring, rollback
                strategy, and learning feedback before any action.
              </li>
            </ul>
          </section>
        </section>

        <section className="grid gap-4 xl:grid-cols-2">
          <JsonPreview title="Commerce Snapshot" data={action.autonomous_shopping} />
          <JsonPreview title="Booking Snapshot" data={action.autonomous_booking} />
          <JsonPreview title="Execution Bus" data={action.execution_bus} />
          <JsonPreview title="Verification" data={action.verification} />
          <JsonPreview title="Universal Execution" data={universal.recent} />
          <JsonPreview title="Pending Collaboration" data={universal.pending_collaboration} />
          <JsonPreview title="Self Healing" data={universal.recovery_actions} />
          <JsonPreview title="Automation Plans" data={universal.automation_plans} />
          <JsonPreview title="Digital Twin Profile" data={twin.profile} />
          <JsonPreview title="Future Predictions" data={twin.future_predictions} />
          <JsonPreview title="Risk Heatmaps" data={twin.risk_heatmaps} />
          <JsonPreview title="Decision Comparisons" data={twin.decision_comparisons} />
          <JsonPreview title="Timeline Projections" data={twin.timeline_projections} />
          <JsonPreview title="Predictive Recommendations" data={twin.recommendations} />
        </section>
      </div>
    </main>
  );
}
