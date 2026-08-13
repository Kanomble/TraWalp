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
from trading_system.config import load_settings
from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.database import Database
from trading_system.data.market_sessions import effective_trading_session
from trading_system.data.sec_client import SecClient
from trading_system.data.sync import DataSynchronizer
from trading_system.fundamentals.debug import debug_fundamentals
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
    parser.add_argument("--config", default="config/strategy.yaml", type=Path)
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
    refresh_market = commands.add_parser(
        "refresh-market", help="Refresh batched current snapshots without downloading history"
    )
    refresh_market.add_argument("--symbols", nargs="*")
    commands.add_parser("status", help="Show local dataset freshness")
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
    settings = load_settings(args.config)
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
    if args.command in {"sync", "sync-assets", "update-bars", "refresh-market"}:
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
    if args.command == "status":
        print(_format_data_status(database.dataset_states()))
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
            f"\nCSV: {csv_path}\nJSON: {json_path}"
        )
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
    )


def _format_data_status(states: dict[str, dict]) -> str:
    labels = {
        "asset_universe": "Asset universe",
        "sec": "SEC fundamentals",
        "historical_bars": "Historical bars",
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
            "bars_updated",
            "errors",
            "elapsed_seconds",
        ):
            if key in state:
                lines.append(f"  {key.replace('_', ' ')}: {state[key]}")
    return "\n".join(lines)


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
