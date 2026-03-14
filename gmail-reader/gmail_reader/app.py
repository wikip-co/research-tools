from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "scholar-alerts.db"
DEFAULT_QUERY = (
    "from:scholaralerts-noreply@google.com OR "
    "from:scholar-alerts.bounces.google.com"
)

POSITIVE_TERMS = {
    "review": 3,
    "systematic review": 4,
    "meta-analysis": 4,
    "randomized": 2,
    "clinical": 2,
    "human": 2,
    "patients": 2,
    "therapy": 2,
    "treatment": 2,
    "disease": 2,
    "inflammation": 2,
    "oxidative stress": 2,
    "neuro": 2,
    "cancer": 2,
    "metabolic": 1,
    "cardio": 1,
    "immune": 1,
    "microbiome": 1,
    "gut": 1,
    "brain": 1,
    "healing": 2,
    "health": 1,
}

NEGATIVE_TERMS = {
    "feed": -4,
    "broiler": -4,
    "poultry": -4,
    "cat food": -5,
    "dog food": -5,
    "pet food": -5,
    "aquaculture": -4,
    "fertilizer": -4,
    "crop": -3,
    "spinach": -3,
    "mustard plant": -4,
    "aphid": -5,
    "soil": -3,
    "agriculture": -3,
    "field trial": -2,
    "fish": -3,
    "livestock": -4,
}

OPEN_ACCESS_DOMAINS = {
    "frontiersin.org",
    "mdpi.com",
    "pmc.ncbi.nlm.nih.gov",
    "pubmed.ncbi.nlm.nih.gov",
}


def is_open_access_url(url: str) -> bool:
    if not url:
        return False
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return False
    hostname = hostname.removeprefix("www.")
    return any(hostname == domain or hostname.endswith("." + domain) for domain in OPEN_ACCESS_DOMAINS)


def _url_matches_source(url: str, source: str) -> bool:
    """Check whether a URL's hostname contains the given source keyword."""
    if not url or not source:
        return True
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return False
    return source.lower() in hostname.lower()


