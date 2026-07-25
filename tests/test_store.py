"""Tests for the SQLite approval store."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mcp_approvals.models import (
    DEFAULT_TTL_HOURS,
    AlreadyDecidedError,
    Decision,
    EventType,
    RequestNotFoundError,
    RiskLevel,
    SelfApprovalError,
    Status,
)
from mcp_approvals.store import ApprovalStore, oldest_first, summarise

from .conftest import submit_sample


class TestSubmitValidation:
    def test_rejects_empty_action(self, store: ApprovalStore) -> None:
        with pytest.raises(ValueError, match="action must not be empty"):
            store.submit(action="  ", payload={}, risk="low", requester="agent")

    def test_rejects_empty_requester(self, store: ApprovalStore) -> None:
        with pytest.raises(ValueError, match="requester must not be empty"):
            store.submit(action="do", payload={}, risk="low", requester="")

    def test_rejects_unknown_risk(self, store: ApprovalStore) -> None:
        with pytest.raises(ValueError, match="risk must be one of"):
            store.submit(action="do", payload={}, risk="spicy", requester="agent")

    def test_rejects_non_object_payload(self, store: ApprovalStore) -> None:
        with pytest.raises(ValueError, match="payload must be an object"):
            store.submit(action="do", payload=["a"], risk="low", requester="agent")  # type: ignore[arg-type]

    def test_rejects_oversized_payload(self, store: ApprovalStore) -> None:
        with pytest.raises(ValueError, match="at most 65536 bytes"):
            store.submit(
                action="do",
                payload={"blob": "x" * 70_000},
                risk="low",
                requester="agent",
            )

    def test_rejects_oversized_action(self, store: ApprovalStore) -> None:
        with pytest.raises(ValueError, match="at most 400 characters"):
            store.submit(
                action="x" * 401, payload={}, risk="low", requester="agent"
            )

    @pytest.mark.parametrize("ttl", [0, -1, 3000])
    def test_rejects_out_of_range_ttl(self, store: ApprovalStore, ttl: int) -> None:
        with pytest.raises(ValueError, match="ttl_hours must be between"):
            store.submit(
                action="do", payload={}, risk="low", requester="agent", ttl_hours=ttl
            )


class TestSubmit:
    def test_creates_a_pending_request(self, store: ApprovalStore) -> None:
        request = submit_sample(store)
        assert request.status is Status.PENDING
        assert request.request_id.startswith("ar_")
        assert request.decision is None
        assert request.payload["to"] == "cfo@example.com"

    def test_ttl_defaults_by_risk(self, store: ApprovalStore, now: datetime) -> None:
        for risk in RiskLevel:
            request = store.submit(
                action="do", payload={}, risk=risk, requester="agent", now=now
            )
            expected = now + timedelta(hours=DEFAULT_TTL_HOURS[risk])
            assert request.expires_at == expected

    def test_explicit_ttl_wins(self, store: ApprovalStore, now: datetime) -> None:
        request = store.submit(
            action="do",
            payload={},
            risk="low",
            requester="agent",
            ttl_hours=5,
            now=now,
        )
        assert request.expires_at == now + timedelta(hours=5)

    def test_configured_default_ttl_applies(self, store: ApprovalStore, now: datetime) -> None:
        request = store.submit(
            action="do",
            payload={},
            risk="low",
            requester="agent",
            default_ttl_hours=2,
            now=now,
        )
        assert request.expires_at == now + timedelta(hours=2)

    def test_ids_are_unique(self, store: ApprovalStore) -> None:
        ids = {submit_sample(store).request_id for _ in range(25)}
        assert len(ids) == 25

    def test_writes_a_submitted_audit_entry(self, store: ApprovalStore) -> None:
        request = submit_sample(store)
        entries = store.audit_entries(request.request_id)
        assert len(entries) == 1
        assert entries[0].event_type is EventType.SUBMITTED
        assert entries[0].actor == "outreach-agent"
        assert entries[0].detail["payload"]["to"] == "cfo@example.com"

    def test_payload_is_stored_verbatim(self, store: ApprovalStore) -> None:
        payload = {"nested": {"b": 2, "a": [1, 2, 3]}, "flag": True}
        request = store.submit(
            action="do", payload=payload, risk="low", requester="agent"
        )
        assert store.get(request.request_id).payload == payload


class TestDecide:
    def test_approve_moves_to_approved(self, store: ApprovalStore) -> None:
        request = submit_sample(store)
        decided = store.decide(
            request_id=request.request_id,
            decision="approve",
            decider="mark",
            reason="Reviewed the draft.",
        )
        assert decided.status is Status.APPROVED
        assert decided.decision is Decision.APPROVE
        assert decided.decider == "mark"
        assert decided.decided_at is not None

    def test_reject_moves_to_rejected(self, store: ApprovalStore) -> None:
        request = submit_sample(store)
        decided = store.decide(
            request_id=request.request_id,
            decision="reject",
            decider="mark",
            reason="Wrong recipient.",
        )
        assert decided.status is Status.REJECTED

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("approve", Decision.APPROVE),
            ("approved", Decision.APPROVE),
            ("yes", Decision.APPROVE),
            ("reject", Decision.REJECT),
            ("denied", Decision.REJECT),
            ("NO", Decision.REJECT),
        ],
    )
    def test_decision_variants(
        self, store: ApprovalStore, value: str, expected: Decision
    ) -> None:
        request = submit_sample(store)
        decided = store.decide(
            request_id=request.request_id,
            decision=value,
            decider="mark",
            reason="Because.",
        )
        assert decided.decision is expected

    def test_unknown_decision_is_rejected(self, store: ApprovalStore) -> None:
        request = submit_sample(store)
        with pytest.raises(ValueError, match="must be 'approve' or 'reject'"):
            store.decide(
                request_id=request.request_id, decision="maybe", decider="mark"
            )

    def test_unknown_request_raises(self, store: ApprovalStore) -> None:
        with pytest.raises(RequestNotFoundError):
            store.decide(request_id="ar_nope", decision="approve", decider="mark")

    def test_writes_a_decided_audit_entry(self, store: ApprovalStore) -> None:
        request = submit_sample(store)
        store.decide(
            request_id=request.request_id,
            decision="approve",
            decider="mark",
            reason="Fine.",
        )
        entries = store.audit_entries(request.request_id)
        assert [entry.event_type for entry in entries] == [
            EventType.SUBMITTED,
            EventType.DECIDED,
        ]
        assert entries[1].actor == "mark"
        assert entries[1].detail["decision"] == "approve"


class TestDecisionsAreImmutable:
    def test_second_decision_is_refused(self, store: ApprovalStore) -> None:
        request = submit_sample(store)
        store.decide(
            request_id=request.request_id,
            decision="approve",
            decider="mark",
            reason="Fine.",
        )
        with pytest.raises(AlreadyDecidedError, match="already approved"):
            store.decide(
                request_id=request.request_id,
                decision="reject",
                decider="someone-else",
                reason="Changed my mind.",
            )

    def test_the_original_decision_survives_the_attempt(
        self, store: ApprovalStore
    ) -> None:
        request = submit_sample(store)
        store.decide(
            request_id=request.request_id,
            decision="approve",
            decider="mark",
            reason="Fine.",
        )
        with pytest.raises(AlreadyDecidedError):
            store.decide(
                request_id=request.request_id,
                decision="reject",
                decider="attacker",
                reason="No.",
            )
        current = store.get(request.request_id)
        assert current.status is Status.APPROVED
        assert current.decider == "mark"
        assert len(store.audit_entries(request.request_id)) == 2

    def test_error_message_names_the_original_decider(
        self, store: ApprovalStore
    ) -> None:
        request = submit_sample(store)
        store.decide(
            request_id=request.request_id,
            decision="approve",
            decider="mark",
            reason="Fine.",
        )
        with pytest.raises(AlreadyDecidedError) as excinfo:
            store.decide(
                request_id=request.request_id, decision="approve", decider="other"
            )
        assert "mark" in str(excinfo.value)
        assert "never overwritten" in str(excinfo.value)


class TestSeparationOfDuties:
    def test_self_approval_is_blocked_by_default(self, store: ApprovalStore) -> None:
        request = submit_sample(store, requester="outreach-agent")
        with pytest.raises(SelfApprovalError, match="cannot decide it"):
            store.decide(
                request_id=request.request_id,
                decision="approve",
                decider="outreach-agent",
                reason="Looks fine to me.",
            )

    def test_self_approval_check_is_case_insensitive(
        self, store: ApprovalStore
    ) -> None:
        request = submit_sample(store, requester="Outreach-Agent")
        with pytest.raises(SelfApprovalError):
            store.decide(
                request_id=request.request_id,
                decision="approve",
                decider="outreach-agent",
                reason="Fine.",
            )

    def test_self_approval_can_be_enabled_deliberately(self, db_path: Path) -> None:
        with ApprovalStore(db_path, allow_self_approval=True) as store:
            request = submit_sample(store, requester="agent")
            decided = store.decide(
                request_id=request.request_id,
                decision="approve",
                decider="agent",
                reason="Single-operator setup.",
            )
            assert decided.status is Status.APPROVED

    def test_blocked_self_approval_leaves_the_request_pending(
        self, store: ApprovalStore
    ) -> None:
        request = submit_sample(store)
        with pytest.raises(SelfApprovalError):
            store.decide(
                request_id=request.request_id,
                decision="approve",
                decider="outreach-agent",
                reason="Fine.",
            )
        assert store.get(request.request_id).status is Status.PENDING
        assert len(store.audit_entries(request.request_id)) == 1


class TestReasonPolicy:
    def test_rejection_requires_a_reason(self, store: ApprovalStore) -> None:
        request = submit_sample(store, risk="low")
        with pytest.raises(ValueError, match="reason is required"):
            store.decide(
                request_id=request.request_id, decision="reject", decider="mark"
            )

    @pytest.mark.parametrize("risk", ["high", "critical"])
    def test_high_risk_approval_requires_a_reason(
        self, store: ApprovalStore, risk: str
    ) -> None:
        request = submit_sample(store, risk=risk)
        with pytest.raises(ValueError, match="reason is required"):
            store.decide(
                request_id=request.request_id, decision="approve", decider="mark"
            )

    @pytest.mark.parametrize("risk", ["low", "medium"])
    def test_low_risk_approval_needs_no_reason(
        self, store: ApprovalStore, risk: str
    ) -> None:
        request = submit_sample(store, risk=risk)
        decided = store.decide(
            request_id=request.request_id, decision="approve", decider="mark"
        )
        assert decided.status is Status.APPROVED

    def test_whitespace_only_reason_counts_as_missing(
        self, store: ApprovalStore
    ) -> None:
        request = submit_sample(store, risk="low")
        with pytest.raises(ValueError, match="reason is required"):
            store.decide(
                request_id=request.request_id,
                decision="reject",
                decider="mark",
                reason="   ",
            )


class TestExpiry:
    def test_pending_request_expires_after_its_deadline(
        self, store: ApprovalStore, now: datetime
    ) -> None:
        request = store.submit(
            action="do", payload={}, risk="low", requester="agent", ttl_hours=1, now=now
        )
        later = now + timedelta(hours=2)
        assert store.get(request.request_id, now=later).status is Status.EXPIRED

    def test_expiry_is_recorded_in_the_audit_log(
        self, store: ApprovalStore, now: datetime
    ) -> None:
        request = store.submit(
            action="do", payload={}, risk="low", requester="agent", ttl_hours=1, now=now
        )
        store.get(request.request_id, now=now + timedelta(hours=2))
        entries = store.audit_entries(request.request_id)
        assert entries[-1].event_type is EventType.EXPIRED
        assert entries[-1].actor == "system"
        assert "fails closed" in entries[-1].detail["note"]

    def test_expiry_happens_once(self, store: ApprovalStore, now: datetime) -> None:
        request = store.submit(
            action="do", payload={}, risk="low", requester="agent", ttl_hours=1, now=now
        )
        later = now + timedelta(hours=2)
        for _ in range(3):
            store.get(request.request_id, now=later)
        expired = [
            entry
            for entry in store.audit_entries(request.request_id)
            if entry.event_type is EventType.EXPIRED
        ]
        assert len(expired) == 1

    def test_expired_request_cannot_be_decided(
        self, store: ApprovalStore, now: datetime
    ) -> None:
        request = store.submit(
            action="do", payload={}, risk="low", requester="agent", ttl_hours=1, now=now
        )
        with pytest.raises(AlreadyDecidedError, match="expired"):
            store.decide(
                request_id=request.request_id,
                decision="approve",
                decider="mark",
                reason="Late.",
                now=now + timedelta(hours=2),
            )
        assert store.get(request.request_id).status is Status.EXPIRED

    def test_listing_sweeps_overdue_requests(
        self, store: ApprovalStore, now: datetime
    ) -> None:
        store.submit(
            action="a", payload={}, risk="low", requester="agent", ttl_hours=1, now=now
        )
        store.submit(
            action="b", payload={}, risk="low", requester="agent", ttl_hours=99, now=now
        )
        pending = store.list_requests(now=now + timedelta(hours=2))
        assert [request.action for request in pending] == ["b"]


class TestListing:
    def test_defaults_to_pending_only(self, store: ApprovalStore) -> None:
        keep = submit_sample(store, action="keep")
        decided = submit_sample(store, action="decided")
        store.decide(
            request_id=decided.request_id,
            decision="approve",
            decider="mark",
            reason="Fine.",
        )
        pending = store.list_requests()
        assert [request.request_id for request in pending] == [keep.request_id]

    def test_oldest_first(self, store: ApprovalStore, now: datetime) -> None:
        first = store.submit(
            action="first", payload={}, risk="low", requester="a", now=now
        )
        second = store.submit(
            action="second",
            payload={},
            risk="low",
            requester="a",
            now=now + timedelta(minutes=5),
        )
        listed = store.list_requests(now=now + timedelta(minutes=10))
        assert [item.request_id for item in listed] == [
            first.request_id,
            second.request_id,
        ]

    def test_filter_by_risk(self, store: ApprovalStore) -> None:
        submit_sample(store, risk="low")
        high = submit_sample(store, risk="critical")
        listed = store.list_requests(risk="critical")
        assert [item.request_id for item in listed] == [high.request_id]

    def test_filter_by_requester_is_case_insensitive(
        self, store: ApprovalStore
    ) -> None:
        wanted = submit_sample(store, requester="Billing-Agent")
        submit_sample(store, requester="other-agent")
        listed = store.list_requests(requester="billing-agent")
        assert [item.request_id for item in listed] == [wanted.request_id]

    def test_filter_by_action_substring(self, store: ApprovalStore) -> None:
        wanted = submit_sample(store, action="delete_production_index")
        submit_sample(store, action="send_email")
        listed = store.list_requests(action_contains="PRODUCTION")
        assert [item.request_id for item in listed] == [wanted.request_id]

    def test_status_none_returns_everything(self, store: ApprovalStore) -> None:
        submit_sample(store)
        decided = submit_sample(store)
        store.decide(
            request_id=decided.request_id,
            decision="approve",
            decider="mark",
            reason="Fine.",
        )
        assert len(store.list_requests(status=None)) == 2

    def test_limit_is_applied(self, store: ApprovalStore) -> None:
        for _ in range(5):
            submit_sample(store)
        assert len(store.list_requests(limit=2)) == 2

    @pytest.mark.parametrize("limit", [0, -1, 501])
    def test_invalid_limit_is_rejected(
        self, store: ApprovalStore, limit: int
    ) -> None:
        with pytest.raises(ValueError, match="limit must be between"):
            store.list_requests(limit=limit)

    def test_counts_by_status(self, store: ApprovalStore) -> None:
        submit_sample(store)
        rejected = submit_sample(store)
        store.decide(
            request_id=rejected.request_id,
            decision="reject",
            decider="mark",
            reason="No.",
        )
        counts = store.counts_by_status()
        assert counts["pending"] == 1
        assert counts["rejected"] == 1
        assert counts["approved"] == 0


class TestAuditTrail:
    def test_whole_log_is_ordered(self, store: ApprovalStore) -> None:
        first = submit_sample(store)
        second = submit_sample(store)
        store.decide(
            request_id=first.request_id,
            decision="approve",
            decider="mark",
            reason="Fine.",
        )
        entries = store.audit_entries()
        assert [entry.seq for entry in entries] == [1, 2, 3]
        assert entries[1].request_id == second.request_id

    def test_scoped_to_one_request(self, store: ApprovalStore) -> None:
        first = submit_sample(store)
        submit_sample(store)
        entries = store.audit_entries(first.request_id)
        assert {entry.request_id for entry in entries} == {first.request_id}

    def test_unknown_request_raises(self, store: ApprovalStore) -> None:
        with pytest.raises(RequestNotFoundError):
            store.audit_entries("ar_nope")

    def test_invalid_limit_is_rejected(self, store: ApprovalStore) -> None:
        with pytest.raises(ValueError, match="limit must be between"):
            store.audit_entries(limit=0)


class TestAppendOnlyEnforcement:
    def test_update_is_blocked_by_a_trigger(
        self, store: ApprovalStore, db_path: Path
    ) -> None:
        submit_sample(store)
        raw = sqlite3.connect(str(db_path))
        try:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute("UPDATE audit_log SET actor = 'forged' WHERE seq = 1")
        finally:
            raw.close()

    def test_delete_is_blocked_by_a_trigger(
        self, store: ApprovalStore, db_path: Path
    ) -> None:
        submit_sample(store)
        raw = sqlite3.connect(str(db_path))
        try:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute("DELETE FROM audit_log WHERE seq = 1")
        finally:
            raw.close()


class TestChainVerification:
    def test_empty_store_verifies(self, store: ApprovalStore) -> None:
        assert store.verify().valid

    def test_healthy_store_verifies(self, store: ApprovalStore) -> None:
        for _ in range(3):
            request = submit_sample(store, risk="low")
            store.decide(
                request_id=request.request_id, decision="approve", decider="mark"
            )
        result = store.verify()
        assert result.valid
        assert result.entries_checked == 6

    def test_tampering_is_detected_even_after_dropping_the_triggers(
        self, store: ApprovalStore, db_path: Path
    ) -> None:
        """The scenario the hash chain exists for.

        An attacker with write access to the file can remove the append-only
        triggers. The chain still gives them away.
        """
        request = submit_sample(store, risk="low")
        store.decide(
            request_id=request.request_id,
            decision="reject",
            decider="mark",
            reason="Not appropriate.",
        )
        assert store.verify().valid
        store.close()

        raw = sqlite3.connect(str(db_path))
        raw.execute("DROP TRIGGER audit_log_no_update")
        forged = json.dumps({"decision": "approve", "reason": "Actually fine."})
        raw.execute("UPDATE audit_log SET detail = ? WHERE seq = 2", (forged,))
        raw.commit()
        raw.close()

        with ApprovalStore(db_path) as reopened:
            result = reopened.verify()
            assert not result.valid
            assert result.broken_at_seq == 2
            assert "Content mismatch" in (result.problem or "")

    def test_deleting_an_entry_is_detected(
        self, store: ApprovalStore, db_path: Path
    ) -> None:
        for _ in range(3):
            submit_sample(store, risk="low")
        store.close()

        raw = sqlite3.connect(str(db_path))
        raw.execute("DROP TRIGGER audit_log_no_delete")
        raw.execute("DELETE FROM audit_log WHERE seq = 2")
        raw.commit()
        raw.close()

        with ApprovalStore(db_path) as reopened:
            result = reopened.verify()
            assert not result.valid
            assert "Sequence gap" in (result.problem or "")


class TestPersistence:
    def test_data_survives_reopening(self, db_path: Path) -> None:
        with ApprovalStore(db_path) as store:
            request = submit_sample(store)
            request_id = request.request_id
        with ApprovalStore(db_path) as reopened:
            assert reopened.get(request_id).status is Status.PENDING
            assert reopened.verify().valid

    def test_parent_directories_are_created(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "approvals.db"
        with ApprovalStore(nested) as store:
            submit_sample(store)
        assert nested.exists()

    def test_chain_continues_across_reopen(self, db_path: Path) -> None:
        with ApprovalStore(db_path) as store:
            submit_sample(store)
        with ApprovalStore(db_path) as reopened:
            submit_sample(reopened)
            entries = reopened.audit_entries()
            assert [entry.seq for entry in entries] == [1, 2]
            assert entries[1].prev_hash == entries[0].entry_hash
            assert reopened.verify().valid


class TestHelpers:
    def test_summarise_is_compact_and_serialisable(self, store: ApprovalStore) -> None:
        submit_sample(store)
        rows = summarise(store.list_requests())
        json.dumps(rows)
        assert "payload" not in rows[0]
        assert rows[0]["action"] == "send_customer_email"

    def test_oldest_first(self, store: ApprovalStore, now: datetime) -> None:
        second = store.submit(
            action="b",
            payload={},
            risk="low",
            requester="a",
            now=now + timedelta(hours=1),
        )
        first = store.submit(
            action="a", payload={}, risk="low", requester="a", now=now
        )
        ordered = oldest_first([second, first])
        assert [item.request_id for item in ordered] == [
            first.request_id,
            second.request_id,
        ]
