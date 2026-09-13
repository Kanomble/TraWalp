"""Small offline deterministic benchmark; never opens the research database."""

import json
from datetime import date
from time import perf_counter

from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.models.fundamentals import CompanyIdentity, FundamentalMetrics
from trading_system.models.signals import TechnicalSnapshot
from trading_system.strategy.screener import Screener, _PreparedCandidate


def cross_section(count=600):
    return [
        _PreparedCandidate(
            company=CompanyIdentity(
                symbol=f"S{i:04}", cik=f"{i + 1:010}", name="Fixture", sic=f"{3500 + i // 40}"
            ),
            fundamentals=FundamentalMetrics(
                revenue_growth=0.1 + i % 13 / 100,
                eps_growth=0.2,
                operating_cash_flow_positive=True,
                operating_cash_flow_growth=0.15,
                operating_margin=0.25,
                roic=0.2,
                debt_to_ebitda=1.0,
                pe=12.0 + i % 7,
                ev_to_ebitda=8.0,
                ev_to_ebit=10.0,
                fcf_yield=0.08,
            ),
            technical=TechnicalSnapshot(
                price=100,
                sma20=98,
                sma50=95,
                sma200=90,
                sma20_rising=True,
                momentum126=0.2,
                momentum5=0.03,
                drawdown_52w=-0.03,
                atr14=2,
                relative_volume=1.2,
            ),
            average_dollar_volume_20d=20_000_000,
            base_exclusions=[],
            data_warnings=[],
            analysis_date=date(2024, 8, 1),
        )
        for i in range(count)
    ]


def compare_discovery(sessions_count, companies_count):
    from datetime import timedelta

    from test_f_candidate_discovery import FixtureSource, config_for_fixture

    from trading_system.backtest.engine import CachedScreenSource
    from trading_system.backtest.f_candidates import ReplaySpool
    from trading_system.backtest.lifecycle_validation import iter_f_candidates

    config = config_for_fixture()
    sessions = [date(2024, 8, 1) + timedelta(days=i) for i in range(sessions_count + 1)]
    candidates = cross_section(companies_count)
    # Include technical-gate rejections while retaining the complete peer universe.
    for i, candidate in enumerate(candidates):
        if i % 4:
            candidate.technical = candidate.technical.model_copy(update={"sma20_rising": False})
    results, expected = {}, None
    for reference in (True, False):
        source = FixtureSource(candidates, config)
        if reference:
            # Isolate peer preparation: both runs use the same streaming F discovery,
            # scoring, replay and cache behavior. Only this run uses the DataFrame oracle.
            def dataframe_peer_index(prepared, screener=source.screener):
                started = perf_counter()
                table = screener._peer_table(prepared)
                screener.diagnostics.peer_table_seconds += perf_counter() - started
                screener.diagnostics.peer_table_rows += len(table)
                return screener._peer_index(table)

            source.screener._f_peer_index = dataframe_peer_index
        cached = CachedScreenSource(source)
        spool = ReplaySpool()
        started = perf_counter()
        values = [
            (
                signal.isoformat(),
                execution.isoformat(),
                rank,
                record.symbol,
                record.scores.model_dump(),
            )
            for signal, execution, rank, record in iter_f_candidates(
                cached,
                config,
                sessions,
                replay_spool=spool,
            )
        ]
        elapsed = perf_counter() - started
        label = "reference" if reference else "optimized"
        results[label] = {
            "elapsed_seconds": elapsed,
            "candidate_discovery_seconds": source.discovery_diagnostics.screen_session_seconds,
            "candidate_count": len(values),
            "full_screen_cache_size": len(cached.cache),
            "discovery_diagnostics": source.discovery_diagnostics.as_dict(),
            "replay_bytes": spool.file.tell(),
        }
        spool.file.close()
        if reference:
            expected = values
        else:
            assert values == expected
        print(
            f"{label}: {elapsed:.3f}s; candidates={len(values)}; "
            f"full_screen_cache={len(cached.cache)}",
            flush=True,
        )
    results.update(
        sessions=sessions_count,
        companies=companies_count,
        speedup=results["reference"]["elapsed_seconds"] / results["optimized"]["elapsed_seconds"],
        exact_candidate_equality=True,
        peer_preparation_speedup=(
            sum(
                results["reference"]["discovery_diagnostics"][key]
                for key in ("peer_table_seconds", "peer_group_lookup_seconds")
            )
            / sum(
                results["optimized"]["discovery_diagnostics"][key]
                for key in ("peer_table_seconds", "peer_group_lookup_seconds")
            )
        ),
    )
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=3)
    parser.add_argument("--companies", type=int, default=200)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.compare:
        result = compare_discovery(args.sessions, args.companies)
        print(json.dumps(result, indent=2))
        if args.output:
            from pathlib import Path

            Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        raise SystemExit(0)
    screener = Screener(Database("unused-benchmark.sqlite3"), load_settings().strategy)
    started = perf_counter()
    for _ in range(args.sessions):
        prepared = cross_section(args.companies)
        screener.report_from_prepared(
            prepared, [], requested_as_of=date(2024, 8, 1), market_session=date(2024, 8, 1)
        )
    print(
        json.dumps(
            {
                "sessions": args.sessions,
                "companies": args.companies,
                "elapsed_seconds": perf_counter() - started,
                **screener.diagnostics.as_dict(),
            },
            indent=2,
        )
    )
