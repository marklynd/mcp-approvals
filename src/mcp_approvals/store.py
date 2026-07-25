"""SQLite-backed approval store with an append-only, hash-chained audit log.

Durability and integrity choices worth knowing about:

* **The audit log is append-only in the database, not just in the application.**
  SQLite triggers raise on any ``UPDATE`` or ``DELETE`` against ``audit_log``, so
  a bug in this module, or someone poking at the file with the ``sqlite3`` CLI,
  hits the same wall. Dropping the triggers is possible with file write access,
  which is exactly why the hash chain exists as a second layer.
* **Sequence numbers are assigned explicitly inside the write transaction**,
  not by ``AUTOINCREMENT``, because the sequence number is part of the hashed
  material and must be known before the hash is computed.
* **Writes use ``BEGIN IMMEDIATE``** and are serialised by a process-level lock.
  Two concurrent appends must not read the same chain head.
* **Expiry is evaluated lazily on read.** There is no background thread. A
  pending request past its deadline is transitioned to ``expired`` and an audit
  entry is written the next time anything looks at it, so the log reflects the
  transition rather than a silent state change.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from .hashchain import GENESIS_HASH, ChainVerification, compute_entry_hash, verify_chain
from .models import (
    DEFAULT_TTL_HOURS,
    REASON_REQUIRED_RISKS,
    AlreadyDecidedError,
    ApprovalRequest,
    AuditEntry,
    Decision,
    EventType,
    RequestNotFoundError,
    RiskLevel,
    SelfApprovalError,
    Status,
    from_iso,
    to_iso,
    utc_now,
)

SCHEMA_VERSION: Final[int] = 1

MAX_ACTION_CHARS: Final[int] = 400
MAX_PRINCIPAL_CHARS: Final[int] = 200
MAX_REASON_CHARS: Final[int] = 2000
MAX_CONTEXT_CHARS: Final[int] = 4000
MAX_PAYLOAD_BYTES: Final[int] = 64 * 1024
MAX_LIST_LIMIT: Final[int] = 500
DEFAULT_LIST_LIMIT: Final[int] = 50

# Plain string, not an f-string: the schema version is inserted with a bound
# parameter in __init__ so no value is ever interpolated into SQL text.
_SCHEMA: Final[str] = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_requests (
    request_id  TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    risk        TEXT NOT NULL
                CHECK (risk IN ('low', 'medium', 'high', 'critical')),
    requester   TEXT NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    context     TEXT,
    decided_at  TEXT,
    decider     TEXT,
    decision    TEXT CHECK (decision IS NULL OR decision IN ('approve', 'reject')),
    reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_status  ON approval_requests (status);
CREATE INDEX IF NOT EXISTS idx_requests_expires ON approval_requests (expires_at);
CREATE INDEX IF NOT EXISTS idx_requests_created ON approval_requests (created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    seq         INTEGER PRIMARY KEY,
    request_id  TEXT NOT NULL REFERENCES approval_requests (request_id),
    event_type  TEXT NOT NULL
                CHECK (event_type IN ('submitted', 'decided', 'expired')),
    actor       TEXT NOT NULL,
    detail      TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log (request_id);

-- The audit log is append-only at the storage layer, not merely by convention.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: entries cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: entries cannot be deleted');
END;
"""


def _new_request_id() -> str:
    """Return a new opaque request identifier."""
    return f"ar_{uuid.uuid4().hex[:16]}"


