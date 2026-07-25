"""Tests for the hash chain primitives."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from mcp_approvals.hashchain import (
    GENESIS_HASH,
    canonical_json,
    chain_head,
    compute_entry_hash,
    hash_for_entry,
    verify_chain,
)
from mcp_approvals.models import AuditEntry, EventType

MOMENT = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


def make_entry(seq: int, prev_hash: str, *, detail: dict | None = None) -> AuditEntry:
    """Build a correctly hashed entry for testing."""
    payload = detail if detail is not None else {"n": seq}
    entry_hash = compute_entry_hash(
        seq=seq,
        request_id="ar_test",
        event_type="submitted",
        actor="agent",
        detail=payload,
        recorded_at="2026-07-25T12:00:00Z",
        prev_hash=prev_hash,
    )
    return AuditEntry(
        seq=seq,
        request_id="ar_test",
        event_type=EventType.SUBMITTED,
        actor="agent",
        detail=payload,
        recorded_at=MOMENT,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )


def make_chain(length: int) -> list[AuditEntry]:
    """Build a valid chain of ``length`` entries."""
    entries: list[AuditEntry] = []
    prev = GENESIS_HASH
    for seq in range(1, length + 1):
        entry = make_entry(seq, prev)
        entries.append(entry)
        prev = entry.entry_hash
    return entries


class TestCanonicalJson:
    def test_key_order_does_not_change_the_encoding(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_encoding_is_compact(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_nested_structures_are_also_ordered(self) -> None:
        left = canonical_json({"x": {"b": 1, "a": 2}})
        right = canonical_json({"x": {"a": 2, "b": 1}})
        assert left == right

    def test_non_ascii_is_preserved_not_escaped(self) -> None:
        assert "é" in canonical_json({"name": "café"})

    def test_unserialisable_values_fall_back_to_str(self) -> None:
        assert "2026" in canonical_json({"when": MOMENT})


class TestComputeEntryHash:
    def test_is_deterministic(self) -> None:
        kwargs = {
            "seq": 1,
            "request_id": "ar_1",
            "event_type": "submitted",
            "actor": "agent",
            "detail": {"a": 1},
            "recorded_at": "2026-07-25T12:00:00Z",
            "prev_hash": GENESIS_HASH,
        }
        assert compute_entry_hash(**kwargs) == compute_entry_hash(**kwargs)

    def test_is_sha256_shaped(self) -> None:
        digest = compute_entry_hash(
            seq=1,
            request_id="ar_1",
            event_type="submitted",
            actor="agent",
            detail={},
            recorded_at="2026-07-25T12:00:00Z",
            prev_hash=GENESIS_HASH,
        )
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    @pytest.mark.parametrize(
        "field,value",
        [
            ("seq", 2),
            ("request_id", "ar_other"),
            ("event_type", "decided"),
            ("actor", "someone-else"),
            ("detail", {"a": 2}),
            ("recorded_at", "2026-07-25T12:00:01Z"),
            ("prev_hash", "f" * 64),
        ],
    )
    def test_every_field_is_covered_by_the_hash(self, field: str, value: object) -> None:
        base: dict[str, object] = {
            "seq": 1,
            "request_id": "ar_1",
            "event_type": "submitted",
            "actor": "agent",
            "detail": {"a": 1},
            "recorded_at": "2026-07-25T12:00:00Z",
            "prev_hash": GENESIS_HASH,
        }
        changed = {**base, field: value}
        assert compute_entry_hash(**base) != compute_entry_hash(**changed)  # type: ignore[arg-type]


class TestVerifyChain:
    def test_empty_chain_is_valid(self) -> None:
        result = verify_chain([])
        assert result.valid
        assert result.entries_checked == 0
        assert result.head_hash == GENESIS_HASH

    def test_valid_chain_verifies(self) -> None:
        entries = make_chain(5)
        result = verify_chain(entries)
        assert result.valid
        assert result.entries_checked == 5
        assert result.head_hash == entries[-1].entry_hash

    def test_edited_detail_is_detected(self) -> None:
        entries = make_chain(4)
        entries[1] = replace(entries[1], detail={"n": "tampered"})
        result = verify_chain(entries)
        assert not result.valid
        assert result.broken_at_seq == 2
        assert "Content mismatch" in (result.problem or "")

    def test_edited_actor_is_detected(self) -> None:
        entries = make_chain(3)
        entries[2] = replace(entries[2], actor="not-the-real-decider")
        result = verify_chain(entries)
        assert not result.valid
        assert result.broken_at_seq == 3

    def test_deleted_entry_is_detected(self) -> None:
        entries = make_chain(5)
        del entries[2]
        result = verify_chain(entries)
        assert not result.valid
        assert result.broken_at_seq == 4
        assert "Sequence gap" in (result.problem or "")

    def test_reordered_entries_are_detected(self) -> None:
        entries = make_chain(4)
        entries[1], entries[2] = entries[2], entries[1]
        result = verify_chain(entries)
        assert not result.valid

    def test_broken_link_is_detected(self) -> None:
        entries = make_chain(3)
        forged = make_entry(2, prev_hash="a" * 64)
        entries[1] = forged
        result = verify_chain(entries)
        assert not result.valid
        assert result.broken_at_seq == 2

    def test_appending_a_forged_entry_is_detected(self) -> None:
        entries = make_chain(2)
        entries.append(make_entry(3, prev_hash=GENESIS_HASH))
        result = verify_chain(entries)
        assert not result.valid
        assert result.broken_at_seq == 3

    def test_failure_payload_explains_the_impact(self) -> None:
        entries = make_chain(3)
        entries[1] = replace(entries[1], detail={"n": "tampered"})
        payload = verify_chain(entries).to_dict()
        assert payload["valid"] is False
        assert "untrustworthy" in payload["interpretation"]
        assert payload["broken_at_seq"] == 2

    def test_success_payload_points_at_external_anchoring(self) -> None:
        payload = verify_chain(make_chain(2)).to_dict()
        assert payload["valid"] is True
        assert "Anchor head_hash" in payload["interpretation"]
        assert payload["algorithm"] == "sha256"


class TestChainHead:
    def test_empty_chain_head_is_genesis(self) -> None:
        assert chain_head([]) == GENESIS_HASH

    def test_head_is_the_last_entry_hash(self) -> None:
        entries = make_chain(3)
        assert chain_head(entries) == entries[-1].entry_hash


class TestHashForEntry:
    def test_recomputes_the_stored_hash(self) -> None:
        entry = make_entry(1, GENESIS_HASH)
        assert hash_for_entry(entry) == entry.entry_hash
