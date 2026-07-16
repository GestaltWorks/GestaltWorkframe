import type { ConnectionStatus, IntakeAnswers, IntakeQuestion, Mode } from "./types";

export const emptyIntake: IntakeAnswers = {
  objective: "",
  building: "",
  maturity: "",
  help_needed: "",
};

export const createClientId = (prefix: string) => {
  const browserCrypto = globalThis.crypto;
  if (browserCrypto?.randomUUID) return browserCrypto.randomUUID();
  if (browserCrypto?.getRandomValues) {
    const suffix = Array.from(browserCrypto.getRandomValues(new Uint32Array(4)), (value) => value.toString(36)).join("-");
    return `${prefix}-${suffix}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

export const createTerminalSessionId = () => createClientId("terminal");
export const createMessageId = () => createClientId("message");

export const openingLines = [
  "Welcome, Traveler!",
  "Settle in. We will help find the right path through the work.",
  "A few guided prompts shape the session before chat begins.",
];

export const anthillFillBottom = 72;
export const anthillFillHeight = 54;

// Typed inside the particle frame while the terminal mounts behind it.
// Brand-neutral defaults; deployments override via their copy bundle.
export const entryBootLines = [
  "$ terminal init",
  "  loading guided intake ......... ok",
  "  connecting providers .......... ok",
  "",
  "> A few guided prompts shape the session.",
  "",
  "$ ",
];

export const defaultModes: Mode[] = [
  { id: "pipeline", name: "Service Inquiry", description: "" },
  { id: "automator", name: "Automator Assistance", description: "" },
  { id: "educator", name: "Automation Educator", description: "" },
];

export const connectionStatusLabel: Record<ConnectionStatus, string> = {
  checking: "Checking",
  online: "Online",
  offline: "Offline",
};

export const connectionStatusClass: Record<ConnectionStatus, string> = {
  checking: "text-brand-gold-warm",
  online: "text-brand-sage",
  offline: "text-red-300",
};

// Maps full option strings returned by /intake/questions to shorter display labels.
// Keys must stay in sync with the server-side option text - a mismatch falls through
// gracefully (full string shown), but the display label will be wrong.
export const objectiveOptionLabels: Record<string, string> = {
  "Explore automation support or consulting": "Explore automation support",
  "Get help building or debugging a workflow": "Build or debug a workflow",
  "Learn how automation works": "Learn automation concepts",
  "Find reusable workflows, patterns, or examples": "Search reusable patterns",
};

export const defaultIntakeQuestions: IntakeQuestion[] = [
  {
    id: "objective",
    label: "What are you hoping to accomplish?",
    options: Object.keys(objectiveOptionLabels),
  },
  {
    id: "building",
    label: "What are you trying to do or build?",
    type: "text",
    placeholder: "Example: onboard users, clean data, sync tickets, learn concepts...",
  },
  {
    id: "maturity",
    label: "How automated are you today?",
    options: ["Just starting", "Some scripts/workflows", "Several production automations", "Mature automation program"],
  },
  {
    id: "help_needed",
    label: "What would be most useful right now?",
    options: [
      "Help me choose the next step",
      "Give me a technical answer I can use",
      "Show me examples or patterns",
      "Walk me through it so I understand",
    ],
  },
];