@dataclass
class ArticleCandidate:
    alert_name: str
    rank_in_email: int
    title: str
    authors: str
    publication_info: str
    snippet: str
    scholar_url: str
    article_url: str
    pdf_url: str | None
    format_label: str | None
    author_count: int | None
    score: int
    status: str
    reasons: list[str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse Google Scholar alert emails from Gmail into SQLite."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="Path to the SQLite database file.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Ingest Scholar alert emails.")
    sync_parser.add_argument(
        "--days-back",
        type=int,
        default=30,
        help="Look back this many days unless --after is provided.",
    )
    sync_parser.add_argument(
        "--after",
        help="Inclusive lower bound in YYYY-MM-DD format.",
    )
    sync_parser.add_argument(
        "--before",
        help="Exclusive upper bound in YYYY-MM-DD format.",
    )
    sync_parser.add_argument(
        "--max-messages",
        type=int,
        help="Maximum number of Gmail messages to ingest.",
    )
    sync_parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="Base Gmail search query. Date filters are appended automatically.",
    )

    subparsers.add_parser("alerts", help="List alert names in the database.")

    articles_parser = subparsers.add_parser("articles", help="List stored articles.")
    articles_parser.add_argument(
        "--status",
        choices=["selected", "review", "rejected", "all"],
        default="selected",
        help="Filter articles by triage status.",
    )
    articles_parser.add_argument(
        "--alert-name",
        help="Only return rows for this alert name.",
    )
    articles_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of rows to return.",
    )

    curate_parser = subparsers.add_parser(
        "curate",
        help="Search a recent Gmail window and return parsed Scholar candidates for agent review.",
    )
    curate_parser.add_argument(
        "--topic",
        required=True,
        help="Topic or phrase to match against parsed alert results.",
    )
    curate_parser.add_argument(
        "--days-back",
        type=int,
        default=1,
        help="Look back this many days unless --after is provided.",
    )
    curate_parser.add_argument(
        "--after",
        help="Inclusive lower bound in YYYY-MM-DD format.",
    )
    curate_parser.add_argument(
        "--before",
        help="Exclusive upper bound in YYYY-MM-DD format.",
    )
    curate_parser.add_argument(
        "--max-messages",
        type=int,
        default=25,
        help="Maximum number of Gmail messages to inspect.",
    )
    curate_parser.add_argument(
        "--max-results",
        type=int,
        default=10,
        help="Maximum number of parsed article candidates to return.",
    )
    curate_parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="Base Gmail search query. Date filters are appended automatically.",
    )

    search_parser = subparsers.add_parser(
        "search",
        help="Run an arbitrary Scholar-focused Gmail search and return parsed candidates for agent use.",
    )
    search_parser.add_argument(
        "--gmail-query",
        default="",
        help="Additional Gmail search terms appended to the base Scholar query.",
    )
    search_parser.add_argument(
        "--topic",
        help="Optional topic or phrase used to rank parsed article candidates.",
    )
    search_parser.add_argument(
        "--days-back",
        type=int,
        default=1,
        help="Look back this many days unless --after is provided.",
    )
    search_parser.add_argument(
        "--after",
        help="Inclusive lower bound in YYYY-MM-DD format.",
    )
    search_parser.add_argument(
        "--before",
        help="Exclusive upper bound in YYYY-MM-DD format.",
    )
    search_parser.add_argument(
        "--max-messages",
        type=int,
        default=25,
        help="Maximum number of Gmail messages to inspect.",
    )
    search_parser.add_argument(
        "--max-results",
        type=int,
        default=10,
        help="Maximum number of parsed article candidates to return.",
    )
    search_parser.add_argument(
        "--include-review",
        action="store_true",
        help="Include items marked review when no topic is provided.",
    )
    search_parser.add_argument(
        "--save",
        action="store_true",
        help="Persist parsed messages and articles into SQLite while searching.",
    )
    search_parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="Base Gmail search query. Date filters are appended automatically.",
    )

    backlog_parser = subparsers.add_parser(
        "backlog",
        help="Query the DB for unprocessed candidate articles from the backlog.",
    )
    backlog_parser.add_argument(
        "--status",
        choices=["selected", "review", "rejected", "all"],
        default="selected",
        help="Filter articles by triage status (default: selected).",
    )
    backlog_parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Minimum score threshold (e.g. --min-score 18).",
    )
    backlog_parser.add_argument(
        "--source",
        help="Domain keyword filter (e.g. 'frontiersin', 'mdpi'). Implies open-access filtering.",
    )
    backlog_parser.add_argument(
        "--open-access",
        action="store_true",
        help="Only return open-access articles (frontiersin, mdpi, pmc, pubmed).",
    )
    backlog_parser.add_argument(
        "--include-processed",
        action="store_true",
        help="Include articles already marked as processed.",
    )
    backlog_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of rows to return.",
    )

    mark_processed_parser = subparsers.add_parser(
        "mark-processed",
        help="Set processed_at on an article row by URL.",
    )
    mark_processed_parser.add_argument(
        "article_url",
        help="The article_url to mark as processed.",
    )

    return parser


def ensure_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            alert_name TEXT,
            subject TEXT,
            from_address TEXT,
            sent_at TEXT,
            snippet TEXT,
            raw_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            article_key TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            alert_name TEXT NOT NULL,
            rank_in_email INTEGER NOT NULL,
            title TEXT NOT NULL,
            authors TEXT,
            publication_info TEXT,
            snippet TEXT,
            scholar_url TEXT,
            article_url TEXT,
            pdf_url TEXT,
            format_label TEXT,
            author_count INTEGER,
            score INTEGER NOT NULL,
            status TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_open_access INTEGER NOT NULL DEFAULT 0,
            processed_at TEXT,
            FOREIGN KEY (message_id) REFERENCES messages(message_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status, alert_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_message_id ON articles(message_id)"
    )
    # Migrations for existing databases
    for migration in (
        "ALTER TABLE articles ADD COLUMN is_open_access INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE articles ADD COLUMN processed_at TEXT",
    ):
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def run_gws(*args: str, params: dict[str, Any]) -> dict[str, Any]:
    cmd = ["gws", *args, "--params", json.dumps(params, separators=(",", ":"))]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "gws command failed"
        raise RuntimeError(stderr)
    return json.loads(result.stdout)


def gmail_query(base_query: str, after: str | None, before: str | None) -> str:
    parts = [f"({base_query})"]
    if after:
        parts.append(f"after:{after.replace('-', '/')}")
    if before:
        parts.append(f"before:{before.replace('-', '/')}")
    return " ".join(parts)


