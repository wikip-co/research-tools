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
uv run main.py "https://example.org/article" --output "../../Current Events/Research/article-review.md"
```

Scrape a local PDF:

```bash
cd web-scraper
uv run main.py "/tmp/paper.pdf" --output ./out/paper-review.md
```

## Notes

- The tool always returns JSON when run with the default `--format json`.
- Use `--output` to write a markdown packet to disk.
- The legacy positional output argument is still supported for compatibility.
- The JSON payload includes `footnote_markdown`, which follows this repo's numbered-footnote style.
- Local PDFs and remote PDF URLs are supported through a `pypdf` extraction path.
- `--agent-browser-mode auto|off|force` controls the optional browser-rendered fallback.
- `auto` only falls back when the primary scrape is weak or fails, `off` disables fallback, and `force` always uses the browser path.
- When possible, the scraper enriches missing metadata from Crossref, PubMed, and Unpaywall to improve DOI/PMID/open-access coverage.
