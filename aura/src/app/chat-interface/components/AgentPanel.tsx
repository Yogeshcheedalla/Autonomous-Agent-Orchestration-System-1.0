'use client';

import React, { useState } from 'react';
import {
  Bot,
  CheckCircle2,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Terminal,
  Shield,
  Zap,
  Globe,
  Monitor,
  X,
} from 'lucide-react';
import { toast } from 'sonner';

interface AgentStep {
  id: string;
  label: string;
  status: 'pending' | 'running' | 'done' | 'error' | 'waiting_permission';
  detail?: string;
  requiresPermission?: boolean;
}

interface AgentTask {
  id: string;
  title: string;
  type: 'browser' | 'desktop' | 'analysis' | 'code';
  steps: AgentStep[];
  status: 'idle' | 'running' | 'paused' | 'completed' | 'error';
  progress: number;
}

const DEMO_TASKS: AgentTask[] = [
  {
    id: 'agent-001',
    title: 'Search and summarize latest AI news',
    type: 'browser',
    status: 'completed',
    progress: 100,
    steps: [
      { id: 's1', label: 'Open browser', status: 'done', detail: 'Launched headless browser' },
      {
        id: 's2',
        label: 'Navigate to news.ycombinator.com',
        status: 'done',
        detail: 'Page loaded in 1.2s',
      },
      {
        id: 's3',
        label: 'Extract top AI stories',
        status: 'done',
        detail: 'Found 8 relevant articles',
      },
      {
        id: 's4',
        label: 'Summarize content',
        status: 'done',
        detail: 'Generated 3-paragraph summary',
      },
    ],
  },
  {
    id: 'agent-002',
    title: 'Organize downloads folder',
    type: 'desktop',
    status: 'paused',
    progress: 40,
    steps: [
      { id: 's1', label: 'Scan Downloads folder', status: 'done', detail: 'Found 47 files' },
      {
        id: 's2',
        label: 'Categorize by file type',
        status: 'done',
        detail: 'Images: 12, Docs: 18, Code: 17',
      },
      {
        id: 's3',
        label: 'Create subfolders',
        status: 'waiting_permission',
        detail: 'Will create 3 new folders',
        requiresPermission: true,
      },
      { id: 's4', label: 'Move files', status: 'pending' },
    ],
  },
];

const TYPE_ICONS = {
  browser: Globe,
  desktop: Monitor,
  analysis: Zap,
  code: Terminal,
};

const TYPE_COLORS = {
  browser: 'text-blue-500 bg-blue-500/10 border-blue-500/20',
  desktop: 'text-purple-500 bg-purple-500/10 border-purple-500/20',
  analysis: 'text-amber-500 bg-amber-500/10 border-amber-500/20',
  code: 'text-green-500 bg-green-500/10 border-green-500/20',
};

const STATUS_ICONS = {
  done: <CheckCircle2 size={13} className="text-accent" />,
  running: (
    <div className="w-3 h-3 rounded-full border-2 border-primary border-t-transparent animate-spin" />
  ),
  error: <AlertCircle size={13} className="text-red-500" />,
  pending: <div className="w-3 h-3 rounded-full border border-border" />,
  waiting_permission: <Shield size={13} className="text-amber-500" />,
};

