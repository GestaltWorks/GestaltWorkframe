"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { clearAdminToken, readAdminToken, writeAdminToken } from "@/lib/adminToken";
import { apiUrl } from "@/lib/api";
import { safeHref } from "@/lib/safeHref";
import { AddCustomItemModal } from "@/components/admin/AddCustomItemModal";

// ---------------------------------------------------------------------------
// Types (mirror the Phase A backend shapes from core/discovery_queue.py).
// ---------------------------------------------------------------------------

type DiscoveryFind = {
  id: string;
  title: string;
  display_title?: string;
  display_caption?: string;
  url: string;
  summary_text: string;
  source_name: string;
  display_source_name?: string;
  watch_type: string;
  finding_type: string;
  importance_signal: string;
  status: string;
  decision_notes: string;
  first_seen_at: string;
  last_seen_at: string;
  ingested_into_chroma: boolean;
  published_to_library_repo: boolean;
  library_target_path: string;
  library_file_url: string;
  library_promotion_error: string;
  promoted_at: string | null;
  featured: boolean;
  featured_at: string | null;
  // Phase 2 curation split. ticker_featured is deprecated (the public
  // ticker now reads from published_in_newsletter_at) but kept here so
  // the type still matches the backend serializer until the legacy
  // mirror is removed.
  ticker_featured: boolean;
  ticker_featured_at: string | null;
  newsletter_pending: boolean;
  // Per-issue assignment FK. When set, the find is tagged for a
  // specific newsletter issue and shows up in that issue's Compose
  // view. Null means unassigned.
  newsletter_issue_id: string | null;
  dismissed: boolean;
  // Stamped automatically when a NewsletterIssue containing this find
  // is approved + sent. Drives the public ticker. UI uses this to show
  // a "published" badge.
  published_in_newsletter_at: string | null;
  // Category rollup fields. For github_repo_artifact_scan finds the
  // backend writes one row per top-level directory; `category` carries
  // the directory name and `child_count` the number of leaf files
  // aggregated. Non-rollup sources leave these as "" / 0.
  category: string;
  child_count: number;
  last_upstream_updated_at: string | null;
  source_featured: boolean;
  review_topic: string;
  review_tags: string[];
  content_type: string;
  event_kind: string;
  review_lane: string;
  approval_required: boolean;
  publish_score: number;
  ingest_score: number;
  newsletter_score: number;
  newsletter_candidate: boolean;
  routine_update: boolean;
  suggested_action: string;
};

type AssignableIssue = {
  id: string;
  // Sticky human label set at creation ("0a", "1c", "3"). Display
  // this; ship_number is null for unsent issues.
  display_label: string;
  ship_number: number | null;
  subject: string;
  status: string;
  target_send_at: string | null;
  scheduled_send_at: string | null;
};

type DiscoverySource = {
  id: string;
  name: string;
  watch_type: string;
  target: string;
  refresh_interval_seconds: number;
  importance_floor: string;
  active: boolean;
  featured?: boolean;
  last_status: string;
  last_error: string;
  notes: string;
  last_polled_at: string | null;
  updated_at: string | null;
  consecutive_failures: number;
};

type SourceWithActivity = {
  id: string;
  name: string;
  display_name?: string;
  watch_type: string;
  target: string;
  featured: boolean;
  active: boolean;
  last_polled_at: string | null;
  last_activity_at: string | null;
  total_finds: number;
  notable_finds: number;
  featured_finds: number;
  sample_titles: string[];
  recent_finds: {
    id: string;
    title: string;
    display_title?: string;
    url: string;
    finding_type: string;
    status: string;
    featured: boolean;
    // Phase 2 curation flags echoed from the find row.
    ticker_featured?: boolean;
    newsletter_pending?: boolean;
    dismissed?: boolean;
    importance_signal: string;
    last_seen_at: string;
  }[];
};

type Tab = "sources" | "items" | "new_sources" | "featured";

const TAB_ORDER: Tab[] = ["sources", "items", "new_sources", "featured"];
const TAB_LABELS: Record<Tab, string> = {
  sources: "Sources & activity",
  items: "Recent items",
  new_sources: "New source candidates",
  featured: "Featured",
};
const TAB_HELP: Record<Tab, string> = {
  sources: "Approved sources, rolled up by activity. Feature a source to spotlight it permanently in Strong Signals. Each source shows a New content count when it has auto-indexed items you haven't curated yet. Click View items to drill in, filter by date or topic, and queue items for the next newsletter or dismiss them.",
  items: "Auto-indexed first-class events (releases, posts, new repos). Per item: Feature in ticker (30-day rolling), pick a newsletter issue to tag it into via the Newsletter dropdown, or Dismiss (stop counting against the New content badge).",
  new_sources: "The one remaining approval gate: candidates from scout / tracked-creator new-repo events.",
  featured: "Currently featured sources and items already published to a newsletter. Unfeature a source to remove it from Strong Signals. The public ticker is downstream of newsletter sends, not toggled here.",
};

// ---------------------------------------------------------------------------
// Source-add form (kept from the prior panel; new sources are added manually).
// ---------------------------------------------------------------------------

const defaultSourceForm = {
  name: "",
  watch_type: "github_repo_watch",
  target: "",
  description: "",
  refresh_cadence: "daily",
  canonical_url: "",
  provenance: "Public source reviewed by operator.",
  license_notes: "Link and summarize only until reviewed.",
  attribution: "Upstream source owner.",
  trust_tier: "public_reviewed_source",
  display_policy: "public_after_source_review",
  retrieval_policy: "approved_for_grounded_retrieval_after_review",
  curriculum_policy: "not_approved_by_default",
  agent_access_policy: "read_only_public_source_only",
  secret_handling: "no_credentials_required",
  importance_floor: "normal",
  active: true,
  notes: "",
};

type SourceFormKey = keyof typeof defaultSourceForm;

const sourceFieldLabels: Record<SourceFormKey, string> = {
  name: "Source name",
  watch_type: "Watch type",
  target: "Target URL, account, topic, or query",
  description: "Description",
  refresh_cadence: "Refresh cadence",
  canonical_url: "Canonical source URL",
  provenance: "Provenance",
  license_notes: "License notes",
  attribution: "Attribution",
  trust_tier: "Trust tier",
  display_policy: "Display policy",
  retrieval_policy: "Retrieval policy",
  curriculum_policy: "Curriculum policy",
  agent_access_policy: "Agent access policy",
  secret_handling: "Secret handling",
  importance_floor: "Importance floor",
  active: "Active",
  notes: "Operator notes",
};

const sourceFieldGroups: { legend: string; fields: SourceFormKey[] }[] = [
  { legend: "Source identity", fields: ["name", "watch_type", "target", "description", "canonical_url"] },
  { legend: "Policy", fields: ["provenance", "license_notes", "attribution", "trust_tier", "display_policy", "retrieval_policy", "curriculum_policy", "agent_access_policy", "secret_handling"] },
  { legend: "Cadence and notes", fields: ["refresh_cadence", "importance_floor", "notes"] },
];

