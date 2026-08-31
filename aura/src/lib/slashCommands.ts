'use client';

export type SlashCommandCategory =
  | 'Admin'
  | 'Planning'
  | 'Memory'
  | 'Automation'
  | 'Writing'
  | 'Coding'
  | 'Social'
  | 'Security'
  | 'Language'
  | 'Cognitive';

export type SlashCommandDefinition = {
  name: string;
  category: SlashCommandCategory;
  description: string;
  template: string;
  aliases?: string[];
};

const inputFallback = '[add the details here]';

export const SLASH_COMMANDS: SlashCommandDefinition[] = [
  {
    name: 'help',
    category: 'Admin',
    description: 'Show the available Akansha slash commands.',
    template: 'Show me Akansha slash command help grouped by category.',
  },
  {
    name: 'summarize',
    category: 'Admin',
    description: 'Summarize text with decisions, risks, and next actions.',
    template: 'Summarize this clearly with key decisions, risks, and next actions:\n{input}',
  },
  {
    name: 'brief',
    category: 'Admin',
    description: 'Create a short executive brief.',
    template:
      'Create a concise executive brief with context, current status, blockers, and next steps:\n{input}',
  },
  {
    name: 'admin-report',
    category: 'Admin',
    description: 'Prepare an administrative status report.',
    template:
      'Prepare an administrative status report with completed work, pending work, risks, owners, deadlines, and recommended action:\n{input}',
    aliases: ['report'],
  },
  {
    name: 'status',
    category: 'Admin',
    description: 'Turn notes into a crisp progress update.',
    template:
      'Turn this into a professional progress update with done, doing, blocked, and next sections:\n{input}',
  },
  {
    name: 'policy',
    category: 'Admin',
    description: 'Draft a policy, rule, or governance note.',
    template:
      'Draft a clear policy with purpose, scope, rules, exceptions, and enforcement steps:\n{input}',
  },
  {
    name: 'sop',
    category: 'Admin',
    description: 'Create a standard operating procedure.',
    template:
      'Create a standard operating procedure with prerequisites, steps, checks, and escalation path:\n{input}',
  },
  {
    name: 'meeting',
    category: 'Admin',
    description: 'Create a meeting agenda.',
    template:
      'Create a meeting agenda with objective, attendees, topics, questions, and expected decisions:\n{input}',
  },
  {
    name: 'minutes',
    category: 'Admin',
    description: 'Convert notes into meeting minutes.',
    template:
      'Convert these notes into meeting minutes with decisions, action items, owners, and deadlines:\n{input}',
  },
  {
    name: 'decision',
    category: 'Admin',
    description: 'Write a decision memo.',
    template:
      'Write a decision memo with background, options, recommendation, tradeoffs, and final decision:\n{input}',
  },
  {
    name: 'risk',
    category: 'Admin',
    description: 'Create a risk register.',
    template:
      'Create a risk register with risk, likelihood, impact, mitigation, owner, and review date:\n{input}',
  },
  {
    name: 'twin',
    category: 'Cognitive',
    description: 'Use Akansha digital twin reasoning for personal prediction and planning.',
    template:
      'Use Akansha Cognitive Digital Twin mode. Build a personal context model, simulate likely outcomes, predict risks, compare choices, recommend the best action, and explain confidence:\n{input}',
    aliases: ['digital-twin'],
  },
  {
    name: 'simulate',
    category: 'Cognitive',
    description: 'Compare future scenarios with probabilities, risks, and recommendations.',
    template:
      'Run a future simulation. Compare scenarios, estimate timeline, resources, success probability, risk score, hidden dependencies, and final recommendation:\n{input}',
  },
  {
    name: 'goal',
    category: 'Cognitive',
    description: 'Turn a long-term goal into milestones, tasks, risks, and tracking.',
    template:
      'Use the Autonomous Goal Engine. Decompose this into goals, subgoals, milestones, tasks, dependencies, deadlines, progress metrics, blockers, and next actions:\n{input}',
  },
  {
    name: 'breakdown',
    category: 'Cognitive',
    description: 'Break a large conversation or task into a compact execution plan.',
    template:
      'Break this large request into a compact execution plan. Extract intent, constraints, risks, required agents, required skills, verification checks, and the shortest safe workflow:\n{input}',
  },
  {
    name: 'audit',
    category: 'Admin',
    description: 'Audit content or a workflow for gaps.',
    template:
      'Audit this for bugs, missing requirements, risks, compliance gaps, and practical fixes:\n{input}',
  },
  {
    name: 'todo',
    category: 'Planning',
    description: 'Add a to-do, asking only for missing details.',
    template: 'Add {input} to my to-do list.',
  },
  {
    name: 'calendar',
    category: 'Planning',
    description: 'Add a calendar item with start/end/reminder details.',
    template: 'Add {input} to my calendar.',
  },
  {
    name: 'remind',
    category: 'Planning',
    description: 'Create a reminder from a natural instruction.',
    template: 'Set a reminder for {input}.',
  },
  {
    name: 'remember',
    category: 'Memory',
    description: 'Save something important to memory.',
    template: 'Save this as an important memory and make it available in future chats:\n{input}',
  },
  {
    name: 'forget',
    category: 'Memory',
    description: 'Remove or ignore a memory.',
    template:
      'Find and remove this memory if it exists. If removal is not possible, explain what should be forgotten:\n{input}',
  },
  {
    name: 'history',
    category: 'Memory',
    description: 'Search previous chat history.',
    template: 'Search my chat history and summarize anything relevant to:\n{input}',
  },
  {
    name: 'email',
    category: 'Writing',
    description: 'Draft a polished email.',
    template: 'Draft a polished email with subject, greeting, concise body, and closing:\n{input}',
  },
  {
    name: 'reply',
    category: 'Writing',
    description: 'Suggest replies in different tones.',
    template:
      'Suggest three reply options: friendly, professional, and short. Include emojis only if appropriate:\n{input}',
  },
  {
    name: 'translate',
    category: 'Language',
    description: 'Translate while preserving tone.',
    template:
      'Translate this while preserving meaning and tone. If no target language is given, ask once:\n{input}',
  },
  {
    name: 'telugu',
    category: 'Language',
    description: 'Respond in Telugu plus English where useful.',
    template:
      'Respond in natural Telugu plus English where helpful. Keep it clear and conversational:\n{input}',
  },
  {
    name: 'hindi',
    category: 'Language',
    description: 'Respond in Hindi.',
    template: 'Respond in natural Hindi. Keep it clear and conversational:\n{input}',
  },
  {
    name: 'debug',
    category: 'Coding',
    description: 'Debug an error with root cause and fix.',
    template: 'Debug this. Explain the root cause, exact fix, and verification steps:\n{input}',
  },
  {
    name: 'code-review',
    category: 'Coding',
    description: 'Review code for bugs and regressions.',
    template:
      'Review this code for bugs, regressions, security issues, and missing tests. Lead with findings:\n{input}',
  },
  {
    name: 'test-plan',
    category: 'Coding',
    description: 'Create a focused test plan.',
    template:
      'Create a focused test plan with critical cases, edge cases, negative cases, and smoke checks:\n{input}',
  },
  {
    name: 'browser',
    category: 'Automation',
    description: 'Run a browser automation task.',
    template: 'Open and automate this browser task: {input}',
  },
  {
    name: 'desktop',
    category: 'Automation',
    description: 'Force Akansha to open something as a desktop app.',
    template: 'Open {input} in the desktop app.',
    aliases: ['app', 'desktop-app'],
  },
  {
    name: 'web',
    category: 'Automation',
    description: 'Force Akansha to open something in the browser.',
    template: 'Open {input} in the web browser.',
  },
  {
    name: 'website',
    category: 'Automation',
    description: 'Force Akansha to open something as a website in the browser.',
    template: 'Open {input} in the web browser.',
    aliases: ['site'],
  },
  {
    name: 'automations',
    category: 'Automation',
    description: 'List active task automations, schedules, and recent execution logs.',
    template:
      'List all active task automations, background schedules, and recent execution audit logs.',
    aliases: ['task-automations', 'canvas-automations'],
  },
  {
    name: 'automate',
    category: 'Automation',
    description: 'Create a scheduled or webhook task automation.',
    template: 'Create a task automation for: {input}',
    aliases: ['schedule-task', 'task-studio'],
  },
  {
    name: 'run-automation',
    category: 'Automation',
    description: 'Trigger an immediate execution of a task automation.',
    template: 'Trigger immediate execution of task automation: {input}',
    aliases: ['trigger-automation', 'exec-automation'],
  },
  {
    name: 'open',
    category: 'Automation',
    description: 'Open a link, app, or website.',
    template: 'Open {input}',
  },
  {
    name: 'social',
    category: 'Social',
    description: 'Review social messages and suggest replies.',
    template:
      'Review connected social messages for this request, summarize who messaged me, recommend replies, and only send anything after my approval:\n{input}',
  },
  {
    name: 'security',
    category: 'Security',
    description: 'Check secrets, auth, privacy, or access risks.',
    template:
      'Review this for security, privacy, authentication, authorization, secret handling, and data-access risks. Give concrete fixes:\n{input}',
  },
];

