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

The scraper accepts only a syntactically valid bare DOI, recovers one from the
article URL or extracted body when publisher fields contain labels such as
`DOI:`, discards placeholder authors/journals/dates, and uses Crossref or PubMed
to fill missing citation metadata. A complete abstract in the article body is
preferred over an ellipsized metadata preview. Repairs and remaining
`citation_metadata_issues` are recorded in the packet.

The packet gate rejects unresolved placeholder citation metadata, truncated
abstracts, malformed DOI/reference pairs, CAPTCHA, robot, challenge,
access-error, and publisher error pages regardless of body length. It also
rejects a DOI when its Crossref title is inconsistent with the scraped article
title. FlareSolverr receives the
original article URL as a fallback; CAPTCHA HTML from an earlier scraper is not
passed into FlareSolverr. A challenge response returned by FlareSolverr is still
invalid and can fall through to agent-browser.

## Quality Gates

The publisher:

1. scrapes with the existing Scrapling → FlareSolverr → agent-browser path;
2. validates retrieval plus enriched citation metadata before model use;
3. checks duplicates using the DOI, DOI URL, resolved URL, requested URL, and
   original intake URL rather than trusting one identifier;
4. retrieves candidate content homes using title, abstract, keywords, alert
   topic, tags, paths, and article bodies;
5. asks the local model for a structured append plan;
6. requires each proposed bullet to carry an exact source quote and checks that
   the bullet remains near-verbatim;
7. runs separate target-placement and evidence-support reviews against the
   selected candidate metadata and selected target Markdown only after the
   structured plan passes deterministic validation;
8. accepts critic issues only from fixed code/severity sets and only when each
   objection contains an exact source or target-page quotation; self-
   contradictory or ungrounded findings are recorded but cannot gate a draft;
9. applies only a validated plan in an isolated git worktree based on
   `origin/main`; and
10. opens a draft PR only when `--publish` is supplied and every gate passes.

A single plan has one target. When a study spans a broad botanical, a named
cultivar, and an isolated compound, the plan must restrict bullets to the entity
that belongs on the selected page or return `needs_review`; compound-specific
mechanisms are not silently folded into a broader page that does not already
cover that compound. Two or more bullets about a newly introduced isolated
compound produce a deterministic `unsafe_context_inference` placement-review
finding. Required mode must revise it or use the audited human override; the
publisher still does not create new articles autonomously.

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

### Critic modes

`required` is the default and is the only mode that can publish normally:

```bash
./agent-workflow local-publish URL --critic-mode required
```

`advisory` still runs both reviews and records their findings. It may produce a
validated patch after deterministic gates pass, but it suppresses commit, push,
and PR creation even when `--publish` is present:

```bash
./agent-workflow local-publish URL --critic-mode advisory --publish
```

`off` skips both critic calls and is limited to manual ad-hoc dry runs. Combining
it with `--publish` is an error:

```bash
./agent-workflow local-publish URL --critic-mode off
```

After a human reviews the packet, selected target, plan, deterministic results,
and critic findings, a required-mode rejection can be overridden only with both
an explicit flag and audit reason:

```bash
./agent-workflow local-publish URL --critic-mode required --publish \
  --allow-critic-rejection \
  --override-reason "Human reviewed target and evidence"
```

The override does not bypass packet/citation metadata, duplicate, exact-quote,
near-verbatim, preclinical-placement, or rendered-Markdown gates. The report and
draft PR retain the critic findings and reason. It is unavailable to
`local-worker`, and no path auto-merges.

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

The passive worker always uses `critic-mode=required`. It exposes no critic
override flags, and the publisher also rejects an override when invoked with
the internal passive-worker context.

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