def _require_text(
    value: object, field: str, max_chars: int, *, allow_empty: bool = False
) -> str:
    """Validate and normalise a required text field."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string (received {type(value).__name__})")
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > max_chars:
        raise ValueError(
            f"{field} must be at most {max_chars} characters (received {len(cleaned)})"
        )
    return cleaned


class ApprovalStore:
    """Persistent store for approval requests and their audit trail.

    The store owns one SQLite connection and serialises writes with a lock, so a
    single instance is safe to share across threads. Separate processes are also
    safe: WAL mode plus ``BEGIN IMMEDIATE`` means a concurrent writer waits
    rather than corrupting the chain.
    """

    def __init__(self, db_path: str | Path, *, allow_self_approval: bool = False) -> None:
        """Open (and if necessary create) the store at ``db_path``.

        Args:
            db_path: SQLite file path. ``":memory:"`` is supported for tests.
            allow_self_approval: Whether a requester may decide their own
                request.
        """
        self.db_path = Path(db_path) if str(db_path) != ":memory:" else Path(":memory:")
        self.allow_self_approval = allow_self_approval
        self._lock = threading.RLock()

        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            str(db_path), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(_SCHEMA)
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()

    def __enter__(self) -> ApprovalStore:
        """Enter a context manager block."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the store on leaving a context manager block."""
        self.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def submit(
        self,
        *,
        action: str,
        payload: dict[str, Any],
        risk: object,
        requester: str,
        ttl_hours: int | None = None,
        context: str | None = None,
        default_ttl_hours: int | None = None,
        now: datetime | None = None,
    ) -> ApprovalRequest:
        """Record a new request for human approval.

        Args:
            action: Short identifier of what the agent wants to do, for example
                ``"send_email"`` or ``"delete_production_index"``.
            payload: The concrete parameters of the action. Stored verbatim so
                the decision can be replayed against exactly what was proposed.
            risk: One of ``low``, ``medium``, ``high``, ``critical``.
            requester: Who or what is asking, for example an agent name.
            ttl_hours: Explicit time-to-live. Defaults to the per-risk value.
            context: Optional free-text explaining why the action is proposed.
            default_ttl_hours: Configured override of the per-risk defaults.
            now: Injected clock, for tests.

        Returns:
            The stored :class:`ApprovalRequest`.

        Raises:
            ValueError: If any field is missing, oversized or invalid.
        """
        action_text = _require_text(action, "action", MAX_ACTION_CHARS)
        requester_text = _require_text(requester, "requester", MAX_PRINCIPAL_CHARS)
        risk_level = RiskLevel.parse(risk)

        if not isinstance(payload, dict):
            raise ValueError(
                f"payload must be an object (received {type(payload).__name__})"
            )
        try:
            payload_json = json.dumps(payload, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"payload must be JSON-serialisable: {exc}") from exc
        if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"payload must serialise to at most {MAX_PAYLOAD_BYTES} bytes"
            )

        context_text: str | None = None
        if context is not None:
            context_text = _require_text(
                context, "context", MAX_CONTEXT_CHARS, allow_empty=True
            ) or None

        effective_ttl = self._resolve_ttl(risk_level, ttl_hours, default_ttl_hours)
        created = now or utc_now()
        expires = created + timedelta(hours=effective_ttl)
        request_id = _new_request_id()

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """
                    INSERT INTO approval_requests
                        (request_id, action, payload, risk, requester, status,
                         created_at, expires_at, context)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        action_text,
                        payload_json,
                        risk_level.value,
                        requester_text,
                        Status.PENDING.value,
                        to_iso(created),
                        to_iso(expires),
                        context_text,
                    ),
                )
                self._append_audit(
                    request_id=request_id,
                    event_type=EventType.SUBMITTED,
                    actor=requester_text,
                    detail={
                        "action": action_text,
                        "risk": risk_level.value,
                        "payload": payload,
                        "context": context_text,
                        "expires_at": to_iso(expires),
                        "ttl_hours": effective_ttl,
                    },
                    recorded_at=created,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

        return self.get(request_id)

    def decide(
        self,
        *,
        request_id: str,
        decision: object,
        decider: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalRequest:
        """Record a human decision on a pending request.

        Args:
            request_id: The request to decide.
            decision: ``approve`` or ``reject``.
            decider: The human accountable for the decision.
            reason: Free-text justification. Mandatory for rejections and for
                high and critical risk approvals.
            now: Injected clock, for tests.

        Returns:
            The updated :class:`ApprovalRequest`.

        Raises:
            RequestNotFoundError: If no such request exists.
            AlreadyDecidedError: If the request already reached a terminal state.
                Decisions are never overwritten.
            SelfApprovalError: If the decider is the requester and self-approval
                is disabled.
            ValueError: If a field is invalid or a required reason is missing.
        """
        request_ref = _require_text(request_id, "request_id", 100)
        decider_text = _require_text(decider, "decider", MAX_PRINCIPAL_CHARS)
        verdict = Decision.parse(decision)

        reason_text: str | None = None
        if reason is not None:
            reason_text = _require_text(
                reason, "reason", MAX_REASON_CHARS, allow_empty=True
            ) or None

        moment = now or utc_now()

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM approval_requests WHERE request_id = ?",
                    (request_ref,),
                ).fetchone()
                if row is None:
                    raise RequestNotFoundError(f"No approval request {request_ref!r}")

                current = self._row_to_request(row)

                if current.status is Status.PENDING and current.expires_at <= moment:
                    self._transition_to_expired(current, moment)
                    raise AlreadyDecidedError(
                        f"Request {request_ref!r} expired at "
                        f"{to_iso(current.expires_at)} and can no longer be decided. "
                        "Submit a new request."
                    )

                if current.status.is_terminal:
                    raise AlreadyDecidedError(
                        f"Request {request_ref!r} is already {current.status.value} "
                        f"(decided by {current.decider or 'n/a'} at "
                        f"{to_iso(current.decided_at) if current.decided_at else 'n/a'}). "
                        "Decisions are never overwritten; submit a new request instead."
                    )

                if (
                    not self.allow_self_approval
                    and decider_text.lower() == current.requester.lower()
                ):
                    raise SelfApprovalError(
                        f"{decider_text!r} raised this request and cannot decide it. "
                        "Set MCP_APPROVALS_ALLOW_SELF_APPROVAL=true only if you "
                        "have a deliberate reason to remove separation of duties."
                    )

                if reason_text is None and (
                    verdict is Decision.REJECT or current.risk in REASON_REQUIRED_RISKS
                ):
                    raise ValueError(
                        "reason is required when rejecting, and when deciding a "
                        f"{current.risk.value} risk request"
                    )

                self._connection.execute(
                    """
                    UPDATE approval_requests
                       SET status = ?, decision = ?, decider = ?, reason = ?,
                           decided_at = ?
                     WHERE request_id = ? AND status = 'pending'
                    """,
                    (
                        verdict.resulting_status.value,
                        verdict.value,
                        decider_text,
                        reason_text,
                        to_iso(moment),
                        request_ref,
                    ),
                )
                self._append_audit(
                    request_id=request_ref,
                    event_type=EventType.DECIDED,
                    actor=decider_text,
                    detail={
                        "decision": verdict.value,
                        "resulting_status": verdict.resulting_status.value,
                        "reason": reason_text,
                        "action": current.action,
                        "risk": current.risk.value,
                        "requester": current.requester,
                    },
                    recorded_at=moment,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

        return self.get(request_ref)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, request_id: str, *, now: datetime | None = None) -> ApprovalRequest:
        """Return one request, applying expiry if it is overdue.

        Raises:
            RequestNotFoundError: If no such request exists.
            ValueError: If ``request_id`` is not a non-empty string.
        """
        request_ref = _require_text(request_id, "request_id", 100)
        moment = now or utc_now()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM approval_requests WHERE request_id = ?", (request_ref,)
            ).fetchone()
            if row is None:
                raise RequestNotFoundError(f"No approval request {request_ref!r}")
            request = self._row_to_request(row)
            if request.status is Status.PENDING and request.expires_at <= moment:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    self._transition_to_expired(request, moment)
                    self._connection.execute("COMMIT")
                except Exception:
                    self._connection.execute("ROLLBACK")
                    raise
                row = self._connection.execute(
                    "SELECT * FROM approval_requests WHERE request_id = ?",
                    (request_ref,),
                ).fetchone()
                request = self._row_to_request(row)
        return request

    def list_requests(
        self,
        *,
        status: object = Status.PENDING,
        risk: object = None,
        requester: str | None = None,
        action_contains: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        now: datetime | None = None,
    ) -> list[ApprovalRequest]:
        """Return requests matching the filter, oldest first.

        Oldest first is deliberate: a queue read newest-first quietly starves the
        requests that have been waiting longest, which is the opposite of what an
        approval queue should do.

        Args:
            status: Status to filter on, or ``None`` for all statuses.
            risk: Risk level to filter on, or ``None`` for all.
            requester: Exact requester match, case-insensitive.
            action_contains: Substring match on the action, case-insensitive.
            limit: Maximum rows returned, 1 to 500.
            now: Injected clock, for tests.

        Raises:
            ValueError: If a filter value or the limit is invalid.
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")

        moment = now or utc_now()
        self._expire_due(moment)

        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            status_value = (
                status if isinstance(status, Status) else Status(str(status).lower())
            )
            clauses.append("status = ?")
            params.append(status_value.value)
        if risk is not None:
            clauses.append("risk = ?")
            params.append(RiskLevel.parse(risk).value)
        if requester is not None:
            clauses.append("LOWER(requester) = ?")
            params.append(_require_text(requester, "requester", MAX_PRINCIPAL_CHARS).lower())
        if action_contains is not None:
            clauses.append("LOWER(action) LIKE ?")
            needle = _require_text(action_contains, "action_contains", MAX_ACTION_CHARS)
            params.append(f"%{needle.lower()}%")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        with self._lock:
            # S608: `where` is assembled only from the fixed clause strings a
            # few lines above. Every caller-supplied value is a bound parameter
            # in `params` and never reaches the SQL text.
            rows = self._connection.execute(
                f"SELECT * FROM approval_requests {where} "  # noqa: S608
                "ORDER BY created_at ASC, request_id ASC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_request(row) for row in rows]

    def audit_entries(
        self, request_id: str | None = None, *, limit: int = MAX_LIST_LIMIT
    ) -> list[AuditEntry]:
        """Return audit entries in sequence order.

        Args:
            request_id: Restrict to one request, or ``None`` for the whole log.
            limit: Maximum entries returned, 1 to 500.

        Raises:
            RequestNotFoundError: If ``request_id`` does not exist.
            ValueError: If the limit is invalid.
        """
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")

        with self._lock:
            if request_id is not None:
                request_ref = _require_text(request_id, "request_id", 100)
                exists = self._connection.execute(
                    "SELECT 1 FROM approval_requests WHERE request_id = ?",
                    (request_ref,),
                ).fetchone()
                if exists is None:
                    raise RequestNotFoundError(f"No approval request {request_ref!r}")
                rows = self._connection.execute(
                    "SELECT * FROM audit_log WHERE request_id = ? "
                    "ORDER BY seq ASC LIMIT ?",
                    (request_ref, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM audit_log ORDER BY seq ASC LIMIT ?", (limit,)
                ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def verify(self) -> ChainVerification:
        """Verify the whole audit chain from genesis to head."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM audit_log ORDER BY seq ASC"
            ).fetchall()
        return verify_chain([self._row_to_entry(row) for row in rows])

    def counts_by_status(self, *, now: datetime | None = None) -> dict[str, int]:
        """Return the number of requests in each status."""
        self._expire_due(now or utc_now())
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS n FROM approval_requests GROUP BY status"
            ).fetchall()
        counts = {status.value: 0 for status in Status}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_ttl(
        risk: RiskLevel, ttl_hours: int | None, default_ttl_hours: int | None
    ) -> int:
        """Return the effective time-to-live in hours."""
        if ttl_hours is not None:
            if not isinstance(ttl_hours, int) or isinstance(ttl_hours, bool):
                raise ValueError("ttl_hours must be an integer number of hours")
            if not 1 <= ttl_hours <= 24 * 90:
                raise ValueError("ttl_hours must be between 1 and 2160 (90 days)")
            return ttl_hours
        if default_ttl_hours is not None:
            return default_ttl_hours
        return DEFAULT_TTL_HOURS[risk]

    def _expire_due(self, moment: datetime) -> None:
        """Transition every overdue pending request to expired."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM approval_requests "
                "WHERE status = 'pending' AND expires_at <= ? "
                "ORDER BY created_at ASC",
                (to_iso(moment),),
            ).fetchall()
            if not rows:
                return
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    self._transition_to_expired(self._row_to_request(row), moment)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _transition_to_expired(
        self, request: ApprovalRequest, moment: datetime
    ) -> None:
        """Mark ``request`` expired and log it. Caller holds the transaction."""
        self._connection.execute(
            "UPDATE approval_requests SET status = ? WHERE request_id = ? "
            "AND status = 'pending'",
            (Status.EXPIRED.value, request.request_id),
        )
        self._append_audit(
            request_id=request.request_id,
            event_type=EventType.EXPIRED,
            actor="system",
            detail={
                "expired_at": to_iso(request.expires_at),
                "observed_at": to_iso(moment),
                "action": request.action,
                "risk": request.risk.value,
                "note": (
                    "No decision was recorded before the deadline. The gate fails "
                    "closed: an expired request is not an approval."
                ),
            },
            recorded_at=moment,
        )

    def _append_audit(
        self,
        *,
        request_id: str,
        event_type: EventType,
        actor: str,
        detail: dict[str, Any],
        recorded_at: datetime,
    ) -> None:
        """Append one hash-chained entry. Caller holds the write transaction."""
        head = self._connection.execute(
            "SELECT seq, entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = (head["seq"] + 1) if head else 1
        prev_hash = head["entry_hash"] if head else GENESIS_HASH
        recorded_iso = to_iso(recorded_at)

        entry_hash = compute_entry_hash(
            seq=seq,
            request_id=request_id,
            event_type=event_type.value,
            actor=actor,
            detail=detail,
            recorded_at=recorded_iso,
            prev_hash=prev_hash,
        )
        self._connection.execute(
            """
            INSERT INTO audit_log
                (seq, request_id, event_type, actor, detail, recorded_at,
                 prev_hash, entry_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                request_id,
                event_type.value,
                actor,
                json.dumps(detail, sort_keys=True, default=str),
                recorded_iso,
                prev_hash,
                entry_hash,
            ),
        )

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> ApprovalRequest:
        """Map a database row to an :class:`ApprovalRequest`."""
        return ApprovalRequest(
            request_id=row["request_id"],
            action=row["action"],
            payload=json.loads(row["payload"]),
            risk=RiskLevel(row["risk"]),
            requester=row["requester"],
            status=Status(row["status"]),
            created_at=from_iso(row["created_at"]),
            expires_at=from_iso(row["expires_at"]),
            context=row["context"],
            decided_at=from_iso(row["decided_at"]) if row["decided_at"] else None,
            decider=row["decider"],
            decision=Decision(row["decision"]) if row["decision"] else None,
            reason=row["reason"],
        )

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
        """Map a database row to an :class:`AuditEntry`."""
        return AuditEntry(
            seq=row["seq"],
            request_id=row["request_id"],
            event_type=EventType(row["event_type"]),
            actor=row["actor"],
            detail=json.loads(row["detail"]),
            recorded_at=from_iso(row["recorded_at"]),
            prev_hash=row["prev_hash"],
            entry_hash=row["entry_hash"],
        )


def summarise(requests: Iterable[ApprovalRequest]) -> list[dict[str, Any]]:
    """Return a compact list view of ``requests``, for queue displays."""
    return [
        {
            "request_id": request.request_id,
            "action": request.action,
            "risk": request.risk.value,
            "requester": request.requester,
            "status": request.status.value,
            "created_at": to_iso(request.created_at),
            "expires_at": to_iso(request.expires_at),
            "context": request.context,
        }
        for request in requests
    ]


def oldest_first(requests: Sequence[ApprovalRequest]) -> list[ApprovalRequest]:
    """Return ``requests`` sorted oldest first, ties broken by id."""
    return sorted(requests, key=lambda request: (request.created_at, request.request_id))
