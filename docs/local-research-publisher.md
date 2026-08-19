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
   only `blocking` findings and invalid critic responses stop a publication:
   validated `review`-severity findings still drive draft repair attempts, but
   once attempts are exhausted the best deterministic-valid draft publishes with
   those findings listed in the draft PR's critic audit
   (`critic_publication_note: published_with_review_findings`), because the
   draft-PR reviewer — PRs are never auto-merged — is the intended judge of
   review-severity placement questions;
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
target and every target is validated and criticized independently. A plan may
propose multiple sections of the same page — for example `## Composition` and
`## Healing Properties` on one new entity page — as separate proposals sharing
the target path with different headings. Broad study vocabulary (for example
`metabolic`) is not treated as entity identity.

**New-article creation, precisely.** Earlier revisions of this document stated
that the publisher does not create new articles; that statement was wrong — the
document had drifted behind the code, whose behavior is intended (PR #27
legitimately created `Natural Healing/Fruits/Citrus/citrus.md`). The actual
contract: under every claim policy, the planner may propose `create_new` with a
safe new Markdown path below the required domain when no existing page is
compatible, including the seeded category catch-all. No extra flag is required
— creation rides the ordinary `--publish` gate. A new page requires a
definition-form lead (`lead_kind: definition` for uncited general-knowledge
definitions, or `source_grounded` when the source itself contains one — a
topic-relevance framing sentence is rejected as `lead_not_definition_form`),
focused tags including its exact entity/title, category rationale, at least one
direct finding, exact quotes, both critics, rendered-Markdown validation, and
the same draft-PR gate as an existing-page update. The run returns
`needs_review` with a human-only `new_article_recommendation` instead of
creating anything when the plan is not deterministically valid, when the critic
raises a blocking finding or rejects every proposed target's entity placement,
or when a validated balance finding cannot be repaired.

The planner is steered toward concrete evidence: when the packet's Results
sections contain quantitative sentences, they are surfaced verbatim in the
prompt (`QUANTITATIVE_RESULTS_CANDIDATES`) and a plan whose supplied-paper
bullets are all qualitative records the warning `missing_quantitative_outcome`;
when the source quantifies constituent compounds, a missing `## Composition`
proposal records `missing_composition_section`. Composition bullets use the
`composition` evidence scope, which is exempt from the animal-scope rule
because measurements are not outcome claims.

Wrong-entity and mere-mention placements remain gating failures. The exact
passage for every target must assert that target's primary title/path entity;
tag overlap is not sufficient for a target discovered only from full text. In
particular, a clementine/pink-grapefruit or generic citrus blend cannot be filed
under Bergamot.

The seeded category catch-all page is the deliberate exception: it exists to
host findings about blends, concentrates, extracts, juices, and other products
derived from its category, provided each bullet keeps its formulation or
processing scope explicit in near-verbatim text. The placement critic receives
the seed and is told the derived-product-versus-whole-category distinction is
at most a `warning` there, and a deterministic backstop demotes an
`entity_not_supported` objection on the seeded catch-all to `warning` whenever
the objection's own exact quote contains the target entity (recorded as
`severity_demoted_from_*_seeded_catch_all_scope`). Objections whose quote does
not involve the target category at all keep their severity. This resolves the
earlier planner/critic contradiction where the planner was required to create
`Natural Healing/Fruits/Citrus/citrus.md` for a citrus-concentrate study and
the critic then blocked every concentrate-scoped bullet on that same page.

A retry that abandons an earlier deterministically valid plan by returning
`needs_review` is treated as capitulation while attempts remain: the planner is
re-prompted (`planner_abandoned_valid_plan`) to keep unobjected targets and
rescope, retarget, or exclude only the claims a critic objected to.

`rat`, `rats`, `mouse`, and `mice` are explicit preclinical cues, alongside
animal/preclinical/in-vivo labels. Direct preclinical findings require `animal`
evidence scope. If a suitable heading is not explicitly animal/preclinical, the
integrated renderer inserts and validates an evidence warning. Near-verbatim validation
compares normalized word-token sequences with automatic junk suppression
disabled, making the threshold stable for long or repetitive source text.

### Claim policies

Accepted claim policies: `integrated`, `strict`, `compendium`.

That line is the canonical enum. The CLI exposes `--claim-policy integrated`
(default) and `--claim-policy strict`; `compendium` is a legacy stored alias
that old queue jobs may still carry, and a unit test
(`test_documented_claim_policies_match_code_enum`) fails CI whenever this
documented set diverges from the code's `CLAIM_POLICIES` enum.

How the three differ:

- **`integrated`** (default, production): extracts direct `source_finding`
  claims from the supplied paper *and* passage-grounded `background_fact`
  claims from claim-bearing full-text sections (Introduction, Discussion),
  each with full cited-reference provenance. All deterministic gates apply —
  packet/citation metadata, duplicate, entity assertion, exact-quote,
  near-verbatim, preclinical scope (`source_finding` from a preclinical paper
  must be `animal` or `composition` scope), section provenance, rendered
  Markdown — plus both critics.
- **`strict`**: direct findings only; background facts are rejected
  (`background_claim_not_allowed`), Introduction-sourced findings are
  misclassifications, and every preclinical claim must be `animal`-scoped
  (composition measurements excepted). Same gates otherwise; retained for
  focused diagnostics.
- **`compendium`** (legacy alias): interpreted exactly as `integrated`; it is
  accepted so historical queue jobs and stored reports remain replayable,
  and it selects the same extraction and gate behavior. New jobs should not
  use it.

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
### Evidence-tier structure under Healing Properties

For Healing Properties targets (and any plan with animal-scope or
subsection-tagged bullets) the renderer groups claims by evidence tier
instead of emitting one flat list:

