"""A human-in-the-loop approval gate for agent actions, exposed over MCP.

The store is usable directly as a Python library, without an MCP client::

    from mcp_approvals import ApprovalStore

    with ApprovalStore("approvals.db") as store:
        request = store.submit(
            action="send_customer_email",
            payload={"to": "cfo@example.com"},
            risk="high",
            requester="outreach-agent",
        )
        store.decide(
            request_id=request.request_id,
            decision="approve",
            decider="mark",
            reason="Reviewed the draft.",
        )
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, load_config
from .hashchain import ChainVerification, verify_chain
from .models import (
    AlreadyDecidedError,
    ApprovalError,
    ApprovalRequest,
    AuditEntry,
    Decision,
    EventType,
    RequestNotFoundError,
    RiskLevel,
    SelfApprovalError,
    Status,
)
from .store import ApprovalStore

__all__ = [
    "AlreadyDecidedError",
    "ApprovalError",
    "ApprovalRequest",
    "ApprovalStore",
    "AuditEntry",
    "ChainVerification",
    "Config",
    "Decision",
    "EventType",
    "RequestNotFoundError",
    "RiskLevel",
    "SelfApprovalError",
    "Status",
    "__version__",
    "load_config",
    "verify_chain",
]
