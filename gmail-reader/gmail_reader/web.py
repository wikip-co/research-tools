from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .app import DEFAULT_DB_PATH, ensure_db, utc_now_iso

ARTICLE_STATUSES = ("selected", "review", "rejected", "invalid")
JOB_STATES = ("queued", "running", "completed", "failed")
PR_URL_PATTERN = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+")


def env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else default


def default_workspace_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".gitmodules").is_file() and (parent / "research-tools").is_dir():
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


def html_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


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
1. Read `research-tools/docs/research-publishing-style-guide.md` before editing content.
2. Use `research-tools/agent-workflow intake` or the underlying tools to scrape each source, check duplicates, and match existing content.
3. Prefer appending to an existing `content` markdown article when the source naturally belongs there. Create a new article only when there is no good existing home.
4. Keep edits inside the `content` repo unless tooling metadata is truly required.
5. Preserve existing frontmatter, heading, bullet, citation, and footnote style.
6. Cite every research-backed claim with footnotes. Do not overstate findings beyond the study type and evidence.
7. Use `research-tools/agent-workflow publish-pr --draft` after applying article changes so I can review the PR.
8. In your final message, include the PR URL, changed article paths, and any selected articles that you could not process.

Do not mark the SQLite rows processed yourself; this web job runner will set `processed_at` after a successful Codex exit.

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


