'use client';

import React, { useCallback, useEffect, useState } from 'react';
import {
  Zap,
  Play,
  Pause,
  Plus,
  Trash2,
  RefreshCw,
  Clock,
  Webhook,
  Activity,
  CheckCircle2,
  XCircle,
  Layers,
  Sparkles,
  Search,
  Code2,
  Terminal,
  ChevronRight,
  Filter,
  Loader2,
} from 'lucide-react';
import { toast } from 'sonner';
import { apiUrl } from '@/lib/apiBase';

export interface TaskAutomation {
  id: number;
  name: string;
  description: string;
  trigger_type: 'schedule' | 'webhook' | 'event' | 'manual';
  trigger_config: {
    interval_minutes?: number;
    cron?: string;
    webhook_key?: string;
    source?: string;
  };
  action_type:
    | 'github_decompose'
    | 'daily_report'
    | 'desktop_action'
    | 'ai_workflow'
    | 'custom_agent';
  action_payload: {
    prompt?: string;
    repo?: string;
    channel?: string;
    action?: string;
    target?: string;
  };
  status: 'active' | 'paused' | 'running' | 'error';
  last_run_at: string | null;
  next_run_at: string | null;
  run_count: number;
  created_at: string;
}

export interface AutomationLog {
  id: number;
  automation_id: number;
  automation_name: string;
  trigger_source: string;
  status: 'success' | 'failed' | 'running';
  output_summary: string;
  details: Record<string, unknown>;
  started_at: string;
  completed_at: string | null;
}

export interface AutomationTemplate {
  template_id: string;
  name: string;
  description: string;
  trigger_type: 'schedule' | 'webhook' | 'event' | 'manual';
  // Borrowed from TaskAutomation rather than re-declared. A template and a saved
  // automation carry the same two config bags from the same backend catalog, but
  // this interface used to type them `Record<string, any>` -- so `tpl.action_payload
  // .prompt` was `any` here and a checked `string | undefined` two interfaces up,
  // and a renamed key would only ever be caught on one of the two paths.
  trigger_config: TaskAutomation['trigger_config'];
  action_type: string;
  action_payload: TaskAutomation['action_payload'];
}

