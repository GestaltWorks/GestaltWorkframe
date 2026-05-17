"use client";

import { useEffect, useRef, useState } from "react";
import { clearAdminToken, readAdminToken, writeAdminToken } from "@/lib/adminToken";
import { apiUrl } from "@/lib/api";
import { AddCustomItemModal } from "@/components/admin/AddCustomItemModal";

type IssueSummary = {
  id: string;
  // Sticky human label set at creation ("0a", "1c", "3"). Replaces the
  // legacy issue_number int. Always read this for display.
  display_label: string;
  // Monotonic ship counter assigned only at successful send. Null for
  // drafts / awaiting / approved / sending / skipped rows.
  ship_number: number | null;
  slug: string;
  subject: string;
  status: string;
  period_start: string;
  period_end: string;
  approved_by: string;
  approved_at: string | null;
  target_send_at: string | null;
  approval_email_sent_at: string | null;
  scheduled_send_at: string | null;
  sent_at: string | null;
  unpublished_at: string | null;
  created_at: string;
  updated_at: string;
  notes: string;
  find_count: number;
};

type IssueDetail = IssueSummary & {
  finds: Array<{
    id: string;
    title: string;
    display_title?: string;
    url: string;
    summary_text: string;
    display_source_name?: string;
    source_name: string;
    content_type: string;
    review_topic: string;
  }>;
  editorial_markdown: string;
  html_preview: string;
  plain_preview: string;
  linkedin_post: string;
};

const STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  awaiting_approval: "Awaiting approval",
  approved: "Approved (scheduled)",
  sending: "Sending…",
  sent: "Sent",
  skipped: "Skipped (no items)",
};

// Status sets that drive button visibility. Keep these in sync with
// the backend state machine in core/newsletter.py.
const EDITABLE_STATUSES = new Set(["draft", "awaiting_approval"]);
const UNPUBLISHABLE_STATUSES = new Set(["sent", "sending", "approved"]);

/** Format a JS Date as "YYYY-MM-DDTHH:mm" in the user's local zone for the
 *  datetime-local input. */
function localInputValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Default schedule the picker pre-fills: now + 30 minutes (server's
 *  DEFAULT_SCHEDULE_DELAY). */
function defaultScheduleString(): string {
  return localInputValue(new Date(Date.now() + 30 * 60 * 1000));
}

/** Human-friendly "Issue {label}" string. Falls back to slug if the
 *  row predates the labeling migration. */
function issueLabel(summary: { display_label?: string; slug: string }): string {
  if (summary.display_label) return `Issue ${summary.display_label}`;
  return summary.slug;
}

