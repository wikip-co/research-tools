# content-agent-tools

Standalone runtime for the `content` repo's agent tooling.

This repository separates the operational tooling from the markdown content repository so you can run agents on a stable machine instead of a laptop. It packages the current tools into one image and expects the content repo to be mounted at runtime.

**Operators / Hermes handoff:** see [`RELEASE_NOTES.md`](./RELEASE_NOTES.md) for current host layout and [`docs/local-research-publisher.md`](docs/local-research-publisher.md) for the production local-model path. The canonical cross-repository administration guide is [`../docs/research-production-operations.md`](../docs/research-production-operations.md).

## Included Tools

- `gmail-reader`: read Google Scholar alert mail via `gws` and store results in SQLite
- `gmail-reader-web`: LAN web UI for browsing the SQLite intake DB, triaging rows, and launching Codex processing jobs
- `wiki-automation`: build queues, search content, and prepare scrape packets
- `image-upload`: upload article images to Cloudinary, including browser-captured screenshots
- `web-scraper`: scrape source URLs into structured packets, with optional FlareSolverr (Cloudflare) and `agent-browser` fallbacks
- local llama.cpp publisher: guarded ad-hoc URL processing plus a durable SQLite queue, structured draft/critic passes, deterministic gates, isolated worktrees, and optional draft PRs

## Runtime Model

- The content repo is mounted read-write at `/workspace/content`
- The SQLite DB is mounted from the host at `/var/lib/content-agent/gmail-reader`
- Secrets are pulled from `vault.wikip.co` at container start
- `gws` and `agent-browser` are installed in the image with `npm install -g`

## Required Vault Fields

The bootstrap can read either one combined secret or separate secrets for each service.

Default Google secret:

- Preferred path: `secret/data/Google/oauth/credentials`
- Fallback path also supported: `secret/data/Google/oauth`
- Preferred field: `google_workspace_cli_credentials_json`

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
- `VAULT_GOOGLE_SECRET_PATH=secret/data/Google/oauth/credentials`
- `VAULT_CLOUDINARY_SECRET_PATH=secret/data/cloudinary`
- `LOCAL_LLM_BASE_URL=http://127.0.0.1:8080/v1`
- `LOCAL_LLM_MODEL=qwen3.6-35b-a3b-q8_0-mtp`

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
- `auth-bootstrap`: fetch Google OAuth credentials from Vault
- `scripts/entrypoint.sh`: container entrypoint
- `scripts/fetch-vault-secrets.sh`: fetch and export runtime secrets
- `docker-compose.yml`: local deployment template

## Local Setup

For local development without Docker:

```bash
# Initial setup - install dependencies and verify auth
./agent-workflow setup

# Load credentials into your shell
source auth-bootstrap
```

Local workflow note:

- `agent-workflow` no longer searches or edits the `research-tools` checkout by default.
- If `CONTENT_REPO_ROOT` is not explicitly set, it now uses a managed working copy at `runtime/content-repo`.
- On first use it clones from `CONTENT_REPO_SOURCE_PATH` when available, otherwise from `CONTENT_REPO_GIT_URL`.

The local bash entrypoints (`agent-workflow`, `auth-bootstrap`, and `scripts/fetch-vault-secrets.sh`) now parse the repo `.env` safely when present. `image-upload` also reads `.env` directly and can bootstrap Cloudinary credentials from Vault when only the Vault settings are present.

For workspace-level setup and CI-aligned installs:

```bash
uv sync --all-packages
make test
```

## Agent Workflow Commands

