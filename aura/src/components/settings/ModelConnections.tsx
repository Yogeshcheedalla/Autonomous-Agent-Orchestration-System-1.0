'use client';

/**
 * Which model answers, chosen by the person the answers are for.
 *
 * The ask was "model connection UI for Ollama with model choice, and OpenRouter
 * with automatic failover". The failover half is backend behaviour and needs no
 * screen; the choice half needs exactly this one, and it has an unusual job for
 * a settings panel: on most machines it opens with *nothing to choose from*,
 * because Ollama is a separate program that has to be installed first.
 *
 * So the empty state is the main state, and it is written as instructions rather
 * than as an apology. A panel that says "no local models found" and stops has
 * told the user nothing they can act on.
 *
 * The cascade is shown in full, in order, because it is the honest answer to the
 * question this screen actually raises -- "so which one am I talking to?" --
 * where a single highlighted name would imply a certainty the fallback chain
 * does not have.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Check, Cloud, Cpu, ExternalLink, RefreshCw, Terminal } from 'lucide-react';
import { toast } from 'sonner';
import { apiUrl } from '@/lib/apiBase';

interface LocalModel {
  model_id: string;
  name: string;
  size_label: string;
  parameter_size: string;
  quantization: string;
}

interface SuggestedModel {
  name: string;
  why: string;
}

interface RoutesState {
  local: {
    base_url: string;
    available: boolean;
    error: string;
    models: LocalModel[];
    suggested: SuggestedModel[];
    install_url: string;
  };
  cloud: { configured: boolean; models: string[]; cooling: Record<string, number> };
  preference: { preferred: string; updated_at: string };
  cascade: string[];
}

function CopyableCommand({ command }: { command: string }) {
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(command);
        toast.success('Copied. Paste it into a terminal.');
      }}
      className="flex w-full items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2 text-left font-mono text-xs text-foreground transition-colors hover:border-primary/40"
    >
      <Terminal size={13} className="shrink-0 text-muted-foreground" />
      {command}
    </button>
  );
}

export default function ModelConnections() {
  const [state, setState] = useState<RoutesState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      const res = await fetch(apiUrl(`/api/models/routes${refresh ? '?refresh=1' : ''}`));
      if (!res.ok) throw new Error('Could not read model providers');
      setState((await res.json()) as RoutesState);
    } catch {
      toast.error('Could not reach the backend to read model providers');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  const choose = useCallback(
    async (model: string) => {
      setBusy(model || 'clear');
      try {
        const res = await fetch(apiUrl('/api/models/preference'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data?.detail || 'Could not save that choice');
        toast.success(model ? `${model} will be tried first` : 'Back to the default order');
        await load(false);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : 'Could not save that choice');
      } finally {
        setBusy('');
      }
    },
    [load]
  );

  const preferred = state?.preference.preferred ?? '';

  return (
    <div className="space-y-6">
      <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
        <div className="mb-2 flex items-start justify-between gap-4">
          <h3 className="flex items-center gap-2 text-lg font-semibold">
            <Cpu size={18} className="text-primary" />
            On this machine
          </h3>
          <button
            type="button"
            onClick={() => void load(true)}
            disabled={loading}
            className="flex items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
            Check again
          </button>
        </div>
        <p className="mb-5 text-sm text-muted-foreground">
          A local model runs on your own CPU. It costs nothing, works with no internet, and answers
          when every hosted route is out of quota. Akansha talks to it through Ollama at{' '}
          <span className="font-mono text-xs">{state?.local.base_url ?? 'localhost:11434'}</span>.
        </p>

        {state?.local.available ? (
          <div className="space-y-2">
            {state.local.models.map((model) => {
              const active = preferred === model.model_id;
              return (
                <button
                  key={model.model_id}
                  type="button"
                  disabled={busy !== ''}
                  onClick={() => void choose(active ? '' : model.model_id)}
                  className={`flex w-full items-center justify-between gap-3 rounded-xl border px-4 py-3 text-left transition-all disabled:opacity-60 ${
                    active
                      ? 'border-primary bg-primary/5'
                      : 'border-border hover:border-primary/40 hover:bg-muted/40'
                  }`}
                >
                  <span>
                    <span className="block text-sm font-medium text-foreground">{model.name}</span>
                    <span className="block text-xs text-muted-foreground">
                      {[model.parameter_size, model.quantization, model.size_label]
                        .filter(Boolean)
                        .join(' · ') || 'local model'}
                    </span>
                  </span>
                  {active ? (
                    <span className="flex shrink-0 items-center gap-1 text-xs font-semibold text-primary">
                      <Check size={14} /> First choice
                    </span>
                  ) : (
                    <span className="shrink-0 text-xs text-muted-foreground">Use first</span>
                  )}
                </button>
              );
            })}
          </div>
        ) : (
          <div className="space-y-4">
            <p className="rounded-xl border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-foreground">
              {state?.local.error || 'Looking for a local model server…'}
            </p>
            <div className="space-y-3">
              <p className="text-sm font-medium text-foreground">To run models locally:</p>
              <a
                href={state?.local.install_url ?? 'https://ollama.com/download'}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 text-sm font-medium text-primary hover:underline"
              >
                1. Install Ollama <ExternalLink size={13} />
              </a>
              <p className="text-sm text-foreground">
                2. Pull a model — any of these work well here:
              </p>
              <div className="space-y-2">
                {(state?.local.suggested ?? []).map((entry) => (
                  <div key={entry.name} className="space-y-1">
                    <CopyableCommand command={`ollama pull ${entry.name}`} />
                    <p className="px-1 text-xs text-muted-foreground">{entry.why}</p>
                  </div>
                ))}
              </div>
              <p className="text-sm text-foreground">
                3. Press <span className="font-medium">Check again</span>.
              </p>
            </div>
          </div>
        )}
      </div>

      <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
        <h3 className="mb-2 flex items-center gap-2 text-lg font-semibold">
          <Cloud size={18} className="text-primary" />
          Hosted models
        </h3>
        <p className="mb-5 text-sm text-muted-foreground">
          {state?.cloud.configured
            ? 'Reached through OpenRouter with the key in your .env file. Tried in this order; a route that runs out of credit or stops serving your key is moved to the back for 15 minutes and returns on its own.'
            : 'No OpenRouter key is configured, so none of these can be used. Add OPENROUTER_API_KEY to aura/.env and restart the backend.'}
        </p>
        <div className="space-y-2">
          {(state?.cloud.models ?? []).map((model) => {
            const cooling = state?.cloud.cooling?.[model];
            const active = preferred === model;
            return (
              <button
                key={model}
                type="button"
                disabled={busy !== '' || !state?.cloud.configured}
                onClick={() => void choose(active ? '' : model)}
                className={`flex w-full items-center justify-between gap-3 rounded-xl border px-4 py-3 text-left transition-all disabled:opacity-60 ${
                  active
                    ? 'border-primary bg-primary/5'
                    : 'border-border hover:border-primary/40 hover:bg-muted/40'
                }`}
              >
                <span className="min-w-0">
                  <span className="block truncate font-mono text-xs text-foreground">{model}</span>
                  {cooling ? (
                    <span className="block text-xs text-amber-500">
                      Skipped for another {Math.ceil(cooling / 60)} min — it failed for a reason
                      that will not fix itself in a retry
                    </span>
                  ) : null}
                </span>
                {active ? (
                  <span className="flex shrink-0 items-center gap-1 text-xs font-semibold text-primary">
                    <Check size={14} /> First choice
                  </span>
                ) : (
                  <span className="shrink-0 text-xs text-muted-foreground">Use first</span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      <div className="rounded-2xl border border-border bg-card/50 p-6 backdrop-blur-sm">
        <h3 className="mb-2 text-lg font-semibold">The order right now</h3>
        <p className="mb-4 text-sm text-muted-foreground">
          Akansha tries these top to bottom and speaks with the first one that answers. Voice uses
          only the first three, because a listener cannot wait out a long chain of failures.
        </p>
        <ol className="space-y-1.5">
          {(state?.cascade ?? []).map((model, index) => (
            <li key={model} className="flex items-center gap-3 text-sm">
              <span className="w-5 shrink-0 text-right text-xs text-muted-foreground">
                {index + 1}
              </span>
              <span className="truncate font-mono text-xs text-foreground">{model}</span>
              {index < 3 ? (
                <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-primary">
                  voice
                </span>
              ) : null}
            </li>
          ))}
        </ol>
        {(state?.cascade ?? []).length === 0 && !loading ? (
          <p className="text-sm text-amber-500">
            Nothing is connected, so Akansha cannot answer at all right now. Add an OpenRouter key
            or install a local model above.
          </p>
        ) : null}
        {preferred ? (
          <button
            type="button"
            onClick={() => void choose('')}
            disabled={busy !== ''}
            className="mt-4 text-xs font-medium text-muted-foreground underline hover:text-foreground disabled:opacity-50"
          >
            Clear my choice and use the default order
          </button>
        ) : null}
      </div>
    </div>
  );
}
