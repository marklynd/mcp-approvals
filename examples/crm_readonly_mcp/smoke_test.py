"""Exercise real MCP initialization, discovery, calls, and rejection over stdio."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .core import TOOL_NAMES, dispatch


async def run_smoke() -> dict[str, Any]:
    """Use an actual SDK client and child process, not a mocked tool call."""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "examples.crm_readonly_mcp.server"],
        cwd=str(Path(__file__).resolve().parents[2]),
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == set(TOOL_NAMES)
        for tool in tools.tools:
            assert tool.inputSchema["additionalProperties"] is False
            assert tool.annotations is not None and tool.annotations.readOnlyHint is True
        outputs = {}
        for name in TOOL_NAMES:
            result = await session.call_tool(name, {})
            assert not result.isError
            assert result.structuredContent == dispatch(name, {})
            outputs[name] = result.structuredContent
        negative_calls: list[tuple[str, dict[str, Any]]] = [
            ("send_email", {}),
            ("update_contact", {"record_id": "SYN-002"}),
            ("crm_review_candidates", {"limit": 21}),
            ("crm_review_candidates", {"limit": True}),
            ("crm_review_candidates", {"limit": 1.5}),
            ("crm_review_candidates", {"min_idle_days": 0}),
            ("crm_review_candidates", {"fields": ["internal_note"]}),
            ("crm_funnel_summary", {"campaign": "webinar; DROP TABLE leads"}),
            ("crm_funnel_summary", {"path": "/etc/passwd"}),
            ("crm_data_contract", {"sql": "SELECT * FROM leads"}),
        ]
        for name, arguments in negative_calls:
            result = await session.call_tool(name, arguments)
            assert result.isError, (name, arguments)
            assert len(result.model_dump_json()) < 2048
        # Server remains usable and deterministic after invalid/mutation attempts.
        result = await session.call_tool("crm_review_candidates", {})
        assert result.structuredContent == outputs["crm_review_candidates"]
    return {
        "status": "PASS",
        "transport": "MCP stdio (actual SDK client and server process)",
        "protocol_version": initialized.protocolVersion,
        "tools_discovered": sorted(TOOL_NAMES),
        "successful_tool_calls": 4,
        "negative_calls_rejected": len(negative_calls),
        "funnel": outputs["crm_funnel_summary"],
        "review_candidates": outputs["crm_review_candidates"],
    }


if __name__ == "__main__":
    print(
        json.dumps(asyncio.run(asyncio.wait_for(run_smoke(), timeout=30)), indent=2, sort_keys=True)
    )
