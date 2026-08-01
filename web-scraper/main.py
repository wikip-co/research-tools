#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re as _re
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from content_agent_core.agent_browser import (
    agent_browser_env,
    overlay_cleanup_script,
    resolve_agent_browser_binary,
    run_agent_browser_json,
)
from content_agent_core.dotenv import load_dotenv_files
from content_agent_core.http import fetch_json
from markitdown import MarkItDown
from pypdf import PdfReader
from scrapling.fetchers import StealthyFetcher

TOOL_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = TOOL_DIR.parent

load_dotenv_files(
    [
        Path.cwd() / ".env",
        TOOL_DIR / ".env",
        WORKSPACE_ROOT / ".env",
    ]
)

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

AGENT_BROWSER_OVERLAY_SELECTORS = [
    "[class*='cookie']",
    "[id*='cookie']",
    "[class*='consent']",
    "[id*='consent']",
    "[class*='banner']",
    "[id*='banner']",
    "[class*='popup']",
    "[id*='popup']",
    "[aria-modal='true']",
    "[role='dialog']",
]

MIN_BODY_MARKDOWN_CHARS = int(os.getenv("WEB_SCRAPER_MIN_BODY_CHARS", "800"))
MIN_ABSTRACT_CHARS = int(os.getenv("WEB_SCRAPER_MIN_ABSTRACT_CHARS", "120"))
AGENT_BROWSER_MODE_DEFAULT = os.getenv("WEB_SCRAPER_AGENT_BROWSER_MODE", "auto").strip().lower() or "auto"
AGENT_BROWSER_COMMAND = os.getenv("WEB_SCRAPER_AGENT_BROWSER_COMMAND", "agent-browser")
AGENT_BROWSER_SETTLE_MS = int(os.getenv("WEB_SCRAPER_AGENT_BROWSER_SETTLE_MS", "1200"))
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "").strip()
AGENT_BROWSER_FORCE_DOMAINS = tuple(
    pattern.strip().lower()
    for pattern in os.getenv("WEB_SCRAPER_AGENT_BROWSER_DOMAINS", "").split(",")
    if pattern.strip()
)
# FlareSolverr (Cloudflare / DDoS-GUARD bypass proxy). Default matches local docker compose.
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "http://127.0.0.1:8191/v1").strip()
FLARESOLVERR_MODE_DEFAULT = (
    os.getenv("WEB_SCRAPER_FLARESOLVERR_MODE", "auto").strip().lower() or "auto"
)
FLARESOLVERR_MAX_TIMEOUT_MS = int(os.getenv("WEB_SCRAPER_FLARESOLVERR_TIMEOUT_MS", "120000"))

WEAK_CONTENT_PATTERNS = {
    "javascript_required": _re.compile(r"enable\s+javascript|javascript\s+(is\s+)?required", _re.IGNORECASE),
    "bot_challenge": _re.compile(r"verify\s+you\s+are\s+human|checking\s+your\s+browser|cloudflare", _re.IGNORECASE),
    "cookie_wall": _re.compile(r"accept\s+(all\s+)?cookies|cookie\s+preferences", _re.IGNORECASE),
    "access_denied": _re.compile(r"access\s+denied|request\s+blocked|forbidden", _re.IGNORECASE),
}

DOI_PATTERN = _re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+\b", _re.IGNORECASE)
PDF_ABSTRACT_PATTERN = _re.compile(
    r"\babstract\b[\s:.-]*(.+?)(?=\b(?:keywords?|introduction|background|methods?|materials|results?|discussion|conclusion|references)\b)",
    _re.IGNORECASE | _re.DOTALL,
)
PDF_KEYWORDS_PATTERN = _re.compile(
    r"\bkeywords?\b[\s:.-]*(.+?)(?=\n{2,}|\b(?:introduction|background|methods?|materials|results?|discussion|conclusion|references)\b)",
    _re.IGNORECASE | _re.DOTALL,
)

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


