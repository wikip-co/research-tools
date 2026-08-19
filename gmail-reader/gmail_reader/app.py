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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

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

DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
WORKFLOW_STATES = (
    "discovered",
    "scraped",
    "matched",
    "drafted",
    "committed",
    "pr_open",
    "merged",
)
WORKFLOW_STATE_RANK = {state: index for index, state in enumerate(WORKFLOW_STATES)}
WORKFLOW_STATE_TIMESTAMP_COLUMNS = {
    "scraped": "scraped_at",
    "matched": "matched_at",
    "drafted": "drafted_at",
    "committed": "committed_at",
    "pr_open": "pr_opened_at",
    "merged": "merged_at",
}
PUBLICATION_JOB_STATES = (
    "queued",
    "leased",
    "scraping",
    "matching",
    "drafting",
    "validating",
    "validated",
    "needs_review",
    "publishing",
    "pr_open",
    "duplicate",
    "rejected",
    "retry",
    "failed",
)
PUBLICATION_JOB_TERMINAL_STATES = {"pr_open", "duplicate", "rejected", "failed"}
PUBLICATION_JOB_STOPPED_STATES = {*PUBLICATION_JOB_TERMINAL_STATES, "needs_review", "validated"}
DEFAULT_RESEARCH_DOMAIN = "Natural Healing"
PUBLICATION_CLAIM_POLICIES = {"integrated", "strict", "compendium"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_research_domain(value: str) -> str:
    domain = re.sub(r"\s+", " ", value).strip()
    if not domain or domain in {".", ".."} or "/" in domain or "\\" in domain:
        raise ValueError("domain must be one top-level content directory name")
    return domain


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


def normalize_doi(value: str) -> str:
    match = DOI_PATTERN.search(value or "")
    if not match:
        return ""
    doi = match.group(0).rstrip(").,;").lower()
    return doi


def extract_pmid(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    hostname = (parsed.hostname or "").lower()
    if "pubmed.ncbi.nlm.nih.gov" not in hostname:
        return ""
    match = re.search(r"/(\d{5,10})/?$", parsed.path)
    return match.group(1) if match else ""


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
    return urlunparse(("https", hostname, path, "", "", ""))


def paper_identity(title: str, article_url: str, doi: str = "", pmid: str = "") -> tuple[str, str, str, str]:
    normalized_doi = normalize_doi(doi or article_url)
    normalized_pmid = (pmid or extract_pmid(article_url)).strip()
    canonical_url = canonicalize_url(article_url)
    if normalized_doi:
        basis = f"doi:{normalized_doi}"
    elif normalized_pmid:
        basis = f"pmid:{normalized_pmid}"
    elif canonical_url:
        basis = f"url:{canonical_url}"
    else:
        normalized_title = normalize_space(re.sub(r"[^a-z0-9]+", " ", title.lower()))
        basis = f"title:{normalized_title}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return digest, canonical_url, normalized_doi, normalized_pmid


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
        choices=["selected", "review", "rejected", "invalid", "all"],
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
        choices=["selected", "review", "rejected", "invalid", "all"],
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

    enqueue_parser = subparsers.add_parser(
        "enqueue-publication",
        help="Queue one stored article for the local publishing worker.",
    )
    enqueue_parser.add_argument("identifier", help="Article key or article URL.")
    enqueue_parser.add_argument("--max-attempts", type=int, default=3)
    enqueue_parser.add_argument("--domain", required=True)
    enqueue_parser.add_argument(
        "--claim-policy", choices=["integrated", "strict"], default="integrated"
    )

    enqueue_backlog_parser = subparsers.add_parser(
        "enqueue-publication-backlog",
        help="Queue the highest-scoring unprocessed Scholar backlog rows.",
    )
    enqueue_backlog_parser.add_argument("--status", default="selected", choices=["selected", "review"])
    enqueue_backlog_parser.add_argument("--min-score", type=int, default=12)
    enqueue_backlog_parser.add_argument("--limit", type=int, default=10)
    enqueue_backlog_parser.add_argument("--max-attempts", type=int, default=3)
    enqueue_backlog_parser.add_argument("--domain", required=True)
    enqueue_backlog_parser.add_argument(
        "--claim-policy", choices=["integrated", "strict"], default="integrated"
    )

    claim_parser = subparsers.add_parser(
        "claim-publication",
        help="Atomically lease the next eligible local publishing job.",
    )
    claim_parser.add_argument("--worker", required=True, help="Stable worker identifier.")
    claim_parser.add_argument("--lease-seconds", type=int, default=3600)

    jobs_parser = subparsers.add_parser(
        "publication-jobs",
        help="List local publishing jobs.",
    )
    jobs_parser.add_argument("--state", choices=["all", *PUBLICATION_JOB_STATES], default="all")
    jobs_parser.add_argument("--limit", type=int, default=20)

    update_job_parser = subparsers.add_parser(
        "set-publication-job-state",
        help="Record a local publishing job transition and its outputs.",
    )
    update_job_parser.add_argument("job_id", type=int)
    update_job_parser.add_argument("--state", required=True, choices=PUBLICATION_JOB_STATES)
    update_job_parser.add_argument("--worker", default="")
    update_job_parser.add_argument("--paper-key", default="")
    update_job_parser.add_argument("--packet-path", default="")
    update_job_parser.add_argument("--target-path", default="")
    update_job_parser.add_argument("--branch", default="")
    update_job_parser.add_argument("--commit", default="")
    update_job_parser.add_argument("--pr", default="")
    update_job_parser.add_argument("--error", default="")
    update_job_parser.add_argument("--result-json", default="")
    update_job_parser.add_argument("--run-id", default="")

    requeue_job_parser = subparsers.add_parser(
        "requeue-publication",
        help="Explicitly reset a stopped publication job for another run.",
    )
    requeue_job_parser.add_argument("job_id", type=int)
    requeue_job_parser.add_argument("--reason", required=True)

    backfill_parser = subparsers.add_parser(
        "backfill-paper-keys",
        help="Link historical article rows to canonical paper records.",
    )
    backfill_parser.add_argument(
        "--status",
        choices=["selected", "review", "rejected", "invalid", "all"],
        default="selected",
    )
    backfill_parser.add_argument("--limit", type=int, default=1000)
    backfill_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write links; without this flag the command is a dry run.",
    )

    papers_parser = subparsers.add_parser(
        "papers",
        help="List canonical paper records and publishing state.",
    )
    papers_parser.add_argument(
        "--status",
        choices=["all", "matched", "unmatched", "archived", "unarchived"],
        default="all",
        help="Filter paper records by publishing/archive state.",
    )
    papers_parser.add_argument(
        "--workflow-state",
        choices=["all", *WORKFLOW_STATES],
        default="all",
        help="Optional workflow-state filter for the paper lifecycle.",
    )
    papers_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of paper rows to return.",
    )

    find_paper_parser = subparsers.add_parser(
        "find-paper",
        help="Find a canonical paper by URL, DOI, PMID, or paper key.",
    )
    find_paper_parser.add_argument(
        "identifier",
        help="Paper URL, DOI, PMID, or paper key.",
    )

    upsert_paper_parser = subparsers.add_parser(
        "upsert-paper",
        help="Upsert a canonical paper record outside the Gmail sync flow.",
    )
    upsert_paper_parser.add_argument("--title", required=True, help="Canonical paper title.")
    upsert_paper_parser.add_argument("--url", default="", help="Primary article URL.")
    upsert_paper_parser.add_argument("--doi", default="", help="DOI for the paper.")
    upsert_paper_parser.add_argument("--pmid", default="", help="PMID for the paper.")
    upsert_paper_parser.add_argument(
        "--workflow-state",
        choices=WORKFLOW_STATES,
        default="discovered",
        help="Initial workflow state for the paper record.",
    )
    upsert_paper_parser.add_argument(
        "--matched-content-path",
        default="",
        help="Optional matched markdown path in the content repo.",
    )

    set_state_parser = subparsers.add_parser(
        "set-paper-state",
        help="Advance the workflow state for a canonical paper and update related metadata.",
    )
    set_state_parser.add_argument("identifier", help="Paper URL, DOI, PMID, or paper key.")
    set_state_parser.add_argument(
        "--state",
        required=True,
        choices=WORKFLOW_STATES,
        help="Workflow state to apply.",
    )
    set_state_parser.add_argument(
        "--matched-content-path",
        default="",
        help="Optional content path associated with the paper.",
    )
    set_state_parser.add_argument("--commit", default="", help="Optional git commit SHA.")
    set_state_parser.add_argument("--pr", default="", help="Optional PR URL or identifier.")
    set_state_parser.add_argument(
        "--archive-path",
        default="",
        help="Optional archived source path to attach while updating the state.",
    )

    mark_published_parser = subparsers.add_parser(
        "mark-published",
        help="Compatibility alias for advancing a paper into drafted/committed/pr_open.",
    )
    mark_published_parser.add_argument(
        "identifier",
        help="Paper URL, DOI, PMID, or paper key.",
    )
    mark_published_parser.add_argument(
        "--matched-content-path",
        required=True,
        help="Markdown path in the content repo that this paper maps to.",
    )
    mark_published_parser.add_argument("--commit", default="", help="Optional git commit SHA.")
    mark_published_parser.add_argument("--pr", default="", help="Optional PR URL or identifier.")

    attach_archive_parser = subparsers.add_parser(
        "attach-archive",
        help="Attach an archived source path to a canonical paper.",
    )
    attach_archive_parser.add_argument(
        "identifier",
        help="Paper URL, DOI, PMID, or paper key.",
    )
    attach_archive_parser.add_argument(
        "--archive-path",
        required=True,
        help="Archived PDF/HTML path for the paper.",
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
            paper_key TEXT,
            domain TEXT NOT NULL DEFAULT 'Natural Healing',
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
            FOREIGN KEY (message_id) REFERENCES messages(message_id),
            FOREIGN KEY (paper_key) REFERENCES papers(paper_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
            paper_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            canonical_url TEXT,
            doi TEXT,
            pmid TEXT,
            workflow_state TEXT NOT NULL DEFAULT 'discovered',
            workflow_state_updated_at TEXT,
            matched_content_path TEXT,
            published_commit TEXT,
            published_pr TEXT,
            scraped_at TEXT,
            matched_at TEXT,
            drafted_at TEXT,
            committed_at TEXT,
            pr_opened_at TEXT,
            merged_at TEXT,
            archived_source_path TEXT,
            archived_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS publication_jobs (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_key TEXT NOT NULL,
            paper_key TEXT,
            source_url TEXT NOT NULL,
            canonical_source_url TEXT,
            alert_name TEXT,
            domain TEXT NOT NULL DEFAULT 'Natural Healing',
            claim_policy TEXT NOT NULL DEFAULT 'integrated',
            state TEXT NOT NULL DEFAULT 'queued',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            lease_owner TEXT,
            lease_expires_at TEXT,
            next_run_at TEXT,
            run_id TEXT,
            packet_path TEXT,
            target_path TEXT,
            branch TEXT,
            commit_sha TEXT,
            pr_url TEXT,
            result_json TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY (article_key) REFERENCES articles(article_key),
            FOREIGN KEY (paper_key) REFERENCES papers(paper_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS publication_job_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            detail_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES publication_jobs(job_id)
        )
        """
    )
    # Migrations for existing databases
    for migration in (
        "ALTER TABLE articles ADD COLUMN paper_key TEXT",
        "ALTER TABLE articles ADD COLUMN is_open_access INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE articles ADD COLUMN processed_at TEXT",
        "ALTER TABLE articles ADD COLUMN domain TEXT NOT NULL DEFAULT 'Natural Healing'",
        "ALTER TABLE papers ADD COLUMN workflow_state TEXT NOT NULL DEFAULT 'discovered'",
        "ALTER TABLE papers ADD COLUMN workflow_state_updated_at TEXT",
        "ALTER TABLE papers ADD COLUMN scraped_at TEXT",
        "ALTER TABLE papers ADD COLUMN matched_at TEXT",
        "ALTER TABLE papers ADD COLUMN drafted_at TEXT",
        "ALTER TABLE papers ADD COLUMN committed_at TEXT",
        "ALTER TABLE papers ADD COLUMN pr_opened_at TEXT",
        "ALTER TABLE papers ADD COLUMN merged_at TEXT",
        "ALTER TABLE papers ADD COLUMN archived_at TEXT",
        "ALTER TABLE publication_jobs ADD COLUMN canonical_source_url TEXT",
        "ALTER TABLE publication_jobs ADD COLUMN domain TEXT NOT NULL DEFAULT 'Natural Healing'",
        "ALTER TABLE publication_jobs ADD COLUMN claim_policy TEXT NOT NULL DEFAULT 'integrated'",
        "ALTER TABLE publication_jobs ADD COLUMN next_run_at TEXT",
        "ALTER TABLE publication_jobs ADD COLUMN run_id TEXT",
    ):
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status, alert_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_message_id ON articles(message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_paper_key ON articles(paper_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_papers_canonical_url ON papers(canonical_url)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_papers_pmid ON papers(pmid)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_papers_workflow_state ON papers(workflow_state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_publication_jobs_state ON publication_jobs(state, next_run_at, lease_expires_at, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_publication_jobs_claimable ON publication_jobs(state, next_run_at, lease_expires_at, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_publication_job_events_job ON publication_job_events(job_id, event_id)"
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_jobs_active_article
        ON publication_jobs(article_key)
        WHERE state NOT IN ('pr_open', 'duplicate', 'rejected', 'failed')
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_jobs_active_source
        ON publication_jobs(source_url)
        WHERE state NOT IN ('pr_open', 'duplicate', 'rejected', 'failed')
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_jobs_active_canonical_source
        ON publication_jobs(canonical_source_url)
        WHERE canonical_source_url IS NOT NULL
          AND canonical_source_url != ''
          AND state NOT IN ('pr_open', 'duplicate', 'rejected', 'failed')
        """
    )
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


def workflow_state_rank(state: str | None) -> int:
    return WORKFLOW_STATE_RANK.get((state or "").strip(), 0)


def derive_workflow_state(
    requested_state: str | None,
    *,
    matched_content_path: str = "",
    commit: str = "",
    pr: str = "",
) -> str:
    state = (requested_state or "").strip() or "discovered"
    if pr and workflow_state_rank(state) < workflow_state_rank("pr_open"):
        state = "pr_open"
    elif commit and workflow_state_rank(state) < workflow_state_rank("committed"):
        state = "committed"
    elif matched_content_path and workflow_state_rank(state) < workflow_state_rank("matched"):
        state = "matched"
    if state not in WORKFLOW_STATE_RANK:
        raise ValueError(f"Unsupported workflow state: {state}")
    return state


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
            utc_now_iso(),
        ),
    )