def combined_gmail_query(
    base_query: str, extra_query: str, after: str | None, before: str | None
) -> str:
    root = base_query.strip()
    if extra_query.strip():
        root = f"({root}) ({extra_query.strip()})"
    return gmail_query(root, after, before)


def parse_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def compute_window(days_back: int, after: str | None, before: str | None) -> tuple[str, str | None]:
    effective_after = parse_date(after) if after else (date.today() - timedelta(days=days_back)).isoformat()
    effective_before = parse_date(before) if before else None
    return effective_after, effective_before


def list_message_ids(query: str, max_messages: int | None) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    page_token: str | None = None

    while True:
        remaining = max_messages - len(messages) if max_messages is not None else None
        if remaining is not None and remaining <= 0:
            break

        params: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": min(100, remaining) if remaining is not None else 100,
        }
        if page_token:
            params["pageToken"] = page_token

        payload = run_gws("gmail", "users", "messages", "list", params=params)
        messages.extend(payload.get("messages", []))
        page_token = payload.get("nextPageToken")

        if not page_token:
            break

    return messages[:max_messages] if max_messages is not None else messages


def get_message(message_id: str) -> dict[str, Any]:
    return run_gws(
        "gmail",
        "users",
        "messages",
        "get",
        params={"userId": "me", "id": message_id, "format": "full"},
    )


def header_map(payload: dict[str, Any]) -> dict[str, str]:
    headers = payload.get("headers", [])
    return {item.get("name", "").lower(): item.get("value", "") for item in headers}


def decode_body_data(data: str) -> str:
    decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return decoded.decode("utf-8", errors="replace")