def normalize_whitespace(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def normalize_block_whitespace(value: str | None) -> str:
    if not value:
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def first_nonempty(*values: str | None) -> str:
    for value in values:
        cleaned = normalize_whitespace(value)
        if cleaned:
            return cleaned
    return ""


def looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def looks_like_pdf_source(value: str) -> bool:
    if value.lower().endswith(".pdf"):
        return True
    if looks_like_url(value):
        path = urlparse(value).path.lower()
        return path.endswith(".pdf")
    return Path(value).expanduser().suffix.lower() == ".pdf"


def unique_nonempty(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = normalize_whitespace(value)
        if cleaned and cleaned not in seen:
            unique.append(cleaned)
            seen.add(cleaned)
    return unique


def first_item(values: list[str]) -> str:
    return values[0] if values else ""


def meta_values(soup: BeautifulSoup, attr_name: str, attr_value: str) -> list[str]:
    values: list[str] = []
    for tag in soup.find_all("meta", attrs={attr_name: attr_value}):
        content = normalize_whitespace(tag.get("content", ""))
        if content:
            values.append(content)
    return unique_nonempty(values)


def selector_texts(soup: BeautifulSoup, selector: str) -> list[str]:
    values: list[str] = []
    for element in soup.select(selector):
        text = normalize_whitespace(element.get_text(" ", strip=True))
        if text:
            values.append(text)
    return unique_nonempty(values)


def remove_unwanted_nodes(soup: BeautifulSoup) -> BeautifulSoup:
    for selector in UNWANTED_SELECTORS:
        for element in soup.select(selector):
            element.decompose()
    return soup


def markdown_content_score(markdown: str) -> int:
    normalized = normalize_whitespace(markdown)
    if not normalized:
        return 0
    score = len(normalized)
    lowered = normalized.lower()
    for pattern in WEAK_CONTENT_PATTERNS.values():
        if pattern.search(lowered):
            score -= 1000
    return score


def body_candidates(raw_html: str) -> list[tuple[str, str]]:
    soup = remove_unwanted_nodes(BeautifulSoup(raw_html, "html.parser"))
    candidates: list[tuple[str, str]] = []
    seen_html: set[str] = set()

    def add_candidate(label: str, html: str) -> None:
        if html and html not in seen_html:
            candidates.append((label, html))
            seen_html.add(html)

    for selector in CONTENT_SELECTORS:
        main = soup.select_one(selector)
        if main:
            add_candidate(selector, str(main))

    if soup.body:
        add_candidate("body", str(soup.body))
    add_candidate("document", str(soup))
    return candidates


def extract_best_body_markdown(raw_html: str) -> tuple[str, str]:
    best_markdown = ""
    best_label = "document"
    best_score = -1

    for label, fragment in body_candidates(raw_html):
        markdown = html_to_markdown(fragment)
        score = markdown_content_score(markdown)

        if label in CONTENT_SELECTORS and score >= MIN_BODY_MARKDOWN_CHARS:
            return markdown, label

        if score > best_score:
            best_markdown = markdown
            best_label = label
            best_score = score

    return best_markdown, best_label


def match_hostname(hostname: str, pattern: str) -> bool:
    if not hostname or not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return hostname == suffix or hostname.endswith(f".{suffix}")
    return hostname == pattern


def force_agent_browser_for_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return any(match_hostname(hostname, pattern) for pattern in AGENT_BROWSER_FORCE_DOMAINS)


def resolve_agent_browser_mode(cli_mode: str | None) -> str:
    mode = (cli_mode or AGENT_BROWSER_MODE_DEFAULT or "auto").lower()
    if mode not in {"auto", "off", "force"}:
        raise ValueError(f"Unsupported agent-browser mode: {mode}")
    return mode


def resolve_flaresolverr_mode(cli_mode: str | None) -> str:
    mode = (cli_mode or FLARESOLVERR_MODE_DEFAULT or "auto").lower()
    if mode not in {"auto", "off", "force"}:
        raise ValueError(f"Unsupported flaresolverr mode: {mode}")
    return mode


def flaresolverr_configured() -> bool:
    return bool(FLARESOLVERR_URL)


def fetch_with_flaresolverr(url: str) -> tuple[str, str]:
    """Fetch URL HTML via FlareSolverr (solves Cloudflare challenges when possible)."""
    if not FLARESOLVERR_URL:
        raise RuntimeError("FLARESOLVERR_URL is not configured")

    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": FLARESOLVERR_MAX_TIMEOUT_MS,
    }
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        FLARESOLVERR_URL,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    # Allow long CF solves; urllib default can be tight.
    timeout_s = max(30.0, FLARESOLVERR_MAX_TIMEOUT_MS / 1000.0 + 15.0)
    with urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if data.get("status") != "ok":
        raise RuntimeError(
            f"FlareSolverr failed: {data.get('message') or data.get('status') or 'unknown error'}"
        )
    solution = data.get("solution") or {}
    html = solution.get("response") or ""
    final_url = solution.get("url") or url
    if not isinstance(html, str) or not html.strip():
        raise RuntimeError("FlareSolverr returned empty HTML")
    # Still stuck on challenge page?
    low = html.lower()
    if "just a moment" in low and "cloudflare" in low and len(html) < 20000:
        raise RuntimeError("FlareSolverr returned Cloudflare challenge page")
    if not isinstance(final_url, str) or not final_url.strip():
        final_url = url
    return final_url, html


def fetch_with_agent_browser(url: str) -> tuple[str, str]:
    binary = resolve_agent_browser_binary(AGENT_BROWSER_COMMAND)
    if not binary:
        raise RuntimeError("agent-browser is not installed")

    env = agent_browser_env(
        explicit_path_env_var="WEB_SCRAPER_AGENT_BROWSER_EXECUTABLE_PATH"
    )
    session = f"web-scraper-{secrets.token_hex(6)}"

    run_agent_browser_json(binary, session, ["open", url], env=env, timeout=60)
    try:
        run_agent_browser_json(
            binary,
            session,
            ["wait", str(AGENT_BROWSER_SETTLE_MS)],
            env=env,
            timeout=20,
        )
    except RuntimeError:
        pass

    try:
        run_agent_browser_json(
            binary,
            session,
            ["eval", overlay_cleanup_script(AGENT_BROWSER_OVERLAY_SELECTORS)],
            env=env,
            timeout=15,
        )
        run_agent_browser_json(binary, session, ["wait", "300"], env=env, timeout=10)
    except RuntimeError:
        pass

    html_payload = run_agent_browser_json(
        binary,
        session,
        ["eval", "document.documentElement.outerHTML"],
        env=env,
        timeout=60,
    )
    url_payload = run_agent_browser_json(
        binary,
        session,
        ["eval", "location.href"],
        env=env,
        timeout=15,
    )
    html = html_payload.get("result", "")
    final_url = url_payload.get("result", url)
    if not isinstance(html, str) or not html.strip():
        raise RuntimeError("agent-browser returned empty HTML")
    if not isinstance(final_url, str) or not final_url.strip():
        final_url = url
    return final_url, html

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
        body_text.lower(),
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
    candidates = body_candidates(raw_html)
    return candidates[0][1] if candidates else raw_html


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


def extract_first_doi(*values: str) -> str:
    for value in values:
        if not value:
            continue
        match = DOI_PATTERN.search(value)
        if match:
            return match.group(0).rstrip(").,;")
    return ""


def first_nonempty_line(text: str, fallback: str) -> str:
    for line in text.splitlines():
        cleaned = normalize_whitespace(line)
        if len(cleaned) >= 8:
            return cleaned
    return fallback


def extract_pdf_abstract(text: str) -> str:
    match = PDF_ABSTRACT_PATTERN.search(text)
    if not match:
        return ""
    return normalize_whitespace(match.group(1))


def extract_pdf_keywords(text: str) -> str:
    match = PDF_KEYWORDS_PATTERN.search(text)
    if not match:
        return ""
    raw = match.group(1).replace("\n", " ")
    return normalize_whitespace(raw.strip(" .;"))


def download_remote_file(url: str, suffix: str) -> Path:
    request = Request(
        url,
        headers={"User-Agent": "content-agent-tools/0.1"},
    )
    with urlopen(request, timeout=60) as response:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(response.read())
            return Path(handle.name)


def normalize_title_key(value: str) -> str:
    return _re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def title_match_score(left: str, right: str) -> float:
    left_terms = set(normalize_title_key(left).split())
    right_terms = set(normalize_title_key(right).split())
    if not left_terms or not right_terms:
        return 0.0
    if left_terms == right_terms:
        return 1.0
    return len(left_terms & right_terms) / max(len(left_terms), len(right_terms))


def strip_markup(value: str) -> str:
    if not value:
        return ""
    return normalize_whitespace(
        BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    )


def normalize_date_parts(parts: list[list[int]]) -> str:
    if not parts:
        return ""
    first = parts[0]
    if not first:
        return ""
    year = first[0]
    month = first[1] if len(first) > 1 else None
    day = first[2] if len(first) > 2 else None
    if month is None:
        return f"{year:04d}"
    if day is None:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def crossref_summary(work: dict[str, Any]) -> dict[str, Any]:
    authors = ", ".join(
        normalize_whitespace(
            " ".join(
                part
                for part in [author.get("given", ""), author.get("family", "")]
                if part
            )
        )
        for author in work.get("author", [])
        if author.get("given") or author.get("family")
    )
    title_values = work.get("title") or []
    journal_values = work.get("container-title") or []
    return {
        "doi": normalize_whitespace(work.get("DOI", "")),
        "title": first_item(title_values),
        "authors": normalize_whitespace(authors),
        "journal": first_item(journal_values),
        "pub_date": first_nonempty(
            normalize_date_parts(work.get("published-print", {}).get("date-parts", [])),
            normalize_date_parts(work.get("published-online", {}).get("date-parts", [])),
            normalize_date_parts(work.get("issued", {}).get("date-parts", [])),
        ),
        "abstract": strip_markup(work.get("abstract", "")),
        "score": work.get("score", 0),
        "url": normalize_whitespace(work.get("URL", "")),
    }


def fetch_crossref_metadata(doi: str, title: str) -> dict[str, Any]:
    if doi:
        payload = fetch_json(f"https://api.crossref.org/works/{quote(doi, safe='')}")
        message = payload.get("message", {})
        return crossref_summary(message)

    if not title:
        return {}

    payload = fetch_json(
        "https://api.crossref.org/works"
        f"?query.title={quote(title)}&rows=5"
    )
    items = payload.get("message", {}).get("items", [])
    for item in items:
        summary = crossref_summary(item)
        if title_match_score(title, summary.get("title", "")) >= 0.75:
            return summary
    return {}


def fetch_pubmed_metadata(doi: str, title: str, url: str) -> dict[str, Any]:
    pmid = extract_pmid(url)
    if not pmid and doi:
        payload = fetch_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            f"?db=pubmed&retmode=json&term={quote(f'{doi}[doi]')}"
        )
        pmid = first_item(payload.get("esearchresult", {}).get("idlist", []))
    if not pmid and title:
        title_term = quote(f'"{title}"[Title]')
        payload = fetch_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
            f"?db=pubmed&retmode=json&term={title_term}"
        )
        pmid = first_item(payload.get("esearchresult", {}).get("idlist", []))
    if not pmid:
        return {}

    payload = fetch_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        f"?db=pubmed&retmode=json&id={quote(pmid)}"
    )
    summary = payload.get("result", {}).get(pmid, {})
    article_ids = summary.get("articleids", [])
    normalized_doi = ""
    for article_id in article_ids:
        if article_id.get("idtype") == "doi" and article_id.get("value"):
            normalized_doi = normalize_whitespace(article_id["value"])
            break
    authors = ", ".join(
        normalize_whitespace(author.get("name", ""))
        for author in summary.get("authors", [])
        if author.get("name")
    )
    return {
        "pmid": pmid,
        "doi": normalized_doi,
        "title": normalize_whitespace(summary.get("title", "")),
        "authors": normalize_whitespace(authors),
        "journal": normalize_whitespace(summary.get("fulljournalname", "")),
        "pub_date": normalize_whitespace(
            summary.get("sortpubdate") or summary.get("pubdate", "")
        )[:10],
    }


