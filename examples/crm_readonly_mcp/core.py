"""Small, deterministic query surface with no writes, SQL, paths, or network."""

from typing import Any

from .fixtures import CAMPAIGNS, DATASET_ID, LEADS, REVIEW_STAGES, SNAPSHOT_DATE, STAGES

PUBLIC_CANDIDATE_FIELDS = (
    "record_id",
    "campaign",
    "stage",
    "days_since_activity",
    "review_reason",
)
TOOL_NAMES = ("crm_data_contract", "crm_funnel_summary", "crm_review_candidates")


def _metadata() -> dict[str, Any]:
    return {
        "dataset_id": DATASET_ID,
        "synthetic": True,
        "snapshot_date": SNAPSHOT_DATE.isoformat(),
        "read_only": True,
    }


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}.")
    return value


def _campaign(value: Any) -> str:
    if type(value) is not str or value not in ("all", *CAMPAIGNS):
        raise ValueError("campaign must be all, webinar, newsletter, or event.")
    return value


def data_contract() -> dict[str, Any]:
    """Describe what this demonstration can and cannot reveal or do."""
    return {
        **_metadata(),
        "record_count": len(LEADS),
        "campaigns": list(CAMPAIGNS),
        "stages": list(STAGES),
        "candidate_fields": list(PUBLIC_CANDIDATE_FIELDS),
        "excluded_fields": ["internal_note", "opted_in", "do_not_contact"],
        "candidate_rule": "Opted in, not do-not-contact, mql/sql/opportunity, idle at least min_idle_days.",
        "candidate_order": "days_since_activity descending, then record_id ascending",
        "limits": {"max_candidates": 20, "min_idle_days_range": [1, 90]},
        "limitations": [
            "Newly authored synthetic-data demonstration, not a live CRM integration.",
            "Current-stage counts are not historical conversion rates or marketing results.",
            "Eligibility flags are invented screening inputs, not legal consent verification.",
            "Review candidates are not permission to contact anyone.",
            "No authentication, tenant isolation, durable audit, approval gate, or writes.",
        ],
    }


def funnel_summary(campaign: str = "all") -> dict[str, Any]:
    """Count records at each current stage, not historical stage transitions."""
    campaign = _campaign(campaign)
    selected = [row for row in LEADS if campaign == "all" or row.campaign == campaign]
    counts = {stage: sum(row.stage == stage for row in selected) for stage in STAGES}
    return {
        **_metadata(),
        "campaign": campaign,
        "total_records": len(selected),
        "current_stage_counts": counts,
        "screened_out_records": sum(not row.opted_in or row.do_not_contact for row in selected),
        "interpretation": "Snapshot stage inventory including screened-out records; not conversion rates or ROI.",
    }


def review_candidates(
    campaign: str = "all",
    min_idle_days: int = 14,
    limit: int = 5,
) -> dict[str, Any]:
    """Return an allowlisted, bounded human-review shortlist; never send anything."""
    campaign = _campaign(campaign)
    min_idle_days = _integer(min_idle_days, 1, 90, "min_idle_days")
    limit = _integer(limit, 1, 20, "limit")
    rows: list[dict[str, Any]] = []
    for row in LEADS:
        idle_days = (SNAPSHOT_DATE - row.last_activity).days
        if (
            (campaign == "all" or row.campaign == campaign)
            and row.opted_in
            and not row.do_not_contact
            and row.stage in REVIEW_STAGES
            and idle_days >= min_idle_days
        ):
            # Deliberate projection: never serialize the entire fixture record.
            rows.append(
                {
                    "record_id": row.record_id,
                    "campaign": row.campaign,
                    "stage": row.stage,
                    "days_since_activity": idle_days,
                    "review_reason": "Review stale qualified record; verify context and permission before any outreach.",
                }
            )
    rows.sort(key=lambda row: (-int(row["days_since_activity"]), str(row["record_id"])))
    return {
        **_metadata(),
        "filters": {"campaign": campaign, "min_idle_days": min_idle_days, "limit": limit},
        "matched_count": len(rows),
        "returned_count": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "candidates": rows[:limit],
        "requires_human_review": True,
        "outreach_authorized": False,
        "next_step": "Human review only. This server cannot draft, send, update records, or grant approval.",
    }


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown tools and arguments rather than interpreting arbitrary input."""
    if type(name) is not str or name not in TOOL_NAMES:
        raise ValueError("Unsupported tool; only the three documented read-only tools exist.")
    if type(arguments) is not dict:
        raise ValueError("arguments must be an object.")
    allowed = {
        "crm_data_contract": set(),
        "crm_funnel_summary": {"campaign"},
        "crm_review_candidates": {"campaign", "min_idle_days", "limit"},
    }[name]
    if arguments.keys() - allowed:
        raise ValueError("Unsupported argument; only documented filters are accepted.")
    if name == "crm_data_contract":
        return data_contract()
    if name == "crm_funnel_summary":
        return funnel_summary(**arguments)
    return review_candidates(**arguments)
