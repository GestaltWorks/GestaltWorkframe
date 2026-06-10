"use client";

import { useState } from "react";
import { clearAdminToken, readAdminToken, writeAdminToken } from "@/lib/adminToken";
import { apiUrl } from "@/lib/api";
import type {
  HandoffPacket,
  ProviderHealth,
  ProviderKeysPayload,
} from "./admin/health/types";
import {
  apiTargetLabel,
  countValue,
  localApiTarget,
  percentValue,
  policyPresets,
  responseError,
  responseTimestamp,
  routingStrategies,
  terminalHref,
} from "./admin/health/constants";
import {
  CollapsibleSection,
  CommandCopy,
  HandoffCard,
  Metric,
  ModelGroup,
  NumberField,
  ProviderBudgetsSection,
  ProviderKeysSection,
  Toggle,
  TopList,
} from "./admin/health/parts";

export default function AdminHealthPanel() {
  const [health, setHealth] = useState<ProviderHealth | null>(null);
  const [token, setToken] = useState(() => readAdminToken());
  const [error, setError] = useState("");
  const [loading, setLoading] = useState("");
  const [saving, setSaving] = useState("");
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);
  const [handoffs, setHandoffs] = useState<HandoffPacket[]>([]);
  const [chatMetricsOpen, setChatMetricsOpen] = useState(false);
  const [handoffsOpen, setHandoffsOpen] = useState(false);
  const [budgetsOpen, setBudgetsOpen] = useState(false);
  const [keysOpen, setKeysOpen] = useState(false);
  const [providerKeys, setProviderKeys] = useState<ProviderKeysPayload | null>(null);
  const [policyOpen, setPolicyOpen] = useState(true);
  const [runbookOpen, setRunbookOpen] = useState(false);
  const [localModelsOpen, setLocalModelsOpen] = useState(false);
  const [cloudModelsOpen, setCloudModelsOpen] = useState(false);
  const [copiedCommand, setCopiedCommand] = useState("");

  const adminHeaders = () => ({ "Content-Type": "application/json", "X-Admin-Token": token.trim() });
  const busy = Boolean(loading || saving);

  const loadHealth = async (forceRefresh = false) => {
    if (!token.trim()) {
      setError("Enter the admin access token to unlock admin health.");
      return;
    }
    setLoading(forceRefresh ? "Running live provider checks..." : "Loading cached health snapshot...");
    setError("");
    try {
      writeAdminToken(token);
      const suffix = forceRefresh ? "?refresh=true" : "";
      const response = await fetch(apiUrl(`/admin/api/health${suffix}`), { cache: "no-store", headers: adminHeaders() });
      if (response.status === 401) throw new Error("Invalid admin access token.");
      if (response.status === 503) throw new Error("Admin access is not configured on the API service.");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setHealth(await response.json() as ProviderHealth);
      if (handoffsOpen) await loadHandoffs();
      setLastUpdatedAt(responseTimestamp(response));
    } catch (err) {
      setHealth(null);
      setError(err instanceof Error ? err.message : "Failed to load admin health");
    } finally {
      setLoading("");
    }
  };

  const loadHandoffs = async () => {
    const response = await fetch(apiUrl("/admin/api/handoffs?limit=12"), { cache: "no-store", headers: adminHeaders() });
    if (!response.ok) throw new Error(await responseError(response, `Handoff fetch failed with HTTP ${response.status}`));
    const data = await response.json() as { packets?: HandoffPacket[] };
    setHandoffs(data.packets || []);
  };

  const refreshHandoffs = () => {
    loadHandoffs().catch((err) => setError(err instanceof Error ? err.message : "Failed to load handoff packets"));
  };

  const loadProviderKeys = async () => {
    const response = await fetch(apiUrl("/admin/api/provider-keys"), { cache: "no-store", headers: adminHeaders() });
    if (!response.ok) throw new Error(await responseError(response, `Provider keys fetch failed HTTP ${response.status}`));
    setProviderKeys(await response.json() as ProviderKeysPayload);
  };

  const setProviderKey = async (provider_id: string, key: string): Promise<string | null> => {
    try {
      const response = await fetch(apiUrl(`/admin/api/provider-keys/${provider_id}`), {
        method: "POST",
        headers: adminHeaders(),
        body: JSON.stringify({ key }),
      });
      if (!response.ok) return await responseError(response, `HTTP ${response.status}`);
      await loadProviderKeys();
      return null;
    } catch (err) {
      return err instanceof Error ? err.message : "Failed to save key";
    }
  };

  const deleteProviderKey = async (provider_id: string) => {
    try {
      await fetch(apiUrl(`/admin/api/provider-keys/${provider_id}`), { method: "DELETE", headers: adminHeaders() });
      await loadProviderKeys();
    } catch {
      // silent -- key may already be gone
    }
  };

  const patchProviderBudget = async (provider_id: string, max_daily_usd: number, max_monthly_usd: number): Promise<string | null> => {
    return patchPolicy({
      provider_budgets: { [provider_id]: { max_daily_usd, max_monthly_usd } },
    });
  };

  const patchPolicy = async (patch: Record<string, unknown>): Promise<string | null> => {
    if (!token.trim()) {
      const msg = "Enter the admin access token before saving policy changes.";
      setError(msg);
      return msg;
    }
    setSaving("Saving policy...");
    setError("");
    try {
      const response = await fetch(apiUrl("/admin/api/policy"), {
        method: "PATCH",
        headers: adminHeaders(),
        body: JSON.stringify(patch),
      });
      if (response.status === 401) throw new Error("Invalid admin access token.");
      if (!response.ok) throw new Error(await responseError(response, `HTTP ${response.status}`));
      setHealth(await response.json() as ProviderHealth);
      setLastUpdatedAt(responseTimestamp(response));
      return null;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to update policy";
      setError(msg);
      return msg;
    } finally {
      setSaving("");
    }
  };

  const models = health?.models || [];
  const localModels = models.filter((model) => !["low_cost", "premium"].includes(model.cost_tier || ""));
  const cloudModels = models.filter((model) => ["low_cost", "premium"].includes(model.cost_tier || ""));
  const policy = health?.policy;
  const cachedModels = models.filter((model) => model.health_cached).length;
  const skippedHealthChecks = models.filter((model) => model.health_checked === false).length;
  const lastUpdatedLabel = lastUpdatedAt ? new Date(lastUpdatedAt).toLocaleTimeString() : "not loaded";
  const chatMetrics = health?.metrics?.chat;
  const copyCommand = async (label: string, command: string) => {
    try {
      await navigator.clipboard.writeText(command);
      setCopiedCommand(label);
    } catch {
      setError("Clipboard access denied. Copy the command text manually.");
    }
  };

  return (
    <>
    <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-brand-gold focus:px-4 focus:py-2 focus:text-brand-dark">Skip to admin health</a>
    <main id="main-content" className="min-h-screen bg-brand-dark px-4 py-8 font-inter text-brand-offwhite sm:px-8">
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="space-y-2">
          <p className="font-rajdhani text-sm uppercase tracking-[0.3em] text-brand-gold-warm/75">Admin health</p>
          <h1 className="font-rajdhani text-4xl font-semibold">Terminal provider status</h1>
          <p className="max-w-3xl text-sm leading-6 text-brand-offwhite/62">
            Admin controls for runtime model routing policy, provider availability, and local model runtime.
          </p>
          <p className="text-xs text-brand-offwhite/42">
            API target: <span className="text-brand-gold-warm">{apiTargetLabel}</span>
          </p>
          <a href={terminalHref} className="inline-flex rounded-xl border border-brand-gold-warm/20 bg-black/25 px-4 py-2 text-sm text-brand-gold-warm transition-colors hover:border-brand-gold">
            Open terminal
          </a>
        </header>

        <section className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-5 text-sm text-brand-offwhite/70">
          <h2 className="font-rajdhani text-2xl text-brand-offwhite">Admin access</h2>
          <div className="mt-3 flex flex-col gap-3 sm:flex-row">
            <label htmlFor="admin-access-token" className="sr-only">Admin access token</label>
            <input
              id="admin-access-token"
              type="password"
              autoComplete="off"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="Admin access token"
              className="min-w-0 flex-1 rounded-xl border border-brand-gold-warm/15 bg-black/35 px-4 py-2 text-brand-offwhite outline-none focus:border-brand-gold"
            />
            <button type="button" disabled={busy} onClick={() => loadHealth(false)} className="rounded-xl bg-brand-gold px-5 py-2 font-semibold text-brand-dark disabled:cursor-wait disabled:opacity-60">{loading ? "Loading..." : "Unlock health"}</button>
            {token.trim() && (
              <button
                type="button"
                onClick={() => {
                  clearAdminToken();
                  setToken("");
                  setHealth(null);
                  setHandoffs([]);
                  setError("");
                }}
                title="Clear the admin token from this browser. Use this on shared machines."
                className="rounded-xl border border-brand-gold-warm/30 px-4 py-2 text-sm text-brand-offwhite/70 hover:border-red-200 hover:text-red-100"
              >
                Sign out admin
              </button>
            )}
          </div>
          <p className="mt-2 text-xs text-brand-offwhite/42">
            {localApiTarget ? (
              <>Use the local admin access token configured for this API process.</>
            ) : (
              <>Production requires the server-side admin access token.</>
            )}
          </p>
        </section>

        {error && <section role="alert" className="rounded-2xl border border-red-300/30 bg-red-950/20 p-4 text-red-100">Health fetch failed: {error}</section>}
        {(loading || saving) && <section role="status" aria-live="polite" className="rounded-2xl border border-brand-gold-warm/20 bg-black/25 p-4 text-brand-gold-warm">{loading || saving}</section>}

        {health && (
          <>
            <section className="grid gap-3 md:grid-cols-4">
              <Metric label="API status" value={health.status} />
              <Metric label="Premium cloud" value={policy?.cloud_spillover_enabled ? "enabled" : "disabled"} />
              <Metric label="Low-cost cloud" value={policy?.low_cost_enabled ? "enabled" : "disabled"} />
              <Metric label="Claude fallback" value={policy?.claude_enabled ? "enabled" : "disabled"} />
              <Metric label="Routes" value={`${models.length}`} />
              <Metric label="Health snapshot" value={`${cachedModels} cached / ${skippedHealthChecks} skipped`} />
              <Metric label="Last updated" value={lastUpdatedLabel} />
              <button type="button" disabled={busy} onClick={() => loadHealth(true)} className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4 text-left text-brand-gold-warm transition-colors hover:border-brand-gold disabled:cursor-wait disabled:opacity-60">
                {loading ? "Checking providers..." : "Force health refresh"}
                <span className="mt-1 block text-xs text-brand-offwhite/42">Live checks may take several seconds when providers are offline.</span>
              </button>
            </section>

            {chatMetrics && (
              <CollapsibleSection
                title="Chat metrics"
                summary={`Started ${chatMetrics.started_at ? new Date(chatMetrics.started_at).toLocaleString() : "unknown"} / ${countValue(chatMetrics.total_turns)} turns`}
                open={chatMetricsOpen}
                onToggle={() => setChatMetricsOpen(!chatMetricsOpen)}
              >
                <p className="text-xs text-brand-offwhite/42">
                  In-memory operational counters since API process start. No prompts, response text, intake answers, IPs, headers, or secrets are included.
                </p>
                <div className="mt-4 grid gap-3 md:grid-cols-4">
                  <Metric label="Chat turns" value={countValue(chatMetrics.total_turns)} />
                  <Metric label="Completed" value={countValue(chatMetrics.completed_turns)} />
                  <Metric label="Failed" value={`${countValue(chatMetrics.failed_turns)} (${percentValue(chatMetrics.failure_rate)})`} />
                  <Metric label="Avg latency" value={`${chatMetrics.avg_duration_ms ?? 0} ms`} />
                  <Metric label="Avg output" value={`${chatMetrics.avg_output_tokens_estimate ?? 0} est. tokens`} />
                  <Metric label="Total output" value={`${chatMetrics.total_output_tokens_estimate ?? 0} est. tokens`} />
                  <Metric label="Cloud turns" value={countValue(chatMetrics.by_route_family?.cloud)} />
                  <Metric label="Local turns" value={countValue(chatMetrics.by_route_family?.local)} />
                </div>
                {health?.generation_concurrency && (
                  <div className="mt-4">
                    <p className="text-xs uppercase tracking-[0.2em] text-brand-offwhite/38">Generation concurrency (live)</p>
                    <div className="mt-2 grid gap-3 md:grid-cols-3">
                      <Metric
                        label="Active total"
                        value={`${health.generation_concurrency.active_total ?? 0} / ${health.generation_concurrency.max_total ?? "?"}`}
                      />
                      <Metric
                        label="Active local"
                        value={`${health.generation_concurrency.active_local ?? 0} / ${health.generation_concurrency.max_local ?? "?"}`}
                      />
                      <Metric
                        label="Active cloud"
                        value={`${health.generation_concurrency.active_cloud ?? 0} / ${health.generation_concurrency.max_cloud ?? "?"}`}
                      />
                    </div>
                  </div>
                )}
                <div className="mt-4 grid gap-3 md:grid-cols-3">
                  <TopList title="Modes" values={chatMetrics.by_mode} />
                  <TopList title="Routes" values={chatMetrics.by_route} />
                  <TopList title="Route family" values={chatMetrics.by_route_family} />
                </div>
              </CollapsibleSection>
            )}

            <CollapsibleSection
              title="Recent handoff packets"
              summary={handoffs.length > 0 ? `${handoffs.length} packet${handoffs.length !== 1 ? "s" : ""} loaded` : "Sanitized contact and intake summaries"}
              open={handoffsOpen}
              onToggle={() => {
                  const opening = !handoffsOpen;
                  setHandoffsOpen(opening);
                  if (opening && handoffs.length === 0) {
                    loadHandoffs().catch((err) => setError(err instanceof Error ? err.message : "Failed to load handoff packets"));
                  }
                }}
            >
                  <div className="mb-4 flex justify-end">
                    <button type="button" disabled={busy} onClick={refreshHandoffs} className="rounded-xl border border-brand-gold-warm/20 bg-black/25 px-4 py-2 text-xs text-brand-gold-warm transition-colors hover:border-brand-gold disabled:cursor-wait disabled:opacity-60">
                      Refresh packets
                    </button>
                  </div>
                  <div className="grid gap-3 lg:grid-cols-2">
                    {handoffs.length ? handoffs.map((packet) => <HandoffCard key={`${packet.source}-${packet.record_id}`} packet={packet} />) : (
                      <p className="rounded-xl border border-brand-gold-warm/15 bg-black/25 p-4 text-xs text-brand-offwhite/38">No handoff packets recorded yet.</p>
                    )}
                  </div>
            </CollapsibleSection>

            <CollapsibleSection
              title="Provider budgets"
              summary={health.cloud_budget?.providers ? `${Object.keys(health.cloud_budget.providers).length} providers tracked` : "Per-provider USD caps and balance"}
              open={budgetsOpen}
              onToggle={() => {
                const opening = !budgetsOpen;
                setBudgetsOpen(opening);
              }}
            >
              {health.cloud_budget?.providers ? (
                <ProviderBudgetsSection
                  providers={health.cloud_budget.providers}
                  disabled={busy}
                  onPatchBudget={patchProviderBudget}
                />
              ) : (
                <p className="text-xs text-brand-offwhite/38">No per-provider budget data in health snapshot. Reload health to fetch.</p>
              )}
            </CollapsibleSection>

            <CollapsibleSection
              title="Provider API keys"
              summary={providerKeys ? `${Object.values(providerKeys.providers).filter((s) => s.has_stored_key || s.has_env_key).length} of ${Object.keys(providerKeys.providers).length} configured` : "Encrypted key store"}
              open={keysOpen}
              onToggle={() => {
                const opening = !keysOpen;
                setKeysOpen(opening);
                if (opening && !providerKeys) {
                  loadProviderKeys().catch((err) => setError(err instanceof Error ? err.message : "Failed to load provider keys"));
                }
              }}
            >
              {providerKeys ? (
                <ProviderKeysSection
                  providers={providerKeys.providers}
                  disabled={busy}
                  onSetKey={setProviderKey}
                  onDeleteKey={(pid) => { deleteProviderKey(pid).catch(() => undefined); }}
                />
              ) : (
                <p className="text-xs text-brand-offwhite/38">Loading provider key status...</p>
              )}
            </CollapsibleSection>

            {policy && (
              <CollapsibleSection
                title="Runtime routing policy"
                summary={`${policy.routing_strategy} / cloud ${policy.cloud_spillover_enabled ? "enabled" : "disabled"}`}
                open={policyOpen}
                onToggle={() => setPolicyOpen(!policyOpen)}
              >
                <div className="mt-4 rounded-xl border border-brand-gold-warm/15 bg-black/25 p-4">
                  <label className="text-xs uppercase tracking-[0.2em] text-brand-offwhite/38" htmlFor="routing-strategy">Routing strategy</label>
                  <select
                    id="routing-strategy"
                    value={policy.routing_strategy}
                    onChange={(event) => patchPolicy({ routing_strategy: event.target.value })}
                    className="mt-2 w-full rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-2 text-brand-offwhite outline-none focus:border-brand-gold"
                  >
                    {routingStrategies.map((strategy) => (
                      <option key={strategy.value} value={strategy.value}>{strategy.label}</option>
                    ))}
                  </select>
                  <p className="mt-2 text-xs text-brand-offwhite/55">
                    {routingStrategies.find((strategy) => strategy.value === policy.routing_strategy)?.description || routingStrategies[0].description}
                  </p>
                  <p className="mt-2 text-xs text-brand-offwhite/38">
                    Admin changes are runtime-only until restart/deploy. Policy saves return cached health for speed. Use Force health refresh when you need a live provider probe.
                  </p>
                </div>
                <div className="mt-4 grid gap-3 md:grid-cols-3">
                  <Toggle label="Enable premium cloud" checked={policy.cloud_spillover_enabled} disabled={busy} onChange={(checked) => patchPolicy({ cloud_spillover_enabled: checked })} />
                  <Toggle label="Low-cost cloud routes" checked={policy.low_cost_enabled} disabled={busy} onChange={(checked) => patchPolicy({ low_cost_enabled: checked, cloud_spillover_enabled: checked ? true : policy.cloud_spillover_enabled })} />
                  <Toggle label="Premium Claude routes" checked={policy.claude_enabled} disabled={busy} onChange={(checked) => patchPolicy({ claude_enabled: checked, cloud_spillover_enabled: checked ? true : policy.cloud_spillover_enabled })} />
                </div>
                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  <NumberField key={`turn-${policy.max_calls_per_turn}`} label="Calls per turn" value={policy.max_calls_per_turn} disabled={busy} onSave={(value) => patchPolicy({ max_calls_per_turn: value })} />
                  <NumberField key={`session-${policy.max_calls_per_session}`} label="Calls per session" value={policy.max_calls_per_session} disabled={busy} onSave={(value) => patchPolicy({ max_calls_per_session: value })} />
                  <NumberField key={`day-${policy.max_calls_per_day}`} label="Calls per day" value={policy.max_calls_per_day} disabled={busy} onSave={(value) => patchPolicy({ max_calls_per_day: value })} />
                  <NumberField key={`month-${policy.max_calls_per_month}`} label="Calls per month" value={policy.max_calls_per_month} disabled={busy} onSave={(value) => patchPolicy({ max_calls_per_month: value })} />
                  <NumberField key={`usd-day-${policy.max_daily_usd}`} label="Daily USD cap" value={policy.max_daily_usd} disabled={busy} onSave={(value) => patchPolicy({ max_daily_usd: value })} />
                  <NumberField key={`usd-month-${policy.max_monthly_usd}`} label="Monthly USD cap" value={policy.max_monthly_usd} disabled={busy} onSave={(value) => patchPolicy({ max_monthly_usd: value })} />
                  <NumberField key={`input-tokens-${policy.max_input_tokens_per_call}`} label="Input tokens per call" value={policy.max_input_tokens_per_call} disabled={busy} onSave={(value) => patchPolicy({ max_input_tokens_per_call: value })} />
                  <NumberField key={`output-tokens-${policy.max_output_tokens_per_call}`} label="Output tokens per call" value={policy.max_output_tokens_per_call} disabled={busy} onSave={(value) => patchPolicy({ max_output_tokens_per_call: value })} />
                </div>
                <div className="mt-4 grid gap-3 md:grid-cols-3">
                  {policyPresets.map((preset) => (
                    <button
                      key={preset.name}
                      type="button"
                      disabled={busy}
                      onClick={() => patchPolicy({
                        cloud_spillover_enabled: true,
                        max_calls_per_turn: preset.max_calls_per_turn,
                        max_calls_per_session: preset.max_calls_per_session,
                        max_calls_per_day: preset.max_calls_per_day,
                        max_calls_per_month: preset.max_calls_per_month,
                        max_daily_usd: preset.max_daily_usd,
                        max_monthly_usd: preset.max_monthly_usd,
                        max_input_tokens_per_call: preset.max_input_tokens_per_call,
                        max_output_tokens_per_call: preset.max_output_tokens_per_call,
                      })}
                      className="rounded-xl border border-brand-gold-warm/15 bg-black/25 p-4 text-left transition-colors hover:border-brand-gold disabled:cursor-wait disabled:opacity-60"
                    >
                      <span className="font-rajdhani text-xl text-brand-gold-warm">{preset.name}</span>
                      <span className="mt-1 block text-xs text-brand-offwhite/55">{preset.description}</span>
                      <span className="mt-2 block text-xs text-brand-offwhite/38">
                        {preset.max_calls_per_turn}/turn / {preset.max_calls_per_session}/session / ${preset.max_daily_usd}/day / {preset.max_output_tokens_per_call} out
                      </span>
                    </button>
                  ))}
                </div>
                <p className="mt-3 text-xs text-brand-offwhite/42">
                  Recommendation: use Low only for short smoke tests, Medium for public testing, and High only when you are actively watching usage. Presets set turn/session/day/month/USD caps and enable the spillover gate.
                </p>
              </CollapsibleSection>
            )}

            <CollapsibleSection
              title="Local model runbook"
              summary="Copy/paste commands for optional local routes. Cloud routes use server-side API keys."
              open={runbookOpen}
              onToggle={() => setRunbookOpen(!runbookOpen)}
            >
              <p className="text-xs leading-5 text-brand-offwhite/55">
                Run these from the app repo on the GPU host. Starting a local route is optional. Cloud routes are driven by API keys in the API process environment, never by browser input.
              </p>
              <CommandCopy label="Start default local" command={'powershell -ExecutionPolicy Bypass -File .\\llm\\start_server.ps1 -Route llama-3.1-8b-q4'} copiedCommand={copiedCommand} onCopy={copyCommand} />
              <CommandCopy label="Start Llama 3.2 3B local" command={'powershell -ExecutionPolicy Bypass -File .\\llm\\start_server.ps1 -Route llama-3.2-3b-q4'} copiedCommand={copiedCommand} onCopy={copyCommand} />
              <CommandCopy label="Start Qwen coder local" command={'powershell -ExecutionPolicy Bypass -File .\\llm\\start_server.ps1 -Route qwen-2.5-coder-7b-q4'} copiedCommand={copiedCommand} onCopy={copyCommand} />
              <CommandCopy label="Stop local llama-server" command={'Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process'} copiedCommand={copiedCommand} onCopy={copyCommand} />
            </CollapsibleSection>

            <ModelGroup title="Local models" models={localModels} open={localModelsOpen} onToggleOpen={() => setLocalModelsOpen(!localModelsOpen)} disabled={busy} onRouteToggle={(name, enabled) => patchPolicy({ routes: { [name]: enabled } })} />
            <ModelGroup title="Cloud / premium models" models={cloudModels} open={cloudModelsOpen} onToggleOpen={() => setCloudModelsOpen(!cloudModelsOpen)} disabled={busy} onRouteToggle={(name, enabled) => patchPolicy({ routes: { [name]: enabled } })} />

          </>
        )}
      </div>
    </main>
    </>
  );
}