export default function AgentPanel() {
  const [tasks, setTasks] = useState<AgentTask[]>(DEMO_TASKS);
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(new Set(['agent-002']));
  const [newTaskInput, setNewTaskInput] = useState('');
  const [logsVisible, setLogsVisible] = useState(false);

  const toggleExpand = (id: string) => {
    setExpandedTasks((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handlePermission = (taskId: string, stepId: string, allow: boolean) => {
    if (allow) {
      setTasks((prev) =>
        prev.map((t) => {
          if (t.id !== taskId) return t;
          return {
            ...t,
            status: 'running',
            steps: t.steps.map((s) => (s.id === stepId ? { ...s, status: 'running' } : s)),
          };
        })
      );
      toast.success('Permission granted — continuing task');
      // Simulate completion
      setTimeout(() => {
        setTasks((prev) =>
          prev.map((t) => {
            if (t.id !== taskId) return t;
            return {
              ...t,
              status: 'completed',
              progress: 100,
              steps: t.steps.map((s) => ({ ...s, status: 'done' as const })),
            };
          })
        );
      }, 2000);
    } else {
      setTasks((prev) =>
        prev.map((t) => {
          if (t.id !== taskId) return t;
          return { ...t, status: 'paused' };
        })
      );
      toast.info('Task paused — permission denied');
    }
  };

  const startNewTask = () => {
    if (!newTaskInput.trim()) return;
    const newTask: AgentTask = {
      id: `agent-${Date.now()}`,
      title: newTaskInput.trim(),
      type: 'analysis',
      status: 'running',
      progress: 15,
      steps: [
        { id: 'ns1', label: 'Analyzing task requirements', status: 'running' },
        { id: 'ns2', label: 'Breaking into sub-steps', status: 'pending' },
        { id: 'ns3', label: 'Executing plan', status: 'pending' },
        { id: 'ns4', label: 'Generating report', status: 'pending' },
      ],
    };
    setTasks((prev) => [newTask, ...prev]);
    setExpandedTasks((prev) => new Set([...prev, newTask.id]));
    setNewTaskInput('');
    toast.success('Agent task started');
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-border shrink-0">
        <div className="flex items-center gap-2">
          <Bot size={14} className="text-primary" />
          <span className="text-xs font-semibold text-foreground">AI Agent</span>
        </div>
        <button
          onClick={() => setLogsVisible(!logsVisible)}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          <Terminal size={11} />
          Logs
        </button>
      </div>

      <div className="flex-1 overflow-y-auto scrollbar-thin p-3 space-y-3">
        {/* New task input */}
        <div className="flex gap-2">
          <input
            type="text"
            placeholder="Describe a task for the agent..."
            value={newTaskInput}
            onChange={(e) => setNewTaskInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && startNewTask()}
            className="flex-1 bg-muted rounded-lg text-xs px-3 py-2 text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary/40 border border-border"
          />
          <button
            onClick={startNewTask}
            className="px-3 py-2 rounded-lg bg-primary text-white text-xs font-medium hover:bg-primary-hover transition-colors shrink-0"
          >
            Run
          </button>
        </div>

        {/* Task list */}
        {tasks.map((task) => {
          const TypeIcon = TYPE_ICONS[task.type];
          const isExpanded = expandedTasks.has(task.id);
          const permissionStep = task.steps.find((s) => s.status === 'waiting_permission');

          return (
            <div
              key={task.id}
              className="rounded-xl border border-border bg-card/50 overflow-hidden"
            >
              {/* Task header */}
              <button
                onClick={() => toggleExpand(task.id)}
                className="flex items-start gap-2.5 w-full p-3 hover:bg-muted/30 transition-colors text-left"
              >
                <span
                  className={`text-xs px-1.5 py-0.5 rounded border font-medium shrink-0 ${TYPE_COLORS[task.type]}`}
                >
                  <TypeIcon size={10} className="inline mr-1" />
                  {task.type}
                </span>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-foreground truncate">{task.title}</p>
                  {/* Progress bar */}
                  <div className="mt-1.5 h-1 bg-muted rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full transition-all duration-500"
                      style={{
                        width: `${task.progress}%`,
                        background:
                          task.status === 'completed'
                            ? 'var(--teal-accent)'
                            : task.status === 'error'
                              ? '#EF4444'
                              : 'linear-gradient(90deg, var(--violet-primary), var(--teal-accent))',
                      }}
                    />
                  </div>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {task.progress}% · {task.status}
                  </p>
                </div>
                {isExpanded ? (
                  <ChevronDown size={13} className="text-muted-foreground shrink-0 mt-0.5" />
                ) : (
                  <ChevronRight size={13} className="text-muted-foreground shrink-0 mt-0.5" />
                )}
              </button>

              {/* Permission prompt */}
              {permissionStep && (
                <div className="mx-3 mb-3 p-3 rounded-xl bg-amber-500/5 border border-amber-500/20">
                  <div className="flex items-center gap-2 mb-2">
                    <Shield size={13} className="text-amber-500" />
                    <span className="text-xs font-semibold text-amber-600 dark:text-amber-400">
                      Permission Required
                    </span>
                  </div>
                  <p className="text-xs text-muted-foreground mb-3">
                    <strong className="text-foreground">{permissionStep.label}</strong>:{' '}
                    {permissionStep.detail}
                  </p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => handlePermission(task.id, permissionStep.id, true)}
                      className="flex-1 px-3 py-1.5 rounded-lg bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 transition-colors border border-accent/20"
                    >
                      Allow
                    </button>
                    <button
                      onClick={() => handlePermission(task.id, permissionStep.id, false)}
                      className="flex-1 px-3 py-1.5 rounded-lg bg-muted text-muted-foreground text-xs font-medium hover:bg-red-500/10 hover:text-red-500 transition-colors border border-border"
                    >
                      Deny
                    </button>
                  </div>
                </div>
              )}

              {/* Steps */}
              {isExpanded && (
                <div className="px-3 pb-3 space-y-1.5">
                  {task.steps.map((step, idx) => (
                    <div key={step.id} className="flex items-start gap-2.5">
                      <div className="shrink-0 mt-0.5">{STATUS_ICONS[step.status]}</div>
                      <div className="flex-1 min-w-0">
                        <p
                          className={`text-xs font-medium ${step.status === 'pending' ? 'text-muted-foreground' : 'text-foreground'}`}
                        >
                          {idx + 1}. {step.label}
                        </p>
                        {step.detail && (
                          <p className="text-xs text-muted-foreground mt-0.5">{step.detail}</p>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {/* Logs panel */}
        {logsVisible && (
          <div className="rounded-xl border border-border bg-zinc-950 dark:bg-zinc-900 overflow-hidden">
            <div className="flex items-center justify-between px-3 py-2 border-b border-border">
              <span className="text-xs font-mono text-muted-foreground">Agent Logs</span>
              <button
                onClick={() => setLogsVisible(false)}
                className="text-muted-foreground hover:text-foreground"
              >
                <X size={12} />
              </button>
            </div>
            <div className="p-3 font-mono text-xs text-zinc-300 space-y-1 max-h-32 overflow-y-auto scrollbar-thin">
              <p>
                <span className="text-green-400">[INFO]</span> Agent initialized
              </p>
              <p>
                <span className="text-blue-400">[TASK]</span> Loaded 2 tasks from history
              </p>
              <p>
                <span className="text-amber-400">[WARN]</span> Task agent-002 awaiting permission
              </p>
              <p>
                <span className="text-green-400">[INFO]</span> Browser automation ready
              </p>
              <p>
                <span className="text-zinc-500">[DEBUG]</span> Memory context loaded: 5 items
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
