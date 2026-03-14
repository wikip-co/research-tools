# Gmail Reader Workflow

## Purpose

This workflow turns Google Scholar alert emails into a practical research intake pipeline for the wiki.

The resulting SQLite database should be treated as a broad intake layer. It is not a fully curated or quality-verified library of sources.

It supports two modes:

- **Backfill / queue building**: ingest many Scholar alert emails into SQLite so the project has a persistent backlog of candidate sources.
- **On-demand curation**: search a narrow recent window, let the agent inspect the parsed candidates, and return a small list of promising URLs for immediate downstream work.
- **On-demand mailbox search**: run a richer Gmail query against recent Scholar emails so the agent can satisfy specific requests from the inbox without depending only on the backlog.

## High-Level Flow

1. **Gmail Reader**
   - Read Google Scholar alert emails through `gws`
   - Parse each email into alert name + article candidates
   - Store candidates in SQLite for backlog tracking
   - Optionally return a smaller topic-focused candidate set for immediate review
   - Optionally run a richer Gmail query such as `label:inbox newer_than:1d` for agent-driven inbox searches

2. **Agent Curation**
   - Use the parsed metadata, snippet, authorship, and article URL
   - Apply narrower task-specific judgment than the default heuristic
   - Treat stored triage labels as hints, not final truth
   - Examples:
     - "Find today's strawberry research about muscle mass"
     - "Give me three good URLs from today: one health/nutrition and two DevOps"
   - Pick the best URLs instead of relying only on static score thresholds

3. **Web Scraper**
   - Pass the chosen URLs into the web scraping tool
   - Convert the source page into markdown suitable for repo updates

4. **Image Upload**
   - Only needed when the pipeline creates a brand-new wiki article
   - Generate or source a single thumbnail image for the new article
   - Send the local thumbnail asset through the image upload tool
   - Return the Cloudinary URL and related metadata
   - If the uploaded public ID matches the article slug or title-derived name, that is enough
   - If not, set the markdown frontmatter image field explicitly, for example `image: goatberry`

5. **Wiki Update**
   - Update an existing markdown article or create a new one
   - Add citations from the scraped source material
   - Keep references aligned with the source article
   - If the article is new, include the uploaded thumbnail metadata

6. **Future Archive Step**
   - Store downloaded PDFs or source snapshots for long-term provenance
   - This is not implemented yet, but the workflow should preserve article URLs and PDF URLs now so the archive step can be added later

## Recommended Command Patterns

### Build the long-term backlog

```bash
uv run gmail-reader sync --days-back 365
```

### Review the queue

```bash
uv run gmail-reader alerts
```

```bash
uv run gmail-reader articles --status selected --limit 50
```

```bash
uv run gmail-reader articles --status review --limit 50
```

### Narrow topic search for agent-led curation

```bash
uv run gmail-reader curate --topic "strawberry muscle mass" --days-back 1 --max-results 10
```

```bash
uv run gmail-reader curate --topic "devops kubernetes terraform" --days-back 1 --max-results 10
```

### General recent inbox search for agent-led requests

```bash
uv run gmail-reader search --gmail-query 'label:inbox newer_than:1d' --topic 'strawberry muscle mass' --max-results 10
```

```bash
uv run gmail-reader search --gmail-query 'label:inbox newer_than:1d' --topic 'health nutrition' --max-results 10
```

```bash
uv run gmail-reader search --gmail-query 'label:inbox newer_than:1d' --topic 'devops kubernetes terraform' --max-results 10
```

```bash
uv run gmail-reader search --gmail-query 'label:inbox newer_than:1d' --include-review --save --max-results 20
```

## Cron-Oriented Agent Loop

1. Run a small recent sync to keep the backlog warm.
2. Run one or more `search` commands for the day's content themes.
3. Let the agent select one or two high-quality URLs from the returned candidates.
4. Scrape those URLs into markdown.
5. Match the scraped research against existing wiki articles.
6. Update existing articles when there is overlap.
7. Create a new article when the topic does not already exist.
8. If a new article is created, attach a simple thumbnail and set frontmatter `image`.

## Why Both Modes Exist

- The **database mode** prevents losing relevant papers and creates a durable work queue.
- The **curation mode** handles requests where the user wants a small number of high-quality URLs from a narrow time window and expects the agent to exercise judgment.
- The **mailbox search mode** gives the agent a direct way to satisfy ad hoc requests like "today's strawberry paper about muscle mass" or "three strong URLs from today's inbox."

These modes complement each other. The backlog is broad. The curated response is narrow.
