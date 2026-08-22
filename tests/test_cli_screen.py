from datetime import date
from types import SimpleNamespace

import pytest

from trading_system import cli
from trading_system.config import StorageConfig, load_settings
from trading_system.data.database import Database
from trading_system.models.screening import ScreenReport


class EmptyScreener:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def run(self, as_of: date) -> ScreenReport:
        return ScreenReport(
            as_of=as_of,
            generated_at="2025-01-02T00:00:00+00:00",
            analyzed_count=0,
            eligible_count=0,
            records=(),
        )


def test_cli_reports_missing_explicit_config_without_traceback(tmp_path, capsys) -> None:
    missing = tmp_path / "missing.yaml"

    result = cli.main(["--config", str(missing), "status"])

    assert result == 2
    assert "Configuration error" in capsys.readouterr().err


def test_screen_cli_exports_without_api_credentials(tmp_path, monkeypatch, capsys) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "test.sqlite3", reports_path=tmp_path / "reports"
            )
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    monkeypatch.setattr(cli, "Screener", EmptyScreener)

    result = cli.main(["screen", "--as-of", "2025-01-02"])

    output = capsys.readouterr().out
    assert result == 0
    assert "No stocks passed" in output
    assert (tmp_path / "reports" / "screen_2025-01-02.csv").exists()
    assert (tmp_path / "reports" / "screen_2025-01-02.json").exists()


def test_explain_cli_reports_unknown_local_symbol(tmp_path, monkeypatch, capsys) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "test.sqlite3", reports_path=tmp_path / "reports"
            )
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    monkeypatch.setattr(cli, "Screener", EmptyScreener)

    assert cli.main(["explain", "missing", "--as-of", "2025-01-02"]) == 1
    assert "MISSING" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command", ["backtest", "compare-strategies", "backtest-compare"]
)
def test_backtest_cli_rejects_invalid_period_clearly(
    tmp_path, monkeypatch, capsys, command
) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "backtest.sqlite3",
                reports_path=tmp_path / "reports",
            )
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )

    result = cli.main([command, "--start", "2025-02-01", "--end", "2025-01-01"])

    assert result == 1
    assert "start must not be after end" in capsys.readouterr().err


def test_research_local_only_flag_never_invokes_intraday_prefetch(
    tmp_path, monkeypatch
) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "research.sqlite3",
                reports_path=tmp_path / "reports",
            )
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    preparation = SimpleNamespace(
        intraday_requirements=(object(),),
        sessions=(date(2026, 5, 1), date(2026, 5, 4)),
        intraday_candidate_symbols=1,
    )
    monkeypatch.setattr(cli, "prepare_strategy_comparison", lambda *_args, **_kwargs: preparation)
    monkeypatch.setattr(cli, "_comparison_data_qualification", lambda *_args: {})
    monkeypatch.setattr(cli, "_qualification_metadata", lambda _reports: {})
    monkeypatch.setattr(cli, "_qualification_session_statuses", lambda *_args: {})
    monkeypatch.setattr(cli, "format_data_qualification_header", lambda _metadata: "")
    monkeypatch.setattr(
        cli,
        "comparison_intraday_prefetch_metadata",
        lambda *_args, **_kwargs: SimpleNamespace(required=True, enabled=False),
    )
    monkeypatch.setattr(
        cli,
        "prefetch_comparison_intraday_data",
        lambda *_args, **_kwargs: pytest.fail("local-only run attempted intraday prefetch"),
    )
    comparison = SimpleNamespace()
    monkeypatch.setattr(cli, "compare_strategies", lambda *_args, **_kwargs: comparison)
    monkeypatch.setattr(cli, "export_research_comparison", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "format_comparison_table", lambda _comparison: "research")

    result = cli.main(
        [
            "compare-strategies",
            "--start",
            "2026-05-01",
            "--end",
            "2026-05-04",
            "--include",
            "research-d1-d5",
            "--no-intraday-prefetch",
        ]
    )

    assert result == 0


def test_extended_validation_cli_never_constructs_a_synchronizer(
    tmp_path, monkeypatch
) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "validation.sqlite3",
                reports_path=tmp_path / "reports",
            )
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    monkeypatch.setattr(
        cli,
        "_synchronizer",
        lambda *_args, **_kwargs: pytest.fail("validation attempted network setup"),
    )
    bundle = SimpleNamespace()
    monkeypatch.setattr(cli, "run_extended_validation", lambda *_args: bundle)
    monkeypatch.setattr(cli, "export_extended_validation", lambda *_args: {})
    monkeypatch.setattr(
        cli, "format_extended_validation_summary", lambda _bundle: "validation"
    )

    assert cli.main(["validate-extended"]) == 0


