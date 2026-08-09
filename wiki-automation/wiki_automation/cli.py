from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from math import log
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import yaml

SKIP_DIRS = {".git", ".github", ".venv", "node_modules", "__pycache__"}
DEFAULT_TAGS = ["Research"]
SEARCH_FIELD_WEIGHTS = {
    "title": 30.0,
    "tags": 20.0,
    "permalink": 20.0,
    "path": 15.0,
    "body": 15.0,
}
RESEARCH_MATCH_STOPWORDS = {
    "about", "after", "among", "analysis", "article", "based", "benefit",
    "benefits", "clinical", "disease", "effect", "effects", "evidence",
    "expanding", "findings", "health", "human", "mechanism", "mechanisms",
    "meta", "model", "models", "potential", "research", "review", "role",
    "signal", "study", "studies", "systematic", "therapy", "treatment",
    "using", "with", "without", "from", "into", "this", "that", "their",
    "these", "those", "were", "been", "have", "has", "and", "the", "for",
}

TOOL_DIR = Path(__file__).resolve().parents[1]
AGENT_TOOLS_ROOT = Path(
    os.environ.get("AGENT_TOOLS_ROOT", str(TOOL_DIR.parent))
).expanduser().resolve()
DEFAULT_MANAGED_CONTENT_REPO_ROOT = Path(
    os.environ.get(
        "CONTENT_REPO_MANAGED_ROOT",
        str(AGENT_TOOLS_ROOT / "runtime" / "content-repo"),
    )
).expanduser().resolve()
REPO_ROOT = Path(
    os.environ.get("CONTENT_REPO_ROOT", str(DEFAULT_MANAGED_CONTENT_REPO_ROOT))
).expanduser().resolve()
GMAIL_READER_DIR = AGENT_TOOLS_ROOT / "gmail-reader"
WEB_SCRAPER_DIR = AGENT_TOOLS_ROOT / "web-scraper"
IMAGE_UPLOAD_DIR = AGENT_TOOLS_ROOT / "image-upload"
DEFAULT_OUTPUT_DIR = TOOL_DIR / "out"
CONTENT_INDEX_DIR = AGENT_TOOLS_ROOT / "runtime" / "indexes"
DEFAULT_ARCHIVE_DIR = Path(
    os.environ.get("AGENT_ARCHIVE_ROOT", str(AGENT_TOOLS_ROOT / "archive"))
).expanduser().resolve()


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


def split_markdown_document(text: str) -> tuple[str | None, str]:
    if not text.startswith("---\n"):
        return None, text

    lines = text.splitlines()
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return None, text

    frontmatter = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])
    return frontmatter, body