def run_job(job_id: int, db_path: Path, workspace_root: Path, article_keys: list[str]) -> None:
    conn = connect(db_path)
    try:
        job = fetch_job(conn, job_id)
        if not job:
            return
        command = json.loads(job["command_json"])
        prompt = job["prompt"]
        conn.execute(
            "UPDATE article_jobs SET state = 'running', started_at = ? WHERE job_id = ?",
            (utc_now_iso(), job_id),
        )
        conn.commit()
        result = subprocess.run(
            command,
            input=prompt,
            cwd=workspace_root,
            text=True,
            capture_output=True,
        )
        log = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        pr_matches = PR_URL_PATTERN.findall(log)
        pr_url = pr_matches[-1] if pr_matches else ""
        if result.returncode == 0:
            mark_articles_processed(conn, article_keys)
            state = "completed"
            error = ""
        else:
            state = "failed"
            error = f"Codex exited with {result.returncode}"
        conn.execute(
            """
            UPDATE article_jobs
            SET state = ?, finished_at = ?, log = ?, exit_code = ?, pr_url = ?, error = ?
            WHERE job_id = ?
            """,
            (state, utc_now_iso(), log[-200000:], result.returncode, pr_url, error, job_id),
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

    def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
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

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        return parse_qs(raw, keep_blank_values=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/":
            self.send_html(render_index(self.server.db_path, params))
        elif parsed.path == "/jobs":
            self.send_html(render_jobs(self.server.db_path))
        elif parsed.path.startswith("/jobs/"):
            job_id = parse_int(parsed.path.rsplit("/", 1)[-1], 0, minimum=0, maximum=1_000_000)
            self.send_html(render_job_detail(self.server.db_path, job_id))
        else:
            self.send_html(render_page("Not Found", "<p>Not found.</p>"), HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/jobs" or parsed.path.startswith("/jobs/"):
            self.send_head_ok()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        form = self.read_form()
        if parsed.path != "/articles/action":
            self.send_html(render_page("Not Found", "<p>Not found.</p>"), HTTPStatus.NOT_FOUND)
            return
        action = (form.get("action", [""])[0] or "").strip()
        article_keys = selected_values(form, "article_key")
        return_to = form.get("return_to", ["/"])[0] or "/"
        conn = connect(self.server.db_path)
        try:
            if action.startswith("status:"):
                update_article_status(conn, article_keys, action.split(":", 1)[1])
                self.redirect(return_to)
                return
            if action == "mark_processed":
                mark_articles_processed(conn, article_keys)
                self.redirect(return_to)
                return
            if action == "process_codex":
                articles = fetch_articles_by_key(conn, article_keys)
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


def render_page(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(title)}</title>
  <style>
    :root {{ color-scheme: light; --line: #d8dee4; --bg: #f6f8fa; --ink: #1f2328; --muted: #656d76; --accent: #0969da; }}
    body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; color: var(--ink); background: white; font-size: 14px; }}
    header {{ display: flex; align-items: center; gap: 16px; padding: 12px 18px; border-bottom: 1px solid var(--line); background: var(--bg); position: sticky; top: 0; z-index: 5; }}
    header a {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
    main {{ padding: 16px 18px 32px; }}
    form.filters {{ display: grid; grid-template-columns: minmax(220px, 1fr) repeat(5, max-content); gap: 8px; align-items: end; margin-bottom: 14px; }}
    label {{ display: grid; gap: 4px; color: var(--muted); font-size: 12px; }}
    input, select, button, textarea {{ font: inherit; }}
    input, select {{ border: 1px solid var(--line); border-radius: 6px; padding: 7px 8px; background: white; }}
    button {{ border: 1px solid var(--line); border-radius: 6px; padding: 7px 10px; background: #fff; cursor: pointer; }}
    button.primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
    button.danger {{ color: #cf222e; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 10px 0; }}
    .muted {{ color: var(--muted); }}
    table {{ border-collapse: collapse; width: 100%; table-layout: fixed; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 8px; vertical-align: top; text-align: left; }}
    th {{ background: var(--bg); font-size: 12px; color: var(--muted); position: sticky; top: 49px; z-index: 4; }}
    tr:hover td {{ background: #f6f8fa; }}
    .title {{ font-weight: 650; }}
    .snippet {{ color: var(--muted); margin-top: 4px; max-height: 4.5em; overflow: hidden; }}
    .pill {{ display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 1px 7px; font-size: 12px; background: #fff; margin: 0 4px 4px 0; }}
    .status-selected {{ border-color: #1a7f37; color: #1a7f37; }}
    .status-review {{ border-color: #9a6700; color: #9a6700; }}
    .status-rejected, .status-invalid {{ border-color: #cf222e; color: #cf222e; }}
    .job-running, .job-queued {{ border-color: #0969da; color: #0969da; }}
    .job-completed {{ border-color: #1a7f37; color: #1a7f37; }}
    .job-failed {{ border-color: #cf222e; color: #cf222e; }}
    .links a {{ display: inline-block; margin-right: 8px; color: var(--accent); }}
    .pager {{ display: flex; gap: 8px; align-items: center; margin: 12px 0; }}
    pre {{ white-space: pre-wrap; overflow: auto; background: #f6f8fa; border: 1px solid var(--line); border-radius: 6px; padding: 12px; }}
    @media (max-width: 900px) {{ form.filters {{ grid-template-columns: 1fr 1fr; }} th:nth-child(5), td:nth-child(5) {{ display: none; }} }}
  </style>
</head>
<body>
  <header>
    <strong>Research Triage</strong>
    <a href="/">Articles</a>
    <a href="/jobs">Jobs</a>
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
  <a href="/?{urlencode(prev_query)}">Previous</a>
  <a href="/?{urlencode(next_query)}">Next</a>
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
    <button class="primary" name="action" value="process_codex">Process with Codex</button>
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
        state_bits.append(f'<span class="pill status-selected">processed</span>')
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
<h1>Jobs</h1>
<table>
  <thead><tr><th>Job</th><th>State</th><th>Rows</th><th>Created</th><th>Finished</th><th>Result</th></tr></thead>
  <tbody>{rows or '<tr><td colspan="6">No jobs yet.</td></tr>'}</tbody>
</table>
"""
    return render_page("Jobs", content)


def render_job_detail(db_path: Path, job_id: int) -> str:
    conn = connect(db_path)
    try:
        job = fetch_job(conn, job_id)
        items = fetch_job_items(conn, job_id) if job else []
    finally:
        conn.close()
    if not job:
        return render_page("Job not found", "<p>Job not found.</p>")
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
<h2>Log</h2>
<pre>{html_escape(job.get('log') or job.get('error') or '')}</pre>
<h2>Prompt</h2>
<pre>{html_escape(job.get('prompt') or '')}</pre>
"""
    return render_page(f"Job {job_id}", content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a LAN-accessible Gmail Reader triage UI.")
    parser.add_argument("--db", default=os.environ.get("GMAIL_READER_DB", str(DEFAULT_DB_PATH)))
    parser.add_argument("--host", default=os.environ.get("GMAIL_READER_WEB_HOST", "0.0.0.0"))
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
    connect(db_path).close()
    server = TriageServer((args.host, args.port), Handler, db_path=db_path, workspace_root=workspace_root)
    print(f"Serving research triage UI on http://{args.host}:{args.port}")
    print(f"SQLite DB: {db_path}")
    print(f"Workspace root: {workspace_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping research triage UI")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
