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
4. restricts retrieval to the required top-level domain, then establishes page
   eligibility from exact title/stem entity phrases; title, keyword, and alert
   matches carry the most weight, while tags, category folders, and body
   overlap only rank an already eligible page;
5. asks the local model for a structured plan containing one or more separate
   target proposals, each with its own primary target entity, claims, evidence
   scopes, rationale, and explicit exclusions;
6. requires each proposed bullet to carry an exact contiguous source passage,
   source section, claim kind, and evidence scope, and checks that the bullet
   remains near-verbatim; background claims also retain every associated
   reference record;
7. runs separate target-placement and evidence-support reviews for every
   proposed target against that candidate's metadata and Markdown only after
   the complete structured plan passes deterministic validation;
8. accepts critic issues only from fixed code/severity sets and only when each
   objection contains an exact source or target-page quotation; self-
   contradictory or ungrounded findings are recorded but cannot gate a draft;
   published PRs show these separately under **Rejected critic observations
   (non-blocking)** with their validation errors, so reviewers can inspect them
   without mistaking them for authoritative findings;
9. retains every repair attempt, complete critic feedback, and the best
   deterministic-valid attempt rather than replacing useful history with a
   later invalid repair;
10. may mix guarded existing-page updates with creation of focused new entity
    pages below the requested domain when no exact page exists;
11. applies only a validated plan in an isolated git worktree based on
    `origin/main`; and
12. opens a draft PR only when `--publish` is supplied and every target's gate
    passes.

A single paper may have several target proposals, but each claim belongs to one
target and every target is validated and criticized independently. Broad study
vocabulary (for example `metabolic`) is not treated as entity identity. When no
existing page is compatible, the planner may propose a safe new Markdown path
below the required domain—for example
`Natural Healing/Fruits/Citrus/citrus.md`. A new page requires a source-grounded
lead, focused tags including its exact entity/title, category rationale, at
least one direct finding, exact quotes, both critics, rendered-Markdown
validation, and the same draft-PR gate
as an existing-page update. If those conditions are not met, it stops at
`needs_review`.

Wrong-entity and mere-mention placements remain gating failures. The exact
passage for every target must assert that target's primary title/path entity;
tag overlap is not sufficient for a target discovered only from full text. In
particular, a clementine/pink-grapefruit or generic citrus blend cannot be filed
under Bergamot.

`rat`, `rats`, `mouse`, and `mice` are explicit preclinical cues, alongside
animal/preclinical/in-vivo labels. Direct preclinical findings require `animal`
evidence scope. If a suitable heading is not explicitly animal/preclinical, the
integrated renderer inserts and validates an evidence warning. Near-verbatim validation
compares normalized word-token sequences with automatic junk suppression
disabled, making the threshold stable for long or repetitive source text.

### Integrated claim policy and prompt context

The default `integrated` policy combines direct findings from the supplied
paper with useful background facts from claim-bearing full-text sections such
as the Introduction and Discussion. There is no separate compendium workflow.
The legacy stored value `compendium` is interpreted as integrated behavior for
old jobs, while `--claim-policy strict` remains available only for focused
direct-finding diagnostics.

Every integrated bullet records:

- `claim_kind`: `source_finding` or `background_fact`;
- the exact `source_quote` and `source_section`;
- the target's primary `target_entity` and the claim-specific evidence scope;
- and, when the passage cites earlier work, `cited_references` entries retaining
  every exact citation marker, exact reference-list entry, and source-provided
  URL.

Missing or invented passage/reference provenance is a deterministic rejection.
Global exclusions must describe material genuinely omitted from every target;
reasons saying a passage was already captured, integrated, or not excluded are
also rejected as contradictory.
Published background bullets receive their own footnote containing the supplied
paper metadata, exact source passage, section, and earlier cited reference, so
the secondary provenance is not mistaken for a direct finding. Direct findings
remain labeled with the supplied paper's actual study type.

Under the integrated policy, an animal-scoped claim may use an otherwise appropriate
existing heading. When the heading does not itself say Animal/Preclinical, the
renderer inserts a mandatory, validated animal/preclinical evidence warning.
This relaxation does not change the evidence scope or imply human efficacy.

Increasing the llama.cpp context window is not the primary provenance fix. The
current service already has a large context, and sending an unstructured full
article plus every full candidate page makes associations less reliable. The
publisher instead normalizes claim-bearing sections and reference-list entries,
keeps exact citation markers with their records, caps total candidate context,
and reports the actual source/candidate character budgets used. The active
262,144-token server remains ample headroom for this bounded prompt.

