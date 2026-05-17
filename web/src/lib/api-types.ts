export type ProviderStatus = {
  name?: string;
  role: string;
  configured: boolean;
  healthy: boolean;
  endpoint_healthy?: boolean;
  model_available?: boolean;
  callable?: boolean;
  blocked_reason?: string;
  provider_type?: string;
  model?: string;
  profile_name?: string;
  cost_tier?: string;
  // Admin-only fields (present in /admin/api/health responses)
  routing_priority?: number;
  admin_enabled?: boolean;
  deployment_status?: string;
  runtime_group?: string;
  runtime_endpoint?: string;
  runtime_warm?: boolean;
  runtime_reachable?: boolean;
  runtime_blocked_reason?: string;
  enabled_by_default?: boolean;
  health_checked?: boolean;
  health_cached?: boolean;
};

export type CloudBudgetStatus = {
  enabled?: boolean;
  configured?: boolean;
  accounting_blocked?: boolean;
  store_ready?: boolean;
};
