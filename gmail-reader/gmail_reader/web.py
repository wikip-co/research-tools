from __future__ import annotations

import argparse
import fcntl
import functools
import html
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any
from urllib.parse import parse_qs, urlencode, urlparse

from .app import (
    DEFAULT_DB_PATH,
    PUBLICATION_JOB_STATES,
    ensure_db,
    publication_queue_status,
    reconcile_publication_jobs,
    requeue_publication_job,
    utc_now_iso,
)

ARTICLE_STATUSES = ("selected", "review", "rejected", "invalid")
JOB_STATES = ("queued", "running", "completed", "failed")
PR_URL_PATTERN = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+")
MAX_FORM_BYTES = 1_000_000
LOCAL_NAME_SUFFIXES = (".lan", ".local", ".home", ".internal", ".home.arpa", ".localdomain")
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    ),
)
# One unattended agent run at a time: concurrent runs edit the same content
# checkout and open competing pull requests.
CODEX_RUN_LOCK = threading.Lock()
# A run holds CODEX_RUN_LOCK for its whole lifetime, so a hung run would block
# every later one. Runs past this deadline are stopped and marked failed.
DEFAULT_CODEX_TIMEOUT_SECONDS = 3 * 60 * 60
CODEX_STOP_GRACE_SECONDS = 15


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def codex_launcher_enabled() -> bool:
    return not env_flag("GMAIL_READER_WEB_DISABLE_CODEX")


def host_name(value: str) -> str:
    """Return the lower-case host part of a Host header or URL authority."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


@functools.lru_cache(maxsize=1)
def machine_host_names() -> frozenset[str]:
    # getfqdn can wait on DNS; resolve once, not on every request.
    return frozenset({"localhost", socket.gethostname().lower(), socket.getfqdn().lower()})


def allowed_host_names() -> set[str]:
    names = set(machine_host_names())
    extra = os.environ.get("GMAIL_READER_WEB_ALLOWED_HOSTS", "")
    names.update(item.strip().lower() for item in extra.split(",") if item.strip())
    return names


def is_allowed_host(host_header: str) -> bool:
    """Accept only names that can belong to this machine.

    A public DNS name pointed at a LAN address (DNS rebinding) makes a browser
    treat this UI as same-origin with an attacker's page. IP literals, bare
    host names, local-only suffixes and explicitly allowed names cannot be
    registered by a third party.
    """
    name = host_name(host_header)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if "." not in name or name in allowed_host_names():
        return True
    return name.endswith(LOCAL_NAME_SUFFIXES)


def is_same_origin_request(headers: Any) -> bool:
    """Reject state-changing requests that a different site asked the browser to send.

    Non-browser clients send none of these headers and are allowed; browsers
    always send at least one of them on a cross-site POST.
    """
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    host = (headers.get("Host") or "").strip().lower()
    for header in ("Origin", "Referer"):
        value = (headers.get(header) or "").strip()
        if not value:
            continue
        if value == "null":
            return False
        return urlparse(value).netloc.lower() == host
    return True


def safe_return_to(value: str) -> str:
    """Only redirect back into this application."""
    if value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value and "\r" not in value:
        return value
    return "/"


def env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default


def default_workspace_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        # The coordinator is a flat workspace described by workspace-repos.tsv;
        # .gitmodules marks the layout that preceded it.
        if (parent / "research-tools").is_dir() and (
            (parent / "workspace-repos.tsv").is_file() or (parent / ".gitmodules").is_file()
        ):
            return parent
    return current.parents[2]


def ensure_web_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_jobs (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            article_count INTEGER NOT NULL DEFAULT 0,
            command_json TEXT NOT NULL DEFAULT '[]',
            prompt TEXT NOT NULL DEFAULT '',
            log TEXT NOT NULL DEFAULT '',
            exit_code INTEGER,
            pr_url TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_job_items (
            job_id INTEGER NOT NULL,
            article_key TEXT NOT NULL,
            article_url TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (job_id, article_key),
            FOREIGN KEY (job_id) REFERENCES article_jobs(job_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_article_jobs_state ON article_jobs(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_article_job_items_article ON article_job_items(article_key)")
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = ensure_db(db_path)
    ensure_web_schema(conn)
    return conn


def runner_lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".triage-ui.lock")


def acquire_runner_lock(db_path: Path) -> IO[str] | None:
    """Take the exclusive per-database runner lock, or return None if it is held.

    Only the lock holder may run Codex jobs or fail interrupted ones, so a second
    triage UI on the same database cannot rewrite the live instance's jobs. The
    kernel releases the lock when the holder exits, crashes included.
    """
    path = runner_lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def fail_interrupted_jobs(conn: sqlite3.Connection) -> int:
    """Close out jobs whose runner thread died with a previous server process.

    Call this only while holding the runner lock (acquire_runner_lock): with the
    lock held no other live process can own a queued or running job.
    """
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE article_jobs
        SET state = 'failed', finished_at = ?,
            error = 'Interrupted: the triage UI restarted before this job finished. '
                    || 'Check the content repository for a partial branch before retrying.'
        WHERE state IN ('queued', 'running')
        """,
        (now,),
    )
    conn.commit()
    return int(cur.rowcount)


def active_job_article_keys(conn: sqlite3.Connection, article_keys: list[str]) -> set[str]:
    if not article_keys:
        return set()
    placeholders = ",".join("?" for _ in article_keys)
    rows = conn.execute(
        f"""
        SELECT DISTINCT ji.article_key
        FROM article_job_items ji
        JOIN article_jobs j ON j.job_id = ji.job_id
        WHERE j.state IN ('queued', 'running') AND ji.article_key IN ({placeholders})
        """,
        article_keys,
    ).fetchall()
    return {row["article_key"] for row in rows}


