# Web Scraping Tool

This tool scrapes an article URL or PDF and returns structured JSON for downstream repository updates. It can also write a markdown packet containing the extracted content and a repo-compatible footnote block.

## Location

`research-tools/web-scraper`

## Run

Using `uv` (preferred):

```bash
cd web-scraper
uv run web-scraper "<URL-or-PDF>" --output "<output_file.md>"
```

Using `pip` + `python`:

```bash
cd web-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py "<URL-or-PDF>" "<output_file.md>"
```

## Example

```bash
cd web-scraper
uv run web-scraper "https://example.org/article" --output "./out/article-review.md"
```

Scrape a local PDF:

```bash
cd web-scraper
uv run web-scraper "/tmp/paper.pdf" --output ./out/paper-review.md
```

Cloudflare-blocked HTML (uses local FlareSolverr when primary scrape is weak):

```bash
# ensure FlareSolverr is up (always pull latest image)
cd ..
docker compose -f deploy/docker-compose.flaresolverr.yml up -d --pull always
cd web-scraper
uv run web-scraper "https://journals.sagepub.com/doi/full/10.1177/25151355251387927" \
  --flaresolverr-mode auto --output ./out/sage.md
```

## Notes

- The tool always returns JSON when run with the default `--format json`.
- Use `--output` to write a markdown packet to disk.
- The legacy positional output argument is still supported for compatibility.
- The JSON payload includes `footnote_markdown`, which follows this repo's numbered-footnote style.
- Local PDFs and remote PDF URLs are supported through a `pypdf` extraction path.
- **Content quality bar:** prefer full text; **abstract-only is acceptable** for paywalled articles.
- When possible, the scraper enriches missing metadata from Crossref, PubMed, and Unpaywall to improve DOI/PMID/open-access coverage.

## Retrieval fallbacks

Order on weak/blocked HTML:

1. **scrapling** (primary stealth fetch)
2. **FlareSolverr** (Cloudflare / DDoS-GUARD challenges)
3. **agent-browser** (headed/headless browser fallback)

| Flag | Values | Purpose |
|------|--------|---------|
| `--agent-browser-mode` | `auto` (default) / `off` / `force` | Browser-rendered fallback |
| `--flaresolverr-mode` | `auto` (default) / `off` / `force` | CF challenge bypass |

### FlareSolverr env

| Variable | Default | Meaning |
|----------|---------|---------|
| `FLARESOLVERR_URL` | `http://127.0.0.1:8191/v1` | FlareSolverr API endpoint |
| `WEB_SCRAPER_FLARESOLVERR_MODE` | `auto` | Default mode when CLI flag omitted |
| `WEB_SCRAPER_FLARESOLVERR_TIMEOUT_MS` | `120000` | Max challenge solve time |

Deploy docs: [`../deploy/README.md`](../deploy/README.md). Compose always uses `ghcr.io/flaresolverr/flaresolverr:latest` with `pull_policy: always` so routine `up -d --pull always` keeps pace with CF changes while upstream is maintained.
