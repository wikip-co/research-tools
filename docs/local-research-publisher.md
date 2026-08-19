# Local Research Publisher

The production publisher turns an ad-hoc article URL, local PDF, or queued
Google Scholar result into a validated Markdown patch and, when explicitly
requested, a draft pull request in `content`.

The default is deliberately small: deterministic intake, one local-model pass,
deterministic validation, rendering, and optional draft-PR delivery. The former
multi-pass planner/critic workflow remains available as `--pipeline legacy` for
compatibility, but it is no longer the production default.

For host services, backup/recovery, queue administration, and downstream site
deployment, see
[`../../docs/research-production-operations.md`](../../docs/research-production-operations.md).

## Default architecture

1. Scrape the original URL with the existing Scrapling, FlareSolverr, and
   browser fallback chain.
2. Validate source identity, full-text sufficiency, DOI/title consistency, and
   citation metadata before invoking the model.
3. Check the content base, canonical paper state, and open draft-PR heads for
   the DOI and source URL.
4. Rank compatible pages inside the required top-level domain and suggest a
   safe new entity path when no exact page exists.
5. Build one bounded prompt from the abstract, Results, Conclusion,
   traditional-use sections, Introduction, and Discussion plus compact page
   identity/heading context.
6. Make exactly one local-model call that extracts both the study's core
   results and useful traditional or background healing uses stated in the
   paper, then maps them to existing pages or a focused new page.
7. Deterministically validate exact quotations, near-verbatim bullets, study
   type, evidence scope, entity/page compatibility, safe paths, and new-page
   metadata.
8. Render every accepted claim with one shared bibliographic footnote for the
   main scraped article, validate Markdown and Git scope, and write an immutable
   run report and patch.
9. With `--publish`, commit in an isolated `origin/main` worktree, push a new
   branch, and open a draft PR. Nothing auto-merges.

There are no model critic calls, approval loops, syntax-repair calls, or cited-
reference graph in the default path. Malformed or deterministically invalid
model output stops as `needs_review`; it does not consume another model pass.

## Why the prompt is bounded

The earlier production prompt combined nearly the entire paper, normalized
reference entries, and large candidate-page documents. A live citrus run sent
187,213 user characters (62,624 prompt tokens) to a 27B local model. Overlapping
runs on a `-np 1` llama server then spent the same 600-second HTTP timeout partly
waiting in the server queue.

The simple prompt reserves portions of a 55,000-character source budget for
both direct results and background/traditional sections, caps candidate-page
context at 20,000 characters total, omits the paper's reference list, and caps
completion at 6,000 tokens. An OS advisory lock fails a second publisher run
quickly with `local_model_busy` instead of allowing two HTTP requests to
compete for the single model slot.

## Packet contract

The packet is the normalized evidence bundle produced before model use. It
contains requested and resolved URLs, title, DOI/PMID, authors, journal, date,
study type, abstract, extracted body, retrieval backend, enrichment metadata,
and warnings.

The packet gate rejects CAPTCHA, robot, challenge, login, publisher-error, and
insufficient-content pages even when the HTML is large. It also rejects invalid
or placeholder citation metadata, truncated abstracts, malformed DOIs, and a
DOI whose enriched title conflicts with the scraped paper.

## One-pass output contract

The model returns one JSON object with:

- `decision`: `publish_changes`, `duplicate`, or `needs_review`;
- the source-supported study type;
- one to four target proposals;
- `append_existing` or `create_new`, a domain-safe target path, the exact target
  entity, heading, and placement rationale;
- new-page title, tags, definition lead, and category rationale when needed;
- one-idea bullets containing `text`, an exact contiguous `source_quote`, the
  exact `source_section`, `claim_kind`, `evidence_scope`, and optional property
  subsection.

`source_finding` is reserved for the paper's own results.
`background_fact` covers useful background, traditional, historical, or
healing-use statements made by the paper. Both kinds cite the main article.

Earlier works mentioned by the paper are intentionally not modeled as
bibliographic dependencies. The published Markdown never links those earlier
references from this pipeline.

## Deterministic gates

The single pass may publish only when all applicable checks pass:

- source packet and DOI/title identity are valid;
- DOI/URL is absent from the base content, active paper state, and open PR
  heads;
- an existing target is one of the domain-scoped entity candidates, or a new
  path is safely below the selected domain;
- every bullet has an exact source passage and remains near-verbatim;
- every target entity appears in the bullet and its source passage;
- source findings are not mislabeled as Introduction background;
- animal findings remain animal-scoped and receive the rendered preclinical
  warning/species context;