class RoutedSynchronizer:
    def __init__(self) -> None:
        self.called: str | None = None

    def sync_full(self, _symbols):
        self.called = "full"
        return {"mode": "full"}

    def sync_sec_incremental(self, _symbols):
        self.called = "incremental"
        return {"mode": "incremental"}

    def sync_assets(self):
        self.called = "assets"
        return {"records_updated": 1}

    def sync_historical_bars(self, _symbols):
        self.called = "bars"
        return {"records_updated": 1}

    def sync_daily_history(
        self, symbols, start, end, *, incremental, include_benchmark
    ):
        self.called = "daily_history"
        return {
            "symbols": symbols,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "incremental": incremental,
            "include_benchmark": include_benchmark,
        }

    def refresh_market(self, _symbols):
        self.called = "market"
        return {"symbols_updated": 1}

    def sync_intraday(
        self, symbols, timeframes, start, end, *, incremental, extended_hours
    ):
        self.called = "intraday"
        return {
            "symbols": list(symbols),
            "timeframes": [str(item) for item in timeframes],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "incremental": incremental,
            "extended_hours": extended_hours,
        }


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["sync"], "full"),
        (["sync", "--full"], "full"),
        (["sync", "--incremental"], "incremental"),
        (["sync-assets"], "assets"),
        (["update-bars"], "bars"),
        (
            [
                "sync-daily-history",
                "--start",
                "2024-01-01",
                "--end",
                "2026-08-12",
                "--symbols",
                "AAPL,SPY",
            ],
            "daily_history",
        ),
        (["refresh-market"], "market"),
    ],
)
def test_data_cli_commands_route_to_independent_stages(
    tmp_path, monkeypatch, arguments, expected
) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / f"{expected}.sqlite3",
                reports_path=tmp_path / "reports",
            )
        }
    )
    routed = RoutedSynchronizer()
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    monkeypatch.setattr(cli, "_synchronizer", lambda *_args, **_kwargs: routed)

    assert cli.main(arguments) == 0
    assert routed.called == expected


def test_sync_intraday_cli_routes_explicit_symbols_and_multiple_timeframes(
    tmp_path, monkeypatch, capsys
) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "intraday.sqlite3",
                reports_path=tmp_path / "reports",
            )
        }
    )
    routed = RoutedSynchronizer()
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    monkeypatch.setattr(cli, "_synchronizer", lambda *_args, **_kwargs: routed)

    result = cli.main(
        [
            "sync-intraday",
            "--symbols",
            "nvda,AAPL,nvda",
            "--start",
            "2026-07-01",
            "--end",
            "2026-07-05",
            "--timeframes",
            "5m,15m,1h",
        ]
    )

    assert result == 0
    assert routed.called == "intraday"
    payload = capsys.readouterr().out
    assert '"symbols": [' in payload
    assert '"AAPL"' in payload and '"NVDA"' in payload
    assert '"timeframes": [' in payload
    assert '"5m"' in payload and '"15m"' in payload and '"1h"' in payload


def test_status_cli_reads_persisted_freshness_without_credentials(
    tmp_path, monkeypatch, capsys
) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    database_path = tmp_path / "status.sqlite3"
    database = Database(database_path)
    database.initialize()
    database.set_sync_value(
        "dataset",
        "asset_universe",
        {
            "status": "success",
            "last_success_at": "2026-08-13T06:00:00+00:00",
            "assets_received": 13364,
            "assets_upserted": 13364,
            "assets_deactivated": 66,
            "tradable_assets_after": 13364,
        },
    )
    database.set_sync_value(
        "dataset",
        "sec",
        {
            "status": "success",
            "last_success_at": "2026-08-13T06:12:00+00:00",
            "mode": "incremental",
            "companies_updated": 47,
        },
    )
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(database_path=database_path, reports_path=tmp_path / "reports")
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )

    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "TraWalp data status" in output
    assert "SEC fundamentals" in output
    assert "assets received: 13364" in output
    assert "assets deactivated: 66" in output
    assert "tradable assets after: 13364" in output
    assert "incremental" in output
    assert "companies updated: 47" in output


def test_storage_report_and_cleanup_dry_run_need_no_credentials(
    tmp_path, monkeypatch, capsys
) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    database_path = tmp_path / "storage.sqlite3"
    database = Database(database_path)
    database.initialize()
    database.cache_sec_payload("0000001234", "companyfacts", {"source": "copy"})
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(database_path=database_path, reports_path=tmp_path / "reports")
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )

    assert cli.main(["storage-report"]) == 0
    assert "TraWalp database storage report" in capsys.readouterr().out
    assert cli.main(["db-cleanup", "--dry-run", "--vacuum"]) == 0
    output = capsys.readouterr().out
    assert "No changes made (--dry-run)" in output
    assert "VACUUM not run" in output
    assert database.has_cached_sec_payload("0000001234", "companyfacts")
