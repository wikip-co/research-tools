from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gmail_reader.app import ensure_db, utc_now_iso
from gmail_reader.web import (
    build_codex_prompt,
    connect,
    create_job,
    fetch_articles,
    fetch_articles_by_key,
    fetch_job,
    mark_articles_processed,
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
        self.assertIn("research-publishing-style-guide.md", prompt)
        self.assertEqual(updated, 1)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
