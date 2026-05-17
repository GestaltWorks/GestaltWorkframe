"""Deterministic grouping, lanes, and newsletter suggestions for discovery findings."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

NEWSLETTER_THRESHOLD = 55
STRONG_CANDIDATE_THRESHOLD = 70
INGEST_THRESHOLD = 60
ROUTINE_THRESHOLD = 30


def discovery_review_metadata(find: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(find.get(key) or "") for key in ("title", "summary_text", "url", "source_name", "watch_type", "finding_type")).lower()
    signal_text = " ".join(str(find.get(key) or "") for key in ("title", "summary_text", "finding_type")).lower()
    finding_type = str(find.get("finding_type") or "")
    importance = str(find.get("importance_signal") or "normal")
    watch_type = str(find.get("watch_type") or "")
    topic = _topic(finding_type, text)
    tags = _tags(finding_type, importance, text)
    content_type = _content_type(finding_type, text)
    event_kind = _event_kind(finding_type, watch_type, signal_text)
    publish_score = _publish_score(finding_type, importance, signal_text, tags, content_type, event_kind)
    ingest_score = _ingest_score(importance, tags, content_type, event_kind, finding_type)
    if "new-source" in tags:
        publish_score = min(publish_score, NEWSLETTER_THRESHOLD - 1)
    routine_update = event_kind == "source_update" and publish_score < ROUTINE_THRESHOLD and (importance == "low" or ingest_score < INGEST_THRESHOLD) and not _notable_source_update(signal_text)
    lane = _review_lane(event_kind, publish_score, ingest_score, tags, routine_update)
    return {
        "review_topic": topic,
        "review_tags": tags,
        "content_type": content_type,
        "event_kind": event_kind,
        "review_lane": lane,
        "approval_required": lane not in {"routine_updates", "low_signal"},
        "publish_score": publish_score,
        "ingest_score": ingest_score,
        "newsletter_score": publish_score,
        "newsletter_candidate": publish_score >= NEWSLETTER_THRESHOLD and "new-source" not in tags,
        "routine_update": routine_update,
        "suggested_action": _suggested_action(publish_score, ingest_score, tags, topic, lane),
    }


def enrich_discovery_find(find: dict[str, Any]) -> dict[str, Any]:
    return {**find, **discovery_review_metadata(find)}


def summarize_discovery_finds(finds: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = [enrich_discovery_find(find) for find in finds]
    topics: dict[str, list[dict[str, Any]]] = defaultdict(list)
    lanes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sources: Counter[str] = Counter()
    source_high: Counter[str] = Counter()
    for find in enriched:
        topics[str(find["review_topic"])].append(find)
        lanes[str(find["review_lane"])].append(find)
        source = str(find.get("source_name") or "unknown")
        sources[source] += 1
        if find.get("importance_signal") == "high" or int(find.get("newsletter_score") or 0) >= NEWSLETTER_THRESHOLD:
            source_high[source] += 1
    topic_groups = []
    for topic, items in topics.items():
        ranked = sorted(items, key=_rank_key)[:5]
        topic_groups.append({"topic": topic, "count": len(items), "high_count": sum(1 for item in items if item.get("importance_signal") == "high"), "newsletter_candidates": sum(1 for item in items if item.get("newsletter_candidate")), "items": [_brief(item) for item in ranked]})
    suggested_posts = [_brief(item) for item in sorted((item for item in enriched if item.get("newsletter_candidate")), key=_rank_key)[:8]]
    new_sources = [_brief(item) for item in sorted((item for item in enriched if "new-source" in item.get("review_tags", [])), key=_rank_key)[:8]]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(enriched),
        "pending": sum(1 for item in enriched if item.get("status") == "pending"),
        "approved": sum(1 for item in enriched if item.get("status") == "approved"),
        "high_importance": sum(1 for item in enriched if item.get("importance_signal") == "high"),
        "topic_groups": sorted(topic_groups, key=lambda group: (-int(group["newsletter_candidates"]), -int(group["high_count"]), -int(group["count"]), str(group["topic"]))),
        "lanes": [{"lane": lane, "label": _lane_label(lane), "count": len(items), "items": [_brief(item) for item in sorted(items, key=_lane_rank_key)[:6]]} for lane, items in sorted(lanes.items(), key=lambda row: (_lane_order(row[0]), row[0]))],
        "prominent_sources": [{"source_name": name, "count": count, "notable_count": source_high[name]} for name, count in sources.most_common(8)],
        "suggested_posts": suggested_posts,
        "ingestion_candidates": [_brief(item) for item in sorted((item for item in enriched if int(item.get("ingest_score") or 0) >= INGEST_THRESHOLD), key=_ingest_rank_key)[:8]],
        "new_source_candidates": new_sources,
        "routine_updates": [_brief(item) for item in sorted((item for item in enriched if item.get("routine_update")), key=_rank_key)[:8]],
    }


def _topic(finding_type: str, text: str) -> str:
    if finding_type == "new_source_candidate":
        return "New sources to consider"
    if _has(text, "release", "changelog", "version", "tagged", "breaking"):
        return "Releases and major updates"
    if _has(text, "workflow", "bundle", "template", "automation", "crate"):
        return "Workflows, bundles, and templates"
    if _has(text, "app builder", "html", "ui", "component", "page"):
        return "App Builder and UI"
    if _has(text, "jinja", "filter", "schema", "json", "api"):
        return "Schemas, filters, Jinja, and APIs"
    if _has(text, "docs", "documentation", "reference", "guide", "lesson"):
        return "Docs and references"
    if _has(text, "reddit", "forum", "discord", "community", "youtube"):
        return "Community signals"
    if _has(text, "github", "commit", "repository", "repo"):
        return "Repository activity"
    if finding_type == "diff":
        return "Tracked page changes"
    return "General automation intelligence"


def _tags(finding_type: str, importance: str, text: str) -> list[str]:
    tags = []
    if finding_type == "new_source_candidate":
        tags.append("new-source")
    if importance == "high":
        tags.append("high-importance")
    for tag, needles in {
        "release": ("release", "changelog", "version"),
        "workflow": ("workflow", "bundle", "template"),
        "docs": ("docs", "documentation", "reference", "guide"),
        "community": ("reddit", "forum", "discord", "youtube", "community"),
        "github": ("github", "repository", "commit", "pull request"),
        "app-builder": ("app builder", "html", "ui", "component"),
        "api-schema": ("api", "schema", "json", "jinja", "filter"),
        "editorial": ("blog", "newsletter", "article", "linkedin", "post"),
    }.items():
        if _has(text, *needles):
            tags.append(tag)
    return sorted(set(tags)) or ["review"]


def _content_type(finding_type: str, text: str) -> str:
    if finding_type == "new_source_candidate":
        return "source"
    if _has(text, "blog", "newsletter", "article", "linkedin", "post"):
        return "article"
    if _has(text, "workflow", "bundle", "template"):
        return "workflow"
    if _has(text, "repo", "repository", "github"):
        return "repo"
    if _has(text, "lesson", "course", "tutorial", "guide"):
        return "education"
    if _has(text, "docs", "documentation", "reference", "api", "schema"):
        return "reference"
    return "update"


def _event_kind(finding_type: str, watch_type: str, text: str) -> str:
    if finding_type == "new_source_candidate":
        return "new_source"
    # Release/post signals from diff-style watchers are notable content, not routine source churn.
    if finding_type in {"release", "video", "post"} or _has(text, "new post", "new article", "release", "changelog"):
        return "new_content"
    if finding_type == "diff" or watch_type in {"github_repo_artifact_scan", "web_diff_watch"} or _has(text, "updated", "changed", "commit"):
        return "source_update"
    return "discovery"


def _publish_score(finding_type: str, importance: str, text: str, tags: list[str], content_type: str, event_kind: str) -> int:
    score = {"high": 45, "normal": 20, "low": 5}.get(importance, 15)
    score += 35 if finding_type == "new_source_candidate" else 0
    score += 25 if "release" in tags else 0
    score += 15 if content_type in {"article", "repo", "education"} else 0
    score += 20 if event_kind == "new_content" else 0
    score += 15 if "workflow" in tags or "api-schema" in tags else 0
    score += 10 if "docs" in tags or "app-builder" in tags else 0
    score += 10 if "community" in tags else 0
    if finding_type == "diff" and event_kind != "new_content" and not _has(text, "release", "major", "breaking"):
        score -= 10
    if event_kind == "source_update" and not _has(text, "release", "major", "new", "added", "bundle", "workflow"):
        score -= 15
    return max(0, min(score, 100))


def _ingest_score(importance: str, tags: list[str], content_type: str, event_kind: str, finding_type: str) -> int:
    if event_kind == "new_source":
        return 0
    high_value_tags = {"workflow", "api-schema", "app-builder"}.intersection(tags)
    if event_kind == "source_update" and finding_type in {"artifact", "diff"} and not high_value_tags:
        return 0
    score = {"high": 40, "normal": 20, "low": 5}.get(importance, 15)
    score += 30 if content_type in {"workflow", "reference", "repo"} else 0
    score += 25 if "workflow" in tags or "api-schema" in tags else 0
    score += 20 if "docs" in tags or "app-builder" in tags else 0
    score += 10 if content_type == "education" else 0
    return max(0, min(score, 100))


def _review_lane(event_kind: str, publish_score: int, ingest_score: int, tags: list[str], routine_update: bool) -> str:
    if "new-source" in tags or event_kind == "new_source":
        return "new_discoveries"
    if routine_update:
        return "routine_updates"
    if publish_score >= NEWSLETTER_THRESHOLD:
        return "publish_candidates"
    if ingest_score >= INGEST_THRESHOLD:
        return "kb_ingestion_candidates"
    if event_kind == "source_update":
        return "source_updates"
    return "low_signal"


def _notable_source_update(text: str) -> bool:
    return _has(text, "release", "major", "breaking", "workflow", "bundle", "schema", "api", "lesson", "tutorial")


def _suggested_action(publish_score: int, ingest_score: int, tags: list[str], topic: str, lane: str) -> str:
    if "new-source" in tags:
        return "Review source quality, then track it if it can feed library."
    if lane == "routine_updates":
        return "Routine tracked-source update. Refresh source freshness, do not gate unless it becomes notable."
    if publish_score >= STRONG_CANDIDATE_THRESHOLD:
        return "Strong Updates/newsletter candidate. Review for public summary and optional editorial note."
    if publish_score >= NEWSLETTER_THRESHOLD:
        return "Possible Updates mention. Batch with related findings."
    if ingest_score >= INGEST_THRESHOLD:
        return "Good library indexing candidate. Review for retrieval value even if it is not public-news worthy."
    if topic == "Repository activity":
        return "Likely source-update noise unless tied to a release, workflow, or docs change."
    return "Queue for normal review."


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "url", "source_name", "finding_type", "importance_signal", "review_topic", "review_tags", "content_type", "event_kind", "review_lane", "publish_score", "ingest_score", "newsletter_score", "routine_update", "suggested_action")
    return {key: item.get(key) for key in keys}


def _rank_key(item: dict[str, Any]) -> tuple[int, str]:
    return (-int(item.get("newsletter_score") or 0), str(item.get("title") or ""))


def _ingest_rank_key(item: dict[str, Any]) -> tuple[int, str]:
    return (-int(item.get("ingest_score") or 0), str(item.get("title") or ""))


def _lane_rank_key(item: dict[str, Any]) -> tuple[int, int, str]:
    return (-int(item.get("publish_score") or 0), -int(item.get("ingest_score") or 0), str(item.get("title") or ""))


def _lane_order(lane: str) -> int:
    return {"publish_candidates": 0, "kb_ingestion_candidates": 1, "new_discoveries": 2, "source_updates": 3, "routine_updates": 4, "low_signal": 5}.get(lane, 9)


def _lane_label(lane: str) -> str:
    return {
        "publish_candidates": "Publish candidates",
        "kb_ingestion_candidates": "library indexing candidates",
        "new_discoveries": "New discoveries",
        "source_updates": "Source updates",
        "routine_updates": "Routine updates",
        "low_signal": "Low-signal activity",
    }.get(lane, lane.replace("_", " ").title())


def _has(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)