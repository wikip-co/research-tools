from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from gmail_reader.app import (
    claim_publication_job,
    enqueue_publication_job,
    ensure_db,
    set_publication_job_state,
    utc_now_iso,
)
from gmail_reader.web import (
    Handler,
    TriageServer,
    acquire_runner_lock,
    active_job_article_keys,
    append_job_log,
    default_workspace_root,
    fail_interrupted_jobs,
    is_allowed_host,
    is_same_origin_request,
    safe_return_to,
    build_codex_prompt,
    connect,
    codex_command,
    create_job,
    fetch_articles,
    fetch_articles_by_key,
    fetch_job,
    job_log_delta,
    main,
    mark_articles_processed,
    run_job,
    update_article_status,
)


def insert_article(db_path: Path, *, article_key: str = "article-1") -> None:
    conn = ensure_db(db_path)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO messages (
            message_id, thread_id, alert_name, subject, from_address, sent_at,
            snippet, raw_json, imported_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "message-1",
            "thread-1",
            "Nutrition",
            "Scholar Alert",
            "scholaralerts-noreply@google.com",
            now,
            "snippet",
            "{}",
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO articles (
            article_key, message_id, alert_name, rank_in_email, title, authors,
            publication_info, snippet, scholar_url, article_url, pdf_url,
            format_label, author_count, score, status, reasons_json,
            created_at, updated_at, is_open_access
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_key,
            "message-1",
            "Nutrition",
            1,
            "Example Study",
            "A Author",
            "Journal, 2026",
            "Useful finding",
            "",
            "https://example.org/study",
            "",
            "",
            1,
            25,
            "selected",
            "[]",
            now,
            now,
            1,
        ),
    )
    conn.commit()
    conn.close()


class WebHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "papers.db"
        insert_article(self.db_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_update_status_and_filter_articles(self) -> None:
        conn = connect(self.db_path)
        try:
            updated = update_article_status(conn, ["article-1"], "invalid")
            self.assertEqual(updated, 1)
            rows, total = fetch_articles(
                conn,
                {
                    "status": "invalid",
                    "processed": "all",
                    "alert_name": "",
                    "q": "",
                    "min_score": 0,
                    "limit": 20,
                    "offset": 0,
                },
            )
        finally:
            conn.close()

        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["status"], "invalid")

    def test_job_creation_and_processed_marker(self) -> None:
        conn = connect(self.db_path)
        try:
            articles = fetch_articles_by_key(conn, ["article-1"])
            prompt = build_codex_prompt(
                workspace_root=Path("/tmp/research"),
                db_path=self.db_path,
                articles=articles,
            )
            job_id = create_job(conn, articles, prompt, ["codex", "exec", "-"])
            job = fetch_job(conn, job_id)
            updated = mark_articles_processed(conn, ["article-1"])
            rows, _ = fetch_articles(
                conn,
                {
                    "status": "all",
                    "processed": "processed",
                    "alert_name": "",
                    "q": "",
                    "min_score": 0,
                    "limit": 20,
                    "offset": 0,
                },
            )
        finally:
            conn.close()

        self.assertIsNotNone(job)
        self.assertIn("https://agent.wikip.co/research-publishing-style-guide/", prompt)
        self.assertIn("https://agent.wikip.co/natural-healing-content-style-guide/", prompt)
        self.assertEqual(updated, 1)
        self.assertEqual(len(rows), 1)

    def test_codex_global_flags_precede_exec_subcommand(self) -> None:
        command = codex_command(Path("/tmp/research"))
        self.assertLess(command.index("--ask-for-approval"), command.index("exec"))
        self.assertLess(command.index("--sandbox"), command.index("exec"))
        self.assertEqual(command[-1], "-")

    def test_append_job_log_keeps_recent_output(self) -> None:
        conn = connect(self.db_path)
        try:
            articles = fetch_articles_by_key(conn, ["article-1"])
            job_id = create_job(conn, articles, "prompt", ["codex", "exec", "-"])
            append_job_log(conn, job_id, "abc", max_chars=5)
            append_job_log(conn, job_id, "def", max_chars=5)
            job = fetch_job(conn, job_id)
        finally:
            conn.close()

        self.assertEqual(job["log"], "bcdef")

    def test_job_log_delta_returns_appended_output(self) -> None:
        conn = connect(self.db_path)
        try:
            articles = fetch_articles_by_key(conn, ["article-1"])
            job_id = create_job(conn, articles, "prompt", ["codex", "exec", "-"])
            append_job_log(conn, job_id, "first\n")
            first = job_log_delta(conn, job_id, 0)
            append_job_log(conn, job_id, "second\n")
            second = job_log_delta(conn, job_id, first["offset"])
        finally:
            conn.close()

        self.assertEqual(first["chunk"], "first\n")
        self.assertEqual(second["chunk"], "second\n")
        self.assertFalse(second["reset"])

    def test_job_log_delta_resets_when_offset_is_past_truncated_log(self) -> None:
        conn = connect(self.db_path)
        try:
            articles = fetch_articles_by_key(conn, ["article-1"])
            job_id = create_job(conn, articles, "prompt", ["codex", "exec", "-"])
            append_job_log(conn, job_id, "abc")
            delta = job_log_delta(conn, job_id, 20)
        finally:
            conn.close()

        self.assertEqual(delta["chunk"], "abc")
        self.assertTrue(delta["reset"])

    def test_interrupted_jobs_are_failed_on_startup(self) -> None:
        conn = connect(self.db_path)
        try:
            articles = fetch_articles_by_key(conn, ["article-1"])
            job_id = create_job(conn, articles, "prompt", ["codex", "exec", "-"])
            self.assertEqual(active_job_article_keys(conn, ["article-1"]), {"article-1"})
            self.assertEqual(fail_interrupted_jobs(conn), 1)
            job = fetch_job(conn, job_id)
            self.assertEqual(active_job_article_keys(conn, ["article-1"]), set())
        finally:
            conn.close()
        self.assertEqual(job["state"], "failed")
        self.assertIn("Interrupted", job["error"])

    def test_runner_lock_is_exclusive_per_database(self) -> None:
        first = acquire_runner_lock(self.db_path)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(acquire_runner_lock(self.db_path))
        finally:
            first.close()
        again = acquire_runner_lock(self.db_path)
        self.assertIsNotNone(again)
        again.close()

    def test_second_instance_leaves_the_live_instances_jobs_alone(self) -> None:
        conn = connect(self.db_path)
        try:
            job_id = create_job(conn, fetch_articles_by_key(conn, ["article-1"]), "prompt", ["codex"])
        finally:
            conn.close()
        live = acquire_runner_lock(self.db_path)
        try:
            with mock.patch("builtins.print"):
                code = main(["--db", str(self.db_path), "--host", "127.0.0.1", "--port", "0"])
        finally:
            live.close()
        self.assertEqual(code, 1)
        conn = connect(self.db_path)
        try:
            self.assertEqual(fetch_job(conn, job_id)["state"], "queued")
        finally:
            conn.close()

    def test_hung_codex_run_is_stopped_and_releases_the_queue(self) -> None:
        # The grandchild keeps stdout open, so only a process-group kill ends the run.
        command = ["sh", "-c", "echo started; sleep 60 & sleep 60"]
        conn = connect(self.db_path)
        try:
            job_id = create_job(conn, fetch_articles_by_key(conn, ["article-1"]), "prompt", command)
        finally:
            conn.close()
        with mock.patch.dict(os.environ, {"CODEX_WEB_TIMEOUT_SECONDS": "0.5"}):
            runner = threading.Thread(
                target=run_job, args=(job_id, self.db_path, Path(self.tempdir.name), ["article-1"])
            )
            runner.start()
            runner.join(timeout=20)
        self.assertFalse(runner.is_alive())
        conn = connect(self.db_path)
        try:
            job = fetch_job(conn, job_id)
            self.assertEqual(active_job_article_keys(conn, ["article-1"]), set())
        finally:
            conn.close()
        self.assertEqual(job["state"], "failed")
        self.assertIn("did not finish", job["error"])
        self.assertIn("started", job["log"])


