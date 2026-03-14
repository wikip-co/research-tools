from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", ".github", ".venv", "node_modules", "__pycache__"}
DEFAULT_TAGS = ["Research"]
SEARCH_FIELD_WEIGHTS = {
    "title": 30.0,
    "tags": 20.0,
    "permalink": 20.0,
    "path": 15.0,
    "body": 15.0,
}

TOOL_DIR = Path(__file__).resolve().parents[1]
AGENT_TOOLS_ROOT = Path(
    os.environ.get("AGENT_TOOLS_ROOT", str(TOOL_DIR.parent))
).expanduser().resolve()
REPO_ROOT = Path(
    os.environ.get("CONTENT_REPO_ROOT", str(Path.cwd()))
).expanduser().resolve()
GMAIL_READER_DIR = AGENT_TOOLS_ROOT / "gmail-reader"
WEB_SCRAPER_DIR = AGENT_TOOLS_ROOT / "web-scraper"
IMAGE_UPLOAD_DIR = AGENT_TOOLS_ROOT / "image-upload"
DEFAULT_OUTPUT_DIR = TOOL_DIR / "out"


@dataclass
class ArticleRecord:
    path: str
    title: str
    stem: str
    tags: list[str]
    permalink: str | None
    body: str

    @property
    def route_key(self) -> str:
        if self.permalink:
            return self.permalink.strip("/")
        return self.stem


