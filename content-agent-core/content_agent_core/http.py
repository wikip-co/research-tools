from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen

DEFAULT_USER_AGENT = "content-agent-tools/0.1"


def fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> str:
    request_headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        **(headers or {}),
    }
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    return json.loads(fetch_text(url, headers=headers, timeout=timeout))
