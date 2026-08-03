import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# Host shells and research-tools/.env often set a direct production CONTENT_REPO_ROOT
# (e.g. /home/anthony/Research/content). That path must not leak into managed-clone tests.
_CONTENT_ENV_KEYS = (
    "CONTENT_REPO_ROOT",
    "CONTENT_REPO_SOURCE_PATH",
    "CONTENT_REPO_MANAGED_ROOT",
    "CONTENT_REPO_GIT_URL",
    "CONTENT_REPO_HOST_PATH",
    "CONTENT_REPO_REF",
)


def _isolated_content_env(source_repo: Path, managed_repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in _CONTENT_ENV_KEYS:
        env.pop(key, None)
    # Prevent load_dotenv from reintroducing production CONTENT_REPO_ROOT from .env
    env["AGENT_WORKFLOW_SKIP_DOTENV"] = "1"
    # Explicit empty root selects managed-clone mode in ensure_content_repo_ready
    env["CONTENT_REPO_ROOT"] = ""
    env["CONTENT_REPO_SOURCE_PATH"] = str(source_repo)
    env["CONTENT_REPO_MANAGED_ROOT"] = str(managed_repo)
    return env


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

            env = _isolated_content_env(source_repo, managed_repo)

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
            # Direct root should not be the host production path when testing managed mode
            self.assertEqual(
                payload["result"]["paths"]["content_repo_root"],
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

            env = _isolated_content_env(source_repo, managed_repo)

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