def normalize(text: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def slugify(text: str) -> str:
    return re.sub(r"-{2,}", "-", normalize(text).replace(" ", "-")).strip("-")


def parse_markdown_article(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text

    lines = text.splitlines()
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text

    metadata: dict[str, Any] = {}
    current_key: str | None = None
    list_values: dict[str, list[str]] = {}

    for raw_line in lines[1:end_index]:
        line = raw_line.rstrip()
        if not line:
            continue
        if line.lstrip().startswith("-") and current_key:
            value = line.lstrip()[1:].strip()
            list_values.setdefault(current_key, []).append(value)
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        metadata[current_key] = value.strip().strip("'\"")

    for key, values in list_values.items():
        metadata[key] = values

    body = "\n".join(lines[end_index + 1 :])
    return metadata, body


def parse_frontmatter(path: Path) -> dict[str, Any]:
    metadata, _ = parse_markdown_article(path)
    return metadata


def coerce_frontmatter_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    return []


def load_articles(repo_root: Path) -> list[ArticleRecord]:
    articles: list[ArticleRecord] = []
    for path in sorted(repo_root.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        metadata, body = parse_markdown_article(path)
        articles.append(
            ArticleRecord(
                path=str(path.relative_to(repo_root)),
                title=str(metadata.get("title", path.stem)),
                stem=path.stem,
                tags=coerce_frontmatter_list(metadata.get("tags", [])),
                permalink=metadata.get("permalink") or None,
                body=body,
            )
        )
    return articles


def collect_all_tags(articles: list[ArticleRecord]) -> dict[str, int]:
    """Collect all tags from articles with their frequency counts."""
    tag_counts: dict[str, int] = {}
    for article in articles:
        for tag in article.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return tag_counts


def suggest_tags(
    tag_counts: dict[str, int],
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Suggest tags matching a query, sorted by relevance and frequency."""
    query_norm = normalize(query)
    query_terms = query_norm.split()
    suggestions = []

    for tag, count in tag_counts.items():
        tag_norm = normalize(tag)
        tag_terms = set(tag_norm.split())
        # Score based on match quality
        score = 0.0
        if query_norm == tag_norm:
            score = 100.0
        elif query_norm in tag_norm:
            score = 80.0
        elif tag_norm in query_norm:
            score = 70.0
        else:
            # Check individual term matches (both directions)
            matched_in_tag = sum(1 for term in query_terms if term in tag_norm)
            matched_in_query = sum(1 for term in tag_terms if term in query_norm)
            if matched_in_tag > 0:
                score = 50.0 * (matched_in_tag / len(query_terms))
            elif matched_in_query > 0:
                score = 40.0 * (matched_in_query / len(tag_terms))

        if score > 0:
            suggestions.append({
                "tag": tag,
                "count": count,
                "score": round(score, 2),
            })

    suggestions.sort(key=lambda x: (-x["score"], -x["count"], x["tag"].lower()))
    return suggestions[:limit]


def find_reference_urls(body: str) -> list[dict[str, Any]]:
    """Extract all reference URLs from article body."""
    references = []
    # Match footnote definitions like [^1]: **Title:** [text](url)
    footnote_pattern = re.compile(
        r'\[\^(\d+)\]:\s*(?:\*\*Title:\*\*\s*)?\[([^\]]*)\]\(([^)]+)\)',
        re.MULTILINE
    )
    for match in footnote_pattern.finditer(body):
        ref_num, title, url = match.groups()
        references.append({
            "ref_num": int(ref_num),
            "title": title.strip(),
            "url": url.strip(),
        })

    # Also match simpler formats like [^1]: [text](url)
    simple_pattern = re.compile(
        r'\[\^(\d+)\]:\s*\[([^\]]*)\]\(([^)]+)\)',
        re.MULTILINE
    )
    seen_refs = {r["ref_num"] for r in references}
    for match in simple_pattern.finditer(body):
        ref_num, title, url = match.groups()
        if int(ref_num) not in seen_refs:
            references.append({
                "ref_num": int(ref_num),
                "title": title.strip(),
                "url": url.strip(),
            })

    return sorted(references, key=lambda x: x["ref_num"])


def check_reference_exists(
    articles: list[ArticleRecord],
    url: str,
) -> dict[str, Any] | None:
    """Check if a URL is already referenced in any article."""
    url_normalized = url.strip().rstrip("/").lower()
    # Also check without protocol
    url_no_protocol = re.sub(r'^https?://', '', url_normalized)

    for article in articles:
        refs = find_reference_urls(article.body)
        for ref in refs:
            ref_url = ref["url"].strip().rstrip("/").lower()
            ref_no_protocol = re.sub(r'^https?://', '', ref_url)
            if url_normalized == ref_url or url_no_protocol == ref_no_protocol:
                return {
                    "exists": True,
                    "path": article.path,
                    "title": article.title,
                    "ref_num": ref["ref_num"],
                    "ref_title": ref["title"],
                }
    return {"exists": False}


def get_next_reference_number(body: str) -> int:
    """Get the next available reference number for an article."""
    refs = find_reference_urls(body)
    if not refs:
        return 1
    return max(r["ref_num"] for r in refs) + 1


def score_match(title: str, article: ArticleRecord) -> float:
    title_norm = normalize(title)
    article_title_norm = normalize(article.title)
    stem_norm = normalize(article.stem)
    path_norm = normalize(article.path)

    exact_bonus = 0
    if title_norm == article_title_norm:
        exact_bonus = 35
    elif title_norm == stem_norm:
        exact_bonus = 30

    title_ratio = SequenceMatcher(None, title_norm, article_title_norm).ratio()
    stem_ratio = SequenceMatcher(None, title_norm, stem_norm).ratio()
    path_ratio = SequenceMatcher(None, title_norm, path_norm).ratio()

    title_tokens = set(title_norm.split())
    article_tokens = set(article_title_norm.split()) | set(stem_norm.split())
    overlap = 0.0
    if title_tokens and article_tokens:
        overlap = len(title_tokens & article_tokens) / len(title_tokens)

    score = max(title_ratio, stem_ratio, path_ratio) * 70 + overlap * 30 + exact_bonus
    return min(score, 100.0)


def top_matches(
    articles: list[ArticleRecord],
    title: str,
    limit: int = 5,
    min_score: float = 30.0,
) -> list[dict[str, Any]]:
    matches = []
    for article in articles:
        score = score_match(title, article)
        if score < min_score:
            continue
        matches.append(
            {
                "path": article.path,
                "title": article.title,
                "tags": article.tags,
                "permalink": article.permalink,
                "route_key": article.route_key,
                "score": round(score, 2),
                "match_method": "score",
            }
        )
    matches.sort(key=lambda item: (-item["score"], item["path"].lower()))
    return matches[:limit]


def alert_name_matches_article(alert_name: str, article: ArticleRecord) -> bool:
    """Return True if any significant keyword from the alert name appears in the article path or title."""
    keywords = [kw for kw in re.split(r'[\s"\']+', alert_name.lower()) if len(kw) > 3]
    path_lower = article.path.lower()
    title_lower = article.title.lower()
    return any(kw in path_lower or kw in title_lower for kw in keywords)


def top_matches_extended(
    articles: list[ArticleRecord],
    title: str,
    alert_name: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Lower-threshold matching plus alert-name keyword matching for --match-existing."""
    seen_paths: set[str] = set()
    matches = []

    for article in articles:
        score = score_match(title, article)
        if score < 15:
            continue
        seen_paths.add(article.path)
        matches.append(
            {
                "path": article.path,
                "title": article.title,
                "tags": article.tags,
                "permalink": article.permalink,
                "route_key": article.route_key,
                "score": round(score, 2),
                "match_method": "score",
            }
        )

    if alert_name:
        for article in articles:
            if article.path in seen_paths:
                continue
            if alert_name_matches_article(alert_name, article):
                matches.append(
                    {
                        "path": article.path,
                        "title": article.title,
                        "tags": article.tags,
                        "permalink": article.permalink,
                        "route_key": article.route_key,
                        "score": 0.0,
                        "match_method": "alert_keyword",
                    }
                )

    matches.sort(key=lambda item: (-item["score"], item["path"].lower()))
    return matches[:limit]


def normalize_search_query(query: str) -> tuple[str, list[str]]:
    query_norm = normalize(query)
    if not query_norm:
        raise ValueError("query must contain at least one letter or number")
    return query_norm, query_norm.split()


def build_search_fields(article: ArticleRecord) -> dict[str, str]:
    return {
        "title": normalize(article.title),
        "tags": normalize(" ".join(article.tags)),
        "permalink": normalize(article.permalink or ""),
        "path": normalize(article.path),
        "body": normalize(article.body),
    }


def matched_search_units(
    field_text: str,
    query_norm: str,
    query_terms: list[str],
    match_mode: str,
) -> list[str]:
    if not field_text:
        return []
    if match_mode == "phrase":
        return [query_norm] if query_norm in field_text else []
    return [term for term in query_terms if term in field_text]


def search_article(
    article: ArticleRecord,
    query_norm: str,
    query_terms: list[str],
    match_mode: str,
    fields: list[str],
) -> dict[str, Any] | None:
    normalized_fields = build_search_fields(article)
    field_matches: dict[str, list[str]] = {}

    for field in fields:
        matches = matched_search_units(
            normalized_fields[field],
            query_norm,
            query_terms,
            match_mode,
        )
        if matches:
            field_matches[field] = matches

    if match_mode == "phrase":
        if not field_matches:
            return None
        matched_terms = [query_norm]
        unit_count = 1
    else:
        matched_term_set = {term for matches in field_matches.values() for term in matches}
        if match_mode == "any":
            if not matched_term_set:
                return None
        elif matched_term_set != set(query_terms):
            return None
        matched_terms = sorted(matched_term_set)
        unit_count = len(query_terms)

    score = 0.0
    for field, matches in field_matches.items():
        coverage = len(set(matches)) / unit_count
        score += SEARCH_FIELD_WEIGHTS[field] * coverage

    title_norm = normalized_fields["title"]
    path_norm = normalized_fields["path"]
    permalink_norm = normalized_fields["permalink"]
    stem_norm = normalize(article.stem)
    if query_norm == title_norm:
        score += 30
    elif query_norm == permalink_norm:
        score += 25
    elif query_norm == path_norm:
        score += 20
    elif query_norm == stem_norm:
        score += 15
    elif match_mode == "phrase" and query_norm in title_norm:
        score += 10

    return {
        "path": article.path,
        "title": article.title,
        "tags": article.tags,
        "permalink": article.permalink,
        "route_key": article.route_key,
        "score": round(min(score, 100.0), 2),
        "matched_fields": list(field_matches.keys()),
        "matched_terms": matched_terms,
        "snippet": build_body_snippet(article.body, query_norm, query_terms, match_mode),
        "match_method": "search",
    }


def build_body_snippet(
    body: str,
    query_norm: str,
    query_terms: list[str],
    match_mode: str,
    window: int = 180,
) -> str | None:
    snippet_source = re.sub(r"\s+", " ", body).strip()
    if not snippet_source:
        return None

    source_lower = snippet_source.lower()
    needles = [query_norm] if match_mode == "phrase" else query_terms
    locations = [
        (source_lower.find(needle), needle)
        for needle in needles
        if source_lower.find(needle) >= 0
    ]
    if not locations:
        return None

    first_index, needle = min(locations, key=lambda item: item[0])
    start = max(0, first_index - 60)
    end = min(len(snippet_source), first_index + len(needle) + window)
    snippet = snippet_source[start:end].strip()
    if start > 0:
        snippet = f"...{snippet}"
    if end < len(snippet_source):
        snippet = f"{snippet}..."
    return snippet


def search_articles(
    articles: list[ArticleRecord],
    query: str,
    *,
    match_mode: str = "all",
    fields: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    query_norm, query_terms = normalize_search_query(query)
    active_fields = fields or list(SEARCH_FIELD_WEIGHTS.keys())

    matches = []
    for article in articles:
        result = search_article(
            article,
            query_norm=query_norm,
            query_terms=query_terms,
            match_mode=match_mode,
            fields=active_fields,
        )
        if result is not None:
            matches.append(result)

    matches.sort(key=lambda item: (-item["score"], item["path"].lower()))
    return matches[:limit]


def ensure_output_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_tool_dir(path: Path, label: str) -> Path:
    if not path.is_dir():
        raise FileNotFoundError(f"{label} is missing at {path}")
    return path


def gmail_reader_command(*args: str) -> list[str]:
    command = ["gmail-reader"]
    db_path = os.environ.get("GMAIL_READER_DB", "").strip()
    if db_path:
        command.extend(["--db", db_path])
    command.extend(args)
    return command


def run_json_tool(tool_dir: Path, args: list[str]) -> dict[str, Any]:
    command = ["uv", "run", *args]
    result = subprocess.run(
        command,
        cwd=tool_dir,
        text=True,
        capture_output=True,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    payload_text = stdout or stderr
    if not payload_text:
        raise RuntimeError(f"Command produced no output: {' '.join(command)}")

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Command did not return JSON: {' '.join(command)}\nstdout={stdout}\nstderr={stderr}"
        ) from exc

    if result.returncode != 0 or not payload.get("ok", False):
        raise RuntimeError(payload.get("error", f"Command failed: {' '.join(command)}"))
    return payload["result"]


def article_stub(
    *,
    title: str,
    tags: list[str],
    image: str | None,
    permalink: str | None,
    abstract: str,
    footnote: str,
) -> str:
    frontmatter_lines = [
        "---",
        f"title: {title}",
    ]
    if permalink:
        frontmatter_lines.append(f"permalink: {permalink}")
    if image:
        frontmatter_lines.append(f"image: {image}")
    frontmatter_lines.append("tags:")
    for tag in tags:
        frontmatter_lines.append(f"- {tag}")
    frontmatter_lines.append("---")

    abstract_block = f"\n## Abstract\n\n{abstract}\n" if abstract else ""
    return "\n".join(frontmatter_lines) + (
        "\n\nBrief introduction.\n"
        "\n## Key Findings\n\n"
        "- Add the first validated finding here.[^1]\n"
        f"{abstract_block}"
        "\n## Notes\n\n"
        "- Review the scraped packet before publishing.\n\n"
        f"{footnote}\n"
    )


def unique_route_key(candidate_path: Path, articles: list[ArticleRecord]) -> str | None:
    route_key = candidate_path.stem
    existing = {article.route_key for article in articles}
    if route_key not in existing:
        return None
    parts = [slugify(part) for part in candidate_path.with_suffix("").parts if slugify(part)]
    return "/".join(parts)


def format_reference_block(
    ref_num: int,
    scrape: dict[str, Any],
) -> str:
    """Format a reference block in the content repo style."""
    title = scrape.get("title", "Unknown Title")
    url = scrape.get("reference_url") or scrape.get("url", "")
    journal = scrape.get("journal", "")
    pub_date = scrape.get("pub_date", "Unknown")
    study_type = scrape.get("study_type", "Research Article")
    authors = scrape.get("authors", "Unknown")

    lines = [
        f'[^{ref_num}]: **Title:** [{title}]({url})<br>',
    ]
    if journal:
        lines.append(f'**Publication:** [{journal}]({scrape.get("url", "")})<br>')
    lines.append(f'**Date:** {pub_date}<br>')
    lines.append(f'**Study Type:** {study_type}<br>')
    lines.append(f'**Author(s):** {authors}<br>')
    lines.append(f'**Source:** [{scrape.get("url", "")}]({scrape.get("url", "")})')

    return "\n".join(lines)


def extract_key_findings(abstract: str, limit: int = 5) -> list[str]:
    """Extract key findings from abstract as bullet points."""
    if not abstract:
        return []

    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+', abstract.strip())

    # Filter for informative sentences (skip short or uninformative ones)
    findings = []
    skip_patterns = [
        r'^(this|the|we|our|in this|here|however|therefore|thus|hence|moreover|furthermore)\b',
        r'^(background|introduction|methods?|results?|conclusions?|objectives?|aims?|purpose)[:.]?\s*$',
    ]

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 30:
            continue
        if any(re.match(pat, sentence, re.IGNORECASE) for pat in skip_patterns):
            continue
        # Prefer sentences with findings indicators
        if len(findings) < limit:
            findings.append(sentence)

    return findings[:limit]


def suggest_tags_for_content(
    existing_tags: dict[str, int],
    title: str,
    abstract: str,
    keywords: str,
    current_article_tags: list[str],
) -> list[str]:
    """Suggest new tags based on content that aren't already on the article."""
    # Combine text sources
    combined = " ".join([title, abstract, keywords]).lower()
    current_tags_lower = {t.lower() for t in current_article_tags}

    suggestions = []
    for tag in existing_tags:
        tag_lower = tag.lower()
        # Skip if already on article
        if tag_lower in current_tags_lower:
            continue
        # Check if tag appears in content
        # Handle multi-word tags
        tag_pattern = re.escape(tag_lower).replace(r"\ ", r"\s+")
        if re.search(tag_pattern, combined, re.IGNORECASE):
            suggestions.append(tag)

    # Sort by frequency in repo
    suggestions.sort(key=lambda t: -existing_tags.get(t, 0))
    return suggestions[:10]


def append_research(args: argparse.Namespace) -> dict[str, Any]:
    """Scrape a URL and append research to an existing article."""
    ensure_tool_dir(WEB_SCRAPER_DIR, "web-scraper")
    articles = load_articles(REPO_ROOT)
    tag_counts = collect_all_tags(articles)

    # Find target article
    target_path = Path(args.target)
    if target_path.is_absolute():
        target_path = target_path.relative_to(REPO_ROOT)

    target_article = None
    for article in articles:
        if article.path == str(target_path):
            target_article = article
            break

    if not target_article:
        raise FileNotFoundError(f"Target article not found: {target_path}")

    # Check for duplicate reference
    existing_ref = check_reference_exists(articles, args.url)
    if existing_ref.get("exists"):
        return {
            "status": "duplicate",
            "message": f"URL already referenced in {existing_ref['path']} as [^{existing_ref['ref_num']}]",
            "existing_reference": existing_ref,
        }

    # Scrape the URL
    scrape = run_json_tool(
        WEB_SCRAPER_DIR,
        ["main.py", args.url],
    )

    # Get next reference number
    next_ref = get_next_reference_number(target_article.body)

    # Suggest tags
    suggested_tags = suggest_tags_for_content(
        tag_counts,
        scrape.get("title", ""),
        scrape.get("abstract", ""),
        scrape.get("keywords", ""),
        target_article.tags,
    )

    # Extract key findings
    key_findings = extract_key_findings(scrape.get("abstract", ""))

    # Format content section
    content_lines = []
    if args.section:
        if args.subsection:
            content_lines.append(f"\n### {args.subsection}\n")
        else:
            content_lines.append(f"\n### {scrape.get('title', 'New Research')}\n")

    # Add abstract summary or key findings
    if key_findings:
        for finding in key_findings[:3]:
            content_lines.append(f"- {finding}[^{next_ref}]")
    elif scrape.get("abstract"):
        abstract = scrape["abstract"]
        if len(abstract) > 500:
            abstract = abstract[:500] + "..."
        content_lines.append(f"{abstract}[^{next_ref}]")

    content_block = "\n".join(content_lines) + "\n"

    # Format reference
    reference_block = format_reference_block(next_ref, scrape)

    result = {
        "target_article": target_article.path,
        "target_title": target_article.title,
        "scrape": {
            "url": scrape.get("url"),
            "title": scrape.get("title"),
            "study_type": scrape.get("study_type"),
            "authors": scrape.get("authors"),
            "pub_date": scrape.get("pub_date"),
        },
        "next_ref_num": next_ref,
        "suggested_tags": suggested_tags,
        "content_block": content_block,
        "reference_block": reference_block,
    }

    # If --apply flag is set, actually modify the file
    if args.apply:
        full_path = REPO_ROOT / target_path
        original_content = full_path.read_text(encoding="utf-8")

        # Find where to insert content
        new_content = original_content

        if args.section:
            # Find the section header
            section_pattern = re.compile(
                rf'^(#{{1,3}})\s+{re.escape(args.section)}\s*$',
                re.MULTILINE | re.IGNORECASE
            )
            section_match = section_pattern.search(original_content)

            if section_match:
                # Find the end of this section (next same-level or higher header, or EOF)
                section_level = len(section_match.group(1))
                section_end = section_match.end()

                # Look for next section of same or higher level
                next_section = re.compile(
                    rf'^#{{1,{section_level}}}\s+\S',
                    re.MULTILINE
                )
                next_match = next_section.search(original_content, section_end)

                if next_match:
                    insert_pos = next_match.start()
                else:
                    # Insert before references section if it exists
                    refs_match = re.search(r'\n\[\^1\]:', original_content)
                    if refs_match:
                        insert_pos = refs_match.start()
                    else:
                        insert_pos = len(original_content)

                new_content = (
                    original_content[:insert_pos].rstrip() +
                    "\n" + content_block + "\n" +
                    original_content[insert_pos:]
                )
            else:
                # Section not found - append before references
                refs_match = re.search(r'\n\[\^1\]:', original_content)
                if refs_match:
                    insert_pos = refs_match.start()
                    new_content = (
                        original_content[:insert_pos].rstrip() +
                        f"\n\n## {args.section}\n" + content_block + "\n" +
                        original_content[insert_pos:]
                    )
                else:
                    new_content = original_content.rstrip() + f"\n\n## {args.section}\n" + content_block
        else:
            # No section specified - append before references
            refs_match = re.search(r'\n\[\^1\]:', original_content)
            if refs_match:
                insert_pos = refs_match.start()
                new_content = (
                    original_content[:insert_pos].rstrip() +
                    "\n" + content_block + "\n" +
                    original_content[insert_pos:]
                )
            else:
                new_content = original_content.rstrip() + "\n" + content_block

        # Append reference at end
        new_content = new_content.rstrip() + "\n\n" + reference_block + "\n"

        # Add suggested tags to frontmatter if requested
        if args.add_tags and suggested_tags:
            tags_to_add = suggested_tags[:5] if not args.tags else args.tags
            # Parse frontmatter
            if new_content.startswith("---\n"):
                end_match = re.search(r'\n---\n', new_content[4:])
                if end_match:
                    fm_end = end_match.start() + 4
                    frontmatter = new_content[4:fm_end]
                    rest = new_content[fm_end + 4:]

                    # Add new tags
                    for tag in tags_to_add:
                        if tag not in target_article.tags:
                            frontmatter = frontmatter.rstrip() + f"\n- {tag}"

                    new_content = "---\n" + frontmatter + "\n---\n" + rest

        full_path.write_text(new_content, encoding="utf-8")
        result["applied"] = True
        result["status"] = "success"

        # Commit if requested
        if args.commit:
            import subprocess
            commit_msg = f"Add {scrape.get('study_type', 'research')} on {scrape.get('title', 'topic')[:50]}"
            subprocess.run(
                ["git", "add", str(target_path)],
                cwd=REPO_ROOT,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=REPO_ROOT,
                capture_output=True,
            )
            result["committed"] = True
    else:
        result["applied"] = False
        result["status"] = "preview"
        result["message"] = "Use --apply to write changes to the article"

    return result


def prepare_packet(args: argparse.Namespace) -> dict[str, Any]:
    ensure_tool_dir(WEB_SCRAPER_DIR, "web-scraper")
    output_dir = ensure_output_dir(Path(args.output_dir).expanduser().resolve())
    articles = load_articles(REPO_ROOT)

    source_slug = slugify(args.slug or Path(args.url).stem or "source")
    scrape_output_path = output_dir / f"{source_slug}-source.md"
    scrape = run_json_tool(
        WEB_SCRAPER_DIR,
        ["main.py", args.url, "--output", str(scrape_output_path)],
    )

    if args.match_existing:
        matches = top_matches_extended(
            articles,
            scrape["title"],
            alert_name=getattr(args, "alert_name", "") or "",
            limit=args.limit,
        )
    else:
        matches = top_matches(articles, scrape["title"], limit=args.limit)
    image_result = None
    image_public_id = args.image_public_id
    if args.image_file:
        ensure_tool_dir(IMAGE_UPLOAD_DIR, "image-upload")
        if not image_public_id:
            image_public_id = slugify(scrape["title"])
        upload_args = [
            "image-upload",
            args.image_file,
            "--public-id",
            image_public_id,
            "--validate-url",
        ]
        if args.image_folder:
            upload_args.extend(["--folder", args.image_folder])
        image_result = run_json_tool(IMAGE_UPLOAD_DIR, upload_args)
        image_public_id = image_result["public_id"]

    created_article_path = None
    suggested_article_path = None
    suggested_permalink = None
    selected_tags = args.tags or (
        matches[0]["tags"] if matches and matches[0]["score"] >= 75 and matches[0]["tags"] else DEFAULT_TAGS
    )

    if args.create_new or args.article_path:
        if args.article_path:
            relative_path = Path(args.article_path)
        elif args.category:
            relative_path = Path(args.category) / f"{slugify(scrape['title'])}.md"
        else:
            raise ValueError("--create-new requires --category or --article-path")

        if relative_path.is_absolute():
            raise ValueError("--article-path must be relative to the repo root")

        target_path = REPO_ROOT / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        suggested_article_path = str(relative_path)
        suggested_permalink = unique_route_key(relative_path, articles)
        if target_path.exists() and not args.overwrite:
            raise FileExistsError(f"Target article already exists: {relative_path}")

        stub = article_stub(
            title=args.title or scrape["title"],
            tags=selected_tags,
            image=image_public_id,
            permalink=suggested_permalink,
            abstract=scrape["abstract"],
            footnote=scrape["footnote_markdown"],
        )
        target_path.write_text(stub, encoding="utf-8")
        created_article_path = str(relative_path)

    packet = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "source_url": args.url,
        "scrape": scrape,
        "matches": matches,
        "selected_tags": selected_tags,
        "image_upload": image_result,
        "suggested_article_path": suggested_article_path,
        "suggested_permalink": suggested_permalink,
        "created_article_path": created_article_path,
    }

    packet_slug = slugify(scrape["title"]) or source_slug
    packet_path = output_dir / f"{packet_slug}-packet.json"
    packet_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    packet["packet_path"] = str(packet_path)

    # Mark the article as processed in the gmail-reader DB (non-fatal if unavailable)
    try:
        run_json_tool(GMAIL_READER_DIR, gmail_reader_command("mark-processed", args.url))
    except Exception:
        pass

    return packet


def queue_articles(args: argparse.Namespace) -> dict[str, Any]:
    ensure_tool_dir(GMAIL_READER_DIR, "gmail-reader")
    output_dir = ensure_output_dir(Path(args.output_dir).expanduser().resolve())
    articles = load_articles(REPO_ROOT)

    search_args = gmail_reader_command(
        "search",
        "--gmail-query",
        args.gmail_query,
        "--days-back",
        str(args.days_back),
        "--max-messages",
        str(args.max_messages),
        "--max-results",
        str(args.max_results),
    )
    if args.topic:
        search_args.extend(["--topic", args.topic])
    if args.include_review:
        search_args.append("--include-review")
    if args.save:
        search_args.append("--save")

    search_result = run_json_tool(GMAIL_READER_DIR, search_args)
    enriched_articles = []
    for item in search_result.get("articles", []):
        enriched_articles.append(
            {
                **item,
                "matches": top_matches(articles, item["title"], limit=args.limit),
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "search": search_result,
        "articles": enriched_articles,
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    queue_path = output_dir / f"queue-{timestamp}.json"
    queue_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["queue_path"] = str(queue_path)
    return payload


def match_title(args: argparse.Namespace) -> dict[str, Any]:
    articles = load_articles(REPO_ROOT)
    return {
        "title": args.title,
        "matches": top_matches(articles, args.title, limit=args.limit),
    }


def list_tags(args: argparse.Namespace) -> dict[str, Any]:
    articles = load_articles(REPO_ROOT)
    tag_counts = collect_all_tags(articles)

    # Use a sensible default limit for suggestions if not specified
    limit = args.limit if args.limit > 0 else 20

    if args.suggest:
        suggestions = suggest_tags(tag_counts, args.suggest, limit=limit)
        return {
            "query": args.suggest,
            "suggestions": suggestions,
            "total_unique_tags": len(tag_counts),
        }

    # Sort by frequency (descending) then alphabetically
    sorted_tags = sorted(
        [{"tag": tag, "count": count} for tag, count in tag_counts.items()],
        key=lambda x: (-x["count"], x["tag"].lower()),
    )

    if args.limit > 0:
        sorted_tags = sorted_tags[: args.limit]

    return {
        "tags": sorted_tags,
        "total_unique_tags": len(tag_counts),
        "total_tag_usages": sum(tag_counts.values()),
    }


def check_ref(args: argparse.Namespace) -> dict[str, Any]:
    articles = load_articles(REPO_ROOT)
    result = check_reference_exists(articles, args.url)
    result["url"] = args.url
    return result


def search_content(args: argparse.Namespace) -> dict[str, Any]:
    articles = load_articles(REPO_ROOT)
    fields = args.fields or list(SEARCH_FIELD_WEIGHTS.keys())
    return {
        "query": args.query,
        "match": args.match,
        "fields": fields,
        "matches": search_articles(
            articles,
            args.query,
            match_mode=args.match,
            fields=fields,
            limit=args.limit,
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agent orchestration helpers for the content repository."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    match_parser = subparsers.add_parser("match", help="Find likely existing article matches for a title.")
    match_parser.add_argument("title", help="Title or topic to match against the content repo.")
    match_parser.add_argument("--limit", type=int, default=5, help="Maximum matches to return.")

    search_parser = subparsers.add_parser("search", help="Search local markdown articles by content and metadata.")
    search_parser.add_argument("query", help="Search query to run against the content repo.")
    search_parser.add_argument(
        "--match",
        choices=["any", "all", "phrase"],
        default="all",
        help='Match mode: "all" requires every query token somewhere in the article, "any" allows partial hits, "phrase" looks for the full normalized phrase.',
    )
    search_parser.add_argument(
        "--field",
        dest="fields",
        action="append",
        choices=sorted(SEARCH_FIELD_WEIGHTS.keys()),
        default=[],
        help="Restrict search to one or more fields. Repeat to search multiple fields.",
    )
    search_parser.add_argument("--limit", type=int, default=20, help="Maximum matches to return.")

    queue_parser = subparsers.add_parser("queue", help="Build a cron-friendly candidate queue from Gmail Reader.")
    queue_parser.add_argument("--topic", help="Optional topic filter for gmail-reader search.")
    queue_parser.add_argument("--gmail-query", default="label:inbox newer_than:1d", help="Additional Gmail query terms.")
    queue_parser.add_argument("--days-back", type=int, default=1, help="Mailbox search window.")
    queue_parser.add_argument("--max-messages", type=int, default=25, help="Maximum messages to inspect.")
    queue_parser.add_argument("--max-results", type=int, default=10, help="Maximum article results to keep.")
    queue_parser.add_argument("--limit", type=int, default=5, help="Maximum content matches per result.")
    queue_parser.add_argument("--include-review", action="store_true", help="Include gmail-reader review items.")
    queue_parser.add_argument("--save", action="store_true", help="Persist parsed mail back into the gmail-reader database.")
    queue_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for the generated queue packet.")

    prepare_parser = subparsers.add_parser("prepare", help="Scrape a URL, match it to the repo, and optionally create a new article stub.")
    prepare_parser.add_argument("url", help="URL to scrape.")
    prepare_parser.add_argument("--title", help="Optional title override for a newly created article.")
    prepare_parser.add_argument("--slug", help="Optional slug for packet file names.")
    prepare_parser.add_argument("--category", help="Relative category path for a new article.")
    prepare_parser.add_argument("--article-path", help="Explicit relative output path for a new article.")
    prepare_parser.add_argument("--create-new", action="store_true", help="Create a new article stub.")
    prepare_parser.add_argument("--overwrite", action="store_true", help="Allow overwriting an existing target article.")
    prepare_parser.add_argument("--tag", dest="tags", action="append", default=[], help="Tag to place on a newly created article. Repeatable.")
    prepare_parser.add_argument("--image-file", help="Optional local image file to upload before creating a new article.")
    prepare_parser.add_argument("--image-public-id", help="Optional Cloudinary public ID for the article image.")
    prepare_parser.add_argument("--image-folder", help="Optional Cloudinary folder for uploaded images.")
    prepare_parser.add_argument("--limit", type=int, default=5, help="Maximum match candidates to return.")
    prepare_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for generated scrape packets.")
    prepare_parser.add_argument(
        "--match-existing",
        action="store_true",
        help="Lower match threshold to 15 and also match by alert name keywords.",
    )
    prepare_parser.add_argument(
        "--alert-name",
        default="",
        help="Alert name for keyword matching when --match-existing is set.",
    )

    tags_parser = subparsers.add_parser(
        "tags",
        help="List all tags in the content repo with frequency counts.",
    )
    tags_parser.add_argument(
        "--suggest",
        help="Suggest tags matching a query term.",
    )
    tags_parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum tags to return (0 = all).",
    )

    check_ref_parser = subparsers.add_parser(
        "check-ref",
        help="Check if a URL is already referenced in any article.",
    )
    check_ref_parser.add_argument(
        "url",
        help="URL to check for existing references.",
    )

    append_parser = subparsers.add_parser(
        "append",
        help="Scrape a URL and append research to an existing article.",
    )
    append_parser.add_argument(
        "url",
        help="URL to scrape and add to the article.",
    )
    append_parser.add_argument(
        "--target",
        required=True,
        help="Relative path to the target article in the content repo.",
    )
    append_parser.add_argument(
        "--section",
        help="Section header to insert content under (e.g., 'Disease / Symptom Treatment').",
    )
    append_parser.add_argument(
        "--subsection",
        help="Subsection header to create (e.g., 'Spine Health').",
    )
    append_parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        default=[],
        help="Tag to add to the article. Repeatable.",
    )
    append_parser.add_argument(
        "--add-tags",
        action="store_true",
        help="Automatically add suggested tags to the article frontmatter.",
    )
    append_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes to the article (default is preview mode).",
    )
    append_parser.add_argument(
        "--commit",
        action="store_true",
        help="Create a git commit after applying changes (implies --apply).",
    )

    backlog_parser = subparsers.add_parser(
        "backlog",
        help="Query the gmail-reader DB for unprocessed candidate articles.",
    )
    backlog_parser.add_argument(
        "--status",
        choices=["selected", "review", "rejected", "all"],
        default="selected",
    )
    backlog_parser.add_argument("--min-score", type=int, default=0)
    backlog_parser.add_argument(
        "--source",
        help="Domain keyword filter (e.g. 'frontiersin', 'mdpi').",
    )
    backlog_parser.add_argument("--open-access", action="store_true")
    backlog_parser.add_argument("--include-processed", action="store_true")
    backlog_parser.add_argument("--limit", type=int, default=20)

    return parser


def backlog_query(args: argparse.Namespace) -> dict[str, Any]:
    ensure_tool_dir(GMAIL_READER_DIR, "gmail-reader")
    backlog_args = gmail_reader_command(
        "backlog",
        "--status", args.status,
        "--min-score", str(args.min_score),
        "--limit", str(args.limit),
    )
    if args.source:
        backlog_args.extend(["--source", args.source])
    if args.open_access:
        backlog_args.append("--open-access")
    if args.include_processed:
        backlog_args.append("--include-processed")
    return run_json_tool(GMAIL_READER_DIR, backlog_args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "match":
            result = match_title(args)
        elif args.command == "search":
            result = search_content(args)
        elif args.command == "queue":
            result = queue_articles(args)
        elif args.command == "prepare":
            result = prepare_packet(args)
        elif args.command == "backlog":
            result = backlog_query(args)
        elif args.command == "tags":
            result = list_tags(args)
        elif args.command == "check-ref":
            result = check_ref(args)
        elif args.command == "append":
            if args.commit:
                args.apply = True  # --commit implies --apply
            result = append_research(args)
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "result": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
