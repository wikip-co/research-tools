from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gmail_reader.app import (
    backfill_paper_keys,
    claim_publication_job,
    enqueue_publication_backlog,
    enqueue_publication_job,
    ensure_db,
    requeue_publication_job,
    set_publication_job_state,
    utc_now_iso,
)


class PublicationJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "jobs.db"
        conn = ensure_db(self.db_path)
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO messages (
                message_id, thread_id, alert_name, subject, from_address,
                raw_json, imported_at
            ) VALUES ('message-1', 'thread-1', 'Quercetin', 'Scholar Alert',
                      'scholar@example.com', '{}', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO articles (
                article_key, message_id, alert_name, rank_in_email, title,
                article_url, score, status, reasons_json, created_at, updated_at
            ) VALUES ('article-1', 'message-1', 'Quercetin', 1,
                      'Example Study', 'https://example.org/study', 20,
                      'selected', '[]', ?, ?)
            """,
            (now, now),
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_enqueue_claim_and_resolve_job(self) -> None:
        queued = enqueue_publication_job(
            db_path=self.db_path,
            identifier="article-1",
            max_attempts=3,
        )
        self.assertTrue(queued["created"])
        self.assertEqual(queued["job"]["domain"], "Natural Healing")
        self.assertEqual(queued["job"]["claim_policy"], "integrated")
        self.assertEqual(
            queued["job"]["canonical_source_url"], "https://example.org/study"
        )

        duplicate_enqueue = enqueue_publication_job(
            db_path=self.db_path,
            identifier="article-1",
            max_attempts=3,
        )
        self.assertFalse(duplicate_enqueue["created"])

        claimed = claim_publication_job(
            db_path=self.db_path,
            worker="test-worker",
            lease_seconds=300,
        )
        self.assertTrue(claimed["claimed"])
        self.assertEqual(claimed["job"]["attempt_count"], 1)

        resolved = set_publication_job_state(
            db_path=self.db_path,
            job_id=claimed["job"]["job_id"],
            state="pr_open",
            worker="test-worker",
            target_path="Natural Healing/example.md",
            pr="https://github.com/example/content/pull/1",
        )
        self.assertEqual(resolved["job"]["state"], "pr_open")
        conn = ensure_db(self.db_path)
        processed_at = conn.execute(
            "SELECT processed_at FROM articles WHERE article_key = 'article-1'"
        ).fetchone()["processed_at"]
        conn.close()
        self.assertTrue(processed_at)

    def test_failed_job_does_not_mark_article_processed(self) -> None:
        queued = enqueue_publication_job(
            db_path=self.db_path,
            identifier="https://example.org/study",
            max_attempts=1,
        )
        set_publication_job_state(
            db_path=self.db_path,
            job_id=queued["job"]["job_id"],
            state="failed",
            error="model timeout",
        )
        conn = ensure_db(self.db_path)
        processed_at = conn.execute(
            "SELECT processed_at FROM articles WHERE article_key = 'article-1'"
        ).fetchone()["processed_at"]
        conn.close()
        self.assertIsNone(processed_at)

    def test_paper_key_backfill_is_dry_run_by_default(self) -> None:
        preview = backfill_paper_keys(
            db_path=self.db_path,
            status="selected",
            limit=10,
            apply=False,
        )
        self.assertEqual(preview["linkable"], 1)
        conn = ensure_db(self.db_path)
        self.assertIsNone(
            conn.execute(
                "SELECT paper_key FROM articles WHERE article_key = 'article-1'"
            ).fetchone()["paper_key"]
        )
        conn.close()

        applied = backfill_paper_keys(
            db_path=self.db_path,
            status="selected",
            limit=10,
            apply=True,
        )
        self.assertEqual(applied["linkable"], 1)
        conn = ensure_db(self.db_path)
        self.assertTrue(
            conn.execute(
                "SELECT paper_key FROM articles WHERE article_key = 'article-1'"
            ).fetchone()["paper_key"]
        )
        conn.close()

    def test_enqueue_backlog_does_not_queue_same_article_twice(self) -> None:
        first = enqueue_publication_backlog(
            db_path=self.db_path,
            status="selected",
            min_score=6,
            limit=10,
            max_attempts=3,
        )
        second = enqueue_publication_backlog(
            db_path=self.db_path,
            status="selected",
            min_score=6,
            limit=10,
            max_attempts=3,
        )
        self.assertEqual(first["queued_count"], 1)
        self.assertEqual(second["queued_count"], 0)

    def test_same_source_from_two_alerts_has_one_active_job(self) -> None:
        conn = ensure_db(self.db_path)
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO articles (
                article_key, message_id, alert_name, rank_in_email, title,
                article_url, score, status, reasons_json, created_at, updated_at
            ) VALUES ('article-2', 'message-1', 'Polyphenols', 2,
                      'Example Study', 'https://example.org/study', 19,
                      'selected', '[]', ?, ?)
            """,
            (now, now),
        )
        conn.commit()
        conn.close()
        first = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )
        second = enqueue_publication_job(
            db_path=self.db_path, identifier="article-2", max_attempts=3
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])

    def test_canonical_source_variants_have_one_active_job(self) -> None:
        conn = ensure_db(self.db_path)
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO articles (
                article_key, message_id, alert_name, rank_in_email, title,
                article_url, score, status, reasons_json, created_at, updated_at
            ) VALUES ('article-2', 'message-1', 'Polyphenols', 2,
                      'Example Study mirror', 'http://www.example.org/study/?utm_source=alert', 19,
                      'selected', '[]', ?, ?)
            """,
            (now, now),
        )
        conn.commit()
        conn.close()
        first = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )
        second = enqueue_publication_job(
            db_path=self.db_path, identifier="article-2", max_attempts=3
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])

    def test_retry_is_deferred_until_next_run(self) -> None:
        queued = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )
        claimed = claim_publication_job(
            db_path=self.db_path, worker="test-worker", lease_seconds=300
        )
        retried = set_publication_job_state(
            db_path=self.db_path,
            job_id=queued["job"]["job_id"],
            state="retry",
            worker="test-worker",
            error="temporary model timeout",
        )
        self.assertTrue(retried["job"]["next_run_at"])
        immediately_claimed = claim_publication_job(
            db_path=self.db_path, worker="other-worker", lease_seconds=300
        )
        self.assertFalse(immediately_claimed["claimed"])
        self.assertEqual(claimed["job"]["attempt_count"], 1)

    def test_validated_job_requires_explicit_audited_requeue(self) -> None:
        queued = enqueue_publication_job(
            db_path=self.db_path,
            identifier="article-1",
            max_attempts=3,
            domain="Natural Healing",
        )
        set_publication_job_state(
            db_path=self.db_path,
            job_id=queued["job"]["job_id"],
            state="validated",
            run_id="run-123",
            packet_path="/tmp/run-123/report.json",
        )
        waiting = claim_publication_job(
            db_path=self.db_path, worker="test-worker", lease_seconds=300
        )
        self.assertFalse(waiting["claimed"])
        requeued = requeue_publication_job(
            db_path=self.db_path,
            job_id=queued["job"]["job_id"],
            reason="reviewed dry-run and now ready to publish",
        )
        self.assertEqual(requeued["job"]["state"], "queued")
        self.assertEqual(requeued["job"]["attempt_count"], 0)
        self.assertEqual(requeued["prior_outputs"]["run_id"], "run-123")


if __name__ == "__main__":
    unittest.main()
