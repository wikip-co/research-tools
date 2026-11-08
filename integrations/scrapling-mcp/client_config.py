#!/usr/bin/env python3
"""Print client examples for this installation; never write global settings."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=["codex", "claude"])
    args = parser.parse_args()
    executable = Path(__file__).resolve().parent / ".venv/bin/scrapling-mcp"
    if not executable.is_file():
        parser.error("Run uv sync --project integrations/scrapling-mcp --locked first")
    if args.client == "codex":
        print("[mcp_servers.scrapling]\ncommand = " + json.dumps(str(executable)))
        print("startup_timeout_sec = 30\ntool_timeout_sec = 60")
        print('enabled_tools = ["make_request", "browser_fetch_once"]')
    else:
        print(
            json.dumps(
                {
                    "mcpServers": {
                        "scrapling": {
                            "type": "stdio",
                            "command": str(executable),
                            "args": [],
                        }
                    }
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