def clean_frontmatter_mapping(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if value in (None, "", []):
            continue
        cleaned[str(key)] = value
    return cleaned


def parse_markdown_article(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    frontmatter_text, body = split_markdown_document(text)
    if frontmatter_text is None:
        return {}, text

    try:
        metadata = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        return {"_frontmatter_error": str(exc)}, body
    if not isinstance(metadata, dict):
        return {"_frontmatter_error": "frontmatter is not a YAML mapping"}, body
    return metadata, body


def build_markdown_article(metadata: dict[str, Any], body: str) -> str:
    cleaned = clean_frontmatter_mapping(metadata)
    if not cleaned:
        return body.lstrip("\n")

    frontmatter = yaml.safe_dump(
        cleaned,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
    ).strip()
    normalized_body = body.lstrip("\n")
    if normalized_body:
        return f"---\n{frontmatter}\n---\n\n{normalized_body}"
    return f"---\n{frontmatter}\n---\n"


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


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return url.strip().lower()
    if not parsed.scheme or not parsed.netloc:
        return url.strip().lower()
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    return f"https://{hostname}{path}"


def paper_fingerprint(identifier: str) -> str:
    normalized = canonicalize_url(identifier) or normalize(identifier)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def load_articles(repo_root: Path) -> list[ArticleRecord]:
    if not repo_root.is_dir():
        raise FileNotFoundError(
            f"Content repo not found: {repo_root}. Run ./agent-workflow setup or configure CONTENT_REPO_ROOT."
        )
    if repo_root == AGENT_TOOLS_ROOT:
        raise ValueError(
            "CONTENT_REPO_ROOT points at research-tools itself. Use the managed content repo working copy instead."
        )

    cache_path = CONTENT_INDEX_DIR / f"{hashlib.sha256(str(repo_root).encode('utf-8')).hexdigest()[:16]}.json"
    snapshot: list[dict[str, int | str]] = []
    markdown_paths: list[Path] = []
    for path in sorted(repo_root.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        stat = path.stat()
        snapshot.append(
            {
                "path": str(path.relative_to(repo_root)),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            }
        )
        markdown_paths.append(path)

    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("repo_root") == str(repo_root) and cached.get("snapshot") == snapshot:
                return [
                    ArticleRecord(
                        path=item["path"],
                        title=item["title"],
                        stem=item["stem"],
                        tags=item["tags"],
                        permalink=item.get("permalink"),
                        body=item["body"],
                    )
                    for item in cached.get("articles", [])
                ]
        except Exception:
            pass

    articles: list[ArticleRecord] = []
    for path in markdown_paths:
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

    CONTENT_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "repo_root": str(repo_root),
                "snapshot": snapshot,
                "articles": [
                    {
                        "path": article.path,
                        "title": article.title,
                        "stem": article.stem,
                        "tags": article.tags,
                        "permalink": article.permalink,
                        "body": article.body,
                    }
                    for article in articles
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
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
    url_canonical = canonicalize_url(url_normalized)
    # Also check without protocol
    url_no_protocol = re.sub(r'^https?://', '', url_normalized)

    for article in articles:
        refs = find_reference_urls(article.body)
        for ref in refs:
            ref_url = ref["url"].strip().rstrip("/").lower()
            ref_canonical = canonicalize_url(ref_url)
            ref_no_protocol = re.sub(r'^https?://', '', ref_url)
            if (
                url_normalized == ref_url
                or url_no_protocol == ref_no_protocol
                or (url_canonical and ref_canonical == url_canonical)
            ):
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


def research_match_candidates(
    articles: list[ArticleRecord],
    *,
    title: str,
    abstract: str = "",
    keywords: str = "",
    alert_name: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Retrieve content homes using source entities, not only paper-title fuzziness.

    Research paper titles are often descriptions rather than encyclopedia topics.
    This scorer emphasizes rare compounds/conditions repeated in the title,
    abstract, keywords, or Scholar alert name and searches titles, tags, paths,
    and existing article bodies.
    """

    def useful_terms(value: str) -> list[str]:
        return [
            term
            for term in normalize(value).split()
            if len(term) >= 4 and term not in RESEARCH_MATCH_STOPWORDS
        ]

    title_terms = useful_terms(title)
    abstract_terms = useful_terms(abstract)
    keyword_terms = useful_terms(keywords)
    alert_terms = useful_terms(alert_name)
    query_counts: Counter[str] = Counter()
    query_counts.update({term: 5 for term in alert_terms})
    query_counts.update({term: 3 for term in title_terms})
    query_counts.update({term: 3 for term in keyword_terms})
    for term, count in Counter(abstract_terms).items():
        query_counts[term] += min(count, 3)

    if not query_counts:
        return top_matches(articles, title, limit=limit)

    article_fields: list[tuple[ArticleRecord, dict[str, set[str]]]] = []
    document_frequency: Counter[str] = Counter()
    for article in articles:
        fields = {
            "title": set(useful_terms(article.title)),
            "tags": set(useful_terms(" ".join(article.tags))),
            "path": set(useful_terms(article.path)),
            "body": set(useful_terms(article.body)),
        }
        article_fields.append((article, fields))
        document_frequency.update(set().union(*fields.values()) & query_counts.keys())

    corpus_size = max(len(articles), 1)
    field_weights = {"title": 9.0, "tags": 7.0, "path": 5.0, "body": 1.0}
    alert_phrase = normalize(alert_name)
    results: list[dict[str, Any]] = []
    for article, fields in article_fields:
        contributions: dict[str, float] = {}
        matched_terms: set[str] = set()
        score = 0.0
        for field, field_terms in fields.items():
            field_score = 0.0
            for term in field_terms & query_counts.keys():
                idf = log((corpus_size + 1) / (document_frequency[term] + 1)) + 1.0
                field_score += query_counts[term] * idf * field_weights[field]
                matched_terms.add(term)
            if field_score:
                contributions[field] = round(field_score, 2)
                score += field_score

        normalized_identity = " ".join(
            [normalize(article.title), normalize(" ".join(article.tags)), normalize(article.path)]
        )
        if alert_phrase and alert_phrase in normalized_identity:
            score += 100.0
            contributions["alert_phrase"] = 100.0
        if not matched_terms:
            continue
        results.append(
            {
                "path": article.path,
                "title": article.title,
                "tags": article.tags,
                "permalink": article.permalink,
                "route_key": article.route_key,
                "score": round(score, 2),
                "match_method": "research_hybrid",
                "matched_terms": sorted(matched_terms),
                "score_components": contributions,
            }
        )

    results.sort(key=lambda item: (-item["score"], item["path"].lower()))
    return results[:limit]


def match_research_packet(
    articles: list[ArticleRecord],
    scrape: dict[str, Any],
    *,
    alert_name: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return topic-oriented homes for a normalized scraper result."""
    return research_match_candidates(
        articles,
        title=str(scrape.get("title") or ""),
        abstract=str(scrape.get("abstract") or ""),
        keywords=str(scrape.get("keywords") or ""),
        alert_name=alert_name,
        limit=limit,
    )


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


def scrape_source_packet(source: str, output_path: Path | None = None) -> dict[str, Any]:
    scrape_args = ["main.py", source]
    if output_path is not None:
        scrape_args.extend(["--output", str(output_path)])
    return run_json_tool(WEB_SCRAPER_DIR, scrape_args)


def sync_paper_record(
    scrape: dict[str, Any],
    *,
    workflow_state: str = "discovered",
    matched_content_path: str = "",
) -> dict[str, Any] | None:
    if not GMAIL_READER_DIR.is_dir():
        return None
    url = scrape.get("reference_url") or scrape.get("url") or scrape.get("requested_url") or ""
    title = scrape.get("title") or "Untitled Source"
    doi = scrape.get("doi") or ""
    pmid = scrape.get("pmid") or ""
    if not (title or url or doi):
        return None

    upsert_args = gmail_reader_command(
        "upsert-paper",
        "--title",
        title,
        "--url",
        url,
        "--doi",
        doi,
        "--pmid",
        pmid,
        "--workflow-state",
        workflow_state,
    )
    if matched_content_path:
        upsert_args.extend(["--matched-content-path", matched_content_path])
    try:
        return run_json_tool(GMAIL_READER_DIR, upsert_args)
    except Exception:
        return None


def set_paper_workflow_state(
    identifier: str,
    workflow_state: str,
    *,
    matched_content_path: str = "",
    archive_path: str = "",
    commit: str = "",
    pr: str = "",
) -> dict[str, Any] | None:
    if not GMAIL_READER_DIR.is_dir() or not identifier:
        return None
    args = gmail_reader_command(
        "set-paper-state",
        identifier,
        "--state",
        workflow_state,
    )
    if matched_content_path:
        args.extend(["--matched-content-path", matched_content_path])
    if commit:
        args.extend(["--commit", commit])
    if pr:
        args.extend(["--pr", pr])
    if archive_path:
        args.extend(["--archive-path", archive_path])
    try:
        return run_json_tool(GMAIL_READER_DIR, args)
    except Exception:
        return None


def attach_archived_source_state(identifier: str, archive_path: str) -> dict[str, Any] | None:
    if not GMAIL_READER_DIR.is_dir() or not identifier or not archive_path:
        return None
    return set_paper_workflow_state(
        identifier,
        "discovered",
        archive_path=archive_path,
    )


def archive_source_material(source: str, *, root: Path = DEFAULT_ARCHIVE_DIR) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = slugify(Path(source).stem if not source.startswith("http") else paper_fingerprint(source))
    archive_dir = ensure_output_dir(root / timestamp[:4] / timestamp[4:6] / f"{timestamp}-{slug}")

    metadata: dict[str, Any] = {
        "archived_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "requested_source": source,
    }
    if source.startswith("http://") or source.startswith("https://"):
        request = Request(source, headers={"User-Agent": "content-agent-tools/0.1"})
        with urlopen(request, timeout=60) as response:
            payload = response.read()
            content_type = response.headers.get_content_type()
            final_url = response.geturl()
        metadata["final_url"] = final_url
        metadata["content_type"] = content_type
        if source.lower().endswith(".pdf") or content_type == "application/pdf":
            extension = ".pdf"
            archive_path = archive_dir / "source.pdf"
        else:
            extension = ".html"
            archive_path = archive_dir / "source.html"
        archive_path.write_bytes(payload)
        metadata["archive_kind"] = extension.lstrip(".")
    else:
        local_path = Path(source).expanduser().resolve()
        if not local_path.is_file():
            raise FileNotFoundError(f"Archive source not found: {source}")
        extension = local_path.suffix or ".bin"
        archive_path = archive_dir / f"source{extension}"
        shutil.copy2(local_path, archive_path)
        metadata["archive_kind"] = extension.lstrip(".")
        metadata["final_url"] = ""
        metadata["content_type"] = ""

    metadata["archive_path"] = str(archive_path)
    (archive_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8",
    )
    return metadata


def article_stub(
    *,
    title: str,
    tags: list[str],
    image: str | None,
    permalink: str | None,
    abstract: str,
    footnote: str,
) -> str:
    abstract_block = f"\n## Abstract\n\n{abstract}\n" if abstract else ""
    metadata: dict[str, Any] = {
        "title": title,
        "tags": tags,
    }
    if permalink:
        metadata["permalink"] = permalink
    if image:
        metadata["image"] = image
    body = (
        "Brief introduction.\n"
        "\n## Key Findings\n\n"
        "- Add the first validated finding here.[^1]\n"
        f"{abstract_block}"
        "\n## Notes\n\n"
        "- Review the scraped packet before publishing.\n\n"
        f"{footnote}\n"
    )
    return build_markdown_article(metadata, body)


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
            tags_to_add = args.tags or suggested_tags[:5]
            frontmatter_text, body_text = split_markdown_document(new_content)
            if frontmatter_text is not None:
                metadata = yaml.safe_load(frontmatter_text) or {}
                existing_tags = coerce_frontmatter_list(metadata.get("tags", []))
                metadata["tags"] = existing_tags + [
                    tag for tag in tags_to_add if tag not in existing_tags
                ]
                new_content = build_markdown_article(metadata, body_text)

        full_path.write_text(new_content, encoding="utf-8")
        result["applied"] = True
        result["status"] = "success"
        sync_paper_record(
            scrape,
            workflow_state="drafted",
            matched_content_path=str(target_path),
        )

        # Commit if requested
        if args.commit:
            import subprocess
            commit_msg = f"Add {scrape.get('study_type', 'research')} on {scrape.get('title', 'topic')[:50]}"
            subprocess.run(
                ["git", "add", str(target_path)],
                cwd=REPO_ROOT,
                capture_output=True,
            )
            commit_result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            result["committed"] = True
            if commit_result.returncode == 0:
                commit_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                result["commit_sha"] = commit_sha
                set_paper_workflow_state(
                    scrape.get("reference_url") or scrape.get("url") or args.url,
                    "committed",
                    matched_content_path=str(target_path),
                    commit=commit_sha,
                )
        else:
            set_paper_workflow_state(
                scrape.get("reference_url") or scrape.get("url") or args.url,
                "drafted",
                matched_content_path=str(target_path),
            )
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
    scrape = scrape_source_packet(args.url, scrape_output_path)

    matches = match_research_packet(
        articles,
        scrape,
        alert_name=getattr(args, "alert_name", "") or "",
        limit=args.limit,
    )
    image_result = None
    image_public_id = args.image_public_id

    image_capture_url = args.image_screenshot_url or (args.url if args.image_screenshot else None)
    image_source_count = sum(
        1 for value in (args.image_file, image_capture_url) if value
    )
    if image_source_count > 1:
        raise ValueError(
            "Use only one image source: --image-file, --image-screenshot, or --image-screenshot-url"
        )

    if args.image_file or image_capture_url:
        ensure_tool_dir(IMAGE_UPLOAD_DIR, "image-upload")
        if not image_public_id:
            image_public_id = slugify(scrape["title"])
        if args.image_file:
            upload_args = [
                "image-upload",
                args.image_file,
            ]
        else:
            upload_args = [
                "image-upload",
                "--capture-url",
                image_capture_url,
            ]
            if args.image_screenshot_output:
                upload_args.extend(["--capture-output", args.image_screenshot_output])
            if args.image_screenshot_full_page:
                upload_args.append("--full-page")
            if args.image_screenshot_annotate:
                upload_args.append("--annotate")
            upload_args.extend(["--wait-ms", str(args.image_screenshot_wait_ms)])

        upload_args.extend([
            "--public-id",
            image_public_id,
            "--validate-url",
        ])
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
    packet["paper"] = sync_paper_record(
        scrape,
        workflow_state="drafted" if created_article_path else "matched" if matches else "scraped",
        matched_content_path=created_article_path or "",
    )
    if created_article_path:
        packet["paper_state"] = set_paper_workflow_state(
            scrape.get("reference_url") or scrape.get("url") or args.url,
            "drafted",
            matched_content_path=created_article_path,
        )

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
                "matches": research_match_candidates(
                    articles,
                    title=str(item.get("title") or ""),
                    abstract=str(item.get("abstract") or ""),
                    keywords=str(item.get("keywords") or ""),
                    alert_name=str(item.get("alert_name") or ""),
                    limit=args.limit,
                ),
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
        "matches": research_match_candidates(
            articles,
            title=args.title,
            abstract=args.abstract,
            keywords=args.keywords,
            alert_name=args.alert_name,
            limit=args.limit,
        ),
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


def audit_tags(args: argparse.Namespace) -> dict[str, Any]:
    articles = load_articles(REPO_ROOT)
    groups: dict[str, dict[str, Any]] = {}
    for article in articles:
        for tag in article.tags:
            normalized = normalize(tag)
            group = groups.setdefault(
                normalized,
                {"normalized": normalized, "variants": {}, "paths": set(), "total": 0},
            )
            group["variants"][tag] = group["variants"].get(tag, 0) + 1
            group["paths"].add(article.path)
            group["total"] += 1

    conflicts = []
    for group in groups.values():
        if len(group["variants"]) < 2 and not args.include_all:
            continue
        sorted_variants = sorted(
            (
                {"tag": tag, "count": count}
                for tag, count in group["variants"].items()
            ),
            key=lambda item: (-item["count"], item["tag"].lower()),
        )
        conflicts.append(
            {
                "normalized": group["normalized"],
                "total": group["total"],
                "suggested_canonical": sorted_variants[0]["tag"],
                "variants": sorted_variants,
                "example_paths": sorted(group["paths"])[:5],
            }
        )

    conflicts.sort(key=lambda item: (-len(item["variants"]), -item["total"], item["normalized"]))
    if args.limit > 0:
        conflicts = conflicts[: args.limit]
    return {
        "group_count": len(conflicts),
        "groups": conflicts,
    }


def lint_frontmatter(args: argparse.Namespace) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(REPO_ROOT.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        scanned += 1
        metadata, body = parse_markdown_article(path)
        file_issues: list[str] = []
        rel_path = str(path.relative_to(REPO_ROOT))

        if "_frontmatter_error" in metadata:
            file_issues.append(f"invalid_frontmatter:{metadata['_frontmatter_error']}")
        else:
            title = metadata.get("title")
            tags = metadata.get("tags")
            if not title or not str(title).strip():
                file_issues.append("missing_title")
            if tags is None:
                file_issues.append("missing_tags")
            elif not isinstance(tags, list):
                file_issues.append("tags_not_list")
            else:
                lowered: set[str] = set()
                duplicates: list[str] = []
                for tag in coerce_frontmatter_list(tags):
                    lowered_tag = tag.lower()
                    if lowered_tag in lowered and tag not in duplicates:
                        duplicates.append(tag)
                    lowered.add(lowered_tag)
                if duplicates:
                    file_issues.append(f"duplicate_tags:{', '.join(duplicates)}")
        if not body.strip():
            file_issues.append("empty_body")
        if file_issues:
            issues.append({"path": rel_path, "issues": file_issues})

    return {
        "scanned_files": scanned,
        "issue_count": len(issues),
        "files": issues[: args.limit] if args.limit > 0 else issues,
    }


def check_duplicate_paper(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(getattr(args, "repo_root", REPO_ROOT)).expanduser().resolve()
    articles = load_articles(repo_root)
    identifier = args.identifier.strip()
    normalized_identifier = normalize(identifier)
    canonical_identifier = canonicalize_url(identifier)
    content_hits: list[dict[str, Any]] = []

    for article in articles:
        refs = find_reference_urls(article.body)
        for ref in refs:
            ref_url = ref["url"].strip()
            ref_title = ref["title"].strip()
            if (
                identifier.lower() in ref_url.lower()
                or (canonical_identifier and canonicalize_url(ref_url) == canonical_identifier)
                or normalize(ref_title) == normalized_identifier
            ):
                content_hits.append(
                    {
                        "path": article.path,
                        "title": article.title,
                        "ref_num": ref["ref_num"],
                        "ref_title": ref_title,
                        "ref_url": ref_url,
                    }
                )

    paper_result = None
    if GMAIL_READER_DIR.is_dir():
        try:
            paper_result = run_json_tool(
                GMAIL_READER_DIR,
                gmail_reader_command("find-paper", identifier),
            )
        except Exception:
            paper_result = None

    return {
        "identifier": identifier,
        "content_hit_count": len(content_hits),
        "content_hits": content_hits[: args.limit] if args.limit > 0 else content_hits,
        "paper_result": paper_result,
    }


def archive_source_command(args: argparse.Namespace) -> dict[str, Any]:
    metadata = archive_source_material(
        args.source,
        root=Path(args.archive_root).expanduser().resolve(),
    )
    paper_state = attach_archived_source_state(
        args.identifier or args.source,
        metadata["archive_path"],
    )
    return {
        "archive": metadata,
        "paper_state": paper_state,
    }


def ingest_paper(args: argparse.Namespace) -> dict[str, Any]:
    ensure_tool_dir(WEB_SCRAPER_DIR, "web-scraper")
    output_dir = ensure_output_dir(Path(args.output_dir).expanduser().resolve())
    articles = load_articles(REPO_ROOT)
    source_slug = slugify(args.slug or Path(args.source).stem or "source")
    scrape_output_path = output_dir / f"{source_slug}-source.md"
    scrape = scrape_source_packet(args.source, scrape_output_path)
    matches = match_research_packet(
        articles,
        scrape,
        alert_name=args.alert_name,
        limit=args.limit,
    )
    archive_result = None
    if args.archive:
        archive_result = archive_source_material(
            args.source,
            root=Path(args.archive_root).expanduser().resolve(),
        )
        attach_archived_source_state(
            scrape.get("reference_url") or scrape.get("url") or args.source,
            archive_result["archive_path"],
        )

    packet = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "source": args.source,
        "scrape": scrape,
        "matches": matches,
        "archive": archive_result,
        "paper": sync_paper_record(
            scrape,
            workflow_state="matched" if matches else "scraped",
        ),
    }
    packet_path = output_dir / f"{source_slug}-ingest.json"
    packet_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    packet["packet_path"] = str(packet_path)
    return packet


def intake_source(args: argparse.Namespace) -> dict[str, Any]:
    ensure_tool_dir(WEB_SCRAPER_DIR, "web-scraper")
    output_dir = ensure_output_dir(Path(args.output_dir).expanduser().resolve())
    articles = load_articles(REPO_ROOT)
    source_slug = slugify(args.slug or Path(args.source).stem or "source")
    scrape_output_path = output_dir / f"{source_slug}-source.md"
    scrape = scrape_source_packet(args.source, scrape_output_path)
    matches = match_research_packet(
        articles,
        scrape,
        alert_name=args.alert_name,
        limit=args.limit,
    )
    duplicate = check_duplicate_paper(
        argparse.Namespace(identifier=scrape.get("reference_url") or scrape.get("url") or args.source, limit=args.limit)
    )
    archive_result = None
    if args.archive:
        archive_result = archive_source_material(
            args.source,
            root=Path(args.archive_root).expanduser().resolve(),
        )
        attach_archived_source_state(
            scrape.get("reference_url") or scrape.get("url") or args.source,
            archive_result["archive_path"],
        )

    suggested_action = "new_article"
    if duplicate.get("content_hit_count", 0) > 0:
        suggested_action = "already_cited"
    elif matches:
        suggested_action = "append_existing"

    packet = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "source": args.source,
        "scrape": scrape,
        "matches": matches,
        "duplicate": duplicate,
        "archive": archive_result,
        "suggested_action": suggested_action,
        "paper": sync_paper_record(
            scrape,
            workflow_state="matched" if matches or duplicate.get("content_hit_count", 0) else "scraped",
        ),
    }
    packet_path = output_dir / f"{source_slug}-intake.json"
    packet_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
    packet["packet_path"] = str(packet_path)
    return packet


def local_publish_command(args: argparse.Namespace) -> dict[str, Any]:
    from .local_publish import configured_client_values, run_local_publish

    default_base_url, default_model = configured_client_values()
    result = run_local_publish(
        source=args.source,
        alert_name=args.alert_name,
        content_repo=REPO_ROOT,
        tools_root=AGENT_TOOLS_ROOT,
        output_dir=Path(args.output_dir).expanduser().resolve(),
        base_url=args.base_url or default_base_url,
        model=args.model or default_model,
        publish=args.publish,
        base_ref=args.base_ref,
        max_candidates=args.limit,
        max_draft_attempts=args.max_draft_attempts,
    )
    candidates = result.get("candidates") or []
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "report_path": result.get("report_path"),
        "diff_path": result.get("diff_path"),
        "target_path": result.get("target_path"),
        "worktree": result.get("worktree"),
        "branch": result.get("branch"),
        "commit": result.get("commit"),
        "pr_url": result.get("pr_url"),
        "packet_validation": result.get("packet_validation"),
        "plan_validation": result.get("plan_validation"),
        "rendered_validation": result.get("rendered_validation"),
        "critic": result.get("critic"),
        "top_candidate": candidates[0] if candidates else None,
        "duplicate": result.get("duplicate"),
    }


def update_local_job(job_id: int, state: str, worker: str, **values: str) -> dict[str, Any]:
    command = gmail_reader_command(
        "set-publication-job-state",
        str(job_id),
        "--state",
        state,
        "--worker",
        worker,
    )
    option_names = {
        "paper_key": "--paper-key",
        "packet_path": "--packet-path",
        "target_path": "--target-path",
        "branch": "--branch",
        "commit": "--commit",
        "pr": "--pr",
        "error": "--error",
        "result_json": "--result-json",
    }
    for key, option in option_names.items():
        if values.get(key):
            command.extend([option, values[key]])
    return run_json_tool(GMAIL_READER_DIR, command)


def enqueue_local_command(args: argparse.Namespace) -> dict[str, Any]:
    ensure_tool_dir(GMAIL_READER_DIR, "gmail-reader")
    return run_json_tool(
        GMAIL_READER_DIR,
        gmail_reader_command(
            "enqueue-publication",
            args.identifier,
            "--max-attempts",
            str(args.max_attempts),
        ),
    )


def enqueue_local_backlog_command(args: argparse.Namespace) -> dict[str, Any]:
    ensure_tool_dir(GMAIL_READER_DIR, "gmail-reader")
    return run_json_tool(
        GMAIL_READER_DIR,
        gmail_reader_command(
            "enqueue-publication-backlog",
            "--status",
            args.status,
            "--min-score",
            str(args.min_score),
            "--limit",
            str(args.limit),
            "--max-attempts",
            str(args.max_attempts),
        ),
    )


def local_worker_command(args: argparse.Namespace) -> dict[str, Any]:
    from .local_publish import configured_client_values, run_local_publish

    ensure_tool_dir(GMAIL_READER_DIR, "gmail-reader")
    default_base_url, default_model = configured_client_values()
    worker = args.worker or f"{os.uname().nodename}-{os.getpid()}"
    processed: list[dict[str, Any]] = []
    for _ in range(args.max_jobs):
        claim = run_json_tool(
            GMAIL_READER_DIR,
            gmail_reader_command(
                "claim-publication",
                "--worker",
                worker,
                "--lease-seconds",
                str(args.lease_seconds),
            ),
        )
        if not claim.get("claimed"):
            break
        job = claim["job"]
        job_id = int(job["job_id"])
        job_output = Path(args.output_dir).expanduser().resolve() / f"job-{job_id}"

        def progress(state: str) -> None:
            update_local_job(job_id, state, worker)

        try:
            progress("scraping")
            report = run_local_publish(
                source=str(job["source_url"]),
                alert_name=str(job.get("alert_name") or ""),
                content_repo=REPO_ROOT,
                tools_root=AGENT_TOOLS_ROOT,
                output_dir=job_output,
                base_url=args.base_url or default_base_url,
                model=args.model or default_model,
                publish=args.publish,
                base_ref=args.base_ref,
                max_candidates=args.limit,
                max_draft_attempts=args.max_draft_attempts,
                progress=progress,
            )
            status = str(report.get("status") or "needs_review")
            terminal_state = {
                "duplicate": "duplicate",
                "needs_review": "needs_review",
                "pr_open": "pr_open",
                "validated_draft": "needs_review",
            }.get(status, "needs_review")
            summary = {
                "status": status,
                "reason": report.get("reason"),
                "report_path": report.get("report_path"),
                "diff_path": report.get("diff_path"),
            }
            update_local_job(
                job_id,
                terminal_state,
                worker,
                packet_path=str(report.get("report_path") or ""),
                target_path=str(report.get("target_path") or ""),
                branch=str(report.get("branch") or ""),
                commit=str(report.get("commit") or ""),
                pr=str(report.get("pr_url") or ""),
                result_json=json.dumps(summary),
            )
            processed.append({"job_id": job_id, **summary})
        except Exception as exc:
            next_state = (
                "retry"
                if int(job["attempt_count"]) < int(job["max_attempts"])
                else "failed"
            )
            update_local_job(job_id, next_state, worker, error=str(exc)[:4000])
            processed.append({"job_id": job_id, "status": next_state, "error": str(exc)})
    return {"worker": worker, "processed": processed, "idle": not processed}


def create_pull_request(
    *,
    base: str,
    head: str | None,
    title: str | None,
    body: str | None,
    fill: bool,
    draft: bool,
) -> dict[str, Any]:
    gh_binary = shutil.which("gh")
    if not gh_binary:
        raise FileNotFoundError("gh CLI is not installed")
    if not fill and (not title or not body):
        raise ValueError("Provide --fill or both --title and --body")

    command = [gh_binary, "pr", "create", "--base", base]
    if head:
        command.extend(["--head", head])
    if draft:
        command.append("--draft")
    if fill:
        command.append("--fill")
    else:
        command.extend(["--title", title, "--body", body])

    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gh pr create failed")
    return {
        "url": result.stdout.strip(),
        "base": base,
        "head": head or "",
        "draft": draft,
    }


def open_pull_request(args: argparse.Namespace) -> dict[str, Any]:
    return create_pull_request(
        base=args.base,
        head=args.head,
        title=args.title,
        body=args.body,
        fill=args.fill,
        draft=args.draft,
    )


def changed_repo_paths() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git status failed")
    paths: list[str] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        cleaned = path.strip()
        if cleaned:
            paths.append(cleaned)
    return paths


def article_repo_paths(paths: list[str]) -> list[str]:
    selected: list[str] = []
    for value in paths:
        path = Path(value)
        if path.suffix.lower() != ".md":
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        selected.append(path.as_posix())
    return sorted(set(selected))


def default_commit_message(paths: list[str]) -> str:
    if len(paths) == 1:
        return f"Update article {Path(paths[0]).stem.replace('-', ' ')}"
    return f"Update {len(paths)} articles"


def default_branch_name(paths: list[str]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    seed = Path(paths[0]).stem if paths else "articles"
    return f"agent/{slugify(seed)[:40]}-{timestamp}"


def ensure_branch_checked_out(branch: str) -> None:
    existing = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        command = ["git", "checkout", branch]
    else:
        command = ["git", "checkout", "-b", branch]
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git checkout failed")


def stage_and_commit_paths(paths: list[str], commit_message: str, include_all: bool) -> str:
    add_command = ["git", "add", "-A"] if include_all else ["git", "add", "--", *paths]
    add_result = subprocess.run(add_command, cwd=REPO_ROOT, capture_output=True, text=True)
    if add_result.returncode != 0:
        raise RuntimeError(add_result.stderr.strip() or add_result.stdout.strip() or "git add failed")

    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        raise RuntimeError(commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit failed")

    sha_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if sha_result.returncode != 0:
        raise RuntimeError(sha_result.stderr.strip() or "git rev-parse failed")
    return sha_result.stdout.strip()


def push_branch(branch: str, remote: str) -> None:
    result = subprocess.run(
        ["git", "push", "--set-upstream", remote, branch],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git push failed")


def update_paper_states_for_paths(
    paths: list[str],
    workflow_state: str,
    *,
    commit: str = "",
    pr: str = "",
) -> list[dict[str, Any]]:
    if not GMAIL_READER_DIR.is_dir() or not paths:
        return []
    matched = run_json_tool(
        GMAIL_READER_DIR,
        gmail_reader_command("papers", "--status", "matched", "--limit", "5000"),
    ).get("papers", [])
    by_path = {
        str(item.get("matched_content_path", "")).replace("\\", "/"): item
        for item in matched
        if item.get("matched_content_path")
    }
    updates: list[dict[str, Any]] = []
    for path in paths:
        record = by_path.get(path.replace("\\", "/"))
        if not record:
            continue
        updated = set_paper_workflow_state(
            record["paper_key"],
            workflow_state,
            matched_content_path=path,
            commit=commit,
            pr=pr,
        )
        if updated:
            updates.append(updated)
    return updates


def publish_pull_request(args: argparse.Namespace) -> dict[str, Any]:
    if not shutil.which("git"):
        raise FileNotFoundError("git is not installed")
    if not shutil.which("gh"):
        raise FileNotFoundError("gh CLI is not installed")

    selected_paths = [Path(path).as_posix() for path in (args.paths or [])]
    if not selected_paths:
        selected_paths = article_repo_paths(changed_repo_paths())
    if not selected_paths:
        raise ValueError("No changed article markdown files found in the content repo")

    branch = args.branch or default_branch_name(selected_paths)
    ensure_branch_checked_out(branch)

    commit_message = args.commit_message or default_commit_message(selected_paths)
    commit_sha = stage_and_commit_paths(selected_paths, commit_message, args.include_all)
    commit_updates = update_paper_states_for_paths(
        selected_paths,
        "committed",
        commit=commit_sha,
    )

    push_branch(branch, args.remote)
    pr_title = args.title or commit_message
    pr_body = args.body or "Updated article files:\n" + "\n".join(f"- `{path}`" for path in selected_paths)
    pr = create_pull_request(
        base=args.base,
        head=branch,
        title=pr_title,
        body=pr_body,
        fill=args.fill,
        draft=args.draft,
    )
    pr_updates = update_paper_states_for_paths(
        selected_paths,
        "pr_open",
        commit=commit_sha,
        pr=pr["url"],
    )
    return {
        "branch": branch,
        "base": args.base,
        "remote": args.remote,
        "paths": selected_paths,
        "commit_message": commit_message,
        "commit_sha": commit_sha,
        "pull_request": pr,
        "paper_commit_updates": commit_updates,
        "paper_pr_updates": pr_updates,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agent orchestration helpers for the content repository."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    match_parser = subparsers.add_parser("match", help="Find likely existing article matches for a title.")
    match_parser.add_argument("title", help="Title or topic to match against the content repo.")
    match_parser.add_argument("--abstract", default="", help="Optional abstract text for topic retrieval.")
    match_parser.add_argument("--keywords", default="", help="Optional source keywords for topic retrieval.")
    match_parser.add_argument("--alert-name", default="", help="Optional Scholar alert name for topic retrieval.")
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
    prepare_parser.add_argument(
        "--image-screenshot",
        action="store_true",
        help="Capture a screenshot of the source URL and upload it as the article image.",
    )
    prepare_parser.add_argument(
        "--image-screenshot-url",
        help="Capture a screenshot of a different URL and upload it as the article image.",
    )
    prepare_parser.add_argument(
        "--image-screenshot-output",
        help="Optional local path or directory to keep the captured screenshot.",
    )
    prepare_parser.add_argument(
        "--image-screenshot-full-page",
        action="store_true",
        help="Capture a full-page screenshot when using image screenshot mode.",
    )
    prepare_parser.add_argument(
        "--image-screenshot-annotate",
        action="store_true",
        help="Annotate interactive elements when using image screenshot mode.",
    )
    prepare_parser.add_argument(
        "--image-screenshot-wait-ms",
        type=int,
        default=1500,
        help="Extra wait time before capturing a screenshot for the article image.",
    )
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

    audit_tags_parser = subparsers.add_parser(
        "audit-tags",
        help="Group similar tags to identify normalization and merge candidates.",
    )
    audit_tags_parser.add_argument(
        "--include-all",
        action="store_true",
        help="Include groups even when there is only one observed tag variant.",
    )
    audit_tags_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum groups to return.",
    )

    lint_parser = subparsers.add_parser(
        "lint-frontmatter",
        help="Scan markdown files for invalid or inconsistent frontmatter.",
    )
    lint_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum files with issues to return (0 = all).",
    )

    check_ref_parser = subparsers.add_parser(
        "check-ref",
        help="Check if a URL is already referenced in any article.",
    )
    check_ref_parser.add_argument(
        "url",
        help="URL to check for existing references.",
    )

    duplicate_paper_parser = subparsers.add_parser(
        "check-duplicate-paper",
        help="Check for duplicate paper references in content and the Gmail paper index.",
    )
    duplicate_paper_parser.add_argument(
        "identifier",
        help="URL, DOI, PMID, or title fragment to check.",
    )
    duplicate_paper_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum matching content references to return.",
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
        choices=["selected", "review", "rejected", "invalid", "all"],
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

    archive_parser = subparsers.add_parser(
        "archive-source",
        help="Archive a PDF or HTML source snapshot for long-term provenance.",
    )
    archive_parser.add_argument("source", help="HTTP(S) URL or local file path to archive.")
    archive_parser.add_argument(
        "--identifier",
        help="Optional paper identifier to attach the archive to. Defaults to the source value.",
    )
    archive_parser.add_argument(
        "--archive-root",
        default=str(DEFAULT_ARCHIVE_DIR),
        help="Root directory for archived source material.",
    )

    ingest_parser = subparsers.add_parser(
        "ingest-paper",
        help="Normalize a URL or PDF into a packet with scrape data, matches, and optional archive output.",
    )
    ingest_parser.add_argument("source", help="HTTP(S) URL or local PDF path to ingest.")
    ingest_parser.add_argument("--slug", help="Optional slug for generated packet names.")
    ingest_parser.add_argument("--alert-name", default="", help="Optional Scholar alert name for topic retrieval.")
    ingest_parser.add_argument(
        "--archive",
        action="store_true",
        help="Archive the raw source alongside the ingest packet.",
    )
    ingest_parser.add_argument(
        "--archive-root",
        default=str(DEFAULT_ARCHIVE_DIR),
        help="Root directory for archived source material.",
    )
    ingest_parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for generated ingest packets.",
    )
    ingest_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum content matches to return.",
    )

    intake_parser = subparsers.add_parser(
        "intake",
        help="Scrape, deduplicate, match, and optionally archive a source without modifying content.",
    )
    intake_parser.add_argument("source", help="HTTP(S) URL or local PDF path to intake.")
    intake_parser.add_argument("--slug", help="Optional slug for generated packet names.")
    intake_parser.add_argument("--alert-name", default="", help="Optional Scholar alert name for topic retrieval.")
    intake_parser.add_argument(
        "--archive",
        action="store_true",
        help="Archive the raw source alongside the intake packet.",
    )
    intake_parser.add_argument(
        "--archive-root",
        default=str(DEFAULT_ARCHIVE_DIR),
        help="Root directory for archived source material.",
    )
    intake_parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for generated intake packets.",
    )
    intake_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum duplicate/match candidates to return.",
    )

    local_publish_parser = subparsers.add_parser(
        "local-publish",
        help="Draft and validate a research update with the local llama.cpp model.",
    )
    local_publish_parser.add_argument("source", help="HTTP(S) article URL or PDF path.")
    local_publish_parser.add_argument("--alert-name", default="", help="Optional Scholar alert topic.")
    local_publish_parser.add_argument("--base-url", default="", help="OpenAI-compatible llama.cpp /v1 base URL.")
    local_publish_parser.add_argument("--model", default="", help="Local llama.cpp model identifier.")
    local_publish_parser.add_argument("--base-ref", default="origin/main", help="Git ref for the isolated content worktree.")
    local_publish_parser.add_argument("--limit", type=int, default=5, help="Maximum target candidates.")
    local_publish_parser.add_argument("--max-draft-attempts", type=int, default=3, help="Maximum draft/critic repair passes.")
    local_publish_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Packet, report, and patch directory.")
    local_publish_parser.add_argument(
        "--publish",
        action="store_true",
        help="Commit, push, and open a draft PR after all quality gates pass.",
    )

    enqueue_local_parser = subparsers.add_parser(
        "enqueue-local",
        help="Queue a stored Scholar article for the local publishing worker.",
    )
    enqueue_local_parser.add_argument("identifier", help="Article key or source URL.")
    enqueue_local_parser.add_argument("--max-attempts", type=int, default=3)

    enqueue_local_backlog_parser = subparsers.add_parser(
        "enqueue-local-backlog",
        help="Queue a high-scoring batch from the unprocessed Scholar database.",
    )
    enqueue_local_backlog_parser.add_argument("--status", choices=["selected", "review"], default="selected")
    enqueue_local_backlog_parser.add_argument("--min-score", type=int, default=12)
    enqueue_local_backlog_parser.add_argument("--limit", type=int, default=10)
    enqueue_local_backlog_parser.add_argument("--max-attempts", type=int, default=3)

    local_worker_parser = subparsers.add_parser(
        "local-worker",
        help="Lease and process queued research articles with the local model.",
    )
    local_worker_parser.add_argument("--worker", default="", help="Stable worker identifier.")
    local_worker_parser.add_argument("--lease-seconds", type=int, default=3600)
    local_worker_parser.add_argument("--max-jobs", type=int, default=1)
    local_worker_parser.add_argument("--base-url", default="")
    local_worker_parser.add_argument("--model", default="")
    local_worker_parser.add_argument("--base-ref", default="origin/main")
    local_worker_parser.add_argument("--limit", type=int, default=5)
    local_worker_parser.add_argument("--max-draft-attempts", type=int, default=3)
    local_worker_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR / "local-jobs"))
    local_worker_parser.add_argument(
        "--publish",
        action="store_true",
        help="Open draft PRs for jobs that pass every quality gate.",
    )

    open_pr_parser = subparsers.add_parser(
        "open-pr",
        help="Open a PR from the mounted content repo using the gh CLI.",
    )
    open_pr_parser.add_argument("--base", default="main", help="Base branch for the PR.")
    open_pr_parser.add_argument("--head", help="Head branch for the PR.")
    open_pr_parser.add_argument("--title", help="PR title.")
    open_pr_parser.add_argument("--body", help="PR body text.")
    open_pr_parser.add_argument("--fill", action="store_true", help="Let gh fill the PR title/body from commits.")
    open_pr_parser.add_argument("--draft", action="store_true", help="Create the PR as a draft.")

    publish_pr_parser = subparsers.add_parser(
        "publish-pr",
        help="Create a branch, commit changed article files, push, and open a PR.",
    )
    publish_pr_parser.add_argument("--base", default="main", help="Base branch for the PR.")
    publish_pr_parser.add_argument("--branch", help="Explicit branch name to create or reuse.")
    publish_pr_parser.add_argument("--remote", default="origin", help="Git remote to push to.")
    publish_pr_parser.add_argument("--title", help="Optional PR title override.")
    publish_pr_parser.add_argument("--body", help="Optional PR body override.")
    publish_pr_parser.add_argument(
        "--fill",
        action="store_true",
        help="Let gh fill the PR title/body from commits instead of using generated values.",
    )
    publish_pr_parser.add_argument("--draft", action="store_true", help="Create the PR as a draft.")
    publish_pr_parser.add_argument("--commit-message", help="Optional commit message override.")
    publish_pr_parser.add_argument(
        "--include-all",
        action="store_true",
        help="Stage all repository changes instead of only changed markdown articles.",
    )
    publish_pr_parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=[],
        help="Specific repo-relative article path to include. Repeatable.",
    )

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
        elif args.command == "audit-tags":
            result = audit_tags(args)
        elif args.command == "lint-frontmatter":
            result = lint_frontmatter(args)
        elif args.command == "check-ref":
            result = check_ref(args)
        elif args.command == "check-duplicate-paper":
            result = check_duplicate_paper(args)
        elif args.command == "append":
            if args.commit:
                args.apply = True  # --commit implies --apply
            result = append_research(args)
        elif args.command == "archive-source":
            result = archive_source_command(args)
        elif args.command == "ingest-paper":
            result = ingest_paper(args)
        elif args.command == "intake":
            result = intake_source(args)
        elif args.command == "local-publish":
            result = local_publish_command(args)
        elif args.command == "enqueue-local":
            result = enqueue_local_command(args)
        elif args.command == "enqueue-local-backlog":
            result = enqueue_local_backlog_command(args)
        elif args.command == "local-worker":
            result = local_worker_command(args)
        elif args.command == "open-pr":
            result = open_pull_request(args)
        elif args.command == "publish-pr":
            result = publish_pull_request(args)
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