export default function TaskAutomationStudio() {
  const [automations, setAutomations] = useState<TaskAutomation[]>([]);
  const [templates, setTemplates] = useState<AutomationTemplate[]>([]);
  const [logs, setLogs] = useState<AutomationLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterStatus, setFilterStatus] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState('');

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editingAutomation, setEditingAutomation] = useState<TaskAutomation | null>(null);

  // Form states
  const [formName, setFormName] = useState('');
  const [formDescription, setFormDescription] = useState('');
  const [formTriggerType, setFormTriggerType] = useState<
    'schedule' | 'webhook' | 'event' | 'manual'
  >('schedule');
  const [formIntervalMinutes, setFormIntervalMinutes] = useState(60);
  const [formActionType, setFormActionType] =
    useState<TaskAutomation['action_type']>('ai_workflow');
  const [formPrompt, setFormPrompt] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const fetchAutomations = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch('/api/automations');
      if (res.ok) {
        const data = await res.json();
        setAutomations(data.automations || []);
        setTemplates(data.templates || []);
      }
    } catch (err) {
      console.error('Failed to load task automations:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchLogs = useCallback(async () => {
    try {
      const res = await fetch('/api/automations/logs/all');
      if (res.ok) {
        const data = await res.json();
        setLogs(data.logs || []);
      }
    } catch (err) {
      console.error('Failed to load automation logs:', err);
    }
  }, []);

  useEffect(() => {
    fetchAutomations();
    fetchLogs();
  }, [fetchAutomations, fetchLogs]);

  const handleCreateOrUpdate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formName.trim()) {
      toast.error('Please enter an automation name.');
      return;
    }
    setSubmitting(true);
    try {
      const payload = {
        name: formName.trim(),
        description: formDescription.trim(),
        trigger_type: formTriggerType,
        trigger_config:
          formTriggerType === 'schedule'
            ? { interval_minutes: formIntervalMinutes }
            : { source: 'webhook' },
        action_type: formActionType,
        action_payload: { prompt: formPrompt.trim() },
        status: 'active',
      };

      const url = editingAutomation
        ? `/api/automations/${editingAutomation.id}`
        : '/api/automations';
      const method = editingAutomation ? 'PUT' : 'POST';

      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        toast.success(
          editingAutomation
            ? `Automation '${formName}' updated successfully.`
            : `Automation '${formName}' created!`
        );
        setShowCreateModal(false);
        resetForm();
        fetchAutomations();
      } else {
        toast.error('Failed to save task automation.');
      }
    } catch {
      toast.error('Error connecting to backend.');
    } finally {
      setSubmitting(false);
    }
  };

  const resetForm = () => {
    setFormName('');
    setFormDescription('');
    setFormTriggerType('schedule');
    setFormIntervalMinutes(60);
    setFormActionType('ai_workflow');
    setFormPrompt('');
    setEditingAutomation(null);
  };

  const handleOpenEdit = (auto: TaskAutomation) => {
    setEditingAutomation(auto);
    setFormName(auto.name);
    setFormDescription(auto.description || '');
    setFormTriggerType(auto.trigger_type);
    setFormIntervalMinutes(auto.trigger_config.interval_minutes || 60);
    setFormActionType(auto.action_type);
    setFormPrompt(auto.action_payload.prompt || '');
    setShowCreateModal(true);
  };

  const handleUseTemplate = (tpl: AutomationTemplate) => {
    setEditingAutomation(null);
    setFormName(tpl.name);
    setFormDescription(tpl.description);
    setFormTriggerType(tpl.trigger_type);
    setFormIntervalMinutes(tpl.trigger_config.interval_minutes || 60);
    setFormActionType(tpl.action_type as TaskAutomation['action_type']);
    setFormPrompt(tpl.action_payload.prompt || '');
    setShowCreateModal(true);
  };

  const handleToggleStatus = async (auto: TaskAutomation) => {
    try {
      const res = await fetch(`/api/automations/${auto.id}/toggle`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        toast.info(`Automation '${auto.name}' is now ${data.status}`);
        fetchAutomations();
      }
    } catch {
      toast.error('Failed to toggle status.');
    }
  };

  const handleTriggerNow = async (auto: TaskAutomation) => {
    toast.loading(`Triggering '${auto.name}'...`, { id: `trig-${auto.id}` });
    try {
      const res = await fetch(`/api/automations/${auto.id}/trigger`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        if (data.success) {
          toast.success(`Success: ${data.output_summary}`, { id: `trig-${auto.id}` });
        } else {
          toast.error(`Execution failed: ${data.error || 'Unknown error'}`, {
            id: `trig-${auto.id}`,
          });
        }
        fetchAutomations();
        fetchLogs();
      }
    } catch {
      toast.error('Failed to trigger execution.', { id: `trig-${auto.id}` });
    }
  };

  const handleDelete = async (auto: TaskAutomation) => {
    if (!confirm(`Are you sure you want to delete '${auto.name}'?`)) return;
    try {
      const res = await fetch(`/api/automations/${auto.id}`, { method: 'DELETE' });
      if (res.ok) {
        toast.success(`Automation '${auto.name}' deleted.`);
        fetchAutomations();
        fetchLogs();
      }
    } catch {
      toast.error('Failed to delete automation.');
    }
  };

  // Metrics
  const activeCount = automations.filter((a) => a.status === 'active').length;
  const totalRuns = automations.reduce((acc, a) => acc + (a.run_count || 0), 0);
  const successLogsCount = logs.filter((l) => l.status === 'success').length;
  const successRate = logs.length > 0 ? Math.round((successLogsCount / logs.length) * 100) : 100;

  const filteredAutomations = automations.filter((a) => {
    const matchesStatus = filterStatus === 'all' || a.status === filterStatus;
    const matchesSearch =
      a.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (a.description || '').toLowerCase().includes(searchQuery.toLowerCase());
    return matchesStatus && matchesSearch;
  });

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-6 md:p-10 font-sans space-y-8">
      {/* Header Banner */}
      <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 border-b border-slate-800/80 pb-6">
        <div>
          <div className="flex items-center gap-3">
            <div className="p-2.5 rounded-xl bg-gradient-to-tr from-cyan-500/20 to-blue-600/30 text-cyan-400 border border-cyan-500/30">
              <Zap className="w-6 h-6 animate-pulse" />
            </div>
            <div>
              <h1 className="text-2xl md:text-3xl font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-white via-slate-200 to-cyan-400">
                Task Automations Studio
              </h1>
              <p className="text-xs md:text-sm text-slate-400 mt-0.5">
                Always-on control center for self-hosted coding agents, schedules & webhook
                triggers.
              </p>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3 w-full md:w-auto">
          <button
            onClick={() => {
              fetchAutomations();
              fetchLogs();
            }}
            className="p-2.5 rounded-lg border border-slate-800 bg-slate-900/80 hover:bg-slate-800 text-slate-300 transition-colors"
            title="Refresh automations"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={() => {
              resetForm();
              setShowCreateModal(true);
            }}
            className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-gradient-to-r from-cyan-500 to-blue-600 hover:from-cyan-400 hover:to-blue-500 text-white font-medium text-sm shadow-lg shadow-cyan-500/20 transition-all"
          >
            <Plus className="w-4 h-4" />
            <span>Create Automation</span>
          </button>
        </div>
      </div>

      {/* Metrics Summary Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="p-5 rounded-2xl bg-slate-900/60 border border-slate-800/80 backdrop-blur-sm space-y-2">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold tracking-wider uppercase">
              Total Automations
            </span>
            <Layers className="w-4 h-4 text-cyan-400" />
          </div>
          <div className="text-3xl font-bold text-white">{automations.length}</div>
          <div className="text-xs text-slate-400">{activeCount} active workflows</div>
        </div>

        <div className="p-5 rounded-2xl bg-slate-900/60 border border-slate-800/80 backdrop-blur-sm space-y-2">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold tracking-wider uppercase">
              Scheduled Triggers
            </span>
            <Clock className="w-4 h-4 text-blue-400" />
          </div>
          <div className="text-3xl font-bold text-white">
            {automations.filter((a) => a.trigger_type === 'schedule').length}
          </div>
          <div className="text-xs text-slate-400">Running background runner loop</div>
        </div>

        <div className="p-5 rounded-2xl bg-slate-900/60 border border-slate-800/80 backdrop-blur-sm space-y-2">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold tracking-wider uppercase">Total Executions</span>
            <Activity className="w-4 h-4 text-purple-400" />
          </div>
          <div className="text-3xl font-bold text-white">{totalRuns}</div>
          <div className="text-xs text-slate-400">{logs.length} audit logs stored</div>
        </div>

        <div className="p-5 rounded-2xl bg-slate-900/60 border border-slate-800/80 backdrop-blur-sm space-y-2">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold tracking-wider uppercase">Success Rate</span>
            <CheckCircle2 className="w-4 h-4 text-emerald-400" />
          </div>
          <div className="text-3xl font-bold text-emerald-400">{successRate}%</div>
          <div className="text-xs text-slate-400">{successLogsCount} successful runs</div>
        </div>
      </div>

      {/* Preset Templates Quick Start */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-cyan-400" />
          <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">
            Quick-Start Templates
          </h2>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {templates.map((tpl) => (
            <div
              key={tpl.template_id}
              className="p-4 rounded-xl bg-slate-900/40 hover:bg-slate-900/80 border border-slate-800/80 hover:border-cyan-500/40 transition-all cursor-pointer group flex flex-col justify-between"
              onClick={() => handleUseTemplate(tpl)}
            >
              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <span className="font-semibold text-sm text-slate-200 group-hover:text-cyan-300 transition-colors">
                    {tpl.name}
                  </span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-slate-800 text-slate-400 font-mono">
                    {tpl.trigger_type}
                  </span>
                </div>
                <p className="text-xs text-slate-400 line-clamp-2">{tpl.description}</p>
              </div>
              <div className="flex items-center justify-between mt-4 pt-3 border-t border-slate-800/60 text-xs text-cyan-400 font-medium">
                <span>Use template</span>
                <ChevronRight className="w-4 h-4 group-hover:translate-x-1 transition-transform" />
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Main Content Area: Automations List & Filters */}
      <div className="space-y-4">
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-4">
          <div className="relative flex-1 max-w-md">
            <Search className="w-4 h-4 absolute left-3.5 top-3 text-slate-500" />
            <input
              type="text"
              placeholder="Search automations..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-2 rounded-xl bg-slate-900/80 border border-slate-800 text-sm text-slate-200 focus:outline-none focus:border-cyan-500/50"
            />
          </div>

          <div className="flex items-center gap-2">
            <Filter className="w-4 h-4 text-slate-400" />
            {['all', 'active', 'paused', 'error'].map((st) => (
              <button
                key={st}
                onClick={() => setFilterStatus(st)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium capitalize transition-colors ${
                  filterStatus === st
                    ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                    : 'bg-slate-900/60 text-slate-400 border border-slate-800 hover:text-slate-200'
                }`}
              >
                {st}
              </button>
            ))}
          </div>
        </div>

        {/* Automations Cards */}
        {filteredAutomations.length === 0 ? (
          <div className="p-12 rounded-2xl bg-slate-900/30 border border-slate-800/60 text-center space-y-3">
            <div className="p-3 rounded-full bg-slate-800/50 text-slate-400 w-fit mx-auto">
              <Zap className="w-6 h-6" />
            </div>
            <h3 className="text-base font-semibold text-slate-300">No automations found</h3>
            <p className="text-xs text-slate-400 max-w-sm mx-auto">
              Create your first scheduled or webhook automation to start executing tasks
              autonomously.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4">
            {filteredAutomations.map((auto) => {
              const isWebhook = auto.trigger_type === 'webhook';
              const webhookUrl = isWebhook
                ? apiUrl(`/api/webhooks/${auto.trigger_config.webhook_key || 'wh_default'}`)
                : null;

              return (
                <div
                  key={auto.id}
                  className="p-5 rounded-2xl bg-slate-900/60 border border-slate-800/80 hover:border-slate-700/80 transition-all flex flex-col md:flex-row md:items-center justify-between gap-4"
                >
                  <div className="space-y-2 flex-1">
                    <div className="flex items-center gap-3">
                      <h3 className="font-semibold text-base text-slate-100">{auto.name}</h3>
                      <span
                        className={`text-[11px] font-medium px-2.5 py-0.5 rounded-full border capitalize ${
                          auto.status === 'active'
                            ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                            : auto.status === 'paused'
                              ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
                              : 'bg-rose-500/10 text-rose-400 border-rose-500/20'
                        }`}
                      >
                        {auto.status}
                      </span>
                      <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-400">
                        {auto.action_type}
                      </span>
                    </div>

                    {auto.description && (
                      <p className="text-xs text-slate-400 line-clamp-1">{auto.description}</p>
                    )}

                    <div className="flex flex-wrap items-center gap-4 text-xs text-slate-400 pt-1">
                      <div className="flex items-center gap-1.5">
                        {isWebhook ? (
                          <Webhook className="w-3.5 h-3.5 text-cyan-400" />
                        ) : (
                          <Clock className="w-3.5 h-3.5 text-blue-400" />
                        )}
                        <span>
                          {isWebhook
                            ? 'Webhook Ingress'
                            : `Every ${auto.trigger_config.interval_minutes || 60}m`}
                        </span>
                      </div>

                      {auto.last_run_at && (
                        <div>
                          Last run:{' '}
                          {new Date(auto.last_run_at).toLocaleTimeString([], {
                            hour: '2-digit',
                            minute: '2-digit',
                          })}
                        </div>
                      )}

                      <div>Runs: {auto.run_count}</div>

                      {webhookUrl && (
                        <div className="font-mono text-[10px] text-cyan-400 bg-slate-950 px-2 py-0.5 rounded border border-cyan-500/20 truncate max-w-xs">
                          {webhookUrl}
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-2 pt-3 md:pt-0 border-t md:border-t-0 border-slate-800">
                    <button
                      onClick={() => handleTriggerNow(auto)}
                      className="px-3 py-1.5 rounded-lg bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 text-xs font-medium flex items-center gap-1.5 transition-colors"
                    >
                      <Play className="w-3.5 h-3.5" />
                      <span>Run Now</span>
                    </button>

                    <button
                      onClick={() => handleToggleStatus(auto)}
                      className="p-2 rounded-lg bg-slate-800/60 hover:bg-slate-800 text-slate-300 text-xs transition-colors"
                      title={auto.status === 'active' ? 'Pause automation' : 'Resume automation'}
                    >
                      {auto.status === 'active' ? (
                        <Pause className="w-3.5 h-3.5" />
                      ) : (
                        <Play className="w-3.5 h-3.5 text-emerald-400" />
                      )}
                    </button>

                    <button
                      onClick={() => handleOpenEdit(auto)}
                      className="p-2 rounded-lg bg-slate-800/60 hover:bg-slate-800 text-slate-300 text-xs transition-colors"
                      title="Edit automation"
                    >
                      <Code2 className="w-3.5 h-3.5" />
                    </button>

                    <button
                      onClick={() => handleDelete(auto)}
                      className="p-2 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/20 text-xs transition-colors"
                      title="Delete automation"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Global Audit Logs View */}
      <div className="space-y-4 pt-6 border-t border-slate-800/80">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Terminal className="w-4 h-4 text-purple-400" />
            <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wider">
              Recent Execution Logs
            </h2>
          </div>
          <span className="text-xs text-slate-500">{logs.length} latest entries</span>
        </div>

        <div className="rounded-2xl bg-slate-900/60 border border-slate-800/80 overflow-hidden">
          <div className="divide-y divide-slate-800/60 font-mono text-xs">
            {logs.slice(0, 8).map((log) => (
              <div
                key={log.id}
                className="p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-2 hover:bg-slate-800/30"
              >
                <div className="flex items-center gap-2.5 overflow-hidden">
                  {log.status === 'success' ? (
                    <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
                  ) : log.status === 'failed' ? (
                    <XCircle className="w-4 h-4 text-rose-400 shrink-0" />
                  ) : (
                    <Loader2 className="w-4 h-4 text-cyan-400 animate-spin shrink-0" />
                  )}
                  <span className="font-semibold text-slate-200">{log.automation_name}</span>
                  <span className="text-slate-500 text-[10px]">[{log.trigger_source}]</span>
                  <span className="text-slate-400 truncate">{log.output_summary}</span>
                </div>
                <div className="text-[10px] text-slate-500 shrink-0">
                  {new Date(log.started_at).toLocaleString()}
                </div>
              </div>
            ))}
            {logs.length === 0 && (
              <div className="p-6 text-center text-slate-500 text-xs font-sans">
                No execution logs recorded yet.
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Create / Edit Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="w-full max-w-lg rounded-2xl bg-slate-900 border border-slate-800 shadow-2xl p-6 space-y-6">
            <div className="flex items-center justify-between border-b border-slate-800 pb-4">
              <h3 className="text-lg font-bold text-slate-100">
                {editingAutomation ? 'Edit Automation' : 'Create Task Automation'}
              </h3>
              <button
                onClick={() => setShowCreateModal(false)}
                className="text-slate-400 hover:text-slate-200 text-sm"
              >
                ✕
              </button>
            </div>

            <form onSubmit={handleCreateOrUpdate} className="space-y-4 text-sm">
              <div className="space-y-1">
                <label className="text-xs font-semibold text-slate-300">Automation Name</label>
                <input
                  type="text"
                  required
                  placeholder="e.g. GitHub Issue Decomposer"
                  value={formName}
                  onChange={(e) => setFormName(e.target.value)}
                  className="w-full px-3.5 py-2 rounded-xl bg-slate-950 border border-slate-800 text-slate-200 focus:outline-none focus:border-cyan-500"
                />
              </div>

              <div className="space-y-1">
                <label className="text-xs font-semibold text-slate-300">Description</label>
                <input
                  type="text"
                  placeholder="Brief workflow summary..."
                  value={formDescription}
                  onChange={(e) => setFormDescription(e.target.value)}
                  className="w-full px-3.5 py-2 rounded-xl bg-slate-950 border border-slate-800 text-slate-200 focus:outline-none focus:border-cyan-500"
                />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <label className="text-xs font-semibold text-slate-300">Trigger Type</label>
                  <select
                    value={formTriggerType}
                    onChange={(e) =>
                      setFormTriggerType(e.target.value as TaskAutomation['trigger_type'])
                    }
                    className="w-full px-3.5 py-2 rounded-xl bg-slate-950 border border-slate-800 text-slate-200 focus:outline-none focus:border-cyan-500"
                  >
                    <option value="schedule">Schedule (Interval)</option>
                    <option value="webhook">Webhook Trigger</option>
                    <option value="manual">Manual Trigger Only</option>
                  </select>
                </div>

                <div className="space-y-1">
                  <label className="text-xs font-semibold text-slate-300">Action Type</label>
                  <select
                    value={formActionType}
                    onChange={(e) =>
                      setFormActionType(e.target.value as TaskAutomation['action_type'])
                    }
                    className="w-full px-3.5 py-2 rounded-xl bg-slate-950 border border-slate-800 text-slate-200 focus:outline-none focus:border-cyan-500"
                  >
                    <option value="ai_workflow">AI Agent Workflow</option>
                    <option value="github_decompose">GitHub Issue Decomposer</option>
                    <option value="daily_report">Daily Project Summary</option>
                    <option value="desktop_action">Desktop App Action</option>
                  </select>
                </div>
              </div>

              {formTriggerType === 'schedule' && (
                <div className="space-y-1">
                  <label className="text-xs font-semibold text-slate-300">Interval (Minutes)</label>
                  <input
                    type="number"
                    min="1"
                    value={formIntervalMinutes}
                    onChange={(e) => setFormIntervalMinutes(parseInt(e.target.value) || 60)}
                    className="w-full px-3.5 py-2 rounded-xl bg-slate-950 border border-slate-800 text-slate-200 focus:outline-none focus:border-cyan-500"
                  />
                </div>
              )}

              <div className="space-y-1">
                <label className="text-xs font-semibold text-slate-300">
                  Prompt / Payload Specification
                </label>
                <textarea
                  rows={3}
                  placeholder="Specific task instructions or prompt template for the AI runner..."
                  value={formPrompt}
                  onChange={(e) => setFormPrompt(e.target.value)}
                  className="w-full px-3.5 py-2 rounded-xl bg-slate-950 border border-slate-800 text-slate-200 focus:outline-none focus:border-cyan-500"
                />
              </div>

              <div className="flex items-center justify-end gap-3 pt-4 border-t border-slate-800">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 font-medium"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting}
                  className="px-4 py-2 rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 hover:from-cyan-400 hover:to-blue-500 text-white font-semibold flex items-center gap-2"
                >
                  {submitting && <Loader2 className="w-4 h-4 animate-spin" />}
                  <span>{editingAutomation ? 'Save Changes' : 'Create Automation'}</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
