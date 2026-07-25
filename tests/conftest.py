"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp_approvals.store import ApprovalStore


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """A temporary database path that does not exist yet."""
    return tmp_path / "nested" / "approvals.db"


@pytest.fixture()
def store(db_path: Path) -> Iterator[ApprovalStore]:
    """A fresh store on disk, closed on teardown.

    On disk rather than in memory on purpose: the append-only triggers, WAL mode
    and directory creation are all things worth exercising for real.
    """
    instance = ApprovalStore(db_path)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def now() -> datetime:
    """A fixed clock, so expiry behaviour is testable without sleeping."""
    return datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


def submit_sample(
    store: ApprovalStore,
    *,
    action: str = "send_customer_email",
    risk: str = "high",
    requester: str = "outreach-agent",
    **kwargs: object,
):
    """Submit a representative request."""
    return store.submit(
        action=action,
        payload={"to": "cfo@example.com", "subject": "Q3 proposal"},
        risk=risk,
        requester=requester,
        **kwargs,  # type: ignore[arg-type]
    )