export default function AdminNewsletterPanel() {
  const [token, setToken] = useState(() => readAdminToken());
  const [issues, setIssues] = useState<IssueSummary[]>([]);
  const [selected, setSelected] = useState<IssueDetail | null>(null);
  const [editorial, setEditorial] = useState("");
  const [subject, setSubject] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [copiedLinkedIn, setCopiedLinkedIn] = useState(false);
  const [showAddCustom, setShowAddCustom] = useState(false);
  const [scheduleInput, setScheduleInput] = useState(defaultScheduleString());
  const [newIssueTarget, setNewIssueTarget] = useState("");
  const [showNewIssueForm, setShowNewIssueForm] = useState(false);
  // Track whether the editorial form has unsaved changes so we don't
  // silently overwrite the operator's work when they click another
  // issue in the sidebar.
  const [dirty, setDirty] = useState(false);
  // Snapshot of subject/editorial from the last load so a "dirty"
  // computation can compare against the server state, not the
  // operator's draft.
  const lastLoadedRef = useRef<{ subject: string; editorial: string } | null>(null);
  const busy = Boolean(status);

  // Auto-dismiss success notices after 5s so they don't pile up. Error
  // banners stay until cleared by the next action so the operator
  // actually sees them.
  useEffect(() => {
    if (!notice) return;
    const handle = window.setTimeout(() => setNotice(""), 5000);
    return () => window.clearTimeout(handle);
  }, [notice]);

  // Mark the editorial form dirty when subject or editorial diverges
  // from the last-loaded snapshot.
  useEffect(() => {
    if (!lastLoadedRef.current) {
      setDirty(false);
      return;
    }
    setDirty(
      subject !== lastLoadedRef.current.subject ||
        editorial !== lastLoadedRef.current.editorial,
    );
  }, [subject, editorial]);

  const headers = () => ({ "Content-Type": "application/json", "X-Admin-Token": token.trim() });

  const persistToken = () => writeAdminToken(token);

  const signOutAdmin = () => {
    clearAdminToken();
    setToken("");
    setIssues([]);
    setSelected(null);
    lastLoadedRef.current = null;
    setDirty(false);
    setNotice("Admin session cleared. Paste the token again to unlock.");
  };

  const loadIssues = async () => {
    if (!token.trim()) {
      setError("Enter the admin access token to unlock newsletter review.");
      return;
    }
    persistToken();
    setStatus("Loading issues…");
    setError("");
    try {
      const response = await fetch(apiUrl("/admin/api/newsletter/issues"), { cache: "no-store", headers: headers() });
      if (!response.ok) throw new Error(`Issues fetch failed with HTTP ${response.status}`);
      const data = (await response.json()) as { issues?: IssueSummary[] };
      setIssues(data.issues || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Issues fetch failed");
    } finally {
      setStatus("");
    }
  };

  // Internal loader used after destructive actions where we know the
  // current detail view has no unsaved edits (we just performed a
  // mutation that updated the server state).
  const refreshIssueDetail = async (id: string) => {
    setStatus("Refreshing…");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/newsletter/issues/${id}`), { cache: "no-store", headers: headers() });
      if (!response.ok) throw new Error(`Detail fetch failed with HTTP ${response.status}`);
      const data = (await response.json()) as { issue?: IssueDetail };
      if (data.issue) {
        setSelected(data.issue);
        setEditorial(data.issue.editorial_markdown);
        setSubject(data.issue.subject);
        lastLoadedRef.current = {
          subject: data.issue.subject,
          editorial: data.issue.editorial_markdown,
        };
        setDirty(false);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Detail fetch failed");
    } finally {
      setStatus("");
    }
  };

  const loadIssueDetail = async (id: string) => {
    // Guard: don't silently overwrite unsaved editorial / subject when
    // switching between issues. The operator gets to choose.
    if (dirty && selected && selected.id !== id) {
      const proceed = window.confirm(
        "You have unsaved editorial changes on the current issue. " +
          "Switch and discard them?",
      );
      if (!proceed) return;
    }
    await refreshIssueDetail(id);
  };

  const openNewIssueForm = async () => {
    setShowNewIssueForm(true);
    setError("");
    try {
      const response = await fetch(
        apiUrl("/admin/api/newsletter/assignable-issues"),
        { cache: "no-store", headers: headers() },
      );
      if (response.ok) {
        const data = (await response.json()) as { next_default_target_send_at?: string };
        if (data.next_default_target_send_at) {
          setNewIssueTarget(localInputValue(new Date(data.next_default_target_send_at)));
          return;
        }
      }
    } catch {
      // Fall through to the client-side fallback below.
    }
    setNewIssueTarget(localInputValue(new Date(Date.now() + 10 * 24 * 60 * 60 * 1000)));
  };

  const createNewIssue = async () => {
    const parsed = newIssueTarget ? new Date(newIssueTarget) : null;
    if (parsed && Number.isNaN(parsed.getTime())) {
      setError("Could not parse the target send date.");
      return;
    }
    if (parsed && parsed.getTime() <= Date.now()) {
      setError("Target send date must be in the future.");
      return;
    }
    setStatus("Creating new issue…");
    setError("");
    try {
      const body = parsed ? { target_send_at: parsed.toISOString() } : {};
      const response = await fetch(apiUrl("/admin/api/newsletter/issues/new"), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `New issue failed with HTTP ${response.status}`);
      }
      const data = (await response.json()) as { issue?: IssueDetail };
      if (data.issue) {
        setSelected(data.issue);
        setEditorial(data.issue.editorial_markdown);
        setSubject(data.issue.subject);
        lastLoadedRef.current = {
          subject: data.issue.subject,
          editorial: data.issue.editorial_markdown,
        };
        setDirty(false);
        setNotice(
          `${issueLabel(data.issue)} created. Tag items in from /admin/discovery, then write the editorial here.`,
        );
      }
      setShowNewIssueForm(false);
      await loadIssues();
    } catch (err) {
      setError(err instanceof Error ? err.message : "New issue failed");
    } finally {
      setStatus("");
    }
  };

  const removeFindFromIssue = async (findId: string, title: string) => {
    if (!selected) return;
    if (!window.confirm(`Remove "${title}" from ${issueLabel(selected)}?`)) return;
    setStatus("Removing item…");
    setError("");
    try {
      const response = await fetch(
        apiUrl(`/admin/api/discovery/finds/${findId}/assign-issue`),
        {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({ issue_id: null, reviewer: "admin" }),
        },
      );
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Remove failed with HTTP ${response.status}`);
      }
      setNotice("Item removed from this issue.");
      await refreshIssueDetail(selected.id);
      await loadIssues();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Remove failed");
    } finally {
      setStatus("");
    }
  };

  const saveEditorial = async () => {
    if (!selected) return;
    setStatus("Saving editorial…");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/newsletter/issues/${selected.id}/editorial`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ editorial_markdown: editorial, subject }),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Editorial save failed with HTTP ${response.status}`);
      }
      const data = (await response.json()) as { issue?: IssueDetail };
      if (data.issue) {
        setSelected(data.issue);
        lastLoadedRef.current = {
          subject: data.issue.subject,
          editorial: data.issue.editorial_markdown,
        };
        setDirty(false);
      }
      setNotice("Editorial saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Editorial save failed");
    } finally {
      setStatus("");
    }
  };

  const approveAndSchedule = async (sendAt: Date | null) => {
    if (!selected) return;
    // eslint-disable-next-line react-hooks/purity -- click-handler scope, not render
    const nowTs = Date.now();
    if (sendAt && sendAt.getTime() <= nowTs) {
      setError("Scheduled send time must be in the future.");
      return;
    }
    const label = sendAt
      ? `at ${sendAt.toLocaleString()}`
      : "in about 30 minutes (default delay)";
    if (!window.confirm(`Approve "${subject}" and schedule send ${label}?\n\nThe issue will be sent to every active subscriber when the dispatcher fires. Use "Cancel scheduled send" before that time if you need to pull it back.`)) {
      return;
    }
    setStatus("Approving and scheduling…");
    setError("");
    try {
      const body = sendAt ? { scheduled_send_at: sendAt.toISOString() } : {};
      const response = await fetch(apiUrl(`/admin/api/newsletter/issues/${selected.id}/approve`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Approval failed with HTTP ${response.status}`);
      }
      setNotice("Approved. Send is scheduled — the dispatcher picks it up at the scheduled time.");
      await refreshIssueDetail(selected.id);
      await loadIssues();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Approval failed");
    } finally {
      setStatus("");
    }
  };

  const cancelScheduledSend = async () => {
    if (!selected) return;
    if (!window.confirm(`Cancel the scheduled send for "${subject}"?\n\nThis pulls the issue back to "Awaiting approval" so you can edit it again. The dispatcher will not send it.`)) {
      return;
    }
    setStatus("Cancelling scheduled send…");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/newsletter/issues/${selected.id}/cancel-send`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Cancel failed with HTTP ${response.status}`);
      }
      // Reset the schedule picker so a re-approve doesn't reuse the
      // (now stale) prior value.
      setScheduleInput(defaultScheduleString());
      setNotice("Scheduled send cancelled. The issue is back to awaiting approval.");
      await refreshIssueDetail(selected.id);
      await loadIssues();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Cancel failed");
    } finally {
      setStatus("");
    }
  };

  const dispatchDueNow = async () => {
    if (!window.confirm("Run the dispatcher across ALL approved issues whose scheduled time has passed?")) {
      return;
    }
    setStatus("Dispatching due sends…");
    setError("");
    try {
      const response = await fetch(apiUrl("/admin/api/newsletter/dispatch-due"), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Dispatch failed with HTTP ${response.status}`);
      }
      const data = (await response.json()) as { summary?: { dispatched?: number; failed?: number } };
      const dispatched = data.summary?.dispatched ?? 0;
      const failed = data.summary?.failed ?? 0;
      setNotice(`Dispatcher run: ${dispatched} sent, ${failed} failed.`);
      if (selected) await refreshIssueDetail(selected.id);
      await loadIssues();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Dispatch failed");
    } finally {
      setStatus("");
    }
  };

  const deleteIssue = async () => {
    if (!selected) return;
    const label = issueLabel(selected);
    const confirmation = window.prompt(
      `HARD DELETE ${label}?\n\n` +
        `Status: ${STATUS_LABELS[selected.status] || selected.status}\n` +
        `Items tagged: ${selected.find_count}\n\n` +
        `This will:\n` +
        `  - Remove the issue row from the DB (cannot be undone)\n` +
        `  - Purge every delivery record for this issue\n` +
        `  - Revert all tagged finds to neutral (clear FK, clear newsletter_pending, clear published_in_newsletter_at)\n\n` +
        `Type the issue label exactly (${selected.display_label}) to confirm:`,
    );
    if (confirmation !== selected.display_label) {
      if (confirmation !== null) {
        setError(`Delete cancelled: typed value did not match "${selected.display_label}".`);
      }
      return;
    }
    setStatus("Deleting issue…");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/newsletter/issues/${selected.id}`), {
        method: "DELETE",
        headers: headers(),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Delete failed with HTTP ${response.status}`);
      }
      const data = (await response.json()) as {
        deleted?: { finds_reverted?: number; deliveries_purged?: number };
      };
      const reverted = data.deleted?.finds_reverted ?? 0;
      const purged = data.deleted?.deliveries_purged ?? 0;
      setNotice(
        `${label} deleted. Finds reverted: ${reverted}. Deliveries purged: ${purged}.`,
      );
      setSelected(null);
      setEditorial("");
      setSubject("");
      lastLoadedRef.current = null;
      setDirty(false);
      await loadIssues();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setStatus("");
    }
  };

  const unpublishIssue = async () => {
    if (!selected) return;
    const label = issueLabel(selected);
    const message =
      selected.status === "approved"
        ? `Unpublish ${label}?\n\nThis cancels the scheduled send AND hides the issue from the public archive. Status goes back to "Awaiting approval". Subscribers will not receive this issue.`
        : `Unpublish ${label}?\n\nThis hides the issue from the public archive but preserves the delivery audit trail. Sent emails cannot be recalled.`;
    if (!window.confirm(message)) return;
    setStatus("Unpublishing…");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/newsletter/issues/${selected.id}/unpublish`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Unpublish failed with HTTP ${response.status}`);
      }
      setNotice(`${label} unpublished. Hidden from the public archive.`);
      await refreshIssueDetail(selected.id);
      await loadIssues();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unpublish failed");
    } finally {
      setStatus("");
    }
  };

  const copyLinkedIn = async () => {
    if (!selected) return;
    try {
      await navigator.clipboard.writeText(selected.linkedin_post);
      setCopiedLinkedIn(true);
      setTimeout(() => setCopiedLinkedIn(false), 3000);
    } catch {
      setError("Clipboard write blocked. Select the text below and copy manually.");
    }
  };

  const isEditable = Boolean(selected && EDITABLE_STATUSES.has(selected.status));
  const isUnpublishable = Boolean(
    selected && UNPUBLISHABLE_STATUSES.has(selected.status) && !selected.unpublished_at,
  );
  const isSending = selected?.status === "sending";

  return (
    <main id="main-content" className="min-h-screen bg-brand-dark px-4 py-12 text-brand-offwhite sm:px-8 lg:px-12">
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <header>
          <p className="font-rajdhani text-sm uppercase tracking-[0.32em] text-brand-gold-warm/80">Admin newsletter</p>
          <h1 className="mt-4 font-rajdhani text-5xl font-semibold leading-none text-brand-offwhite">Issue review</h1>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-brand-offwhite/65">
            Compose a draft from currently-pending finds, edit the editorial intro, preview the email render, and approve to distribute to the subscriber list. LinkedIn copy is generated automatically; paste it manually for v1.
          </p>
        </header>

        <section className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4 sm:p-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:gap-4">
            <label htmlFor="newsletter-token" className="sr-only">Admin access token</label>
            <input
              id="newsletter-token"
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="Admin access token"
              className="min-w-0 flex-1 rounded-xl border border-brand-gold-warm/15 bg-black/35 px-4 py-2 outline-none focus:border-brand-gold"
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => void loadIssues()}
              className="rounded-xl bg-brand-gold px-5 py-2 font-semibold text-brand-dark disabled:opacity-60"
            >
              Unlock &amp; refresh
            </button>
            {token.trim() && (
              <button
                type="button"
                onClick={signOutAdmin}
                className="rounded-xl border border-brand-gold-warm/30 px-4 py-2 text-sm text-brand-offwhite/70 hover:border-red-200 hover:text-red-100"
              >
                Sign out admin
              </button>
            )}
          </div>
        </section>

        {error && (
          <section role="alert" className="flex items-start justify-between gap-3 rounded-2xl border border-red-300/30 bg-red-950/20 p-4 text-red-100">
            <span>{error}</span>
            <button
              type="button"
              onClick={() => setError("")}
              className="rounded-full border border-red-300/30 px-2 text-xs text-red-100 hover:border-red-200"
              aria-label="Dismiss error"
            >
              ✕
            </button>
          </section>
        )}
        {notice && <section role="status" className="rounded-2xl border border-brand-sage/40 bg-brand-sage/15 p-4 text-brand-offwhite/80">{notice}</section>}
        {status && (
          <section role="status" className="flex items-center gap-3 rounded-2xl border border-brand-gold-warm/20 bg-black/25 p-4 text-brand-gold-warm">
            <span aria-hidden className="inline-block h-3 w-3 animate-pulse rounded-full bg-brand-gold-warm" />
            {status}
          </section>
        )}

        <section className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4 sm:p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="font-rajdhani text-2xl text-brand-offwhite">Issues</h2>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={busy || !token.trim()}
                onClick={() => void openNewIssueForm()}
                className="rounded-xl bg-brand-gold px-4 py-2 text-sm font-semibold text-brand-dark hover:bg-brand-gold-mid disabled:opacity-60"
              >
                + New issue
              </button>
              <button
                type="button"
                disabled={busy || !token.trim()}
                onClick={() => setShowAddCustom(true)}
                className="rounded-xl border border-brand-gold-warm/30 px-4 py-2 text-sm text-brand-gold-warm hover:border-brand-gold disabled:opacity-60"
              >
                Add custom URL
              </button>
              <button
                type="button"
                disabled={busy || !token.trim()}
                onClick={() => void dispatchDueNow()}
                title="Send every approved issue whose scheduled time has passed (global, not scoped to the selected issue)."
                className="rounded-xl border border-brand-gold-warm/30 px-4 py-2 text-sm text-brand-gold-warm hover:border-brand-gold disabled:opacity-60"
              >
                Run dispatcher (global)
              </button>
            </div>
          </div>
          {showNewIssueForm && (
            <div className="mt-4 grid gap-3 rounded-xl border border-brand-gold-warm/20 bg-black/30 p-3">
              <label className="grid gap-1 text-xs uppercase tracking-wider text-brand-offwhite/55">
                Target send (local time)
                <input
                  type="datetime-local"
                  value={newIssueTarget}
                  onChange={(event) => setNewIssueTarget(event.target.value)}
                  className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-2 text-sm text-brand-offwhite outline-none focus:border-brand-gold"
                />
              </label>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void createNewIssue()}
                  className="rounded-xl bg-brand-gold px-4 py-2 text-sm font-semibold text-brand-dark hover:bg-brand-gold-mid disabled:opacity-60"
                >
                  Create draft
                </button>
                <button
                  type="button"
                  onClick={() => setShowNewIssueForm(false)}
                  className="rounded-xl border border-brand-gold-warm/30 px-4 py-2 text-sm text-brand-offwhite/70 hover:border-brand-gold-warm/60"
                >
                  Cancel
                </button>
              </div>
              <p className="text-xs text-brand-offwhite/55">
                Default is 10 days after the most recent sent issue, or today + 10 days on a fresh install.
                Drafts get a sticky letter label ({(() => { const maxShip = issues.reduce<number | null>((m, i) => i.ship_number != null && (m == null || i.ship_number > m) ? i.ship_number : m, null); return maxShip != null ? `next: "${maxShip}a"` : '"0a", "0b"…'; })()}); the integer ship number is assigned at successful send.
              </p>
            </div>
          )}
          <p className="mt-3 text-xs text-brand-offwhite/55">
            Tag items into a draft from <code className="rounded bg-black/40 px-1 py-0.5">/admin/discovery</code>{" "}
            using the per-find issue dropdown, or paste a custom URL with the button above. Write the
            editorial in the Compose view to the right.
          </p>
        </section>

        {showAddCustom && (
          <AddCustomItemModal
            onClose={() => setShowAddCustom(false)}
            onCreated={() => {
              setNotice("Custom item added to the newsletter queue.");
            }}
            defaultQueueForNewsletter={true}
          />
        )}

        <section className="grid gap-6 lg:grid-cols-[1fr_2fr]">
          <aside className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4">
            <h2 className="font-rajdhani text-xl text-brand-offwhite">Issues</h2>
            <p className="mt-1 text-xs text-brand-offwhite/55">{issues.length} {issues.length === 1 ? "issue" : "issues"} total</p>
            <ul className="mt-3 grid gap-2">
              {issues.map((issue) => {
                const headline = issue.display_label
                  ? `Issue ${issue.display_label}`
                  : issue.subject || issue.slug;
                const subhead = issue.display_label ? (issue.subject || issue.slug) : "";
                const targetLabel = issue.target_send_at
                  ? new Date(issue.target_send_at).toLocaleDateString()
                  : issue.scheduled_send_at
                    ? new Date(issue.scheduled_send_at).toLocaleDateString()
                    : new Date(issue.period_end).toLocaleDateString();
                return (
                  <li key={issue.id}>
                    <button
                      type="button"
                      onClick={() => void loadIssueDetail(issue.id)}
                      className={`w-full rounded-xl border px-3 py-2 text-left text-sm transition-colors ${
                        selected?.id === issue.id
                          ? "border-brand-gold bg-brand-gold/10 text-brand-gold"
                          : "border-brand-gold-warm/15 text-brand-offwhite/80 hover:border-brand-gold-warm/40"
                      } ${issue.unpublished_at ? "opacity-60" : ""}`}
                    >
                      <p className="font-rajdhani text-base font-semibold">
                        {headline}
                        {issue.unpublished_at && (
                          <span className="ml-2 text-[10px] uppercase tracking-wider text-red-300/80">unpublished</span>
                        )}
                      </p>
                      {subhead && (
                        <p className="text-xs text-brand-offwhite/70 line-clamp-1">{subhead}</p>
                      )}
                      <p className="text-[10px] uppercase tracking-wider text-brand-offwhite/55">
                        {STATUS_LABELS[issue.status] || issue.status} · {issue.find_count} {issue.find_count === 1 ? "item" : "items"}
                      </p>
                      <p className="text-[10px] text-brand-offwhite/40">
                        target send {targetLabel}
                      </p>
                    </button>
                  </li>
                );
              })}
              {!issues.length && <li className="text-xs text-brand-offwhite/55">No issues yet. Click + New issue above to bootstrap the first one.</li>}
            </ul>
          </aside>

          <article className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4">
            {!selected && <p className="text-sm text-brand-offwhite/55">Select an issue from the list, or click + New issue to bootstrap one.</p>}
            {selected && (
              <div className="grid gap-4">
                <header className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-xs uppercase tracking-wider text-brand-gold-warm/70">
                      {STATUS_LABELS[selected.status] || selected.status}
                      {selected.unpublished_at && (
                        <span className="ml-2 rounded-full border border-red-300/40 px-2 py-0.5 text-[10px] text-red-200">unpublished</span>
                      )}
                      {dirty && (
                        <span className="ml-2 rounded-full border border-brand-gold/40 px-2 py-0.5 text-[10px] text-brand-gold">unsaved changes</span>
                      )}
                    </p>
                    <h2 className="mt-1 font-rajdhani text-2xl text-brand-offwhite">
                      Compose {issueLabel(selected)}
                      {selected.subject && (
                        <span className="ml-2 text-base font-normal text-brand-offwhite/70">· {selected.subject}</span>
                      )}
                    </h2>
                    <p className="mt-1 text-xs text-brand-offwhite/55">
                      {selected.find_count} {selected.find_count === 1 ? "item" : "items"}
                      {selected.target_send_at && (
                        <> · target send {new Date(selected.target_send_at).toLocaleString()}</>
                      )}
                      {selected.sent_at && <> · sent {new Date(selected.sent_at).toLocaleString()}</>}
                      {selected.ship_number != null && <> · ship #{selected.ship_number}</>}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {isUnpublishable && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void unpublishIssue()}
                        className="rounded-xl border border-brand-gold-warm/40 px-3 py-1.5 text-xs text-brand-gold-warm hover:border-red-200 hover:text-red-100 disabled:opacity-60"
                        title="Hide from public archive; preserves audit trail."
                      >
                        Unpublish
                      </button>
                    )}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void deleteIssue()}
                      className="rounded-xl border border-red-300/40 px-3 py-1.5 text-xs font-semibold text-red-200 hover:border-red-200 hover:bg-red-950/40 disabled:opacity-60"
                      title="Hard delete: removes the issue, purges deliveries, reverts tagged finds. Cannot be undone."
                    >
                      Delete
                    </button>
                  </div>
                </header>

                {isSending && (
                  <p className="rounded-xl border border-brand-gold-warm/30 bg-black/30 px-3 py-2 text-xs text-brand-gold-warm">
                    Dispatcher is currently sending this issue. Controls are disabled until the send loop finishes (status flips to <code>sent</code>) or operations recover from a wedge.
                  </p>
                )}

                {isEditable && (
                  <details open className="rounded-xl border border-brand-gold-warm/20 bg-black/20">
                    <summary className="cursor-pointer p-3 text-sm font-semibold text-brand-gold-warm">
                      Items in this issue ({selected.find_count})
                    </summary>
                    <ul className="grid gap-2 border-t border-brand-gold-warm/10 p-3">
                      {selected.finds.length === 0 && (
                        <li className="text-xs text-brand-offwhite/55">
                          No items tagged yet. Open <code className="rounded bg-black/40 px-1">/admin/discovery</code>, find the items you want, and pick &quot;{issueLabel(selected)}&quot; from the per-find newsletter dropdown.
                        </li>
                      )}
                      {selected.finds.map((find) => (
                        <li key={find.id} className="flex flex-wrap items-start justify-between gap-2 rounded-lg border border-brand-gold-warm/10 bg-black/20 px-3 py-2">
                          <div className="min-w-0 flex-1">
                            <p className="truncate text-sm text-brand-offwhite/85">{find.display_title || find.title}</p>
                            <p className="text-xs text-brand-offwhite/55">{find.display_source_name || find.source_name}</p>
                          </div>
                          <div className="flex items-center gap-2">
                            {find.url && (
                              <a
                                href={find.url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="rounded-full border border-brand-gold-warm/30 px-2.5 py-1 text-[10px] text-brand-offwhite/70 hover:border-brand-gold hover:text-brand-gold"
                              >
                                Open ↗
                              </a>
                            )}
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => void removeFindFromIssue(find.id, find.display_title || find.title)}
                              className="rounded-full border border-red-300/30 px-2.5 py-1 text-[10px] font-semibold text-red-200 hover:border-red-200 hover:text-red-100 disabled:opacity-60"
                            >
                              Remove
                            </button>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}

                {isEditable && (
                  <div className="grid gap-3">
                    <label className="grid gap-1 text-xs uppercase tracking-wider text-brand-offwhite/55">
                      Subject
                      <input
                        type="text"
                        value={subject}
                        onChange={(event) => setSubject(event.target.value)}
                        className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-2 text-sm text-brand-offwhite outline-none focus:border-brand-gold"
                      />
                    </label>
                    <label className="grid gap-1 text-xs uppercase tracking-wider text-brand-offwhite/55">
                      Editorial (markdown subset: **bold**, *italic*, [link](url), blank line for paragraph break)
                      <textarea
                        value={editorial}
                        onChange={(event) => setEditorial(event.target.value)}
                        rows={8}
                        className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-2 font-mono text-sm text-brand-offwhite outline-none focus:border-brand-gold"
                      />
                    </label>
                    <button
                      type="button"
                      disabled={busy || !dirty}
                      onClick={() => void saveEditorial()}
                      className="self-start rounded-xl border border-brand-gold-warm/40 px-4 py-2 text-sm text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold disabled:opacity-60"
                    >
                      {dirty ? "Save editorial" : "Editorial saved"}
                    </button>

                    <div className="mt-2 grid gap-3 rounded-xl border border-brand-gold-warm/20 bg-black/30 p-3">
                      <p className="font-rajdhani text-sm uppercase tracking-[0.22em] text-brand-gold-warm/80">
                        Approve &amp; schedule
                      </p>
                      <label className="grid gap-1 text-xs uppercase tracking-wider text-brand-offwhite/55">
                        Send at (local time)
                        <input
                          type="datetime-local"
                          value={scheduleInput}
                          onChange={(event) => setScheduleInput(event.target.value)}
                          className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-2 text-sm text-brand-offwhite outline-none focus:border-brand-gold"
                        />
                      </label>
                      <div className="flex flex-wrap items-center gap-2">
                        <button
                          type="button"
                          disabled={busy || dirty}
                          onClick={() => {
                            const parsed = scheduleInput ? new Date(scheduleInput) : null;
                            if (parsed && Number.isNaN(parsed.getTime())) {
                              setError("Could not parse the scheduled send time.");
                              return;
                            }
                            void approveAndSchedule(parsed);
                          }}
                          title={dirty ? "Save editorial first" : "Approve and schedule for the picker time"}
                          className="rounded-xl bg-brand-gold px-4 py-2 text-sm font-semibold text-brand-dark hover:bg-brand-gold-mid disabled:opacity-60"
                        >
                          Approve &amp; schedule send
                        </button>
                        <button
                          type="button"
                          disabled={busy || dirty}
                          onClick={() => void approveAndSchedule(null)}
                          className="rounded-xl border border-brand-gold-warm/40 px-4 py-2 text-sm text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold disabled:opacity-60"
                          title={dirty ? "Save editorial first" : "Default 30-minute cancel window"}
                        >
                          Approve (default delay)
                        </button>
                      </div>
                      <p className="text-xs text-brand-offwhite/55">
                        Default delay is 30 minutes. The dispatcher fires hourly (cron) plus the global
                        &quot;Run dispatcher (global)&quot; button in the toolbar; pick a specific local
                        time if you want a precise send slot.
                      </p>
                    </div>
                  </div>
                )}

                {selected.status === "approved" && (
                  <div className="grid gap-3 rounded-xl border border-brand-gold-warm/20 bg-black/30 p-3">
                    <p className="font-rajdhani text-sm uppercase tracking-[0.22em] text-brand-gold-warm/80">
                      Scheduled send
                    </p>
                    <p className="text-sm text-brand-offwhite/85">
                      {selected.scheduled_send_at
                        ? `Will send at ${new Date(selected.scheduled_send_at).toLocaleString()}.`
                        : "Approved without a scheduled time."}
                    </p>
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void cancelScheduledSend()}
                        className="rounded-xl border border-brand-gold-warm/40 px-4 py-2 text-sm text-brand-gold-warm hover:border-red-200 hover:text-red-100 disabled:opacity-60"
                      >
                        Cancel scheduled send
                      </button>
                    </div>
                  </div>
                )}

                <details open className="rounded-xl border border-brand-gold-warm/15 bg-white">
                  <summary className="cursor-pointer p-3 text-sm font-semibold text-brand-dark/85">HTML preview (what subscribers will see)</summary>
                  <div className="border-t border-brand-gold-warm/15">
                    <iframe
                      title="Newsletter HTML preview"
                      srcDoc={selected.html_preview}
                      sandbox=""
                      className="h-[640px] w-full"
                    />
                  </div>
                </details>

                <details className="rounded-xl border border-brand-gold-warm/15 bg-black/15">
                  <summary className="cursor-pointer p-3 text-sm font-semibold text-brand-gold-warm">Plain-text version</summary>
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words p-3 text-xs text-brand-offwhite/75">{selected.plain_preview}</pre>
                </details>

                <details className="rounded-xl border border-brand-gold-warm/15 bg-black/15">
                  <summary className="cursor-pointer p-3 text-sm font-semibold text-brand-gold-warm">LinkedIn post (copy &amp; paste)</summary>
                  <div className="p-3">
                    <button
                      type="button"
                      onClick={() => void copyLinkedIn()}
                      className="mb-2 rounded-lg border border-brand-gold-warm/30 px-3 py-1 text-xs text-brand-gold-warm hover:border-brand-gold"
                    >
                      {copiedLinkedIn ? "Copied!" : "Copy for LinkedIn"}
                    </button>
                    <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded bg-black/30 p-3 text-xs text-brand-offwhite/75">{selected.linkedin_post}</pre>
                  </div>
                </details>

                {!isEditable && (
                  <details className="rounded-xl border border-brand-gold-warm/15 bg-black/15">
                    <summary className="cursor-pointer p-3 text-sm font-semibold text-brand-gold-warm">Included finds ({selected.find_count})</summary>
                    <ul className="border-t border-brand-gold-warm/10">
                      {selected.finds.map((find) => (
                        <li key={find.id} className="border-b border-brand-gold-warm/10 px-3 py-2 last:border-b-0">
                          <p className="text-sm text-brand-offwhite/85">{find.display_title || find.title}</p>
                          <p className="text-xs text-brand-offwhite/55">{find.display_source_name || find.source_name}</p>
                        </li>
                      ))}
                    </ul>
                  </details>
                )}

                {selected.notes && (
                  <details className="rounded-xl border border-brand-gold-warm/15 bg-black/15">
                    <summary className="cursor-pointer p-3 text-xs text-brand-offwhite/55">Operator notes / dispatch audit</summary>
                    <pre className="overflow-x-auto whitespace-pre-wrap break-words p-3 text-[11px] text-brand-offwhite/55">{selected.notes}</pre>
                  </details>
                )}
              </div>
            )}
          </article>
        </section>
      </div>
    </main>
  );
}
