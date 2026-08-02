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
  // True when the router built this route from the live catalog rather than
  // from llm/profiles.json. Its name is `catalog:<catalog id>`. Shown so an
  // operator can tell a route nobody typed into a file from one somebody did.
  catalog_derived?: boolean;
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
