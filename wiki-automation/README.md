# wiki-automation

Cron-friendly helper CLI for the content repo's agent workflow.

It does five things:

- search local markdown content by title, tags, permalink, path, and body
- build a structured queue from `gmail-reader`
- query the stored gmail-reader backlog for unprocessed candidate articles
- match scraped or proposed titles against existing markdown articles
- prepare a scrape packet and optionally create a new article stub with optional Cloudinary upload

## Setup

Install the tool itself:

```bash
cd .github/agent-tools/wiki-automation
uv sync
```

The helper shells out to sibling tools, so these also need to be usable:

- `.github/agent-tools/gmail-reader`
- `.github/agent-tools/web-scraper`
- `.github/agent-tools/image-upload` when uploading images

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
  --output-dir ./.github/agent-tools/wiki-automation/out
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

## Output

The CLI always prints JSON:

- `search` returns local markdown matches with matched fields, matched terms, and a snippet
- `queue` writes a queue packet with gmail-reader results plus content match candidates
- `backlog` returns stored gmail-reader candidates filtered for downstream article work
- `prepare` writes a scrape packet and, when requested, a new article stub in the repo
- `match` returns scored existing article candidates

By default packets are written under:

```bash
.github/agent-tools/wiki-automation/out
```

## Manual Trigger

For your current workflow, the simpler entrypoint is the repo-level manual launcher:

```bash
./agent-workflow queue --topic "health nutrition"
```

Other common manual commands:

```bash
./agent-workflow backlog --open-access --min-score 18 --limit 20
./agent-workflow match "postpartum hypertension"
./agent-workflow search "postpartum hypertension" --match phrase
./agent-workflow prepare "https://example.org/article" --category "Child Development/Infant/Nutrition" --create-new --tag Nutrition
./agent-workflow validate
```

If you want scheduling later, use this launcher rather than scheduling a raw LLM prompt.
