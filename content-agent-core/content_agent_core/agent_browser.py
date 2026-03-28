from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any


def resolve_agent_browser_binary(command: str = "agent-browser") -> str | None:
    return shutil.which(command)


def agent_browser_env(*, explicit_path_env_var: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if explicit_path_env_var:
        explicit_path = os.getenv(explicit_path_env_var)
        if explicit_path and "AGENT_BROWSER_EXECUTABLE_PATH" not in env:
            env["AGENT_BROWSER_EXECUTABLE_PATH"] = explicit_path
    env.setdefault("AGENT_BROWSER_IDLE_TIMEOUT_MS", "10000")
    return env


def run_agent_browser_json(
    binary: str,
    session: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 45,
) -> dict[str, Any]:
    command = [binary, "--json", "--session", session, *args]
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    payload_text = stdout or stderr
    if not payload_text:
        raise RuntimeError(f"agent-browser produced no output: {' '.join(command)}")

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"agent-browser returned non-JSON output: {' '.join(command)}\nstdout={stdout}\nstderr={stderr}"
        ) from exc

    if result.returncode != 0 or not payload.get("success", False):
        raise RuntimeError(
            payload.get("error", f"agent-browser command failed: {' '.join(command)}")
        )

    return payload.get("data", {})


def overlay_cleanup_script(selectors: list[str]) -> str:
    selectors_json = json.dumps(selectors)
    return f"""
(() => {{
  const selectors = {selectors_json};
  let removed = 0;
  for (const selector of selectors) {{
    for (const element of document.querySelectorAll(selector)) {{
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      const looksLikeOverlay =
        style.position === "fixed" ||
        style.position === "sticky" ||
        element.getAttribute("aria-modal") === "true" ||
        element.getAttribute("role") === "dialog" ||
        (rect.width >= window.innerWidth * 0.75 && rect.height >= window.innerHeight * 0.2);
      if (looksLikeOverlay) {{
        element.remove();
        removed += 1;
      }}
    }}
  }}
  document.documentElement.style.overflow = "auto";
  if (document.body) {{
    document.body.style.overflow = "auto";
  }}
  return removed;
}})()
""".strip()
