"""Tests for environment configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_approvals.config import (
    ENV_ALLOW_SELF_APPROVAL,
    ENV_DB_PATH,
    ENV_DEFAULT_TTL_HOURS,
    default_db_path,
    load_config,
)


class TestDefaults:
    def test_no_environment_gives_sensible_defaults(self) -> None:
        config = load_config({})
        assert config.db_path == default_db_path()
        assert config.default_ttl_hours is None
        assert config.allow_self_approval is False

    def test_default_path_follows_xdg_when_set(self, monkeypatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-example")  # noqa: S108 - path string only, never written
        assert default_db_path() == Path("/tmp/xdg-example/mcp-approvals/approvals.db")  # noqa: S108

    def test_default_path_falls_back_to_local_share(self, monkeypatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert default_db_path().parts[-3:] == (
            ".local",
            "share",
        ) + ("mcp-approvals",) or default_db_path().name == "approvals.db"


class TestDbPath:
    def test_explicit_path_is_used(self) -> None:
        config = load_config({ENV_DB_PATH: "/tmp/custom/approvals.db"})  # noqa: S108
        assert config.db_path == Path("/tmp/custom/approvals.db")  # noqa: S108

    def test_tilde_is_expanded(self) -> None:
        config = load_config({ENV_DB_PATH: "~/approvals.db"})
        assert "~" not in str(config.db_path)

    def test_blank_falls_back_to_default(self) -> None:
        assert load_config({ENV_DB_PATH: "   "}).db_path == default_db_path()


class TestTtl:
    def test_valid_ttl_is_parsed(self) -> None:
        assert load_config({ENV_DEFAULT_TTL_HOURS: "12"}).default_ttl_hours == 12

    def test_non_numeric_ttl_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="whole number of hours"):
            load_config({ENV_DEFAULT_TTL_HOURS: "soon"})

    @pytest.mark.parametrize("value", ["0", "-5", "99999"])
    def test_out_of_range_ttl_is_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="must be between"):
            load_config({ENV_DEFAULT_TTL_HOURS: value})


class TestSelfApproval:
    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_values(self, value: str) -> None:
        assert load_config({ENV_ALLOW_SELF_APPROVAL: value}).allow_self_approval

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
    def test_falsey_values(self, value: str) -> None:
        assert not load_config({ENV_ALLOW_SELF_APPROVAL: value}).allow_self_approval

    def test_nonsense_is_rejected_loudly(self) -> None:
        with pytest.raises(ValueError, match="must be one of"):
            load_config({ENV_ALLOW_SELF_APPROVAL: "sometimes"})
