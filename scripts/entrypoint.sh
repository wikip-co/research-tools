#!/usr/bin/env bash

set -euo pipefail

export AGENT_TOOLS_ROOT="${AGENT_TOOLS_ROOT:-/opt/content-agent-tools}"
export CONTENT_REPO_ROOT="${CONTENT_REPO_ROOT:-/workspace/content}"
export GMAIL_READER_DB="${GMAIL_READER_DB:-/var/lib/content-agent/gmail-reader/scholar-alerts.db}"

mkdir -p "$(dirname "$GMAIL_READER_DB")"

if [[ -n "${VAULT_SECRET_PATH:-}" || -n "${VAULT_GOOGLE_SECRET_PATH:-}" || -n "${VAULT_CLOUDINARY_SECRET_PATH:-}" ]]; then
  # Source the bootstrap so exported secrets remain available to the agent process.
  source /opt/content-agent-tools/scripts/fetch-vault-secrets.sh
fi

cd "$CONTENT_REPO_ROOT"
exec "$@"