def fetch_publication_jobs(
    conn: sqlite3.Connection, state: str = "all", limit: int = 100
) -> list[dict[str, Any]]:
    where = "" if state == "all" else "WHERE j.state = ?"
    values: list[Any] = [] if state == "all" else [state]
    rows = conn.execute(
        f"""
        SELECT j.job_id, j.state, j.attempt_count, j.max_attempts, j.lease_owner,
               j.lease_expires_at, j.next_run_at, j.domain, j.source_url, j.pr_url,
               j.target_path, j.error, j.created_at, j.updated_at, j.finished_at,
               a.title
        FROM publication_jobs j
        LEFT JOIN articles a ON a.article_key = j.article_key
        {where}
        ORDER BY j.job_id DESC
        LIMIT ?
        """,
        [*values, limit],
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_publication_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT j.*, a.title FROM publication_jobs j
        LEFT JOIN articles a ON a.article_key = j.article_key
        WHERE j.job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if not row:
        return None
    job = dict(row)
    job["events"] = [
        dict(event)
        for event in conn.execute(
            """
            SELECT event_id, from_state, to_state, detail_json, created_at
            FROM publication_job_events WHERE job_id = ? ORDER BY event_id
            """,
            (job_id,),
        )
    ]
    return job


def health_payload(db_path: Path) -> dict[str, Any]:
    """Liveness plus the facts an operator or agent needs before trusting the queue."""
    payload: dict[str, Any] = {"ok": True, "checked_at": utc_now_iso(), "db_path": str(db_path)}
    try:
        conn = connect(db_path)
        try:
            payload["integrity"] = conn.execute("PRAGMA quick_check").fetchone()[0]
            payload["articles"] = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            payload["unprocessed_selected"] = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE status = 'selected' AND processed_at IS NULL"
            ).fetchone()[0]
            payload["latest_message_imported_at"] = conn.execute(
                "SELECT MAX(imported_at) FROM messages"
            ).fetchone()[0]
            payload["codex_jobs_active"] = conn.execute(
                "SELECT COUNT(*) FROM article_jobs WHERE state IN ('queued', 'running')"
            ).fetchone()[0]
        finally:
            conn.close()
        queue = publication_queue_status(db_path=db_path)
        payload["queue"] = {
            key: queue[key]
            for key in (
                "total", "counts", "claimable_now", "awaiting_human",
                "expired_leases", "unrecoverable_without_reconcile",
            )
        }
        payload["codex_launcher_enabled"] = codex_launcher_enabled()
        if payload["integrity"] != "ok":
            payload["ok"] = False
    except Exception as exc:
        payload["ok"] = False
        payload["error"] = str(exc)
    return payload


def html_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def json_script_data(value: Any) -> str:
    return json.dumps(value).replace("</", "<\\/")


def parse_int(value: str | None, default: int, *, minimum: int = 0, maximum: int = 500) -> int:
    try:
        number = int(value or default)
    except ValueError:
        return default
    return max(minimum, min(maximum, number))


def selected_values(params: dict[str, list[str]], name: str) -> list[str]:
    return [value for value in params.get(name, []) if value.strip()]


def article_filters(params: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "status": (params.get("status", ["selected"])[0] or "selected"),
        "processed": (params.get("processed", ["unprocessed"])[0] or "unprocessed"),
        "alert_name": (params.get("alert_name", [""])[0] or "").strip(),
        "q": (params.get("q", [""])[0] or "").strip(),
        "min_score": parse_int(params.get("min_score", ["0"])[0], 0, minimum=-100, maximum=1000),
        "limit": parse_int(params.get("limit", ["50"])[0], 50, minimum=10, maximum=250),
        "offset": parse_int(params.get("offset", ["0"])[0], 0, minimum=0, maximum=1_000_000),
    }


def build_article_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if filters["status"] != "all":
        clauses.append("a.status = ?")
        values.append(filters["status"])
    if filters["processed"] == "processed":
        clauses.append("a.processed_at IS NOT NULL")
    elif filters["processed"] == "unprocessed":
        clauses.append("a.processed_at IS NULL")
    if filters["alert_name"]:
        clauses.append("a.alert_name = ?")
        values.append(filters["alert_name"])
    if filters["min_score"]:
        clauses.append("a.score >= ?")
        values.append(filters["min_score"])
    if filters["q"]:
        like = f"%{filters['q']}%"
        clauses.append(
            "(a.title LIKE ? OR a.authors LIKE ? OR a.publication_info LIKE ? OR a.snippet LIKE ? OR a.article_url LIKE ?)"
        )
        values.extend([like, like, like, like, like])
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", values


