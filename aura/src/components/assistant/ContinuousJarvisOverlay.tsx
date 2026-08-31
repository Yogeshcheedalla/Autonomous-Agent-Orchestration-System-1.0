'use client';

import React from 'react';
import { Play, Pause, Square, Mic, Cpu, CheckCircle2, Circle } from 'lucide-react';

export interface ContinuousSubtask {
  id: string;
  goal_description: string;
  status: 'pending' | 'executing' | 'completed' | 'failed' | 'paused';
  voice_update_prompt: string;
}

export interface ContinuousJarvisState {
  sessionId: string | null;
  mainGoal: string;
  status: 'idle' | 'active' | 'paused' | 'completed' | 'cancelled';
  currentStepIndex: number;
  subtasks: ContinuousSubtask[];
  voiceLogs: string[];
}

interface ContinuousJarvisOverlayProps {
  jarvisState: ContinuousJarvisState;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onVoiceInterrupt: (command: string) => void;
}

export const ContinuousJarvisOverlay: React.FC<ContinuousJarvisOverlayProps> = ({
  jarvisState,
  onPause,
  onResume,
  onCancel,
  onVoiceInterrupt,
}) => {
  if (!jarvisState.sessionId || jarvisState.status === 'idle') {
    return null;
  }

  const activeSubtask = jarvisState.subtasks[jarvisState.currentStepIndex];

  return (
    <div className="fixed bottom-6 right-6 z-50 w-96 rounded-2xl border border-white/20 bg-slate-900/80 p-5 shadow-2xl backdrop-blur-xl transition-all duration-300 text-white font-sans">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-white/10 pb-3 mb-4">
        <div className="flex items-center space-x-2">
          <div className="relative flex h-3 w-3">
            <span
              className={`absolute inline-flex h-full w-full animate-ping rounded-full ${jarvisState.status === 'active' ? 'bg-cyan-400' : 'bg-amber-400'} opacity-75`}
            ></span>
            <span
              className={`relative inline-flex h-3 w-3 rounded-full ${jarvisState.status === 'active' ? 'bg-cyan-500' : 'bg-amber-500'}`}
            ></span>
          </div>
          <span className="text-sm font-semibold tracking-wide text-cyan-300 uppercase">
            Jarvis Continuous Mode
          </span>
        </div>
        <div className="flex items-center space-x-1">
          {jarvisState.status === 'active' ? (
            <button
              onClick={onPause}
              className="rounded-lg p-1.5 hover:bg-white/10 text-slate-300 hover:text-white transition"
              title="Pause execution"
            >
              <Pause className="h-4 w-4" />
            </button>
          ) : (
            <button
              onClick={onResume}
              className="rounded-lg p-1.5 hover:bg-white/10 text-cyan-400 hover:text-cyan-300 transition"
              title="Resume execution"
            >
              <Play className="h-4 w-4" />
            </button>
          )}
          <button
            onClick={onCancel}
            className="rounded-lg p-1.5 hover:bg-red-500/20 text-red-400 hover:text-red-300 transition"
            title="Cancel task"
          >
            <Square className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Main Goal */}
      <div className="mb-4">
        <div className="text-xs text-slate-400 uppercase tracking-wider mb-1">Active Goal</div>
        <div className="text-sm font-medium text-slate-100 line-clamp-2">
          {jarvisState.mainGoal}
        </div>
      </div>

      {/* Spoken Voice Prompt Banner */}
      {activeSubtask && (
        <div className="mb-4 flex items-center space-x-3 rounded-xl border border-cyan-500/30 bg-cyan-500/10 p-3">
          <Mic className="h-5 w-5 text-cyan-400 animate-pulse flex-shrink-0" />
          <div className="text-xs text-cyan-200">
            <span className="font-semibold text-cyan-300">Spoken Status: </span>
            {activeSubtask.voice_update_prompt}
          </div>
        </div>
      )}

      {/* Subtasks Progress List */}
      <div className="space-y-2 max-h-48 overflow-y-auto pr-1 custom-scrollbar mb-4">
        {jarvisState.subtasks.map((task, idx) => {
          const isCurrent = idx === jarvisState.currentStepIndex;
          const isDone = idx < jarvisState.currentStepIndex || task.status === 'completed';

          return (
            <div
              key={task.id}
              className={`flex items-start space-x-2.5 rounded-lg p-2 text-xs transition ${
                isCurrent
                  ? 'border border-cyan-500/40 bg-cyan-950/40 text-cyan-100'
                  : isDone
                    ? 'text-slate-400 opacity-80'
                    : 'text-slate-500'
              }`}
            >
              {isDone ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-400 flex-shrink-0 mt-0.5" />
              ) : isCurrent ? (
                <Cpu className="h-4 w-4 text-cyan-400 animate-spin flex-shrink-0 mt-0.5" />
              ) : (
                <Circle className="h-4 w-4 text-slate-600 flex-shrink-0 mt-0.5" />
              )}
              <div className="flex-1">
                <div className={`font-medium ${isCurrent ? 'text-cyan-200' : ''}`}>
                  {task.goal_description}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Voice Interrupt Buttons Quick Bar */}
      <div className="flex items-center justify-between border-t border-white/10 pt-3 text-[11px] text-slate-400">
        <span>Say: &quot;Akansha pause&quot; or &quot;stop&quot;</span>
        <button onClick={() => onVoiceInterrupt('pause')} className="text-cyan-400 hover:underline">
          Simulate Interrupt
        </button>
      </div>
    </div>
  );
};

export default ContinuousJarvisOverlay;
