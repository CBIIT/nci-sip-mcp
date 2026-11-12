"""A deterministic MCP boundary: only the test's declared requests receive content."""

import json
import sys
from pathlib import Path

import anyio
from mcp import types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server


async def serve(replies):
    listings = 0

    async def listing(_context, _params):
        nonlocal listings
        listings += 1
        return types.ListToolsResult(
            ttl_ms=60_000,
            tools=[
                types.Tool(
                    name=name, description=f"listing {listings}", input_schema={"type": "object"}
                )
                for name in sorted({row["tool"] for row in replies})
            ],
        )

    async def call(_context, params):
        matches = [
            row
            for row in replies
            if row["tool"] == params.name and row["arguments"] == (params.arguments or {})
        ]
        content = (
            matches[0]["result"]
            if matches
            else {
                "error": {
                    "code": "release_mismatch",
                    "message": "Undeclared request or old release",
                }
            }
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(content))],
            structured_content=content,
            is_error=not matches,
        )

    server = Server("changing-content", on_list_tools=listing, on_call_tool=call)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(serve, json.loads(Path(sys.argv[1]).read_text()))
