import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ScrapingConfigurationTests(unittest.TestCase):
    def test_one_workspace_scrapling_lock(self):
        project = tomllib.loads((ROOT / "web-scraper/pyproject.toml").read_text())
        self.assertIn("scrapling[fetchers]==0.5.1", project["project"]["dependencies"])
        self.assertEqual((ROOT / "web-scraper/uv.lock").resolve(), ROOT / "uv.lock")
        locked = tomllib.loads((ROOT / "uv.lock").read_text())
        self.assertEqual(
            [p["version"] for p in locked["package"] if p["name"] == "scrapling"],
            ["0.5.1"],
        )

    def test_mcp_is_separate_from_production(self):
        locked = tomllib.loads((ROOT / "uv.lock").read_text())
        self.assertNotIn("mcp", {p["name"] for p in locked["package"]})
        optional = tomllib.loads(
            (ROOT / "integrations/scrapling-mcp/pyproject.toml").read_text()
        )
        self.assertEqual(optional["project"]["dependencies"], ["scrapling[ai]==0.5.1"])
        workspace = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertIn(
            "integrations/scrapling-mcp",
            workspace["tool"]["uv"]["workspace"]["exclude"],
        )

    def test_flaresolverr_is_private_and_versioned(self):
        compose = (ROOT / "deploy/docker-compose.flaresolverr.yml").read_text()
        self.assertIn('"127.0.0.1:8191:8191"', compose)
        self.assertNotIn(":latest", compose)
        self.assertNotIn("pull_policy: always", compose)
        self.assertIn('LOG_HTML: "false"', compose)
        self.assertIn("CAPTCHA_SOLVER: none", compose)

    def test_client_examples_use_installed_stdio_executable(self):
        source = ROOT / "integrations/scrapling-mcp/client_config.py"
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            script = folder / "client_config.py"
            script.write_text(source.read_text())
            executable = folder / ".venv/bin/scrapling-mcp"
            executable.parent.mkdir(parents=True)
            executable.touch()
            outputs = {}
            for client in ("codex", "claude"):
                outputs[client] = subprocess.run(
                    [sys.executable, str(script), client],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            codex = tomllib.loads(outputs["codex"])["mcp_servers"]["scrapling"]
            claude = json.loads(outputs["claude"])["mcpServers"]["scrapling"]
            self.assertEqual(codex["command"], str(executable))
            self.assertEqual(claude["command"], str(executable))
            self.assertEqual(claude["type"], "stdio")
            self.assertNotIn("--http", outputs["claude"])
