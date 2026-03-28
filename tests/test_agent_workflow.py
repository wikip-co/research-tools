import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class AgentWorkflowTests(unittest.TestCase):
    def test_doctor_reports_configured_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_repo = Path(tmpdir) / "content-source"
            managed_repo = Path(tmpdir) / "managed-content"
            source_repo.mkdir()
            subprocess.run(["git", "init"], cwd=source_repo, check=True, capture_output=True)
            (source_repo / "article.md").write_text(
                "---\n"
                "title: Alpha\n"
                "tags:\n"
                "  - Research\n"
                "---\n\n"
                "Alpha article body.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "article.md"], cwd=source_repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
                cwd=source_repo,
                check=True,
                capture_output=True,
            )

            env = os.environ.copy()
            env["CONTENT_REPO_SOURCE_PATH"] = str(source_repo)
            env["CONTENT_REPO_MANAGED_ROOT"] = str(managed_repo)

            result = subprocess.run(
                ["./agent-workflow", "doctor"],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(
                payload["result"]["paths"]["managed_content_repo_root"],
                str(managed_repo),
            )

    def test_search_uses_managed_clone_when_source_repo_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_repo = Path(tmpdir) / "content-source"
            managed_repo = Path(tmpdir) / "managed-content"
            source_repo.mkdir()
            subprocess.run(["git", "init"], cwd=source_repo, check=True, capture_output=True)
            (source_repo / "article.md").write_text(
                "---\n"
                "title: Alpha Article\n"
                "tags:\n"
                "  - Research\n"
                "---\n\n"
                "Alpha pipeline details.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "article.md"], cwd=source_repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
                cwd=source_repo,
                check=True,
                capture_output=True,
            )

            env = os.environ.copy()
            env["CONTENT_REPO_SOURCE_PATH"] = str(source_repo)
            env["CONTENT_REPO_MANAGED_ROOT"] = str(managed_repo)

            result = subprocess.run(
                ["./agent-workflow", "search", "Alpha pipeline"],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["matches"][0]["path"], "article.md")
            self.assertTrue((managed_repo / ".git").exists())


if __name__ == "__main__":
    unittest.main()
