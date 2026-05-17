# Disaster Recovery Runbook

Operational procedures for backup, restore, and incident recovery on a
production deployment of this framework. Update it when the procedure
changes; out-of-date runbooks are worse than no runbook.

The example commands below use environment placeholders that should be
set per deployment:

- `$VPS_HOST`, `$VPS_USER` — SSH target for the production host
- `$APP_DIR` — application directory on the host (e.g. `/opt/<deployment>`)
- `$SERVICE_NAME` — systemd unit for the API (e.g. `<deployment>`)
- `$RUN_USER` — user the service runs as
- `$BACKUP_DIR` — where snapshots are written (e.g. `/var/backups/<deployment>`)
- `$PUBLIC_URL` — public base URL for health/admin endpoints

## Backups

### What is backed up

Production data lives in SQLite files inside `$APP_DIR`. The nightly
backup workflow snapshots every configured DB file via SQLite's online
`.backup` command, which produces a transactionally consistent copy
without taking the application offline.

By default the workflow backs up:

- `$APP_DIR/database.db` (resolved from `APP_DATABASE_PATH` in
  `$APP_DIR/.env` when set, otherwise the documented default)

Any corpus repository written by the publisher is Git-backed. Files
published there are recovered or removed with normal GitHub history/revert
operations.

Chroma (`kb/chroma_db/`) is a derived retrieval index, not a primary backup
artifact. Recover it by stopping the app and rebuilding from clean configured
sources with `uv run python -m kb.maintenance rebuild-chroma --yes`.

Additional paths can be added via `EXTRA_DB_PATHS` in
`.github/workflows/backup.yml` (space-separated absolute paths).

### Where backups live

Host path: `$BACKUP_DIR`

Files are named `database-YYYYMMDD-HHMMSS.sqlite.gz` (UTC timestamp).
The 14 most recent days are retained; older files are pruned by the
backup script.

### Schedule

Daily at 03:11 UTC via the `Database backup` GitHub Actions workflow.
The workflow can also be triggered manually from the Actions tab with
`workflow_dispatch`.

### Verifying a backup

After the workflow runs, the workflow log shows lines like:

```
ok: $APP_DIR/database.db -> $BACKUP_DIR/database-YYYYMMDD-HHMMSS.sqlite.gz (N bytes)
summary: success=1 failure=0 retained_window_days=14
```

Cross-check on the host:

```bash
ssh "$VPS_USER@$VPS_HOST" ls -lh "$BACKUP_DIR"
```

A working backup is at least a few KB in size, never zero. If the
workflow reports `failure_count > 0`, the job exits non-zero and the
GitHub Actions UI shows a red status.

### Manual backup

To take a snapshot outside the schedule, run the workflow manually from
the Actions UI or invoke the backup script on the host with the
appropriate `APP_DIR` value.

## Restore

### Restore the most recent backup

The application reads `APP_DATABASE_PATH` (or `APP_DATABASE_URL`) from
the service `.env` to find its DB. Restore = stopping the service,
replacing the file, restarting.

```bash
ssh "$VPS_USER@$VPS_HOST"

# 1. Stop the API to release the WAL.
sudo systemctl stop "$SERVICE_NAME"

# 2. Pick the backup file to restore (newest is normally the right one).
ls -lh "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/database-YYYYMMDD-HHMMSS.sqlite.gz"

# 3. Move the current DB aside so you can recover from it if needed.
sudo mv "$APP_DIR/database.db" "$APP_DIR/database.db.broken-$(date +%s)"

# 4. Decompress the backup into place.
sudo gunzip -c "$BACKUP" > /tmp/restore.db
sudo chown "$RUN_USER:$RUN_USER" /tmp/restore.db
sudo mv /tmp/restore.db "$APP_DIR/database.db"

# 5. Quick integrity check before bringing the service back.
sudo -u "$RUN_USER" sqlite3 "$APP_DIR/database.db" "PRAGMA integrity_check;"
# Expected output: ok

# 6. Restart and verify.
sudo systemctl start "$SERVICE_NAME"
curl -s "$PUBLIC_URL/health"
# Expected: {"status":"ok"}
```

### Restore a specific point in time

The backup file name is `database-YYYYMMDD-HHMMSS.sqlite.gz` in UTC.
Pick the most recent backup file before the target time. SQLite's WAL
flushes on each commit, so the backup is point-in-time accurate to
the moment the `.backup` call started.

### Restore to a clone (test before touching production)

For drills and post-incident forensics, restore to a sandbox first:

