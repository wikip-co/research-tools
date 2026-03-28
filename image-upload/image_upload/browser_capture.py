from __future__ import annotations

import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

from content_agent_core.agent_browser import (
    agent_browser_env,
    overlay_cleanup_script,
    resolve_agent_browser_binary,
    run_agent_browser_json,
)

AGENT_BROWSER_COMMAND = os.getenv("IMAGE_UPLOAD_AGENT_BROWSER_COMMAND") or os.getenv(
    "WEB_SCRAPER_AGENT_BROWSER_COMMAND",
    "agent-browser",
)

OVERLAY_SELECTORS = [
    "[class*='cookie']",
    "[id*='cookie']",
    "[class*='consent']",
    "[id*='consent']",
    "[class*='banner']",
    "[id*='banner']",
    "[class*='popup']",
    "[id*='popup']",
    "[aria-modal='true']",
    "[role='dialog']",
]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "page-screenshot"


def default_basename(url: str) -> str:
    sanitized = slugify(url)
    digest = secrets.token_hex(4)
    return f"{sanitized[:60]}-{digest}"


def resolve_output_path(
    output_path: str | Path | None,
    *,
    basename: str,
) -> tuple[Path, bool, str | None]:
    if output_path is None:
        temp_dir = Path(tempfile.mkdtemp(prefix="page-screenshot-"))
        return temp_dir / f"{basename}.png", True, str(temp_dir)

    path = Path(output_path).expanduser().resolve()
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        path = path / f"{basename}.png" if not path.suffix else path.with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, False, None


def capture_page_screenshot(
    url: str,
    *,
    output_path: str | Path | None = None,
    basename: str | None = None,
    full_page: bool = False,
    annotate: bool = False,
    wait_ms: int = 1500,
) -> dict[str, Any]:
    binary = resolve_agent_browser_binary(AGENT_BROWSER_COMMAND)
    if not binary:
        raise RuntimeError("agent-browser is not installed")

    basename = slugify(basename or default_basename(url))
    destination, temporary, temp_dir = resolve_output_path(output_path, basename=basename)
    env = agent_browser_env(
        explicit_path_env_var="IMAGE_UPLOAD_AGENT_BROWSER_EXECUTABLE_PATH"
    )
    session = f"page-shot-{secrets.token_hex(6)}"

    run_agent_browser_json(binary, session, ["open", url], env=env, timeout=60)
    try:
        run_agent_browser_json(
            binary,
            session,
            ["wait", str(wait_ms)],
            env=env,
            timeout=20,
        )
    except RuntimeError:
        pass

    try:
        run_agent_browser_json(
            binary,
            session,
            ["eval", overlay_cleanup_script(OVERLAY_SELECTORS)],
            env=env,
            timeout=15,
        )
        run_agent_browser_json(binary, session, ["wait", "250"], env=env, timeout=10)
    except RuntimeError:
        pass

    screenshot_args = ["screenshot", str(destination)]
    if full_page:
        screenshot_args.append("--full")
    if annotate:
        screenshot_args.append("--annotate")
    run_agent_browser_json(binary, session, screenshot_args, env=env, timeout=60)

    title_payload = run_agent_browser_json(
        binary,
        session,
        ["eval", "document.title"],
        env=env,
        timeout=10,
    )
    url_payload = run_agent_browser_json(
        binary,
        session,
        ["eval", "location.href"],
        env=env,
        timeout=10,
    )
    if not destination.is_file():
        raise RuntimeError(f"Screenshot was not created: {destination}")

    return {
        "requested_url": url,
        "final_url": url_payload.get("result", url),
        "page_title": title_payload.get("result", ""),
        "local_path": str(destination),
        "temporary": temporary,
        "temporary_dir": temp_dir,
        "full_page": full_page,
        "annotate": annotate,
        "wait_ms": wait_ms,
        "bytes": destination.stat().st_size,
    }


def cleanup_capture(capture: dict[str, Any]) -> None:
    if not capture.get("temporary"):
        return
    local_path = capture.get("local_path")
    temp_dir = capture.get("temporary_dir")
    if local_path:
        path = Path(local_path)
        if path.exists():
            path.unlink()
    if temp_dir:
        temp_path = Path(temp_dir)
        if temp_path.exists():
            shutil.rmtree(temp_path, ignore_errors=True)
