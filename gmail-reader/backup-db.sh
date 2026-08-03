#!/usr/bin/env bash
# Consistent online backup of scholar-alerts SQLite → NAS (or local path).
set -euo pipefail

SOURCE_DB="${1:-$(cd "$(dirname "$0")" && pwd)/data/scholar-alerts.db}"
BACKUP_ROOT="${2:-/mnt/naspi5/content-agent-backups/gmail-reader}"
KEEP_COUNT="${BACKUP_KEEP_COUNT:-14}"

if [[ ! -f "$SOURCE_DB" ]]; then
  echo "Source database not found: $SOURCE_DB" >&2
  exit 1
fi

if [[ ! -d "$(dirname "$BACKUP_ROOT")" && ! -d "$BACKUP_ROOT" ]]; then
  echo "Backup root parent missing (is NAS mounted?): $(dirname "$BACKUP_ROOT")" >&2
  exit 1
fi

mkdir -p "$BACKUP_ROOT"

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_file="$BACKUP_ROOT/scholar-alerts-$timestamp.db"
latest_file="$BACKUP_ROOT/scholar-alerts-latest.db"
tmp_file="$BACKUP_ROOT/.scholar-alerts-$timestamp.db.partial"

cleanup() { rm -f "$tmp_file"; }
trap cleanup EXIT

# Prefer sqlite online backup API while UI may hold the DB open.
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$SOURCE_DB" ".timeout 60000" ".backup '$tmp_file'"
else
  echo "sqlite3 not found; falling back to cp (less safe while writers are active)" >&2
  cp "$SOURCE_DB" "$tmp_file"
fi

# Atomic-ish publish
mv -f "$tmp_file" "$backup_file"
cp -f "$backup_file" "$latest_file"

integrity="$(sqlite3 "$latest_file" 'PRAGMA integrity_check;')"
if [[ "$integrity" != "ok" ]]; then
  echo "Integrity check FAILED on $latest_file: $integrity" >&2
  exit 2
fi

articles="$(sqlite3 "$latest_file" 'SELECT count(*) FROM articles;')"
messages="$(sqlite3 "$latest_file" 'SELECT count(*) FROM messages;')"
bytes="$(stat -c%s "$backup_file" 2>/dev/null || stat -f%z "$backup_file")"

printf 'Backed up %s\n' "$SOURCE_DB"
printf 'Snapshot: %s (%s bytes)\n' "$backup_file" "$bytes"
printf 'Latest:   %s\n' "$latest_file"
printf 'Integrity: %s | articles=%s messages=%s\n' "$integrity" "$articles" "$messages"

# Retention: keep latest N timestamped snapshots (+ always keep *-latest.db)
mapfile -t old < <(ls -1t "$BACKUP_ROOT"/scholar-alerts-[0-9]*.db 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) || true)
if ((${#old[@]})); then
  printf 'Pruning %s old snapshot(s)\n' "${#old[@]}"
  rm -f -- "${old[@]}"
fi