Draft generation has a separate 10,000-token completion budget. A response that
reaches that ceiling is recorded as truncated and fails closed; it is not sent
through a futile syntax repair. If a response stops normally but contains
malformed JSON, the raw output, SHA-256, usage, and finish reason are retained
and one syntax-only repair call is allowed before deterministic validation.

For paths under `Natural Healing/`,
`Research/docs/natural-healing-content-style-guide.md` is authoritative. Its
near-verbatim, bullet-first style is intentionally preserved.

## Ad-hoc Usage

Dry-run first. `--domain` is required so retrieval and any new path stay within
one top-level content domain:

```bash
./agent-workflow local-publish "https://example.org/article" \
  --domain "Natural Healing" --alert-name "Quercetin"
```

Every invocation writes a new immutable
`out/runs/<timestamp>-<source-slug>-<source-hash>/` directory. It contains
`source.md`, `packet.json`, `report.json`, and `proposed.patch` when rendering
succeeds. The report records start/end/duration, repository revisions, options,
domain, candidate scoring and context budgets, duplicate checks, each model
call's duration and token usage, every draft/critic attempt, malformed raw
model output and format-repair history when applicable, artifact paths, and an
explicit `publication_outcome`. Re-running a source never overwrites a prior
run.

The CLI also emits JSON progress records to stderr for scraping, matching, each
draft/format-repair/deterministic-validation/critic attempt, and final render
validation so a long local generation is distinguishable from a stalled job.

After reviewing pilot output, allow a validated draft PR:

```bash
./agent-workflow local-publish "https://example.org/article" \
  --domain "Natural Healing" --alert-name "Quercetin" --publish
```

### Critic modes

`required` is the default and is the only mode that can publish normally:

```bash
./agent-workflow local-publish URL --domain "Natural Healing" --critic-mode required
```

`advisory` still runs both reviews and records their findings. It may produce a
validated patch after deterministic gates pass, but it suppresses commit, push,
and PR creation even when `--publish` is present:

```bash
./agent-workflow local-publish URL --domain "Natural Healing" --critic-mode advisory --publish
```

`off` skips both critic calls and is limited to manual ad-hoc dry runs. Combining
it with `--publish` is an error:

```bash
./agent-workflow local-publish URL --domain "Natural Healing" --critic-mode off
```

After a human reviews the packet, selected targets, plan, deterministic results,
and critic findings, an override request can be recorded only with both an
explicit flag and audit reason:

```bash
./agent-workflow local-publish URL --domain "Natural Healing" --critic-mode required --publish \
  --allow-critic-rejection \
  --override-reason "Human reviewed target and evidence"
```

The override request and reason are retained for audit, but do not bypass a
failed critic or any packet/citation metadata, duplicate, entity, exact-quote,
near-verbatim, preclinical-placement, or rendered-Markdown gate. It is
unavailable to `local-worker`, and no path auto-merges.

## Database Queue

Queue one known row or a score-ordered batch:

```bash
./agent-workflow enqueue-local ARTICLE_KEY --domain "Natural Healing"
./agent-workflow enqueue-local-backlog --domain "Natural Healing" \
  --min-score 12 --limit 10
```

Process one job without publishing, or enable draft PR publication:

```bash
./agent-workflow local-worker --max-jobs 1
./agent-workflow local-worker --max-jobs 1 --publish
```

The domain and claim policy are persisted when the job is enqueued; the worker
does not reinterpret them from whichever flags happen to be present later.
Canonicalized source URLs prevent query-string, scheme, or `www` variants from
creating parallel active jobs. Jobs use atomic leases and retain state/event
history. The terminal states
`pr_open`, `duplicate`, and `rejected` mark an article processed. A failure does
not. Exhausted failures remain visible for diagnosis instead of disappearing
from the backlog.

Inspect queue state without changing it:

```bash
uv run --directory gmail-reader gmail-reader \
  --db gmail-reader/data/scholar-alerts.db \
  publication-jobs --state all --limit 50
```

`validated` is a successful dry run and is distinct from `needs_review`.
Neither is automatically claimed again. `retry` carries an attempt count and a
real `next_run_at`; delays begin at five minutes and double up to six hours.
`failed` means the retry budget was exhausted. To publish a reviewed dry run or
retry a repaired stopped job, explicitly reset it with an audit reason:

```bash
./agent-workflow requeue-local JOB_ID \
  --reason "Reviewed dry-run; ready for one publication attempt"
./agent-workflow local-worker --max-jobs 1 --publish
```

Every transition, including explicit requeue and prior artifact pointers, is
appended to `publication_job_events`.

The passive worker always uses `critic-mode=required` and the job's persisted
integrated policy. It exposes no critic override flags, and the publisher also
rejects an override when invoked with the internal passive-worker context.

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
