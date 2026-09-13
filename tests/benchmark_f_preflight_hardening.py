"""Bounded synthetic v1/v2 comparison; only an owned temporary SQLite database is written."""

import argparse
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

from benchmark_f_discovery import cross_section
from reference_f_preflight import (
    ReferenceCoverage,
    ReferenceDiscovery,
    reference_replay_report,
    whole_database_fingerprint,
)
from test_backtest_features import _bars, _facts
from test_f_candidate_discovery import FixtureSource, config_for_fixture
from test_f_lifecycle_v2 import native_session

from trading_system.backtest.f_candidates import ReplaySpool, data_fingerprint, load_manifest
from trading_system.backtest.features import (
    _empty_candidate,
    _fast_technical_snapshot,
    _technical_arrays,
    _technical_snapshot_arrays,
)
from trading_system.backtest.lifecycle_validation import (
    build_f_intraday_entry_preflight,
    export_f_intraday_entry_preflight,
)
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between


class BenchmarkSource(FixtureSource):
    def __init__(self, count, config, sessions):
        candidates = cross_section(count)
        for i, candidate in enumerate(candidates):
            if i % 5:
                company = candidate.company.model_copy(update={"sic": "6000"})
                candidates[i] = _empty_candidate(company, sessions[0], "financial_excluded")
            elif i % 20:
                candidate.technical = candidate.technical.model_copy(update={"sma20_rising": False})
        super().__init__(candidates, config)
        self.sessions = sessions

    def _candidate(self, company, session):
        candidate = super()._candidate(company, session)
        number = int(company.symbol[1:])
        index = self.sessions.index(session)
        if number % 20 == 0 and (index < number % 7 or index % 9 == 7):
            candidate = replace(candidate, base_exclusions=["insufficient_liquidity"])
        return candidate


def seed_database(path, source, sessions):
    database = Database(path)
    database.initialize()
    companies = source._companies
    daily_sessions = trading_sessions_between(date(2023, 1, 1), sessions[-1])
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO assets VALUES (?,?,NULL,1,1,0,'fixture')",
            ((c.symbol, c.name) for c in companies),
        )
        connection.executemany(
            "INSERT INTO companies VALUES (?,?,?,?,NULL,'fixture')",
            ((c.cik, c.symbol, c.name, c.sic) for c in companies),
        )
        connection.executemany(
            "INSERT INTO bars(symbol,timeframe,timestamp,open,high,low,close,volume) "
            "VALUES (?,'1d',?,'100','102','98','100',2000000)",
            (
                (symbol, datetime.combine(day, time.min, UTC).isoformat())
                for symbol in [*(c.symbol for c in companies), "SPY"]
                for day in daily_sessions
            ),
        )
        # Same small accounting history per issuer, plus future filings that v2 excludes.
        facts = _facts("AAA", "0000000001")
        connection.executemany(
            "INSERT INTO fundamental_facts(cik,symbol,metric,taxonomy,tag,value,unit,"
            "period_start,period_end,filed,form,accession_number) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (
                    c.cik,
                    c.symbol,
                    f.metric,
                    f.taxonomy,
                    f.tag,
                    str(f.value),
                    f.unit,
                    f.period_start.isoformat() if f.period_start else None,
                    f.period_end.isoformat(),
                    (sessions[-1] + timedelta(days=30)).isoformat()
                    if future
                    else f.filed.isoformat(),
                    f.form,
                    f"{c.symbol}-{f.accession_number}-{future}",
                )
                for c in companies
                for future in (False, True)
                for f in facts
            ),
        )
    return database, len(daily_sessions) * (len(companies) + 1), 2 * len(companies) * len(facts)


