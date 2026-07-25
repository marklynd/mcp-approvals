#!/usr/bin/env python3
"""Import the server, list its tools, and walk one full gated workflow.

Uses a throwaway database in the system temp directory, so it never touches a
real approval log::

    python examples/smoke_test.py

Exits non-zero if any tool fails, if a decided request can be overwritten, or if
the audit chain does not verify.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from mcp_approvals import __version__  # noqa: E402
from mcp_approvals.config import Config  # noqa: E402
from mcp_approvals.server import (  # noqa: E402
    audit_trail,
    configure,
    decide,
    gate_policy,
    get_status,
    list_pending,
    mcp,
    registered_tool_names,
    submit_for_approval,
    verify_audit_chain,
)
from mcp_approvals.store import ApprovalStore  # noqa: E402


def _preview(payload: Any, limit: int = 200) -> str:
    """Return a truncated one-line JSON preview of ``payload``."""
    text = json.dumps(payload, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + "..."


def main() -> int:
    """Run the smoke test. Returns a process exit code."""
    print(f"mcp-approvals {__version__}")
    print(f"server name: {mcp.name}")
    print()

    registered = registered_tool_names()
    print(f"Registered tools ({len(registered)}):")
    for name in registered:
        print(f"  - {name}")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke.db"
        store = ApprovalStore(db_path)
        configure(
            Config(db_path=db_path, default_ttl_hours=None, allow_self_approval=False),
            store=store,
        )

        failures: list[str] = []

        print("Walking a full gated workflow:")

        submitted = submit_for_approval(
            action="wire_transfer",
            payload={"amount_usd": 48000, "beneficiary": "Acme Ltd"},
            risk="critical",
            requester="finance-agent",
            context="Invoice INV-2041, matched to PO-889.",
        )
        request_id = submitted["request_id"]
        print(f"  1. submit_for_approval -> {request_id} ({submitted['status']})")

        before = get_status(request_id)
        print(f"  2. get_status          -> may_proceed={before['may_proceed']}")
        if before["may_proceed"]:
            failures.append("A pending request reported may_proceed=true")

        queue = list_pending(risk="critical")
        print(f"  3. list_pending        -> {queue['count']} awaiting decision")
        if request_id not in {item["request_id"] for item in queue["requests"]}:
            failures.append("The submitted request did not appear in the queue")

        self_approval = decide(
            request_id=request_id,
            decision="approve",
            decider="finance-agent",
            reason="Looks fine to me.",
        )
        print(f"  4. decide (self)       -> blocked: {self_approval.get('error')}")
        if self_approval.get("error") != "self_approval_blocked":
            failures.append("Self-approval was not blocked")

        decided = decide(
            request_id=request_id,
            decision="approve",
            decider="mark",
            reason="Confirmed the beneficiary by phone.",
        )
        print(f"  5. decide (human)      -> {decided['status']} by {decided['decider']}")

        after = get_status(request_id)
        print(f"  6. get_status          -> may_proceed={after['may_proceed']}")
        if not after["may_proceed"]:
            failures.append("An approved request did not report may_proceed=true")

        overwrite = decide(
            request_id=request_id,
            decision="reject",
            decider="someone-else",
            reason="Changed my mind.",
        )
        print(f"  7. decide again        -> refused: {overwrite.get('error')}")
        if overwrite.get("error") != "already_decided":
            failures.append("A decided request was not protected from overwrite")

        trail = audit_trail(request_id)
        print(
            f"  8. audit_trail         -> {trail['count']} entries, "
            f"head {trail['chain_head'][:12]}..."
        )

        verification = verify_audit_chain()
        print(f"  9. verify_audit_chain  -> valid={verification['valid']}")
        if not verification["valid"]:
            failures.append("The audit chain failed verification")

        policy = gate_policy()
        print(f" 10. gate_policy         -> {_preview(policy)}")

        store.close()

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(f"All {len(registered)} tools registered; gated workflow behaved correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