const watchTypeOptions = [
  { value: "all", label: "All types" },
  { value: "github_repo_watch", label: "GitHub repo (releases)" },
  { value: "github_repo_artifact_scan", label: "GitHub repo (file changes)" },
  { value: "github_user_watch", label: "GitHub user / org" },
  { value: "github_topic_watch", label: "GitHub topic" },
  { value: "rss_watch", label: "RSS feed" },
  { value: "subreddit_watch", label: "Subreddit" },
  { value: "youtube_feed_watch", label: "YouTube feed" },
  { value: "web_diff_watch", label: "Web diff" },
  { value: "brave_search_watch", label: "Brave saved search" },
  { value: "discovery_scout", label: "Scout (candidate sources)" },
];

// ---------------------------------------------------------------------------

export default function AdminDiscoveryPanel() {
  const [token, setToken] = useState(() => readAdminToken());
  const [tab, setTab] = useState<Tab>("sources");
  const [sourcesWithActivity, setSourcesWithActivity] = useState<SourceWithActivity[]>([]);
  const [items, setItems] = useState<DiscoveryFind[]>([]);
  const [newSourceCandidates, setNewSourceCandidates] = useState<DiscoveryFind[]>([]);
  const [allSources, setAllSources] = useState<DiscoverySource[]>([]);
  const [sourceForm, setSourceForm] = useState(defaultSourceForm);
  const [showAddForm, setShowAddForm] = useState(false);
  const [watchTypeFilter, setWatchTypeFilter] = useState<string>("all");
  const [sourceNameFilter, setSourceNameFilter] = useState<string>("");
  const [expandedSourceId, setExpandedSourceId] = useState<string | null>(null);
  // Phase 2: per-source uncurated counts drive the "New content (N)"
  // badge on each source card. Fetched alongside the sources rollup.
  const [uncuratedCounts, setUncuratedCounts] = useState<Record<string, number>>({});
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [notice, setNotice] = useState("");
  const [showAddCustom, setShowAddCustom] = useState(false);
  // Open newsletter issues the operator can tag finds onto. Fetched
  // once at unlock + after any assign action so the dropdowns on
  // every find row stay current.
  const [assignableIssues, setAssignableIssues] = useState<AssignableIssue[]>([]);
  const busy = Boolean(status);

  const headers = () => ({ "Content-Type": "application/json", "X-Admin-Token": token.trim() });

  const persistToken = () => {
    writeAdminToken(token);
  };

  const signOutAdmin = () => {
    clearAdminToken();
    setToken("");
    setSourcesWithActivity([]);
    setItems([]);
    setNewSourceCandidates([]);
    setAllSources([]);
    setError("");
    setNotice("Admin session cleared. Paste the token again to unlock.");
  };

  const load = async (nextTab: Tab = tab) => {
    if (!token.trim()) {
      setError("Enter the admin access token to unlock discovery review.");
      return;
    }
    persistToken();
    setStatus(`Loading ${TAB_LABELS[nextTab].toLowerCase()}...`);
    setError("");
    try {
      if (nextTab === "sources" || nextTab === "featured") {
        // Featured tab reads from the same rollup, then filters client-side.
        const response = await fetch(apiUrl("/admin/api/discovery/sources-with-activity?window_days=30&limit=500"), {
          cache: "no-store",
          headers: headers(),
        });
        if (!response.ok) throw new Error(`Sources fetch failed with HTTP ${response.status}`);
        const data = (await response.json()) as { sources?: SourceWithActivity[] };
        setSourcesWithActivity(data.sources || []);
        // Fetch uncurated counts in parallel so the New content badge is fresh.
        try {
          const countsResp = await fetch(apiUrl("/admin/api/discovery/uncurated-counts"), {
            cache: "no-store",
            headers: headers(),
          });
          if (countsResp.ok) {
            const cdata = (await countsResp.json()) as { counts?: Record<string, number> };
            setUncuratedCounts(cdata.counts || {});
          }
        } catch {
          // Non-fatal: badge just won't render this cycle.
        }
      }
      if (nextTab === "items" || nextTab === "featured") {
        const response = await fetch(
          apiUrl("/admin/api/discovery/finds?status=auto_indexed&limit=250"),
          { cache: "no-store", headers: headers() },
        );
        if (!response.ok) throw new Error(`Items fetch failed with HTTP ${response.status}`);
        const data = (await response.json()) as { finds?: DiscoveryFind[] };
        setItems(data.finds || []);
      }
      if (nextTab === "new_sources") {
        const response = await fetch(
          apiUrl("/admin/api/discovery/finds?status=pending&limit=100"),
          { cache: "no-store", headers: headers() },
        );
        if (!response.ok) throw new Error(`Pending fetch failed with HTTP ${response.status}`);
        const data = (await response.json()) as { finds?: DiscoveryFind[] };
        // Only new_source_candidate finding_type lives in pending under Phase A.
        setNewSourceCandidates((data.finds || []).filter((f) => f.finding_type === "new_source_candidate"));
      }
      // Always refresh the full source list for the add form's reference.
      const response = await fetch(apiUrl("/admin/api/discovery/sources"), {
        cache: "no-store",
        headers: headers(),
      });
      if (response.ok) {
        const data = (await response.json()) as { sources?: DiscoverySource[] };
        setAllSources(data.sources || []);
      }
      // Refresh the newsletter assignment dropdown population on every
      // load so the issue list the operator sees on each find row
      // matches the current set of open drafts.
      await loadAssignableIssues();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Discovery fetch failed");
    } finally {
      setStatus("");
    }
  };

  const switchTab = (next: Tab) => {
    setTab(next);
    setNotice("");
    setExpandedSourceId(null);
    setSourceNameFilter("");
    setWatchTypeFilter("all");
    void load(next);
  };

  const onTabKeyDown = (currentTab: Tab) => (event: React.KeyboardEvent<HTMLButtonElement>) => {
    const idx = TAB_ORDER.indexOf(currentTab);
    let nextIdx = idx;
    if (event.key === "ArrowRight") nextIdx = (idx + 1) % TAB_ORDER.length;
    else if (event.key === "ArrowLeft") nextIdx = (idx - 1 + TAB_ORDER.length) % TAB_ORDER.length;
    else if (event.key === "Home") nextIdx = 0;
    else if (event.key === "End") nextIdx = TAB_ORDER.length - 1;
    else return;
    event.preventDefault();
    const target = document.getElementById(`discovery-tab-${TAB_ORDER[nextIdx]}`);
    target?.focus();
    switchTab(TAB_ORDER[nextIdx]);
  };

  const featureSource = async (sourceId: string, featured: boolean) => {
    setStatus(featured ? "Featuring source..." : "Unfeaturing source...");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/discovery/sources/${sourceId}/feature`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ featured, reviewer: "admin" }),
      });
      if (!response.ok) throw new Error(`Feature toggle failed with HTTP ${response.status}`);
      setNotice(featured ? "Source featured." : "Source unfeatured.");
      await load(tab);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Feature toggle failed");
    } finally {
      setStatus("");
    }
  };

  // Phase 2 curation split: three purpose-specific actions per find.
  // Each one only touches the flag it owns; the backend keeps the legacy
  // `featured` mirror aligned for compatibility with older serializers.
  //
  // Three feature flags are independent:
  // - source.featured -> Strong Signals (permanent source-level spotlight)
  // - find.ticker_featured -> appears in the rolling 30-day ticker
  // - find.published_in_newsletter_at -> in the newsletter archive
  //
  // An item can be any combination of the three.

  const tickerFeatureFind = async (findId: string, featured: boolean) => {
    setStatus(featured ? "Adding to ticker..." : "Removing from ticker...");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/discovery/finds/${findId}/ticker-feature`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ featured, reviewer: "admin" }),
      });
      if (!response.ok) throw new Error(`Ticker toggle failed with HTTP ${response.status}`);
      setNotice(featured ? "Featured on the rolling ticker (30-day window)." : "Removed from the ticker.");
      await load(tab);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ticker toggle failed");
    } finally {
      setStatus("");
    }
  };

  const loadAssignableIssues = async () => {
    try {
      const response = await fetch(
        apiUrl("/admin/api/newsletter/assignable-issues"),
        { cache: "no-store", headers: headers() },
      );
      if (!response.ok) return;
      const data = (await response.json()) as { issues?: AssignableIssue[] };
      setAssignableIssues(data.issues || []);
    } catch {
      // Non-fatal; dropdown will just be empty until next unlock.
    }
  };

  const assignFindToIssue = async (findId: string, issueId: string | null) => {
    setStatus(issueId ? "Tagging into issue..." : "Removing from issue...");
    setError("");
    try {
      const response = await fetch(
        apiUrl(`/admin/api/discovery/finds/${findId}/assign-issue`),
        {
          method: "POST",
          headers: headers(),
          body: JSON.stringify({ issue_id: issueId, reviewer: "admin" }),
        },
      );
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Assignment failed with HTTP ${response.status}`);
      }
      setNotice(issueId ? "Tagged for that newsletter issue." : "Removed from newsletter issue.");
      await loadAssignableIssues();
      await load(tab);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Assignment failed");
    } finally {
      setStatus("");
    }
  };

  const createIssueAndAssign = async (findId: string) => {
    const raw = window.prompt(
      "Target send date for the new issue (YYYY-MM-DD or YYYY-MM-DDTHH:mm). Leave empty for the server default (last sent + 10 days).",
      "",
    );
    if (raw === null) return; // operator cancelled
    setStatus("Creating new issue...");
    setError("");
    try {
      const body: { target_send_at?: string } = {};
      if (raw.trim()) {
        const parsed = new Date(raw.trim());
        if (Number.isNaN(parsed.getTime())) {
          setError("Could not parse the target send date.");
          return;
        }
        body.target_send_at = parsed.toISOString();
      }
      const response = await fetch(apiUrl("/admin/api/newsletter/issues/new"), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `New issue failed with HTTP ${response.status}`);
      }
      const data = (await response.json()) as { issue?: { id: string; display_label?: string } };
      const newIssueId = data.issue?.id;
      if (!newIssueId) throw new Error("New issue response was missing the issue id");
      await assignFindToIssue(findId, newIssueId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "New issue failed");
    } finally {
      setStatus("");
    }
  };

  // queueForNewsletter (the legacy boolean toggle) was replaced by the
  // per-issue dropdown above. The /newsletter-queue endpoint stays on
  // the backend as a shim for any out-of-band caller; this UI no
  // longer hits it.

  const dismissFind = async (findId: string, dismissed: boolean) => {
    setStatus(dismissed ? "Dismissing item..." : "Restoring item...");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/discovery/finds/${findId}/dismiss`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ dismissed, reviewer: "admin" }),
      });
      if (!response.ok) throw new Error(`Dismiss toggle failed with HTTP ${response.status}`);
      setNotice(dismissed ? "Item dismissed." : "Item restored.");
      await load(tab);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Dismiss toggle failed");
    } finally {
      setStatus("");
    }
  };

  // Removal surface for public feed content. The backend exposes
  // `POST /admin/api/discovery/finds/{id}/unpublish-latest` which flips the
  // find's status to "withdrawn" so it disappears from the public feed.
  // Use this when an auto-indexed item should not be on the public Updates
  // surface (off-topic, low quality, premature, vendor noise, etc.).
  const unpublishFromLatest = async (findId: string, title: string) => {
    if (!window.confirm(`Remove "${title}" from the public feed?\n\nThe item is set to status "withdrawn". It stays in the database for audit but no longer appears on the public Updates feed or newsletter.`)) {
      return;
    }
    setStatus("Removing from public feed...");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/discovery/finds/${findId}/unpublish-latest`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ reviewer: "admin", notes: "Removed from public feed via admin panel." }),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Unpublish failed with HTTP ${response.status}`);
      }
      setNotice("Item removed from public feed.");
      await load(tab);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unpublish failed");
    } finally {
      setStatus("");
    }
  };

  const approveNewSource = async (find: DiscoveryFind, accept: boolean) => {
    const decision = accept ? "approve" : "reject";
    setStatus(`${accept ? "Approving" : "Rejecting"} new source candidate...`);
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/discovery/finds/${find.id}/${decision}`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ reviewer: "admin", notes: "" }),
      });
      if (!response.ok) throw new Error(`Decision failed with HTTP ${response.status}`);
      setNotice(accept ? "Approved; promote to a watched source from the source list." : "Rejected.");
      setNewSourceCandidates((current) => current.filter((f) => f.id !== find.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Decision failed");
    } finally {
      setStatus("");
    }
  };

  const promoteToSource = async (find: DiscoveryFind) => {
    setStatus("Promoting to watched source...");
    setError("");
    try {
      const response = await fetch(apiUrl(`/admin/api/discovery/finds/${find.id}/promote-source`), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ reviewer: "admin", notes: "", refresh_cadence: "daily", add_artifact_scan: true }),
      });
      if (!response.ok) throw new Error(`Promotion failed with HTTP ${response.status}`);
      setNotice("Source promoted. Future content will auto-index.");
      setNewSourceCandidates((current) => current.filter((f) => f.id !== find.id));
      await load(tab);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Promotion failed");
    } finally {
      setStatus("");
    }
  };

  const submitSourceForm = async (event: FormEvent) => {
    event.preventDefault();
    setStatus("Adding source...");
    setError("");
    try {
      const response = await fetch(apiUrl("/admin/api/discovery/sources"), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(sourceForm),
      });
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail ?? `Source add failed with HTTP ${response.status}`);
      }
      setNotice(`Source ${sourceForm.name} added.`);
      setSourceForm(defaultSourceForm);
      setShowAddForm(false);
      await load(tab);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Source add failed");
    } finally {
      setStatus("");
    }
  };

  // -----------------------------------------------------------------------
  // Derived: filtered + ordered views
  // -----------------------------------------------------------------------

  const filteredSourcesActivity = useMemo(() => {
    return sourcesWithActivity.filter((source) => {
      if (watchTypeFilter !== "all" && source.watch_type !== watchTypeFilter) return false;
      if (sourceNameFilter && !source.name.toLowerCase().includes(sourceNameFilter.toLowerCase())) return false;
      if (tab === "featured" && !source.featured) return false;
      return true;
    });
  }, [sourcesWithActivity, watchTypeFilter, sourceNameFilter, tab]);

  const filteredItems = useMemo(() => {
    return items.filter((item) => {
      if (watchTypeFilter !== "all" && item.watch_type !== watchTypeFilter) return false;
      if (sourceNameFilter && !item.source_name.toLowerCase().includes(sourceNameFilter.toLowerCase())) return false;
      if (tab === "featured" && !item.featured) return false;
      return true;
    });
  }, [items, watchTypeFilter, sourceNameFilter, tab]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  return (
    <main id="main-content" className="min-h-screen bg-brand-dark px-4 py-12 text-brand-offwhite sm:px-8 lg:px-12">
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <header>
          <p className="font-rajdhani text-sm uppercase tracking-[0.32em] text-brand-gold-warm/80">Admin discovery</p>
          <h1 className="mt-4 font-rajdhani text-5xl font-semibold leading-none text-brand-offwhite">Source curation</h1>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-brand-offwhite/65">{TAB_HELP[tab]}</p>
        </header>

        <section className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4 sm:p-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:gap-4">
            <label htmlFor="admin-token" className="sr-only">Admin access token</label>
            <input
              id="admin-token"
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="Admin access token"
              className="min-w-0 flex-1 rounded-xl border border-brand-gold-warm/15 bg-black/35 px-4 py-2 outline-none focus:border-brand-gold"
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => void load(tab)}
              className="rounded-xl bg-brand-gold px-5 py-2 font-semibold text-brand-dark disabled:opacity-60"
            >
              Unlock & refresh
            </button>
            {token.trim() && (
              <button
                type="button"
                onClick={signOutAdmin}
                title="Clear the admin token from this browser. Use this on shared machines."
                className="rounded-xl border border-brand-gold-warm/30 px-4 py-2 text-sm text-brand-offwhite/70 hover:border-red-200 hover:text-red-100"
              >
                Sign out admin
              </button>
            )}
            <button
              type="button"
              disabled={busy || !token.trim()}
              onClick={() => setShowAddCustom(true)}
              title="Paste a hand-picked URL into the newsletter / latest queue."
              className="rounded-xl border border-brand-gold-warm/30 px-4 py-2 text-sm text-brand-gold-warm hover:border-brand-gold disabled:opacity-60"
            >
              Add custom item
            </button>
          </div>
        </section>

        <nav className="flex flex-wrap gap-2" role="tablist" aria-label="Discovery review sections">
          {TAB_ORDER.map((item) => (
            <button
              key={item}
              id={`discovery-tab-${item}`}
              type="button"
              role="tab"
              aria-selected={tab === item}
              aria-controls={`discovery-panel-${item}`}
              tabIndex={tab === item ? 0 : -1}
              onClick={() => switchTab(item)}
              onKeyDown={onTabKeyDown(item)}
              className={`rounded-xl border px-4 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-gold ${
                tab === item
                  ? "border-brand-gold bg-brand-gold text-brand-dark"
                  : "border-brand-gold-warm/15 bg-black/25 text-brand-gold-warm"
              }`}
            >
              {TAB_LABELS[item]}
            </button>
          ))}
        </nav>

        {error && (
          <section role="alert" className="rounded-2xl border border-red-300/30 bg-red-950/20 p-4 text-red-100">
            {error}
          </section>
        )}
        {notice && (
          <section role="status" className="rounded-2xl border border-brand-sage/40 bg-brand-sage/15 p-4 text-brand-offwhite/80">
            {notice}
          </section>
        )}
        {status && (
          <section role="status" className="rounded-2xl border border-brand-gold-warm/20 bg-black/25 p-4 text-brand-gold-warm">
            {status}
          </section>
        )}

        {(tab === "sources" || tab === "items" || tab === "featured") && (
          <FilterChips
            watchTypeFilter={watchTypeFilter}
            onWatchTypeChange={setWatchTypeFilter}
            sourceNameFilter={sourceNameFilter}
            onSourceNameChange={setSourceNameFilter}
          />
        )}

        <section
          id={`discovery-panel-${tab}`}
          role="tabpanel"
          aria-labelledby={`discovery-tab-${tab}`}
          className="grid gap-4"
        >
          {(tab === "sources" || (tab === "featured" && filteredSourcesActivity.length > 0)) && (
            <SourcesActivityList
              sources={filteredSourcesActivity}
              uncuratedCounts={uncuratedCounts}
              expandedId={expandedSourceId}
              token={token}
              assignableIssues={assignableIssues}
              onToggleExpand={(id) => setExpandedSourceId((current) => (current === id ? null : id))}
              onFeatureSource={featureSource}
              onTickerFeatureFind={tickerFeatureFind}
              onAssignFindToIssue={assignFindToIssue}
              onCreateIssueAndAssign={createIssueAndAssign}
              onDismissFind={dismissFind}
              onUnpublishFromLatest={unpublishFromLatest}
              busy={busy}
            />
          )}

          {(tab === "items" || (tab === "featured" && filteredItems.length > 0)) && (
            <ItemsList
              items={filteredItems}
              assignableIssues={assignableIssues}
              onTickerFeatureFind={tickerFeatureFind}
              onAssignFindToIssue={assignFindToIssue}
              onCreateIssueAndAssign={createIssueAndAssign}
              onDismissFind={dismissFind}
              onUnpublishFromLatest={unpublishFromLatest}
              busy={busy}
            />
          )}

          {tab === "new_sources" && (
            <NewSourceCandidatesList
              candidates={newSourceCandidates}
              onDecide={approveNewSource}
              onPromote={promoteToSource}
              busy={busy}
            />
          )}
        </section>

        {tab === "sources" && (
          <section className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4 sm:p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="font-rajdhani text-2xl text-brand-offwhite">Add a new watched source manually</h2>
              <button
                type="button"
                onClick={() => setShowAddForm((current) => !current)}
                className="rounded-full border border-brand-gold-warm/30 px-4 py-1 text-xs text-brand-gold-warm hover:border-brand-gold"
              >
                {showAddForm ? "Hide form" : "Show form"}
              </button>
            </div>
            {showAddForm && (
              <form onSubmit={submitSourceForm} className="mt-4 grid gap-5">
                {sourceFieldGroups.map((group) => (
                  <fieldset key={group.legend} className="grid gap-3 rounded-xl border border-brand-gold-warm/15 bg-black/20 p-4">
                    <legend className="px-2 text-xs uppercase tracking-[0.22em] text-brand-offwhite/55">{group.legend}</legend>
                    {group.fields.map((key) => {
                      if (key === "active") {
                        return (
                          <label key={key} className="flex items-center gap-2 text-sm text-brand-offwhite/70">
                            <input
                              type="checkbox"
                              checked={sourceForm.active}
                              onChange={(event) => setSourceForm({ ...sourceForm, active: event.target.checked })}
                            />
                            {sourceFieldLabels[key]}
                          </label>
                        );
                      }
                      const id = `source-form-${key}`;
                      return (
                        <label key={key} htmlFor={id} className="grid gap-1 text-sm text-brand-offwhite/70">
                          <span>{sourceFieldLabels[key]}</span>
                          <input
                            id={id}
                            value={String(sourceForm[key])}
                            onChange={(event) => setSourceForm({ ...sourceForm, [key]: event.target.value })}
                            className="rounded-xl border border-brand-gold-warm/15 bg-black/35 px-4 py-2 text-sm outline-none focus:border-brand-gold"
                          />
                        </label>
                      );
                    })}
                  </fieldset>
                ))}
                <button
                  type="submit"
                  disabled={busy}
                  className="self-start rounded-xl bg-brand-gold px-5 py-2 font-semibold text-brand-dark disabled:opacity-60"
                >
                  Add source
                </button>
              </form>
            )}
            {allSources.length > 0 && !showAddForm && (
              <p className="mt-3 text-xs text-brand-offwhite/55">
                {allSources.length} sources tracked. The add form is for sources the scout did not surface; routine new
                source candidates from tracked creators appear on the &quot;New source candidates&quot; tab.
              </p>
            )}
          </section>
        )}
      </div>

      {showAddCustom && (
        <AddCustomItemModal
          onClose={() => setShowAddCustom(false)}
          onCreated={() => {
            setNotice("Custom item added. It is live on the public feed and queued for the next newsletter.");
            void load(tab);
          }}
          defaultQueueForNewsletter={true}
        />
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function FilterChips({
  watchTypeFilter,
  onWatchTypeChange,
  sourceNameFilter,
  onSourceNameChange,
}: {
  watchTypeFilter: string;
  onWatchTypeChange: (value: string) => void;
  sourceNameFilter: string;
  onSourceNameChange: (value: string) => void;
}) {
  return (
    <section className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4" aria-label="Filters">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:gap-4">
        <label className="grid gap-1 text-xs uppercase tracking-[0.22em] text-brand-offwhite/55">
          <span>Source type</span>
          <select
            value={watchTypeFilter}
            onChange={(event) => onWatchTypeChange(event.target.value)}
            className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-2 text-sm text-brand-offwhite outline-none focus:border-brand-gold"
          >
            {watchTypeOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
        <label className="grid flex-1 gap-1 text-xs uppercase tracking-[0.22em] text-brand-offwhite/55">
          <span>Filter by source name</span>
          <input
            type="search"
            value={sourceNameFilter}
            onChange={(event) => onSourceNameChange(event.target.value)}
            placeholder="repo, author, blog..."
            className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-2 text-sm text-brand-offwhite outline-none focus:border-brand-gold"
          />
        </label>
      </div>
    </section>
  );
}

// Statuses that mean the find is still on the public feed.
// Mirrors core/discovery_queue.PUBLIC_FIND_STATUSES. We only surface the
// "Remove from feed" button for finds currently in this set; items that
// were already withdrawn or rejected don't need the action.
const PUBLIC_FIND_STATUSES = new Set(["approved", "published", "auto_indexed"]);

// NewsletterAssignDropdown: per-find <select> that lists every open
// issue, plus a "+ New issue..." escape hatch and a "Not in any issue"
// row that clears the assignment. Selected value tracks the find's
// current newsletter_issue_id so the operator can see what's tagged
// without expanding the row.
//
// The compact variant uses smaller padding for the per-source
// drilldown rows (which already render with smaller text); the default
// variant matches the larger Items tab cards.
function NewsletterAssignDropdown({
  findId,
  currentIssueId,
  issues,
  onAssign,
  onCreateNew,
  busy,
  compact = false,
}: {
  findId: string;
  currentIssueId: string | null;
  issues: AssignableIssue[];
  onAssign: (issueId: string | null) => void | Promise<void>;
  onCreateNew: () => void | Promise<void>;
  busy: boolean;
  compact?: boolean;
}) {
  const value = currentIssueId || "";
  const size = compact ? "text-[10px] px-2 py-1" : "text-xs px-2.5 py-1.5";
  const labelClass = compact
    ? "sr-only"
    : "text-[10px] uppercase tracking-wider text-brand-offwhite/55";
  const indicator = currentIssueId
    ? "border-brand-sage/60 bg-brand-sage/20 text-brand-offwhite"
    : "border-brand-gold-warm/40 text-brand-gold-warm";
  return (
    <label className="flex items-center gap-1.5">
      <span className={labelClass}>Newsletter</span>
      <select
        value={value}
        disabled={busy}
        onChange={(event) => {
          const next = event.target.value;
          if (next === "__new__") {
            void onCreateNew();
            return;
          }
          void onAssign(next || null);
        }}
        aria-label={`Newsletter issue for find ${findId}`}
        className={`min-w-[10rem] rounded-full border bg-black/40 ${size} font-semibold transition-colors hover:border-brand-gold disabled:opacity-60 ${indicator}`}
      >
        <option value="">— Not in any issue —</option>
        {issues.map((issue) => {
          const target = issue.target_send_at
            ? new Date(issue.target_send_at).toLocaleDateString()
            : issue.scheduled_send_at
              ? new Date(issue.scheduled_send_at).toLocaleDateString()
              : "TBD";
          return (
            <option key={issue.id} value={issue.id}>
              Issue {issue.display_label} · {target} · {issue.status}
            </option>
          );
        })}
        <option value="__new__">+ New issue...</option>
      </select>
    </label>
  );
}


function SourcesActivityList({
  sources,
  uncuratedCounts,
  expandedId,
  token,
  assignableIssues,
  onToggleExpand,
  onFeatureSource,
  onTickerFeatureFind,
  onAssignFindToIssue,
  onCreateIssueAndAssign,
  onDismissFind,
  onUnpublishFromLatest,
  busy,
}: {
  sources: SourceWithActivity[];
  uncuratedCounts: Record<string, number>;
  expandedId: string | null;
  token: string;
  assignableIssues: AssignableIssue[];
  onToggleExpand: (id: string) => void;
  onFeatureSource: (id: string, featured: boolean) => Promise<void>;
  onTickerFeatureFind: (id: string, featured: boolean) => Promise<void>;
  onAssignFindToIssue: (id: string, issueId: string | null) => Promise<void>;
  onCreateIssueAndAssign: (id: string) => Promise<void>;
  onDismissFind: (id: string, dismissed: boolean) => Promise<void>;
  onUnpublishFromLatest: (id: string, title: string) => Promise<void>;
  busy: boolean;
}) {
  if (sources.length === 0) {
    return (
      <p className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-6 text-sm text-brand-offwhite/55">
        No sources with recent activity in this window.
      </p>
    );
  }
  return (
    <ul className="grid gap-3">
      {sources.map((source) => {
        const newContentCount = uncuratedCounts[source.id] || 0;
        return (
        <li key={source.id} className="rounded-2xl border border-brand-gold-warm/15 bg-black/25">
          <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-2">
                <h3 className="font-rajdhani text-xl font-semibold text-brand-gold-warm">{source.display_name || source.name}</h3>
                {source.featured && (
                  <span className="rounded-full bg-brand-gold/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-brand-gold">
                    Strong signal
                  </span>
                )}
                {newContentCount > 0 && (
                  <span
                    title="Auto-indexed items from this source that have not been featured, queued, or dismissed yet"
                    className="rounded-full bg-brand-sage/30 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-brand-offwhite"
                  >
                    New content ({newContentCount})
                  </span>
                )}
                <span className="text-xs text-brand-offwhite/55">{source.watch_type}</span>
              </div>
              <p className="mt-1 text-xs text-brand-offwhite/55">{source.target}</p>
              <p className="mt-2 text-xs text-brand-offwhite/65">
                {source.notable_finds} notable {source.notable_finds === 1 ? "item" : "items"} ·{" "}
                {source.total_finds} total this window
                {source.last_activity_at && (
                  <span> · last active {new Date(source.last_activity_at).toLocaleString()}</span>
                )}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void onFeatureSource(source.id, !source.featured)}
                className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors disabled:opacity-60 ${
                  source.featured
                    ? "border-brand-gold-warm/40 text-brand-offwhite/65 hover:border-brand-offwhite/45 hover:text-brand-offwhite"
                    : "border-brand-gold-warm/40 text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold"
                }`}
              >
                {source.featured ? "Unfeature source" : "Feature as Strong signal"}
              </button>
              <button
                type="button"
                onClick={() => onToggleExpand(source.id)}
                aria-expanded={expandedId === source.id}
                className="rounded-full border border-brand-gold-warm/20 px-3 py-1.5 text-xs text-brand-gold-warm/80 hover:border-brand-gold hover:text-brand-gold"
              >
                {expandedId === source.id ? "Hide items" : "View items"}
              </button>
            </div>
          </div>
          {expandedId === source.id && (
            <SourceDrilldown
              sourceId={source.id}
              token={token}
              busy={busy}
              assignableIssues={assignableIssues}
              onTickerFeatureFind={onTickerFeatureFind}
              onAssignFindToIssue={onAssignFindToIssue}
              onCreateIssueAndAssign={onCreateIssueAndAssign}
              onDismissFind={onDismissFind}
              onUnpublishFromLatest={onUnpublishFromLatest}
            />
          )}
        </li>
        );
      })}
    </ul>
  );
}

