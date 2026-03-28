from __future__ import annotations

import os
from pathlib import Path


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_dotenv_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            continue
        values[normalized_key] = _strip_quotes(value.strip())
    return values


def load_dotenv_files(
    candidate_paths: list[str | Path],
    *,
    environ: dict[str, str] | None = None,
    override: bool = False,
) -> dict[str, str]:
    env = environ if environ is not None else os.environ
    loaded: dict[str, str] = {}

    for candidate in candidate_paths:
        path = Path(candidate).expanduser()
        if not path.is_file():
            continue
        parsed = parse_dotenv_text(path.read_text(encoding="utf-8"))
        for key, value in parsed.items():
            if override or key not in env:
                env[key] = value
            loaded[key] = value
    return loaded
