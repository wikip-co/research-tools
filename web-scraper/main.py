#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from markitdown import MarkItDown
from scrapling.fetchers import StealthyFetcher

CONTENT_SELECTORS = [
    ".html-body",
    "article",
    ".article-content",
    ".article-body",
    '[role="main"]',
    "main",
    ".post-content",
    ".entry-content",
    "#content",
    ".main-content",
]

UNWANTED_SELECTORS = [
    "nav",
    "header",
    "footer",
    "script",
    "style",
    ".sidebar",
    ".comments",
    ".social-share",
    ".related-posts",
    ".advertisement",
    ".navigation",
    ".menu",
    ".widget",
    ".header",
    ".footer",
    ".author-bio",
    ".newsletter-signup",
    ".subscription-box",
    ".article-notes",
    "[class*='cookie']",
    "[class*='banner']",
    "[class*='popup']",
    "[id*='cookie']",
    "[id*='banner']",
    "[id*='popup']",
    "figure > figcaption",
]


def clean_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for selector in UNWANTED_SELECTORS:
        for element in soup.select(selector):
            element.decompose()

    for selector in CONTENT_SELECTORS:
        main = soup.select_one(selector)
        if main:
            return str(main)
    return str(soup)


def html_to_markdown(html: str) -> str:
    converter = MarkItDown()
    with tempfile.NamedTemporaryFile(
        suffix=".html",
        mode="w",
        encoding="utf-8",
        delete=False,
    ) as handle:
        handle.write(html)
        tmp_path = handle.name

    try:
        result = converter.convert(tmp_path)
        return result.text_content.strip()
    finally:
        os.unlink(tmp_path)


def citation_url(data: dict[str, str]) -> str:
    doi = data["doi"].strip()
    if doi:
        return doi if doi.startswith("http") else f"https://doi.org/{doi}"
    return data["url"]


def footnote_markdown(data: dict[str, str]) -> str:
    title_link = citation_url(data)
    publication_name = data["journal"] or "Source"
    publication_line = f"**Publication:** [{publication_name}]({data['url']})<br>"
    return "\n".join(
        [
            f"[^1]: **Title:** [{data['title']}]({title_link})<br>",
            publication_line,
            f"**Date:** {data['pub_date'] or 'Unknown'}<br>",
            f"**Author(s):** {data['authors'] or 'Unknown'}<br>",
            f"**Source URL:** [{data['url']}]({data['url']})",
        ]
    )


def markdown_packet(data: dict[str, str]) -> str:
    abstract_section = f"\n## Abstract\n\n{data['abstract']}\n" if data["abstract"] else ""
    keywords_section = f"\n## Keywords\n\n{data['keywords']}\n" if data["keywords"] else ""
    return (
        f"# {data['title']}\n\n"
        f"> Scraped: {data['scraped_at']}\n"
        f"> Source: {data['url']}\n\n"
        f"## Metadata\n\n"
        f"- **Authors:** {data['authors'] or 'Unknown'}\n"
        f"- **Journal:** {data['journal'] or 'N/A'}\n"
        f"- **Published:** {data['pub_date'] or 'Unknown'}\n"
        f"- **DOI:** {data['doi'] or 'N/A'}\n"
        f"{keywords_section}"
        f"{abstract_section}"
        f"\n## Extracted Content\n\n"
        f"{data['body_markdown']}\n\n"
        f"## Suggested Footnote\n\n"
        f"{data['footnote_markdown']}\n"
    )


def scrape_article(url: str) -> dict[str, str]:
    page = StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=True,
        disable_resources=False,
    )
    if not page:
        raise RuntimeError("Failed to fetch: empty response")

    raw_html = page.html_content
    title = (
        page.css('meta[name="citation_title"]::attr(content)').get()
        or page.css("h1::text").get()
        or page.css("title::text").get()
        or "Unknown Title"
    )

    author_tags = page.css('meta[name="citation_author"]::attr(content)').getall()
    if author_tags:
        authors = ", ".join(author.strip() for author in author_tags if author.strip())
    else:
        author_candidates = (
            page.css('[class*="author"] [class*="name"]::text').getall()
            or page.css('[class*="author"]::text').getall()
        )
        authors = ", ".join(author.strip() for author in author_candidates if author.strip())

    doi = (
        page.css('meta[name="citation_doi"]::attr(content)').get()
        or page.css('[class*="doi"]::text').get()
        or ""
    )
    journal = (
        page.css('meta[name="citation_journal_title"]::attr(content)').get()
        or page.css('meta[name="citation_publisher"]::attr(content)').get()
        or ""
    )
    pub_date = (
        page.css('meta[name="citation_publication_date"]::attr(content)').get()
        or page.css('meta[name="citation_date"]::attr(content)').get()
        or ""
    )
    keyword_tags = page.css('meta[name="citation_keywords"]::attr(content)').getall()
    keywords = ", ".join(keyword.strip() for keyword in keyword_tags if keyword.strip())
    abstract = (
        page.css('meta[name="citation_abstract"]::attr(content)').get()
        or page.css('meta[name="description"]::attr(content)').get()
        or ""
    )
    if abstract:
        abstract = BeautifulSoup(abstract, "html.parser").get_text(" ", strip=True)

    body_markdown = html_to_markdown(clean_html(raw_html))
    data = {
        "url": url,
        "title": title.strip(),
        "authors": authors.strip(),
        "abstract": abstract.strip(),
        "body_markdown": body_markdown,
        "doi": doi.strip(),
        "journal": journal.strip(),
        "pub_date": pub_date.strip(),
        "keywords": keywords.strip(),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
    }
    data["reference_url"] = citation_url(data)
    data["footnote_markdown"] = footnote_markdown(data)
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape an article URL and return structured markdown-ready data."
    )
    parser.add_argument("url", help="Article URL to scrape.")
    parser.add_argument(
        "legacy_output",
        nargs="?",
        help="Legacy optional markdown output path for compatibility.",
    )
    parser.add_argument(
        "--output",
        help="Optional markdown output path.",
    )
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format. JSON is the default agent contract.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output_path = args.output or args.legacy_output

    try:
        data = scrape_article(args.url)
        packet = markdown_packet(data)

        if output_path:
            destination = Path(output_path).expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(packet, encoding="utf-8")
            data["markdown_output_path"] = str(destination)
        else:
            data["markdown_output_path"] = None

        if args.format == "markdown":
            print(packet)
        else:
            print(json.dumps({"ok": True, "result": data}, indent=2))
    except Exception as exc:
        if args.format == "markdown":
            print(f"Error: {exc}", file=sys.stderr)
        else:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
