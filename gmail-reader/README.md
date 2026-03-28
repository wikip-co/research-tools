# gmail-reader

Small CLI tool for agent-friendly ingestion of Google Scholar alert emails from Gmail. All commands return JSON.

## What It Does

- Reads Google Scholar alert emails through the `gws` CLI
- Parses each alert email into structured article candidates
- Runs a first-pass heuristic triage to keep obviously relevant studies and filter noisy matches
- Stores message history and article candidates in SQLite for later processing
- Maintains canonical `papers` records so publish/archive state can survive repeated Scholar alerts
- Tracks each paper through a workflow lifecycle from discovery to merged PR

## Important Framing

This database is a research intake index, not a validated corpus.

That means:

- rows are parsed from Google Scholar alert emails
- triage labels are heuristic only
- `selected` means "looks promising"
- `review` means "ambiguous, needs agent or human judgment"
- `rejected` means "likely noisy or off-target," not "proven irrelevant"

Do not assume every stored article is accurate, high quality, or appropriate for the wiki without a second pass.

## Setup

This tool expects `gws` to already be installed and authenticated.

Environment requirements:

- `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE` or another supported `gws` auth mechanism
- `gws` available on `PATH`

Install dependencies:

```bash
uv sync
```

## Usage

Ingest Google Scholar alerts from the last 30 days:

```bash
uv run gmail-reader sync --days-back 30
```

Ingest a one-year backfill:

```bash
uv run gmail-reader sync --days-back 365
```

Limit the sync to 25 messages while testing:

```bash
uv run gmail-reader sync --days-back 30 --max-messages 25
```

List alert topics discovered in the database:

```bash
uv run gmail-reader alerts
```

List selected article candidates for further work:

```bash
uv run gmail-reader articles --status selected --limit 20
```

Inspect articles that still need manual review:

```bash
uv run gmail-reader articles --status review --limit 20
```

Use a custom database path:

```bash
uv run gmail-reader sync --db /tmp/scholar-alerts.db --days-back 90
```

Search today's Scholar emails with an arbitrary Gmail constraint and rank by topic:

```bash
uv run gmail-reader search --gmail-query 'label:inbox newer_than:1d' --topic 'strawberry muscle mass' --max-results 10
```

Search today's inbox and return the strongest already-selected items:

```bash
uv run gmail-reader search --gmail-query 'label:inbox newer_than:1d' --max-results 10
```

Search recent mail, include review items, and save what was parsed back into SQLite:

```bash
uv run gmail-reader search --gmail-query 'newer_than:2d' --include-review --save --max-results 20
```

## Common Workflows

Build the initial backlog from the last year:

```bash
uv run gmail-reader sync --days-back 365
```

Refresh the backlog each day:

```bash
uv run gmail-reader sync --days-back 2
```

Find recent strawberry-related research from the inbox:

```bash
uv run gmail-reader search --gmail-query 'label:inbox newer_than:1d' --topic 'strawberry muscle mass' --max-results 10
```

Find recent DevOps-oriented results from the inbox:

```bash
uv run gmail-reader search --gmail-query 'label:inbox newer_than:1d' --topic 'devops kubernetes terraform github actions' --max-results 10
```

List all alert topics currently in the database:

```bash
uv run gmail-reader alerts
```

Inspect a larger review queue:

```bash
uv run gmail-reader articles --status review --limit 100
```

Inspect all stored article rows without status filtering:

```bash
uv run gmail-reader articles --status all --limit 100
```

## Agent Contract

This project is designed for agent use through a small CLI surface that always writes JSON.

Preferred invocation form:

- `uv run gmail-reader ...`

Supported command patterns:

- Sync Scholar alerts: `gmail-reader sync [--days-back N | --after YYYY-MM-DD] [--before YYYY-MM-DD] [--max-messages N] [--query QUERY] [--db PATH]`
- List alert names: `gmail-reader alerts [--db PATH]`
- List stored articles: `gmail-reader articles [--status selected|review|rejected|all] [--alert-name NAME] [--limit N] [--db PATH]`
- Curate a recent topic-focused subset: `gmail-reader curate --topic "strawberry muscle mass" [--days-back N | --after YYYY-MM-DD] [--max-messages N] [--max-results N] [--query QUERY] [--db PATH]`
- Search recent Scholar mail with arbitrary Gmail filters: `gmail-reader search [--gmail-query QUERY] [--topic TOPIC] [--days-back N | --after YYYY-MM-DD] [--before YYYY-MM-DD] [--max-messages N] [--max-results N] [--include-review] [--save] [--query QUERY] [--db PATH]`
- List canonical papers: `gmail-reader papers [--status all|matched|unmatched|archived|unarchived] [--limit N] [--db PATH]`
- Filter papers by workflow state: `gmail-reader papers [--workflow-state discovered|scraped|matched|drafted|committed|pr_open|merged]`
- Find a paper: `gmail-reader find-paper <identifier> [--db PATH]`
- Upsert a paper: `gmail-reader upsert-paper --title TITLE [--url URL] [--doi DOI] [--pmid PMID] [--workflow-state STATE] [--matched-content-path PATH] [--db PATH]`
- Advance a paper: `gmail-reader set-paper-state <identifier> --state discovered|scraped|matched|drafted|committed|pr_open|merged [--matched-content-path PATH] [--commit SHA] [--pr URL] [--archive-path PATH] [--db PATH]`
- Mark a paper as published/matched: `gmail-reader mark-published <identifier> --matched-content-path PATH [--commit SHA] [--pr URL] [--db PATH]`
- Attach an archive path: `gmail-reader attach-archive <identifier> --archive-path PATH [--db PATH]`