def fetch_unpaywall_metadata(doi: str) -> dict[str, Any]:
    if not doi or not UNPAYWALL_EMAIL:
        return {}
    payload = fetch_json(
        "https://api.unpaywall.org/v2/"
        f"{quote(doi, safe='')}?email={quote(UNPAYWALL_EMAIL)}"
    )
    best_location = payload.get("best_oa_location") or {}
    return {
        "is_open_access": bool(payload.get("is_oa")),
        "best_open_access_url": normalize_whitespace(
            best_location.get("url_for_pdf") or best_location.get("url", "")
        ),
        "oa_status": normalize_whitespace(payload.get("oa_status", "")),
    }


def enrich_metadata(data: dict[str, Any]) -> dict[str, Any]:
    external_metadata: dict[str, Any] = {}
    enrichment_sources: list[str] = []
    enrichment_errors: list[str] = []

    crossref_data: dict[str, Any] = {}
    try:
        crossref_data = fetch_crossref_metadata(
            normalize_whitespace(str(data.get("doi", ""))),
            normalize_whitespace(str(data.get("title", ""))),
        )
    except Exception as exc:
        enrichment_errors.append(f"crossref:{exc}")
    if crossref_data:
        external_metadata["crossref"] = crossref_data
        enrichment_sources.append("crossref")

    pubmed_data: dict[str, Any] = {}
    try:
        pubmed_data = fetch_pubmed_metadata(
            normalize_whitespace(str(data.get("doi", ""))) or crossref_data.get("doi", ""),
            normalize_whitespace(str(data.get("title", ""))),
            normalize_whitespace(str(data.get("url", ""))),
        )
    except Exception as exc:
        enrichment_errors.append(f"pubmed:{exc}")
    if pubmed_data:
        external_metadata["pubmed"] = pubmed_data
        enrichment_sources.append("pubmed")

    unpaywall_data: dict[str, Any] = {}
    try:
        unpaywall_data = fetch_unpaywall_metadata(
            normalize_whitespace(str(data.get("doi", "")))
            or crossref_data.get("doi", "")
            or pubmed_data.get("doi", "")
        )
    except Exception as exc:
        enrichment_errors.append(f"unpaywall:{exc}")
    if unpaywall_data:
        external_metadata["unpaywall"] = unpaywall_data
        enrichment_sources.append("unpaywall")

    def fill_if_missing(key: str, value: str) -> None:
        if value and not normalize_whitespace(str(data.get(key, ""))):
            data[key] = value

    fill_if_missing("doi", crossref_data.get("doi", ""))
    fill_if_missing("doi", pubmed_data.get("doi", ""))
    fill_if_missing("title", crossref_data.get("title", ""))
    fill_if_missing("title", pubmed_data.get("title", ""))
    fill_if_missing("authors", crossref_data.get("authors", ""))
    fill_if_missing("authors", pubmed_data.get("authors", ""))
    fill_if_missing("journal", crossref_data.get("journal", ""))
    fill_if_missing("journal", pubmed_data.get("journal", ""))
    fill_if_missing("pub_date", crossref_data.get("pub_date", ""))
    fill_if_missing("pub_date", pubmed_data.get("pub_date", ""))
    fill_if_missing("abstract", crossref_data.get("abstract", ""))

    data["pmid"] = normalize_whitespace(
        str(data.get("pmid", "")) or pubmed_data.get("pmid", "")
    )
    data["is_open_access"] = bool(
        data.get("is_open_access")
        or unpaywall_data.get("is_open_access")
        or looks_like_url(str(data.get("url", "")))
        and "pmc.ncbi.nlm.nih.gov" in str(data.get("url", ""))
    )
    if unpaywall_data.get("best_open_access_url"):
        data["best_open_access_url"] = unpaywall_data["best_open_access_url"]
    if enrichment_sources:
        data["enrichment_sources"] = enrichment_sources
        data["external_metadata"] = external_metadata
    if enrichment_errors:
        data["enrichment_errors"] = enrichment_errors
    return data


