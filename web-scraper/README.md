# Web Scraping Tool

This tool scrapes a URL and returns structured JSON for downstream repository updates. It can also write a markdown packet containing the extracted content and a repo-compatible footnote block.

## Location

`.github/agent-tools/web-scraper`

## Run

Using `uv` (preferred):

```bash
cd .github/agent-tools/web-scraper
uv run main.py "<URL>" --output "<output_file.md>"
```

Using `pip` + `python`:

```bash
cd .github/agent-tools/web-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py "<URL>" "<output_file.md>"
```

## Example

```bash
cd .github/agent-tools/web-scraper
uv run main.py "https://example.org/article" --output "../../Current Events/Research/article-review.md"
```

## Notes

- The tool always returns JSON when run with the default `--format json`.
- Use `--output` to write a markdown packet to disk.
- The legacy positional output argument is still supported for compatibility.
- The JSON payload includes `footnote_markdown`, which follows this repo's numbered-footnote style.
