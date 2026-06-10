"""Tests for the discovery display layer.

Verifies that machine-friendly source slugs and handler-generated finding
titles are rewritten into human-readable strings before they reach any
public or admin UI surface.
"""

from __future__ import annotations

import pytest

from gestaltworkframe.core.discovery_display import (
    display_finding_caption,
    display_finding_title,
    display_source_name,
    enrich_find_display,
    enrich_source_display,
)


# ---- source name transforms ------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("platform_official_blog", "Platform Official Blog"),
        ("gigacode_blog", "Gigacode Blog"),
        ("discovery_scout", "Discovery scout"),
        ("community_org_automation_workflows", "Community Org Automation Workflows"),
        ("platform_docs_help_artifacts", "Platform Docs Help"),
        ("community_member_automation_buddy", "Community Member Automation Buddy"),
        ("example/repo-a", "example/repo-a"),  # slash form preserved
        ("", ""),
    ],
)
def test_display_source_name(raw, expected):
    assert display_source_name(raw) == expected


# ---- finding title transforms ----------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # github_topic handler: "X/Y matches topic:Z"
        ("example-author/automation-bundles matches topic:automation", "New automation repo: example-author/automation-bundles"),
        # github_user_org handler: "Account repository: name"
        ("platform-app repository: docs.platform.example.com", "New repo from platform-app: docs.platform.example.com"),
        # github_repo_artifact handler: "X/Y artifact: path"
        (
            "community-org/Automation-Workflows artifact: Processes/Docs - Seed Default Folders.bundle.json",
            "community-org/Automation-Workflows - Processes/Docs - Seed Default Folders.bundle.json",
        ),
        # github_repo commit (with message)
        ("example-author/automation-bundles commit a1b2c3d: fix bundle import", "example-author/automation-bundles commit: fix bundle import (a1b2c3d)"),
        # github_repo commit (no message)
        ("example-author/automation-bundles commit a1b2c3d", "example-author/automation-bundles commit a1b2c3d"),
        # RSS / blog titles pass through unchanged
        (
            "A new automation pattern for client onboarding",
            "A new automation pattern for client onboarding",
        ),
        # Release titles pass through unchanged
        ("repo-a v1.0", "repo-a v1.0"),
        ("", ""),
    ],
)
def test_display_finding_title(raw, expected):
    assert display_finding_title(raw) == expected


# ---- caption template ------------------------------------------------------

def test_display_finding_caption_known_types():
    assert display_finding_caption("release", "Platform Official Blog") == "Release from Platform Official Blog"
    assert display_finding_caption("rss_item", "Gigacode Blog") == "Article from Gigacode Blog"
    assert display_finding_caption("github_topic_match", "example-author") == "New repo from example-author"
    assert display_finding_caption("artifact", "community-org") == "Repository file from community-org"


def test_display_finding_caption_unknown_type_falls_back():
    # Unknown finding_type should still produce something readable.
    assert display_finding_caption("magic_event_kind", "X") == "Magic event kind from X"
    assert display_finding_caption("", "X") == "Finding from X"


# ---- enrichers add fields without dropping existing data -------------------

def test_enrich_find_display_adds_three_fields_idempotently():
    payload = {
        "id": "f-1",
        "source_name": "platform_official_blog",
        "title": "A new automation pattern for client onboarding",
        "finding_type": "rss_item",
        "watch_type": "rss_watch",
    }
    enriched = enrich_find_display(payload)
    assert enriched["display_source_name"] == "Platform Official Blog"
    assert enriched["display_title"] == "A new automation pattern for client onboarding"
    assert enriched["display_caption"] == "Article from Platform Official Blog"
    # Original fields untouched.
    assert enriched["source_name"] == "platform_official_blog"
    assert enriched["title"] == "A new automation pattern for client onboarding"
    # Idempotent.
    enrich_find_display(enriched)
    assert enriched["display_title"] == "A new automation pattern for client onboarding"


def test_enrich_source_display_humanizes_name_and_recent_finds():
    payload = {
        "id": "src-x",
        "name": "community_org_automation_workflows_artifacts",
        "watch_type": "github_repo_artifact_scan",
        "recent_finds": [
            {
                "id": "f-1",
                "title": "community-org/Automation-Workflows artifact: workflows/onboarding.bundle.json",
                "finding_type": "artifact",
            },
            {
                "id": "f-2",
                "title": "example-author/automation-bundles matches topic:automation",
                "finding_type": "github_topic_match",
            },
        ],
    }
    enrich_source_display(payload)
    assert payload["display_name"] == "Community Org Automation Workflows"
    assert payload["recent_finds"][0]["display_title"] == "community-org/Automation-Workflows - workflows/onboarding.bundle.json"
    assert payload["recent_finds"][1]["display_title"] == "New automation repo: example-author/automation-bundles"
