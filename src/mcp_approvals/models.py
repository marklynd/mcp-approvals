"""Domain types for the approval gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Final


class RiskLevel(str, Enum):
    """How consequential the requested action is.

    Risk drives the default time-to-live and whether a written reason is
    mandatory, so it is a control input rather than a label.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def parse(cls, value: object) -> RiskLevel:
        """Return the matching risk level.

        Raises:
            ValueError: If ``value`` is not one of the four levels.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        raise ValueError(
            f"risk must be one of {', '.join(level.value for level in cls)} "
            f"(received {value!r})"
        )


class Status(str, Enum):
    """Lifecycle state of a request.

    ``PENDING`` is the only non-terminal state. Once a request leaves it, it
    never returns; a superseded decision is recorded as a new request, not as an
    edit of the old one.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        """Whether no further decision is possible."""
        return self is not Status.PENDING


class Decision(str, Enum):
    """A human's verdict on a pending request."""

    APPROVE = "approve"
    REJECT = "reject"

    @classmethod
    def parse(cls, value: object) -> Decision:
        """Return the matching decision, accepting common variants.

        ``"approve"``, ``"approved"``, ``"reject"``, ``"rejected"``, ``"deny"``
        and ``"denied"`` are all understood.

        Raises:
            ValueError: If ``value`` is not a recognised decision.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalised = value.strip().lower()
            if normalised in {"approve", "approved", "approval", "yes"}:
                return cls.APPROVE
            if normalised in {"reject", "rejected", "deny", "denied", "no"}:
                return cls.REJECT
        raise ValueError(
            f"decision must be 'approve' or 'reject' (received {value!r})"
        )

    @property
    def resulting_status(self) -> Status:
        """The status a request lands in once this decision is recorded."""
        return Status.APPROVED if self is Decision.APPROVE else Status.REJECTED


class EventType(str, Enum):
    """The kinds of entry that appear in the append-only audit log."""

    SUBMITTED = "submitted"
    DECIDED = "decided"
    EXPIRED = "expired"


#: Default time-to-live per risk level. Higher risk expires sooner: a critical
#: request that nobody has looked at in eight hours should fail closed rather
#: than sit in a queue waiting to be rubber-stamped.
DEFAULT_TTL_HOURS: Final[dict[RiskLevel, int]] = {
    RiskLevel.LOW: 168,
    RiskLevel.MEDIUM: 72,
    RiskLevel.HIGH: 24,
    RiskLevel.CRITICAL: 8,
}

#: Risk levels where a decision must carry a written reason.
REASON_REQUIRED_RISKS: Final[frozenset[RiskLevel]] = frozenset(
    {RiskLevel.HIGH, RiskLevel.CRITICAL}
)


def utc_now() -> datetime:
    """Return the current UTC time, truncated to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(moment: datetime) -> str:
    """Return ``moment`` as an ISO 8601 string in UTC with a ``Z`` suffix."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    """Parse an ISO 8601 timestamp produced by :func:`to_iso`.

    Raises:
        ValueError: If ``text`` is not a parseable timestamp.
    """
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ApprovalRequest:
    """A request for a human decision before an agent proceeds."""

    request_id: str
    action: str
    payload: dict[str, Any]
    risk: RiskLevel
    requester: str
    status: Status
    created_at: datetime
    expires_at: datetime
    context: str | None = None
    decided_at: datetime | None = None
    decider: str | None = None
    decision: Decision | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "request_id": self.request_id,
            "action": self.action,
            "payload": self.payload,
            "risk": self.risk.value,
            "requester": self.requester,
            "status": self.status.value,
            "created_at": to_iso(self.created_at),
            "expires_at": to_iso(self.expires_at),
            "context": self.context,
            "decided_at": to_iso(self.decided_at) if self.decided_at else None,
            "decider": self.decider,
            "decision": self.decision.value if self.decision else None,
            "reason": self.reason,
            "is_terminal": self.status.is_terminal,
        }


@dataclass(frozen=True)
class AuditEntry:
    """One immutable entry in the hash-chained audit log."""

    seq: int
    request_id: str
    event_type: EventType
    actor: str
    detail: dict[str, Any]
    recorded_at: datetime
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "seq": self.seq,
            "request_id": self.request_id,
            "event_type": self.event_type.value,
            "actor": self.actor,
            "detail": self.detail,
            "recorded_at": to_iso(self.recorded_at),
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


class ApprovalError(Exception):
    """Base class for approval-gate failures."""


class RequestNotFoundError(ApprovalError):
    """The requested id does not exist in this store."""


class AlreadyDecidedError(ApprovalError):
    """The request has already reached a terminal state.

    Raised instead of overwriting, because a silent overwrite is exactly the
    failure this component exists to prevent.
    """


class SelfApprovalError(ApprovalError):
    """The decider is the same principal that raised the request."""
