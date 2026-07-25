"""MCP server exposing a human-in-the-loop approval gate.

Run over stdio::

    python -m mcp_approvals

or::

    mcp-approvals

The contract an agent should follow:

1. Call ``submit_for_approval`` **before** taking a consequential action.
2. Poll ``get_status``. Proceed **only** on ``"approved"``.
3. Treat ``pending``, ``rejected`` and ``expired`` identically: do not act.

The server cannot enforce step 3 - it does not sit in the network path of
whatever the agent is doing. What it does guarantee is that the decision, or the
absence of one, is recorded in an append-only, hash-chained log that will not
agree with you afterwards if you skipped the gate.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .config import Config, load_config
from .hashchain import GENESIS_HASH
from .models import (
    DEFAULT_TTL_HOURS,
    REASON_REQUIRED_RISKS,
    AlreadyDecidedError,
    RequestNotFoundError,
    RiskLevel,
    SelfApprovalError,
    Status,
)
from .store import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, ApprovalStore, summarise

INSTRUCTIONS = """\
A human-in-the-loop approval gate for agent actions.

Before taking a consequential action - sending an external email, moving money,
deleting data, changing production configuration, publishing anything - call
submit_for_approval and stop. Poll get_status and proceed ONLY when it returns
"approved". Pending, rejected and expired all mean: do not act.

