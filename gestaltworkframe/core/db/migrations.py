"""PRAGMA-based additive migrations for existing SQLite deployments.

Replace with Alembic before moving off SQLite. Until then, every schema
change must be additive (new columns with defaults, new indexes) so the
helpers here can apply them in place without dropping or rewriting tables.
init_db runs SQLModel.metadata.create_all first (creates tables that don't
exist yet), then runs each per-table migration to add any missing columns
or indexes.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import SQLModel

from gestaltworkframe.core.db.engine import engine
# Importing models registers them with SQLModel.metadata so create_all sees them.
from gestaltworkframe.core.db import models  # noqa: F401


_ID_BATCH_SIZE = 500


def _id_batches(ids: list[str], size: int = _ID_BATCH_SIZE):
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


async def _delete_discovery_find_ids(conn, ids: list[str]) -> None:
    for batch in _id_batches(ids):
        await conn.execute(
            text("DELETE FROM discovery_find WHERE id IN ({})".format(
                ",".join(f":id{i}" for i in range(len(batch)))
            )),
            {f"id{i}": rid for i, rid in enumerate(batch)},
        )


def _label_letter(idx: int) -> str:
    """Convert a 0-based index into a base-26 lowercase letter suffix.

    0 -> 'a', 25 -> 'z', 26 -> 'aa', 51 -> 'az', 52 -> 'ba', etc.
    Mirrors the live label-computer in core.newsletter so the
    migration backfill produces the same shape the runtime would.
    """
    if idx < 0:
        raise ValueError(f"label index must be non-negative, got {idx}")
    n = idx
    chars: list[str] = []
    while True:
        chars.append(chr(ord("a") + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(chars))


async def _migrate_contact_table(conn) -> None:
    result = await conn.execute(text("PRAGMA table_info(contactrecord)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        "ip_address": "VARCHAR NOT NULL DEFAULT ''",
        "updated_at": "DATETIME",
    }

    for name, ddl in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE contactrecord ADD COLUMN {name} {ddl}"))

    # Drop the dead `notified` column from older deployments. The flag
    # was replaced by the per-attempt `contact_notification` audit
    # table; the column lingered as `BOOLEAN NOT NULL` with no SQL
    # DEFAULT, so every fresh insert from the post-removal model
    # crashed with `NOT NULL constraint failed: contactrecord.notified`.
    # SQLite 3.35+ supports ALTER TABLE DROP COLUMN; older runtimes
    # will skip this branch and stay on the legacy schema. The model
    # never references the column either way.
    if "notified" in existing:
        try:
            await conn.execute(text("ALTER TABLE contactrecord DROP COLUMN notified"))
        except Exception:  # noqa: BLE001
            # SQLite below 3.35 cannot drop columns. Leave the column
            # in place; the only consequence is the original schema
            # bug returning. Operators on that runtime should upgrade
            # SQLite or run the documented manual rebuild.
            pass

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_contactrecord_email_role "
            "ON contactrecord (email, role)"
        )
    )


async def _migrate_discovery_source_table(conn) -> None:
    result = await conn.execute(text("PRAGMA table_info(discovery_source)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        "notes": "VARCHAR NOT NULL DEFAULT ''",
        # Phase A curation: featured sources get spotlight treatment on the
        # public library surface. Index supports filtering the curation queue.
        "featured": "BOOLEAN NOT NULL DEFAULT 0",
    }
    for name, ddl in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE discovery_source ADD COLUMN {name} {ddl}"))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_source_featured ON discovery_source (featured)")
    )


async def _migrate_discovery_find_table(conn) -> None:
    result = await conn.execute(text("PRAGMA table_info(discovery_find)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        "library_target_path": "VARCHAR NOT NULL DEFAULT ''",
        "library_file_url": "VARCHAR NOT NULL DEFAULT ''",
        "library_promotion_error": "VARCHAR NOT NULL DEFAULT ''",
        "promoted_at": "DATETIME",
        # Phase A curation: legacy single featured flag. Retained for
        # backwards compatibility with serializers and tests. Phase 2
        # introduces the purpose-specific flags below; the backfill at the
        # bottom of this migration copies featured=1 rows into
        # ticker_featured=1 so the public ticker keeps surfacing the same
        # material after the upgrade.
        "featured": "BOOLEAN NOT NULL DEFAULT 0",
        "featured_at": "DATETIME",
        # Phase 2 curation split:
        "ticker_featured": "BOOLEAN NOT NULL DEFAULT 0",
        "ticker_featured_at": "DATETIME",
        "newsletter_pending": "BOOLEAN NOT NULL DEFAULT 0",
        "dismissed": "BOOLEAN NOT NULL DEFAULT 0",
        # Stamped automatically when a NewsletterIssue containing this
        # find is approved + sent. Drives the new ticker query (30-day
        # rolling, max 10 items) so the ticker self-maintains from
        # newsletter activity without a per-find ticker-feature button.
        "published_in_newsletter_at": "DATETIME",
        "canonical_document_json": "TEXT NOT NULL DEFAULT ''",
        # Category rollup for sources that emit multiple leaf files per
        # logical signal (currently github_repo_artifact_scan). The
        # handler writes one find per category instead of one per file;
        # raw_payload.children carries the leaf list.
        "category": "VARCHAR NOT NULL DEFAULT ''",
        "child_count": "INTEGER NOT NULL DEFAULT 0",
        "last_upstream_updated_at": "DATETIME",
    }
    new_columns: list[str] = []
    for name, ddl in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE discovery_find ADD COLUMN {name} {ddl}"))
            new_columns.append(name)
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_find_featured ON discovery_find (featured)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_find_ticker_featured ON discovery_find (ticker_featured)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_find_newsletter_pending ON discovery_find (newsletter_pending)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_find_dismissed ON discovery_find (dismissed)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_find_published_in_newsletter_at "
             "ON discovery_find (published_in_newsletter_at)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_find_source_category "
             "ON discovery_find (discovery_source_id, category)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_find_last_upstream_updated_at "
             "ON discovery_find (last_upstream_updated_at)")
    )

    # One-shot backfill: any row whose legacy `featured` flag was set
    # before the Phase 2 split is treated as ticker-featured starting
    # at the time the column was added (now). Without this, every
    # operator-curated ticker entry would silently disappear from the
    # public surface the moment the new code goes live.
    if "ticker_featured" in new_columns:
        await conn.execute(
            text(
                "UPDATE discovery_find SET ticker_featured = 1, "
                "ticker_featured_at = COALESCE(featured_at, CURRENT_TIMESTAMP) "
                "WHERE featured = 1 AND ticker_featured = 0"
            )
        )


async def _collapse_artifact_finds_into_categories(conn) -> None:
    """One-shot data migration: collapse per-file artifact rows into
    per-category rows for github_repo_artifact_scan sources.

    Before: the artifact-scan handler emitted one DiscoveryFind per leaf
    file. With dozens of files per repo, the admin curation surface
    drowned in file-level noise.

    After: one DiscoveryFind per (source, category) where category is
    the first path segment under the source root. Leaf files live in
    raw_payload.children so retrieval still has the granular list.

    Re-entrant: already-collapsed category rows are used as representatives
    when present, and any remaining uncategorized legacy file rows are folded
    into them on the next run.

    Curation flags are preserved by union: if ANY child had
    ticker_featured=True, the resulting category row carries
    ticker_featured=True. Same for newsletter_pending and
    published_in_newsletter_at (max timestamp wins). dismissed is
    preserved only when ALL children were dismissed.
    """
    import json as _json

    migration_name = "collapse_artifact_finds_into_categories_v1"
    await conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name VARCHAR PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    ))
    applied = (await conn.execute(
        text("SELECT 1 FROM schema_migrations WHERE name = :name LIMIT 1"),
        {"name": migration_name},
    )).fetchone()
    if applied is not None:
        return

    # Find all artifact-scan sources that need collapsing.
    rows = (await conn.execute(text(
        "SELECT id, name, target FROM discovery_source "
        "WHERE watch_type = 'github_repo_artifact_scan'"
    ))).fetchall()
    if not rows:
        await conn.execute(
            text("INSERT OR REPLACE INTO schema_migrations (name) VALUES (:name)"),
            {"name": migration_name},
        )
        return

    select_columns = (
        "id, external_id, title, url, summary_text, raw_payload, "
        "importance_signal, first_seen_at, last_seen_at, status, "
        "decision_notes, reviewer, decided_at, ingested_into_chroma, "
        "published_to_library_repo, library_target_path, library_file_url, "
        "library_promotion_error, promoted_at, featured, featured_at, "
        "ticker_featured, ticker_featured_at, newsletter_pending, "
        "dismissed, published_in_newsletter_at, finding_type"
    )

    for source_id, source_name, source_target in rows:
        finds = (await conn.execute(
            text(
                f"SELECT {select_columns} FROM discovery_find "
                "WHERE discovery_source_id = :sid AND category = '' "
                "ORDER BY first_seen_at ASC, id ASC"
            ),
            {"sid": source_id},
        )).mappings().all()
        if not finds:
            continue

        groups: dict[str, list[dict]] = {}
        for row in finds:
            path = _category_path_from_external_id(row["external_id"]) or _category_path_from_title(
                row["title"], source_name,
            )
            category = path.split("/", 1)[0] if path else ""
            if not category:
                # Couldn't derive a category — leave the row alone.
                continue
            groups.setdefault(category, []).append(row)

        for category, group in groups.items():
            existing_rep = (await conn.execute(
                text(
                    f"SELECT {select_columns} FROM discovery_find "
                    "WHERE discovery_source_id = :sid "
                    "AND category != '' "
                    "AND (category = :category OR external_id = :external_id) "
                    "ORDER BY first_seen_at ASC, id ASC LIMIT 1"
                ),
                {"sid": source_id, "category": category, "external_id": f"category:{category}"},
            )).mappings().first()
            rep = existing_rep or group[0]  # earliest first_seen wins thanks to ORDER BY above
            children_by_external_id: dict[str, dict] = {}
            any_ticker = False
            any_pending = False
            # True only when every represented child row was dismissed.
            all_dismissed = True
            max_ticker_at = None
            max_published = None
            max_featured_at = None
            any_legacy_featured = False
            any_ingested = False
            any_published_repo = False
            max_last_seen = rep["last_seen_at"]
            try:
                rep_payload = _json.loads(rep["raw_payload"] or "{}")
            except Exception:
                rep_payload = {}
            for child in rep_payload.get("children") or []:
                if isinstance(child, dict) and child.get("external_id"):
                    child_path = str(child.get("path") or "") or _category_path_from_external_id(
                        str(child.get("external_id") or ""),
                    ) or _category_path_from_title(str(child.get("title") or ""), source_name)
                    child.setdefault("path", child_path)
                    child.setdefault("sha", _artifact_sha_from_external_id(str(child.get("external_id") or "")))
                    child.setdefault("kind", child.get("finding_type", "repo_artifact"))
                    child.setdefault("score", 0)
                    children_by_external_id[str(child["external_id"])] = child
            for row in group:
                child_path = _category_path_from_external_id(row["external_id"]) or _category_path_from_title(
                    row["title"], source_name,
                )
                children_by_external_id[row["external_id"]] = {
                    "external_id": row["external_id"],
                    "path": child_path,
                    "sha": _artifact_sha_from_external_id(row["external_id"]),
                    "kind": row["finding_type"],
                    "score": 0,
                    "title": row["title"],
                    "url": row["url"],
                    "summary_text": row["summary_text"],
                    "importance_signal": row["importance_signal"],
                    "finding_type": row["finding_type"],
                }
            flag_rows = ([existing_rep] if existing_rep else []) + group
            # Existing category rows are not children, but their curation flags
            # are operator intent and must survive a re-entrant cleanup pass.
            for row in flag_rows:
                if row["ticker_featured"]:
                    any_ticker = True
                    if row["ticker_featured_at"] and (
                        max_ticker_at is None or row["ticker_featured_at"] > max_ticker_at
                    ):
                        max_ticker_at = row["ticker_featured_at"]
                if row["newsletter_pending"]:
                    any_pending = True
                if not row["dismissed"]:
                    all_dismissed = False
                if row["published_in_newsletter_at"] and (
                    max_published is None or row["published_in_newsletter_at"] > max_published
                ):
                    max_published = row["published_in_newsletter_at"]
                if row["featured"]:
                    any_legacy_featured = True
                    if row["featured_at"] and (max_featured_at is None or row["featured_at"] > max_featured_at):
                        max_featured_at = row["featured_at"]
                if row["ingested_into_chroma"]:
                    any_ingested = True
                if row["published_to_library_repo"]:
                    any_published_repo = True
                if row["last_seen_at"] and (max_last_seen is None or row["last_seen_at"] > max_last_seen):
                    max_last_seen = row["last_seen_at"]

            children = list(children_by_external_id.values())
            rep_payload["children"] = children
            rep_payload["category"] = category
            source_target_clean = str(source_target or "").strip()
            collapsed_url = (
                f"https://github.com/{source_target_clean}/tree/HEAD/{category}"
                if source_target_clean else rep["url"]
            )

            await conn.execute(
                text(
                    "UPDATE discovery_find SET "
                    "  category = :category, "
                    "  child_count = :count, "
                    "  external_id = :external_id, "
                    "  title = :title, "
                    "  url = :url, "
                    "  raw_payload = :payload, "
                    "  last_seen_at = :last_seen, "
                    "  ticker_featured = :ticker, "
                    "  ticker_featured_at = :ticker_at, "
                    "  newsletter_pending = :pending, "
                    "  dismissed = :dismissed, "
                    "  published_in_newsletter_at = :published_at, "
                    "  featured = :legacy_featured, "
                    "  featured_at = :legacy_featured_at, "
                    "  ingested_into_chroma = :ingested, "
                    "  published_to_library_repo = :published_repo "
                    "WHERE id = :rep_id"
                ),
                {
                    "category": category,
                    "count": len(children),
                    "external_id": f"category:{category}",
                    "title": f"{source_name}/{category}" if source_name else category,
                    "url": collapsed_url,
                    "payload": _json.dumps(rep_payload),
                    "last_seen": max_last_seen,
                    "ticker": 1 if any_ticker else 0,
                    "ticker_at": max_ticker_at,
                    "pending": 1 if any_pending else 0,
                    "dismissed": 1 if all_dismissed else 0,
                    "published_at": max_published,
                    "legacy_featured": 1 if any_legacy_featured else 0,
                    "legacy_featured_at": max_featured_at,
                    "ingested": 1 if any_ingested else 0,
                    "published_repo": 1 if any_published_repo else 0,
                    "rep_id": rep["id"],
                },
            )
            if existing_rep is not None:
                delete_ids = [row["id"] for row in group]
            else:
                delete_ids = [row["id"] for row in group if row["id"] != rep["id"]]
            await _delete_discovery_find_ids(conn, delete_ids)

    await conn.execute(
        text("INSERT OR REPLACE INTO schema_migrations (name) VALUES (:name)"),
        {"name": migration_name},
    )


def _display_label_epoch(label: str) -> int | None:
    digits = ""
    for char in label:
        if not char.isdigit():
            break
        digits += char
    suffix = label[len(digits):]
    if not digits or not suffix or not suffix.islower() or not suffix.isalpha():
        return None
    return int(digits)


def _category_path_from_external_id(external_id: str) -> str:
    """Extract the path portion from `artifact:<path>:<sha>` external IDs.

    Returns "" when the external_id doesn't match that shape (already
    rolled up, RSS source, etc.)."""
    if not external_id or not external_id.startswith("artifact:"):
        return ""
    body = external_id[len("artifact:"):]
    if ":" not in body:
        path = body
    else:
        path = body.rsplit(":", 1)[0]
    return path if "/" in path else ""


def _artifact_sha_from_external_id(external_id: str) -> str:
    if not external_id or not external_id.startswith("artifact:"):
        return ""
    body = external_id[len("artifact:"):]
    if ":" not in body:
        return ""
    return body.rsplit(":", 1)[1]


def _category_path_from_title(title: str, source_name: str) -> str:
    """Fallback: derive the category path from the find title when the
    external_id isn't in artifact:<path>:<sha> shape.

    Titles look like '<source_name> artifact: <path>' for the v1 handler.
    Newer titles may use '<source_name> - <category>/<rest>' shape."""
    if not title:
        return ""
    marker = " artifact: "
    if marker in title:
        path = title.split(marker, 1)[1].strip()
        return path if "/" in path else ""
    prefix = f"{source_name} - " if source_name else ""
    if prefix and title.startswith(prefix):
        path = title[len(prefix):].strip()
        return path if "/" in path else ""
    return ""


async def _migrate_newsletter_issue_table(conn) -> None:
    """Additive migration for newsletter_issue. The table itself was
    created via SQLModel.metadata.create_all in Phase 3; this helper
    handles columns added after that initial deploy.
    """
    result = await conn.execute(text("PRAGMA table_info(newsletter_issue)"))
    if not result.fetchall():
        # Table doesn't exist yet on this environment; create_all will
        # build it on next run. Nothing to migrate.
        return
    result = await conn.execute(text("PRAGMA table_info(newsletter_issue)"))
    existing = {row[1] for row in result.fetchall()}
    columns = {
        # When the operator clicks Approve & schedule, this gets set to
        # the chosen send timestamp (default = now + 30 minutes). The
        # dispatcher polls for status=approved AND scheduled_send_at <=
        # now and ships those issues.
        "scheduled_send_at": "DATETIME",
        # Operator's intended send date, set at issue creation. The
        # daily cron uses this to fire the approval-reminder email
        # 24 hours ahead.
        "target_send_at": "DATETIME",
        # De-dupe guard so the daily approval-reminder cron doesn't
        # spam the operator if they don't react immediately.
        "approval_email_sent_at": "DATETIME",
        # Monotonic ship counter. Assigned only at successful send.
        # NULL for drafts / awaiting_approval / approved / sending /
        # skipped rows. Replaces the legacy auto-on-create issue_number.
        "ship_number": "INTEGER",
        # Sticky human-facing identifier set at creation. For sent rows
        # this is str(ship_number); for unsent rows it carries a
        # base-26 letter suffix anchored to max(ship_number) at the
        # time of creation (e.g. "0a", "1c"). UI / emails / archive
        # all read this instead of issue_number.
        "display_label": "VARCHAR NOT NULL DEFAULT ''",
        # When set, the issue is hidden from the public archive and
        # ticker. Used by the Unpublish action; preserves audit history
        # while pulling the issue from public surfaces.
        "unpublished_at": "DATETIME",
    }
    new_columns: list[str] = []
    for name, ddl in columns.items():
        if name not in existing:
            await conn.execute(text(f"ALTER TABLE newsletter_issue ADD COLUMN {name} {ddl}"))
            new_columns.append(name)
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_newsletter_issue_scheduled_send_at "
             "ON newsletter_issue (scheduled_send_at)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_newsletter_issue_target_send_at "
             "ON newsletter_issue (target_send_at)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_newsletter_issue_ship_number "
             "ON newsletter_issue (ship_number)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_newsletter_issue_display_label "
             "ON newsletter_issue (display_label)")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_newsletter_issue_unpublished_at "
             "ON newsletter_issue (unpublished_at)")
    )

    # Backfill existing/blank rows: shipped rows get ship_number assigned
    # in chronological order, and unsent rows get sticky display_label
    # values using the same epoch + base-26 suffix rule the live code uses.
    # This runs when the column is new or when create_all built the current
    # schema before migrations and left default blank labels behind.
    blank_label_count = (await conn.execute(
        text("SELECT COUNT(*) FROM newsletter_issue WHERE display_label = '' OR display_label IS NULL")
    )).scalar_one()
    if "display_label" in new_columns or int(blank_label_count or 0) > 0:
        rows = (await conn.execute(
            text(
                "SELECT id, status FROM newsletter_issue "
                "ORDER BY created_at ASC, id ASC"
            )
        )).fetchall()
        ship_counter = 0
        # Per-epoch unsent counters, keyed by the ship_number that the
        # epoch belongs to (0 before any send, 1 after the first send,
        # etc.). Letters are base-26: a..z, aa..az, ba..zz.
        unsent_counters: dict[int, int] = {}
        for row in rows:
            row_id, status = row[0], row[1]
            if status == "sent":
                ship_counter += 1
                label = str(ship_counter)
                await conn.execute(
                    text(
                        "UPDATE newsletter_issue "
                        "SET ship_number = :n, display_label = :label "
                        "WHERE id = :id"
                    ),
                    {"n": ship_counter, "label": label, "id": row_id},
                )
            else:
                idx = unsent_counters.get(ship_counter, 0)
                unsent_counters[ship_counter] = idx + 1
                label = f"{ship_counter}{_label_letter(idx)}"
                await conn.execute(
                    text(
                        "UPDATE newsletter_issue "
                        "SET display_label = :label WHERE id = :id"
                    ),
                    {"label": label, "id": row_id},
                )

    # UNIQUE indexes for ship_number and display_label.  These enforce
    # the same invariants that __table_args__ UniqueConstraint produces
    # on fresh installs.  Created after the backfill so the labels are
    # all distinct before the constraint is applied.
    # SQLite allows multiple NULLs in a UNIQUE index, so ship_number=NULL
    # on multiple drafts is fine without a partial-index workaround.
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_newsletter_issue_ship_number "
            "ON newsletter_issue (ship_number)"
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_newsletter_issue_display_label "
            "ON newsletter_issue (display_label)"
        )
    )

    # Drop the legacy issue_number column on SQLite 3.35+. Older
    # runtimes silently skip; the column lingers but nothing reads it.
    if "issue_number" in existing:
        try:
            await conn.execute(text(
                "ALTER TABLE newsletter_issue DROP COLUMN issue_number"
            ))
        except Exception:  # noqa: BLE001
            # SQLite < 3.35 cannot drop columns. Leave the column in
            # place; live code no longer reads it.
            pass
        # Drop the matching index if it survived the column drop or
        # the drop was skipped on legacy runtimes.
        try:
            await conn.execute(text(
                "DROP INDEX IF EXISTS ix_newsletter_issue_issue_number"
            ))
        except Exception:  # noqa: BLE001
            pass


async def _migrate_discovery_find_newsletter_issue_id(conn) -> None:
    """Add the newsletter_issue_id FK to discovery_find.

    Lives in its own helper because the migrate_discovery_find_table
    block above is already large and groups its columns by feature.
    This column belongs to the per-issue assignment work and should
    move together as a single readable unit.

    The column is nullable; existing rows are left unassigned. The
    follow-up migration `_assign_pending_finds_to_catchup_issue`
    creates a draft and points existing newsletter_pending=true rows
    at it so no queued items fall through the cracks.
    """
    result = await conn.execute(text("PRAGMA table_info(discovery_find)"))
    existing = {row[1] for row in result.fetchall()}
    if "newsletter_issue_id" not in existing:
        await conn.execute(text(
            "ALTER TABLE discovery_find ADD COLUMN newsletter_issue_id VARCHAR"
        ))
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_discovery_find_newsletter_issue_id "
             "ON discovery_find (newsletter_issue_id)")
    )


async def _assign_pending_finds_to_catchup_issue(conn) -> None:
    """One-shot: existing newsletter_pending=true finds get tagged
    onto a single catch-up draft so they survive the model shift.

    The catch-up issue is created only if pending finds exist AND no
    upcoming draft is already in place. target_send_at defaults to
    next 10-day boundary from the last sent issue, or 10 days from
    today when there's no prior cycle.

    Idempotent: skipped if any discovery_find row already has
    newsletter_issue_id set.
    """
    import uuid as _uuid
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    already = (await conn.execute(
        text("SELECT 1 FROM discovery_find WHERE newsletter_issue_id IS NOT NULL LIMIT 1")
    )).fetchone()
    if already is not None:
        return

    pending = (await conn.execute(
        text("SELECT id FROM discovery_find WHERE newsletter_pending = 1 AND dismissed = 0")
    )).fetchall()
    if not pending:
        return

    # Compute the target send date for the catch-up draft.
    last_sent = (await conn.execute(
        text(
            "SELECT target_send_at, scheduled_send_at, sent_at FROM newsletter_issue "
            "WHERE status = 'sent' ORDER BY sent_at DESC LIMIT 1"
        )
    )).fetchone()
    now = _dt.now(_tz.utc)
    if last_sent and (last_sent[0] or last_sent[1] or last_sent[2]):
        anchor = last_sent[0] or last_sent[1] or last_sent[2]
        if isinstance(anchor, str):
            anchor = _dt.fromisoformat(anchor.replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=_tz.utc)
        target = anchor + _td(days=10)
    else:
        target = now + _td(days=10)
    # If the computed target is already in the past, push it to
    # today + 1 so the cron has 24h headroom to send the reminder.
    if target <= now:
        target = now + _td(days=1)

    # Compute display_label for the catch-up draft using the same
    # rule the live code uses: anchor on max(ship_number), append a
    # base-26 letter suffix counting prior unsent rows in that epoch.
    last_ship_row = (await conn.execute(
        text("SELECT COALESCE(MAX(ship_number), 0) FROM newsletter_issue")
    )).fetchone()
    last_ship = int(last_ship_row[0] if last_ship_row else 0)
    label_rows = (await conn.execute(
        text("SELECT display_label FROM newsletter_issue WHERE ship_number IS NULL")
    )).fetchall()
    epoch_count = sum(
        1 for row in label_rows
        if _display_label_epoch(str(row[0] or "")) == last_ship
    )
    display_label = f"{last_ship}{_label_letter(epoch_count)}"

    issue_id = str(_uuid.uuid4())
    slug = f"catchup-{now.strftime('%Y%m%d-%H%M%S')}"
    await conn.execute(
        text(
            "INSERT INTO newsletter_issue ("
            "  id, display_label, slug, period_start, period_end, status, "
            "  editorial_markdown, finds_json, subject, approved_by, "
            "  target_send_at, created_at, updated_at, notes"
            ") VALUES ("
            "  :id, :label, :slug, :pstart, :pend, 'draft', '', '[]', "
            "  :subject, '', :target, :now, :now, "
            "  'Catch-up draft created during the per-issue-assignment migration.'"
            ")"
        ),
        {
            "id": issue_id,
            "label": display_label,
            "slug": slug,
            "pstart": now - _td(days=10),
            "pend": now,
            "subject": f"Issue {display_label}",
            "target": target,
            "now": now,
        },
    )
    pending_ids = [row[0] for row in pending]
    for batch in _id_batches(pending_ids):
        await conn.execute(
            text("UPDATE discovery_find SET newsletter_issue_id = :iid WHERE id IN ({})".format(
                ",".join(f":id{i}" for i in range(len(batch)))
            )),
            {"iid": issue_id, **{f"id{i}": rid for i, rid in enumerate(batch)}},
        )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _migrate_contact_table(conn)
        await _migrate_discovery_source_table(conn)
        await _migrate_discovery_find_table(conn)
        await _migrate_newsletter_issue_table(conn)
        # newsletter_issue_id FK on discovery_find. Must run AFTER the
        # newsletter_issue migration so issue_number is populated
        # before any code reads it.
        await _migrate_discovery_find_newsletter_issue_id(conn)
        # Run AFTER the column-add migration so the new fields exist.
        # Idempotent: skips sources that already have category rows.
        await _collapse_artifact_finds_into_categories(conn)
        # Migrate any existing newsletter_pending=true rows onto a
        # catch-up draft so they survive the per-issue model shift.
        # Idempotent: skips if any find already has an issue assigned.
        await _assign_pending_finds_to_catchup_issue(conn)