export function normalizeSlashCommandName(value: string) {
  return value.trim().replace(/^\//, '').toLowerCase();
}

export function findSlashCommand(name: string) {
  const normalized = normalizeSlashCommandName(name);
  return SLASH_COMMANDS.find(
    (command) => command.name === normalized || command.aliases?.includes(normalized)
  );
}

export function parseSlashCommand(value: string) {
  const trimmed = value.trim();
  if (!trimmed.startsWith('/')) return null;

  const match = trimmed.match(/^\/([a-z0-9-]+)(?:\s+([\s\S]*))?$/i);
  if (!match) return null;

  const command = findSlashCommand(match[1]);
  if (!command) return null;

  return {
    command,
    remainder: (match[2] || '').trim(),
  };
}

export function expandSlashCommand(value: string) {
  const parsed = parseSlashCommand(value);
  if (!parsed) return value;

  if (parsed.command.name === 'help') {
    const grouped = SLASH_COMMANDS.reduce<Record<string, string[]>>((acc, command) => {
      acc[command.category] = acc[command.category] || [];
      acc[command.category].push(`/${command.name} - ${command.description}`);
      return acc;
    }, {});
    return `Show this Akansha slash-command help in a compact grouped list:\n${Object.entries(
      grouped
    )
      .map(([category, commands]) => `${category}\n${commands.join('\n')}`)
      .join('\n\n')}`;
  }

  if (parsed.command.name === 'desktop') {
    const appTarget =
      (parsed.remainder || inputFallback).replace(/^\s*open\s+/i, '').trim() || inputFallback;
    return `Open ${appTarget} in the desktop app.`;
  }

  if (parsed.command.name === 'web' || parsed.command.name === 'website') {
    const webTarget =
      (parsed.remainder || inputFallback).replace(/^\s*open\s+/i, '').trim() || inputFallback;
    return `Open ${webTarget} in the web browser.`;
  }

  const input = parsed.remainder || inputFallback;
  return parsed.command.template.replace('{input}', input);
}

export function autoRouteCognitivePrompt(value: string) {
  const trimmed = value.trim();
  if (!trimmed || trimmed.startsWith('/')) return value;

  const normalized = trimmed.toLowerCase();
  const cognitiveSignals = [
    'digital twin',
    'simulate',
    'prediction',
    'predict',
    'future',
    'risk',
    'goal',
    'milestone',
    'deadline',
    'decision',
    'compare options',
    'long term',
    'startup',
    'project plan',
    'break down',
    'breakdown',
  ];
  const looksLikeLargeGoal =
    trimmed.length > 420 &&
    /(goal|plan|project|startup|future|risk|milestone|deadline|decision|simulate|predict)/i.test(
      trimmed
    );

  if (!looksLikeLargeGoal && !cognitiveSignals.some((signal) => normalized.includes(signal))) {
    return value;
  }

  return [
    'Use Akansha Cognitive Digital Twin and Goal Engine routing for this request.',
    'Extract the real goal, compress noisy context, simulate outcomes, predict risks, choose the best workflow, and explain confidence before action.',
    '',
    trimmed,
  ].join('\n');
}

export function getSlashCommandSuggestions(value: string) {
  const trimmedStart = value.trimStart();
  if (!trimmedStart.startsWith('/')) return [];

  const commandToken = trimmedStart.split(/\s+/)[0] || '/';
  const query = normalizeSlashCommandName(commandToken);
  if (!query) return SLASH_COMMANDS;

  return SLASH_COMMANDS.filter((command) => {
    return (
      command.name.includes(query) ||
      command.category.toLowerCase().includes(query) ||
      command.description.toLowerCase().includes(query) ||
      command.aliases?.some((alias) => alias.includes(query))
    );
  });
}
