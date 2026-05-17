import type { ProviderStatus } from "@/lib/api-types";

const _apiBase = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
export const apiTargetLabel = _apiBase || "same-origin API";
export const localApiTarget = _apiBase.includes("127.0.0.1") || _apiBase.includes("localhost");
export const terminalHref = "/terminal";

export const policyPresets = [
  {
    name: "Low",
    description: "Smoke-test safe. One premium rescue per turn, two per session.",
    max_calls_per_turn: 1,
    max_calls_per_session: 2,
    max_calls_per_day: 20,
    max_calls_per_month: 500,
    max_daily_usd: 5,
    max_monthly_usd: 50,
    max_input_tokens_per_call: 6000,
    max_output_tokens_per_call: 1024,
  },
  {
    name: "Medium",
    description: "Normal support. Keeps turns bounded but allows a real conversation.",
    max_calls_per_turn: 1,
    max_calls_per_session: 5,
    max_calls_per_day: 40,
    max_calls_per_month: 800,
    max_daily_usd: 10,
    max_monthly_usd: 100,
    max_input_tokens_per_call: 8000,
    max_output_tokens_per_call: 2048,
  },
  {
    name: "High",
    description: "Active debugging. Useful when the local model is offline or weak.",
    max_calls_per_turn: 2,
    max_calls_per_session: 10,
    max_calls_per_day: 80,
    max_calls_per_month: 1200,
    max_daily_usd: 20,
    max_monthly_usd: 200,
    max_input_tokens_per_call: 12000,
    max_output_tokens_per_call: 4096,
  },
];

export const routingStrategies = [
  {
    value: "best_value",
    label: "Best value",
    description: "Rank every enabled route by task fit, capability, cost, and policy. Local wins when it is adequate and policy supports it.",
  },
  {
    value: "prefer_local",
    label: "Prefer local",
    description: "Try enabled local routes first, then cloud overflow if policy and budget allow.",
  },
  {
    value: "prefer_cloud_quality",
    label: "Prefer cloud quality",
    description: "Prefer enabled cloud routes for quality-sensitive work, with local routes as backup.",
  },
  {
    value: "local_only",
    label: "Local only",
    description: "Use only enabled local routes. Cloud routes stay visible but are not selected.",
  },
  {
    value: "cloud_only",
    label: "Cloud only",
    description: "Use only enabled cloud routes. Still requires policy, key, and budget caps.",
  },
];

export const providerName = (provider: ProviderStatus) => provider.model || provider.profile_name || provider.provider_type || provider.role;
export const routeName = (provider: ProviderStatus) => provider.name || provider.profile_name || "";
export const countValue = (value?: number) => `${value ?? 0}`;
export const percentValue = (value?: number) => `${Math.round((value ?? 0) * 1000) / 10}%`;

export const topEntries = (values?: Record<string, number>, limit = 5) => Object.entries(values || {})
  .sort((left, right) => right[1] - left[1])
  .slice(0, limit);

export const responseTimestamp = (response: Response) => {
  const parsed = Date.parse(response.headers.get("date") || "");
  return Number.isFinite(parsed) ? parsed : null;
};

export const responseError = async (response: Response, fallback: string) => {
  try {
    const data = await response.json() as { detail?: unknown };
    if (typeof data.detail === "string" && data.detail.trim()) return data.detail;
  } catch {
    // non-JSON error body
  }
  return fallback;
};

export const reasonLabel = (reason?: string, model?: ProviderStatus) => {
  if (reason === "model_not_available" && ["low_cost", "premium"].includes(model?.cost_tier || "")) {
    return "model ID is not listed by this provider/key";
  }
  const labels: Record<string, string> = {
    missing_api_key: "missing from this API process environment",
    missing_base_url: "cloud base URL is not configured on the API process",
    health_check_failed: "endpoint offline or unreachable",
    model_not_available: "endpoint is up, but this model is not loaded",
    cloud_policy_not_enabled: "cloud/premium policy is off for this status check",
    not_allowed_for_response_policy: "route not allowed for this response policy",
    cloud_tier_disabled: "this cloud tier is disabled",
    route_disabled: "disabled by admin policy",
    circuit_breaker_open: "paused after repeated errors",
    provider_not_built: "provider was not built",
    not_configured: "route is not configured",
    candidate_not_enabled: "candidate route, not enabled by default",
    deployment_disabled: "disabled in profile configuration",
    health_not_checked: "health not checked while route is disabled",
    runtime_unreachable: "runtime endpoint is cold or unreachable",
    runtime_model_not_warm: "runtime is reachable, but this model is not warm",
  };
  return labels[reason || ""] || reason || "ready";
};