def upsert_paper_record(
    conn: sqlite3.Connection,
    *,
    title: str,
    article_url: str,
    doi: str = "",
    pmid: str = "",
    workflow_state: str = "discovered",
    matched_content_path: str = "",
) -> dict[str, str]:
    paper_key, canonical_url, normalized_doi, normalized_pmid = paper_identity(
        title=title,
        article_url=article_url,
        doi=doi,
        pmid=pmid,
    )
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO papers (
            paper_key, title, canonical_url, doi, pmid, workflow_state,
            workflow_state_updated_at, matched_content_path, published_commit,
            published_pr, scraped_at, matched_at, drafted_at, committed_at,
            pr_opened_at, merged_at, archived_source_path, archived_at,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paper_key) DO UPDATE SET
            title = CASE
                WHEN excluded.title IS NOT NULL AND excluded.title <> '' THEN excluded.title
                ELSE papers.title
            END,
            canonical_url = COALESCE(NULLIF(excluded.canonical_url, ''), papers.canonical_url),
            doi = COALESCE(NULLIF(excluded.doi, ''), papers.doi),
            pmid = COALESCE(NULLIF(excluded.pmid, ''), papers.pmid),
            matched_content_path = COALESCE(NULLIF(excluded.matched_content_path, ''), papers.matched_content_path),
            updated_at = excluded.updated_at
        """,
        (
            paper_key,
            title,
            canonical_url,
            normalized_doi,
            normalized_pmid,
            workflow_state,
            now,
            matched_content_path,
            "",
            "",
            now if workflow_state_rank(workflow_state) >= workflow_state_rank("scraped") else "",
            now if workflow_state_rank(workflow_state) >= workflow_state_rank("matched") else "",
            now if workflow_state_rank(workflow_state) >= workflow_state_rank("drafted") else "",
            now if workflow_state_rank(workflow_state) >= workflow_state_rank("committed") else "",
            now if workflow_state_rank(workflow_state) >= workflow_state_rank("pr_open") else "",
            now if workflow_state_rank(workflow_state) >= workflow_state_rank("merged") else "",
            "",
            "",
            now,
            now,
        ),
    )
    return {
        "paper_key": paper_key,
        "canonical_url": canonical_url,
        "doi": normalized_doi,
        "pmid": normalized_pmid,
    }


def upsert_articles(
    conn: sqlite3.Connection, message_id: str, candidates: list[ArticleCandidate]
) -> tuple[int, int, int]:
    inserted = 0
    selected = 0
    review = 0

    for candidate in candidates:
        now = utc_now_iso()
        key = article_key(candidate.alert_name, candidate.title, candidate.article_url)
        paper = upsert_paper_record(
            conn,
            title=candidate.title,
            article_url=candidate.article_url,
        )
        oa = 1 if is_open_access_url(candidate.article_url) else 0
        conn.execute(
            """
            INSERT INTO articles (
                article_key, paper_key, message_id, alert_name, rank_in_email, title, authors,
                publication_info, snippet, scholar_url, article_url, pdf_url,
                format_label, author_count, score, status, reasons_json,
                created_at, updated_at, is_open_access
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_key) DO UPDATE SET
                paper_key = excluded.paper_key,
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
                paper["paper_key"],
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
               SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
               SUM(CASE WHEN status = 'invalid' THEN 1 ELSE 0 END) AS invalid_count
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
        SELECT a.alert_name, a.title, a.authors, a.publication_info, a.article_url, a.pdf_url,
               a.status, a.score, a.reasons_json, a.message_id, a.rank_in_email,
               a.paper_key, p.canonical_url, p.doi, p.pmid, p.matched_content_path,
               p.published_commit, p.published_pr, p.archived_source_path
        FROM articles a
        LEFT JOIN papers p ON p.paper_key = a.paper_key
    """
    clauses: list[str] = []
    params: list[Any] = []

    if status != "all":
        clauses.append("a.status = ?")
        params.append(status)
    if alert_name:
        clauses.append("a.alert_name = ?")
        params.append(alert_name)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY a.score DESC, a.alert_name ASC, a.rank_in_email ASC LIMIT ?"
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
    now = utc_now_iso()
    paper_key, canonical_url, normalized_doi, normalized_pmid = paper_identity("", article_url, article_url, "")
    cur = None
    if normalized_doi or normalized_pmid or canonical_url:
        paper_row = conn.execute(
            """
            SELECT paper_key FROM papers
            WHERE paper_key = ?
               OR canonical_url = ?
               OR doi = ?
               OR pmid = ?
            LIMIT 1
            """,
            (paper_key, canonical_url, normalized_doi, normalized_pmid),
        ).fetchone()
        if paper_row:
            cur = conn.execute(
                """
                UPDATE articles
                SET processed_at = ?, updated_at = ?
                WHERE paper_key = ? AND processed_at IS NULL
                """,
                (now, now, paper_row["paper_key"]),
            )

    if cur is None:
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
        SELECT a.article_key, a.paper_key, a.alert_name, a.title, a.authors, a.article_url, a.pdf_url,
               a.score, a.status, a.is_open_access, a.processed_at, a.created_at, a.reasons_json,
               p.canonical_url, p.doi, p.pmid, p.workflow_state, p.matched_content_path,
               p.published_commit, p.published_pr, p.archived_source_path
        FROM articles a
        LEFT JOIN papers p ON p.paper_key = a.paper_key
    """
    clauses: list[str] = []
    params: list[Any] = []

    if status != "all":
        clauses.append("a.status = ?")
        params.append(status)
    if min_score > 0:
        clauses.append("a.score >= ?")
        params.append(min_score)
    if open_access_only or source:
        clauses.append("a.is_open_access = 1")
    if not include_processed:
        clauses.append(
            "a.processed_at IS NULL AND COALESCE(p.workflow_state, 'discovered') NOT IN ('drafted', 'committed', 'pr_open', 'merged')"
        )

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY a.score DESC, a.alert_name ASC, a.created_at DESC LIMIT ?"
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