def extract_html(payload: dict[str, Any]) -> str:
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")

    if mime_type == "text/html" and data:
        return decode_body_data(data)

    for part in payload.get("parts", []) or []:
        html = extract_html(part)
        if html:
            return html

    if mime_type == "text/plain" and data:
        return decode_body_data(data)

    return ""


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def unwrap_google_redirect(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.netloc.endswith("scholar.google.com") and parsed.path == "/scholar_url":
        target = parse_qs(parsed.query).get("url", [""])[0]
        return unquote(target) if target else url
    return url


def extract_alert_name(soup: BeautifulSoup) -> str:
    footer_link = soup.find("a", href=re.compile(r"/scholar\?q="))
    if footer_link:
        text = normalize_space(footer_link.get_text(" ", strip=True))
        return text.strip("[]")

    subject = soup.title.get_text(strip=True) if soup.title else ""
    subject = subject.replace("- new results", "").strip()
    return subject or "Unknown Alert"


def count_authors(authors: str) -> int | None:
    if not authors:
        return None
    cleaned = authors.replace("…", "")
    if "," in cleaned:
        return len([part for part in cleaned.split(",") if part.strip()])
    if " and " in cleaned.lower():
        return len([part for part in re.split(r"\band\b", cleaned, flags=re.IGNORECASE) if part.strip()])
    return 1


def score_candidate(text: str, author_count: int | None) -> tuple[int, list[str]]:
    lowered = text.lower()
    score = 0
    reasons: list[str] = []

    for term, weight in POSITIVE_TERMS.items():
        if term in lowered:
            score += weight
            reasons.append(f"+{weight}:{term}")

    for term, weight in NEGATIVE_TERMS.items():
        if term in lowered:
            score += weight
            reasons.append(f"{weight}:{term}")

    if author_count is not None:
        if author_count >= 4:
            score += 2
            reasons.append("+2:multi-author")
        elif author_count >= 2:
            score += 1
            reasons.append("+1:team-authored")
        elif author_count == 1:
            score -= 2
            reasons.append("-2:single-author")

    return score, reasons


def classify_candidate(score: int, reasons: list[str]) -> str:
    if score >= 6:
        return "selected"
    if score <= -3:
        return "rejected"
    return "review"


def parse_articles_from_html(html: str, subject: str) -> tuple[str, list[ArticleCandidate]]:
    soup = BeautifulSoup(html, "html.parser")
    alert_name = extract_alert_name(soup)
    candidates: list[ArticleCandidate] = []

    for heading in soup.find_all("h3"):
        link = heading.find("a", class_="gse_alrt_title")
        if link is None:
            continue

        href = link.get("href", "")
        if "scholar_alerts?view_op=" in href:
            continue

        title = normalize_space(link.get_text(" ", strip=True))
        if not title:
            continue

        format_label = None
        prefix = heading.find("span")
        if prefix:
            format_text = normalize_space(prefix.get_text(" ", strip=True))
            if format_text.startswith("[") and format_text.endswith("]"):
                format_label = format_text.strip("[]")

        meta_node = heading.find_next_sibling("div")
        snippet_node = meta_node.find_next_sibling("div") if meta_node else None

        publication_info = normalize_space(meta_node.get_text(" ", strip=True)) if meta_node else ""
        snippet = normalize_space(snippet_node.get_text(" ", strip=True)) if snippet_node else ""
        scholar_url = href
        article_url = unwrap_google_redirect(href)
        author_count = count_authors(publication_info.split(" - ", 1)[0])
        combined_text = " ".join(
            part for part in [alert_name, title, publication_info, snippet] if part
        )
        score, reasons = score_candidate(combined_text, author_count)
        status = classify_candidate(score, reasons)

        pdf_url = None
        for sibling in heading.next_siblings:
            if getattr(sibling, "name", None) == "h3":
                break
            if getattr(sibling, "name", None) == "div":
                for anchor in sibling.find_all("a", href=True):
                    parsed_href = anchor.get("href", "")
                    direct_url = unwrap_google_redirect(parsed_href)
                    if direct_url.lower().endswith(".pdf"):
                        pdf_url = direct_url
                        break
            if pdf_url:
                break

        candidates.append(
            ArticleCandidate(
                alert_name=alert_name or subject.replace("- new results", "").strip(),
                rank_in_email=len(candidates) + 1,
                title=title,
                authors=publication_info.split(" - ", 1)[0].strip() if publication_info else "",
                publication_info=publication_info,
                snippet=snippet,
                scholar_url=scholar_url,
                article_url=article_url,
                pdf_url=pdf_url,
                format_label=format_label,
                author_count=author_count,
                score=score,
                status=status,
                reasons=reasons,
            )
        )

    return alert_name, candidates


def article_key(alert_name: str, title: str, article_url: str) -> str:
    normalized_title = normalize_space(re.sub(r"[^a-z0-9]+", " ", title.lower()))
    normalized_alert = normalize_space(alert_name.lower())
    normalized_url = article_url.strip().lower()
    digest = hashlib.sha256(
        f"{normalized_alert}|{normalized_title}|{normalized_url}".encode("utf-8")
    ).hexdigest()
    return digest


def keyword_hits(topic: str, candidate: ArticleCandidate) -> tuple[int, list[str]]:
    hits = 0
    matched_fields: list[str] = []
    terms = [term for term in re.split(r"\s+", topic.lower()) if term]
    haystacks = {
        "alert_name": candidate.alert_name.lower(),
        "title": candidate.title.lower(),
        "authors": candidate.authors.lower(),
        "publication_info": candidate.publication_info.lower(),
        "snippet": candidate.snippet.lower(),
    }

    for field, text in haystacks.items():
        if topic.lower() in text:
            hits += 3
            matched_fields.append(field)
            continue
        term_matches = sum(1 for term in terms if term in text)
        if term_matches:
            hits += term_matches
            matched_fields.append(field)

    return hits, matched_fields


def upsert_message(conn: sqlite3.Connection, message: dict[str, Any], alert_name: str) -> None:
    payload = message.get("payload", {})
    headers = header_map(payload)
    conn.execute(
        """
        INSERT INTO messages (
            message_id, thread_id, alert_name, subject, from_address, sent_at,
            snippet, raw_json, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            alert_name = excluded.alert_name,
            subject = excluded.subject,
            from_address = excluded.from_address,
            sent_at = excluded.sent_at,
            snippet = excluded.snippet,
            raw_json = excluded.raw_json,
            imported_at = excluded.imported_at
        """,
        (
            message["id"],
            message["threadId"],
            alert_name,
            headers.get("subject"),
            headers.get("from"),
            headers.get("date"),
            message.get("snippet"),
            json.dumps(message, separators=(",", ":")),
            datetime.utcnow().isoformat(timespec="seconds") + "Z",
        ),
    )


def upsert_articles(
    conn: sqlite3.Connection, message_id: str, candidates: list[ArticleCandidate]
) -> tuple[int, int, int]:
    inserted = 0
    selected = 0
    review = 0

    for candidate in candidates:
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        key = article_key(candidate.alert_name, candidate.title, candidate.article_url)
        oa = 1 if is_open_access_url(candidate.article_url) else 0
        conn.execute(
            """
            INSERT INTO articles (
                article_key, message_id, alert_name, rank_in_email, title, authors,
                publication_info, snippet, scholar_url, article_url, pdf_url,
                format_label, author_count, score, status, reasons_json,
                created_at, updated_at, is_open_access
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_key) DO UPDATE SET
                message_id = excluded.message_id,
                rank_in_email = excluded.rank_in_email,
                authors = excluded.authors,
                publication_info = excluded.publication_info,
                snippet = excluded.snippet,
                scholar_url = excluded.scholar_url,
                article_url = excluded.article_url,
                pdf_url = excluded.pdf_url,
                format_label = excluded.format_label,
                author_count = excluded.author_count,
                score = excluded.score,
                status = excluded.status,
                reasons_json = excluded.reasons_json,
                updated_at = excluded.updated_at,
                is_open_access = excluded.is_open_access
            """,
            (
                key,
                message_id,
                candidate.alert_name,
                candidate.rank_in_email,
                candidate.title,
                candidate.authors,
                candidate.publication_info,
                candidate.snippet,
                candidate.scholar_url,
                candidate.article_url,
                candidate.pdf_url,
                candidate.format_label,
                candidate.author_count,
                candidate.score,
                candidate.status,
                json.dumps(candidate.reasons, separators=(",", ":")),
                now,
                now,
                oa,
            ),
        )
        inserted += 1
        if candidate.status == "selected":
            selected += 1
        elif candidate.status == "review":
            review += 1

    return inserted, selected, review


def sync_messages(db_path: Path, days_back: int, after: str | None, before: str | None, max_messages: int | None, base_query: str) -> dict[str, Any]:
    conn = ensure_db(db_path)
    effective_after, effective_before = compute_window(days_back, after, before)
    query = gmail_query(base_query, effective_after, effective_before)
    message_refs = list_message_ids(query, max_messages)

    summary = {
        "db_path": str(db_path),
        "query": query,
        "message_count": 0,
        "article_count": 0,
        "selected_count": 0,
        "review_count": 0,
        "alerts": {},
    }

    for ref in message_refs:
        message = get_message(ref["id"])
        payload = message.get("payload", {})
        headers = header_map(payload)
        html = extract_html(payload)
        if not html:
            continue

        subject = headers.get("subject", "")
        alert_name, candidates = parse_articles_from_html(html, subject)
        upsert_message(conn, message, alert_name)
        inserted, selected, review = upsert_articles(conn, message["id"], candidates)

        summary["message_count"] += 1
        summary["article_count"] += inserted
        summary["selected_count"] += selected
        summary["review_count"] += review
        summary["alerts"].setdefault(alert_name, 0)
        summary["alerts"][alert_name] += len(candidates)

    conn.commit()
    conn.close()
    summary["alerts"] = [
        {"alert_name": name, "article_count": count}
        for name, count in sorted(summary["alerts"].items(), key=lambda item: (-item[1], item[0].lower()))
    ]
    return summary


def list_alerts(db_path: Path) -> dict[str, Any]:
    conn = ensure_db(db_path)
    rows = conn.execute(
        """
        SELECT alert_name, COUNT(*) AS article_count,
               SUM(CASE WHEN status = 'selected' THEN 1 ELSE 0 END) AS selected_count,
               SUM(CASE WHEN status = 'review' THEN 1 ELSE 0 END) AS review_count,
               SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count
        FROM articles
        GROUP BY alert_name
        ORDER BY selected_count DESC, article_count DESC, alert_name ASC
        """
    ).fetchall()
    conn.close()
    return {
        "db_path": str(db_path),
        "alerts": [dict(row) for row in rows],
    }


def list_articles(db_path: Path, status: str, alert_name: str | None, limit: int) -> dict[str, Any]:
    conn = ensure_db(db_path)
    sql = """
        SELECT alert_name, title, authors, publication_info, article_url, pdf_url,
               status, score, reasons_json, message_id, rank_in_email
        FROM articles
    """
    clauses: list[str] = []
    params: list[Any] = []

    if status != "all":
        clauses.append("status = ?")
        params.append(status)
    if alert_name:
        clauses.append("alert_name = ?")
        params.append(alert_name)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY score DESC, alert_name ASC, rank_in_email ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    articles = []
    for row in rows:
        item = dict(row)
        item["reasons"] = json.loads(item.pop("reasons_json"))
        articles.append(item)

    return {
        "db_path": str(db_path),
        "articles": articles,
    }


def mark_article_processed(db_path: Path, article_url: str) -> dict[str, Any]:
    """Set processed_at on unprocessed articles matching article_url."""
    conn = ensure_db(db_path)
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    cur = conn.execute(
        """
        UPDATE articles
        SET processed_at = ?, updated_at = ?
        WHERE article_url = ? AND processed_at IS NULL
        """,
        (now, now, article_url),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return {"updated": updated, "article_url": article_url}


def list_backlog(
    db_path: Path,
    status: str,
    min_score: int,
    source: str | None,
    open_access_only: bool,
    include_processed: bool,
    limit: int,
) -> dict[str, Any]:
    """Query the DB for unprocessed candidate articles with optional filters."""
    conn = ensure_db(db_path)
    sql = """
        SELECT article_key, alert_name, title, authors, article_url, pdf_url,
               score, status, is_open_access, processed_at, created_at, reasons_json
        FROM articles
    """
    clauses: list[str] = []
    params: list[Any] = []

    if status != "all":
        clauses.append("status = ?")
        params.append(status)
    if min_score > 0:
        clauses.append("score >= ?")
        params.append(min_score)
    if open_access_only or source:
        clauses.append("is_open_access = 1")
    if not include_processed:
        clauses.append("processed_at IS NULL")

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY score DESC, alert_name ASC, created_at DESC LIMIT ?"
    params.append(limit * 3 if source else limit)  # over-fetch when filtering by source

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    articles = []
    for row in rows:
        item = dict(row)
        item["reasons"] = json.loads(item.pop("reasons_json"))
        if source and not _url_matches_source(item.get("article_url") or "", source):
            continue
        articles.append(item)
        if len(articles) >= limit:
            break

    return {
        "db_path": str(db_path),
        "filters": {
            "status": status,
            "min_score": min_score,
            "source": source,
            "open_access_only": open_access_only,
            "include_processed": include_processed,
        },
        "article_count": len(articles),
        "articles": articles,
    }


def curate_recent(
    db_path: Path,
    topic: str,
    days_back: int,
    after: str | None,
    before: str | None,
    max_messages: int,
    max_results: int,
    base_query: str,
) -> dict[str, Any]:
    ensure_db(db_path).close()
    effective_after, effective_before = compute_window(days_back, after, before)
    query = gmail_query(base_query, effective_after, effective_before)
    message_refs = list_message_ids(query, max_messages)
    candidates: list[dict[str, Any]] = []

    for ref in message_refs:
        message = get_message(ref["id"])
        payload = message.get("payload", {})
        headers = header_map(payload)
        html = extract_html(payload)
        if not html:
            continue

        subject = headers.get("subject", "")
        _, parsed = parse_articles_from_html(html, subject)
        sent_at = headers.get("date")

        for candidate in parsed:
            hit_score, matched_fields = keyword_hits(topic, candidate)
            if hit_score <= 0:
                continue

            candidates.append(
                {
                    "alert_name": candidate.alert_name,
                    "title": candidate.title,
                    "authors": candidate.authors,
                    "publication_info": candidate.publication_info,
                    "snippet": candidate.snippet,
                    "article_url": candidate.article_url,
                    "pdf_url": candidate.pdf_url,
                    "score": candidate.score,
                    "status": candidate.status,
                    "reasons": candidate.reasons,
                    "keyword_score": hit_score,
                    "matched_fields": matched_fields,
                    "message_id": message["id"],
                    "sent_at": sent_at,
                }
            )

    candidates.sort(
        key=lambda item: (
            -item["keyword_score"],
            -item["score"],
            item["status"] != "selected",
            item["alert_name"].lower(),
            item["title"].lower(),
        )
    )

    return {
        "db_path": str(db_path),
        "query": query,
        "topic": topic,
        "message_count": len(message_refs),
        "match_count": len(candidates),
        "articles": candidates[:max_results],
    }


def search_recent(
    db_path: Path,
    gmail_query_extra: str,
    topic: str | None,
    days_back: int,
    after: str | None,
    before: str | None,
    max_messages: int,
    max_results: int,
    include_review: bool,
    save: bool,
    base_query: str,
) -> dict[str, Any]:
    conn = ensure_db(db_path)
    effective_after, effective_before = compute_window(days_back, after, before)
    query = combined_gmail_query(
        base_query=base_query,
        extra_query=gmail_query_extra,
        after=effective_after,
        before=effective_before,
    )
    message_refs = list_message_ids(query, max_messages)
    candidates: list[dict[str, Any]] = []
    persisted_messages = 0
    persisted_articles = 0

    for ref in message_refs:
        message = get_message(ref["id"])
        payload = message.get("payload", {})
        headers = header_map(payload)
        html = extract_html(payload)
        if not html:
            continue

        subject = headers.get("subject", "")
        alert_name, parsed = parse_articles_from_html(html, subject)
        sent_at = headers.get("date")

        if save:
            upsert_message(conn, message, alert_name)
            inserted, _, _ = upsert_articles(conn, message["id"], parsed)
            persisted_messages += 1
            persisted_articles += inserted

        for candidate in parsed:
            hit_score = 0
            matched_fields: list[str] = []
            if topic:
                hit_score, matched_fields = keyword_hits(topic, candidate)
                if hit_score <= 0:
                    continue
            elif candidate.status == "rejected":
                continue
            elif candidate.status == "review" and not include_review:
                continue

            candidates.append(
                {
                    "alert_name": candidate.alert_name,
                    "title": candidate.title,
                    "authors": candidate.authors,
                    "publication_info": candidate.publication_info,
                    "snippet": candidate.snippet,
                    "article_url": candidate.article_url,
                    "pdf_url": candidate.pdf_url,
                    "score": candidate.score,
                    "status": candidate.status,
                    "reasons": candidate.reasons,
                    "keyword_score": hit_score,
                    "matched_fields": matched_fields,
                    "message_id": message["id"],
                    "sent_at": sent_at,
                }
            )

    if save:
        conn.commit()
    conn.close()

    candidates.sort(
        key=lambda item: (
            -item["keyword_score"],
            -item["score"],
            item["status"] != "selected",
            item["alert_name"].lower(),
            item["title"].lower(),
        )
    )

    return {
        "db_path": str(db_path),
        "query": query,
        "topic": topic,
        "message_count": len(message_refs),
        "match_count": len(candidates),
        "persisted_messages": persisted_messages,
        "persisted_articles": persisted_articles,
        "articles": candidates[:max_results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()

    try:
        if args.command == "sync":
            result = sync_messages(
                db_path=db_path,
                days_back=args.days_back,
                after=args.after,
                before=args.before,
                max_messages=args.max_messages,
                base_query=args.query,
            )
        elif args.command == "alerts":
            result = list_alerts(db_path)
        elif args.command == "articles":
            result = list_articles(
                db_path=db_path,
                status=args.status,
                alert_name=args.alert_name,
                limit=args.limit,
            )
        elif args.command == "curate":
            result = curate_recent(
                db_path=db_path,
                topic=args.topic,
                days_back=args.days_back,
                after=args.after,
                before=args.before,
                max_messages=args.max_messages,
                max_results=args.max_results,
                base_query=args.query,
            )
        elif args.command == "search":
            result = search_recent(
                db_path=db_path,
                gmail_query_extra=args.gmail_query,
                topic=args.topic,
                days_back=args.days_back,
                after=args.after,
                before=args.before,
                max_messages=args.max_messages,
                max_results=args.max_results,
                include_review=args.include_review,
                save=args.save,
                base_query=args.query,
            )
        elif args.command == "backlog":
            result = list_backlog(
                db_path=db_path,
                status=args.status,
                min_score=args.min_score,
                source=args.source,
                open_access_only=args.open_access,
                include_processed=args.include_processed,
                limit=args.limit,
            )
        elif args.command == "mark-processed":
            result = mark_article_processed(
                db_path=db_path,
                article_url=args.article_url,
            )
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "result": result}, indent=2))
    return 0
