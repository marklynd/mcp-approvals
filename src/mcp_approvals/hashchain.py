"""Tamper-evident hash chaining over the audit log.

Each audit entry stores the hash of the entry before it. Changing any field of
any historical entry changes that entry's hash, which breaks its successor's
``prev_hash`` link, and so on to the end of the log. A single edit is therefore
detectable without needing to know what the original value was.

What this does and does not give you:

* It **does** detect after-the-fact edits, deletions and reordering of entries
  by anyone who cannot recompute the whole tail of the chain.
* It **does not** stop someone with write access to the database file from
  rewriting the entire chain from the point of the edit onwards. Defending
  against that needs the chain head anchored somewhere the attacker does not
  control: a periodic write to append-only storage, a transparency log, or a
  signature from a key held elsewhere. :func:`chain_head` returns the value to
  anchor.

That distinction is stated plainly because "tamper-proof" is the claim people
want to make here, and it is not the claim this supports. Tamper-*evident* is.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from .models import AuditEntry, to_iso

#: ``prev_hash`` of the first entry in a chain.
GENESIS_HASH: Final[str] = "0" * 64

HASH_ALGORITHM: Final[str] = "sha256"


def canonical_json(payload: Any) -> str:
    """Serialise ``payload`` so that equal values always produce equal bytes.

    Keys are sorted, separators are fixed and non-ASCII characters are preserved
    rather than escaped. Without this, two logically identical entries could
    hash differently depending on dictionary insertion order, and verification
    would report false tampering.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def compute_entry_hash(
    *,
    seq: int,
    request_id: str,
    event_type: str,
    actor: str,
    detail: Any,
    recorded_at: str,
    prev_hash: str,
) -> str:
    """Return the hash for one audit entry.

    Every field that a reader would rely on is inside the hash, including the
    sequence number, so an entry cannot be silently moved to a different
    position in the log.
    """
    material = canonical_json(
        {
            "seq": seq,
            "request_id": request_id,
            "event_type": event_type,
            "actor": actor,
            "detail": detail,
            "recorded_at": recorded_at,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def hash_for_entry(entry: AuditEntry) -> str:
    """Recompute the hash of an existing :class:`AuditEntry`."""
    return compute_entry_hash(
        seq=entry.seq,
        request_id=entry.request_id,
        event_type=entry.event_type.value,
        actor=entry.actor,
        detail=entry.detail,
        recorded_at=to_iso(entry.recorded_at),
        prev_hash=entry.prev_hash,
    )


@dataclass(frozen=True)
class ChainVerification:
    """The result of verifying an audit chain.

    Attributes:
        valid: Whether every entry hashes correctly and links to its predecessor.
        entries_checked: How many entries were examined.
        head_hash: Hash of the final entry, or the genesis hash if empty.
        broken_at_seq: Sequence number of the first bad entry, if any.
        problem: Human-readable description of the first problem found.
    """

    valid: bool
    entries_checked: int
    head_hash: str
    broken_at_seq: int | None = None
    problem: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        payload: dict[str, Any] = {
            "valid": self.valid,
            "entries_checked": self.entries_checked,
            "head_hash": self.head_hash,
            "algorithm": HASH_ALGORITHM,
        }
        if not self.valid:
            payload["broken_at_seq"] = self.broken_at_seq
            payload["problem"] = self.problem
            payload["interpretation"] = (
                "The audit log has been altered after the fact, or was written "
                "by a different version of this software. Treat every entry "
                "from the broken sequence onwards as untrustworthy."
            )
        else:
            payload["interpretation"] = (
                "Every entry hashes correctly and links to its predecessor. "
                "Anchor head_hash outside this database to also detect a "
                "wholesale rewrite of the chain."
            )
        return payload


def verify_chain(entries: Sequence[AuditEntry]) -> ChainVerification:
    """Verify that ``entries`` form an unbroken hash chain.

    Args:
        entries: Audit entries in ascending sequence order.

    Returns:
        A :class:`ChainVerification`. An empty log is valid by definition.
    """
    expected_prev = GENESIS_HASH
    expected_seq = 1

    for entry in entries:
        if entry.seq != expected_seq:
            return ChainVerification(
                valid=False,
                entries_checked=expected_seq - 1,
                head_hash=expected_prev,
                broken_at_seq=entry.seq,
                problem=(
                    f"Sequence gap: expected seq {expected_seq}, found {entry.seq}. "
                    "An entry was removed or reordered."
                ),
            )
        if entry.prev_hash != expected_prev:
            return ChainVerification(
                valid=False,
                entries_checked=expected_seq - 1,
                head_hash=expected_prev,
                broken_at_seq=entry.seq,
                problem=(
                    f"Broken link at seq {entry.seq}: prev_hash does not match "
                    "the hash of the preceding entry."
                ),
            )
        recomputed = hash_for_entry(entry)
        if recomputed != entry.entry_hash:
            return ChainVerification(
                valid=False,
                entries_checked=expected_seq - 1,
                head_hash=expected_prev,
                broken_at_seq=entry.seq,
                problem=(
                    f"Content mismatch at seq {entry.seq}: the stored entry_hash "
                    "does not match a hash of the stored fields, so a field was "
                    "modified after it was written."
                ),
            )
        expected_prev = entry.entry_hash
        expected_seq += 1

    return ChainVerification(
        valid=True,
        entries_checked=len(entries),
        head_hash=expected_prev,
    )


def chain_head(entries: Sequence[AuditEntry]) -> str:
    """Return the hash to anchor externally, or the genesis hash if empty."""
    return entries[-1].entry_hash if entries else GENESIS_HASH
