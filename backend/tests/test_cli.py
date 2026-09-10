"""Operational CLI: argument parsing and exit codes.

Cron only sees the exit code, so the codes are the contract and are pinned here.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from app.cli import EXIT_MISCONFIGURED, EXIT_OK, build_parser, main


class TestParser:
    def test_every_documented_command_exists(self) -> None:
        parser = build_parser()
        for command in ("collect", "aggregate", "report", "status"):
            assert parser.parse_args([command]).command == command

    def test_collect_defaults_to_sending_alerts(self) -> None:
        assert build_parser().parse_args(["collect"]).no_alerts is False
        assert build_parser().parse_args(["collect", "--no-alerts"]).no_alerts is True

    def test_report_kind_is_constrained(self) -> None:
        assert build_parser().parse_args(["report"]).kind == "hourly"
        assert build_parser().parse_args(["report", "--kind", "daily"]).kind == "daily"
        with pytest.raises(SystemExit):
            build_parser().parse_args(["report", "--kind", "weekly"])

    def test_aggregate_parses_dates(self) -> None:
        args = build_parser().parse_args(
            ["aggregate", "--start", "2026-08-01", "--end", "2026-08-31"]
        )
        assert args.start.isoformat() == "2026-08-01"
        assert args.end.isoformat() == "2026-08-31"

    def test_a_command_is_required(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])


class TestExitCodes:
    def test_collect_without_a_provider_exits_misconfigured(self, monkeypatch) -> None:
        """Cron must be able to tell "no key" apart from "provider down"."""
        import app.cli as cli

        class NoProviders:
            """Stands in for a chain whose providers all lack credentials."""

            chain: ClassVar[list[str]] = ["aerodatabox"]

            def describe(self):
                return [{"name": "aerodatabox", "in_chain": True, "configured": False}]

            async def aclose(self):
                return None

        monkeypatch.setattr(cli, "ProviderRegistry", NoProviders)
        assert main(["collect"]) == EXIT_MISCONFIGURED

    def test_aggregate_rejects_an_inverted_range(self, monkeypatch) -> None:
        assert (
            main(["aggregate", "--start", "2026-09-30", "--end", "2026-09-01"])
            == EXIT_MISCONFIGURED
        )

    def test_exit_ok_is_zero(self) -> None:
        # cron treats any non-zero as failure; 0 must mean success.
        assert EXIT_OK == 0