// SourceDrilldown is the Phase 2 paginated/filterable view of all finds
// from a single source. It fetches /admin/api/discovery/sources/{id}/finds
// on mount and whenever its filter inputs change. Each row exposes the
// four curation actions: Feature in ticker, pick a newsletter issue,
// Dismiss, and Remove from feed. The three feature flags are
// independent: ticker_featured (this rail, 30 days), newsletter_pending
// (next issue), and source.featured (Strong Signals spotlight).
function SourceDrilldown({
  sourceId,
  token,
  busy,
  assignableIssues,
  onTickerFeatureFind,
  onAssignFindToIssue,
  onCreateIssueAndAssign,
  onDismissFind,
  onUnpublishFromLatest,
}: {
  sourceId: string;
  token: string;
  busy: boolean;
  assignableIssues: AssignableIssue[];
  onTickerFeatureFind: (id: string, featured: boolean) => Promise<void>;
  onAssignFindToIssue: (id: string, issueId: string | null) => Promise<void>;
  onCreateIssueAndAssign: (id: string) => Promise<void>;
  onDismissFind: (id: string, dismissed: boolean) => Promise<void>;
  onUnpublishFromLatest: (id: string, title: string) => Promise<void>;
}) {
  const [finds, setFinds] = useState<DiscoveryFind[]>([]);
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [totalPages, setTotalPages] = useState(0);
  const [total, setTotal] = useState(0);
  const [daysFilter, setDaysFilter] = useState<string>(""); // "" / "7" / "30" / "90" / "365"
  const [topicFilter, setTopicFilter] = useState<string>("");
  const [topicInput, setTopicInput] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [drilldownError, setDrilldownError] = useState("");
  // Refresh trigger: bump this from action handlers to force a refetch
  // after the parent's onTickerFeatureFind / onQueueForNewsletter / etc
  // mutate a row server-side. Decoupling from the action callbacks keeps
  // the effect dependency surface small and predictable.
  const [refreshTick, setRefreshTick] = useState(0);

  // Single useEffect drives the fetch. It refires when any dependency
  // (sourceId / page / filters / refreshTick) changes, which is the
  // standard React pattern. The async helper inside guards against
  // setState-after-unmount via the `active` flag.
  useEffect(() => {
    if (!token.trim()) return;
    let active = true;
    const run = async () => {
      setLoading(true);
      setDrilldownError("");
      try {
        const params = new URLSearchParams();
        params.set("page", String(page));
        params.set("page_size", String(pageSize));
        if (daysFilter) params.set("days", daysFilter);
        if (topicFilter) params.set("topic", topicFilter);
        const response = await fetch(
          apiUrl(`/admin/api/discovery/sources/${sourceId}/finds?${params.toString()}`),
          { cache: "no-store", headers: { "Content-Type": "application/json", "X-Admin-Token": token.trim() } },
        );
        if (!response.ok) throw new Error(`Drilldown fetch failed with HTTP ${response.status}`);
        const data = (await response.json()) as { finds?: DiscoveryFind[]; total?: number; total_pages?: number };
        if (!active) return;
        setFinds(data.finds || []);
        setTotal(data.total || 0);
        setTotalPages(data.total_pages || 0);
      } catch (err) {
        if (!active) return;
        setDrilldownError(err instanceof Error ? err.message : "Drilldown fetch failed");
      } finally {
        if (active) setLoading(false);
      }
    };
    void run();
    return () => { active = false; };
  }, [sourceId, page, daysFilter, topicFilter, token, refreshTick]);

  const refetch = () => setRefreshTick((tick) => tick + 1);

  const applyTopicFilter = (event: React.FormEvent) => {
    event.preventDefault();
    setTopicFilter(topicInput.trim());
    setPage(1);
  };

  const onDaysChange = (value: string) => {
    setDaysFilter(value);
    setPage(1);
  };

  const goToPage = (next: number) => {
    setPage(next);
  };

  return (
    <div className="border-t border-brand-gold-warm/10 px-4 py-3">
      <form onSubmit={applyTopicFilter} className="mb-3 flex flex-wrap items-end gap-3">
        <label className="grid gap-1 text-xs uppercase tracking-[0.22em] text-brand-offwhite/55">
          <span>Date window</span>
          <select
            value={daysFilter}
            onChange={(event) => onDaysChange(event.target.value)}
            className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-1.5 text-sm text-brand-offwhite outline-none focus:border-brand-gold"
          >
            <option value="">All time</option>
            <option value="7">Last 7 days</option>
            <option value="30">Last 30 days</option>
            <option value="90">Last 90 days</option>
            <option value="365">Last year</option>
          </select>
        </label>
        <label className="grid flex-1 gap-1 text-xs uppercase tracking-[0.22em] text-brand-offwhite/55">
          <span>Topic search (title or summary)</span>
          <input
            type="search"
            value={topicInput}
            onChange={(event) => setTopicInput(event.target.value)}
            placeholder="onboarding, jinja, webhook..."
            className="rounded-lg border border-brand-gold-warm/15 bg-black/35 px-3 py-1.5 text-sm text-brand-offwhite outline-none focus:border-brand-gold"
          />
        </label>
        <button
          type="submit"
          className="rounded-lg border border-brand-gold-warm/30 px-3 py-1.5 text-xs text-brand-gold-warm hover:border-brand-gold"
        >
          Apply
        </button>
        {(daysFilter || topicFilter) && (
          <button
            type="button"
            onClick={() => { setDaysFilter(""); setTopicFilter(""); setTopicInput(""); setPage(1); refetch(); }}
            className="rounded-lg border border-brand-gold-warm/15 px-3 py-1.5 text-xs text-brand-offwhite/55 hover:border-brand-gold-warm/40 hover:text-brand-offwhite/80"
          >
            Clear filters
          </button>
        )}
      </form>

      {loading && <p className="text-xs text-brand-gold-warm/65">Loading items...</p>}
      {drilldownError && <p role="alert" className="text-xs text-red-200">{drilldownError}</p>}
      {!loading && !drilldownError && finds.length === 0 && (
        <p className="text-xs text-brand-offwhite/55">No items match the current filters.</p>
      )}

      {finds.length > 0 && (
        <>
          <p className="mb-2 text-xs text-brand-offwhite/45">
            Showing page {page} of {totalPages || 1} · {total} {total === 1 ? "item" : "items"} total
          </p>
          <ul className="grid gap-2">
            {finds.map((find) => (
              <li key={find.id} className="rounded-xl border border-brand-gold-warm/10 bg-black/15 p-3">
                <div className="flex flex-wrap items-baseline gap-2">
                  <p className="min-w-0 flex-1 truncate text-sm text-brand-offwhite/85">
                    {find.category ? find.category : (find.display_title || find.title)}
                  </p>
                  {find.category && (
                    <span className="rounded-full bg-brand-gold-warm/15 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-brand-gold-warm">
                      {find.child_count} {find.child_count === 1 ? "file" : "files"}
                    </span>
                  )}
                  {find.published_in_newsletter_at && (
                    <span className="rounded-full bg-brand-gold/20 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-brand-gold">published</span>
                  )}
                  {find.newsletter_pending && (
                    <span className="rounded-full bg-brand-sage/30 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-brand-offwhite">in next issue</span>
                  )}
                  {find.dismissed && (
                    <span className="rounded-full bg-black/40 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-brand-offwhite/55">dismissed</span>
                  )}
                </div>
                <p className="mt-1 text-[10px] text-brand-offwhite/45">
                  {find.finding_type} · {find.status}
                  {find.last_upstream_updated_at
                    ? ` · updated ${new Date(find.last_upstream_updated_at).toLocaleDateString()}`
                    : ""}
                  {" · discovered "}{new Date(find.first_seen_at).toLocaleDateString()}
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {find.url && (
                    <a
                      href={safeHref(find.url) || "#"}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="rounded-full border border-brand-gold-warm/20 px-2.5 py-1 text-[10px] text-brand-offwhite/65 hover:border-brand-gold hover:text-brand-gold"
                    >
                      Open ↗
                    </a>
                  )}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={async () => { await onTickerFeatureFind(find.id, !find.ticker_featured); refetch(); }}
                    title="Show on the 30-day rolling ticker on the home page"
                    className={`rounded-full border px-2.5 py-1 text-[10px] font-semibold disabled:opacity-60 ${
                      find.ticker_featured
                        ? "border-brand-gold/70 bg-brand-gold/25 text-brand-offwhite"
                        : "border-brand-gold-warm/40 text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold"
                    }`}
                  >
                    {find.ticker_featured ? "Remove from ticker" : "Feature in ticker"}
                  </button>
                  <NewsletterAssignDropdown
                    findId={find.id}
                    currentIssueId={find.newsletter_issue_id}
                    issues={assignableIssues}
                    onAssign={async (issueId) => { await onAssignFindToIssue(find.id, issueId); refetch(); }}
                    onCreateNew={async () => { await onCreateIssueAndAssign(find.id); refetch(); }}
                    busy={busy}
                    compact
                  />
                  <button
                    type="button"
                    disabled={busy}
                    onClick={async () => { await onDismissFind(find.id, !find.dismissed); refetch(); }}
                    className="rounded-full border border-brand-gold-warm/20 px-2.5 py-1 text-[10px] font-semibold text-brand-offwhite/55 hover:border-brand-gold-warm/40 hover:text-brand-offwhite/80 disabled:opacity-60"
                  >
                    {find.dismissed ? "Restore" : "Dismiss"}
                  </button>
                  {PUBLIC_FIND_STATUSES.has(find.status) && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={async () => { await onUnpublishFromLatest(find.id, find.display_title || find.title); refetch(); }}
                      title="Status becomes withdrawn"
                      className="rounded-full border border-red-300/30 px-2.5 py-1 text-[10px] font-semibold text-red-200 hover:border-red-200 hover:text-red-100 disabled:opacity-60"
                    >
                      Remove from feed
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>

          {totalPages > 1 && (
            <div className="mt-3 flex items-center justify-between gap-2 text-xs text-brand-offwhite/65">
              <button
                type="button"
                disabled={page <= 1 || loading}
                onClick={() => goToPage(page - 1)}
                className="rounded-full border border-brand-gold-warm/30 px-3 py-1 disabled:opacity-40"
              >
                ← Prev
              </button>
              <span>Page {page} of {totalPages}</span>
              <button
                type="button"
                disabled={page >= totalPages || loading}
                onClick={() => goToPage(page + 1)}
                className="rounded-full border border-brand-gold-warm/30 px-3 py-1 disabled:opacity-40"
              >
                Next →
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function ItemsList({
  items,
  assignableIssues,
  onTickerFeatureFind,
  onAssignFindToIssue,
  onCreateIssueAndAssign,
  onDismissFind,
  onUnpublishFromLatest,
  busy,
}: {
  items: DiscoveryFind[];
  assignableIssues: AssignableIssue[];
  onTickerFeatureFind: (id: string, featured: boolean) => Promise<void>;
  onAssignFindToIssue: (id: string, issueId: string | null) => Promise<void>;
  onCreateIssueAndAssign: (id: string) => Promise<void>;
  onDismissFind: (id: string, dismissed: boolean) => Promise<void>;
  onUnpublishFromLatest: (id: string, title: string) => Promise<void>;
  busy: boolean;
}) {
  if (items.length === 0) {
    return (
      <p className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-6 text-sm text-brand-offwhite/55">
        No auto-indexed items matched the current filters.
      </p>
    );
  }
  return (
    <ul className="grid gap-3">
      {items.map((item) => (
        <li
          key={item.id}
          className="flex flex-col gap-3 rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6"
        >
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-baseline gap-2">
              <h3 className="font-rajdhani text-lg font-semibold text-brand-offwhite">
                {item.category ? `${item.display_source_name || item.source_name} / ${item.category}` : (item.display_title || item.title)}
              </h3>
              {item.category && (
                <span className="rounded-full bg-brand-gold-warm/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-brand-gold-warm">
                  {item.child_count} {item.child_count === 1 ? "file" : "files"}
                </span>
              )}
              {item.published_in_newsletter_at && (
                <span className="rounded-full bg-brand-gold/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-brand-gold">
                  Published
                </span>
              )}
              {item.newsletter_pending && (
                <span className="rounded-full bg-brand-sage/30 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-brand-offwhite">
                  In next issue
                </span>
              )}
              {item.dismissed && (
                <span className="rounded-full bg-black/40 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-brand-offwhite/55">
                  Dismissed
                </span>
              )}
            </div>
            <p className="mt-1 text-xs text-brand-offwhite/55">
              {item.display_source_name || item.source_name} · {item.finding_type} · {item.watch_type}
              {item.importance_signal && item.importance_signal !== "normal" && (
                <span className="ml-2 text-brand-gold-warm">{item.importance_signal}</span>
              )}
            </p>
            <p className="mt-1 text-[11px] text-brand-offwhite/40">
              {item.last_upstream_updated_at && (
                <>updated {new Date(item.last_upstream_updated_at).toLocaleDateString()} · </>
              )}
              discovered {new Date(item.first_seen_at).toLocaleDateString()}
            </p>
            {item.summary_text && (
              <p className="mt-2 line-clamp-3 text-sm leading-6 text-brand-offwhite/72">{item.summary_text}</p>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {item.url && (
              <a
                href={safeHref(item.url) || "#"}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-full border border-brand-gold-warm/20 px-3 py-1.5 text-xs text-brand-offwhite/65 hover:border-brand-gold hover:text-brand-gold"
              >
                Open
              </a>
            )}
            <button
              type="button"
              disabled={busy}
              onClick={() => void onTickerFeatureFind(item.id, !item.ticker_featured)}
              title="Show on the 30-day rolling ticker on the home page"
              className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors disabled:opacity-60 ${
                item.ticker_featured
                  ? "border-brand-gold/70 bg-brand-gold/25 text-brand-offwhite"
                  : "border-brand-gold-warm/40 text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold"
              }`}
            >
              {item.ticker_featured ? "Remove from ticker" : "Feature in ticker"}
            </button>
            <NewsletterAssignDropdown
              findId={item.id}
              currentIssueId={item.newsletter_issue_id}
              issues={assignableIssues}
              onAssign={(issueId) => void onAssignFindToIssue(item.id, issueId)}
              onCreateNew={() => void onCreateIssueAndAssign(item.id)}
              busy={busy}
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => void onDismissFind(item.id, !item.dismissed)}
              className="rounded-full border border-brand-gold-warm/20 px-3 py-1.5 text-xs font-semibold text-brand-offwhite/55 hover:border-brand-gold-warm/40 hover:text-brand-offwhite/80 disabled:opacity-60"
            >
              {item.dismissed ? "Restore" : "Dismiss"}
            </button>
            {PUBLIC_FIND_STATUSES.has(item.status) && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void onUnpublishFromLatest(item.id, item.display_title || item.title)}
                title="Remove from public feed (status becomes withdrawn)"
                className="rounded-full border border-red-300/30 px-3 py-1.5 text-xs font-semibold text-red-200 hover:border-red-200 hover:text-red-100 disabled:opacity-60"
              >
                Remove from feed
              </button>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function NewSourceCandidatesList({
  candidates,
  onDecide,
  onPromote,
  busy,
}: {
  candidates: DiscoveryFind[];
  onDecide: (find: DiscoveryFind, accept: boolean) => Promise<void>;
  onPromote: (find: DiscoveryFind) => Promise<void>;
  busy: boolean;
}) {
  if (candidates.length === 0) {
    return (
      <p className="rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-6 text-sm text-brand-offwhite/55">
        No new source candidates awaiting approval. The scout queues these when it finds new repos from tracked
        creators or net-new sources matching watchlist policy.
      </p>
    );
  }
  return (
    <ul className="grid gap-3">
      {candidates.map((find) => (
        <li
          key={find.id}
          className="grid gap-3 rounded-2xl border border-brand-gold-warm/15 bg-black/25 p-4 sm:grid-cols-[1fr_auto] sm:items-start sm:gap-6"
        >
          <div className="min-w-0">
            <h3 className="font-rajdhani text-lg font-semibold text-brand-offwhite">{find.display_title || find.title}</h3>
            <p className="mt-1 text-xs text-brand-offwhite/55">
              from {find.display_source_name || find.source_name} · suggested type: {find.finding_type}
            </p>
            {find.summary_text && (
              <p className="mt-2 text-sm leading-6 text-brand-offwhite/72">{find.summary_text}</p>
            )}
            {find.url && (
              <a
                href={safeHref(find.url) || "#"}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 inline-flex text-xs text-brand-gold-warm hover:text-brand-gold"
              >
                Open candidate ↗
              </a>
            )}
          </div>
          <div className="flex flex-col gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => void onPromote(find)}
              className="rounded-full bg-brand-gold px-4 py-2 text-xs font-semibold text-brand-dark hover:bg-brand-gold-mid disabled:opacity-60"
            >
              Promote to watched source
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void onDecide(find, true)}
              className="rounded-full border border-brand-gold-warm/40 px-4 py-2 text-xs font-semibold text-brand-gold-warm hover:border-brand-gold hover:text-brand-gold disabled:opacity-60"
            >
              Approve only (no promotion)
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void onDecide(find, false)}
              className="rounded-full border border-red-300/30 px-4 py-2 text-xs font-semibold text-red-200 hover:border-red-200 hover:text-red-100 disabled:opacity-60"
            >
              Reject
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}
