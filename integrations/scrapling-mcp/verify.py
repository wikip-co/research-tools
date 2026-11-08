#!/usr/bin/env python3
"""Opt-in stdio handshake/tool check; optionally retrieve the public example page."""

import argparse
import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "make_request",
    "browser_fetch_once",
    "browser_open",
    "open_request_session",
    "close_session",
    "list_sessions",
    "session_make_request",
    "browser_fetch",
    "browser_extract",
    "browser_actions",
    "browser_evaluate",
    "browser_screenshot",
    "browser_observe",
    "browser_network_requests",
    "browser_network_request",
    "browser_save_state",
    "browser_load_state",
}


async def verify(fetch_example=False):
    executable = Path(__file__).resolve().parent / ".venv/bin/scrapling-mcp"
    async with (
        asyncio.timeout(60),
        stdio_client(StdioServerParameters(command=str(executable))) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        if tools != EXPECTED_TOOLS:
            raise RuntimeError(
                f"Unexpected tools: missing={EXPECTED_TOOLS - tools}, extra={tools - EXPECTED_TOOLS}"
            )
        report = {
            "transport": "stdio",
            "server": initialized.server_info.model_dump(),
            "tools": sorted(tools),
            "tool_count": len(tools),
        }
        if fetch_example:
            result = await session.call_tool(
                "make_request",
                {"url": "https://example.com", "retries": 1, "timeout": 15},
            )
            if result.is_error:
                raise RuntimeError("MCP example request failed")
            text = " ".join(
                block.text for block in result.content if block.type == "text"
            )
            if "documentation examples" not in text:
                raise RuntimeError("MCP example response did not contain expected text")
            report["example_request"] = {
                "url": "https://example.com",
                "expected_text_found": True,
            }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-example", action="store_true")
    asyncio.run(verify(parser.parse_args().fetch_example))
