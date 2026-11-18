from __future__ import annotations

import argparse
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wiki_automation import cli


def worker_args(output_dir: str, max_jobs: int = 2) -> argparse.Namespace:
    return argparse.Namespace(
        worker="worker-a",
        lease_seconds=900,
        max_jobs=max_jobs,
        base_url="",
        backend="local",
        model="",
        base_ref="origin/main",
        limit=12,
        max_draft_attempts=3,
        claim_policy="integrated",
        pipeline="simple",
        publish=False,
        output_dir=output_dir,
    )


def job(job_id: int, token: int, attempts: int = 1, max_attempts: int = 3) -> dict:
    return {
        "claimed": True,
        "job": {
            "job_id": job_id,
            "lease_token": token,
            "source_url": f"https://example.org/{job_id}",
            "attempt_count": attempts,
            "max_attempts": max_attempts,
        },
    }


class FakeQueue:
    """Stands in for the gmail-reader CLI and records every call."""

    def __init__(self, claims: list[dict], lost_states: set[str] | None = None) -> None:
        self.claims = list(claims)
        self.lost_states = lost_states or set()
        self.updates: list[list[str]] = []

    def __call__(self, _tool_dir, command: list[str]) -> dict:
        if "claim-publication" in command:
            return self.claims.pop(0) if self.claims else {"claimed": False, "job": None}
        self.updates.append(command)
        state = command[command.index("--state") + 1]
        if state in self.lost_states:
            raise cli.ToolError("Publication job is not leased by worker-a", "lease_lost")
        return {"job": {}}


class LocalWorkerTests(unittest.TestCase):
    def run_worker(self, queue: FakeQueue, publish, max_jobs: int = 2) -> dict:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            cli, "run_json_tool", queue
        ), mock.patch.object(cli, "ensure_tool_dir"), mock.patch(
            "wiki_automation.local_publish.run_local_publish", publish
        ), mock.patch(
            "wiki_automation.local_publish.configured_client_values",
            return_value=("http://127.0.0.1:1/v1", "model"),
        ):
            return cli.local_worker_command(worker_args(tmp, max_jobs))

    def test_every_report_carries_the_lease_token_and_progress_renews(self) -> None:
        queue = FakeQueue([job(7, token=3)])

        def publish(**kwargs):
            kwargs["progress"]("drafting")
            return {"status": "validated_draft", "run_id": "run-1"}

        result = self.run_worker(queue, publish)
        self.assertEqual(result["processed"][0]["status"], "validated_draft")
        self.assertEqual(len(queue.updates), 3)
        for command in queue.updates:
            self.assertEqual(command[command.index("--lease-token") + 1], "3")
        self.assertIn("--lease-seconds", queue.updates[0])
        self.assertIn("--lease-seconds", queue.updates[1])
        self.assertNotIn("--lease-seconds", queue.updates[2])

    def test_sub_stage_progress_is_reported_as_a_valid_job_state(self) -> None:
        queue = FakeQueue([job(7, token=3)])

        def publish(**kwargs):
            kwargs["progress"]("drafting_attempt_1")
            kwargs["progress"]("deterministic_validation_attempt_1")
            kwargs["progress"]("critic_attempt_2")
            return {"status": "validated_draft"}

        result = self.run_worker(queue, publish)
        self.assertEqual(result["processed"][0]["status"], "validated_draft")
        states = [c[c.index("--state") + 1] for c in queue.updates]
        self.assertEqual(states[:4], ["scraping", "drafting", "validating", "drafting"])
        for command in queue.updates[:4]:
            self.assertIn("--lease-seconds", command)

    def test_every_stage_run_local_publish_reports_maps_to_a_job_state(self) -> None:
        try:
            from gmail_reader.app import PUBLICATION_JOB_STATES
        except ImportError:
            self.skipTest("gmail_reader is not installed in this environment")
        source = (Path(cli.__file__).parent / "local_publish.py").read_text(encoding="utf-8")
        stages = re.findall(r'progress\(f?"([^"]+)"\)', source)
        self.assertIn("matching", stages)
        for stage in stages:
            concrete = re.sub(r"\{[^}]*\}", "1", stage)
            self.assertIn(cli.publication_job_state_for_stage(concrete), PUBLICATION_JOB_STATES, stage)

    def test_lost_lease_stops_the_run_and_the_worker_continues(self) -> None:
        queue = FakeQueue([job(7, token=3), job(8, token=1)], lost_states={"drafting"})
        published: list[str] = []

        def publish(**kwargs):
            if kwargs["source"].endswith("/7"):
                kwargs["progress"]("drafting")
                published.append("job 7 reached publication")
            return {"status": "validated_draft"}

        result = self.run_worker(queue, publish)
        self.assertEqual(published, [])
        self.assertEqual(
            [(item["job_id"], item["status"]) for item in result["processed"]],
            [(7, "lease_lost"), (8, "validated_draft")],
        )
        # The stale worker must not write retry/failed over the new owner's job.
        job7_states = [
            c[c.index("--state") + 1]
            for c in queue.updates
            if c[c.index("set-publication-job-state") + 1] == "7"
        ]
        self.assertEqual(job7_states, ["scraping", "drafting"])

    def test_failure_on_final_attempt_is_recorded_as_failed(self) -> None:
        queue = FakeQueue([job(7, token=1, attempts=3, max_attempts=3)])

        def publish(**kwargs):
            raise RuntimeError("model timeout")

        result = self.run_worker(queue, publish)
        self.assertEqual(result["processed"][0]["status"], "failed")
        last = queue.updates[-1]
        self.assertEqual(last[last.index("--state") + 1], "failed")
        self.assertEqual(last[last.index("--error") + 1], "model timeout")

    def test_unrecordable_failure_does_not_crash_the_worker(self) -> None:
        queue = FakeQueue([job(7, token=1), job(8, token=1)], lost_states={"retry"})

        def publish(**kwargs):
            if kwargs["source"].endswith("/7"):
                raise RuntimeError("scrape failed")
            return {"status": "needs_review"}

        result = self.run_worker(queue, publish)
        self.assertEqual(
            [(item["job_id"], item["status"]) for item in result["processed"]],
            [(7, "lease_lost"), (8, "needs_review")],
        )


class RunCommandTests(unittest.TestCase):
    def test_commands_cannot_prompt_and_time_out(self) -> None:
        import subprocess
        import sys
        from pathlib import Path

        from wiki_automation import local_publish

        seen = local_publish.run_command(
            [sys.executable, "-c", "import os; print(os.environ['GIT_TERMINAL_PROMPT'], os.environ['GH_PROMPT_DISABLED'])"],
            cwd=Path.cwd(),
        )
        self.assertEqual(seen, "0 1")
        with mock.patch.object(local_publish, "COMMAND_TIMEOUT_SECONDS", 0.2):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                local_publish.run_command(
                    [sys.executable, "-c", "import time; time.sleep(5)"], cwd=Path.cwd()
                )
        self.assertTrue(issubclass(subprocess.TimeoutExpired, Exception))


if __name__ == "__main__":
    unittest.main()
