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

    def test_doctor_fails_when_configured_content_repo_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = _isolated_content_env(Path(tmpdir) / "none", Path(tmpdir) / "managed")
            env["CONTENT_REPO_ROOT"] = str(Path(tmpdir) / "missing")
            result = subprocess.run(
                ["./agent-workflow", "doctor"], cwd=REPO_ROOT, env=env, capture_output=True, text=True
            )
            payload = json.loads(result.stdout)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("CONTENT_REPO_ROOT is not a git repository", payload["result"]["problems"][0])

    def test_sync_content_repo_reports_failure_instead_of_silent_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "content"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "--allow-empty", "-m", "init"],
                cwd=repo, check=True, capture_output=True,
            )
            env = _isolated_content_env(repo, Path(tmpdir) / "managed")
            env["CONTENT_REPO_ROOT"] = str(repo)
            env["CONTENT_REPO_REF"] = "no-such-ref"
            result = subprocess.run(
                ["./agent-workflow", "sync-content-repo"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)["ok"])
            self.assertIn("sync step failed", result.stderr)

    def test_setup_counts_errors_without_tripping_errexit(self) -> None:
        # Under `set -e`, ((errors++)) returns status 1 when errors is 0 and
        # aborted setup at its first failed check, hiding every later one.
        script = (REPO_ROOT / "agent-workflow").read_text(encoding="utf-8")
        self.assertNotIn("((errors++))", script)
        probe = subprocess.run(
            ["bash", "-c", "set -euo pipefail; errors=0; errors=$((errors + 1)); echo reached"],
            capture_output=True, text=True,
        )
        self.assertEqual(probe.stdout.strip(), "reached")


if __name__ == "__main__":
    unittest.main()
