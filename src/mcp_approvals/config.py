"""Environment configuration.

Everything is read at process start and exposed as one frozen object so that a
misconfiguration fails immediately with a clear message, rather than at the
moment someone tries to approve something.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ENV_DB_PATH: Final[str] = "MCP_APPROVALS_DB"
ENV_DEFAULT_TTL_HOURS: Final[str] = "MCP_APPROVALS_DEFAULT_TTL_HOURS"
ENV_ALLOW_SELF_APPROVAL: Final[str] = "MCP_APPROVALS_ALLOW_SELF_APPROVAL"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSEY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})

MAX_TTL_HOURS: Final[int] = 24 * 90


def default_db_path() -> Path:
    """Return the default database location.

    Follows the XDG data directory when ``XDG_DATA_HOME`` is set, otherwise
    ``~/.local/share/mcp-approvals/approvals.db``. The directory is not created
    here; :class:`~mcp_approvals.store.ApprovalStore` does that on connect.
    """
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "mcp-approvals" / "approvals.db"


def _parse_bool(raw: str, variable: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSEY:
        return False
    raise ValueError(
        f"{variable} must be one of true/false/1/0/yes/no/on/off (received {raw!r})"
    )


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration.

    Attributes:
        db_path: SQLite file backing the store.
        default_ttl_hours: If set, overrides the per-risk default time-to-live
            for every risk level.
        allow_self_approval: Whether a requester may decide their own request.
            Off by default: separation of duties is the whole point of an
            approval gate, and an agent that can approve its own action is not
            gated at all.
    """

    db_path: Path
    default_ttl_hours: int | None
    allow_self_approval: bool


def load_config(environ: dict[str, str] | None = None) -> Config:
    """Read configuration from the environment.

    Args:
        environ: Mapping to read from. Defaults to ``os.environ``. Injectable so
            tests do not have to mutate global state.

    Returns:
        The resolved :class:`Config`.

    Raises:
        ValueError: If a variable is present but not parseable, or the TTL is
            outside 1 hour to 90 days.
    """
    env = os.environ if environ is None else environ

    raw_path = env.get(ENV_DB_PATH, "").strip()
    db_path = Path(raw_path).expanduser() if raw_path else default_db_path()

    ttl: int | None = None
    raw_ttl = env.get(ENV_DEFAULT_TTL_HOURS, "").strip()
    if raw_ttl:
        try:
            ttl = int(raw_ttl)
        except ValueError as exc:
            raise ValueError(
                f"{ENV_DEFAULT_TTL_HOURS} must be a whole number of hours "
                f"(received {raw_ttl!r})"
            ) from exc
        if not 1 <= ttl <= MAX_TTL_HOURS:
            raise ValueError(
                f"{ENV_DEFAULT_TTL_HOURS} must be between 1 and {MAX_TTL_HOURS} "
                f"hours (received {ttl})"
            )

    allow_self = _parse_bool(
        env.get(ENV_ALLOW_SELF_APPROVAL, "false"), ENV_ALLOW_SELF_APPROVAL
    )

    return Config(
        db_path=db_path,
        default_ttl_hours=ttl,
        allow_self_approval=allow_self,
    )