def scrape_pdf_source(source: str) -> dict[str, Any]:
    temporary_path: Path | None = None
    local_path = Path(source).expanduser()
    requested_source = source

    if looks_like_url(source):
        temporary_path = download_remote_file(source, ".pdf")
        local_path = temporary_path

    if not local_path.is_file():
        raise FileNotFoundError(f"PDF source not found: {source}")

    try:
        reader = PdfReader(str(local_path))
        page_text = [
            normalize_block_whitespace(page.extract_text() or "")
            for page in reader.pages
        ]
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    body_markdown = "\n\n".join(chunk for chunk in page_text if chunk)
    preview_text = "\n\n".join(chunk for chunk in page_text[:3] if chunk)
    metadata = reader.metadata or {}

    doi = extract_first_doi(
        str(metadata.get("/Subject", "")),
        str(metadata.get("/Title", "")),
        preview_text,
        requested_source,
    )
    public_url = requested_source if looks_like_url(requested_source) else ""
    if not public_url and doi:
        public_url = doi if doi.startswith("http") else f"https://doi.org/{doi}"

    title = first_nonempty(
        normalize_whitespace(str(metadata.get("/Title", ""))),
        first_nonempty_line(preview_text, local_path.stem),
        local_path.stem,
    )
    authors = normalize_whitespace(str(metadata.get("/Author", "")))
    abstract = extract_pdf_abstract(preview_text)
    keywords = extract_pdf_keywords(preview_text)
    publication_types: list[str] = []
    mesh_terms: list[str] = []
    study_type = detect_study_type(
        title=title,
        abstract=abstract,
        publication_types=publication_types,
        mesh_terms=mesh_terms,
        body_text=body_markdown[:5000],
    )

    data: dict[str, Any] = {
        "url": public_url or requested_source,
        "requested_url": requested_source,
        "title": title.strip(),
        "authors": authors.strip(),
        "abstract": abstract.strip(),
        "body_markdown": body_markdown,
        "body_selector": "pdf-text",
        "doi": doi.strip(),
        "pmid": "",
        "journal": "",
        "pub_date": "",
        "keywords": keywords.strip(),
        "study_type": study_type,
        "publication_types": publication_types,
        "mesh_terms": mesh_terms,
        "retrieval_backend": "pypdf",
        "source_kind": "pdf",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
    }
    enrich_metadata(data)
    data["reference_url"] = citation_url(data)
    data["footnote_markdown"] = footnote_markdown(data)
    data["retrieval_issues"] = extraction_issues(data)
    return data


