"""Tests for the MCP tool surface and the end-to-end gate behaviour."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_approvals import server
from mcp_approvals.config import Config
from mcp_approvals.store import ApprovalStore

EXPECTED_TOOLS = {
    "submit_for_approval",
    "list_pending",
    "decide",
    "get_status",
    "audit_trail",
    "verify_audit_chain",
    "gate_policy",
}


@pytest.fixture(autouse=True)
def isolated_server(tmp_path: Path) -> Iterator[None]:
    """Point the module-level server at a throwaway database."""
    db_path = tmp_path / "server" / "approvals.db"
    store = ApprovalStore(db_path)
    server.configure(
        Config(db_path=db_path, default_ttl_hours=None, allow_self_approval=False),
        store=store,
    )
    try:
        yield
    finally:
        store.close()
        server._store = None  # noqa: SLF001 - deliberate reset between tests
        server._config = None  # noqa: SLF001


@pytest.fixture(scope="module")
def tools():
    """The registered tool definitions, resolved once."""
    import asyncio

    return asyncio.run(server.mcp.list_tools())


def submit(**overrides) -> dict:
    """Submit a representative request through the tool layer."""
    payload = {
        "action": "send_customer_email",
        "payload": {"to": "cfo@example.com", "subject": "Q3 proposal"},
        "risk": "high",
        "requester": "outreach-agent",
        "context": "Drafted after the discovery call.",
    }
    payload.update(overrides)
    return server.submit_for_approval(**payload)


class TestToolSurface:
    def test_expected_tools_are_registered(self, tools) -> None:
        assert {tool.name for tool in tools} == EXPECTED_TOOLS

    def test_registered_tool_names_helper_agrees(self) -> None:
        assert set(server.registered_tool_names()) == EXPECTED_TOOLS

    def test_every_tool_has_a_useful_description(self, tools) -> None:
        for tool in tools:
            assert tool.description, f"{tool.name} has no description"
            assert len(tool.description) > 80, f"{tool.name} description is too thin"

    def test_required_arguments_are_declared(self, tools) -> None:
        by_name = {tool.name: tool for tool in tools}
        assert set(by_name["submit_for_approval"].inputSchema["required"]) == {
            "action",
            "payload",
            "risk",
            "requester",
        }
        assert set(by_name["decide"].inputSchema["required"]) == {
            "request_id",
            "decision",
            "decider",
        }
        assert by_name["get_status"].inputSchema["required"] == ["request_id"]

    def test_tools_with_no_arguments_declare_none(self, tools) -> None:
        by_name = {tool.name: tool for tool in tools}
        assert by_name["verify_audit_chain"].inputSchema.get("required", []) == []

    def test_server_instructions_state_the_contract(self) -> None:
        assert "proceed ONLY when it returns" in server.mcp.instructions


class TestSubmitTool:
    def test_returns_a_pending_request(self) -> None:
        result = submit()
        assert result["ok"] is True
        assert result["status"] == "pending"
        assert result["request_id"].startswith("ar_")
        assert "Do not take the action" in result["next_step"]

    def test_invalid_risk_raises(self) -> None:
        with pytest.raises(ValueError, match="risk must be one of"):
            submit(risk="spicy")

    def test_output_is_json_serialisable(self) -> None:
        json.dumps(submit())


class TestGetStatusTool:
    def test_pending_blocks_the_agent(self) -> None:
        request_id = submit()["request_id"]
        status = server.get_status(request_id)
        assert status["may_proceed"] is False
        assert "Do not take the action" in status["guidance"]

    def test_approved_permits_the_agent(self) -> None:
        request_id = submit()["request_id"]
        server.decide(
            request_id=request_id,
            decision="approve",
            decider="mark",
            reason="Reviewed the draft.",
        )
        status = server.get_status(request_id)
        assert status["may_proceed"] is True
        assert "may proceed" in status["guidance"]

    def test_rejected_blocks_the_agent(self) -> None:
        request_id = submit()["request_id"]
        server.decide(
            request_id=request_id,
            decision="reject",
            decider="mark",
            reason="Wrong recipient.",
        )
        status = server.get_status(request_id)
        assert status["may_proceed"] is False
        assert "Do not resubmit unchanged" in status["guidance"]

    def test_unknown_id_returns_a_structured_error(self) -> None:
        result = server.get_status("ar_nope")
        assert result["ok"] is False
        assert result["error"] == "request_not_found"


class TestDecideTool:
    def test_double_decision_returns_already_decided(self) -> None:
        request_id = submit()["request_id"]
        server.decide(
            request_id=request_id,
            decision="approve",
            decider="mark",
            reason="Fine.",
        )
        second = server.decide(
            request_id=request_id,
            decision="reject",
            decider="someone-else",
            reason="No.",
        )
        assert second["ok"] is False
        assert second["error"] == "already_decided"
        assert second["current_status"] == "approved"
        assert second["current"]["decider"] == "mark"

    def test_self_approval_returns_a_structured_error(self) -> None:
        request_id = submit(requester="outreach-agent")["request_id"]
        result = server.decide(
            request_id=request_id,
            decision="approve",
            decider="outreach-agent",
            reason="Fine.",
        )
        assert result["ok"] is False
        assert result["error"] == "self_approval_blocked"
        assert server.get_status(request_id)["status"] == "pending"

    def test_unknown_id_returns_a_structured_error(self) -> None:
        result = server.decide(
            request_id="ar_nope", decision="approve", decider="mark", reason="x"
        )
        assert result["ok"] is False
        assert result["error"] == "request_not_found"

    def test_missing_reason_raises(self) -> None:
        request_id = submit(risk="critical")["request_id"]
        with pytest.raises(ValueError, match="reason is required"):
            server.decide(request_id=request_id, decision="approve", decider="mark")


class TestListPendingTool:
    def test_lists_only_pending_by_default(self) -> None:
        keep = submit(action="keep_me")["request_id"]
        decided = submit(action="decide_me")["request_id"]
        server.decide(
            request_id=decided, decision="approve", decider="mark", reason="Fine."
        )
        listing = server.list_pending()
        assert [item["request_id"] for item in listing["requests"]] == [keep]
        assert listing["counts_by_status"]["approved"] == 1

    def test_status_all_returns_everything(self) -> None:
        submit()
        decided = submit()["request_id"]
        server.decide(
            request_id=decided, decision="approve", decider="mark", reason="Fine."
        )
        assert server.list_pending(status="all")["count"] == 2

    def test_filters_are_applied(self) -> None:
        submit(risk="low", action="low_risk_thing")
        wanted = submit(risk="critical", action="drop_database")["request_id"]
        listing = server.list_pending(risk="critical", action_contains="database")
        assert [item["request_id"] for item in listing["requests"]] == [wanted]

    def test_invalid_status_raises_with_valid_options(self) -> None:
        with pytest.raises(ValueError, match="status must be 'all' or one of"):
            server.list_pending(status="nearly")

    def test_output_is_json_serialisable(self) -> None:
        submit()
        json.dumps(server.list_pending())


class TestAuditTools:
    def test_trail_records_submit_and_decide(self) -> None:
        request_id = submit()["request_id"]
        server.decide(
            request_id=request_id,
            decision="approve",
            decider="mark",
            reason="Reviewed.",
        )
        trail = server.audit_trail(request_id)
        assert [entry["event_type"] for entry in trail["entries"]] == [
            "submitted",
            "decided",
        ]
        assert trail["entries"][0]["prev_hash"] == "0" * 64
        assert trail["entries"][1]["prev_hash"] == trail["entries"][0]["entry_hash"]
        assert trail["chain_head"] == trail["entries"][-1]["entry_hash"]

    def test_all_scope(self) -> None:
        submit()
        submit()
        assert server.audit_trail("all")["count"] == 2
        assert server.audit_trail()["count"] == 2

    def test_unknown_id_returns_a_structured_error(self) -> None:
        result = server.audit_trail("ar_nope")
        assert result["ok"] is False
        assert result["error"] == "request_not_found"

    def test_empty_log_reports_the_genesis_head(self) -> None:
        trail = server.audit_trail()
        assert trail["count"] == 0
        assert trail["chain_head"] == "0" * 64

    def test_verify_reports_a_healthy_chain(self) -> None:
        request_id = submit()["request_id"]
        server.decide(
            request_id=request_id, decision="approve", decider="mark", reason="Fine."
        )
        result = server.verify_audit_chain()
        assert result["valid"] is True
        assert result["entries_checked"] == 2
        assert "cannot detect a wholesale rewrite" in result["scope_note"]


class TestGatePolicy:
    def test_reports_the_configured_policy(self) -> None:
        policy = server.gate_policy()
        assert policy["self_approval_allowed"] is False
        assert policy["decisions_are_immutable"] is True
        assert policy["default_ttl_hours_by_risk"]["critical"] == 8
        assert policy["default_ttl_hours_by_risk"]["low"] == 168
        assert set(policy["reason_required_for_risks"]) == {"high", "critical"}
        assert "fail closed" in policy["expiry_behaviour"]

    def test_output_is_json_serialisable(self) -> None:
        json.dumps(server.gate_policy())


class TestEndToEnd:
    def test_the_full_gated_workflow(self) -> None:
        # 1. The agent asks instead of acting.
        submitted = submit(action="wire_transfer", risk="critical")
        request_id = submitted["request_id"]
        assert server.get_status(request_id)["may_proceed"] is False

        # 2. The request appears in the human's queue.
        queue = server.list_pending(risk="critical")
        assert request_id in {item["request_id"] for item in queue["requests"]}

        # 3. The human decides, with a reason because the risk is critical.
        decided = server.decide(
            request_id=request_id,
            decision="approve",
            decider="mark",
            reason="Confirmed the beneficiary by phone.",
        )
        assert decided["ok"] is True

        # 4. The agent may now proceed, and only now.
        assert server.get_status(request_id)["may_proceed"] is True

        # 5. The whole thing is replayable and provably unaltered.
        trail = server.audit_trail(request_id)
        assert trail["count"] == 2
        assert trail["entries"][1]["detail"]["reason"] == (
            "Confirmed the beneficiary by phone."
        )
        assert server.verify_audit_chain()["valid"] is True

    def test_a_rejected_action_stays_rejected(self) -> None:
        request_id = submit(action="delete_customer_data")["request_id"]
        server.decide(
            request_id=request_id,
            decision="reject",
            decider="mark",
            reason="No retention exemption.",
        )
        retry = server.decide(
            request_id=request_id,
            decision="approve",
            decider="mark",
            reason="Second thoughts.",
        )
        assert retry["ok"] is False
        assert server.get_status(request_id)["may_proceed"] is False
        assert server.verify_audit_chain()["valid"] is True


class TestToolDispatch:
    def test_call_through_the_dispatcher(self) -> None:
        import asyncio

        result = asyncio.run(server.mcp.call_tool("gate_policy", {}))
        payload = result[1] if isinstance(result, tuple) else result
        assert payload["ok"] is True

    def test_invalid_argument_surfaces_an_error(self) -> None:
        import asyncio

        with pytest.raises(Exception):  # noqa: B017 - the SDK's error type is not public API
            asyncio.run(
                server.mcp.call_tool(
                    "submit_for_approval",
                    {
                        "action": "",
                        "payload": {},
                        "risk": "low",
                        "requester": "agent",
                    },
                )
            )