- Animal-scope findings render under `### Preclinical Evidence (Animal
  Studies)` — this rendered subsection also satisfies the strict-policy
  preclinical heading requirement, so the plan-level heading gap is now the
  warning `preclinical_heading_scope_warning`, never a gate failure. The
  animal/preclinical evidence blockquote is scoped inside that subsection
  only.
- Background facts render under property-named `###` subsections taken from
  each bullet's new `subsection` field (fallback `Supporting Background`),
  without the animal warning unless their own evidence scope is animal.
- When animal findings concern a specific formulation (bullet text mentions a
  concentrate/formulation/extract/blend), the plan must supply
  `formulation_definition` — one near-verbatim line with an exact
  `source_quote` — and the subsection opens with it, before the warning and
  bullets, so product-specific findings cannot be read as generic entity
  claims (`formulation_definition_missing` otherwise).
- Animal bullets whose near-verbatim text lacks a species/model cue get the
  packet's single standardized scope prefix (for example `In fructose-fed
  rats, `, derived by `packet_animal_model`). The near-verbatim gate excludes
  exactly that whitelisted comma-terminated prefix from token comparison; the
  0.68 threshold is unchanged and applies fully to any other lead-in.
- Methods/process statements (how a formulation was produced or assessed) are
  rejected as effect bullets (`methods_statement_not_effect_claim`); their
  essential content belongs in `formulation_definition`.
- Multi-sentence bullet texts are split into one-idea bullets sharing the same
  footnote, each with its own provenance comment.

The rendered-Markdown gate enforces the structure independently:
`bullet_outside_property_subsection`, `animal_subsection_missing_warning`,
`animal_warning_misapplied_to_background`, `animal_bullet_missing_species_scope`,
`bullet_not_single_idea`, and `formulation_definition_missing_or_misplaced`.

### Citation rendering: one footnote per source, provenance in comments

The renderer emits exactly one bibliographic footnote per unique source, keyed
by DOI with a normalized-URL fallback. Every bullet citing that source —
direct finding or background fact — reuses the same `[^n]` marker, and a
repeated application against a page that already carries the source's footnote
reuses the existing number instead of emitting a second block. The
rendered-Markdown gate rejects `duplicate_source_footnote` if the same DOI ever
appears in two footnote blocks.

Per-claim provenance no longer lives in the footnote. Each bullet is followed
by an adjacent, two-space-indented HTML comment carrying `claim_kind`, the
`source_section`, the exact `source_quote`, and any `cited_references` —
invisible on the rendered site but reviewable in the PR diff. Supplied-paper
findings carry the same provenance fields as background facts. Within
`cited_references`, source-internal fragment anchors (for example
`[Aruoma et al., 2012](#bb0010)`) are collapsed to plain text; when the
reference record carries a resolvable URL or an embedded DOI, the entry links
`https://doi.org/…` instead. The gate independently rejects any rendered link
whose target starts with `#` and matches no in-page anchor
(`dead_anchor_link_*`).

Footnote hygiene rules:

- The **Title** strips publisher suffixes such as ` - ScienceDirect` (Crossref
  enrichment supplies the canonical form when it agrees) and links to the DOI.
- **Study Type** is classified deterministically from content signals (title,
  keywords, abstract, then publication types/MeSH) into the style guide
  vocabulary — Meta Analysis, Review, Animal Study, Human Study, In Vitro. The
  publisher's genre label (for example "Research Article") is only a fallback
  when no signal matches and never overrides a successful classification.
- **Date** is normalized to `YYYY-MM-DD`, degrading to `YYYY-MM` or `YYYY` for
  partial source dates.
- **Institution(s)** and **Copy** (archive links) render only when the packet
  provides them.
- The abstract is emitted at most once per source, controlled by
  `--abstract-mode full|truncated|omit` (default `full`); the complete abstract
  always remains in the packet and report artifacts.

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
unavailable to `local-worker`, and no path auto-merges. Every validated
review-severity finding that publishes — via the override or the ordinary
publish-with-findings path — is also persisted as a `<!-- critic | … -->`
comment adjacent to the affected bullet in the generated Markdown, so the
caveat survives once the PR merges instead of living only in the PR body.

### Balance-finding repair pass

A validated finding of the balance/omitted-qualifier class (currently
`limitation_omitted`) never publishes unresolved in `required` mode. When the
selected attempt would otherwise proceed and such a finding remains, the
pipeline runs exactly one bounded repair:

1. The finding — whose `source_quote` must be a verbatim source passage — is
   fed back to the planner with a constrained prompt allowing only (a) one new
   near-verbatim bullet quoting the qualifier passage, or (b) extending the
   flagged bullet with the qualifier clause under a covering exact quote.
2. The repaired plan re-runs full deterministic validation plus a repair-scope
   diff check (`validate_balance_repair`) that rejects dropped, reworded, or
   unrelated added bullets, then both critics. Because qualifier sentences
   rarely restate the target entity, the entity-assertion and bullet-count
   caps are waived only for the qualifier bullets themselves
   (`*_waived_balance_qualifier` warnings); exact-passage and near-verbatim
   gates are never waived.
3. If the repair validates and the critics no longer report an unresolved
   balance finding, the repaired attempt publishes and is appended to
   `attempt_history` with `balance_repair: true`. On any failure — model error,
   scope violation, failed gates, or a qualifier that is not verbatim in the
   source — the pre-repair best attempt is retained and the run downgrades to
   `needs_review` with reason `balance_finding_unresolved`.

The repair pass is available to the passive `local-worker` (it adds one model
round trip, no new permissions); the critic-rejection override remains
forbidden there. `advisory` and `off` modes are unchanged. The outcome is
recorded under `balance_repair` in the report.

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