```bash
# Setup and authentication
./agent-workflow setup                    # Install deps, verify auth
./agent-workflow doctor                   # JSON environment and repo health check
./agent-workflow sync-content-repo        # Refresh the managed content repo clone
source auth-bootstrap                     # Load Google credentials

# Search and discovery
./agent-workflow search "resveratrol"     # Search content repo
./agent-workflow match "spine health"     # Find matching articles
./agent-workflow tags                     # List all tags with counts
./agent-workflow tags --suggest "cardio"  # Suggest related tags
./agent-workflow audit-tags               # Find tag normalization conflicts
./agent-workflow lint-frontmatter         # Scan content markdown frontmatter
./agent-workflow check-ref "<url>"        # Check if URL already cited
./agent-workflow check-duplicate-paper "<url-or-doi>"

# Email processing
./agent-workflow queue --topic "health"   # Build queue from Gmail
./agent-workflow backlog --open-access    # Query stored backlog

# Article operations
./agent-workflow intake "<url-or-pdf>"    # Scrape + dedupe + match + optional archive
./agent-workflow ingest-paper "<url-or-pdf>" --archive
./agent-workflow prepare "<url>"          # Scrape and create new article
./agent-workflow append "<url>" \         # Append research to existing article
  --target "path/to/article.md" \
  --section "Disease / Symptom Treatment" \
  --subsection "Spine Health" \
  --apply                                 # Use --apply to write, --commit to git commit
./agent-workflow archive-source "<url-or-file>"
./agent-workflow open-pr --fill
./agent-workflow publish-pr --draft

# Production local-model path (dry run is the default)
./agent-workflow local-publish "<url-or-pdf>"
./agent-workflow enqueue-local-backlog --status selected --min-score 12 --limit 10
./agent-workflow local-worker --max-jobs 1
./agent-workflow local-worker --max-jobs 1 --publish
```

## Web Triage UI

Run the local-area-network web UI from `research-tools`:

```bash
./agent-workflow triage-ui
```

By default it binds to:

```text
http://0.0.0.0:8765
```

Open it from another machine on the LAN with this computer's LAN IP address, for example:

```text
http://192.168.1.x:8765
```

The UI reads and writes the Gmail Reader SQLite DB. It supports:

- filtering by status, processed state, alert name, score, and text search
- marking rows as `selected`, `review`, `rejected`, or `invalid`
- marking rows processed without deleting them
- launching a background Codex job for selected rows
- viewing job logs and any detected GitHub PR URL

The Codex job runner uses:

```bash
codex --sandbox danger-full-access --ask-for-approval never exec -C <workspace-root> -
```

The generated prompt tells Codex to read `docs/research-publishing-style-guide.md`, process the selected rows through the existing tooling, update the `content` repo, and open a draft PR for review. The web runner sets `processed_at` only after a successful exit that reports a draft PR URL; a zero exit without a PR is not publication success.

Useful overrides:

- `GMAIL_READER_DB=/path/to/scholar-alerts.db`
- `GMAIL_READER_WEB_HOST=0.0.0.0`
- `GMAIL_READER_WEB_PORT=8765`
- `RESEARCH_WORKSPACE_ROOT=/home/anthony/Research`
- `CODEX_BIN=/path/to/codex`
- `CODEX_WEB_EXTRA_ARGS="--model gpt-5.4"`

### User Service

On the current workstation, the triage UI is installed as a user-level systemd service:

```bash
systemctl --user status research-triage-ui.service
systemctl --user start research-triage-ui.service
systemctl --user stop research-triage-ui.service
systemctl --user restart research-triage-ui.service
systemctl --user enable research-triage-ui.service
```

The service file is:

```text
~/.config/systemd/user/research-triage-ui.service
```

It runs `agent-workflow triage-ui` from this repo, binds to `0.0.0.0:8765`, and uses `gmail-reader/data/scholar-alerts.db`.

A tracked copy of the current service definition is kept at:

```text
systemd/research-triage-ui.service
```