def list_papers(db_path: Path, status: str, workflow_state: str, limit: int) -> dict[str, Any]:
    conn = ensure_db(db_path)
    sql = """
        SELECT paper_key, title, canonical_url, doi, pmid, workflow_state,
               workflow_state_updated_at, matched_content_path, published_commit,
               published_pr, scraped_at, matched_at, drafted_at, committed_at,
               pr_opened_at, merged_at, archived_source_path, archived_at,
               created_at, updated_at
        FROM papers
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status == "matched":
        clauses.append("matched_content_path IS NOT NULL AND matched_content_path <> ''")
    elif status == "unmatched":
        clauses.append("(matched_content_path IS NULL OR matched_content_path = '')")
    elif status == "archived":
        clauses.append("archived_source_path IS NOT NULL AND archived_source_path <> ''")
    elif status == "unarchived":
        clauses.append("(archived_source_path IS NULL OR archived_source_path = '')")
    if workflow_state != "all":
        clauses.append("workflow_state = ?")
        params.append(workflow_state)

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at DESC, title ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return {
        "db_path": str(db_path),
        "status": status,
        "workflow_state": workflow_state,
        "papers": [dict(row) for row in rows],
    }


def resolve_paper_row(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    stripped = identifier.strip()
    normalized_doi = normalize_doi(stripped)
    canonical_url = canonicalize_url(stripped)
    normalized_pmid = stripped if stripped.isdigit() else ""
    # Only compare non-empty values: an empty normalized DOI/PMID must never
    # match rows whose doi/pmid columns are empty strings, which previously
    # resolved identifiers to an arbitrary unrelated paper.
    conditions: list[str] = []
    params: list[str] = []
    for column, value in (
        ("paper_key", stripped),
        ("canonical_url", canonical_url),
        ("doi", normalized_doi),
        ("pmid", normalized_pmid),
    ):
        if value:
            conditions.append(f"{column} = ?")
            params.append(value)
    if not conditions:
        return None
    row = conn.execute(
        f"SELECT * FROM papers WHERE {' OR '.join(conditions)} LIMIT 1",
        params,
    ).fetchone()
    if row:
        return row
    if canonical_url:
        row = conn.execute(
            """
            SELECT p.*
            FROM papers p
            JOIN articles a ON a.paper_key = p.paper_key
            WHERE a.article_url = ?
               OR p.canonical_url = ?
            LIMIT 1
            """,
            (stripped, canonical_url),
        ).fetchone()
    return row


def advance_paper_state(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    state: str | None = None,
    matched_content_path: str = "",
    commit: str = "",
    pr: str = "",
    archive_path: str = "",
) -> dict[str, Any]:
    now = utc_now_iso()
    target_state = derive_workflow_state(
        state,
        matched_content_path=matched_content_path,
        commit=commit,
        pr=pr,
    )
    current_state = (row["workflow_state"] or "discovered").strip()
    effective_state = (
        target_state
        if workflow_state_rank(target_state) >= workflow_state_rank(current_state)
        else current_state
    )

    updates: dict[str, Any] = {
        "workflow_state": effective_state,
        "workflow_state_updated_at": now,
        "updated_at": now,
    }
    if matched_content_path:
        updates["matched_content_path"] = matched_content_path
        if not row["matched_at"] and workflow_state_rank(effective_state) >= workflow_state_rank("matched"):
            updates["matched_at"] = now
    if commit:
        updates["published_commit"] = commit
    if pr:
        updates["published_pr"] = pr
    if archive_path:
        updates["archived_source_path"] = archive_path
        if not row["archived_at"]:
            updates["archived_at"] = now

    for workflow_state, column in WORKFLOW_STATE_TIMESTAMP_COLUMNS.items():
        if workflow_state_rank(effective_state) >= workflow_state_rank(workflow_state) and not row[column]:
            updates[column] = now

    columns = [f"{column} = ?" for column in updates]
    values = list(updates.values()) + [row["paper_key"]]
    conn.execute(
        f"UPDATE papers SET {', '.join(columns)} WHERE paper_key = ?",
        values,
    )
    updated = resolve_paper_row(conn, row["paper_key"])
    return dict(updated) if updated else {"paper_key": row["paper_key"]}


def find_paper(db_path: Path, identifier: str) -> dict[str, Any]:
    conn = ensure_db(db_path)
    row = resolve_paper_row(conn, identifier)
    if not row:
        conn.close()
        return {"found": False, "identifier": identifier}

    paper = dict(row)
    article_rows = conn.execute(
        """
        SELECT alert_name, title, article_url, pdf_url, status, score, processed_at, message_id
        FROM articles
        WHERE paper_key = ?
        ORDER BY updated_at DESC, alert_name ASC
        """,
        (paper["paper_key"],),
    ).fetchall()
    conn.close()
    return {
        "found": True,
        "identifier": identifier,
        "paper": paper,
        "articles": [dict(item) for item in article_rows],
    }


def upsert_external_paper(
    db_path: Path,
    title: str,
    url: str,
    doi: str,
    pmid: str,
    workflow_state: str = "discovered",
    matched_content_path: str = "",
) -> dict[str, Any]:
    conn = ensure_db(db_path)
    paper = upsert_paper_record(
        conn,
        title=title,
        article_url=url,
        doi=doi,
        pmid=pmid,
        workflow_state=workflow_state,
        matched_content_path=matched_content_path,
    )
    row = resolve_paper_row(conn, paper["paper_key"])
    updated = (
        advance_paper_state(
            conn,
            row,
            state=workflow_state,
            matched_content_path=matched_content_path,
        )
        if row
        else paper
    )
    conn.commit()
    conn.close()
    return {"paper": updated}


def mark_paper_published(
    db_path: Path,
    identifier: str,
    matched_content_path: str,
    commit: str,
    pr: str,
) -> dict[str, Any]:
    conn = ensure_db(db_path)
    row = resolve_paper_row(conn, identifier)
    if not row:
        conn.close()
        raise ValueError(f"Paper not found: {identifier}")
    updated = advance_paper_state(
        conn,
        row,
        state="pr_open" if pr else "committed" if commit else "drafted",
        matched_content_path=matched_content_path,
        commit=commit,
        pr=pr,
    )
    conn.commit()
    conn.close()
    return {"paper": updated}


def set_paper_state(
    db_path: Path,
    identifier: str,
    state: str,
    matched_content_path: str,
    commit: str,
    pr: str,
    archive_path: str,
) -> dict[str, Any]:
    conn = ensure_db(db_path)
    row = resolve_paper_row(conn, identifier)
    if not row:
        conn.close()
        raise ValueError(f"Paper not found: {identifier}")
    updated = advance_paper_state(
        conn,
        row,
        state=state,
        matched_content_path=matched_content_path,
        commit=commit,
        pr=pr,
        archive_path=archive_path,
    )
    conn.commit()
    conn.close()
    return {"paper": updated}


def attach_archive(
    db_path: Path,
    identifier: str,
    archive_path: str,
) -> dict[str, Any]:
    conn = ensure_db(db_path)
    row = resolve_paper_row(conn, identifier)
    if not row:
        conn.close()
        raise ValueError(f"Paper not found: {identifier}")
    updated = advance_paper_state(conn, row, archive_path=archive_path)
    conn.commit()
    conn.close()
    return {"paper": updated}


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
            paper_key, canonical_url, doi, pmid = paper_identity(
                candidate.title,
                candidate.article_url,
            )

            candidates.append(
                {
                    "alert_name": candidate.alert_name,
                    "title": candidate.title,
                    "authors": candidate.authors,
                    "publication_info": candidate.publication_info,
                    "snippet": candidate.snippet,
                    "article_url": candidate.article_url,
                    "pdf_url": candidate.pdf_url,
                    "paper_key": paper_key,
                    "canonical_url": canonical_url,
                    "doi": doi,
                    "pmid": pmid,
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
            paper_key, canonical_url, doi, pmid = paper_identity(
                candidate.title,
                candidate.article_url,
            )

            candidates.append(
                {
                    "alert_name": candidate.alert_name,
                    "title": candidate.title,
                    "authors": candidate.authors,
                    "publication_info": candidate.publication_info,
                    "snippet": candidate.snippet,
                    "article_url": candidate.article_url,
                    "pdf_url": candidate.pdf_url,
                    "paper_key": paper_key,
                    "canonical_url": canonical_url,
                    "doi": doi,
                    "pmid": pmid,
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


def enqueue_publication_job(
    *,
    db_path: Path,
    identifier: str,
    max_attempts: int,
    domain: str = DEFAULT_RESEARCH_DOMAIN,
    claim_policy: str = "integrated",
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    domain = normalize_research_domain(domain)
    if claim_policy not in PUBLICATION_CLAIM_POLICIES:
        raise ValueError("invalid claim_policy")
    conn = ensure_db(db_path)
    row = conn.execute(
        """
        SELECT article_key, paper_key, article_url, scholar_url, alert_name, title
        FROM articles
        WHERE article_key = ? OR article_url = ? OR scholar_url = ?
        ORDER BY CASE WHEN article_key = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (identifier, identifier, identifier, identifier),
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Article not found: {identifier}")
    source_url = (row["article_url"] or row["scholar_url"] or "").strip()
    if not source_url:
        conn.close()
        raise ValueError("Article has no source URL")
    now = utc_now_iso()
    canonical_source_url = canonicalize_url(source_url)
    conn.execute(
        "UPDATE articles SET domain = ?, updated_at = ? WHERE article_key = ?",
        (domain, now, row["article_key"]),
    )
    try:
        cursor = conn.execute(
            """
            INSERT INTO publication_jobs (
                article_key, paper_key, source_url, canonical_source_url,
                alert_name, domain, claim_policy, state,
                max_attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (
                row["article_key"],
                row["paper_key"],
                source_url,
                canonical_source_url,
                row["alert_name"],
                domain,
                claim_policy,
                max_attempts,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        active = conn.execute(
            """
            SELECT * FROM publication_jobs
            WHERE (article_key = ? OR source_url = ? OR canonical_source_url = ?)
              AND state NOT IN ('pr_open', 'duplicate', 'rejected', 'failed')
            ORDER BY job_id DESC LIMIT 1
            """,
            (row["article_key"], source_url, canonical_source_url),
        ).fetchone()
        conn.close()
        if active:
            return {"created": False, "job": dict(active)}
        raise exc
    job_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO publication_job_events (job_id, from_state, to_state, detail_json, created_at)
        VALUES (?, NULL, 'queued', ?, ?)
        """,
        (
            job_id,
            json.dumps(
                {"title": row["title"], "domain": domain, "claim_policy": claim_policy}
            ),
            now,
        ),
    )
    conn.commit()
    job = conn.execute("SELECT * FROM publication_jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    return {"created": True, "job": dict(job)}


def enqueue_publication_backlog(
    *,
    db_path: Path,
    status: str,
    min_score: int,
    limit: int,
    max_attempts: int,
    domain: str = DEFAULT_RESEARCH_DOMAIN,
    claim_policy: str = "integrated",
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    domain = normalize_research_domain(domain)
    if claim_policy not in PUBLICATION_CLAIM_POLICIES:
        raise ValueError("invalid claim_policy")
    conn = ensure_db(db_path)
    rows = conn.execute(
        """
        SELECT a.article_key, a.paper_key, a.article_url, a.scholar_url,
               a.alert_name, a.title, a.score
        FROM articles a
        LEFT JOIN papers p ON p.paper_key = a.paper_key
        WHERE a.status = ?
          AND a.domain = ?
          AND a.score >= ?
          AND a.processed_at IS NULL
          AND COALESCE(p.workflow_state, 'discovered') NOT IN
              ('drafted', 'committed', 'pr_open', 'merged')
          AND NOT EXISTS (
              SELECT 1 FROM publication_jobs j
              WHERE j.article_key = a.article_key
                 OR j.source_url = a.article_url
                 OR j.source_url = a.scholar_url
          )
        ORDER BY a.score DESC, a.created_at, a.article_key
        LIMIT ?
        """,
        (status, domain, min_score, max(limit, 0) * 5),
    ).fetchall()
    now = utc_now_iso()
    queued: list[dict[str, Any]] = []
    seen_source_urls: set[str] = set()
    skipped = 0
    for row in rows:
        if len(queued) >= max(limit, 0):
            break
        source_url = (row["article_url"] or row["scholar_url"] or "").strip()
        if not source_url:
            skipped += 1
            continue
        canonical_source = canonicalize_url(source_url)
        if canonical_source in seen_source_urls:
            continue
        seen_source_urls.add(canonical_source)
        active = conn.execute(
            """
            SELECT job_id FROM publication_jobs
            WHERE (source_url = ? OR canonical_source_url = ?)
              AND state NOT IN ('pr_open', 'duplicate', 'rejected', 'failed')
            LIMIT 1
            """,
            (source_url, canonical_source),
        ).fetchone()
        if active:
            continue
        cursor = conn.execute(
            """
            INSERT INTO publication_jobs (
                article_key, paper_key, source_url, canonical_source_url,
                alert_name, domain, claim_policy, state,
                max_attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (
                row["article_key"], row["paper_key"], source_url, canonical_source,
                row["alert_name"], domain, claim_policy, max_attempts, now, now,
            ),
        )
        job_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO publication_job_events
                (job_id, from_state, to_state, detail_json, created_at)
            VALUES (?, NULL, 'queued', ?, ?)
            """,
            (
                job_id,
                json.dumps(
                    {
                        "source": "backlog",
                        "score": row["score"],
                        "domain": domain,
                        "claim_policy": claim_policy,
                    }
                ),
                now,
            ),
        )
        queued.append({"job_id": job_id, "article_key": row["article_key"], "title": row["title"], "score": row["score"]})
    conn.commit()
    conn.close()
    return {
        "domain": domain,
        "claim_policy": claim_policy,
        "queued_count": len(queued),
        "skipped_without_url": skipped,
        "jobs": queued,
    }


def claim_publication_job(
    *,
    db_path: Path,
    worker: str,
    lease_seconds: int,
) -> dict[str, Any]:
    if not worker.strip():
        raise ValueError("worker must not be empty")
    if lease_seconds < 60:
        raise ValueError("lease_seconds must be at least 60")
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = ensure_db(db_path)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        """
        SELECT * FROM publication_jobs
        WHERE attempt_count < max_attempts
          AND (
            state = 'queued'
            OR (
              state = 'retry'
              AND (next_run_at IS NULL OR next_run_at <= ?)
            )
            OR (
              state NOT IN ('pr_open', 'duplicate', 'rejected', 'failed', 'needs_review', 'validated')
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            )
          )
        ORDER BY created_at, job_id
        LIMIT 1
        """,
        (now, now),
    ).fetchone()
    if not row:
        conn.commit()
        conn.close()
        return {"claimed": False, "job": None}
    conn.execute(
        """
        UPDATE publication_jobs
        SET state = 'leased', attempt_count = attempt_count + 1,
            lease_owner = ?, lease_expires_at = ?, updated_at = ?,
            started_at = COALESCE(started_at, ?), error = NULL,
            next_run_at = NULL, finished_at = NULL
        WHERE job_id = ?
        """,
        (worker, expires, now, now, row["job_id"]),
    )
    conn.execute(
        """
        INSERT INTO publication_job_events (job_id, from_state, to_state, detail_json, created_at)
        VALUES (?, ?, 'leased', ?, ?)
        """,
        (row["job_id"], row["state"], json.dumps({"worker": worker, "lease_expires_at": expires}), now),
    )
    conn.commit()
    claimed = conn.execute("SELECT * FROM publication_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
    conn.close()
    return {"claimed": True, "job": dict(claimed)}


def set_publication_job_state(
    *,
    db_path: Path,
    job_id: int,
    state: str,
    worker: str = "",
    **updates: str,
) -> dict[str, Any]:
    if state not in PUBLICATION_JOB_STATES:
        raise ValueError(f"Invalid publication job state: {state}")
    conn = ensure_db(db_path)
    row = conn.execute("SELECT * FROM publication_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Publication job not found: {job_id}")
    if worker and row["lease_owner"] and worker != row["lease_owner"]:
        conn.close()
        raise ValueError(f"Publication job {job_id} is leased by another worker")

    now = utc_now_iso()
    column_map = {
        "paper_key": "paper_key",
        "packet_path": "packet_path",
        "target_path": "target_path",
        "branch": "branch",
        "commit": "commit_sha",
        "pr": "pr_url",
        "error": "error",
        "result_json": "result_json",
        "run_id": "run_id",
    }
    assignments = ["state = ?", "updated_at = ?"]
    params: list[Any] = [state, now]
    detail: dict[str, Any] = {"worker": worker} if worker else {}
    for key, column in column_map.items():
        value = updates.get(key, "")
        if not value:
            continue
        if key == "result_json":
            json.loads(value)
        assignments.append(f"{column} = ?")
        params.append(value)
        detail[key] = value
    if state == "retry":
        delay_seconds = min(300 * (2 ** max(int(row["attempt_count"]) - 1, 0)), 21600)
        next_run_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        assignments.extend(
            [
                "lease_owner = NULL",
                "lease_expires_at = NULL",
                "next_run_at = ?",
                "finished_at = NULL",
            ]
        )
        params.append(next_run_at)
        detail["retry_delay_seconds"] = delay_seconds
        detail["next_run_at"] = next_run_at
    elif state in PUBLICATION_JOB_STOPPED_STATES:
        assignments.extend(["finished_at = ?", "lease_owner = NULL", "lease_expires_at = NULL"])
        params.append(now)
        assignments.append("next_run_at = NULL")
    else:
        assignments.append("next_run_at = NULL")
    params.append(job_id)
    conn.execute(
        f"UPDATE publication_jobs SET {', '.join(assignments)} WHERE job_id = ?",
        params,
    )
    conn.execute(
        """
        INSERT INTO publication_job_events (job_id, from_state, to_state, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (job_id, row["state"], state, json.dumps(detail), now),
    )
    if state in {"pr_open", "duplicate", "rejected"}:
        conn.execute(
            """
            UPDATE articles SET processed_at = COALESCE(processed_at, ?), updated_at = ?
            WHERE article_key = ?
            """,
            (now, now, row["article_key"]),
        )
    conn.commit()
    updated = conn.execute("SELECT * FROM publication_jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    return {"job": dict(updated)}


def requeue_publication_job(
    *, db_path: Path, job_id: int, reason: str
) -> dict[str, Any]:
    reason = reason.strip()
    if not reason:
        raise ValueError("requeue reason must not be empty")
    conn = ensure_db(db_path)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM publication_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Publication job not found: {job_id}")
    allowed_states = {"validated", "needs_review", "failed", "rejected"}
    if row["state"] not in allowed_states:
        conn.close()
        raise ValueError(
            f"Publication job {job_id} cannot be requeued from state {row['state']}"
        )
    now = utc_now_iso()
    prior_outputs = {
        key: row[key]
        for key in ("run_id", "packet_path", "target_path", "branch", "commit_sha", "pr_url")
        if row[key]
    }
    conn.execute(
        """
        UPDATE publication_jobs
        SET state = 'queued', attempt_count = 0,
            lease_owner = NULL, lease_expires_at = NULL, next_run_at = NULL,
            run_id = NULL, packet_path = NULL, target_path = NULL,
            branch = NULL, commit_sha = NULL, pr_url = NULL,
            result_json = NULL, error = NULL, started_at = NULL,
            finished_at = NULL, updated_at = ?
        WHERE job_id = ?
        """,
        (now, job_id),
    )
    conn.execute(
        """
        INSERT INTO publication_job_events
            (job_id, from_state, to_state, detail_json, created_at)
        VALUES (?, ?, 'queued', ?, ?)
        """,
        (
            job_id,
            row["state"],
            json.dumps(
                {"action": "explicit_requeue", "reason": reason, "prior_outputs": prior_outputs}
            ),
            now,
        ),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM publication_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    conn.close()
    return {"job": dict(updated), "prior_outputs": prior_outputs}


def list_publication_jobs(*, db_path: Path, state: str, limit: int) -> dict[str, Any]:
    conn = ensure_db(db_path)
    where = "" if state == "all" else "WHERE j.state = ?"
    params: tuple[Any, ...] = () if state == "all" else (state,)
    rows = conn.execute(
        f"""
        SELECT j.*, a.title, a.score, a.status AS article_status
        FROM publication_jobs j
        JOIN articles a ON a.article_key = j.article_key
        {where}
        ORDER BY j.job_id DESC
        LIMIT ?
        """,
        (*params, max(limit, 0)),
    ).fetchall()
    conn.close()
    return {"state": state, "jobs": [dict(row) for row in rows]}


def backfill_paper_keys(
    *,
    db_path: Path,
    status: str,
    limit: int,
    apply: bool,
) -> dict[str, Any]:
    conn = ensure_db(db_path)
    where = "paper_key IS NULL"
    params: list[Any] = []
    if status != "all":
        where += " AND status = ?"
        params.append(status)
    rows = conn.execute(
        f"""
        SELECT article_key, title, article_url, scholar_url, status
        FROM articles
        WHERE {where}
        ORDER BY score DESC, created_at, article_key
        LIMIT ?
        """,
        (*params, max(limit, 0)),
    ).fetchall()
    preview: list[dict[str, str]] = []
    linked = 0
    skipped = 0
    for row in rows:
        source_url = (row["article_url"] or row["scholar_url"] or "").strip()
        if not source_url:
            skipped += 1
            continue
        key, canonical_url, _doi, _pmid = paper_identity(
            title=row["title"],
            article_url=source_url,
        )
        if len(preview) < 20:
            preview.append(
                {
                    "article_key": row["article_key"],
                    "paper_key": key,
                    "canonical_url": canonical_url,
                    "title": row["title"],
                }
            )
        if apply:
            paper = upsert_paper_record(
                conn,
                title=row["title"],
                article_url=source_url,
            )
            conn.execute(
                "UPDATE articles SET paper_key = ?, updated_at = ? WHERE article_key = ?",
                (paper["paper_key"], utc_now_iso(), row["article_key"]),
            )
        linked += 1
    if apply:
        conn.commit()
    remaining = conn.execute(
        f"SELECT COUNT(*) AS count FROM articles WHERE {where}", params
    ).fetchone()["count"]
    conn.close()
    return {
        "applied": apply,
        "status": status,
        "examined": len(rows),
        "linkable": linked,
        "skipped_without_url": skipped,
        "remaining_after_apply" if apply else "remaining": remaining,
        "preview": preview,
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
        elif args.command == "papers":
            result = list_papers(
                db_path=db_path,
                status=args.status,
                workflow_state=args.workflow_state,
                limit=args.limit,
            )
        elif args.command == "find-paper":
            result = find_paper(
                db_path=db_path,
                identifier=args.identifier,
            )
        elif args.command == "upsert-paper":
            result = upsert_external_paper(
                db_path=db_path,
                title=args.title,
                url=args.url,
                doi=args.doi,
                pmid=args.pmid,
                workflow_state=args.workflow_state,
                matched_content_path=args.matched_content_path,
            )
        elif args.command == "set-paper-state":
            result = set_paper_state(
                db_path=db_path,
                identifier=args.identifier,
                state=args.state,
                matched_content_path=args.matched_content_path,
                commit=args.commit,
                pr=args.pr,
                archive_path=args.archive_path,
            )
        elif args.command == "mark-published":
            result = mark_paper_published(
                db_path=db_path,
                identifier=args.identifier,
                matched_content_path=args.matched_content_path,
                commit=args.commit,
                pr=args.pr,
            )
        elif args.command == "attach-archive":
            result = attach_archive(
                db_path=db_path,
                identifier=args.identifier,
                archive_path=args.archive_path,
            )
        elif args.command == "mark-processed":
            result = mark_article_processed(
                db_path=db_path,
                article_url=args.article_url,
            )
        elif args.command == "enqueue-publication":
            result = enqueue_publication_job(
                db_path=db_path,
                identifier=args.identifier,
                max_attempts=args.max_attempts,
                domain=args.domain,
                claim_policy=args.claim_policy,
            )
        elif args.command == "enqueue-publication-backlog":
            result = enqueue_publication_backlog(
                db_path=db_path,
                status=args.status,
                min_score=args.min_score,
                limit=args.limit,
                max_attempts=args.max_attempts,
                domain=args.domain,
                claim_policy=args.claim_policy,
            )
        elif args.command == "claim-publication":
            result = claim_publication_job(
                db_path=db_path,
                worker=args.worker,
                lease_seconds=args.lease_seconds,
            )
        elif args.command == "publication-jobs":
            result = list_publication_jobs(
                db_path=db_path,
                state=args.state,
                limit=args.limit,
            )
        elif args.command == "set-publication-job-state":
            result = set_publication_job_state(
                db_path=db_path,
                job_id=args.job_id,
                state=args.state,
                worker=args.worker,
                paper_key=args.paper_key,
                packet_path=args.packet_path,
                target_path=args.target_path,
                branch=args.branch,
                commit=args.commit,
                pr=args.pr,
                error=args.error,
                result_json=args.result_json,
                run_id=args.run_id,
            )
        elif args.command == "requeue-publication":
            result = requeue_publication_job(
                db_path=db_path,
                job_id=args.job_id,
                reason=args.reason,
            )
        elif args.command == "backfill-paper-keys":
            result = backfill_paper_keys(
                db_path=db_path,
                status=args.status,
                limit=args.limit,
                apply=args.apply,
            )
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "result": result}, indent=2))
    return 0
