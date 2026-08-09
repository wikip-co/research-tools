# Local Research Publisher

This is the production path for turning an ad-hoc source URL or a queued Google
Scholar result into a reviewed draft pull request in `content` using the local
llama.cpp model.

For host-wide service administration, backup/recovery procedures, diagrams,
and downstream deployment, use the canonical workspace guide at
[`../../docs/research-production-operations.md`](../../docs/research-production-operations.md).

## Packet Contract

A packet is the normalized evidence bundle produced before the LLM runs. It
contains the requested and resolved URLs, title, DOI/PMID, authors, journal,
date, study type, abstract, extracted body, scraper backend, citation metadata,
and retrieval/consistency warnings.

The packet gate rejects CAPTCHA, robot, challenge, access-error, and publisher
error pages regardless of body length. It also rejects a DOI when its Crossref
title is inconsistent with the scraped article title. FlareSolverr receives the
original article URL as a fallback; CAPTCHA HTML from an earlier scraper is not
passed into FlareSolverr. A challenge response returned by FlareSolverr is still
invalid and can fall through to agent-browser.

## Quality Gates

The publisher:

1. scrapes with the existing Scrapling → FlareSolverr → agent-browser path;
2. validates the packet before enrichment or model use;
3. checks URL/DOI duplicates;
4. retrieves candidate content homes using title, abstract, keywords, alert
   topic, tags, paths, and article bodies;
5. asks the local model for a structured append plan;
6. requires each proposed bullet to carry an exact source quote and checks that
   the bullet remains near-verbatim;
7. runs a separate critic pass for support, study-type inflation, and medical
   overclaiming;
8. applies only a validated plan in an isolated git worktree based on
   `origin/main`; and
9. opens a draft PR only when `--publish` is supplied and every gate passes.

For paths under `Natural Healing/`,
`Research/docs/natural-healing-content-style-guide.md` is authoritative. Its
near-verbatim, bullet-first style is intentionally preserved.

## Ad-hoc Usage

Dry-run first. This writes a JSON report, proposed patch, and isolated worktree
without pushing anything:

```bash
./agent-workflow local-publish "https://example.org/article" \
  --alert-name "Quercetin"
```

After reviewing pilot output, allow a validated draft PR:

```bash
./agent-workflow local-publish "https://example.org/article" \
  --alert-name "Quercetin" --publish
```

## Database Queue

Queue one known row or a score-ordered batch:

```bash
./agent-workflow enqueue-local ARTICLE_KEY
./agent-workflow enqueue-local-backlog --min-score 12 --limit 10
```

Process one job without publishing, or enable draft PR publication:

```bash
./agent-workflow local-worker --max-jobs 1
./agent-workflow local-worker --max-jobs 1 --publish
```

Jobs use atomic leases and retain state/event history. The terminal states
`pr_open`, `duplicate`, and `rejected` mark an article processed. A failure does
not. Exhausted failures remain visible for diagnosis instead of disappearing
from the backlog.

Inspect queue state without changing it:

```bash
uv run --directory gmail-reader gmail-reader \
  --db gmail-reader/data/scholar-alerts.db \
  publication-jobs --state all --limit 50
```

`needs_review` preserves the article as unprocessed for human judgment.
`retry` carries an attempt count and `next_run_at`; `failed` means the retry
budget was exhausted. Every transition is appended to
`publication_job_events`.

Historical article rows can be linked to canonical papers in bounded batches.
The command is a dry run unless `--apply` is explicit:

```bash
uv run --directory gmail-reader gmail-reader backfill-paper-keys \
  --status selected --limit 1000
./gmail-reader/backup-db.sh
uv run --directory gmail-reader gmail-reader backfill-paper-keys \
  --status selected --limit 1000 --apply
```

## Timers

Tracked user-service templates are in `systemd/`:

- `research-scholar-sync.{service,timer}` syncs Gmail every 30 minutes.
- `research-local-publisher.{service,timer}` queues and processes one article
  at a time every 15 minutes.

Install and enable them only after the dry-run pilot has been reviewed:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/research-scholar-sync.* systemd/research-local-publisher.* \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now research-scholar-sync.timer
systemctl --user enable --now research-local-publisher.timer
```

The local publisher service includes `--publish`, but draft PRs are never
auto-merged. Edit the copied unit paths if the workspace moves.

On iconium as observed 2026-08-09, these two timers are **not installed or
enabled**. The triage UI, local Qwen llama.cpp service, FlareSolverr container,
and nightly database backup are active. Do not describe the passive publisher
as running until `systemctl --user list-timers 'research-*' --all` confirms it.