To install or reproduce the service on this host:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/research-triage-ui.service ~/.config/systemd/user/research-triage-ui.service
systemctl --user daemon-reload
systemctl --user enable research-triage-ui.service
systemctl --user start research-triage-ui.service
```

If paths differ from `/home/anthony/Research`, edit the copied service file before `daemon-reload`.

## Publishing Through Content

The intended workflow is:

1. Use these tools to decide whether a source belongs in an existing article or a new markdown page in `content`.
2. Write only to the `content` repo.
3. Commit on a branch and open a PR in `content`.
4. After merge to `content/main`, that repo triggers downstream site rebuilds such as `wikip.co`.

Agents should not write generated HTML directly. The static site and Cloudflare deployment path is downstream from `content`.

For a task like "analyze a PDF and incorporate the findings into the content repo":

- Search or match first to avoid creating duplicate pages.
- Preserve the repo's existing markdown and footnote style.
- Cite each published claim with the appropriate footnote.
- Add or update the article reference list.
- Open the PR in `content`, not in `wikip.co`.

## New Features (2026-03)

### `append` command
Scrape a URL and append the research to an existing article. Handles:
- Automatic reference numbering
- Duplicate reference detection
- Tag suggestions based on content
- Section/subsection placement
- Optional git commit

### `tags` command
List all tags in the content repo with frequency counts, or suggest tags matching a query.

### `check-ref` command
Check if a URL is already referenced in any article in the content repo.

### Study Type Detection
The web-scraper now automatically detects study types (Review, Meta-Analysis, RCT, In Vivo, In Vitro, etc.) from article metadata.

### Browser Fallback and Screenshots
- `web-scraper` fallback order on weak/blocked HTML: **scrapling → FlareSolverr → agent-browser**
- `--flaresolverr-mode auto|off|force` solves Cloudflare / bot interstitials via local FlareSolverr (`FLARESOLVERR_URL`, default `http://127.0.0.1:8191/v1`)
- Full text when available; **abstract-only is acceptable** for paywalled publisher pages
- Deploy/update FlareSolverr (always pulls `latest`): see `deploy/README.md` and `deploy/docker-compose.flaresolverr.yml`
  - `docker compose -f deploy/docker-compose.flaresolverr.yml up -d --pull always`
- `web-scraper` supports `--agent-browser-mode auto|off|force` for pages that need a browser-rendered fallback
- `image-upload --capture-url "<url>"` captures a browser screenshot, uploads it to Cloudinary, and returns the hosted URLs in JSON

### PDF Intake and Source Archiving
- `web-scraper` now accepts local PDFs and PDF URLs in addition to HTML URLs
- `wiki-automation ingest-paper "<url-or-pdf>"` emits a normalized packet with match suggestions
- `wiki-automation intake "<url-or-pdf>"` performs scrape, duplicate checks, content matching, and optional archiving without modifying content
- `wiki-automation archive-source "<url-or-file>"` stores a raw snapshot for provenance and records it in the paper index

### Canonical Paper Tracking
- `gmail-reader` now maintains a `papers` table alongside alert occurrences
- Paper records now track workflow state (`discovered`, `scraped`, `matched`, `drafted`, `committed`, `pr_open`, `merged`) plus archive state and git metadata
- `gmail-reader papers`, `find-paper`, `set-paper-state`, `mark-published`, and `attach-archive` expose that state to agents

### PR Publication Workflow
- `wiki-automation publish-pr` creates or reuses a branch, commits changed article markdown, pushes, and opens a PR
- The publish workflow advances matched papers from `drafted` to `committed` to `pr_open` based on the affected article paths

### Web Triage and Agent Jobs
- `gmail-reader-web` provides a LAN-accessible table UI for the SQLite intake DB
- Rows can be marked `selected`, `review`, `rejected`, or `invalid` without deleting data
- Selected rows can launch background Codex jobs that update `content` and submit draft PRs
- Web job state is stored in `article_jobs` and `article_job_items`

### Local llama.cpp Publisher

- `agent-workflow local-publish URL` runs an ad-hoc dry-run through packet,
  retrieval, draft, critic, deterministic validation, and isolated-worktree gates.
- Add `--publish` to open a draft PR after every gate passes.
- `enqueue-local`, `enqueue-local-backlog`, and `local-worker` provide the durable
  SQLite queue used by the passive database workflow.
- See [docs/local-research-publisher.md](docs/local-research-publisher.md) for
  packet semantics, rollout commands, and the optional systemd timers.

### Workspace and CI
- The repo now has a root `uv` workspace, root `uv.lock`, `Makefile`, and GitHub Actions CI
- Runtime DB files are ignored by default and the checked-in SQLite database has been removed from source control

## Notes

- Use a single active writer for the SQLite DB. Do not run multiple containers on multiple machines against the same database file at the same time.
- The content repo no longer needs to carry in-repo agent tooling.
