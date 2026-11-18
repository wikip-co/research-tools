from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gmail_reader.app import (
    LeaseLostError,
    backfill_paper_keys,
    claim_publication_job,
    enqueue_publication_backlog,
    enqueue_publication_job,
    ensure_db,
    main,
    publication_queue_status,
    reconcile_publication_jobs,
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

    def expire_lease(self, job_id: int) -> None:
        conn = ensure_db(self.db_path)
        conn.execute(
            "UPDATE publication_jobs SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE job_id = ?",
            (job_id,),
        )
        conn.commit()
        conn.close()

    def job_row(self, job_id: int) -> dict:
        conn = ensure_db(self.db_path)
        row = dict(conn.execute("SELECT * FROM publication_jobs WHERE job_id = ?", (job_id,)).fetchone())
        conn.close()
        return row

    def events(self, job_id: int) -> list[dict]:
        conn = ensure_db(self.db_path)
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM publication_job_events WHERE job_id = ? ORDER BY event_id", (job_id,)
            )
        ]
        conn.close()
        return rows

    def test_stale_worker_cannot_update_job_stopped_for_review(self) -> None:
        """Replay of the reviewed five-step path: A leases, lease expires, B
        reclaims and stops for review (clearing ownership), then A reports."""
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )["job"]["job_id"]
        first = claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        self.expire_lease(job_id)
        second = claim_publication_job(db_path=self.db_path, worker="worker-b", lease_seconds=300)
        self.assertEqual(second["job"]["lease_token"], first["job"]["lease_token"] + 1)
        set_publication_job_state(
            db_path=self.db_path,
            job_id=job_id,
            state="needs_review",
            worker="worker-b",
            lease_token=second["job"]["lease_token"],
        )
        with self.assertRaises(LeaseLostError):
            set_publication_job_state(
                db_path=self.db_path,
                job_id=job_id,
                state="pr_open",
                worker="worker-a",
                lease_token=first["job"]["lease_token"],
                pr="https://github.com/example/content/pull/9",
            )
        row = self.job_row(job_id)
        self.assertEqual(row["state"], "needs_review")
        self.assertIsNone(row["pr_url"])
        conn = ensure_db(self.db_path)
        processed_at = conn.execute(
            "SELECT processed_at FROM articles WHERE article_key = 'article-1'"
        ).fetchone()["processed_at"]
        conn.close()
        self.assertIsNone(processed_at)
        self.assertEqual(self.events(job_id)[-1]["to_state"], "needs_review")

    def test_superseded_worker_is_rejected_while_new_lease_is_active(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )["job"]["job_id"]
        claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        self.expire_lease(job_id)
        claim_publication_job(db_path=self.db_path, worker="worker-b", lease_seconds=300)
        with self.assertRaises(LeaseLostError):
            set_publication_job_state(
                db_path=self.db_path, job_id=job_id, state="drafting", worker="worker-a"
            )
        self.assertEqual(self.job_row(job_id)["lease_owner"], "worker-b")

    def test_same_worker_name_cannot_reuse_an_older_lease_generation(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )["job"]["job_id"]
        first = claim_publication_job(db_path=self.db_path, worker="host-1", lease_seconds=300)
        self.expire_lease(job_id)
        second = claim_publication_job(db_path=self.db_path, worker="host-1", lease_seconds=300)
        with self.assertRaises(LeaseLostError):
            set_publication_job_state(
                db_path=self.db_path,
                job_id=job_id,
                state="failed",
                worker="host-1",
                lease_token=first["job"]["lease_token"],
            )
        resolved = set_publication_job_state(
            db_path=self.db_path,
            job_id=job_id,
            state="validating",
            worker="host-1",
            lease_token=second["job"]["lease_token"],
        )
        self.assertEqual(resolved["job"]["state"], "validating")

    def test_worker_cannot_report_on_job_it_never_claimed(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )["job"]["job_id"]
        with self.assertRaises(LeaseLostError):
            set_publication_job_state(
                db_path=self.db_path, job_id=job_id, state="scraping", worker="worker-a"
            )
        self.assertEqual(self.job_row(job_id)["state"], "queued")

    def test_progress_report_renews_the_lease(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )["job"]["job_id"]
        claimed = claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        self.expire_lease(job_id)
        set_publication_job_state(
            db_path=self.db_path,
            job_id=job_id,
            state="drafting",
            worker="worker-a",
            lease_token=claimed["job"]["lease_token"],
            lease_seconds=600,
        )
        self.assertGreater(self.job_row(job_id)["lease_expires_at"], utc_now_iso())
        other = claim_publication_job(db_path=self.db_path, worker="worker-b", lease_seconds=300)
        self.assertFalse(other["claimed"])

    def test_expired_lease_on_final_attempt_is_reconciled_to_failed(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=1
        )["job"]["job_id"]
        claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        set_publication_job_state(
            db_path=self.db_path, job_id=job_id, state="drafting", worker="worker-a"
        )
        self.expire_lease(job_id)
        status = publication_queue_status(db_path=self.db_path)
        self.assertEqual(status["expired_leases"], 1)
        self.assertEqual(status["unrecoverable_without_reconcile"], 1)

        next_claim = claim_publication_job(db_path=self.db_path, worker="worker-b", lease_seconds=300)
        self.assertFalse(next_claim["claimed"])
        row = self.job_row(job_id)
        self.assertEqual(row["state"], "failed")
        self.assertIsNone(row["lease_owner"])
        self.assertTrue(row["finished_at"])
        self.assertIn("1/1 attempts", row["error"])
        last = self.events(job_id)[-1]
        self.assertEqual((last["from_state"], last["to_state"]), ("drafting", "failed"))
        self.assertIn("reconcile_exhausted_attempts", last["detail_json"])
        self.assertIn("worker-a", last["detail_json"])
        # The dead worker cannot resurrect it, but an operator can requeue.
        with self.assertRaises(LeaseLostError):
            set_publication_job_state(
                db_path=self.db_path, job_id=job_id, state="pr_open", worker="worker-a"
            )
        requeued = requeue_publication_job(
            db_path=self.db_path, job_id=job_id, reason="worker host rebooted"
        )
        self.assertEqual(requeued["job"]["state"], "queued")
        self.assertTrue(
            claim_publication_job(db_path=self.db_path, worker="worker-b", lease_seconds=300)["claimed"]
        )

    def test_expired_lease_with_attempts_left_is_reclaimed_not_failed(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=2
        )["job"]["job_id"]
        claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        self.expire_lease(job_id)
        self.assertEqual(reconcile_publication_jobs(db_path=self.db_path)["reconciled_count"], 0)
        reclaimed = claim_publication_job(db_path=self.db_path, worker="worker-b", lease_seconds=300)
        self.assertTrue(reclaimed["claimed"])
        self.assertEqual(reclaimed["job"]["attempt_count"], 2)

    def test_retry_without_remaining_attempts_is_reconciled(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=1
        )["job"]["job_id"]
        claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        set_publication_job_state(
            db_path=self.db_path, job_id=job_id, state="retry", worker="worker-a", error="timeout"
        )
        result = reconcile_publication_jobs(db_path=self.db_path)
        self.assertEqual(result["reconciled_count"], 1)
        row = self.job_row(job_id)
        self.assertEqual(row["state"], "failed")
        self.assertIn("Last error: timeout", row["error"])

    def test_operator_override_is_recorded_as_operator(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )["job"]["job_id"]
        claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        set_publication_job_state(db_path=self.db_path, job_id=job_id, state="rejected")
        self.assertIn('"actor": "operator"', self.events(job_id)[-1]["detail_json"])

    def test_invalid_result_json_changes_nothing(self) -> None:
        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )["job"]["job_id"]
        claim_publication_job(db_path=self.db_path, worker="worker-a", lease_seconds=300)
        with self.assertRaises(ValueError):
            set_publication_job_state(
                db_path=self.db_path,
                job_id=job_id,
                state="pr_open",
                worker="worker-a",
                result_json="{not json",
            )
        self.assertEqual(self.job_row(job_id)["state"], "leased")

    def test_cli_reports_lease_loss_with_machine_readable_code(self) -> None:
        import contextlib
        import io
        import json

        job_id = enqueue_publication_job(
            db_path=self.db_path, identifier="article-1", max_attempts=3
        )["job"]["job_id"]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(
                [
                    "--db", str(self.db_path), "set-publication-job-state", str(job_id),
                    "--state", "scraping", "--worker", "ghost",
                ]
            )
        self.assertEqual(code, 1)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["code"], "lease_lost")

    def test_schema_migration_adds_lease_token_to_existing_database(self) -> None:
        conn = ensure_db(self.db_path)
        conn.execute("ALTER TABLE publication_jobs DROP COLUMN lease_token")
        conn.commit()
        conn.close()
        conn = ensure_db(self.db_path)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(publication_jobs)")}
        conn.close()
        self.assertIn("lease_token", columns)

    def test_queue_status_counts_states(self) -> None:
        enqueue_publication_job(db_path=self.db_path, identifier="article-1", max_attempts=3)
        status = publication_queue_status(db_path=self.db_path)
        self.assertEqual(status["counts"]["queued"], 1)
        self.assertEqual(status["claimable_now"], 1)
        self.assertEqual(status["in_flight"], [])
        self.assertTrue(status["oldest_waiting_created_at"])


if __name__ == "__main__":
    unittest.main()