def benchmark(sessions_count=30, companies_count=600):
    config = config_for_fixture()
    sessions = trading_sessions_between(date(2024, 8, 1), date(2024, 10, 31))[: sessions_count + 1]
    if len(sessions) != sessions_count + 1:
        raise ValueError("Use at most 60 synthetic sessions")
    before_source = BenchmarkSource(companies_count, config, sessions)
    after_source = BenchmarkSource(companies_count, config, sessions)
    with TemporaryDirectory(prefix="f-hardening-benchmark-") as directory:
        root = Path(directory)
        database, daily_rows, facts_rows = seed_database(
            root / "fixture.sqlite3", before_source, sessions
        )
        started = perf_counter()
        whole_database_fingerprint(database)
        old_fingerprint_seconds = perf_counter() - started
        scoped_stats = {}
        data_fingerprint(
            database, config, start=sessions[0], end=sessions[-1], diagnostics=scoped_stats
        )
        old_spool = ReplaySpool()
        before_candidates = []
        started = perf_counter()
        for signal, execution in zip(sessions[:-1], sessions[1:], strict=True):
            result = ReferenceDiscovery.reference_f_candidates(before_source, signal, config)
            old_spool.append(result.replay)
            before_candidates.extend(
                (signal.isoformat(), execution.isoformat(), rank, record.symbol, score)
                for rank, (score, record) in enumerate(result.records, 1)
            )
        old_discovery_seconds = perf_counter() - started
        print(f"v1 discovery: {old_discovery_seconds:.3f}s", flush=True)
        for _, execution, _, symbol, _ in before_candidates:
            database.upsert_bars(
                [
                    bar.model_copy(update={"symbol": symbol})
                    for bar in native_session(date.fromisoformat(execution), weak=False)
                ]
            )
        requirements_keys = [
            (symbol, date.fromisoformat(signal), date.fromisoformat(execution))
            for signal, execution, _, symbol, _ in before_candidates
        ]
        started = perf_counter()
        baseline_batches = sum(
            1
            for _ in ReferenceCoverage(database.path).iter_entry_coverage_batches(requirements_keys)
        )
        baseline_coverage_load_seconds = perf_counter() - started
        report, manifest = build_f_intraday_entry_preflight(
            database,
            config,
            sessions[0],
            sessions[-1],
            preparation=SimpleNamespace(sessions=sessions, screen_source=after_source),
            qualification={"ready": True, "failure_reasons": []},
        )
        after_candidates = [
            (
                r["signal_date"],
                r["execution_session"],
                r["candidate_rank"],
                r["symbol"],
                r["evaluation_score"],
            )
            for r in manifest["candidate_sessions"]
        ]
        assert before_candidates == after_candidates
        assert report["intraday_qualified"]
        performance = report["performance"]
        print(f"v2 discovery: {performance['candidate_discovery_seconds']:.3f}s", flush=True)
        started = perf_counter()
        for signal in sessions[:-1]:
            old_spool.file.seek(old_spool.offsets[signal])
            reference_replay_report(json.loads(old_spool.file.readline()))
        old_parse_seconds = perf_counter() - started
        paths = export_f_intraday_entry_preflight(report, manifest, root, stem="synthetic")
        validation_stats = {}
        _, replay = load_manifest(
            paths["intraday_candidates.json"],
            database,
            config,
            sessions[0],
            sessions[-1],
            sessions,
            diagnostics=validation_stats,
        )
        for session in sessions[:-1]:
            replay.screen(session)
            assert len(replay.cache) == 1
        output = {
            "fixture": "synthetic prepared cross-sections; 80% hard rejects, 5% potential F names",
            "sessions": sessions_count,
            "universe_size": companies_count,
            "candidate_count": len(after_candidates),
            "unique_f_symbols": len({r[3] for r in after_candidates}),
            "daily_rows_in_fixture": daily_rows,
            "facts_in_fixture": facts_rows,
            "reference": {
                "fingerprint_seconds": old_fingerprint_seconds,
                "candidate_discovery_seconds": old_discovery_seconds,
                "replay_serialization_seconds": old_spool.serialization_seconds,
                "replay_bytes": old_spool.bytes,
                "replay_records": old_spool.records,
                "manifest_replay_parse_seconds": old_parse_seconds,
                "coverage_load_seconds": baseline_coverage_load_seconds,
                "coverage_sql_queries": 2 * baseline_batches,
                "full_screen_cache_size": 0,
                "candidate_preparation_sql_queries": 0,
            },
            "optimized": {
                **performance,
                "scoped_fingerprint": scoped_stats,
                "manifest_validation": validation_stats,
                "manifest_replay_parse_seconds": replay.parse_seconds,
                "candidate_preparation_sql_queries": 0,
            },
            "exact_candidate_equality": True,
            "synthetic_discovery_speedup": old_discovery_seconds
            / performance["candidate_discovery_seconds"],
            "synthetic_fingerprint_speedup": old_fingerprint_seconds
            / scoped_stats["fingerprint_seconds"],
            "timing_note": "Discovery includes serialization; "
            "manifest validation includes its scoped fingerprint. "
            "Coverage reference load excludes decision evaluation. Do not sum inclusive phases.",
        }
        old_spool.file.close()
        manifest.close()
    bars = _bars("TECH", 400 + sessions_count)
    starts = list(range(400, 400 + sessions_count))
    started = perf_counter()
    expected = [
        _fast_technical_snapshot(bars[max(0, n - config.universe.market_data_days) : n], config)
        for _ in range(30)
        for n in starts
    ]
    old_technical_seconds = perf_counter() - started
    started = perf_counter()
    actual = []
    for _ in range(30):
        arrays = _technical_arrays(bars)
        actual.extend(
            _technical_snapshot_arrays(
                *(a[max(0, n - config.universe.market_data_days) : n] for a in arrays), config
            )
            for n in starts
        )
    assert actual == expected
    output["technical_prefix_benchmark"] = {
        "symbols": 30,
        "sessions": sessions_count,
        "reference_seconds": old_technical_seconds,
        "optimized_seconds": perf_counter() - started,
        "exact_equality": True,
    }
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=30)
    parser.add_argument("--companies", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = benchmark(args.sessions, args.companies)
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
