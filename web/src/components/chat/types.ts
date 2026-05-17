import type { ProviderStatus, CloudBudgetStatus } from "@/lib/api-types";

export type Mode = {
  id: string;
  name: string;
  description: string;
};

export type Message = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  // Populated when the SSE stream fails or a transport-level error fires.
  // Distinct from `content` so partial output isn't blended with error text;
  // the renderer shows the error as a separate banner with a Retry action.
  error?: string;
  // For assistant messages: id of the user message that produced this turn,
  // so a Retry click can re-send that exact prompt without scrolling back.
  promptedByMessageId?: string;
};

export type IntakeAnswers = {
  objective: string;
  building: string;
  maturity: string;
  help_needed: string;
};

export type IntakeQuestion = {
  id: keyof IntakeAnswers;
  label: string;
  type?: "text";
  placeholder?: string;
  options?: string[];
};

export type ConnectionStatus = "checking" | "online" | "offline";
export type EntryStage = "intro" | "splitting" | "done";

export type ProviderHealth = {
  status: string;
  primary: ProviderStatus;
  secondary: ProviderStatus;
  models?: ProviderStatus[];
  cloud_fallback_configured: boolean;
  cloud_budget?: CloudBudgetStatus;
};
