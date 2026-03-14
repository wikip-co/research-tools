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

# Study type detection patterns
STUDY_TYPE_PATTERNS = {
    # Meta-analyses and systematic reviews (check first - most specific)
    "Systematic Review and Meta-Analysis": [
        r"systematic\s+review.*meta[\-\s]?analysis",
        r"meta[\-\s]?analysis.*systematic\s+review",
    ],
    "Meta-Analysis": [
        r"\bmeta[\-\s]?analysis\b",
    ],
    "Systematic Review": [
        r"\bsystematic\s+review\b",
    ],
    # Human studies
    "Human Study: Randomized Controlled Trial": [
        r"\brandomized\s+controlled\s+trial\b",
        r"\brandomised\s+controlled\s+trial\b",
        r"\brct\b",
    ],
    "Human Study: Clinical Trial": [
        r"\bclinical\s+trial\b",
        r"\bcontrolled\s+trial\b",
    ],
    "Human Study: Cohort Study": [
        r"\bcohort\s+study\b",
        r"\bprospective\s+study\b",
        r"\bretrospective\s+study\b",
    ],
    "Human Study: Cross-Sectional": [
        r"\bcross[\-\s]?sectional\b",
    ],
    "Human Study: Case-Control": [
        r"\bcase[\-\s]?control\b",
    ],
    "Human Study: Observational": [
        r"\bobservational\s+study\b",
    ],
    # Animal studies
    "Animal Study: In Vivo": [
        r"\bin\s+vivo\b",
        r"\banimal\s+study\b",
        r"\banimal\s+model\b",
        r"\bmouse\s+model\b",
        r"\brat\s+model\b",
        r"\bmice\b.*\bstudy\b",
        r"\brats\b.*\bstudy\b",
    ],
    # Cell studies
    "Cell Study: In Vitro": [
        r"\bin\s+vitro\b",
        r"\bcell\s+line\b",
        r"\bcell\s+culture\b",
        r"\bcultured\s+cells\b",
    ],
    # Reviews (check after more specific types)
    "Narrative Review": [
        r"\bnarrative\s+review\b",
    ],
    "Review": [
        r"\breview\b",
        r"\boverview\b",
    ],
    # Computational
    "Computational Study": [
        r"\bcomputational\b",
        r"\bin\s+silico\b",
        r"\bmolecular\s+docking\b",
        r"\bnetwork\s+pharmacology\b",
    ],
    # Case reports
    "Case Report": [
        r"\bcase\s+report\b",
        r"\bcase\s+series\b",
    ],
}

import re as _re

def detect_study_type(
    title: str,
    abstract: str,
    publication_types: list[str],
    mesh_terms: list[str],
    body_text: str = "",
) -> str:
    """Detect study type from article metadata and content."""
    # Combine all text sources for pattern matching
    combined_text = " ".join([
        title.lower(),
        abstract.lower(),
        " ".join(pt.lower() for pt in publication_types),
        " ".join(mt.lower() for mt in mesh_terms),
    ])

    # Check publication types first (most reliable for PubMed)
    pub_types_lower = [pt.lower() for pt in publication_types]
    if "meta-analysis" in pub_types_lower and "systematic review" in pub_types_lower:
        return "Systematic Review and Meta-Analysis"
    if "meta-analysis" in pub_types_lower:
        return "Meta-Analysis"
    if "systematic review" in pub_types_lower:
        return "Systematic Review"
    if "review" in pub_types_lower:
        return "Review"
    if "randomized controlled trial" in pub_types_lower:
        return "Human Study: Randomized Controlled Trial"
    if "clinical trial" in pub_types_lower:
        return "Human Study: Clinical Trial"
    if "case reports" in pub_types_lower:
        return "Case Report"
    if "observational study" in pub_types_lower:
        return "Human Study: Observational"

    # Pattern matching on combined text
    for study_type, patterns in STUDY_TYPE_PATTERNS.items():
        for pattern in patterns:
            if _re.search(pattern, combined_text, _re.IGNORECASE):
                return study_type

    # Default
    return "Research Article"


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
    study_type_line = f"**Study Type:** {data.get('study_type', 'Research Article')}<br>"
    return "\n".join(
        [
            f"[^1]: **Title:** [{data['title']}]({title_link})<br>",
            publication_line,
            f"**Date:** {data['pub_date'] or 'Unknown'}<br>",
            study_type_line,
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
        f"- **Study Type:** {data.get('study_type', 'Research Article')}\n"
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

    # Extract publication types (PubMed-specific)
    publication_types = []
    # PubMed uses specific selectors for publication types
    pub_type_elements = page.css('[data-ga-label="publication_types"] a::text').getall()
    if pub_type_elements:
        publication_types = [pt.strip() for pt in pub_type_elements if pt.strip()]
    # Also check for schema.org metadata
    if not publication_types:
        schema_type = page.css('meta[property="og:type"]::attr(content)').get()
        if schema_type:
            publication_types = [schema_type.strip()]

    # Extract MeSH terms (PubMed-specific)
    mesh_terms = []
    mesh_elements = page.css('[data-ga-label="mesh_terms"] a::text').getall()
    if mesh_elements:
        mesh_terms = [mt.strip() for mt in mesh_elements if mt.strip()]

    body_markdown = html_to_markdown(clean_html(raw_html))

    # Detect study type
    study_type = detect_study_type(
        title=title,
        abstract=abstract,
        publication_types=publication_types,
        mesh_terms=mesh_terms,
        body_text=body_markdown[:5000],  # First 5000 chars of body
    )

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
        "study_type": study_type,
        "publication_types": publication_types,
        "mesh_terms": mesh_terms,
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