Every submission and every decision is written to an append-only, hash-chained
audit log. Decisions are never overwritten: deciding an already-decided request
is an error, not an update. Use verify_audit_chain to prove the log has not been
altered.
"""

mcp: FastMCP = FastMCP("approvals", instructions=INSTRUCTIONS)

_config: Config | None = None
_store: ApprovalStore | None = None


def configure(config: Config | None = None, store: ApprovalStore | None = None) -> None:
    """Set the process-wide configuration and store.

    Called automatically on first use. Exposed so tests and embedders can inject
    a temporary database instead of touching the user's real one.

    Args:
        config: Configuration to use. Defaults to reading the environment.
        store: Store to use. Defaults to one opened at ``config.db_path``.
    """
    global _config, _store
    _config = config or load_config()
    if store is not None:
        _store = store
    else:
        if _store is not None:
            _store.close()
        _store = ApprovalStore(
            _config.db_path, allow_self_approval=_config.allow_self_approval
        )


def get_store() -> ApprovalStore:
    """Return the active store, initialising it on first use."""
    if _store is None:
        configure()
    assert _store is not None  # noqa: S101 - configure() guarantees this
    return _store


def get_config() -> Config:
    """Return the active configuration, initialising it on first use."""
    if _config is None:
        configure()
    assert _config is not None  # noqa: S101 - configure() guarantees this
    return _config


def _error(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    """Return a structured, actionable error payload.

    Returned rather than raised for the domain errors a calling model is
    expected to handle (already decided, not found, self-approval), because the
    correct response to those is a different next action, not a retry.
    """
    payload: dict[str, Any] = {"ok": False, "error": kind, "message": message}
    payload.update(extra)
    return payload


@mcp.tool()
def submit_for_approval(
    action: str,
    payload: dict[str, Any],
    risk: str,
    requester: str,
    context: str | None = None,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    """Submit a consequential action for human approval, and stop.

    Call this INSTEAD of taking the action. Then poll get_status and proceed only
    on "approved".

    Args:
        action: Short identifier of the action, for example "send_customer_email"
            or "delete_production_index". Up to 400 characters.
        payload: The concrete parameters, stored verbatim so the decision can be
            replayed against exactly what was proposed. Must be a JSON object of
            at most 64 KB.
        risk: One of "low", "medium", "high", "critical". Drives how quickly the
            request expires and whether a written reason is mandatory.
        requester: Who is asking, normally the agent's name. By default this
            principal may not decide its own request.
        context: Optional explanation of why the action is proposed. This is what
            the human reads first, so write it for them.
        ttl_hours: Override the default time-to-live (low 168h, medium 72h,
            high 24h, critical 8h). 1 to 2160.

    Returns:
        The stored request including its request_id, status "pending" and the
        expiry deadline.

    Raises:
        ValueError: If a field is missing, oversized or invalid.
    """
    config = get_config()
    request = get_store().submit(
        action=action,
        payload=payload,
        risk=risk,
        requester=requester,
        ttl_hours=ttl_hours,
        context=context,
        default_ttl_hours=config.default_ttl_hours,
    )
    result = request.to_dict()
    result["ok"] = True
    result["next_step"] = (
        "Do not take the action. Poll get_status with this request_id and "
        "proceed only if status becomes 'approved'."
    )
    return result


@mcp.tool()
def list_pending(
    risk: str | None = None,
    requester: str | None = None,
    action_contains: str | None = None,
    status: str = "pending",
    limit: int = DEFAULT_LIST_LIMIT,
) -> dict[str, Any]:
    """List requests awaiting a human decision, oldest first.

    Oldest first rather than newest first, so that the requests waiting longest
    surface rather than being starved by newer arrivals.

    Overdue requests are transitioned to "expired" as a side effect of this call,
    and that transition is written to the audit log.

    Args:
        risk: Filter to one risk level: "low", "medium", "high" or "critical".
        requester: Filter to an exact requester, case-insensitive.
        action_contains: Case-insensitive substring filter on the action.
        status: Status to list. Defaults to "pending". Use "all" for every
            status, or one of "approved", "rejected", "expired".
        limit: Maximum requests returned, 1 to 500. Defaults to 50.

    Returns:
        The matching requests in compact form, plus counts by status for the
        whole store.

    Raises:
        ValueError: If a filter value or the limit is invalid.
    """
    store = get_store()
    status_filter: object
    if status is None or str(status).strip().lower() == "all":
        status_filter = None
    else:
        try:
            status_filter = Status(str(status).strip().lower())
        except ValueError as exc:
            valid = ", ".join(item.value for item in Status)
            raise ValueError(
                f"status must be 'all' or one of {valid} (received {status!r})"
            ) from exc

    requests = store.list_requests(
        status=status_filter,
        risk=risk,
        requester=requester,
        action_contains=action_contains,
        limit=limit,
    )
    return {
        "ok": True,
        "count": len(requests),
        "requests": summarise(requests),
        "counts_by_status": store.counts_by_status(),
        "filter": {
            "status": status,
            "risk": risk,
            "requester": requester,
            "action_contains": action_contains,
            "limit": limit,
        },
    }


@mcp.tool()
def decide(
    request_id: str,
    decision: str,
    decider: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Record a human decision on a pending request.

    This is the human's tool, not the agent's. A decision is final: calling it
    again on the same request returns an error rather than overwriting, because a
    silently overwritten decision is the exact failure this component exists to
    prevent. To change course, submit a new request.

    Args:
        request_id: The request to decide.
        decision: "approve" or "reject". Variants such as "approved", "deny" and
            "denied" are accepted.
        decider: The human accountable for this decision. By default this may not
            be the same principal that raised the request.
        reason: Free-text justification. Required for every rejection, and for
            approvals of high and critical risk requests.

    Returns:
        The updated request on success. On a domain failure (unknown id, already
        decided, self-approval blocked) returns an object with ok=false, an
        error code and an explanation, rather than raising.

    Raises:
        ValueError: If a field is invalid or a required reason is missing.
    """
    store = get_store()
    try:
        request = store.decide(
            request_id=request_id,
            decision=decision,
            decider=decider,
            reason=reason,
        )
    except RequestNotFoundError as exc:
        return _error("request_not_found", str(exc), request_id=request_id)
    except AlreadyDecidedError as exc:
        current = store.get(request_id)
        return _error(
            "already_decided",
            str(exc),
            request_id=request_id,
            current_status=current.status.value,
            current=current.to_dict(),
        )
    except SelfApprovalError as exc:
        return _error("self_approval_blocked", str(exc), request_id=request_id)

    result = request.to_dict()
    result["ok"] = True
    result["next_step"] = (
        "The agent may now proceed with the action."
        if request.status is Status.APPROVED
        else "The agent must not take the action."
    )
    return result