- new pages have a definition-form lead, focused tags, a category rationale,
  and at least one direct source finding;
- no bullet introduces an external cited-reference record;
- frontmatter, headings, one shared source footnote, Markdown structure, and
  intended-file Git scope validate.

Warnings such as a missing quantitative result are retained in the report for
human review. A deterministic issue stops publication.

## Citation behavior

Each source is keyed by DOI, with a normalized source-URL fallback. All bullets
from that paper reuse one footnote. The footnote links the title to the DOI and
the publication/source field to the main scraped article. Study type and date
are normalized deterministically.

After the single model response, bounded deterministic cleanup may restore
source whitespace, replace an over-paraphrase with its exact source quote,
remove external-reference fields, or drop an unprovable bullet. It cannot
invent or rewrite a claim, and every action is recorded in the run report.

Per-bullet exact quotation and source-section provenance remains in adjacent
HTML comments for PR review. These comments contain no independently followed
or generated citation link.

## Ad-hoc usage

Start with a dry run:

```bash
./agent-workflow local-publish "https://example.org/article" \
  --domain "Natural Healing" --alert-name "Quercetin"
```

Review `source.md`, `packet.json`, `report.json`, and `proposed.patch` in the
reported immutable run directory. Then request a draft PR:

```bash
./agent-workflow local-publish "https://example.org/article" \
  --domain "Natural Healing" --alert-name "Quercetin" --publish
```

If the DOI or source URL is already present on an open PR, the command returns
`duplicate` with `reason: open_pull_request` and that PR URL without invoking
the model or creating another branch.

For an intentional side-by-side comparison, explicitly retain the duplicate
telemetry but allow a second draft PR:

```bash
./agent-workflow local-publish URL --domain "Natural Healing" \
  --publish --allow-duplicate-pr
```

This flag requires `--publish`. The new PR body records that the duplicate stop
was deliberately bypassed; it never auto-merges either PR.

Only use the compatibility pipeline for diagnosis of older reports:

```bash
./agent-workflow local-publish URL --domain "Natural Healing" \
  --pipeline legacy --critic-mode required
```

Legacy-only flags include `--critic-mode`, `--max-draft-attempts`,
`--allow-critic-rejection`, and `--override-reason`. They do not alter the
single-pass production path.

## Database queue

Queue one article or a bounded backlog:

```bash
./agent-workflow enqueue-local ARTICLE_KEY --domain "Natural Healing"
./agent-workflow enqueue-local-backlog --domain "Natural Healing" \
  --status selected --min-score 12 --limit 10
```

Process one job as a dry run or allow a passing job to open a draft PR:

```bash
./agent-workflow local-worker --max-jobs 1
./agent-workflow local-worker --max-jobs 1 --publish
```

The worker uses the simple pipeline by default. Queue leasing, retries,
`validated`, `needs_review`, `duplicate`, `failed`, and `pr_open` states are
unchanged. Requeue a reviewed stopped job explicitly:

```bash
./agent-workflow requeue-local JOB_ID \
  --reason "Reviewed prior result and authorized one new attempt"
```

## Claim-policy compatibility

Accepted claim policies: `integrated`, `strict`, `compendium`.

The simple pipeline always asks for both direct and background/traditional
claims and applies its own deterministic contract. `integrated` remains the
stored/default policy name, `strict` remains available to the legacy pipeline,
and `compendium` remains a legacy stored alias for historical queue jobs.

## Operational outcomes

- `duplicate`: already cited, already active in paper state, or found on an
  open PR.
- `needs_review`: invalid packet, model busy/error, malformed JSON, uncertain
  model decision, deterministic failure, or rendered-Markdown failure.
- `validated_draft`: dry-run patch passed all gates.
- `pr_open`: draft PR URL was verified.

The passive queue maps these outcomes to its existing durable states and event
history. `pr_open`, `duplicate`, and `rejected` are terminal processing
outcomes; `validated` and `needs_review` require an explicit requeue before a
new attempt.

## Validation after changes

```bash
bash -n agent-workflow scripts/sync-scholar-alerts
uv run python -m unittest discover -s tests -v
uv run --directory gmail-reader python -m unittest discover -s tests -v
uv run --directory web-scraper python -m unittest discover -s tests -v
uv run --directory wiki-automation python -m unittest discover -s tests -v
uv run --directory image-upload python -m unittest discover -s tests -v
./agent-workflow doctor
```