def extract_article_data(url: str, raw_html: str, retrieval_backend: str) -> dict[str, Any]:
    soup = BeautifulSoup(raw_html, "html.parser")

    title = first_nonempty(
        first_item(meta_values(soup, "name", "citation_title")),
        first_item(meta_values(soup, "property", "og:title")),
        first_item(selector_texts(soup, "h1")),
        soup.title.get_text(" ", strip=True) if soup.title else "",
        "Unknown Title",
    )

    author_tags = meta_values(soup, "name", "citation_author")
    if author_tags:
        authors = ", ".join(author_tags)
    else:
        author_candidates = selector_texts(
            soup,
            '[class*="author"] [class*="name"], [class*="author"], [rel="author"]',
        )
        authors = ", ".join(author_candidates)

    doi = first_nonempty(
        first_item(meta_values(soup, "name", "citation_doi")),
        first_item(selector_texts(soup, '[class*="doi"]')),
    )
    journal = first_nonempty(
        first_item(meta_values(soup, "name", "citation_journal_title")),
        first_item(meta_values(soup, "name", "citation_publisher")),
        first_item(meta_values(soup, "property", "og:site_name")),
    )
    time_values = unique_nonempty(
        [tag.get("datetime", "") for tag in soup.find_all("time") if tag.get("datetime")]
    )
    pub_date = first_nonempty(
        first_item(meta_values(soup, "name", "citation_publication_date")),
        first_item(meta_values(soup, "name", "citation_date")),
        first_item(meta_values(soup, "property", "article:published_time")),
        first_item(time_values),
    )
    keyword_tags = meta_values(soup, "name", "citation_keywords") or meta_values(soup, "name", "keywords")
    keywords = ", ".join(keyword_tags)
    abstract = first_nonempty(
        first_item(meta_values(soup, "name", "citation_abstract")),
        first_item(meta_values(soup, "name", "description")),
        first_item(meta_values(soup, "property", "og:description")),
        first_item(selector_texts(soup, ".abstract, [class*='abstract']")),
    )
    if abstract:
        abstract = BeautifulSoup(abstract, "html.parser").get_text(" ", strip=True)
        abstract = normalize_whitespace(abstract)

    publication_types = selector_texts(soup, '[data-ga-label="publication_types"] a')
    if not publication_types:
        publication_types = meta_values(soup, "property", "og:type")

    mesh_terms = selector_texts(soup, '[data-ga-label="mesh_terms"] a')
    body_markdown, body_selector = extract_best_body_markdown(raw_html)

    study_type = detect_study_type(
        title=title,
        abstract=abstract,
        publication_types=publication_types,
        mesh_terms=mesh_terms,
        body_text=body_markdown[:5000],
    )

    data: dict[str, Any] = {
        "url": url,
        "requested_url": url,
        "title": title.strip(),
        "authors": authors.strip(),
        "abstract": abstract.strip(),
        "body_markdown": body_markdown,
        "body_selector": body_selector,
        "doi": doi.strip(),
        "pmid": "",
        "journal": journal.strip(),
        "pub_date": pub_date.strip(),
        "keywords": keywords.strip(),
        "study_type": study_type,
        "publication_types": publication_types,
        "mesh_terms": mesh_terms,
        "retrieval_backend": retrieval_backend,
        "source_kind": "html",
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
    }
    enrich_metadata(data)
    data["reference_url"] = citation_url(data)
    data["footnote_markdown"] = footnote_markdown(data)
    return data


