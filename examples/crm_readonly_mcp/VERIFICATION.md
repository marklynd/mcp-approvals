# Verification record

Executed 2026-10-21 in a cloud Python 3.12.14 environment with the official
`mcp==1.29.0` SDK. All fixtures are synthetic. No user's computer or production
CRM was used. Output artifacts are actual executions, not fabricated UI images.

- New example regression tests: **55 passed**, including real MCP stdio transport.
- Standalone transport smoke: **PASS**. Initialization, discovery of three tools,
  four successful calls, and ten rejected invalid/mutation attempts.
- Repository Ruff lint: **passed**.
- Existing approvals Mypy check: **passed**, seven source files.
- Example Mypy strict check: **passed**, six source files.
- Saved `DEMO_OUTPUT.txt`: exact-match test against a fresh run **passed**.
- Full repository suite: **206 passed, 2 failed**. Not a clean overall CI claim.

## Existing baseline failures, not introduced by this example

The unchanged approvals store tests also fail when run independently, without
importing the new example:

1. `tests/test_store.py::TestExpiry::test_listing_sweeps_overdue_requests`
2. `tests/test_store.py::TestListing::test_oldest_first`

Both inject July 25, 2026 timestamps. `ApprovalStore.submit()` returns
`self.get(request_id)` without forwarding its injected `now`, so the read uses
the real clock and expires those historical requests before the test lists them.
This was reproduced on 2026-10-21. Approvals production logic and its existing
tests are deliberately unchanged by this demonstration; addressing that bug is
separate work. The demo uses its own fixed synthetic snapshot and does not
import the approvals store.

Python 3.10 and 3.11 were not executed locally. Remote CI results, when present,
are authoritative for their own exact commit and should be read on the draft PR.
A successful demonstration is not a claim of production readiness, security
certification, live HubSpot interoperability, or prior marketing outcomes.
