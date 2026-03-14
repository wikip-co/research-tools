#!/usr/bin/env bash
set -euo pipefail

SOURCE_DB="${1:-$(pwd)/data/scholar-alerts.db}"
BACKUP_ROOT="${2:-/mnt/naspi5/content-agent-backups/gmail-reader}"

if [[ ! -f "$SOURCE_DB" ]]; then
  echo "Source database not found: $SOURCE_DB" >&2
  exit 1
fi

mkdir -p "$BACKUP_ROOT"

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_file="$BACKUP_ROOT/scholar-alerts-$timestamp.db"
latest_file="$BACKUP_ROOT/scholar-alerts-latest.db"

cp "$SOURCE_DB" "$backup_file"
cp "$SOURCE_DB" "$latest_file"

printf 'Backed up %s\n' "$SOURCE_DB"
printf 'Snapshot: %s\n' "$backup_file"
printf 'Latest: %s\n' "$latest_file"
