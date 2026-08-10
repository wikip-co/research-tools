# wiki-automation

Cron-friendly helper CLI for the content repo's agent workflow.

It does several things:

- search local markdown content by title, tags, permalink, path, and body
- build a structured queue from `gmail-reader`
- query the stored gmail-reader backlog for unprocessed candidate articles
- match scraped or proposed titles against existing markdown articles
- prepare a scrape packet and optionally create a new article stub with optional Cloudinary upload
- ingest a URL or PDF into a normalized packet and optional source archive
- run an intake workflow that scrapes, checks duplicates, matches content, and optionally archives without modifying content
- audit tags and lint markdown frontmatter
- open a PR from the mounted content repo with `gh`
- publish a PR end-to-end by creating a branch, committing article changes, pushing, and opening the PR
- run the production local llama.cpp publisher for ad-hoc URLs or durable SQLite jobs, with a structured draft, separate placement/evidence reviews, and deterministic quality gates

## Setup

Install the tool itself:

```bash
cd wiki-automation
uv sync
```

The helper shells out to sibling tools, so these also need to be usable:

- `gmail-reader`
- `web-scraper`
- `image-upload` when uploading images

## Usage

Find likely existing article matches for a topic:

```bash
uv run wiki-automation match "postpartum hypertension"
```

Search local markdown content before creating or editing an article:

```bash
uv run wiki-automation search "postpartum hypertension"
uv run wiki-automation search "postpartum hypertension" --match phrase
uv run wiki-automation search "hypertension" --field title --field tags
```

Build a daily queue from recent Gmail alerts:

```bash
uv run wiki-automation queue \
  --topic "health nutrition" \
  --gmail-query 'label:inbox newer_than:1d' \
  --output-dir ./out
```

Query the existing backlog for the strongest unprocessed open-access candidates:

```bash
uv run wiki-automation backlog --open-access --min-score 18 --limit 20
```

Scrape a URL, match it to the repo, and create a new article stub:

```bash
uv run wiki-automation prepare \
  "https://example.org/article" \
  --category "Child Development/Infant/Nutrition" \
  --create-new \
  --tag Nutrition \
  --tag Infant
```

Scrape a URL and upload an image before creating the stub:

```bash
uv run wiki-automation prepare \
  "https://example.org/article" \
  --category "Current Events/Technology" \
  --create-new \
  --image-file /tmp/article.jpg \
  --image-public-id article-slug \
  --tag Technology
```

Scrape a URL and use a browser screenshot as the article image:

```bash
uv run wiki-automation prepare \
  "https://example.org/article" \
  --category "Current Events/Technology" \
  --create-new \
  --image-screenshot \
  --image-screenshot-full-page \
  --image-public-id article-slug-shot \
  --tag Technology
```

Ingest a local PDF and archive the original source snapshot:

```bash
uv run wiki-automation ingest-paper /tmp/paper.pdf --archive --output-dir ./out
```

Run a non-destructive intake pass before choosing append vs new article:

```bash
uv run wiki-automation intake /tmp/paper.pdf --archive --output-dir ./out
```

Audit tag variants and frontmatter quality:

```bash
uv run wiki-automation audit-tags --limit 20
uv run wiki-automation lint-frontmatter --limit 50
```

## Output

The CLI always prints JSON:

- `search` returns local markdown matches with matched fields, matched terms, and a snippet
- `queue` writes a queue packet with gmail-reader results plus content match candidates
- `backlog` returns stored gmail-reader candidates filtered for downstream article work
- `prepare` writes a scrape packet and, when requested, a new article stub in the repo
- `match` returns scored existing article candidates
- `ingest-paper` writes a normalized packet for URL/PDF intake and can archive the raw source
- `intake` writes a packet with scrape data, duplicate checks, content matches, and an action suggestion
- `archive-source` stores a provenance snapshot and attaches it to the canonical paper record
- `audit-tags` groups likely-duplicate tags by normalized form
- `lint-frontmatter` reports invalid YAML, missing titles/tags, duplicate tags, and empty bodies
- `open-pr` shells out to `gh pr create` from the mounted content repo
- `publish-pr` creates a branch, commits changed article markdown, pushes, opens a PR, and advances matched papers to `pr_open`

By default packets are written under:

```bash
out
```

## Manual Trigger

For your current workflow, the simpler entrypoint is the repo-level manual launcher in `research-tools`:

```bash
./agent-workflow queue --topic "health nutrition"
```

Other common manual commands:

```bash
./agent-workflow backlog --open-access --min-score 18 --limit 20
./agent-workflow match "postpartum hypertension"
./agent-workflow search "postpartum hypertension" --match phrase
./agent-workflow intake "https://example.org/article"
./agent-workflow prepare "https://example.org/article" --category "Child Development/Infant/Nutrition" --create-new --tag Nutrition
./agent-workflow publish-pr --draft
```

If you want scheduling later, use this launcher rather than scheduling a raw LLM prompt.

## Local llama.cpp publisher

Dry-run an ad-hoc source, enqueue selected backlog rows, or process one leased
job:

```bash
./agent-workflow local-publish "https://example.org/article"
./agent-workflow enqueue-local-backlog --status selected --min-score 12 --limit 10
./agent-workflow local-worker --max-jobs 1
```

Add `--publish` only after reviewing dry-run reports and patches. Publication
uses an isolated worktree based on `origin/main` and opens a draft PR only when
packet, duplicate, match, evidence, critic, render, and Git-scope gates all
pass. It does not auto-merge or autonomously create a new article. See
[`../docs/local-research-publisher.md`](../docs/local-research-publisher.md).

The default `--critic-mode required` runs independent placement and evidence
reviews grounded in exact source/target quotations. `advisory` can produce a
review patch but suppresses publishing; `off` is ad-hoc dry-run only. A human
can override a required-mode critic rejection only with `--publish`,
`--allow-critic-rejection`, and an `--override-reason`; deterministic and
citation-metadata gates remain mandatory. The passive worker has no override.
Draft PRs separate validated critic findings from rejected observations. The
latter are labeled non-blocking and include the validation errors that prevented
them from influencing the publication decision.
