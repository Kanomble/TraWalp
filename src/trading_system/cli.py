"""Command line interface for synchronization and local point-in-time screening."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
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
    sync = commands.add_parser("sync", help="Synchronize Alpaca and SEC data")
    sync.add_argument(
        "--symbols",
        nargs="*",
        help="Optional symbol subset. Omit to synchronize all tradable US equities.",
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
    if args.command == "sync":
        key, secret = settings.require_alpaca_credentials()
        database = Database(settings.strategy.storage.database_path)
        database.initialize()
        sec_config = settings.strategy.sec
        synchronizer = DataSynchronizer(
            database,
            AlpacaDataClient(
                key,
                secret,
                feed=DataFeed(settings.strategy.universe.market_data_feed),
                adjustment=Adjustment(settings.strategy.universe.market_data_adjustment),
            ),
            SecClient(
                settings.require_sec_user_agent(),
                request_interval_seconds=sec_config.request_interval_seconds,
                timeout_seconds=sec_config.timeout_seconds,
                max_retries=sec_config.max_retries,
            ),
            market_data_days=settings.strategy.universe.market_data_days,
        )
        requested = [symbol.upper() for symbol in args.symbols] if args.symbols else None
        print(json.dumps(synchronizer.sync(requested), indent=2))
        return 0
    database = Database(settings.strategy.storage.database_path)
    database.initialize()
    if args.command == "screen":
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


if __name__ == "__main__":
    raise SystemExit(main())
