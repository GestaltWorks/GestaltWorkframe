"""Admin discovery panel UI contract tests.

After the Phase B redesign, the panel is curation-centric: four tabs
(Sources & activity / Recent items / New source candidates / Featured),
Feature/Unfeature buttons in place of the old approve-everything
queue, source-name and source-type filter chips, and a single
remaining approval gate for new source candidates.
"""

from pathlib import Path


def _panel() -> str:
    return Path("web/src/components/AdminDiscoveryPanel.tsx").read_text(encoding="utf-8")


def test_admin_discovery_has_four_curation_tabs():
    panel = _panel()
    # Tab keys for the WAI-ARIA tabs pattern.
    assert "\"sources\" | \"items\" | \"new_sources\" | \"featured\"" in panel
    # Human-readable labels.
    assert "Sources & activity" in panel
    assert "Recent items" in panel
    assert "New source candidates" in panel
    assert "Featured" in panel
    # Tab IDs for the role=tablist anchor.
    assert 'id={`discovery-tab-${item}`}' in panel


def test_admin_discovery_filter_chips_for_source_type_and_name():
    panel = _panel()
    assert "Source type" in panel
    assert "Filter by source name" in panel
    assert "watchTypeFilter" in panel
    assert "sourceNameFilter" in panel
    # The watch_type select must offer the major options.
    assert "GitHub repo (releases)" in panel
    assert "RSS feed" in panel
    assert "Scout (candidate sources)" in panel


def test_admin_discovery_calls_phase_a_feature_endpoints():
    panel = _panel()
    assert "/feature" in panel
    assert "sources-with-activity" in panel
    assert "?status=auto_indexed" in panel
    assert "?status=pending" in panel


def test_admin_discovery_renders_category_rollup_for_artifact_sources():
    """github_repo_artifact_scan finds carry a `category` field and a
    `child_count`. The admin row renders the category name (instead of
    the raw file title), a "<N> files" pill, and a meta line with both
    `updated` (last_upstream_updated_at) and `discovered` (first_seen_at)
    timestamps. Curation actions (Feature in ticker / Send to next
    newsletter / Dismiss) act on the category row."""
    panel = _panel()
    # Type carries the rollup fields so the admin UI can render them.
    assert "category: string" in panel
    assert "child_count: number" in panel
    assert "last_upstream_updated_at: string | null" in panel
    # Drilldown renders the category-or-title branch.
    assert "find.category ? find.category" in panel
    # ItemsList renders the "source / category" header for rollup rows.
    assert "/ ${item.category}" in panel
    # Files-count pill rendered when the row is a rollup.
    assert '{find.child_count === 1 ? "file" : "files"}' in panel
    assert '{item.child_count === 1 ? "file" : "files"}' in panel
    # Meta line shows discovered + (when present) updated timestamps.
    assert "discovered " in panel
    assert "last_upstream_updated_at" in panel


def test_admin_discovery_supports_feature_and_unfeature_actions():
    """The per-find UI exposes three independent feature flags plus the
    destructive Remove-from-feed action:

    - Feature in ticker (find.ticker_featured): rolling 30-day surface
      on the home page and /library.
    - Newsletter assignment (find.newsletter_issue_id): per-issue
      dropdown that lists every open draft / awaiting-approval issue
      plus a "+ New issue..." escape hatch.
    - Feature as Strong signal (source.featured): permanent source-level
      spotlight in the FeaturedSourcePillars row.

    Plus Dismiss (stops counting against the New content badge) and
    Remove from feed (destructive, sets status=withdrawn).
    """
    panel = _panel()
    # Source-level: permanent featured flag.
    assert "Feature as Strong signal" in panel
    assert "Unfeature source" in panel
    assert "featureSource" in panel
    # Find-level ticker toggle.
    assert "Feature in ticker" in panel
    assert "Remove from ticker" in panel
    assert "tickerFeatureFind" in panel
    assert "/ticker-feature" in panel
    # Find-level newsletter ASSIGNMENT (per-issue dropdown).
    assert "NewsletterAssignDropdown" in panel
    assert "assignFindToIssue" in panel
    assert "createIssueAndAssign" in panel
    assert "/assign-issue" in panel
    assert "+ New issue..." in panel
    assert "newsletter_issue_id" in panel
    # The boolean queue button shape is gone; the dropdown owns the
    # interaction now.
    assert "Send to next newsletter" not in panel
    assert "Pull from newsletter" not in panel
    # Dismiss + endpoint.
    assert "Dismiss" in panel
    assert "dismissFind" in panel
    assert "/dismiss" in panel


def test_admin_discovery_new_content_badge_and_drilldown():
    """Phase 2 admin enhancements: New content badge per source + a
    drilldown view that fetches the per-source paginated find list with
    date and topic filters."""
    panel = _panel()
    assert "New content" in panel
    assert "uncuratedCounts" in panel
    assert "uncurated-counts" in panel
    assert "SourceDrilldown" in panel
    assert "/admin/api/discovery/sources/" in panel
    assert "Date window" in panel
    assert "Topic search" in panel
    assert "Page " in panel  # pagination control


def test_admin_discovery_new_source_candidates_are_the_only_approval_gate():
    panel = _panel()
    assert "Promote to watched source" in panel
    assert "Approve only (no promotion)" in panel
    assert "Reject" in panel
    # Only new_source_candidate finding_type enters the pending queue under Phase A.
    assert 'finding_type === "new_source_candidate"' in panel


def test_admin_discovery_sources_panel_shows_activity_and_recent_items():
    panel = _panel()
    assert "SourcesActivityList" in panel
    assert "View" in panel  # the expand button label includes "View N items"
    assert "Hide items" in panel
    assert "last active" in panel
    assert "notable" in panel


def test_admin_discovery_keyboard_navigation_across_tabs():
    panel = _panel()
    assert 'event.key === "ArrowRight"' in panel
    assert 'event.key === "ArrowLeft"' in panel
    assert 'event.key === "Home"' in panel
    assert 'event.key === "End"' in panel
    assert "TAB_ORDER" in panel


def test_admin_discovery_has_remove_from_feed_action():
    """The admin panel must wire the existing /unpublish-latest backend
    endpoint into the UI so operators can drop an item off the public
    /library/latest feed. Without this, the Phase B redesign leaves no
    removal surface for content that was auto-indexed but should not be
    on the public Updates feed."""
    panel = _panel()
    assert "unpublishFromLatest" in panel
    assert "/unpublish-latest" in panel
    assert "Remove from feed" in panel
    # The removal button is gated by the public-status set so we don't
    # offer it for already-withdrawn or rejected finds.
    assert "PUBLIC_FIND_STATUSES" in panel
    assert '"approved", "published", "auto_indexed"' in panel
    # Both the items list and the expanded source-finds list expose the
    # action, so operators can remove from either surface.
    assert "onUnpublishFromLatest" in panel