Response contract:

- Success is printed to stdout as `{"ok": true, "result": ...}`
- Failures are printed to stderr as `{"ok": false, "error": "..."}`
- Exit code `0` indicates success
- Exit code `1` indicates runtime failure

Operational notes for agents:

- The default database path in the standalone runtime is `/var/lib/content-agent/gmail-reader/scholar-alerts.db`.
- The tool uses a Gmail search query scoped to Google Scholar alert messages by default.
- `sync` stores both the source message metadata and every parsed article candidate.
- Heuristic triage assigns each article one of `selected`, `review`, or `rejected`.
- `selected` is the working queue for downstream article processing.
- Alert occurrences remain separate, but canonical paper state is tracked in the `papers` table.
- Workflow state now distinguishes `scraped`, `matched`, `drafted`, `committed`, `pr_open`, and `merged` instead of treating all downstream work as simply "processed".
- `curate` does not depend on the stored triage alone; it re-reads a small recent mail window and returns parsed candidates that match the requested topic.
- This is intended for agent judgment on narrow user requests such as "today's strawberries research" or "three quality URLs from today's inbox".
- `search` is the more general on-demand entrypoint for agents. It lets the agent combine Gmail search operators like `label:inbox`, `newer_than:1d`, or quoted phrases with optional topic ranking.
- Use `--save` on `search` when you want the live query to also refresh the SQLite backlog.

## SQLite Schema Summary

The database contains three main tables:

- `messages`: one row per ingested Gmail message
- `articles`: one row per parsed Scholar result with triage fields and source links
- `papers`: one row per canonical paper identity with publish/archive state

Useful article columns:

- `alert_name`
- `title`
- `authors`
- `publication_info`
- `article_url`
- `pdf_url`
- `status`
- `score`
- `reasons_json`

## Database Location

The default database path is:

```bash
/var/lib/content-agent/gmail-reader/scholar-alerts.db
```

That is fine for active local work, but for long-term storage it is better to keep the authoritative copy somewhere backed up outside the repo working tree.

Recommended approach:

- Keep the working database local for speed
- Back it up to your NAS on a schedule
- Or point `--db` directly at a synced/backed-up path if the NAS mount is reliable and low-latency

Examples:

```bash
uv run gmail-reader sync --db "$HOME/research/scholar-alerts.db" --days-back 7
```

```bash
uv run gmail-reader sync --db "/mnt/nas/research/scholar-alerts.db" --days-back 7
```

For most setups, local primary DB plus NAS backup is safer than using the NAS file as the live primary database.

## Backup Script

This repo includes a simple manual backup script:

```bash
gmail-reader/backup-db.sh
```

Run it from the tool directory:

```bash
cd gmail-reader
bash ./backup-db.sh
```

By default it copies:

- source DB: `./data/scholar-alerts.db`
- backup folder: `/mnt/naspi5/content-agent-backups/gmail-reader`

It writes two files:

- a timestamped snapshot such as `scholar-alerts-20260309-170000.db`
- `scholar-alerts-latest.db`

You can also override both paths:

```bash
bash ./backup-db.sh /home/anthony/research/scholar-alerts.db /mnt/naspi5/content-agent-backups/gmail-reader
```

## Heuristic Triage

The first-pass scoring is intentionally simple and transparent:

- Boost likely review papers, meta-analyses, systematic reviews, and human-health topics
- Boost multi-author studies
- Down-rank obvious agriculture, feed, crop, aquaculture, and pet-food matches
- Down-rank likely low-signal items such as single-author results

This is only a screening pass. Anything ambiguous is marked `review` instead of being discarded.

The heuristic labels should be treated as soft metadata for retrieval, not as final editorial judgment.
