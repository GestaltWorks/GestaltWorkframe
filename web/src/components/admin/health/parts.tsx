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
