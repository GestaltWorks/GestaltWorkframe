import type { ProviderStatus } from "@/lib/api-types";

export type AdminPolicy = {
  routing_strategy: string;
  cloud_spillover_enabled: boolean;
  low_cost_enabled: boolean;
  claude_enabled: boolean;
  max_calls_per_turn: number;
  max_calls_per_session: number;
  max_calls_per_day: number;
  max_calls_per_month: number;
  max_daily_usd: number;
  max_monthly_usd: number;
  max_input_tokens_per_call: number;
  max_output_tokens_per_call: number;
  route_overrides: Record<string, boolean>;
};

export type ChatMetrics = {
  started_at?: string;
  last_turn_at?: string | null;
  total_turns?: number;
  completed_turns?: number;
  failed_turns?: number;
  failure_rate?: number;
  avg_duration_ms?: number;
  avg_output_tokens_estimate?: number;
  total_output_tokens_estimate?: number;
  by_mode?: Record<string, number>;
  by_route?: Record<string, number>;
  by_route_family?: Record<string, number>;
};

export type GenerationConcurrency = {
  active_total?: number;
  active_local?: number;
  active_cloud?: number;
  max_total?: number;
  max_local?: number;
  max_cloud?: number;
};

export type ProviderHealth = {
  status: string;
  models?: ProviderStatus[];
  policy?: AdminPolicy;
  cloud_budget?: Record<string, unknown>;
  circuit_breaker?: Record<string, unknown>;
  generation_concurrency?: GenerationConcurrency;
  route_diagnostics?: Record<string, unknown>;
  metrics?: { chat?: ChatMetrics };
};

export type HandoffPacket = {
  record_id: string;
  created_at: string;
  source: string;
  packet_type: string;
  title: string;
  summary: string;
  contact?: Record<string, string>;
  fields?: { label: string; value: string }[];
  next_steps?: string[];
  tags?: string[];
};
