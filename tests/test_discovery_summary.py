from __future__ import annotations

import pytest

from gestaltworkframe.core.discovery_digest import DiscoveryDigestConfig, render_discovery_digest_html, send_discovery_digest
from gestaltworkframe.core.discovery_summary import discovery_review_metadata, enrich_discovery_find, summarize_discovery_finds


# Table-driven routing assertions. Each row is one realistic discovery
# finding shape plus the lane/event_kind/approval contract it MUST land
# in. New routing rules should add a row here instead of cloning a
# whole new test function; existing routing rules should be visible at
# a glance via this table rather than scattered across separate tests.
_ROUTING_CASES = [
    pytest.param(
        {
            "title": "Repository README updated",
            "summary_text": "Minor changed file detected",
            "finding_type": "diff",
            "importance_signal": "low",
            "watch_type": "github_repo_artifact_scan",
            "source_name": "example/repo",
        },
        {"event_kind": "source_update", "review_lane": "routine_updates", "approval_required": False, "routine_update": True},
        id="github_readme_diff_is_routine",
    ),
    pytest.param(
        {
            "title": "Tracked docs release update",
            "summary_text": "Release notes added a new workflow bundle",
            "finding_type": "diff",
            "importance_signal": "normal",
            "watch_type": "web_diff_watch",
            "source_name": "vendor docs",
        },
        {"event_kind": "new_content", "review_lane": "publish_candidates", "routine_update": False},
        id="web_diff_with_release_words_is_publish_candidate",
    ),
    pytest.param(
        {
            "title": "Tracked blog changed",
            "summary_text": "New post with automation examples detected",
            "finding_type": "diff",
            "importance_signal": "normal",
            "watch_type": "web_diff_watch",
            "source_name": "automation blog",
        },
        {"event_kind": "new_content", "review_lane": "publish_candidates"},
        id="blog_new_post_diff_is_publish_candidate",
    ),
    pytest.param(
        {
            "title": "README changed",
            "summary_text": "Minor file update detected",
            "finding_type": "diff",
            "importance_signal": "low",
            "watch_type": "github_repo_artifact_scan",
            "source_name": "api docs mirror",
            "url": "https://example.test/api/docs",
        },
        {"event_kind": "source_update", "review_lane": "routine_updates", "routine_update": True},
        id="api_in_source_name_does_not_promote_low_signal_diff",
    ),
]


@pytest.mark.parametrize("payload,expected", _ROUTING_CASES)
def test_discovery_review_metadata_routing(payload, expected):
    """Verify that the metadata classifier routes each shape to the documented lane.

    Drives `discovery_review_metadata` with realistic finding payloads and
    asserts the subset of output fields the table provides. Use this for
    routing-shape regressions; the more detailed scoring tests below cover
    the scoring math.
    """
    metadata = discovery_review_metadata(payload)
    for key, value in expected.items():
        assert metadata[key] == value, f"{key}: got {metadata[key]!r}, expected {value!r}"


def test_discovery_review_metadata_tags_new_sources_and_blog_candidates():
    new_source = discovery_review_metadata(
        {
            "title": "Promising public Automation workflow repo",
            "summary_text": "New source candidate with workflow bundles",
            "finding_type": "new_source_candidate",
            "importance_signal": "high",
            "source_name": "scout",
        }
    )
    release = discovery_review_metadata(
        {
            "title": "Automation workflow bundle release v2",
            "summary_text": "Major changelog for importable automation templates",
            "finding_type": "release",
            "importance_signal": "high",
            "source_name": "github",
        }
    )

    assert new_source["review_topic"] == "New sources to consider"
    assert "new-source" in new_source["review_tags"]
    assert new_source["newsletter_score"] < 55
    assert new_source["newsletter_candidate"] is False
    assert release["newsletter_candidate"] is True
    assert release["review_lane"] == "publish_candidates"
    assert release["publish_score"] >= 55
    assert release["ingest_score"] >= 60
    assert "release" in release["review_tags"]
    assert "workflow" in release["review_tags"]


def test_summarize_discovery_finds_groups_topics_sources_and_suggestions():
    finds = [
        {
            "id": "release-1",
            "title": "Automation workflow bundle release v2",
            "url": "https://github.com/example/repo/releases/2",
            "summary_text": "Major changelog for importable automation templates",
            "finding_type": "release",
            "importance_signal": "high",
            "status": "pending",
            "source_name": "example/repo",
            "watch_type": "github_repo_watch",
        },
        {
            "id": "source-1",
            "title": "New public Automation examples repo",
            "url": "https://github.com/example/examples",
            "summary_text": "New source candidate",
            "finding_type": "new_source_candidate",
            "importance_signal": "normal",
            "status": "pending",
            "source_name": "scout",
            "watch_type": "discovery_scout",
        },
        {
            "id": "diff-1",
            "title": "Repository README updated",
            "url": "https://github.com/example/repo",
            "summary_text": "Minor repository activity",
            "finding_type": "artifact",
            "importance_signal": "low",
            "status": "pending",
            "source_name": "example/repo",
            "watch_type": "github_repo_artifact_scan",
        },
    ]

    summary = summarize_discovery_finds(finds)

    assert summary["total"] == 3
    assert summary["high_importance"] == 1
    assert summary["suggested_posts"][0]["id"] == "release-1"
    assert summary["ingestion_candidates"][0]["id"] == "release-1"
    assert all(item["id"] != "source-1" for item in summary["ingestion_candidates"])
    assert summary["new_source_candidates"][0]["id"] == "source-1"
    assert summary["prominent_sources"][0]["source_name"] == "example/repo"
    assert summary["lanes"][0]["lane"] == "publish_candidates"
    assert any(group["topic"] == "Releases and major updates" for group in summary["topic_groups"])


