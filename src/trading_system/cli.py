"""Command line interface for synchronization and local point-in-time screening."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from alpaca.data.enums import Adjustment, DataFeed

from trading_system.ai.export import NoAICandidatesError, export_ai_candidates
from trading_system.backtest.candidate_audit import run_candidate_audit
from trading_system.backtest.engine import (
    BacktestEngine,
    StrategyComparisonPreparation,
    assess_comparison_intraday_coverage,
    compare_position_management,
    compare_strategies,
    comparison_intraday_prefetch_metadata,
    prefetch_comparison_intraday_data,
    prepare_strategy_comparison,
)
from trading_system.backtest.intraday_isolation import (
    annotate_intraday_isolation_coverage,
    export_intraday_isolation_comparison,
)
from trading_system.backtest.report import (
    export_backtest,
    export_candidate_audit,
    export_comparison,
    export_data_qualification,
    export_research_comparison,
    format_backtest_summary,
    format_candidate_audit_summary,
    format_comparison_table,
    format_data_qualification_header,
)
from trading_system.backtest.validation import (
    export_extended_validation,
    format_extended_validation_summary,
    run_extended_validation,
)
from trading_system.config import StrategyConfig, load_settings
from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.daily_history import warmup_coverage_at
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    effective_trading_session,
    intraday_warmup_start,
    required_daily_warmup_sessions,
    trading_sessions_between,
)
from trading_system.data.qualification import (
    DataQualificationReport,
    qualify_daily_history,
    qualify_intraday_history,
)
from trading_system.data.sec_client import SecClient
from trading_system.data.sync import DataSynchronizer
from trading_system.fundamentals.debug import debug_fundamentals
from trading_system.models.backtest import (
    PositionManagementPreset,
    StrategyComparisonKind,
    StrategyVariant,
)
from trading_system.models.market_data import BarTimeframe
from trading_system.strategy.reporting import (
    export_report,
    format_explanation,
    format_fundamental_debug,
    format_market_debug,
    format_peer_debug,
    format_screen_table,
)
from trading_system.strategy.screener import Screener


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-system")
    parser.add_argument(
        "--config",
        default=None,
        type=Path,
        help="Explicit strategy YAML (default: repository config/strategy.yaml)",
    )
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Synchronize SEC data or run a complete refresh")
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="Run the complete legacy sync pipeline")
    mode.add_argument(
        "--incremental",
        action="store_true",
        help="Check submissions and reload Company Facts only for changed companies",
    )
    sync.add_argument(
        "--symbols",
        nargs="*",
        help="Optional symbol subset. Omit to synchronize all tradable US equities.",
    )
    sync_assets = commands.add_parser("sync-assets", help="Refresh only the Alpaca asset universe")
    sync_assets.add_argument("--symbols", nargs="*", help=argparse.SUPPRESS)
    update_bars = commands.add_parser(
        "update-bars", help="Incrementally update completed historical daily bars"
    )
    update_bars.add_argument("--symbols", nargs="*")
    daily_history = commands.add_parser(
        "sync-daily-history",
        help="Backfill an inclusive adjusted Daily range with backward and forward gaps",
    )
    daily_history.add_argument("--start", type=date.fromisoformat, required=True)
    daily_history.add_argument("--end", type=date.fromisoformat, required=True)
    daily_history.add_argument(
        "--symbols",
        help="Optional comma-separated symbols; omit for the current tradable company universe",
    )
    daily_history.add_argument(
        "--full-window",
        action="store_true",
        help="Force the complete provider range instead of incremental edge gaps",
    )
    intraday = commands.add_parser(
        "sync-intraday", help="Backfill or incrementally update provider-native intraday bars"
    )
    intraday.add_argument("--start", type=date.fromisoformat, required=True)
    intraday.add_argument("--end", type=date.fromisoformat, required=True)
    intraday.add_argument(
        "--timeframes",
        required=True,
        help="Comma-separated provider timeframes: 5m,15m,1h",
    )
    scope = intraday.add_mutually_exclusive_group(required=True)
    scope.add_argument("--symbols", help="Comma-separated explicit symbol list")
    scope.add_argument(
        "--universe",
        choices=("all",),
        help="Explicitly allow the complete synchronized tradable company universe",
    )
    scope.add_argument(
        "--candidates-report",
        type=Path,
        help="Read required symbols from an existing JSON screen/backtest report",
    )
    intraday.add_argument(
        "--extended-hours",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Include provider pre-/after-market bars (default from config)",
    )
    intraday.add_argument(
        "--full-window",
        action="store_true",
        help="Request the complete interval instead of incremental local overlap ranges",
    )
    refresh_market = commands.add_parser(
        "refresh-market", help="Refresh batched current snapshots without downloading history"
    )
    refresh_market.add_argument("--symbols", nargs="*")
    commands.add_parser("status", help="Show local dataset freshness and bar inventory")
    commands.add_parser("data-status", help="Alias for status")
    warmup_coverage = commands.add_parser(
        "daily-history-coverage",
        help="Measure prior-session Daily warmup for the current tradable company universe",
    )
    warmup_coverage.add_argument("--as-of", type=date.fromisoformat, required=True)
    commands.add_parser("storage-report", help="Inspect SQLite allocation and table usage")
    cleanup = commands.add_parser(
        "db-cleanup", help="Explicitly remove guarded legacy SEC Company Facts payloads"
    )
    cleanup.add_argument("--dry-run", action="store_true", help="Report without deleting rows")
    cleanup.add_argument(
        "--vacuum",
        action="store_true",
        help="After cleanup, compact SQLite if conservative free-space checks pass",
    )
    screen = commands.add_parser("screen", help="Run the local point-in-time daily screen")
    screen.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    screen.add_argument("--limit", type=int, default=None)
    backtest = commands.add_parser("backtest", help="Run a point-in-time simulated portfolio")
    backtest.add_argument("--start", type=date.fromisoformat, required=True)
    backtest.add_argument("--end", type=date.fromisoformat, required=True)
    backtest.add_argument(
        "--variant",
        choices=[variant.value for variant in StrategyVariant],
        default=StrategyVariant.FULL.value,
    )
    backtest.add_argument(
        "--strategy",
        choices=[preset.value for preset in PositionManagementPreset],
        default=PositionManagementPreset.CONFIGURED.value,
        help="Position-management preset (configured uses position_management from YAML)",
    )
    comparison = commands.add_parser(
        "compare-strategies",
        help="Compare score variants and position-management strategies on shared screens",
    )
    comparison.add_argument("--start", type=date.fromisoformat, required=True)
    comparison.add_argument("--end", type=date.fromisoformat, required=True)
    comparison.add_argument(
        "--include",
        choices=(
            "all",
            "score-variants",
            "position-management",
            "research-d1-d5",
            "research-intraday-isolation",
        ),
        default="all",
        help="Select all strategies or one comparison family (default: all)",
    )
    comparison.add_argument(
        "--no-intraday-prefetch",
        action="store_true",
        help="Use only local intraday data and skip runs whose data is missing",
    )
    comparison.add_argument(
        "--output-stem",
        help="Write comparison and qualification reports under a new non-existing stem",
    )
    comparison.add_argument(
        "--strict-intraday-coverage",
        action="store_true",
        help="Research-only sensitivity: admit only COMPLETE native intraday symbol-sessions",
    )
    validation = commands.add_parser(
        "validate-extended",
        help="Run the frozen local-only D1/C and C/intraday extended OOS validation",
    )
    validation.add_argument(
        "--start", type=date.fromisoformat, default=date(2025, 5, 1)
    )
    validation.add_argument(
        "--end", type=date.fromisoformat, default=date(2026, 4, 30)
    )
    validation.add_argument(
        "--reference-start", type=date.fromisoformat, default=date(2026, 5, 1)
    )
    validation.add_argument(
        "--reference-end", type=date.fromisoformat, default=date(2026, 8, 12)
    )
    position_comparison = commands.add_parser(
        "backtest-compare", help="Compare the daily position-management presets"
    )
    position_comparison.add_argument("--start", type=date.fromisoformat, required=True)
    position_comparison.add_argument("--end", type=date.fromisoformat, required=True)
    audit = commands.add_parser(
        "audit-candidates",
        help="Audit the production historical candidate funnel and PIT data coverage",
    )
    audit.add_argument("--start", type=date.fromisoformat, required=True)
    audit.add_argument("--end", type=date.fromisoformat, required=True)
    audit.add_argument(
        "--variant",
        choices=[variant.value for variant in StrategyVariant],
        default=StrategyVariant.FULL.value,
        help="Use the configured backtest entry funnel for this score variant (default: C)",
    )
    audit.add_argument(
        "--near-miss-limit",
        type=_positive_int,
        default=10,
        help="Maximum retained near misses per screening session",
    )
    audit.add_argument(
        "--group-by",
        choices=("month",),
        default="month",
        help="Compact terminal grouping; full session records are always exported",
    )
    export_ai = commands.add_parser(
        "export-ai", help="Export ranked screen candidates for manual AI analysis"
    )
    export_ai.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    export_ai.add_argument("--limit", type=_positive_int, default=20)
    export_ai.add_argument("--output", type=Path)
    explain = commands.add_parser("explain", help="Explain one symbol's current screen result")
    explain.add_argument("symbol")
    explain.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    for name, help_text in (
        ("debug-peers", "Inspect SIC peer fallback and valid multiple counts"),
        ("debug-fundamentals", "Inspect XBRL sources and derivation formulas"),
        ("debug-market", "Inspect completed daily bars and technical inputs"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("symbol")
        command.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging(args.verbose)
    try:
        settings = load_settings(args.config)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    database = Database(settings.strategy.storage.database_path)
    if args.command == "storage-report":
        try:
            print(_format_storage_report(database.storage_report()))
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.command == "db-cleanup":
        print(
            "Analyzing legacy SEC Company Facts cache; exact payload sizes may take time...",
            flush=True,
        )
        try:
            result = database.cleanup_raw_sec_cache(dry_run=args.dry_run)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(_format_cleanup_report(result))
        if args.vacuum:
            if args.dry_run:
                print("\nVACUUM not run during --dry-run.")
            else:
                requirements = database.vacuum_requirements()
                print(_format_vacuum_requirements(requirements), flush=True)
                try:
                    database.vacuum()
                except RuntimeError as exc:
                    print(f"\nVACUUM refused: {exc}", file=sys.stderr)
                    return 1
                print("\nVACUUM completed.")
        return 0
    database.initialize()
    if args.command in {
        "sync",
        "sync-assets",
        "update-bars",
        "sync-daily-history",
        "refresh-market",
        "sync-intraday",
    }:
        if args.command == "sync-intraday":
            if args.start > args.end:
                print("Intraday sync refused: --start must not be after --end", file=sys.stderr)
                return 2
            try:
                timeframes = _parse_intraday_timeframes(args.timeframes)
                requested = _intraday_symbols(args, database)
            except ValueError as exc:
                print(f"Intraday sync refused: {exc}", file=sys.stderr)
                return 2
            synchronizer = _synchronizer(
                settings, database, with_alpaca=True, with_sec=False
            )
            extended_hours = (
                settings.strategy.intraday.extended_hours
                if args.extended_hours is None
                else args.extended_hours
            )
            requested_start = min(
                intraday_warmup_start(
                    args.start,
                    timeframe,
                    settings.strategy.intraday.warmup_bars,
                    extended_hours=extended_hours,
                )
                for timeframe in timeframes
            )
            result = synchronizer.sync_intraday(
                requested,
                timeframes,
                requested_start,
                datetime.combine(args.end + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
                incremental=not args.full_window,
                extended_hours=extended_hours,
            )
            result["requested_backtest_start"] = args.start.isoformat()
            result["warmup_start"] = requested_start.isoformat()
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "sync-daily-history":
            if args.start > args.end:
                print(
                    "Daily-history sync refused: --start must not be after --end",
                    file=sys.stderr,
                )
                return 2
            requested = (
                sorted(
                    {
                        symbol.strip().upper()
                        for symbol in args.symbols.split(",")
                        if symbol.strip()
                    }
                )
                if args.symbols
                else None
            )
            synchronizer = _synchronizer(
                settings, database, with_alpaca=True, with_sec=False
            )
            result = synchronizer.sync_daily_history(
                requested,
                args.start,
                args.end,
                incremental=not args.full_window,
                include_benchmark=True,
            )
            result["required_daily_warmup_sessions"] = required_daily_warmup_sessions(
                settings.strategy
            )
            print(json.dumps(result, indent=2))
            return 0
        requested = [symbol.upper() for symbol in args.symbols] if args.symbols else None
        needs_alpaca = args.command != "sync" or not args.incremental
        synchronizer = _synchronizer(
            settings,
            database,
            with_alpaca=needs_alpaca,
            with_sec=args.command == "sync",
        )
        if args.command == "sync":
            # No flag intentionally retains the historical complete-sync behavior.
            result = (
                synchronizer.sync_sec_incremental(requested)
                if args.incremental
                else synchronizer.sync_full(requested)
            )
        elif args.command == "sync-assets":
            result = synchronizer.sync_assets()
        elif args.command == "update-bars":
            result = synchronizer.sync_historical_bars(requested)
        else:
            result = synchronizer.refresh_market(requested)
        print(json.dumps(result, indent=2))
        return 0
    if args.command in {"status", "data-status"}:
        print(
            _format_data_status(
                database.dataset_states(),
                database.bar_inventory(),
                spy_bounds=database.bar_date_bounds("SPY"),
            )
        )
        return 0
    if args.command == "daily-history-coverage":
        symbols = [company.symbol for company in database.list_tradable_companies()]
        report = warmup_coverage_at(
            database,
            symbols,
            args.as_of,
            required_daily_warmup_sessions(settings.strategy),
        )
        print(json.dumps(report, indent=2))
        return 0
    if args.command == "screen":
        _warn_data_freshness(database.dataset_states())
        report = Screener(database, settings.strategy).run(args.as_of)
        csv_path, json_path = export_report(report, settings.strategy.storage.reports_path)
        print(format_screen_table(report, limit=args.limit))
        print(
            f"\nRequested as-of: {report.requested_as_of}"
            f" | Effective completed session: {report.effective_market_session}"
            f"\nAnalyzed: {report.analyzed_count} | Eligible: {report.eligible_count}"
            f" | Identity conflicts excluded: {report.identity_conflicts_excluded}"
            f"\nCSV: {csv_path}\nJSON: {json_path}"
        )
        return 0
    if args.command == "backtest":
        try:
            result = BacktestEngine(database, settings.strategy).run(
                args.start,
                args.end,
                variant=StrategyVariant(args.variant),
                preset=PositionManagementPreset(args.strategy),
            )
        except ValueError as exc:
            print(f"Backtest refused: {exc}", file=sys.stderr)
            return 1
        paths = export_backtest(result, settings.strategy.storage.reports_path)
        print(format_backtest_summary(result))
        print("\n" + "\n".join(f"{name}: {path}" for name, path in paths.items()))
        return 0
    if args.command == "audit-candidates":
        try:
            audit_result = run_candidate_audit(
                database,
                settings.strategy,
                args.start,
                args.end,
                variant=StrategyVariant(args.variant),
                near_miss_limit=args.near_miss_limit,
            )
        except ValueError as exc:
            print(f"Candidate audit refused: {exc}", file=sys.stderr)
            return 1
        paths = export_candidate_audit(
            audit_result, settings.strategy.storage.reports_path
        )
        print(format_candidate_audit_summary(audit_result))
        print("\n" + "\n".join(f"{name}: {path}" for name, path in paths.items()))
        return 0
    if args.command == "backtest-compare":
        try:
            comparison_result = compare_position_management(
                database, settings.strategy, args.start, args.end
            )
        except ValueError as exc:
            print(f"Position-management comparison refused: {exc}", file=sys.stderr)
            return 1
        paths = export_comparison(comparison_result, settings.strategy.storage.reports_path)
        print(format_comparison_table(comparison_result))
        print("\n" + "\n".join(f"{name}: {path}" for name, path in paths.items()))
        return 0
    if args.command == "validate-extended":
        try:
            print("Qualifying frozen OOS data and discovering PIT candidates...", flush=True)
            bundle = run_extended_validation(
                database,
                settings.strategy,
                args.start,
                args.end,
                args.reference_start,
                args.reference_end,
            )
            paths = export_extended_validation(
                bundle, settings.strategy.storage.reports_path
            )
        except (FileExistsError, ValueError) as exc:
            print(f"Extended validation refused: {exc}", file=sys.stderr)
            return 1
        print(format_extended_validation_summary(bundle))
        print("\n" + "\n".join(f"{name}: {path}" for name, path in paths.items()))
        return 0
    if args.command == "compare-strategies":
        try:
            comparison_kind = StrategyComparisonKind(args.include.replace("-", "_"))
            if (
                args.strict_intraday_coverage
                and comparison_kind is not StrategyComparisonKind.RESEARCH_D1_D5
            ):
                raise ValueError(
                    "--strict-intraday-coverage is available only with research-d1-d5"
                )
            print("Preparing strategy comparison...", flush=True)
            preparation = prepare_strategy_comparison(
                database,
                settings.strategy,
                args.start,
                args.end,
                comparison_kind=comparison_kind,
            )
            qualification_reports = _comparison_data_qualification(
                database,
                settings.strategy,
                preparation,
            )
            qualification_metadata = _qualification_metadata(qualification_reports)
            intraday_session_statuses = _qualification_session_statuses(
                qualification_reports, preparation
            )
            print("\n" + format_data_qualification_header(qualification_metadata))
            print(
                "\nShared PIT screens:"
                f"\n  Sessions: {len(preparation.sessions) - 1}"
                f"\n  Intraday candidate symbols: "
                f"{preparation.intraday_candidate_symbols}"
            )
            if not preparation.intraday_requirements:
                print("\nIntraday prefetch: not required")
                prefetch = comparison_intraday_prefetch_metadata(
                    preparation, enabled=not args.no_intraday_prefetch
                )
            elif args.no_intraday_prefetch:
                print("\nIntraday prefetch: disabled (--no-intraday-prefetch)")
                prefetch = comparison_intraday_prefetch_metadata(
                    preparation, enabled=False
                )
            else:
                assessments = assess_comparison_intraday_coverage(
                    database, preparation.intraday_requirements
                )
                print("\nIntraday requirements:")
                for assessment in assessments:
                    requirement = assessment.requirement
                    print(
                        f"  {requirement.timeframe.value}:"
                        f"\n    Candidates: {len(requirement.symbols)}"
                        f"\n    Local complete: {len(assessment.complete_symbols)}"
                        f"\n    Sync required: {len(assessment.sync_symbols)}"
                        f"\n    Warmup bars: {requirement.warmup_bars}"
                        f"\n    Extended hours: "
                        f"{str(requirement.extended_hours).lower()}"
                    )
                sync_count = sum(len(item.sync_symbols) for item in assessments)
                if sync_count:
                    requested_timeframes = ", ".join(
                        item.requirement.timeframe.value
                        for item in assessments
                        if item.sync_symbols
                    )
                    print(
                        f"\nSynchronizing missing {requested_timeframes} history...",
                        flush=True,
                    )
                else:
                    print("\nIntraday prefetch: local coverage complete")
                    print("  No download required")
                prefetch = prefetch_comparison_intraday_data(
                    database,
                    settings.strategy,
                    preparation,
                    assessments,
                    lambda: _synchronizer(
                        settings, database, with_alpaca=True, with_sec=False
                    ),
                )
                if sync_count:
                    synchronized_symbols = sum(
                        item.sync_requested_symbols
                        for item in prefetch.timeframes.values()
                    )
                    print(
                        f"  Symbols requested: {synchronized_symbols}"
                        "\n  Bars added: "
                        f"{sum(item.bars_added for item in prefetch.timeframes.values())}"
                    )
            print("\nRunning strategy comparison...", flush=True)
            comparison_result = compare_strategies(
                database,
                settings.strategy,
                args.start,
                args.end,
                comparison_kind=comparison_kind,
                preparation=preparation,
                intraday_prefetch=prefetch,
                data_qualification=qualification_metadata,
                strict_coverage_sensitivity=args.strict_intraday_coverage,
                intraday_session_statuses=intraday_session_statuses,
                allow_missing_intraday_data=(
                    comparison_kind
                    is StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION
                ),
            )
            strict_comparison = None
            cost_comparisons: dict[str, object] = {}
            isolation_coverage_rows: list[dict] = []
            if (
                comparison_kind is StrategyComparisonKind.RESEARCH_D1_D5
                and not args.strict_intraday_coverage
            ):
                if (
                    settings.strategy.backtest.slippage_bps != 5
                    or settings.strategy.backtest.commission_bps != 0
                ):
                    raise ValueError(
                        "research-d1-d5 requires the frozen 5 bps / 0 bps baseline"
                    )
                print("\nRunning strict intraday coverage sensitivity...", flush=True)
                strict_comparison = compare_strategies(
                    database,
                    settings.strategy,
                    args.start,
                    args.end,
                    comparison_kind=comparison_kind,
                    preparation=preparation,
                    intraday_prefetch=prefetch,
                    data_qualification=qualification_metadata,
                    strict_coverage_sensitivity=True,
                    intraday_session_statuses=intraday_session_statuses,
                )
                cost_comparisons = {"BASELINE": comparison_result}
                for name, slippage_bps, commission_bps in (
                    ("2X_SLIPPAGE", 10, 0),
                    ("3X_SLIPPAGE", 15, 0),
                    ("COMMISSION_SENSITIVITY", 5, 5),
                ):
                    print(f"Running cost stress: {name}...", flush=True)
                    cost_config = settings.strategy.model_copy(
                        update={
                            "backtest": settings.strategy.backtest.model_copy(
                                update={
                                    "slippage_bps": slippage_bps,
                                    "commission_bps": commission_bps,
                                }
                            )
                        }
                    )
                    cost_comparisons[name] = compare_strategies(
                        database,
                        cost_config,
                        args.start,
                        args.end,
                        comparison_kind=comparison_kind,
                        preparation=preparation,
                        intraday_prefetch=prefetch,
                        data_qualification=qualification_metadata,
                        intraday_session_statuses=intraday_session_statuses,
                    )
            elif comparison_kind is StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION:
                if (
                    settings.strategy.backtest.slippage_bps != 5
                    or settings.strategy.backtest.commission_bps != 0
                ):
                    raise ValueError(
                        "research-intraday-isolation requires the frozen 5 bps / 0 bps baseline"
                    )
                comparison_result, isolation_coverage_rows = (
                    annotate_intraday_isolation_coverage(database, comparison_result)
                )
                cost_comparisons = {"BASE": comparison_result}
                for name, slippage_bps, commission_bps in (
                    ("2X", 10, 0),
                    ("3X", 15, 0),
                    ("COMMISSION", 5, 5),
                ):
                    print(f"Running cost stress: {name}...", flush=True)
                    cost_config = settings.strategy.model_copy(
                        update={
                            "backtest": settings.strategy.backtest.model_copy(
                                update={
                                    "slippage_bps": slippage_bps,
                                    "commission_bps": commission_bps,
                                }
                            )
                        }
                    )
                    cost_comparisons[name] = compare_strategies(
                        database,
                        cost_config,
                        args.start,
                        args.end,
                        comparison_kind=comparison_kind,
                        preparation=preparation,
                        intraday_prefetch=prefetch,
                        data_qualification=qualification_metadata,
                        intraday_session_statuses=intraday_session_statuses,
                        allow_missing_intraday_data=True,
                    )
        except ValueError as exc:
            print(f"Strategy comparison refused: {exc}", file=sys.stderr)
            return 1
        try:
            if strict_comparison is not None:
                research_stem = args.output_stem or (
                    f"d1_d5_research_{args.start}_{args.end}"
                )
                paths = export_research_comparison(
                    comparison_result,
                    strict_comparison,
                    cost_comparisons,
                    settings.strategy.storage.reports_path,
                    stem=research_stem,
                )
            elif comparison_kind is StrategyComparisonKind.RESEARCH_INTRADAY_ISOLATION:
                isolation_stem = args.output_stem or (
                    f"intraday_isolation_{args.start}_{args.end}"
                )
                paths = export_intraday_isolation_comparison(
                    comparison_result,
                    cost_comparisons,
                    isolation_coverage_rows,
                    settings.strategy.storage.reports_path,
                    stem=isolation_stem,
                )
            else:
                paths = export_comparison(
                    comparison_result,
                    settings.strategy.storage.reports_path,
                    stem=args.output_stem,
                    overwrite=args.output_stem is None,
                )
            if args.output_stem:
                paths.update(
                    export_data_qualification(
                        qualification_reports,
                        settings.strategy.storage.reports_path,
                        stem=args.output_stem,
                    )
                )
        except FileExistsError as exc:
            print(f"Strategy comparison export refused: {exc}", file=sys.stderr)
            return 1
        print(format_comparison_table(comparison_result))
        print("\n" + "\n".join(f"{name}: {path}" for name, path in paths.items()))
        return 0
    if args.command == "export-ai":
        report = Screener(database, settings.strategy).run(args.as_of)
        try:
            result = export_ai_candidates(report, limit=args.limit, output_path=args.output)
        except NoAICandidatesError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        top = result.payload.candidates[0]
        print(
            "AI candidate export complete"
            f"\nCandidates: {result.payload.candidate_count}"
            f"\nFile: {result.path}"
            f"\nTop quant candidate: {top.symbol} ({top.quant_score:.1f})"
            "\n\nUpload this JSON file to ChatGPT for AI ranking."
        )
        return 0
    if args.command == "explain":
        symbol = args.symbol.upper()
        report = Screener(database, settings.strategy).run(args.as_of)
        record = next((record for record in report.records if record.symbol == symbol), None)
        if record is None:
            print(
                f"Symbol {symbol} is not available in the synchronized universe.", file=sys.stderr
            )
            return 1
        print(format_explanation(record))
        return 0
    if args.command == "debug-peers":
        debug = Screener(database, settings.strategy).debug_peers(args.symbol, args.as_of)
        if debug is None:
            print(f"Symbol {args.symbol.upper()} is not available locally.", file=sys.stderr)
            return 1
        print(format_peer_debug(debug))
        return 0
    if args.command == "debug-fundamentals":
        symbol = args.symbol.upper()
        session = effective_trading_session(args.as_of)
        facts = database.facts_available_as_of(symbol, session)
        if not facts:
            print(f"No point-in-time fundamentals available for {symbol}.", file=sys.stderr)
            return 1
        print(format_fundamental_debug(debug_fundamentals(symbol, facts, session)))
        return 0
    if args.command == "debug-market":
        debug = Screener(database, settings.strategy).debug_market(args.symbol, args.as_of)
        if debug.bar_count == 0:
            print(f"No completed daily bars available for {args.symbol.upper()}.", file=sys.stderr)
            return 1
        print(format_market_debug(debug))
        return 0
    return 2


def _synchronizer(
    settings,
    database: Database,
    *,
    with_alpaca: bool,
    with_sec: bool,
) -> DataSynchronizer:
    universe = settings.strategy.universe
    alpaca = None
    if with_alpaca:
        key, secret = settings.require_alpaca_credentials()
        alpaca = AlpacaDataClient(
            key,
            secret,
            feed=DataFeed(universe.market_data_feed),
            adjustment=Adjustment(universe.market_data_adjustment),
        )
    sec = None
    companyfacts_unavailable_ttl = timedelta(days=7)
    if with_sec:
        sec_config = settings.strategy.sec
        sec = SecClient(
            settings.require_sec_user_agent(),
            request_interval_seconds=sec_config.request_interval_seconds,
            timeout_seconds=sec_config.timeout_seconds,
            max_retries=sec_config.max_retries,
        )
        companyfacts_unavailable_ttl = timedelta(days=sec_config.companyfacts_unavailable_ttl_days)
    return DataSynchronizer(
        database,
        alpaca,
        sec,
        market_data_days=universe.market_data_days,
        exclude_financials=universe.exclude_financials,
        exclude_reits=universe.exclude_reits,
        companyfacts_unavailable_ttl=companyfacts_unavailable_ttl,
        intraday_enabled=settings.strategy.intraday.enabled,
        intraday_timeframes=settings.strategy.intraday.timeframes,
        intraday_extended_hours=settings.strategy.intraday.extended_hours,
        intraday_incremental=settings.strategy.intraday.sync.incremental,
        intraday_overlap_bars=settings.strategy.intraday.sync.overlap_bars,
        intraday_symbol_batch_size=settings.strategy.intraday.sync.symbol_batch_size,
        intraday_request_window_days=settings.strategy.intraday.sync.request_window_days,
    )


def _parse_intraday_timeframes(raw: str) -> tuple[BarTimeframe, ...]:
    try:
        parsed = tuple(
            dict.fromkeys(
                BarTimeframe(item.strip().lower())
                for item in raw.split(",")
                if item.strip()
            )
        )
    except ValueError as exc:
        raise ValueError("--timeframes accepts only 5m,15m,1h") from exc
    if not parsed or any(not item.intraday for item in parsed):
        raise ValueError("--timeframes accepts only 5m,15m,1h")
    return parsed


def _intraday_symbols(args, database: Database) -> list[str]:
    if args.symbols:
        symbols = sorted(
            {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
        )
    elif args.universe == "all":
        symbols = [company.symbol for company in database.list_tradable_companies()]
    else:
        payload = json.loads(args.candidates_report.read_text(encoding="utf-8"))
        symbols = sorted(_symbols_in_json(payload))
    if not symbols:
        raise ValueError("symbol selection is empty")
    return symbols


def _symbols_in_json(value) -> set[str]:
    output: set[str] = set()
    if isinstance(value, dict):
        symbol = value.get("symbol")
        if isinstance(symbol, str) and symbol.strip():
            output.add(symbol.strip().upper())
        for nested in value.values():
            output.update(_symbols_in_json(nested))
    elif isinstance(value, list):
        for nested in value:
            output.update(_symbols_in_json(nested))
    return output


def _format_data_status(
    states: dict[str, dict],
    bar_inventory: list[dict] | None = None,
    *,
    spy_bounds: tuple[date | None, date | None] | None = None,
) -> str:
    labels = {
        "asset_universe": "Asset universe",
        "sec": "SEC fundamentals",
        "historical_bars": "Historical bars",
        "daily_history": "Historical Daily backfill",
        "intraday_bars": "Intraday bars",
        "market_snapshot": "Market snapshot",
    }
    lines = ["TraWalp data status"]
    for dataset, label in labels.items():
        state = states.get(dataset, {})
        lines.extend(
            [
                "",
                label,
                f"  status:       {state.get('status', 'never synchronized')}",
                f"  last success: {state.get('last_success_at') or 'N/A'}",
            ]
        )
        for key in (
            "mode",
            "records_updated",
            "assets_received",
            "assets_upserted",
            "assets_deactivated",
            "tradable_assets_after",
            "universe_symbols",
            "sec_mapped_symbols",
            "sec_mapped_ciks",
            "sec_ticker_alias_symbols",
            "sec_unmapped_symbols",
            "companies_checked",
            "companies_updated",
            "facts_processed",
            "change_candidates",
            "companyfacts_unavailable",
            "submissions_unavailable",
            "identity_conflicts",
            "identity_conflicts_skipped",
            "identity_conflict_sample",
            "negative_cache_hits",
            "missing_cik_mappings",
            "unmapped_etf_or_fund",
            "unmapped_warrant",
            "unmapped_unit",
            "unmapped_rights",
            "unmapped_preferred",
            "unmapped_depositary_or_foreign",
            "unmapped_otc_exchange",
            "unmapped_unclassified",
            "sec_requests_total",
            "ticker_map_requests",
            "change_detection_requests",
            "submissions_requests",
            "companyfacts_requests",
            "change_detection_seconds",
            "submissions_seconds",
            "companyfacts_seconds",
            "parse_and_persist_seconds",
            "request_failures",
            "rate_limit_failures",
            "server_failures",
            "timeout_failures",
            "connection_failures",
            "json_failures",
            "parse_failures",
            "database_failures",
            "other_failures",
            "symbols_requested",
            "symbols_updated",
            "symbols_with_data",
            "symbols_without_data",
            "symbols_without_older_data",
            "bars_before",
            "bars_after",
            "bars_received",
            "bars_inserted",
            "bars_updated",
            "errors",
            "elapsed_seconds",
        ):
            if key in state:
                lines.append(f"  {key.replace('_', ' ')}: {state[key]}")
    lines.extend(["", "Bar inventory", "  Timeframe  Symbols          Bars  First / Last"])
    for item in bar_inventory or []:
        lines.append(
            f"  {item['timeframe']:<9} {item['symbols']:>7,} {item['bars']:>13,}  "
            f"{item['first_timestamp']} / {item['last_timestamp']}"
        )
    if not bar_inventory:
        lines.append("  (empty)")
    if spy_bounds is not None:
        spy_first, spy_last = spy_bounds
        lines.extend(
            [
                "",
                "SPY Daily benchmark",
                f"  first: {spy_first.isoformat() if spy_first else 'N/A'}",
                f"  last:  {spy_last.isoformat() if spy_last else 'N/A'}",
            ]
        )
    return "\n".join(lines)


def _comparison_data_qualification(
    database: Database,
    config: StrategyConfig,
    preparation: StrategyComparisonPreparation,
) -> dict[str, DataQualificationReport]:
    symbols = sorted(
        {company.symbol for company in database.list_tradable_companies()} | {"SPY"}
    )
    reports = {
        "daily": qualify_daily_history(
            database,
            symbols,
            preparation.requested_start,
            preparation.requested_end,
            warmup_sessions=required_daily_warmup_sessions(config),
        )
    }
    for requirement in preparation.intraday_requirements:
        reports[f"intraday_{requirement.timeframe.value}"] = qualify_intraday_history(
            database,
            requirement.symbols,
            requirement.requested_start.date(),
            preparation.requested_end,
            requirement.timeframe,
            detail_limit=max(
                1_000,
                len(requirement.symbols)
                * len(
                    trading_sessions_between(
                        requirement.requested_start.date(), preparation.requested_end
                    )
                ),
            ),
        )
    return reports


def _qualification_metadata(
    reports: dict[str, DataQualificationReport],
) -> dict[str, dict]:
    daily = reports["daily"].model_dump(mode="json", exclude={"details"})
    intraday = {
        report.timeframe.value: report.model_dump(mode="json", exclude={"details"})
        for key, report in reports.items()
        if key.startswith("intraday_")
    }
    return {"daily": daily, "intraday": intraday}


def _qualification_session_statuses(
    reports: dict[str, DataQualificationReport],
    preparation: StrategyComparisonPreparation,
) -> dict[tuple[str, date], str]:
    statuses: dict[tuple[str, date], str] = {}
    for requirement in preparation.intraday_requirements:
        report = reports[f"intraday_{requirement.timeframe.value}"]
        for symbol in requirement.symbols:
            for session in requirement.comparison_sessions:
                statuses[(symbol, session)] = "COMPLETE"
        for detail in report.details:
            if detail.session in requirement.comparison_sessions:
                statuses[(detail.symbol, detail.session)] = detail.status.value
    return statuses


def _format_storage_report(report: dict) -> str:
    lines = [
        "TraWalp database storage report",
        "",
        f"Database:              {report['database_path']}",
        f"File size:             {_format_bytes(report['file_bytes'])}",
        f"SQLite page size:      {report['page_size']:,} bytes",
        f"Page count:            {report['page_count']:,}",
        f"Freelist pages:        {report['freelist_pages']:,}",
        "Estimated reclaimable: " + _format_bytes(report["estimated_reclaimable_bytes"]),
        "",
        "Row counts",
    ]
    for table, count in sorted(
        report["row_counts"].items(), key=lambda item: item[1], reverse=True
    ):
        lines.append(f"  {table:<36} {count:>14,}")
    lines.extend(["", "Bars by timeframe"])
    for item in report.get("bar_timeframes", []):
        lines.append(
            f"  {item['timeframe']:<5} symbols={item['symbols']:>6,} bars={item['bars']:>12,} "
            f"first={item['first_timestamp']} last={item['last_timestamp']}"
        )
    lines.extend(["", "Raw SEC cache by endpoint"])
    if report["raw_sec_cache"]:
        for endpoint in report["raw_sec_cache"]:
            lines.append(
                f"  {endpoint['endpoint']:<18} rows={endpoint['rows']:>7,}  "
                f"payload={_format_bytes(endpoint['payload_bytes']):>10}  "
                f"average={_format_bytes(endpoint['average_payload_bytes']):>9}  "
                f"largest={_format_bytes(endpoint['largest_payload_bytes']):>9}"
            )
    else:
        lines.append("  (empty)")
    lines.extend(["", "Table/index allocation"])
    if report["object_sizes"] is None:
        lines.append(f"  unavailable: {report['dbstat_error']}")
        lines.append("  Page, row, and raw-endpoint totals above remain available.")
    else:
        for item in report["object_sizes"]:
            lines.append(f"  {item['name']:<44} {_format_bytes(item['bytes']):>10}")
    return "\n".join(lines)


def _format_cleanup_report(result: dict) -> str:
    if result["safe_rows"] == result["total_rows"]:
        safety = "yes"
    elif result["safe_rows"]:
        safety = "partial"
    else:
        safety = "no"
    lines = [
        "TraWalp database cleanup",
        "",
        f"raw SEC {result['endpoint']}:",
        f"  rows:                    {result['total_rows']:,}",
        f"  payload size:            {_format_bytes(result['total_payload_bytes'])}",
        f"  safe to remove:          {safety}",
        f"  guarded rows:            {result['safe_rows']:,}",
        f"  guarded payload:         {_format_bytes(result['safe_payload_bytes'])}",
        f"  blocked without facts:   {result['blocked_rows']:,}",
        f"  blocked payload:         {_format_bytes(result['blocked_payload_bytes'])}",
        "",
        f"Structured fact rows:      {result['fundamental_fact_rows']:,} (preserved)",
        f"Daily bar rows:            {result['daily_bar_rows']:,} (preserved)",
        "Estimated reclaimable after VACUUM: " + _format_bytes(result["safe_payload_bytes"]),
    ]
    if result["dry_run"]:
        lines.extend(["", "No changes made (--dry-run)."])
    else:
        lines.extend(
            [
                "",
                f"Deleted guarded rows:      {result['deleted_rows']:,}",
                f"Freelist after DELETE:     {result['freelist_pages_after']:,} pages",
                "Physical file size is unchanged until an explicit VACUUM.",
            ]
        )
    if result["blocked_rows"]:
        lines.append("Blocked rows were retained because no normalized fact exists for their CIK.")
    return "\n".join(lines)


def _format_vacuum_requirements(requirements: dict[str, int]) -> str:
    return (
        "\nVACUUM preflight"
        f"\n  current database:       {_format_bytes(requirements['database_bytes'])}"
        f"\n  available disk space:   {_format_bytes(requirements['available_bytes'])}"
        f"\n  conservative temporary: {_format_bytes(requirements['required_temporary_bytes'])}"
        "\nVACUUM may require substantial downtime and temporary disk space."
    )


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def _warn_data_freshness(states: dict[str, dict], *, now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    thresholds = {
        "asset_universe": timedelta(days=7),
        "sec": timedelta(days=7),
        "historical_bars": timedelta(days=3),
        "market_snapshot": timedelta(hours=1),
    }
    for dataset, threshold in thresholds.items():
        raw = states.get(dataset, {}).get("last_success_at")
        if not raw:
            logging.getLogger(__name__).warning("%s has no successful sync metadata", dataset)
            continue
        try:
            age = current - datetime.fromisoformat(str(raw)).astimezone(UTC)
        except ValueError:
            logging.getLogger(__name__).warning("%s has invalid freshness metadata", dataset)
            continue
        if age > threshold:
            logging.getLogger(__name__).warning(
                "%s last updated %s (age %s)", dataset, raw, str(age).split(".")[0]
            )


if __name__ == "__main__":
    raise SystemExit(main())
