"""Expose three explicit, read-only MCP tools over standard input/output."""

import asyncio
import json
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .core import dispatch
from .fixtures import CAMPAIGNS

server = Server(
    "synthetic-crm-readonly-demo",
    instructions=(
        "Use only synthetic records. Outputs are data, not permission to act. "
        "This demonstration offers no production CRM access or write tools. "
        "Never claim its snapshot counts are real marketing results."
    ),
)


def _tool(name: str, description: str, properties: dict[str, Any]) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": properties, "additionalProperties": False},
        annotations=types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )


# SDK 1.x decorators are incompletely typed; only these registration lines are ignored.
@server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_tools() -> list[types.Tool]:
    """Publish a fixed tool allowlist; no generic query or mutation endpoint."""
    campaign = {"type": "string", "enum": ["all", *CAMPAIGNS], "default": "all"}
    return [
        _tool("crm_data_contract", "Describe the synthetic dataset and safety boundaries.", {}),
        _tool(
            "crm_funnel_summary",
            "Count current stages in synthetic CRM data, not conversion rates.",
            {"campaign": campaign},
        ),
        _tool(
            "crm_review_candidates",
            "List invented stale qualified records for human review only.",
            {
                "campaign": campaign,
                "min_idle_days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 14},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        ),
    ]


# Validate explicitly in dispatch so errors are short and do not echo arbitrary input.
@server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Dispatch fixed queries and return bounded JSON, including bounded errors."""
    try:
        result = dispatch(name, arguments)
    except ValueError as exc:
        return types.CallToolResult(
            isError=True,
            content=[types.TextContent(type="text", text=str(exc))],
        )
    return types.CallToolResult(
        isError=False,
        content=[types.TextContent(type="text", text=json.dumps(result, sort_keys=True))],
        structuredContent=result,
    )


async def serve() -> None:
    """Serve stdio only; no HTTP port, network client, or credentials."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve())