```bash
# Pull the backup down to a workstation.
scp "$VPS_USER@$VPS_HOST:$BACKUP_DIR/database-YYYYMMDD-HHMMSS.sqlite.gz" /tmp/

# Inspect locally.
gunzip /tmp/database-YYYYMMDD-HHMMSS.sqlite.gz
sqlite3 /tmp/database-YYYYMMDD-HHMMSS.sqlite ".tables"
sqlite3 /tmp/database-YYYYMMDD-HHMMSS.sqlite "SELECT count(*) FROM conversation;"
```

## Recovery scenarios

### "SQLite database is locked" or write errors on prod

The DB itself is usually fine. Restart the service to clear stale
locks:

```bash
sudo systemctl restart "$SERVICE_NAME"
sudo journalctl -u "$SERVICE_NAME" -n 100 --no-pager
```

If that does not resolve it, check disk space (`df -h`) and inode
exhaustion (`df -i`). Out-of-space conditions cause WAL writes to
fail.

### Corrupt database

If `PRAGMA integrity_check` reports errors:

1. Stop the API: `sudo systemctl stop "$SERVICE_NAME"`
2. Try `.recover`: `sudo -u "$RUN_USER" sqlite3 "$APP_DIR/database.db" ".recover" > /tmp/recovered.sql`
3. Apply to a fresh file: `sqlite3 /tmp/recovered.db < /tmp/recovered.sql`
4. Run `PRAGMA integrity_check` on the recovered file.
5. If it passes, swap it in.
6. If recovery fails, restore from the most recent backup per the
   restore procedure above.

### Lost host

Worst case: the entire host is unrecoverable. New host steps:

1. Provision a fresh host that matches the original capacity.
2. Update the `VPS_HOST` GitHub secret to the new IP/hostname.
3. Run the `Deploy production` workflow from the Actions UI.
4. Once deploy completes, copy the most recent backup from wherever it
   was preserved (off-host mirror, GitHub Actions artifact store, or a
   manual `scp` taken in advance).
5. Restore per the procedure above.
6. Update DNS only after `/health` reports OK from the new host.

### Cloud-budget accounting is stuck

If the admin health panel shows the cloud budget store reporting
`accounting_blocked: true`, a provider returned malformed token-usage
metadata and the gate paused all spend. Recovery:

```bash
curl -X POST \
  -H "X-Admin-Token: $ADMIN_POLICY_TOKEN" \
  "$PUBLIC_URL/admin/api/cloud-budget/clear-accounting-block"
```

The endpoint clears `accounting_blocked` and returns a fresh budget
snapshot. Verify the snapshot shows `accounting_blocked: false` before
expecting cloud spillover to resume.

### Discovery/KB poisoning response

Use this for accidental internal approval or a later-discovered bad source.
Public users cannot directly publish into the public feed, the corpus, or
Chroma, but an operator can approve the wrong thing.

1. Freeze intake from the bad source: open `/admin/discovery`, Source
   health, and set the source inactive. If the scheduler is actively
   compounding the incident, disable the discovery workflow or rotate/
   withhold `ADMIN_POLICY_TOKEN` until triage completes.
2. Remove public exposure: use the admin Discovery affordances to
   unpublish the affected finding from the public feed and the corpus,
   and to purge it from the KB index.
3. For one-off Chroma cleanup from the shell:

```bash
cd "$APP_DIR"
sudo systemctl stop "$SERVICE_NAME"
sudo -u "$RUN_USER" uv run python -m kb.maintenance purge-discovery-find FIND_ID
sudo systemctl start "$SERVICE_NAME"
curl -s "$PUBLIC_URL/health"
```

4. For broad contamination, rebuild the index from clean sources:

```bash
cd "$APP_DIR"
sudo systemctl stop "$SERVICE_NAME"
sudo -u "$RUN_USER" uv run python -m kb.maintenance rebuild-chroma --yes
sudo systemctl start "$SERVICE_NAME"
curl -s "$PUBLIC_URL/health"
```

5. If discovery DB state itself is contaminated, restore SQLite from the
   last known-good backup, then re-run the Chroma rebuild. If corpus
   files were published, revert/delete them in the corpus repo as part
   of the same incident.

Cache note: public feeds have a short max-age plus stale-while-revalidate
window. Retrieval cache defaults to 5 minutes; a service restart clears
process cache immediately.

## Drill cadence

Run a restore drill quarterly. Use a recent backup, restore it into a
sandbox (not production), verify the chat path, and write the date and
outcome in this section so the cadence is auditable.

### Drill log

| Date | Backup tested | Operator | Outcome | Notes |
|---|---|---|---|---|
| (next drill) | | | | |