def fetch_articles(conn: sqlite3.Connection, filters: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    where_sql, values = build_article_where(filters)
    count_row = conn.execute(f"SELECT COUNT(*) AS count FROM articles a {where_sql}", values).fetchone()
    total = int(count_row["count"] if count_row else 0)
    rows = conn.execute(
        f"""
        SELECT
            a.article_key, a.paper_key, a.alert_name, a.rank_in_email, a.title, a.authors,
            a.publication_info, a.snippet, a.article_url, a.pdf_url, a.score, a.status,
            a.is_open_access, a.processed_at, a.created_at, a.updated_at,
            p.workflow_state, p.matched_content_path, p.published_pr,
            (
                SELECT j.state
                FROM article_job_items ji
                JOIN article_jobs j ON j.job_id = ji.job_id
                WHERE ji.article_key = a.article_key
                ORDER BY ji.job_id DESC
                LIMIT 1
            ) AS latest_job_state,
            (
                SELECT j.job_id
                FROM article_job_items ji
                JOIN article_jobs j ON j.job_id = ji.job_id
                WHERE ji.article_key = a.article_key
                ORDER BY ji.job_id DESC
                LIMIT 1
            ) AS latest_job_id
        FROM articles a
        LEFT JOIN papers p ON p.paper_key = a.paper_key
        {where_sql}
        ORDER BY a.score DESC, a.created_at DESC, a.alert_name ASC, a.rank_in_email ASC
        LIMIT ? OFFSET ?
        """,
        [*values, filters["limit"], filters["offset"]],
    ).fetchall()
    return [dict(row) for row in rows], total


def fetch_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT alert_name, COUNT(*) AS article_count
        FROM articles
        GROUP BY alert_name
        ORDER BY alert_name ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_jobs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT job_id, state, created_at, started_at, finished_at, article_count,
               exit_code, pr_url, error
        FROM article_jobs
        ORDER BY job_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM article_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def job_log_delta(conn: sqlite3.Connection, job_id: int, offset: int) -> dict[str, Any] | None:
    job = fetch_job(conn, job_id)
    if not job:
        return None
    log = job.get("log") or job.get("error") or ""
    log_length = len(log)
    safe_offset = max(0, offset)
    reset = False
    if safe_offset > log_length:
        safe_offset = 0
        reset = True
    return {
        "job_id": job_id,
        "state": job.get("state") or "",
        "offset": log_length,
        "chunk": log[safe_offset:],
        "reset": reset,
        "exit_code": job.get("exit_code"),
        "pr_url": job.get("pr_url") or "",
        "error": job.get("error") or "",
    }


def fetch_job_items(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT article_key, article_url, title, status
        FROM article_job_items
        WHERE job_id = ?
        ORDER BY title ASC
        """,
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_articles_by_key(conn: sqlite3.Connection, article_keys: list[str]) -> list[dict[str, Any]]:
    if not article_keys:
        return []
    placeholders = ",".join("?" for _ in article_keys)
    rows = conn.execute(
        f"""
        SELECT a.article_key, a.paper_key, a.alert_name, a.title, a.authors,
               a.publication_info, a.snippet, a.article_url, a.pdf_url, a.score,
               a.status, a.is_open_access, a.processed_at,
               p.workflow_state, p.matched_content_path, p.published_pr
        FROM articles a
        LEFT JOIN papers p ON p.paper_key = a.paper_key
        WHERE a.article_key IN ({placeholders})
        ORDER BY a.score DESC, a.created_at DESC
        """,
        article_keys,
    ).fetchall()
    return [dict(row) for row in rows]


def update_article_status(conn: sqlite3.Connection, article_keys: list[str], status: str) -> int:
    if status not in ARTICLE_STATUSES:
        raise ValueError(f"Unsupported status: {status}")
    if not article_keys:
        return 0
    placeholders = ",".join("?" for _ in article_keys)
    now = utc_now_iso()
    cur = conn.execute(
        f"UPDATE articles SET status = ?, updated_at = ? WHERE article_key IN ({placeholders})",
        [status, now, *article_keys],
    )
    conn.commit()
    return int(cur.rowcount)


def mark_articles_processed(conn: sqlite3.Connection, article_keys: list[str]) -> int:
    if not article_keys:
        return 0
    placeholders = ",".join("?" for _ in article_keys)
    now = utc_now_iso()
    cur = conn.execute(
        f"""
        UPDATE articles
        SET processed_at = COALESCE(processed_at, ?), updated_at = ?
        WHERE article_key IN ({placeholders})
        """,
        [now, now, *article_keys],
    )
    conn.commit()
    return int(cur.rowcount)


def create_job(conn: sqlite3.Connection, articles: list[dict[str, Any]], prompt: str, command: list[str]) -> int:
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO article_jobs (state, created_at, article_count, command_json, prompt)
        VALUES ('queued', ?, ?, ?, ?)
        """,
        (now, len(articles), json.dumps(command), prompt),
    )
    job_id = int(cur.lastrowid)
    conn.executemany(
        """
        INSERT INTO article_job_items (job_id, article_key, article_url, title, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                job_id,
                article["article_key"],
                article.get("article_url") or "",
                article.get("title") or "",
                article.get("status") or "",
                now,
            )
            for article in articles
        ],
    )
    conn.commit()
    return job_id


def append_job_log(conn: sqlite3.Connection, job_id: int, chunk: str, *, max_chars: int = 200000) -> None:
    if not chunk:
        return
    row = conn.execute("SELECT log FROM article_jobs WHERE job_id = ?", (job_id,)).fetchone()
    current = row["log"] if row else ""
    updated = (current + chunk)[-max_chars:]
    conn.execute("UPDATE article_jobs SET log = ? WHERE job_id = ?", (updated, job_id))
    conn.commit()


def build_codex_prompt(
    *,
    workspace_root: Path,
    db_path: Path,
    articles: list[dict[str, Any]],
) -> str:
    payload = [
        {
            "article_key": item.get("article_key"),
            "paper_key": item.get("paper_key"),
            "title": item.get("title"),
            "authors": item.get("authors"),
            "publication_info": item.get("publication_info"),
            "snippet": item.get("snippet"),
            "article_url": item.get("article_url"),
            "pdf_url": item.get("pdf_url"),
            "score": item.get("score"),
            "status": item.get("status"),
            "alert_name": item.get("alert_name"),
            "workflow_state": item.get("workflow_state"),
            "matched_content_path": item.get("matched_content_path"),
        }
        for item in articles
    ]
    return f"""You are Codex running inside the Research workspace at {workspace_root}.

Process the selected research article rows below and submit a draft PR to the `content` repo with the markdown changes.

Required workflow:
1. Read https://agent.wikip.co/research-publishing-style-guide/ before editing content.
   For `content/Natural Healing/**`, also read
   https://agent.wikip.co/natural-healing-content-style-guide/; that category guide is
   authoritative and requires near-verbatim, one-idea bullets with citations.
2. Use `research-tools/agent-workflow intake` or the underlying tools to scrape each source, check duplicates, and match existing content.
3. Prefer appending to an existing `content` markdown article when the source naturally belongs there. Create a new article only when there is no good existing home.
4. Keep edits inside the `content` repo unless tooling metadata is truly required.
5. Preserve existing frontmatter, heading, bullet, citation, and footnote style.
6. Cite every research-backed claim with footnotes. Do not overstate findings beyond the study type and evidence.
7. Use `research-tools/agent-workflow publish-pr --draft` after applying article changes so I can review the PR.
8. In your final message, include the PR URL, changed article paths, and any selected articles that you could not process.

Do not mark the SQLite rows processed yourself; this web job runner will set
`processed_at` only after a successful exit that reports a draft PR URL.

SQLite DB path: {db_path}

Selected article rows:
```json
{json.dumps(payload, indent=2)}
```
"""


def codex_command(workspace_root: Path) -> list[str]:
    codex_bin = os.environ.get("CODEX_BIN", "").strip() or shutil.which("codex") or "codex"
    extra = os.environ.get("CODEX_WEB_EXTRA_ARGS", "").strip()
    command = [
        codex_bin,
        "--sandbox",
        "danger-full-access",
        "--ask-for-approval",
        "never",
        "exec",
        "-C",
        str(workspace_root),
    ]
    if extra:
        command.extend(extra.split())
    command.append("-")
    return command


def codex_timeout_seconds() -> float:
    raw = os.environ.get("CODEX_WEB_TIMEOUT_SECONDS", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_CODEX_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_CODEX_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_CODEX_TIMEOUT_SECONDS


def stop_process_group(process: subprocess.Popen[str], grace_seconds: float = CODEX_STOP_GRACE_SECONDS) -> None:
    """Terminate, then kill, the child's whole process group.

    Codex starts its own tool subprocesses; signalling only the direct child can
    leave a grandchild holding stdout open, so the log loop would never end.
    """
    for sig, wait in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, None)):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if wait is None:
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_job(job_id: int, db_path: Path, workspace_root: Path, article_keys: list[str]) -> None:
    with CODEX_RUN_LOCK:
        _run_job_locked(job_id, db_path, workspace_root, article_keys)


def _run_job_locked(job_id: int, db_path: Path, workspace_root: Path, article_keys: list[str]) -> None:
    conn = connect(db_path)
    try:
        job = fetch_job(conn, job_id)
        if not job or job["state"] != "queued":
            return
        command = json.loads(job["command_json"])
        prompt = job["prompt"]
        conn.execute(
            "UPDATE article_jobs SET state = 'running', started_at = ? WHERE job_id = ?",
            (utc_now_iso(), job_id),
        )
        conn.commit()
        process = subprocess.Popen(
            command,
            cwd=workspace_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        timeout = codex_timeout_seconds()
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            stop_process_group(process)

        watchdog = threading.Timer(timeout, expire)
        watchdog.daemon = True
        watchdog.start()
        log_parts: list[str] = []
        try:
            if process.stdin is not None:
                process.stdin.write(prompt)
                process.stdin.close()
            if process.stdout is not None:
                for line in process.stdout:
                    log_parts.append(line)
                    append_job_log(conn, job_id, line)
            return_code = process.wait()
        finally:
            watchdog.cancel()
            if process.stdout is not None:
                process.stdout.close()
        log = "".join(log_parts).strip()
        pr_matches = PR_URL_PATTERN.findall(log)
        pr_url = pr_matches[-1] if pr_matches else ""
        if timed_out.is_set():
            state = "failed"
            error = (
                f"Codex did not finish within {timeout:g}s (CODEX_WEB_TIMEOUT_SECONDS) and was stopped. "
                "Check the content repository for a partial branch before retrying."
            )
        elif return_code == 0 and pr_url:
            mark_articles_processed(conn, article_keys)
            state = "completed"
            error = ""
        elif return_code == 0:
            state = "failed"
            error = "Codex exited successfully but did not report a pull request URL"
        else:
            state = "failed"
            error = f"Codex exited with {return_code}"
        conn.execute(
            """
            UPDATE article_jobs
            SET state = ?, finished_at = ?, log = ?, exit_code = ?, pr_url = ?, error = ?
            WHERE job_id = ?
            """,
            (state, utc_now_iso(), log[-200000:], return_code, pr_url, error, job_id),
        )
        conn.commit()
    except Exception as exc:
        conn.execute(
            """
            UPDATE article_jobs
            SET state = 'failed', finished_at = ?, error = ?, log = log || ?
            WHERE job_id = ?
            """,
            (utc_now_iso(), str(exc), f"\n\nWEB RUNNER ERROR: {exc}", job_id),
        )
        conn.commit()
    finally:
        conn.close()


class TriageServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], *, db_path: Path, workspace_root: Path):
        super().__init__(server_address, handler)
        self.db_path = db_path
        self.workspace_root = workspace_root


class Handler(BaseHTTPRequestHandler):
    server: TriageServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_security_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)

    def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def reject(self, status: HTTPStatus, message: str) -> None:
        self.send_html(render_page(status.phrase, f"<p>{html_escape(message)}</p>"), status)

    def host_allowed(self) -> bool:
        if is_allowed_host(self.headers.get("Host", "")):
            return True
        self.reject(
            HTTPStatus.FORBIDDEN,
            "This host name is not recognized as this machine. Use its IP address or local "
            "name, or add the name to GMAIL_READER_WEB_ALLOWED_HOSTS.",
        )
        return False

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def send_head_ok(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def read_form(self) -> dict[str, list[str]] | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_FORM_BYTES:
            self.reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Form body is missing a valid length or is too large.")
            return None
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw, keep_blank_values=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/healthz":
            # Liveness stays reachable by any name so monitors need no allowlist.
            payload = health_payload(self.server.db_path)
            self.send_json(payload, HTTPStatus.OK if payload["ok"] else HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if not self.host_allowed():
            return
        if parsed.path == "/":
            self.send_html(render_index(self.server.db_path, params))
        elif parsed.path == "/api/queue":
            self.send_json(publication_queue_status(db_path=self.server.db_path))
        elif parsed.path == "/api/articles":
            filters = article_filters(params)
            conn = connect(self.server.db_path)
            try:
                articles, total = fetch_articles(conn, filters)
            finally:
                conn.close()
            self.send_json({"filters": filters, "total": total, "articles": articles})
        elif parsed.path == "/queue":
            self.send_html(render_queue(self.server.db_path, params))
        elif (match := re.fullmatch(r"/queue/(\d+)", parsed.path)):
            body = render_queue_job(self.server.db_path, int(match.group(1)))
            if body is None:
                self.reject(HTTPStatus.NOT_FOUND, "Publishing job not found.")
            else:
                self.send_html(body)
        elif parsed.path == "/jobs":
            self.send_html(render_jobs(self.server.db_path))
        elif (match := re.fullmatch(r"/jobs/(\d+)/log", parsed.path)):
            job_id = parse_int(match.group(1), 0, minimum=0, maximum=1_000_000)
            offset = parse_int(params.get("offset", ["0"])[0], 0, minimum=0, maximum=1_000_000)
            conn = connect(self.server.db_path)
            try:
                payload = job_log_delta(conn, job_id, offset)
            finally:
                conn.close()
            if payload is None:
                self.send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json(payload)
        elif (match := re.fullmatch(r"/jobs/(\d+)", parsed.path)):
            body = render_job_detail(self.server.db_path, int(match.group(1)))
            if body is None:
                self.reject(HTTPStatus.NOT_FOUND, "Job not found.")
            else:
                self.send_html(body)
        else:
            self.send_html(render_page("Not Found", "<p>Not found.</p>"), HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/jobs", "/queue", "/healthz"} or parsed.path.startswith(("/jobs/", "/queue/")):
            self.send_head_ok()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self.host_allowed():
            return
        if not is_same_origin_request(self.headers):
            self.reject(HTTPStatus.FORBIDDEN, "Cross-site requests cannot change triage state.")
            return
        form = self.read_form()
        if form is None:
            return
        if parsed.path == "/queue/action":
            self.handle_queue_action(form)
            return
        if parsed.path != "/articles/action":
            self.send_html(render_page("Not Found", "<p>Not found.</p>"), HTTPStatus.NOT_FOUND)
            return
        action = (form.get("action", [""])[0] or "").strip()
        article_keys = selected_values(form, "article_key")
        return_to = safe_return_to(form.get("return_to", ["/"])[0] or "/")
        conn = connect(self.server.db_path)
        try:
            if action.startswith("status:"):
                try:
                    update_article_status(conn, article_keys, action.split(":", 1)[1])
                except ValueError as exc:
                    self.reject(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self.redirect(return_to)
                return
            if action == "mark_processed":
                mark_articles_processed(conn, article_keys)
                self.redirect(return_to)
                return
            if action == "process_codex":
                if not codex_launcher_enabled():
                    self.reject(
                        HTTPStatus.FORBIDDEN,
                        "The Codex launcher is disabled (GMAIL_READER_WEB_DISABLE_CODEX).",
                    )
                    return
                busy = active_job_article_keys(conn, article_keys)
                articles = [
                    article
                    for article in fetch_articles_by_key(conn, article_keys)
                    if article["article_key"] not in busy
                ]
                if not articles:
                    self.redirect(return_to)
                    return
                prompt = build_codex_prompt(
                    workspace_root=self.server.workspace_root,
                    db_path=self.server.db_path,
                    articles=articles,
                )
                command = codex_command(self.server.workspace_root)
                job_id = create_job(conn, articles, prompt, command)
                thread = threading.Thread(
                    target=run_job,
                    args=(job_id, self.server.db_path, self.server.workspace_root, [a["article_key"] for a in articles]),
                    daemon=True,
                )
                thread.start()
                self.redirect(f"/jobs/{job_id}")
                return
            self.redirect(return_to)
        finally:
            conn.close()

    def handle_queue_action(self, form: dict[str, list[str]]) -> None:
        action = (form.get("action", [""])[0] or "").strip()
        try:
            if action == "reconcile":
                reconcile_publication_jobs(db_path=self.server.db_path)
                self.redirect("/queue")
                return
            if action == "requeue":
                job_id = int(form.get("job_id", ["0"])[0] or "0")
                reason = (form.get("reason", [""])[0] or "").strip()
                requeue_publication_job(db_path=self.server.db_path, job_id=job_id, reason=reason)
                self.redirect(f"/queue/{job_id}")
                return
        except ValueError as exc:
            self.reject(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self.redirect("/queue")


def render_page(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    :root {{ color-scheme: light dark; --line: #d8dee4; --bg: #f6f8fa; --surface: #ffffff; --ink: #1f2328; --muted: #656d76; --accent: #0969da; --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --line: #30363d; --bg: #161b22; --surface: #0d1117; --ink: #e6edf3; --muted: #9198a1; --accent: #4493f8; --ok: #3fb950; --warn: #d29922; --bad: #f85149; }} }}
    body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; color: var(--ink); background: var(--surface); font-size: 14px; }}
    header {{ display: flex; align-items: center; gap: 16px; padding: 12px 18px; border-bottom: 1px solid var(--line); background: var(--bg); position: sticky; top: 0; z-index: 5; }}
    header a {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
    main {{ padding: 16px 18px 32px; }}
    form.filters {{ display: grid; grid-template-columns: minmax(220px, 1fr) repeat(6, max-content); gap: 8px; align-items: end; margin-bottom: 14px; }}
    label {{ display: grid; gap: 4px; color: var(--muted); font-size: 12px; }}
    input, select, button, textarea {{ font: inherit; }}
    input, select {{ border: 1px solid var(--line); border-radius: 6px; padding: 7px 8px; background: var(--surface); color: var(--ink); }}
    button {{ border: 1px solid var(--line); border-radius: 6px; padding: 7px 10px; background: var(--surface); color: var(--ink); cursor: pointer; }}
    button.primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
    button.danger {{ color: var(--bad); }}
    a {{ color: var(--accent); }}
    a[aria-disabled="true"] {{ color: var(--muted); pointer-events: none; text-decoration: none; }}
    .cards {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0; }}
    .card {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; background: var(--bg); min-width: 110px; }}
    .card b {{ display: block; font-size: 22px; }}
    .card.alert {{ border-color: var(--bad); }}
    .notice {{ border: 1px solid var(--warn); border-radius: 6px; padding: 8px 12px; margin: 10px 0; }}
    .wrap {{ overflow-wrap: anywhere; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0; }}
    .muted {{ color: var(--muted); }}
    table {{ border-collapse: collapse; width: 100%; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; vertical-align: top; text-align: left; }}
    th {{ background: var(--bg); font-size: 12px; color: var(--muted); position: sticky; top: 49px; z-index: 4; }}
    tr:hover td {{ background: var(--bg); }}
    .title {{ font-weight: 650; }}
    .snippet {{ color: var(--muted); margin-top: 4px; max-height: 4.5em; overflow: hidden; }}
    .pill {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; font-size: 12px; background: var(--surface); margin: 0 4px 4px 0; }}
    .status-selected {{ border-color: var(--ok); color: var(--ok); }}
    .status-review {{ border-color: var(--warn); color: var(--warn); }}
    .status-rejected, .status-invalid {{ border-color: var(--bad); color: var(--bad); }}
    .job-running, .job-queued, .job-leased, .job-scraping, .job-matching, .job-drafting, .job-validating, .job-publishing {{ border-color: var(--accent); color: var(--accent); }}
    .job-completed, .job-pr_open, .job-validated {{ border-color: var(--ok); color: var(--ok); }}
    .job-needs_review, .job-retry, .job-duplicate {{ border-color: var(--warn); color: var(--warn); }}
    .job-failed, .job-rejected {{ border-color: var(--bad); color: var(--bad); }}
    .links a {{ display: inline-block; margin-right: 8px; color: var(--accent); }}
    .pager {{ display: flex; gap: 8px; align-items: center; margin: 12px 0; }}
    pre {{ white-space: pre-wrap; overflow: auto; background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 12px; }}
    .job-log-header {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; margin: 20px 0 8px; }}
    .job-log-controls {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; color: var(--muted); font-size: 12px; }}
    .job-terminal {{ min-height: 420px; max-height: 68vh; overflow: auto; border: 1px solid #30363d; border-radius: 6px; padding: 12px; background: #0d1117; color: #e6edf3; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, Liberation Mono, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }}
    .job-terminal:focus {{ outline: 2px solid color-mix(in srgb, var(--accent), transparent 45%); outline-offset: 2px; }}
    .ansi-bold {{ font-weight: 700; }}
    .ansi-dim {{ opacity: .68; }}
    .ansi-italic {{ font-style: italic; }}
    .ansi-underline {{ text-decoration: underline; }}
    .ansi-fg-black {{ color: #8b949e; }}
    .ansi-fg-red {{ color: #ff7b72; }}
    .ansi-fg-green {{ color: #7ee787; }}
    .ansi-fg-yellow {{ color: #d29922; }}
    .ansi-fg-blue {{ color: #79c0ff; }}
    .ansi-fg-magenta {{ color: #d2a8ff; }}
    .ansi-fg-cyan {{ color: #76e3ea; }}
    .ansi-fg-white {{ color: #e6edf3; }}
    .ansi-bg-red {{ background: #da3633; }}
    .ansi-bg-green {{ background: #238636; }}
    .ansi-bg-yellow {{ background: #9e6a03; }}
    .ansi-bg-blue {{ background: #1f6feb; }}
    .ansi-bg-magenta {{ background: #8957e5; }}
    .ansi-bg-cyan {{ background: #0e7490; }}
    .ansi-bg-white {{ background: #6e7681; }}
    @media (max-width: 900px) {{ form.filters {{ grid-template-columns: 1fr 1fr; }} th:nth-child(5), td:nth-child(5) {{ display: none; }} }}
  </style>
</head>
<body>
  <header>
    <strong>Research Triage</strong>
    <a href="/">Articles</a>
    <a href="/queue">Publishing queue</a>
    <a href="/jobs">Codex jobs</a>
  </header>
  <main>{content}</main>
</body>
</html>"""


def render_index(db_path: Path, params: dict[str, list[str]]) -> str:
    filters = article_filters(params)
    conn = connect(db_path)
    try:
        articles, total = fetch_articles(conn, filters)
        alerts = fetch_alerts(conn)
    finally:
        conn.close()
    current_query = {
        key: value
        for key, value in filters.items()
        if key in {"status", "processed", "alert_name", "q", "min_score", "limit", "offset"}
    }
    return_to = "/?" + urlencode(current_query)
    next_query = {**current_query, "offset": filters["offset"] + filters["limit"]}
    prev_query = {**current_query, "offset": max(0, filters["offset"] - filters["limit"])}
    rows = "\n".join(render_article_row(article) for article in articles)
    status_options = "".join(
        option("all", filters["status"], "All statuses")
        + "".join(option(status, filters["status"], status.title()) for status in ARTICLE_STATUSES)
    )
    processed_options = "".join(
        option(value, filters["processed"], label)
        for value, label in (("unprocessed", "Unprocessed"), ("processed", "Processed"), ("all", "All"))
    )
    alert_options = option("", filters["alert_name"], "All alerts") + "".join(
        option(item["alert_name"], filters["alert_name"], f"{item['alert_name']} ({item['article_count']})")
        for item in alerts
    )
    codex_button = (
        '<button class="primary" name="action" value="process_codex" '
        'title="Starts an unattended Codex run with full access to this machine" '
        'onclick="return confirm(\'Start an unattended Codex run with full access to this machine '
        'for the checked rows?\')">Process with Codex (full access)</button>'
        if codex_launcher_enabled()
        else '<span class="muted">Codex launcher disabled</span>'
    )
    content = f"""
<form class="filters" method="get" action="/">
  <label>Search
    <input name="q" value="{html_escape(filters['q'])}" placeholder="title, snippet, URL">
  </label>
  <label>Status
    <select name="status">{status_options}</select>
  </label>
  <label>Processed
    <select name="processed">{processed_options}</select>
  </label>
  <label>Alert
    <select name="alert_name">{alert_options}</select>
  </label>
  <label>Min score
    <input name="min_score" value="{html_escape(filters['min_score'])}" size="5">
  </label>
  <label>Limit
    <input name="limit" value="{html_escape(filters['limit'])}" size="5">
  </label>
  <button class="primary" type="submit">Filter</button>
</form>

<div class="pager">
  <span class="muted">Showing {filters['offset'] + 1 if total else 0}-{min(filters['offset'] + filters['limit'], total)} of {total}</span>
  <a href="/?{urlencode(prev_query)}"{'' if filters['offset'] > 0 else ' aria-disabled="true" tabindex="-1"'}>Previous</a>
  <a href="/?{urlencode(next_query)}"{'' if filters['offset'] + filters['limit'] < total else ' aria-disabled="true" tabindex="-1"'}>Next</a>
</div>

<form method="post" action="/articles/action">
  <input type="hidden" name="return_to" value="{html_escape(return_to)}">
  <div class="toolbar">
    <button type="button" onclick="document.querySelectorAll('input[name=article_key]').forEach(cb => cb.checked = true)">Select page</button>
    <button type="button" onclick="document.querySelectorAll('input[name=article_key]').forEach(cb => cb.checked = false)">Clear</button>
    <button name="action" value="status:selected">Mark selected</button>
    <button name="action" value="status:review">Review</button>
    <button class="danger" name="action" value="status:rejected">Reject</button>
    <button class="danger" name="action" value="status:invalid">Invalid</button>
    <button name="action" value="mark_processed">Mark processed</button>
    {codex_button}
  </div>
  <table>
    <thead>
      <tr>
        <th style="width:34px"></th>
        <th style="width:42%">Article</th>
        <th style="width:15%">Alert</th>
        <th style="width:10%">Score</th>
        <th style="width:17%">State</th>
        <th style="width:16%">Links</th>
      </tr>
    </thead>
    <tbody>{rows or '<tr><td colspan="6">No rows match the current filters.</td></tr>'}</tbody>
  </table>
</form>
"""
    return render_page("Research Triage", content)


def option(value: str, selected: str, label: str) -> str:
    attr = " selected" if value == selected else ""
    return f'<option value="{html_escape(value)}"{attr}>{html_escape(label)}</option>'


def render_article_row(article: dict[str, Any]) -> str:
    status = article.get("status") or ""
    job_state = article.get("latest_job_state") or ""
    job_id = article.get("latest_job_id") or ""
    processed = article.get("processed_at") or ""
    workflow = article.get("workflow_state") or ""
    links = []
    if article.get("article_url"):
        links.append(f'<a href="{html_escape(article["article_url"])}" target="_blank" rel="noreferrer">article</a>')
    if article.get("pdf_url"):
        links.append(f'<a href="{html_escape(article["pdf_url"])}" target="_blank" rel="noreferrer">pdf</a>')
    if article.get("published_pr"):
        links.append(f'<a href="{html_escape(article["published_pr"])}" target="_blank" rel="noreferrer">PR</a>')
    if job_id:
        links.append(f'<a href="/jobs/{html_escape(job_id)}">job {html_escape(job_id)}</a>')
    state_bits = [
        f'<span class="pill status-{html_escape(status)}">{html_escape(status)}</span>',
    ]
    if workflow:
        state_bits.append(f'<span class="pill">{html_escape(workflow)}</span>')
    if processed:
        state_bits.append('<span class="pill status-selected">processed</span>')
    if job_state:
        state_bits.append(f'<span class="pill job-{html_escape(job_state)}">{html_escape(job_state)}</span>')
    return f"""
<tr>
  <td><input type="checkbox" name="article_key" value="{html_escape(article.get('article_key'))}"></td>
  <td>
    <div class="title">{html_escape(article.get('title'))}</div>
    <div class="muted">{html_escape(article.get('authors'))}</div>
    <div class="muted">{html_escape(article.get('publication_info'))}</div>
    <div class="snippet">{html_escape(article.get('snippet'))}</div>
  </td>
  <td>{html_escape(article.get('alert_name'))}</td>
  <td>{html_escape(article.get('score'))}</td>
  <td>{''.join(state_bits)}<div class="muted">{html_escape(processed)}</div></td>
  <td class="links">{''.join(links)}</td>
</tr>
"""


def render_jobs(db_path: Path) -> str:
    conn = connect(db_path)
    try:
        jobs = fetch_jobs(conn, 50)
    finally:
        conn.close()
    rows = "\n".join(
        f"""
        <tr>
          <td><a href="/jobs/{job['job_id']}">{job['job_id']}</a></td>
          <td><span class="pill job-{html_escape(job['state'])}">{html_escape(job['state'])}</span></td>
          <td>{html_escape(job['article_count'])}</td>
          <td>{html_escape(job['created_at'])}</td>
          <td>{html_escape(job['finished_at'])}</td>
          <td>{('<a href="' + html_escape(job['pr_url']) + '" target="_blank" rel="noreferrer">PR</a>') if job.get('pr_url') else html_escape(job.get('error', ''))}</td>
        </tr>
        """
        for job in jobs
    )
    content = f"""
<h1>Codex jobs</h1>
<p class="muted">Runs started from the Articles page. The local publisher's work is under <a href="/queue">Publishing queue</a>.</p>
<table>
  <thead><tr><th>Job</th><th>State</th><th>Rows</th><th>Created</th><th>Finished</th><th>Result</th></tr></thead>
  <tbody>{rows or '<tr><td colspan="6">No jobs yet.</td></tr>'}</tbody>
</table>
"""
    return render_page("Jobs", content)


def render_job_log_script() -> str:
    return """
<script>
(function () {
  const dataEl = document.getElementById("job-log-data");
  const terminal = document.getElementById("job-log-terminal");
  const plain = document.getElementById("job-log-plain");
  const status = document.getElementById("job-live-status");
  const pr = document.getElementById("job-live-pr");
  const follow = document.getElementById("job-follow-tail");
  const copy = document.getElementById("job-copy-log");
  if (!dataEl || !terminal || !plain || !status) return;

  const initial = JSON.parse(dataEl.textContent || "{}");
  const activeClasses = new Set();
  let offset = initial.offset || 0;
  let fullLog = initial.log || "";

  function removePrefix(prefix) {
    for (const value of Array.from(activeClasses)) {
      if (value.startsWith(prefix)) activeClasses.delete(value);
    }
  }

  function setSgr(codes) {
    if (!codes.length) codes = [0];
    for (const code of codes) {
      if (code === 0) activeClasses.clear();
      else if (code === 1) activeClasses.add("ansi-bold");
      else if (code === 2) activeClasses.add("ansi-dim");
      else if (code === 3) activeClasses.add("ansi-italic");
      else if (code === 4) activeClasses.add("ansi-underline");
      else if (code === 22) {
        activeClasses.delete("ansi-bold");
        activeClasses.delete("ansi-dim");
      } else if (code === 23) activeClasses.delete("ansi-italic");
      else if (code === 24) activeClasses.delete("ansi-underline");
      else if (code === 39) removePrefix("ansi-fg-");
      else if (code === 49) removePrefix("ansi-bg-");
      else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {
        const names = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
        removePrefix("ansi-fg-");
        activeClasses.add("ansi-fg-" + names[code % 10]);
      } else if (code >= 40 && code <= 47) {
        const names = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
        removePrefix("ansi-bg-");
        activeClasses.add("ansi-bg-" + names[code - 40]);
      }
    }
  }

  function appendAnsi(text, reset) {
    if (reset) {
      terminal.textContent = "";
      activeClasses.clear();
    }
    if (!text) return;

    const stickToBottom = follow.checked || terminal.scrollTop + terminal.clientHeight >= terminal.scrollHeight - 24;
    const fragment = document.createDocumentFragment();
    let span = document.createElement("span");
    let i = 0;

    function syncSpanClass() {
      span.className = Array.from(activeClasses).join(" ");
    }

    function flushSpan() {
      if (span.childNodes.length) fragment.appendChild(span);
      span = document.createElement("span");
      syncSpanClass();
    }

    syncSpanClass();
    while (i < text.length) {
      if (text.charCodeAt(i) === 27 && text[i + 1] === "[") {
        const end = text.indexOf("m", i + 2);
        if (end !== -1) {
          flushSpan();
          const raw = text.slice(i + 2, end);
          const codes = raw ? raw.split(";").map((part) => Number(part || "0")).filter((value) => Number.isFinite(value)) : [0];
          setSgr(codes);
          syncSpanClass();
          i = end + 1;
          continue;
        }
      }
      const ch = text[i++];
      if (ch === "\\r") continue;
      span.appendChild(document.createTextNode(ch));
    }
    flushSpan();
    terminal.appendChild(fragment);
    if (stickToBottom) terminal.scrollTop = terminal.scrollHeight;
  }

  function setStatus(payload) {
    const state = payload.state || "unknown";
    status.textContent = state;
    status.className = "pill job-" + state;
    if (payload.pr_url && pr) {
      pr.innerHTML = "";
      const link = document.createElement("a");
      link.href = payload.pr_url;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = payload.pr_url;
      pr.appendChild(link);
    }
  }

  async function poll() {
    try {
      const response = await fetch("/jobs/" + initial.job_id + "/log?offset=" + offset, { cache: "no-store" });
      if (!response.ok) throw new Error("log request failed");
      const payload = await response.json();
      setStatus(payload);
      if (payload.reset) {
        fullLog = "";
        plain.textContent = "";
      }
      if (payload.chunk) {
        fullLog += payload.chunk;
        plain.textContent = fullLog;
        appendAnsi(payload.chunk, payload.reset);
      }
      offset = payload.offset || offset;
      if (payload.state === "queued" || payload.state === "running") {
        window.setTimeout(poll, 1000);
      }
    } catch (error) {
      status.textContent = "log disconnected";
      window.setTimeout(poll, 2500);
    }
  }

  copy?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(fullLog);
    copy.textContent = "Copied";
    window.setTimeout(() => { copy.textContent = "Copy log"; }, 1200);
  });

  plain.textContent = fullLog;
  appendAnsi(fullLog, true);
  setStatus(initial);
  if (initial.state === "queued" || initial.state === "running") {
    window.setTimeout(poll, 1000);
  }
})();
</script>
"""


def render_queue(db_path: Path, params: dict[str, list[str]]) -> str:
    state = (params.get("state", ["all"])[0] or "all").strip()
    if state != "all" and state not in PUBLICATION_JOB_STATES:
        state = "all"
    status = publication_queue_status(db_path=db_path)
    conn = connect(db_path)
    try:
        jobs = fetch_publication_jobs(conn, state, 100)
    finally:
        conn.close()
    counts = status["counts"]
    cards = [
        ("Claimable now", status["claimable_now"], False),
        ("In flight", len(status["in_flight"]), False),
        ("Awaiting human", status["awaiting_human"], False),
        ("PR open", counts.get("pr_open", 0), False),
        ("Failed", counts.get("failed", 0), counts.get("failed", 0) > 0),
        ("Expired leases", status["expired_leases"], status["expired_leases"] > 0),
    ]
    card_html = "".join(
        f'<div class="card{" alert" if alert else ""}"><b>{html_escape(value)}</b>{html_escape(label)}</div>'
        for label, value, alert in cards
    )
    notice = ""
    if status["unrecoverable_without_reconcile"]:
        notice = f"""
<form class="notice" method="post" action="/queue/action">
  {html_escape(status['unrecoverable_without_reconcile'])} job(s) lost their worker on the final attempt and cannot be claimed again.
  <button name="action" value="reconcile">Mark them failed</button>
</form>"""
    state_options = option("all", state, "All states") + "".join(
        option(item, state, f"{item} ({counts.get(item, 0)})") for item in PUBLICATION_JOB_STATES
    )
    rows = "\n".join(
        f"""
        <tr>
          <td><a href="/queue/{job['job_id']}">{job['job_id']}</a></td>
          <td><span class="pill job-{html_escape(job['state'])}">{html_escape(job['state'])}</span></td>
          <td class="wrap"><div class="title">{html_escape(job['title'] or job['source_url'])}</div>
              <div class="muted">{html_escape(job['error'] or '')}</div></td>
          <td>{html_escape(job['attempt_count'])}/{html_escape(job['max_attempts'])}</td>
          <td class="wrap">{html_escape(job['lease_owner'] or '')}<div class="muted">{html_escape(job['lease_expires_at'] or job['next_run_at'] or '')}</div></td>
          <td>{html_escape(job['updated_at'])}</td>
          <td>{('<a href="' + html_escape(job['pr_url']) + '" target="_blank" rel="noreferrer">PR</a>') if job.get('pr_url') else ''}</td>
        </tr>
        """
        for job in jobs
    )
    content = f"""
<h1>Publishing queue</h1>
<p class="muted">Jobs for the local publisher worker. Checked {html_escape(status['checked_at'])}; oldest waiting job created {html_escape(status['oldest_waiting_created_at'] or 'n/a')}.</p>
<div class="cards">{card_html}</div>
{notice}
<form class="toolbar" method="get" action="/queue">
  <label>State <select name="state">{state_options}</select></label>
  <button class="primary" type="submit">Filter</button>
</form>
<table>
  <thead><tr><th style="width:60px">Job</th><th style="width:110px">State</th><th>Article</th><th style="width:80px">Attempts</th><th style="width:18%">Lease / next run</th><th style="width:15%">Updated</th><th style="width:50px">PR</th></tr></thead>
  <tbody>{rows or '<tr><td colspan="7">No publishing jobs match.</td></tr>'}</tbody>
</table>
"""
    return render_page("Publishing queue", content)


def render_queue_job(db_path: Path, job_id: int) -> str | None:
    conn = connect(db_path)
    try:
        job = fetch_publication_job(conn, job_id)
    finally:
        conn.close()
    if not job:
        return None
    fields = (
        "source_url", "domain", "claim_policy", "lease_owner", "lease_expires_at",
        "next_run_at", "run_id", "packet_path", "target_path", "branch", "commit_sha",
        "pr_url", "error", "created_at", "started_at", "finished_at",
    )
    field_rows = "".join(
        f"<tr><th style='width:160px;position:static'>{html_escape(name)}</th><td class='wrap'>{html_escape(job.get(name))}</td></tr>"
        for name in fields
        if job.get(name)
    )
    event_rows = "".join(
        f"<tr><td>{html_escape(event['created_at'])}</td><td>{html_escape(event['from_state'] or '')} → {html_escape(event['to_state'])}</td>"
        f"<td class='wrap'>{html_escape(event['detail_json'] or '')}</td></tr>"
        for event in job["events"]
    )
    requeue = ""
    if job["state"] in {"validated", "needs_review", "failed", "rejected"}:
        requeue = f"""
<h2>Run again</h2>
<form class="toolbar" method="post" action="/queue/action">
  <input type="hidden" name="job_id" value="{html_escape(job_id)}">
  <label>Reason (recorded in the job history)
    <input name="reason" required size="60" placeholder="why this job should run again">
  </label>
  <button name="action" value="requeue">Requeue</button>
</form>"""
    content = f"""
<h1>Publishing job {html_escape(job_id)}</h1>
<p><span class="pill job-{html_escape(job['state'])}">{html_escape(job['state'])}</span>
   <span class="pill">attempt {html_escape(job['attempt_count'])}/{html_escape(job['max_attempts'])}</span></p>
<p class="title">{html_escape(job.get('title') or '')}</p>
<table><tbody>{field_rows}</tbody></table>
{requeue}
<h2>History</h2>
<table><thead><tr><th style="width:190px">When</th><th style="width:220px">Transition</th><th>Detail</th></tr></thead>
<tbody>{event_rows}</tbody></table>
"""
    return render_page(f"Publishing job {job_id}", content)


def render_job_detail(db_path: Path, job_id: int) -> str | None:
    conn = connect(db_path)
    try:
        job = fetch_job(conn, job_id)
        items = fetch_job_items(conn, job_id) if job else []
    finally:
        conn.close()
    if not job:
        return None
    log_text = job.get("log") or job.get("error") or ""
    initial_log = {
        "job_id": job_id,
        "state": job.get("state") or "",
        "offset": len(log_text),
        "log": log_text,
        "exit_code": job.get("exit_code"),
        "pr_url": job.get("pr_url") or "",
        "error": job.get("error") or "",
    }
    item_rows = "\n".join(
        f"<tr><td>{html_escape(item['status'])}</td><td>{html_escape(item['title'])}</td><td>{html_escape(item['article_url'])}</td></tr>"
        for item in items
    )
    pr = f'<a href="{html_escape(job["pr_url"])}" target="_blank" rel="noreferrer">{html_escape(job["pr_url"])}</a>' if job.get("pr_url") else ""
    content = f"""
<h1>Job {html_escape(job_id)}</h1>
<p>
  <span class="pill job-{html_escape(job['state'])}">{html_escape(job['state'])}</span>
  <span class="pill">exit {html_escape(job.get('exit_code'))}</span>
  {pr}
</p>
<p class="muted">Created {html_escape(job['created_at'])}; started {html_escape(job['started_at'])}; finished {html_escape(job['finished_at'])}</p>
<h2>Rows</h2>
<table><thead><tr><th>Status</th><th>Title</th><th>URL</th></tr></thead><tbody>{item_rows}</tbody></table>
<div class="job-log-header">
  <h2>Live Log</h2>
  <div class="job-log-controls">
    <span id="job-live-status" class="pill job-{html_escape(job['state'])}">{html_escape(job['state'])}</span>
    <label><input id="job-follow-tail" type="checkbox" checked> Follow tail</label>
    <button id="job-copy-log" type="button">Copy log</button>
    <span id="job-live-pr">{pr}</span>
  </div>
</div>
<div id="job-log-terminal" class="job-terminal" tabindex="0" aria-label="Live Codex job log"></div>
<details>
  <summary>Plain log</summary>
  <pre id="job-log-plain">{html_escape(log_text)}</pre>
</details>
<h2>Prompt</h2>
<pre>{html_escape(job.get('prompt') or '')}</pre>
<script id="job-log-data" type="application/json">{json_script_data(initial_log)}</script>
{render_job_log_script()}
"""
    return render_page(f"Job {job_id}", content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Gmail Reader triage UI (loopback by default; set --host for LAN access).")
    parser.add_argument("--db", default=os.environ.get("GMAIL_READER_DB", str(DEFAULT_DB_PATH)))
    parser.add_argument("--host", default=os.environ.get("GMAIL_READER_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GMAIL_READER_WEB_PORT", "8765")))
    parser.add_argument(
        "--workspace-root",
        default=str(env_path("RESEARCH_WORKSPACE_ROOT", default_workspace_root())),
        help="Research workspace root passed to codex exec.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    workspace_root = Path(args.workspace_root).expanduser().resolve()
    # Bind first: a launch that loses the port race must exit without touching
    # the shared database.
    server = TriageServer((args.host, args.port), Handler, db_path=db_path, workspace_root=workspace_root)
    runner_lock = acquire_runner_lock(db_path)
    if runner_lock is None:
        server.server_close()
        print(
            f"Another triage UI is already running against {db_path} "
            f"(lock: {runner_lock_path(db_path)}). Stop it before starting a new one."
        )
        return 1
    startup = connect(db_path)
    try:
        interrupted = fail_interrupted_jobs(startup)
    finally:
        startup.close()
    if interrupted:
        print(f"Marked {interrupted} interrupted Codex job(s) as failed")
    print(f"Serving research triage UI on http://{args.host}:{args.port}")
    print(f"SQLite DB: {db_path}")
    print(f"Workspace root: {workspace_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping research triage UI")
    finally:
        server.server_close()
        runner_lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