def test_summarize_discovery_finds_handles_empty_list():
    summary = summarize_discovery_finds([])

    assert summary["total"] == 0
    assert summary["topic_groups"] == []
    assert summary["suggested_posts"] == []
    assert summary["ingestion_candidates"] == []
    assert summary["new_source_candidates"] == []


def test_discovery_summary_keeps_new_sources_out_of_ingestion_candidates():
    summary = summarize_discovery_finds(
        [
            {
                "id": "new-source",
                "title": "High value workflow source",
                "summary_text": "New source candidate with workflow bundles and API schemas",
                "finding_type": "new_source_candidate",
                "importance_signal": "high",
                "source_name": "scout",
            }
        ]
    )

    assert summary["new_source_candidates"][0]["id"] == "new-source"
    assert summary["ingestion_candidates"] == []


def test_discovery_summary_keeps_markdown_churn_out_of_ingestion_candidates():
    summary = summarize_discovery_finds(
        [
            {
                "id": "readme-update",
                "title": "README.md updated",
                "url": "https://github.com/example/repo/blob/main/README.md",
                "summary_text": "Markdown file changed in tracked source scan",
                "finding_type": "artifact",
                "importance_signal": "normal",
                "status": "pending",
                "source_name": "example/repo",
                "watch_type": "github_repo_artifact_scan",
            }
        ]
    )

    assert summary["ingestion_candidates"] == []
    assert summary["lanes"][0]["lane"] == "routine_updates"


def test_render_discovery_digest_html_is_newsletter_with_sections():
    html = render_discovery_digest_html(
        [
            {
                "id": "release-1",
                "title": "Automation workflow bundle release v2",
                "url": "https://github.com/example/repo/releases/2",
                "summary_text": "Major changelog for importable automation templates",
                "finding_type": "release",
                "importance_signal": "high",
                "status": "pending",
                "source_name": "example/repo",
                "watch_type": "github_repo_watch",
            }
        ]
    )

    assert "Discovery Newsletter" in html
    assert "Suggested Updates and Additions picks" in html
    assert "Topic map" in html
    assert "Prominent sources" in html


def test_discovery_digest_config_reads_documented_recipient_env(monkeypatch):
    monkeypatch.setenv("DISCOVERY_DIGEST_ENABLED", "true")
    monkeypatch.setenv("DISCOVERY_DIGEST_RECIPIENT", "review@example.test")

    cfg = DiscoveryDigestConfig.from_env()

    assert cfg.enabled is True
    assert cfg.recipient == "review@example.test"


def test_discovery_digest_config_reads_legacy_to_env_as_migration_shim(monkeypatch):
    monkeypatch.setenv("DISCOVERY_DIGEST_ENABLED", "1")
    monkeypatch.delenv("DISCOVERY_DIGEST_RECIPIENT", raising=False)
    monkeypatch.setenv("DISCOVERY_DIGEST_TO", "legacy@example.test")

    cfg = DiscoveryDigestConfig.from_env()

    assert cfg.recipient == "legacy@example.test"


def test_discovery_digest_config_defaults_invalid_max_items(monkeypatch):
    monkeypatch.setenv("DISCOVERY_DIGEST_MAX_ITEMS", "abc")

    cfg = DiscoveryDigestConfig.from_env()

    assert cfg.max_items == 100


def test_enrich_discovery_find_recomputes_existing_review_metadata():
    find = {
        "title": "Release workflow update",
        "finding_type": "release",
        "importance_signal": "high",
        "review_topic": "Stale",
        "review_tags": ["stale"],
        "newsletter_score": 7,
    }

    enriched = enrich_discovery_find(find)

    assert enriched is not find
    assert enriched["review_topic"] == "Releases and major updates"
    assert enriched["newsletter_score"] > 7


def test_render_discovery_digest_html_does_not_link_unsafe_urls():
    html = render_discovery_digest_html(
        [
            {
                "id": "unsafe-1",
                "title": "Unsafe link",
                "url": "javascript:alert(1)",
                "summary_text": "Release workflow update",
                "finding_type": "release",
                "importance_signal": "high",
                "status": "pending",
                "source_name": "external",
                "watch_type": "rss_feed",
            }
        ]
    )

    assert "javascript:alert" not in html
    assert "Unsafe link" in html


@pytest.mark.asyncio
async def test_send_discovery_digest_reads_pending_finds_only(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_list_recent_finds(_session, *, limit: int, status: str | None = None):
        seen["limit"] = limit
        seen["status"] = status
        return []

    async def fake_send_internal_email(subject: str, html: str, *, recipient=None, reply_to=None, sender=None):
        seen["subject"] = subject
        seen["recipient"] = recipient
        return "sent"

    monkeypatch.setattr("gestaltworkframe.core.discovery_digest.list_recent_finds", fake_list_recent_finds)
    monkeypatch.setattr("gestaltworkframe.core.discovery_digest.send_internal_email", fake_send_internal_email)

    status = await send_discovery_digest(None, config=DiscoveryDigestConfig(enabled=True, recipient="review@example.test", max_items=25))

    assert status == "sent"
    assert seen["limit"] == 25
    assert seen["status"] == "pending"
    assert seen["recipient"] == "review@example.test"