class RequestGuardTests(unittest.TestCase):
    def test_same_origin_rules(self) -> None:
        host = {"Host": "iconium.lan:8765"}
        self.assertTrue(is_same_origin_request(host))  # curl and scripts
        self.assertTrue(is_same_origin_request({**host, "Origin": "http://iconium.lan:8765"}))
        self.assertTrue(is_same_origin_request({**host, "Referer": "http://iconium.lan:8765/?q=x"}))
        self.assertFalse(is_same_origin_request({**host, "Origin": "https://evil.example"}))
        self.assertFalse(is_same_origin_request({**host, "Origin": "null"}))
        self.assertFalse(is_same_origin_request({**host, "Referer": "https://evil.example/page"}))
        self.assertFalse(
            is_same_origin_request(
                {**host, "Origin": "http://iconium.lan:8765", "Sec-Fetch-Site": "cross-site"}
            )
        )

    def test_host_allowlist_blocks_rebinding_names(self) -> None:
        for name in ("127.0.0.1:8765", "192.168.1.20:8765", "[::1]:8765", "localhost:8765",
                     "iconium:8765", "iconium.lan:8765", "box.local"):
            self.assertTrue(is_allowed_host(name), name)
        for name in ("", "rebind.evil.example:8765", "iconium.lan.evil.example"):
            self.assertFalse(is_allowed_host(name), name)
        with mock.patch.dict(os.environ, {"GMAIL_READER_WEB_ALLOWED_HOSTS": "box.tailnet.ts.net"}):
            self.assertTrue(is_allowed_host("box.tailnet.ts.net:8765"))

    def test_return_to_stays_local(self) -> None:
        self.assertEqual(safe_return_to("/?status=review"), "/?status=review")
        for value in ("https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)", ""):
            self.assertEqual(safe_return_to(value), "/")

    def test_workspace_root_is_the_flat_coordinator(self) -> None:
        root = default_workspace_root()
        if (root / "workspace-repos.tsv").is_file():
            self.assertTrue((root / "research-tools").is_dir())


class LiveServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "papers.db"
        insert_article(self.db_path)
        connect(self.db_path).close()
        self.server = TriageServer(
            ("127.0.0.1", 0), Handler, db_path=self.db_path, workspace_root=Path(self.tempdir.name)
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tempdir.cleanup()

    def request(self, method: str, path: str, form: dict | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urlencode(form, doseq=True) if form is not None else None
        send_headers = dict(headers or {})
        if body is not None:
            send_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        conn.request(method, path, body=body, headers=send_headers)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        conn.close()
        return response, data

    def article_status(self) -> str:
        conn = ensure_db(self.db_path)
        status = conn.execute("SELECT status FROM articles").fetchone()["status"]
        conn.close()
        return status

    def codex_job_count(self) -> int:
        conn = connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM article_jobs").fetchone()[0]
        conn.close()
        return count

    def test_pages_render_with_security_headers(self) -> None:
        for path in ("/", "/jobs", "/queue"):
            response, body = self.request("GET", path)
            self.assertEqual(response.status, 200, path)
            self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
            self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
            self.assertIn("Research Triage", body)
        self.assertEqual(self.request("GET", "/jobs/999")[0].status, 404)
        self.assertEqual(self.request("GET", "/queue/999")[0].status, 404)
        self.assertEqual(self.request("GET", "/jobs/abc")[0].status, 404)

    def test_cross_site_post_cannot_change_state_or_launch_codex(self) -> None:
        for action in ("status:rejected", "process_codex"):
            response, _ = self.request(
                "POST",
                "/articles/action",
                {"action": action, "article_key": "article-1"},
                {"Origin": "https://evil.example"},
            )
            self.assertEqual(response.status, 403, action)
        self.assertEqual(self.article_status(), "selected")
        self.assertEqual(self.codex_job_count(), 0)

    def test_rebinding_host_is_refused(self) -> None:
        response, _ = self.request("GET", "/", headers={"Host": "rebind.evil.example"})
        self.assertEqual(response.status, 403)
        response, _ = self.request(
            "POST",
            "/articles/action",
            {"action": "status:rejected", "article_key": "article-1"},
            {"Host": "rebind.evil.example", "Origin": "http://rebind.evil.example"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.article_status(), "selected")

    def test_same_origin_post_updates_status_and_redirects_locally(self) -> None:
        origin = f"http://127.0.0.1:{self.port}"
        response, _ = self.request(
            "POST",
            "/articles/action",
            {"action": "status:review", "article_key": "article-1", "return_to": "https://evil.example/"},
            {"Origin": origin},
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/")
        self.assertEqual(self.article_status(), "review")
        response, _ = self.request(
            "POST", "/articles/action", {"action": "status:bogus", "article_key": "article-1"}
        )
        self.assertEqual(response.status, 400)

    def test_codex_launcher_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"GMAIL_READER_WEB_DISABLE_CODEX": "1"}):
            response, _ = self.request(
                "POST", "/articles/action", {"action": "process_codex", "article_key": "article-1"}
            )
            self.assertEqual(response.status, 403)
            self.assertIn("Codex launcher disabled", self.request("GET", "/")[1])
        self.assertEqual(self.codex_job_count(), 0)

    def test_oversized_or_malformed_form_is_rejected(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/articles/action")
        conn.putheader("Content-Length", "99999999")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_health_and_json_endpoints(self) -> None:
        response, body = self.request("GET", "/healthz", headers={"Host": "monitor.example"})
        payload = json.loads(body)
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["integrity"], "ok")
        self.assertEqual(payload["unprocessed_selected"], 1)
        articles = json.loads(self.request("GET", "/api/articles?status=selected")[1])
        self.assertEqual(articles["total"], 1)
        self.assertEqual(articles["articles"][0]["article_key"], "article-1")
        queue = json.loads(self.request("GET", "/api/queue")[1])
        self.assertEqual(queue["total"], 0)

    def test_queue_page_shows_jobs_and_requeue_records_reason(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=1
        )["job"]["job_id"]
        claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        set_publication_job_state(
            db_path=self.db_path, job_id=job_id, state="needs_review", worker="worker-a",
            error="entity gate <rejected>",
        )
        _, body = self.request("GET", "/queue")
        self.assertIn("Example Study", body)
        self.assertIn("entity gate &lt;rejected&gt;", body)
        _, detail = self.request("GET", f"/queue/{job_id}")
        self.assertIn("Requeue", detail)
        self.assertIn("worker-a", detail)

        response, _ = self.request("POST", "/queue/action", {"action": "requeue", "job_id": job_id, "reason": ""})
        self.assertEqual(response.status, 400)
        response, _ = self.request(
            "POST", "/queue/action", {"action": "requeue", "job_id": job_id, "reason": "validator fixed"}
        )
        self.assertEqual(response.status, 303)
        _, detail = self.request("GET", f"/queue/{job_id}")
        self.assertIn("validator fixed", detail)
        self.assertIn("job-queued", detail)

    def test_reconcile_action_fails_abandoned_final_attempt(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=1
        )["job"]["job_id"]
        claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        conn = ensure_db(self.db_path)
        conn.execute("UPDATE publication_jobs SET lease_expires_at = '2000-01-01T00:00:00Z'")
        conn.commit()
        conn.close()
        self.assertIn("Mark them failed", self.request("GET", "/queue")[1])
        self.assertEqual(self.request("POST", "/queue/action", {"action": "reconcile"})[0].status, 303)
        conn = ensure_db(self.db_path)
        state = conn.execute("SELECT state FROM publication_jobs WHERE job_id = ?", (job_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(state, "failed")


if __name__ == "__main__":
    unittest.main()