def extraction_issues(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    title = normalize_whitespace(str(data.get("title", "")))
    abstract = normalize_whitespace(str(data.get("abstract", "")))
    body = normalize_whitespace(str(data.get("body_markdown", "")))

    if title == "Unknown Title":
        issues.append("title_missing")
    if not body:
        issues.append("body_missing")

    has_useful_abstract = len(abstract) >= MIN_ABSTRACT_CHARS
    has_useful_body = len(body) >= MIN_BODY_MARKDOWN_CHARS
    if not has_useful_body and not has_useful_abstract:
        issues.append("content_too_short")

    if not has_useful_body:
        combined = f"{title} {abstract} {body}".lower()
        for issue_name, pattern in WEAK_CONTENT_PATTERNS.items():
            if pattern.search(combined):
                issues.append(issue_name)

    return unique_nonempty(issues)


def extraction_score(data: dict[str, Any]) -> int:
    title = normalize_whitespace(str(data.get("title", "")))
    abstract = normalize_whitespace(str(data.get("abstract", "")))
    body = normalize_whitespace(str(data.get("body_markdown", "")))
    score = 0
    if title and title != "Unknown Title":
        score += 200
    score += min(len(abstract), 1500)
    score += min(len(body), 5000)
    for key in ("doi", "journal", "authors", "pub_date"):
        if normalize_whitespace(str(data.get(key, ""))):
            score += 50
    score -= len(extraction_issues(data)) * 250
    return score


def citation_url(data: dict[str, str]) -> str:
    doi = data.get("doi", "").strip()
    if doi:
        return doi if doi.startswith("http") else f"https://doi.org/{doi}"
    return data.get("url", "").strip()


def footnote_markdown(data: dict[str, str]) -> str:
    title_link = citation_url(data)
    publication_name = data["journal"] or "Source"
    source_url = data.get("url", "").strip()
    if source_url.startswith("http://") or source_url.startswith("https://"):
        publication_line = f"**Publication:** [{publication_name}]({source_url})<br>"
        source_line = f"**Source URL:** [{source_url}]({source_url})"
    else:
        publication_line = f"**Publication:** {publication_name}<br>"
        source_line = f"**Source:** {source_url or data.get('requested_url', 'Unknown')}"
    study_type_line = f"**Study Type:** {data.get('study_type', 'Research Article')}<br>"
    return "\n".join(
        [
            f"[^1]: **Title:** [{data['title']}]({title_link})<br>",
            publication_line,
            f"**Date:** {data['pub_date'] or 'Unknown'}<br>",
            study_type_line,
            f"**Author(s):** {data['authors'] or 'Unknown'}<br>",
            source_line,
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


def scrape_with_scrapling(url: str) -> dict[str, Any]:
    page = StealthyFetcher.fetch(
        url,
        headless=True,
        network_idle=True,
        disable_resources=False,
    )
    if not page or not page.html_content:
        raise RuntimeError("Failed to fetch: empty response")
    return extract_article_data(url, page.html_content, retrieval_backend="scrapling")


def _primary_needs_fallback(primary_data: dict[str, Any] | None, primary_error: Exception | None) -> bool:
    if primary_error is not None:
        return True
    if primary_data is None:
        return True
    return bool(primary_data.get("retrieval_issues"))


def _fallback_trigger_reasons(
    primary_data: dict[str, Any] | None,
    primary_error: Exception | None,
    forced: bool,
) -> list[str]:
    if forced:
        return ["forced"]
    if primary_error is not None:
        return [f"primary_fetch_failed: {primary_error}"]
    if primary_data is not None:
        return list(primary_data.get("retrieval_issues", [])) or ["quality_check"]
    return ["no_primary_data"]


def scrape_article(
    source: str,
    agent_browser_mode: str | None = None,
    flaresolverr_mode: str | None = None,
) -> dict[str, Any]:
    if looks_like_pdf_source(source):
        return scrape_pdf_source(source)

    if not looks_like_url(source):
        raise ValueError("source must be an HTTP(S) URL or a local PDF path")

    ab_mode = resolve_agent_browser_mode(agent_browser_mode)
    fs_mode = resolve_flaresolverr_mode(flaresolverr_mode)
    force_ab = ab_mode == "force" or force_agent_browser_for_url(source)
    force_fs = fs_mode == "force"
    force_any = force_ab or force_fs
    binary = resolve_agent_browser_binary(AGENT_BROWSER_COMMAND)

    primary_data: dict[str, Any] | None = None
    primary_error: Exception | None = None
    best: dict[str, Any] | None = None
    errors: list[str] = []

    if not force_any:
        try:
            primary_data = scrape_with_scrapling(source)
            primary_data["retrieval_issues"] = extraction_issues(primary_data)
            best = primary_data
        except Exception as exc:
            primary_error = exc
            errors.append(f"scrapling: {exc}")

    needs_fallback = force_any or _primary_needs_fallback(primary_data, primary_error)
    trigger_reasons = _fallback_trigger_reasons(primary_data, primary_error, forced=force_any)

    # Prefer FlareSolverr for Cloudflare / weak primary content (proven on Sage etc.).
    should_try_fs = (
        fs_mode != "off"
        and flaresolverr_configured()
        and (
            force_fs
            or needs_fallback
        )
    )
    if should_try_fs:
        try:
            final_url, html = fetch_with_flaresolverr(source)
            fs_data = extract_article_data(final_url, html, retrieval_backend="flaresolverr")
            fs_data["requested_url"] = source
            fs_data["retrieval_issues"] = extraction_issues(fs_data)
            fs_data["fallback_used"] = True
            fs_data["fallback_trigger"] = trigger_reasons
            if best is None or extraction_score(fs_data) >= extraction_score(best):
                best = fs_data
            elif primary_data is not None:
                primary_data["flaresolverr_attempted"] = True
                primary_data["flaresolverr_kept_primary"] = True
        except Exception as fs_exc:
            errors.append(f"flaresolverr: {fs_exc}")
            if primary_data is not None:
                primary_data["flaresolverr_error"] = str(fs_exc)

    # Still weak? Try agent-browser next.
    still_weak = best is None or bool(best.get("retrieval_issues")) or force_ab
    should_try_ab = ab_mode != "off" and binary and still_weak
    if should_try_ab:
        try:
            final_url, rendered_html = fetch_with_agent_browser(source)
            ab_data = extract_article_data(final_url, rendered_html, retrieval_backend="agent-browser")
            ab_data["requested_url"] = source
            ab_data["retrieval_issues"] = extraction_issues(ab_data)
            ab_data["fallback_used"] = True
            ab_data["fallback_trigger"] = trigger_reasons
            if best is None or extraction_score(ab_data) >= extraction_score(best):
                best = ab_data
            elif best is not None:
                best["agent_browser_attempted"] = True
                best["agent_browser_kept_other"] = True
        except Exception as ab_exc:
            errors.append(f"agent-browser: {ab_exc}")
            if best is not None:
                best["agent_browser_error"] = str(ab_exc)

    if best is not None:
        if errors:
            best.setdefault("retrieval_errors", errors)
        return best

    if primary_error is not None:
        detail = "; ".join(errors) if errors else str(primary_error)
        raise RuntimeError(f"All retrieval paths failed: {detail}") from primary_error

    raise RuntimeError(
        "No retrieval path produced data"
        + (f" ({'; '.join(errors)})" if errors else "")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape an article URL or PDF and return structured markdown-ready data."
    )
    parser.add_argument("source", help="Article URL to scrape, or a local/remote PDF source.")
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
    parser.add_argument(
        "--agent-browser-mode",
        choices=["auto", "off", "force"],
        help="Fallback mode for agent-browser: auto, off, or force.",
    )
    parser.add_argument(
        "--flaresolverr-mode",
        choices=["auto", "off", "force"],
        help="Cloudflare bypass via FlareSolverr: auto, off, or force.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output_path = args.output or args.legacy_output

    try:
        data = scrape_article(
            args.source,
            agent_browser_mode=args.agent_browser_mode,
            flaresolverr_mode=args.flaresolverr_mode,
        )
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
