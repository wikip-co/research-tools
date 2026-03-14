# content-agent-tools

Standalone runtime for the `content` repo's agent tooling.

This repository separates the operational tooling from the markdown content repository so you can run agents on a stable machine instead of a laptop. It packages the current tools into one image and expects the content repo to be mounted at runtime.

## Included Tools

- `gmail-reader`: read Google Scholar alert mail via `gws` and store results in SQLite
- `wiki-automation`: build queues, search content, and prepare scrape packets
- `image-upload`: upload article images to Cloudinary
- `web-scraper`: scrape source URLs into structured packets

## Runtime Model

- The content repo is mounted read-write at `/workspace/content`
- The SQLite DB is mounted from the host at `/var/lib/content-agent/gmail-reader`
- Secrets are pulled from `vault.wikip.co` at container start
- `gws` is installed in the image with `npm install -g @googleworkspace/cli`

## Required Vault Fields

The bootstrap can read either one combined secret or separate secrets for each service.

Default Google secret:

- Path: `secret/data/Google/oauth`
- Field: `google_workspace_cli_credentials_json`

Default Cloudinary secret:

- Path: `secret/data/cloudinary`
- Fields: `cloud_name`, `key`, `secret`

You can override the paths and field names with environment variables.

## Quick Start

1. Clone your content repo onto the stable machine.
2. Clone this repo alongside it.
3. Create a `.env` file in the repo root and fill in the Vault connection values.
4. Create a host data directory for the SQLite DB.
5. Start the container:

```bash
docker compose run --rm agent-tools agent-workflow help
```

To restore the latest Gmail reader DB into the mounted host path:

```bash
cp /mnt/naspi5/content-agent-backups/gmail-reader/scholar-alerts-latest.db \
  ./runtime/gmail-reader/scholar-alerts.db
```

## Environment

Important runtime environment variables:

- `CONTENT_REPO_ROOT=/workspace/content`
- `AGENT_TOOLS_ROOT=/opt/content-agent-tools`
- `GMAIL_READER_DB=/var/lib/content-agent/gmail-reader/scholar-alerts.db`
- `VAULT_ADDR=https://vault.wikip.co`
- `VAULT_GOOGLE_SECRET_PATH=secret/data/Google/oauth`
- `VAULT_CLOUDINARY_SECRET_PATH=secret/data/cloudinary`

Vault bootstrap variables:

- `VAULT_TOKEN` or `VAULT_TOKEN_FILE`
- Or `VAULT_USERNAME` plus `VAULT_PASSWORD` or `VAULT_PASSWORD_FILE`
- `VAULT_KV_VERSION=2`
- `VAULT_AUTH_PATH=auth/userpass/login`
- `VAULT_SECRET_PATH`
- `VAULT_GOOGLE_SECRET_PATH`
- `VAULT_CLOUDINARY_SECRET_PATH`
- `VAULT_SECRET_JSON_KEY=google_workspace_cli_credentials_json`
- `VAULT_CLOUDINARY_CLOUD_NAME_KEY=cloud_name`
- `VAULT_CLOUDINARY_API_KEY_KEY=key`
- `VAULT_CLOUDINARY_API_SECRET_KEY=secret`

`VAULT_SECRET_PATH` remains as a backward-compatible fallback when both services live in one secret. If no Vault token is provided, the bootstrap script logs in with the `userpass` auth endpoint and uses the returned client token for the secret reads.

## Layout

- `agent-workflow`: wrapper for the operational commands
- `scripts/entrypoint.sh`: container entrypoint
- `scripts/fetch-vault-secrets.sh`: fetch and export runtime secrets
- `docker-compose.yml`: local deployment template

## Notes

- Use a single active writer for the SQLite DB. Do not run multiple containers on multiple machines against the same database file at the same time.
- The content repo no longer needs to carry in-repo agent tooling.