@mcp.tool()
def get_status(request_id: str) -> dict[str, Any]:
    """Check whether a request has been decided.

    Proceed with the action ONLY if status is "approved". Anything else -
    "pending", "rejected", "expired" - means do not act.

    Args:
        request_id: The request to check.

    Returns:
        The full request including status, decision, decider, reason and whether
        the state is terminal. Returns ok=false with an error code if the id is
        unknown.
    """
    try:
        request = get_store().get(request_id)
    except RequestNotFoundError as exc:
        return _error("request_not_found", str(exc), request_id=request_id)

    result = request.to_dict()
    result["ok"] = True
    result["may_proceed"] = request.status is Status.APPROVED
    if request.status is Status.PENDING:
        result["guidance"] = (
            "Still awaiting a human decision. Do not take the action. "
            f"This request expires at {result['expires_at']}."
        )
    elif request.status is Status.APPROVED:
        result["guidance"] = "Approved. The agent may proceed with exactly this payload."
    elif request.status is Status.REJECTED:
        result["guidance"] = "Rejected. Do not take the action. Do not resubmit unchanged."
    else:
        result["guidance"] = (
            "Expired without a decision. The gate fails closed: this is not an "
            "approval. Submit a new request if the action is still needed."
        )
    return result


@mcp.tool()
def audit_trail(
    request_id: str | None = None,
    limit: int = MAX_LIST_LIMIT,
) -> dict[str, Any]:
    """Return the append-only audit log, in order, with its hash chain.

    Args:
        request_id: Restrict to one request. Omit, or pass "all", for the whole
            log.
        limit: Maximum entries returned, 1 to 500.

    Returns:
        Ordered entries, each with its sequence number, actor, detail, timestamp,
        prev_hash and entry_hash, plus the current chain head. Returns ok=false
        if request_id is unknown.

    Raises:
        ValueError: If the limit is invalid.
    """
    store = get_store()
    scope = request_id
    if scope is not None and str(scope).strip().lower() in {"all", ""}:
        scope = None

    try:
        entries = store.audit_entries(scope, limit=limit)
    except RequestNotFoundError as exc:
        return _error("request_not_found", str(exc), request_id=request_id)

    return {
        "ok": True,
        "scope": scope or "all",
        "count": len(entries),
        "entries": [entry.to_dict() for entry in entries],
        "chain_head": entries[-1].entry_hash if entries else GENESIS_HASH,
        "note": (
            "Entries are append-only and hash-chained. Call verify_audit_chain "
            "to confirm nothing has been altered."
        ),
    }


@mcp.tool()
def verify_audit_chain() -> dict[str, Any]:
    """Verify that the audit log has not been altered.

    Recomputes every entry hash and checks that each links to its predecessor. If
    any historical entry was edited, deleted or reordered, this reports the
    sequence number where the chain breaks.

    Returns:
        Whether the chain is valid, how many entries were checked, the chain head
        to anchor externally, and on failure the sequence number and nature of
        the break.
    """
    verification = get_store().verify()
    payload = verification.to_dict()
    payload["ok"] = True
    payload["scope_note"] = (
        "This detects after-the-fact edits. It cannot detect a wholesale rewrite "
        "of the chain by someone with write access to the database file. Anchor "
        "head_hash somewhere they do not control to close that gap."
    )
    return payload


@mcp.tool()
def gate_policy() -> dict[str, Any]:
    """Describe the gate's configured policy.

    Useful to a calling model deciding what risk level to claim, and to a human
    checking that separation of duties is switched on.

    Returns:
        Default time-to-live per risk level, which risk levels require a written
        reason, whether self-approval is permitted, and where the database lives.
    """
    config = get_config()
    return {
        "ok": True,
        "server_version": __version__,
        "database_path": str(config.db_path),
        "default_ttl_hours_by_risk": {
            level.value: DEFAULT_TTL_HOURS[level] for level in RiskLevel
        },
        "ttl_override_hours": config.default_ttl_hours,
        "reason_required_for_risks": sorted(
            level.value for level in REASON_REQUIRED_RISKS
        ),
        "reason_always_required_for": "rejection",
        "self_approval_allowed": config.allow_self_approval,
        "terminal_statuses": [
            status.value for status in Status if status.is_terminal
        ],
        "decisions_are_immutable": True,
        "expiry_behaviour": "fail closed - an expired request is not an approval",
    }


def registered_tool_names() -> list[str]:
    """Return the names of the tools registered on this server."""
    import asyncio

    async def _collect() -> list[str]:
        return [tool.name for tool in await mcp.list_tools()]

    return asyncio.run(_collect())


def main() -> None:
    """Run the server over stdio. Entry point for the console script."""
    configure()
    mcp.run()


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
