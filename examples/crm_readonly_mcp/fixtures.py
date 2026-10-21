"""Invented CRM records; no production, employer, client, or personal data."""

from dataclasses import dataclass
from datetime import date

SNAPSHOT_DATE = date(2026, 10, 15)
DATASET_ID = "synthetic-crm-v1"
CAMPAIGNS = ("webinar", "newsletter", "event")
STAGES = ("lead", "mql", "sql", "opportunity", "customer", "lost")
REVIEW_STAGES = ("mql", "sql", "opportunity")


@dataclass(frozen=True)
class Lead:
    """Immutable invented record, including fields never returned to a caller."""

    record_id: str
    campaign: str
    stage: str
    last_activity: date
    opted_in: bool = True
    do_not_contact: bool = False
    internal_note: str = "Synthetic internal note: never expose through tools."


LEADS = (
    Lead("SYN-001", "webinar", "lead", date(2026, 10, 14)),
    Lead("SYN-002", "webinar", "mql", date(2026, 9, 29)),
    Lead("SYN-003", "webinar", "sql", date(2026, 9, 24)),
    Lead("SYN-004", "webinar", "opportunity", date(2026, 10, 12)),
    Lead("SYN-005", "newsletter", "mql", date(2026, 10, 1)),
    Lead("SYN-006", "newsletter", "mql", date(2026, 10, 1), opted_in=False),
    Lead("SYN-007", "newsletter", "customer", date(2026, 10, 2)),
    Lead("SYN-008", "newsletter", "lost", date(2026, 10, 2)),
    Lead("SYN-009", "event", "sql", date(2026, 10, 8)),
    Lead("SYN-010", "event", "opportunity", date(2026, 9, 20)),
    Lead("SYN-011", "event", "lead", date(2026, 9, 18)),
    Lead("SYN-012", "event", "sql", date(2026, 9, 15), do_not_contact=True),
)
