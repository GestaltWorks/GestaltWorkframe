import { type ReactNode, useState } from "react";
import type { ProviderStatus } from "@/lib/api-types";
import type { HandoffPacket } from "./types";
import { providerName, reasonLabel, routeName, topEntries } from "./constants";

export function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4"><div className="text-xs uppercase tracking-[0.22em] text-brand-offwhite/38">{label}</div><div className="mt-1 text-brand-gold-warm">{value}</div></div>;
}

export function TopList({ title, values }: { title: string; values?: Record<string, number> }) {
  const entries = topEntries(values);
  return (
    <div className="rounded-xl border border-brand-gold-warm/15 bg-black/25 p-4">
      <h3 className="text-xs uppercase tracking-[0.2em] text-brand-offwhite/38">{title}</h3>
      {entries.length ? (
        <ul className="mt-3 space-y-2">
          {entries.map(([name, count]) => (
            <li key={name} className="flex items-center justify-between gap-3 text-xs">
              <span className="truncate text-brand-offwhite/65">{name}</span>
              <span className="text-brand-gold-warm">{count}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-xs text-brand-offwhite/38">No turns recorded yet.</p>
      )}
    </div>
  );
}

export function CollapsibleSection({ title, summary, open, onToggle, children }: { title: string; summary: string; open: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <section className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 text-sm text-brand-offwhite/70">
      <button type="button" onClick={onToggle} className="flex w-full items-center justify-between px-5 py-4 text-left" aria-expanded={open}>
        <div>
          <h2 className="font-rajdhani text-2xl text-brand-offwhite">{title}</h2>
          <p className="mt-0.5 text-xs text-brand-offwhite/42">{summary}</p>
        </div>
        <span className="ml-4 shrink-0 text-brand-gold-warm/60" aria-hidden="true">{open ? "▲" : "▼"}</span>
      </button>
      {open && <div className="border-t border-brand-gold-warm/15 px-5 pb-5 pt-4">{children}</div>}
    </section>
  );
}

export function CommandCopy({ label, command, copiedCommand, onCopy }: { label: string; command: string; copiedCommand: string; onCopy: (label: string, command: string) => void }) {
  return (
    <div className="mt-3 rounded-xl border border-brand-gold-warm/15 bg-black/25 p-4">
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-xs uppercase tracking-[0.2em] text-brand-offwhite/38">{label}</h3>
        <button type="button" onClick={() => onCopy(label, command)} className="rounded-lg border border-brand-gold-warm/20 px-3 py-1 text-xs text-brand-gold-warm transition-colors hover:border-brand-gold">
          {copiedCommand === label ? "Copied" : "Copy"}
        </button>
      </div>
      <code className="block overflow-x-auto rounded-lg bg-black/35 p-3 text-xs text-brand-offwhite/72">{command}</code>
    </div>
  );
}

export function KeyValueList({ title, entries }: { title: string; entries: [string, string][] }) {
  return (
    <div className="mt-3">
      <h4 className="text-xs uppercase tracking-[0.2em] text-brand-offwhite/38">{title}</h4>
      <dl className="mt-2 space-y-1 text-xs">
        {entries.map(([label, value]) => (
          <div key={`${title}-${label}`} className="grid gap-1 sm:grid-cols-[9rem_1fr]">
            <dt className="text-brand-offwhite/38">{label}</dt>
            <dd className="text-brand-offwhite/65">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function HandoffCard({ packet }: { packet: HandoffPacket }) {
  const fields = packet.fields || [];
  const contactEntries = Object.entries(packet.contact || {});
  return (
    <article className="rounded-xl border border-brand-gold-warm/15 bg-black/25 p-4">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="font-rajdhani text-xl text-brand-gold-warm">{packet.title}</h3>
          <p className="text-xs uppercase tracking-[0.2em] text-brand-offwhite/38">{packet.packet_type} / {packet.source}</p>
        </div>
        <time className="text-xs text-brand-offwhite/38">{new Date(packet.created_at).toLocaleString()}</time>
      </div>
      <p className="mt-3 text-sm leading-6 text-brand-offwhite/72">{packet.summary || "No summary recorded."}</p>
      {contactEntries.length > 0 && <KeyValueList title="Contact" entries={contactEntries} />}
      {fields.length > 0 && <KeyValueList title="Details" entries={fields.map((field) => [field.label, field.value])} />}
      {(packet.next_steps || []).length > 0 && (
        <div className="mt-3">
          <h4 className="text-xs uppercase tracking-[0.2em] text-brand-offwhite/38">Next steps</h4>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-brand-offwhite/58">
            {packet.next_steps?.map((step) => <li key={step}>{step}</li>)}
          </ul>
        </div>
      )}
    </article>
  );
}

export function Toggle({ label, checked, disabled, onChange }: { label: string; checked: boolean; disabled?: boolean; onChange: (checked: boolean) => void }) {
  return <label className="flex items-center justify-between gap-3 rounded-xl border border-brand-gold-warm/15 bg-black/25 px-4 py-3"><span>{label}</span><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} className="h-5 w-5 accent-brand-gold disabled:cursor-wait disabled:opacity-60" /></label>;
}

export function NumberField({ label, value, disabled, onSave }: { label: string; value: number; disabled?: boolean; onSave: (value: number) => void }) {
  const [draft, setDraft] = useState(String(value));
  return <label className="rounded-xl border border-brand-gold-warm/15 bg-black/25 px-4 py-3"><span className="block text-xs uppercase tracking-[0.2em] text-brand-offwhite/38">{label}</span><div className="mt-2 flex gap-2"><input type="number" min="0" value={draft} disabled={disabled} onChange={(event) => setDraft(event.target.value)} className="min-w-0 flex-1 rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-2 text-brand-offwhite outline-none focus:border-brand-gold disabled:cursor-wait disabled:opacity-60" /><button type="button" disabled={disabled} onClick={() => onSave(Number(draft) || 0)} className="rounded-lg bg-brand-gold px-3 py-2 text-brand-dark disabled:cursor-wait disabled:opacity-60">Save</button></div></label>;
}

export function ModelGroup({ title, models, open, onToggleOpen, disabled, onRouteToggle }: { title: string; models: ProviderStatus[]; open: boolean; onToggleOpen: () => void; disabled?: boolean; onRouteToggle: (name: string, enabled: boolean) => void }) {
  const heads = ["Enabled", "Model", "Status", "Group", "Tier", "Role", "Endpoint", "Warm", "Health", "Callable", "Reason"];

  return (
    <section className="overflow-hidden rounded-2xl border border-brand-gold-warm/15 bg-black/25">
      <button type="button" onClick={onToggleOpen} className="flex w-full items-center justify-between border-b border-brand-gold-warm/15 px-4 py-3 text-left" aria-expanded={open}>
        <span>
          <span className="block font-rajdhani text-2xl text-brand-offwhite">{title}</span>
          <span className="mt-0.5 block text-xs text-brand-offwhite/42">{models.length} route{models.length !== 1 ? "s" : ""}</span>
        </span>
        <span className="ml-4 shrink-0 text-brand-gold-warm/60" aria-hidden="true">{open ? "▲" : "▼"}</span>
      </button>
      {open && <div className="overflow-x-auto">
        <table className="w-full min-w-[1040px] text-left text-xs">
          <caption className="sr-only">{title} provider route status and controls</caption>
          <thead className="text-brand-offwhite/42"><tr>{heads.map((head) => <th key={head} className="px-4 py-3 font-normal">{head}</th>)}</tr></thead>
          <tbody>{models.map((model) => (
            <tr key={`${model.profile_name || model.name}-${model.model}`} className="border-t border-brand-gold-warm/10">
              <td className="px-4 py-3"><input type="checkbox" aria-label={`Enable route ${providerName(model)}`} checked={model.admin_enabled !== false} disabled={disabled} onChange={(event) => onRouteToggle(routeName(model), event.target.checked)} className="h-4 w-4 accent-brand-gold disabled:cursor-wait disabled:opacity-60" /></td>
              <td className="px-4 py-3 text-brand-gold-warm">{providerName(model)}</td>
              <td className="px-4 py-3">{model.deployment_status || "active"}{model.enabled_by_default === false ? " / default off" : ""}</td>
              <td className="px-4 py-3">{model.runtime_group || "unknown"}</td>
              <td className="px-4 py-3">{model.cost_tier || "unknown"}</td>
              <td className="px-4 py-3">{model.role}</td>
              <td className="max-w-[16rem] truncate px-4 py-3" title={model.runtime_endpoint || ""}>{model.runtime_endpoint || (model.health_checked ? (model.endpoint_healthy ? "up" : "down") : "not checked")}</td>
              <td className="px-4 py-3">{model.health_checked ? (model.runtime_warm ? "warm" : "cold") : "unknown"}</td>
              <td className="px-4 py-3">{model.health_cached ? "cached" : model.health_checked ? "fresh" : "skipped"}</td>
              <td className="px-4 py-3">{model.callable ? "yes" : "no"}</td>
              <td className="px-4 py-3 text-brand-offwhite/55">{reasonLabel(model.blocked_reason || model.runtime_blocked_reason, model)}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>}
    </section>
  );
}
// ---------------------------------------------------------------------------
// Phase 4: Provider Budget + Keys sections
// ---------------------------------------------------------------------------

import type { ProviderBudgetEntry, ProviderKeyStatus } from "./types";

const PROVIDER_LABELS: Record<string, string> = {
  openrouter: "OpenRouter",
  anthropic: "Anthropic",
  google: "Google",
  openai: "OpenAI",
};

function usd(value: number, decimals = 2) {
  return `$${value.toFixed(decimals)}`;
}

function balanceSourceBadge(source: string) {
  const classes: Record<string, string> = {
    live: "text-green-400",
    cached: "text-brand-gold-warm",
    local_tracking: "text-brand-offwhite/55",
    unavailable: "text-red-400",
  };
  return <span className={`text-xs ${classes[source] ?? "text-brand-offwhite/38"}`}>{source}</span>;
}

export function ProviderBudgetsSection({
  providers,
  disabled,
  onPatchBudget,
}: {
  providers: Record<string, ProviderBudgetEntry>;
  disabled?: boolean;
  onPatchBudget: (provider_id: string, max_daily_usd: number, max_monthly_usd: number) => Promise<string | null>;
}) {
  const entries = Object.entries(providers);
  if (!entries.length) return <p className="text-xs text-brand-offwhite/38">No per-provider budget data available.</p>;
  return (
    <div className="space-y-4">
      <p className="text-xs text-brand-offwhite/42">
        Per-provider USD spend caps and live balance. OpenRouter balance is fetched from the API key; others use local spend tracking.
      </p>
      {entries.map(([pid, entry]) => (
        <ProviderBudgetCard key={pid} pid={pid} entry={entry} disabled={disabled} onPatchBudget={onPatchBudget} />
      ))}
    </div>
  );
}

function ProviderBudgetCard({
  pid,
  entry,
  disabled,
  onPatchBudget,
}: {
  pid: string;
  entry: ProviderBudgetEntry;
  disabled?: boolean;
  onPatchBudget: (provider_id: string, max_daily_usd: number, max_monthly_usd: number) => Promise<string | null>;
}) {
  const [draftDay, setDraftDay] = useState(String(entry.max_daily_usd));
  const [draftMonth, setDraftMonth] = useState(String(entry.max_monthly_usd));
  const [capSaving, setCapSaving] = useState(false);
  const [capError, setCapError] = useState("");
  const bal = entry.balance;
  const dayPct = entry.max_daily_usd > 0 ? Math.min(100, (entry.used?.day_usd ?? 0) / entry.max_daily_usd * 100) : 0;
  return (
    <div className="rounded-xl border border-brand-gold-warm/15 bg-black/25 p-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-rajdhani text-xl text-brand-offwhite">{PROVIDER_LABELS[pid] ?? pid}</h3>
        {!entry.enabled && <span className="rounded-lg border border-brand-gold-warm/20 px-2 py-0.5 text-xs text-brand-offwhite/38">disabled</span>}
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-4">
        <div className="rounded-lg border border-brand-gold-warm/10 bg-black/15 p-3">
          <p className="text-xs uppercase tracking-[0.18em] text-brand-offwhite/38">Day used</p>
          <p className="mt-1 text-brand-gold-warm">{usd(entry.used?.day_usd ?? 0, 4)}</p>
          <p className="text-xs text-brand-offwhite/38">of {usd(entry.max_daily_usd)} cap</p>
          {entry.max_daily_usd > 0 && (
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-brand-gold-warm/10">
              <div className="h-full rounded-full bg-brand-gold-warm" style={{ width: `${dayPct.toFixed(1)}%` }} />
            </div>
          )}
        </div>
        <div className="rounded-lg border border-brand-gold-warm/10 bg-black/15 p-3">
          <p className="text-xs uppercase tracking-[0.18em] text-brand-offwhite/38">Month used</p>
          <p className="mt-1 text-brand-gold-warm">{usd(entry.used?.month_usd ?? 0, 4)}</p>
          <p className="text-xs text-brand-offwhite/38">of {usd(entry.max_monthly_usd)} cap / {entry.used?.month_calls ?? 0} calls</p>
        </div>
        {bal && (
          <div className="rounded-lg border border-brand-gold-warm/10 bg-black/15 p-3">
            <p className="text-xs uppercase tracking-[0.18em] text-brand-offwhite/38">Credit balance {balanceSourceBadge(bal.source)}</p>
            {bal.available ? (
              <>
                <p className="mt-1 text-brand-gold-warm">{usd(bal.remaining_usd, 2)} remaining</p>
                <p className="text-xs text-brand-offwhite/38">{usd(bal.used_usd, 4)} used / {usd(bal.limit_usd)} limit{bal.is_free_tier ? " (free tier)" : ""}</p>
              </>
            ) : (
              <p className="mt-1 text-xs text-red-400">{bal.error || "unavailable"}</p>
            )}
          </div>
        )}
        <div className="rounded-lg border border-brand-gold-warm/10 bg-black/15 p-3">
          <p className="text-xs uppercase tracking-[0.18em] text-brand-offwhite/38">Set caps</p>
          <div className="mt-2 flex flex-col gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-brand-offwhite/38">Daily USD</span>
              <input type="number" min="0" step="0.5" value={draftDay} disabled={disabled || capSaving} onChange={(e) => { setDraftDay(e.target.value); setCapError(""); }} className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-2 py-1 text-xs text-brand-offwhite outline-none focus:border-brand-gold disabled:cursor-wait disabled:opacity-60" />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-brand-offwhite/38">Monthly USD</span>
              <input type="number" min="0" step="1" value={draftMonth} disabled={disabled || capSaving} onChange={(e) => { setDraftMonth(e.target.value); setCapError(""); }} className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-2 py-1 text-xs text-brand-offwhite outline-none focus:border-brand-gold disabled:cursor-wait disabled:opacity-60" />
            </label>
            <button
              type="button"
              disabled={disabled || capSaving}
              onClick={async () => {
                setCapSaving(true);
                setCapError("");
                const err = await onPatchBudget(pid, Number(draftDay) || 0, Number(draftMonth) || 0);
                if (err) setCapError(err);
                setCapSaving(false);
              }}
              className="rounded-lg bg-brand-gold px-3 py-1 text-xs text-brand-dark disabled:cursor-wait disabled:opacity-60"
            >
              {capSaving ? "Saving…" : "Save"}
            </button>
            {capError && <p className="mt-1 text-xs text-red-400">{capError}</p>}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Provider Keys Section
// ---------------------------------------------------------------------------

const SOURCE_LABEL: Record<string, string> = {
  store: "DB (encrypted)",
  env: "env var",
  none: "not configured",
};

const SOURCE_COLOR: Record<string, string> = {
  store: "text-green-400",
  env: "text-brand-gold-warm",
  none: "text-red-400",
};

export function ProviderKeysSection({
  providers,
  disabled,
  onSetKey,
  onDeleteKey,
}: {
  providers: Record<string, ProviderKeyStatus>;
  disabled?: boolean;
  onSetKey: (provider_id: string, key: string) => Promise<string | null>;
  onDeleteKey: (provider_id: string) => void;
}) {
  const entries = Object.entries(providers);
  return (
    <div className="space-y-4">
      <p className="text-xs text-brand-offwhite/42">
        Keys are AES-256-GCM encrypted using your admin token. They are never returned in API responses. Stored keys take precedence over env vars.
      </p>
      {entries.map(([pid, status]) => (
        <ProviderKeyCard key={pid} pid={pid} status={status} disabled={disabled} onSetKey={onSetKey} onDeleteKey={onDeleteKey} />
      ))}
    </div>
  );
}

function ProviderKeyCard({
  pid,
  status,
  disabled,
  onSetKey,
  onDeleteKey,
}: {
  pid: string;
  status: ProviderKeyStatus;
  disabled?: boolean;
  onSetKey: (provider_id: string, key: string) => Promise<string | null>;
  onDeleteKey: (provider_id: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [keyError, setKeyError] = useState("");
  const [testResult, setTestResult] = useState<{ valid: boolean; error?: string } | null>(null);

  const handleSave = async () => {
    if (!draft.trim()) return;
    setSaving(true);
    setKeyError("");
    setTestResult(null);
    const err = await onSetKey(pid, draft.trim());
    setSaving(false);
    if (err) {
      setKeyError(err);
    } else {
      setDraft("");
      setTestResult({ valid: true });
    }
  };

  return (
    <div className="rounded-xl border border-brand-gold-warm/15 bg-black/25 p-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-rajdhani text-xl text-brand-offwhite">{PROVIDER_LABELS[pid] ?? pid}</h3>
        <span className={`text-xs ${SOURCE_COLOR[status.active_source] ?? "text-brand-offwhite/38"}`}>{SOURCE_LABEL[status.active_source] ?? status.active_source}</span>
      </div>
      <p className="mt-1 text-xs text-brand-offwhite/38">
        {status.has_stored_key ? "Stored key is active." : status.has_env_key ? "Using env var; enter a key below to override." : "No key configured."}
      </p>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row">
        <label htmlFor={`provider-key-${pid}`} className="sr-only">API key for {PROVIDER_LABELS[pid] ?? pid}</label>
        <input
          id={`provider-key-${pid}`}
          type="password"
          autoComplete="new-password"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={status.has_stored_key ? "Replace stored key..." : "Enter API key..."}
          disabled={disabled || saving}
          className="min-w-0 flex-1 rounded-xl border border-brand-gold-warm/15 bg-black/35 px-4 py-2 text-sm text-brand-offwhite outline-none focus:border-brand-gold disabled:cursor-wait disabled:opacity-60"
        />
        <button type="button" disabled={disabled || saving || !draft.trim()} onClick={handleSave} className="rounded-xl bg-brand-gold px-5 py-2 text-sm font-semibold text-brand-dark disabled:cursor-wait disabled:opacity-60">
          {saving ? "Saving..." : "Save key"}
        </button>
        {status.has_stored_key && (
          <button type="button" disabled={disabled || saving} onClick={() => { setTestResult(null); onDeleteKey(pid); }} className="rounded-xl border border-red-300/30 px-4 py-2 text-sm text-red-200 hover:border-red-300 hover:text-red-100 disabled:cursor-wait disabled:opacity-60">
            Remove
          </button>
        )}
      </div>
      {keyError && <p className="mt-2 text-xs text-red-400">{keyError}</p>}
      {testResult && <p className={`mt-2 text-xs ${testResult.valid ? "text-green-400" : "text-red-400"}`}>{testResult.valid ? "Key saved and verified." : testResult.error ?? "Key saved but verification failed."}</p>}
    </div>
  );
}
