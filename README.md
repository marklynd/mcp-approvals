# mcp-approvals

[![CI](https://github.com/marklynd/mcp-approvals/actions/workflows/ci.yml/badge.svg)](https://github.com/marklynd/mcp-approvals/actions/workflows/ci.yml) [![Container](https://github.com/marklynd/mcp-approvals/actions/workflows/publish-container.yml/badge.svg)](https://github.com/marklynd/mcp-approvals/actions/workflows/publish-container.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) ![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)

An MCP server that puts a human decision in front of a consequential agent
action, and makes the decision - or the absence of one - permanently
reconstructable.

Submit an action for approval. Stop. A human approves or rejects, with a reason.
Every step lands in an append-only, hash-chained audit log that will not agree
with you afterwards if someone edits it.

SQLite, standard library, no API key, no network calls.

---

## Why this exists

The blocker on agent adoption in a regulated environment is rarely capability.
It is that nobody can answer "who approved that, and can you prove it".

An approval gate is the shape of the answer, and it is the control that shows up
in every framework that matters. NIST AI RMF wants human oversight processes
defined and assessed (`MAP 3.5`, `GOVERN 3.2`) and a mechanism to disengage a
misbehaving system (`MANAGE 2.4`). ISO/IEC 42001 wants processes for responsible
use (`A.9.2`) and event logs (`A.6.2.8`). The EU AI Act requires effective human
oversight for high-risk systems (`Art. 14`) and automatic record-keeping over the
system's lifetime (`Art. 12`).

This implements that pattern as something an agent can actually call, and gets
three details right that homegrown versions usually get wrong:

- **A decision is never overwritten.** Deciding an already-decided request is an
  error, not an update. Silent overwrite is precisely the failure the control
  exists to prevent.
- **Nobody approves their own request.** Separation of duties is on by default.
  An agent that can approve its own action is not gated at all.
- **Timeouts fail closed.** An expired request is not an approval, and the
  expiry itself is written to the log rather than happening silently.

*(Its companion, [mcp-ai-governance](../mcp-ai-governance), maps controls like
this one onto the frameworks above.)*

---

## Install

Requires Python 3.10 or later.

```bash
git clone https://github.com/marklynd/mcp-approvals
cd mcp-approvals
pip install -e ".[dev]"
```

Verify it works:

```bash
python examples/smoke_test.py   # walks a full gated workflow on a temp database
python -m pytest                # 153 tests
```

## Run

```bash
mcp-approvals
# or, without installing:
PYTHONPATH=src python3 -m mcp_approvals
```

### Connect it to Claude Desktop

Add this to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`, Windows:
`%APPDATA%\Claude\claude_desktop_config.json`), then restart the app:

```json
{
  "mcpServers": {
    "approvals": {
      "command": "mcp-approvals",
      "env": {
        "MCP_APPROVALS_DB": "/Users/YOU/.local/share/mcp-approvals/approvals.db",
        "MCP_APPROVALS_ALLOW_SELF_APPROVAL": "false"
      }
    }
  }
}
```

See [`examples/claude_desktop_config.json`](examples/claude_desktop_config.json)
for the run-from-a-clone variant.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MCP_APPROVALS_DB` | `$XDG_DATA_HOME/mcp-approvals/approvals.db`, else `~/.local/share/mcp-approvals/approvals.db` | SQLite file. Parent directories are created. |
| `MCP_APPROVALS_DEFAULT_TTL_HOURS` | unset | Overrides the per-risk time-to-live for every level. 1 to 2160. |
| `MCP_APPROVALS_ALLOW_SELF_APPROVAL` | `false` | Whether a requester may decide its own request. Leave off unless one person is both operator and approver. |

Default time-to-live is set by risk: low 168h, medium 72h, high 24h,
critical 8h. Higher risk expires sooner on purpose - a critical request nobody
has looked at in eight hours should fail closed rather than wait around to be
rubber-stamped.

---

## Worked example

### 1. The agent asks instead of acting

```python
submit_for_approval(
    action="wire_transfer",
    payload={"amount_usd": 48000, "beneficiary": "Acme Ltd", "account": "GB29...0031"},
    risk="critical",
    requester="finance-agent",
    context="Invoice INV-2041, matched to PO-889.",
)
```

```json
{
  "request_id": "ar_a0b1d5a888c3436e",
  "action": "wire_transfer",
  "payload": {"account": "GB29...0031", "amount_usd": 48000, "beneficiary": "Acme Ltd"},
  "risk": "critical",
  "requester": "finance-agent",
  "status": "pending",
  "created_at": "2026-07-25T13:39:49Z",
  "expires_at": "2026-07-25T21:39:49Z",
  "context": "Invoice INV-2041, matched to PO-889.",
  "decided_at": null,
  "decider": null,
  "decision": null,
  "reason": null,
  "is_terminal": false,
  "ok": true,
  "next_step": "Do not take the action. Poll get_status with this request_id and proceed only if status becomes 'approved'."
}
```

### 2. The gate holds

```json
{
  "status": "pending",
  "may_proceed": false,
  "guidance": "Still awaiting a human decision. Do not take the action. This request expires at 2026-07-25T21:39:49Z."
}
```

### 3. The human decides

`decide(request_id, "approve", decider="mark", reason="Confirmed the beneficiary by phone.")`.
The reason is mandatory here because the risk is critical. `may_proceed` becomes
`true`, and only now may the agent act.

### 4. The decision cannot be quietly changed

Someone tries to flip it:

```json
{
  "ok": false,
  "error": "already_decided",
  "message": "Request 'ar_a0b1d5a888c3436e' is already approved (decided by mark at 2026-07-25T13:39:49Z). Decisions are never overwritten; submit a new request instead.",
  "current_status": "approved"
}
```

### 5. The whole thing is replayable

```json
{
  "count": 2,
  "entries": [
    {
      "seq": 1,
      "event_type": "submitted",
      "actor": "finance-agent",
      "detail": {
        "action": "wire_transfer",
        "context": "Invoice INV-2041, matched to PO-889.",
        "expires_at": "2026-07-25T21:39:49Z",
        "payload": {"account": "GB29...0031", "amount_usd": 48000, "beneficiary": "Acme Ltd"},
        "risk": "critical",
        "ttl_hours": 8
      },
      "recorded_at": "2026-07-25T13:39:49Z",
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "entry_hash": "b073e3bbee2a2f374f3ebb6fb0eab7284caa2ae68f6fa45a6509006548e49558"
    },
    {
      "seq": 2,
      "event_type": "decided",
      "actor": "mark",
      "detail": {
        "action": "wire_transfer",
        "decision": "approve",
        "reason": "Confirmed the beneficiary by phone.",
        "requester": "finance-agent",
        "resulting_status": "approved",
        "risk": "critical"
      },
      "recorded_at": "2026-07-25T13:39:49Z",
      "prev_hash": "b073e3bbee2a2f374f3ebb6fb0eab7284caa2ae68f6fa45a6509006548e49558",
      "entry_hash": "83bdacc76a3c86325b2054664074c5af5bbb098af4b9d189f223942dc160c420"
    }
  ],
  "chain_head": "83bdacc76a3c86325b2054664074c5af5bbb098af4b9d189f223942dc160c420"
}
```

### 6. And tampering shows

The audit log is append-only at the storage layer: SQLite triggers reject any
`UPDATE` or `DELETE`. Someone with write access to the file can drop those
triggers. Here is what happens when they do, and edit the decision to say it was
approved for a different reason:

```json
{
  "valid": false,
  "entries_checked": 1,
  "broken_at_seq": 2,
  "problem": "Content mismatch at seq 2: the stored entry_hash does not match a hash of the stored fields, so a field was modified after it was written.",
  "interpretation": "The audit log has been altered after the fact, or was written by a different version of this software. Treat every entry from the broken sequence onwards as untrustworthy."
}
```

That exact scenario - drop the triggers, edit the row, reopen, verify - is a test
in [`tests/test_store.py`](tests/test_store.py).

---

## Tool reference

| Tool | Arguments | Returns |
|---|---|---|
| `submit_for_approval` | `action`, `payload` (object, max 64 KB), `risk` (`low`/`medium`/`high`/`critical`), `requester`, `context` (optional), `ttl_hours` (optional) | The pending request with its `request_id` and expiry deadline |
| `get_status` | `request_id` | Full request plus `may_proceed` and plain-language guidance for the agent |
| `list_pending` | `risk`, `requester`, `action_contains`, `status` (default `pending`, or `all`), `limit` (1-500) | Matching requests oldest first, plus counts by status |
| `decide` | `request_id`, `decision` (`approve`/`reject`), `decider`, `reason` | Updated request, or a structured error for unknown / already-decided / self-approval |
| `audit_trail` | `request_id` (or omit / `"all"`), `limit` (1-500) | Ordered entries with `prev_hash` and `entry_hash`, plus the chain head |
| `verify_audit_chain` | none | Whether the chain is intact, entries checked, chain head, and on failure the sequence number and nature of the break |
| `gate_policy` | none | TTL per risk, which risks require a reason, whether self-approval is allowed, database location |

### The contract an agent should follow

1. Call `submit_for_approval` **before** the action.
2. Poll `get_status`. Proceed **only** on `may_proceed: true`.
3. Treat `pending`, `rejected` and `expired` identically: do not act.

The server states this in its MCP instructions and repeats it in every relevant
response, because that is the only lever it has. See "Scope and limitations".

---

## Design notes

**The audit log is append-only in the database, not just in the application.**

```sql
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: entries cannot be updated'); END;
```

A bug in this module hits the same wall as the `sqlite3` CLI. The hash chain is
the second layer, for when someone removes the first.

**Sequence numbers are assigned inside the write transaction**, not by
`AUTOINCREMENT`, because `seq` is part of the hashed material and must be known
before the hash is computed. That is what makes reordering detectable.

**Hashing is over canonical JSON** - sorted keys, fixed separators - so two
logically identical entries always hash identically. Without that, verification
would report false tampering depending on dictionary insertion order.

**Writes use `BEGIN IMMEDIATE`** and are serialised by a process-level lock. Two
concurrent appends must not read the same chain head. WAL mode means separate
processes wait rather than corrupt.

**Expiry is lazy, evaluated on read.** No background thread. An overdue request
transitions to `expired` and writes an audit entry the next time anything looks
at it, so the log records the transition rather than the state changing silently.

**Queues are read oldest first.** Newest-first quietly starves the requests that
have waited longest, which is backwards for an approval queue.

---

## Scope and limitations

**This server cannot stop an agent from skipping the gate.** It does not sit in
the network path of whatever the agent is doing. It is a protocol the agent
follows, and a record that contradicts the agent if it did not. Enforcement has
to live where the action actually happens: revoke the agent's direct credentials
for the consequential system and make the approved request the only path to
them. That is an architecture decision this repository cannot make for you.

**Tamper-evident, not tamper-proof.** The chain detects edits, deletions and
reordering by anyone who cannot recompute the whole tail. It does not stop
someone with write access to the file from rewriting the chain from the point of
the edit onwards. Closing that needs the head anchored where they have no
control: a periodic append-only write elsewhere, a transparency log, or a
signature from a key held off the box. `verify_audit_chain` returns `head_hash`
for exactly this, and says so in its own output rather than letting you assume
otherwise.

**Identity is a string, not an authenticated principal.** `requester` and
`decider` are whatever the caller says they are. The self-approval check compares
those strings case-insensitively. In a real deployment, bind them to
authenticated identities at the transport or client layer; this component takes
them on trust.

**SQLite, so single-host.** Fine for a workstation, a single agent runner or a
small team. Multi-region or high-write deployments want Postgres, which would
mean reimplementing `store.py` while keeping `hashchain.py` unchanged - the chain
logic has no SQLite dependency.

**Payloads are stored in the clear**, capped at 64 KB. Do not put secrets in
`payload`. Reference them; do not copy them.

**No notification transport.** Nothing emails, pages or Slacks a human when a
request lands. `list_pending` is the queue; wiring it to somewhere a human
actually looks is deliberately out of scope.

**No delegation, escalation or quorum.** One decider, one decision. Two-person
integrity and role-based routing are real requirements in some environments and
are not implemented.

**Clock trust.** Timestamps come from the host clock. Expiry and ordering are
only as trustworthy as it is.

---

## Read-only CRM demonstration

A separately runnable [synthetic CRM MCP example](examples/crm_readonly_mcp/README.md)
shows bounded funnel-stage summaries and human-review candidates using invented
records only. It includes real stdio transport tests and reproducible output.
It is not a live CRM integration and does not invoke or enforce the approval
gate. The approvals core is unchanged.

---

## Development

```bash
python -m pytest        # 153 tests
python -m mypy src      # strict mode
python -m ruff check .
```

Layout:

```
src/mcp_approvals/
  server.py       MCP tool definitions (FastMCP)
  store.py        SQLite store, append-only triggers, expiry
  hashchain.py    canonical JSON, chaining, verification (no storage dependency)
  models.py       risk levels, statuses, decisions, domain errors
  config.py       environment configuration
examples/         MCP client config and a smoke test
tests/            pytest suite
```

The store is usable as a plain Python library, with no MCP client involved:

```python
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
    assert store.verify().valid
```

---

## Licence

MIT. Copyright (c) 2026 Mark Lynd.

---

## Run it as a container

A multi-stage image is published to the GitHub Container Registry on every push to `main`.

```bash
docker pull ghcr.io/marklynd/mcp-approvals:latest
docker run --rm -i ghcr.io/marklynd/mcp-approvals:latest
```

The server speaks MCP over stdio, so `-i` is required and `-t` must be omitted. To wire it into Claude Desktop, point the command at `docker` with `["run","--rm","-i","ghcr.io/marklynd/mcp-approvals:latest"]` as the args.
