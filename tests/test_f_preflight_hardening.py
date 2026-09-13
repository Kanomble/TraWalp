"""Exact hardening regressions on small, deterministic, exclusively local fixtures."""

import json
from collections import Counter
from dataclasses import fields
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_f_lifecycle_v2 as fixtures
from test_backtest_features import _bars, _config, _database, _facts
from test_f_candidate_discovery import FixtureSource, config_for_fixture, varied_cross_section
from test_f_candidate_manifest import export_fixture

from trading_system.backtest import lifecycle_diagnostics
from trading_system.backtest import lifecycle_validation as validation
from trading_system.backtest.engine import evaluate_variant_entry
from trading_system.backtest.f_candidates import (
    data_fingerprint,
    discovery_code_fingerprint,
    load_manifest,
)
from trading_system.backtest.f_replay import ReplayTechnical
from trading_system.backtest.features import (
    _fast_technical_snapshot,
    _technical_arrays,
    _technical_snapshot_arrays,
)
from trading_system.backtest.native_entries import NativeEntrySessions
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import BarTimeframe, TradableAsset
from trading_system.models.signals import TechnicalSnapshot

config = fixtures.config
local_market = fixtures.local_market


def test_code_fingerprint_covers_canonical_identity_resolution(monkeypatch):
    before = discovery_code_fingerprint()
    original = Path.read_bytes

    def changed(path):
        payload = original(path)
        return (
            payload + b"\n# identity resolver change" if path.name == "sec_identity.py" else payload
        )

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert discovery_code_fingerprint() != before


def test_scoped_fingerprint_excludes_future_and_nonparticipating_history(tmp_path):
    database, end = _database(tmp_path)
    config = _config()
    start = end - timedelta(days=30)
    database.upsert_company(CompanyIdentity(symbol="UNRELATED", cik="0000000002", name="Other"))
    before_stats, after_stats = {}, {}
    before = data_fingerprint(database, config, start=start, end=end, diagnostics=before_stats)
    future = [
        fact.model_copy(
            update={
                "filed": end + timedelta(days=1),
                "accession_number": "future-" + fact.accession_number,
            }
        )
        for fact in _facts("AAA", "0000000001")
    ]
    assert any(f.metric == "shares_outstanding" for f in future)
    database.upsert_facts([*future, *_facts("UNRELATED", "0000000002")])
    database.upsert_bars(
        [
            _bars("AAA")[-1].model_copy(
                update={"close": Decimal("999999"), "high": Decimal("999999")}
            )
        ]
    )
    after = data_fingerprint(database, config, start=start, end=end, diagnostics=after_stats)
    assert before == after
    assert before_stats["fingerprint_rows"] == after_stats["fingerprint_rows"]
    assert before_stats["fingerprint_symbols"] == 1
    assert before_stats["fingerprint_stream_queries"] == 6


@pytest.mark.parametrize(
    "correction", ["warmup", "fact", "shares", "universe", "sic", "identity", "quarantine"]
)
def test_scoped_fingerprint_invalidates_all_relevant_mutable_inputs(tmp_path, correction):
    database, end = _database(tmp_path)
    config = _config()
    start = end - timedelta(days=30)
    before = data_fingerprint(database, config, start=start, end=end)
    with database.connect() as connection:
        if correction == "warmup":
            connection.execute(
                "UPDATE bars SET close='92' WHERE timestamp=(SELECT MIN(timestamp) FROM bars)"
            )
        elif correction in ("fact", "shares"):
            metric = "revenue" if correction == "fact" else "shares_outstanding"
            connection.execute("UPDATE fundamental_facts SET value='123' WHERE metric=?", (metric,))
        elif correction == "universe":
            connection.execute("UPDATE assets SET tradable=0 WHERE symbol='AAA'")
        elif correction == "sic":
            connection.execute("UPDATE companies SET sic='3572' WHERE symbol='AAA'")
        elif correction == "identity":
            connection.execute("UPDATE companies SET cik='0000000009' WHERE symbol='AAA'")
        else:
            connection.execute(
                "INSERT INTO sync_state(source,key,value,updated_at) VALUES "
                "('sec_identity_conflicts','AAA',?,'fixture')",
                (json.dumps({"symbol": "AAA", "status": "unresolved"}),),
            )
    assert data_fingerprint(database, config, start=start, end=end) != before


@pytest.mark.parametrize("intraday_only", [False, True])
def test_final_snapshot_guard_runs_after_coverage(monkeypatch, local_market, config, intraday_only):
    _, database, sessions, preparation = fixtures.research_preparation(monkeypatch, local_market)
    original = database.iter_entry_coverage_batches

    def changing(requirements):
        yield from original(requirements)
        if intraday_only:
            database.upsert_bars(fixtures.native_session(sessions[1], weak=False))
        else:
            with database.connect() as connection:
                connection.execute(
                    "UPDATE bars SET close='101' WHERE symbol='SPY' AND timeframe='1d'"
                )

    monkeypatch.setattr(database, "iter_entry_coverage_batches", changing)
    if intraday_only:
        _, manifest = validation.build_f_intraday_entry_preflight(
            database, config, sessions[0], sessions[-1], preparation=preparation
        )
        manifest.close()
    else:
        with pytest.raises(ValueError, match="Discovery inputs changed during preflight"):
            validation.build_f_intraday_entry_preflight(
                database, config, sessions[0], sessions[-1], preparation=preparation
            )


def test_hard_rejection_skips_scoring_only_until_first_relevance(monkeypatch):
    config = config_for_fixture()
    source = FixtureSource(varied_cross_section(), config)
    session = date(2024, 8, 1)
    before = {
        r.symbol: evaluate_variant_entry(r, FROZEN_CHAMPION_F.variant, config)
        for r in source.screen(session).records
    }
    hard = {c.company.symbol for c in source.templates.values() if c.base_exclusions}
    scored, original = [], source.screener._score_inputs

    def score(candidate, peers):
        scored.append(candidate.company.symbol)
        return original(candidate, peers)

    monkeypatch.setattr(source.screener, "_score_inputs", score)
    relevant = set()
    result = source.f_candidates(session, config, replay_symbols=relevant)
    assert not hard.intersection(scored)
    assert result.replay["omitted_rejections"] == Counter(
        e.first_failure for symbol, e in before.items() if symbol not in relevant
    )
    relevant.update(hard)
    later = source.f_candidates(session, config, replay_symbols=relevant)
    assert hard.issubset(scored)
    for row in later.replay["rejected"]:
        from trading_system.backtest.f_replay import replay_record

        assert (
            evaluate_variant_entry(replay_record(row, session), FROZEN_CHAMPION_F.variant, config)
            == before[row["symbol"]]
        )


def test_replay_technical_schema_and_exact_bounded_prefixes():
    assert {f.name for f in fields(ReplayTechnical)} == set(TechnicalSnapshot.model_fields)
    config = _config()
    bars = _bars("AAA", 550)
    arrays = _technical_arrays(bars)
    for length in [1, 14, 15, 20, 50, 63, 126, 200, 252, 320, 321, 400, 550]:
        first = max(0, length - config.universe.market_data_days)
        assert _technical_snapshot_arrays(
            *(values[first:length] for values in arrays), config
        ) == _fast_technical_snapshot(bars[first:length], config)


def test_score_only_exclusion_does_not_take_hard_rejection_shortcut(monkeypatch):
    config = config_for_fixture()
    candidates = varied_cross_section()
    candidate = candidates[1]
    candidate.base_exclusions = ["quality_score_below_minimum"]
    source = FixtureSource(candidates, config)
    original = source.screener._score_inputs
    scored = set()

    def score(candidate, peers):
        scored.add(candidate.company.symbol)
        return original(candidate, peers)

    monkeypatch.setattr(source.screener, "_score_inputs", score)
    source.f_candidates(date(2024, 8, 1), config)
    assert candidate.company.symbol in scored


def test_replay_does_not_rebuild_pydantic_objects(monkeypatch, local_market, config, tmp_path):
    from trading_system.models.screening import ScreenRecord

    database, sessions, path = export_fixture(monkeypatch, local_market, config, tmp_path)
    _, source = load_manifest(path, database, config, sessions[0], sessions[-1], sessions)

    def forbidden(*args, **kwargs):
        pytest.fail("Pydantic reconstruction during trusted replay")

    monkeypatch.setattr(ScreenRecord, "model_validate", forbidden)
    monkeypatch.setattr(TechnicalSnapshot, "model_validate", forbidden)
    for session in sessions[:-1]:
        source.screen(session)
        assert len(source.cache) == 1


@pytest.mark.parametrize("metric", ["revenue", "shares_outstanding"])
def test_manifest_rejects_visible_fact_changes_but_accepts_future_facts(
    monkeypatch, local_market, config, tmp_path, metric
):
    database, sessions, path = export_fixture(monkeypatch, local_market, config, tmp_path)
    fact = next(f for f in _facts("AAA", "0000000001") if f.metric == metric)
    future = fact.model_copy(update={"filed": sessions[-1] + timedelta(days=1)})
    database.upsert_facts([future])
    load_manifest(path, database, config, sessions[0], sessions[-1], sessions)
    visible = fact.model_copy(update={"filed": sessions[0]})
    database.upsert_facts([visible])
    with pytest.raises(ValueError, match="data_snapshot_fingerprint"):
        load_manifest(path, database, config, sessions[0], sessions[-1], sessions)


def test_replay_checks_content_again_if_file_changes_after_validation(
    monkeypatch, local_market, config, tmp_path
):
    database, sessions, path = export_fixture(monkeypatch, local_market, config, tmp_path)
    manifest, source = load_manifest(path, database, config, sessions[0], sessions[-1], sessions)
    replay_path = path.parent / manifest["replay_file"]
    replay_path.write_bytes(replay_path.read_bytes().replace(b'"scores":[90', b'"scores":[80', 1))
    with pytest.raises(ValueError, match="changed after manifest validation"):
        source.screen(sessions[0])


def test_native_entry_provider_is_bounded_and_timeframe_specific(local_market):
    database, sessions = local_market
    database.upsert_bars(fixtures.native_session(sessions[1], weak=False))
    requirements = [("AAA", sessions[i], sessions[i + 1]) for i in range(20)]
    with NativeEntrySessions(cache_limit=3) as provider:
        for bars, closes, _, _ in database.iter_entry_coverage_batches(requirements):
            for symbol, signal, execution in requirements:
                provider.add(
                    symbol, signal, execution, bars[symbol, execution], closes[symbol, signal]
                )
        for key in requirements:
            provider.entry_session(*key)
            assert len(provider.cache) <= 3
        reads = provider.reads
        provider.entry_session(*requirements[-1])
        assert provider.reads == reads
        assert provider.peak_cache_size == 3
        with pytest.raises(ValueError, match="not coverage-verified"):
            provider.entry_session(*requirements[0], timeframe=BarTimeframe.MINUTES_5)


class TemporalScreens:
    """Changing eligibility, held scores, re-entry and permanently rejected names."""

    def __init__(self, sessions):
        self.sessions = sessions

    def screen(self, session):
        index = self.sessions.index(session)
        records = []
        for symbol, eligible_days in [("AAA", (2, 3, 14, 15, 27, 28)), ("BBB", (2, 5, 14, 19, 27))]:
            record = fixtures.record(symbol).model_copy(update={"as_of": session})
            if index not in eligible_days:
                record = record.model_copy(
                    update={
                        "exclusion_reasons": ("insufficient_liquidity",) if index % 2 == 0 else (),
                        "technical": record.technical.model_copy(
                            update={
                                "sma20_rising": False,
                                "momentum5": -0.01 if index % 3 == 0 else 0.01,
                            }
                        ),
                        "scores": record.scores.model_copy(
                            update={
                                "quality": record.scores.quality.model_copy(
                                    update={"score": 40.0 + index}
                                )
                            }
                        ),
                    }
                )
            records.append(record)
        records.extend(
            fixtures.record(f"NEVER{i:02}").model_copy(
                update={
                    "as_of": session,
                    "exclusion_reasons": (
                        "identity_conflict" if i % 2 else "invalid_or_low_price",
                    ),
                }
            )
            for i in range(20)
        )
        return SimpleNamespace(as_of=session, records=tuple(records))


def _temporal_manifest(monkeypatch, local_market, config, tmp_path):
    _, database, sessions, preparation = fixtures.research_preparation(monkeypatch, local_market)
    source = preparation.screen_source = TemporalScreens(sessions)
    daily = database.bars_between(
        ["AAA"],
        datetime.combine(sessions[0], time.min, UTC),
        datetime.combine(sessions[-1], time.max, UTC),
        timeframe=BarTimeframe.DAY_1,
    )
    database.upsert_bars([bar.model_copy(update={"symbol": "BBB"}) for bar in daily])
    database.upsert_assets(
        [TradableAsset(symbol=s, name=s, tradable=True, fractionable=True) for s in ("AAA", "BBB")]
    )
    for i, symbol in enumerate(("AAA", "BBB"), 1):
        database.upsert_company(
            CompanyIdentity(symbol=symbol, name=symbol, cik=str(i).zfill(10), sic="2834")
        )
    for index, session in enumerate(sessions[1:], 1):
        for symbol in ("AAA", "BBB"):
            database.upsert_bars(
                [
                    bar.model_copy(update={"symbol": symbol})
                    for bar in fixtures.native_session(session, weak=index in (3, 15))
                ]
            )
    report, requirements = validation.build_f_intraday_entry_preflight(
        database,
        config,
        sessions[0],
        sessions[-1],
        preparation=preparation,
        qualification={"ready": True, "failure_reasons": []},
    )
    assert report["intraday_qualified"]
    paths = validation.export_f_intraday_entry_preflight(
        report, requirements, tmp_path, stem="temporal"
    )
    path = paths["intraday_candidates.json"]
    assert requirements.replay_spool.records < 2 * len(sessions)
    requirements.close()
    monkeypatch.setattr(
        validation, "_qualify_daily_research", lambda *args: {"ready": True, "failure_reasons": []}
    )
    return database, sessions, source, path


def test_reference_and_reduced_i0_i1_complete_results_tables_and_summary(
    monkeypatch, local_market, config, tmp_path
):
    database, sessions, source, path = _temporal_manifest(
        monkeypatch, local_market, config, tmp_path
    )
    real_load = validation.load_manifest
    diagnostics_class = validation.LifecycleDiagnostics

    def legacy_diagnostics(*args, **kwargs):
        kwargs["profile"] = "lifecycle"
        return diagnostics_class(*args, **kwargs)

    monkeypatch.setattr(validation, "LifecycleDiagnostics", legacy_diagnostics)

    def full_reference(*args, **kwargs):
        manifest, _ = real_load(*args, **kwargs)
        return manifest, source

    monkeypatch.setattr(validation, "load_manifest", full_reference)
    engine_class = validation.BacktestEngine

    class ReferenceEngine(engine_class):
        def __init__(self, *args, **kwargs):
            kwargs.pop("native_entry_provider", None)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(validation, "BacktestEngine", ReferenceEngine)
    reference = validation.run_f_intraday_entry(
        database, config, sessions[0], sessions[-1], candidate_manifest=path
    )
    monkeypatch.setattr(validation, "load_manifest", real_load)
    monkeypatch.setattr(validation, "BacktestEngine", engine_class)
    monkeypatch.setattr(validation, "LifecycleDiagnostics", diagnostics_class)
    original_bars_between = database.bars_between

    def no_repeated_native_read(*args, **kwargs):
        assert kwargs.get("timeframe") is not BarTimeframe.MINUTES_15
        return original_bars_between(*args, **kwargs)

    monkeypatch.setattr(database, "bars_between", no_repeated_native_read)

    def forbidden(*args, **kwargs):
        pytest.fail("diagnostics re-evaluated authoritative F ranks")

    monkeypatch.setattr(lifecycle_diagnostics, "evaluate_variant_entry", forbidden)
    actual = validation.run_f_intraday_entry(
        database, config, sessions[0], sessions[-1], candidate_manifest=path
    )
    omitted_tables = {
        "peer_context",
        "peer_spillover",
        "peer_summary",
        "correlation",
        "trend_health_events",
        "dynamic_profit_events",
    }
    for name in omitted_tables:
        assert actual.tables[name] == []
    assert reference.tables["peer_context"] and reference.tables["trend_health_events"]
    assert {k: v for k, v in actual.tables.items() if k not in omitted_tables} == {
        k: v for k, v in reference.tables.items() if k not in omitted_tables
    }
    assert validation._field_union(actual.tables["entry_gap_analysis"]) == validation._field_union(
        reference.tables["entry_gap_analysis"]
    )
    for key, expected in reference.results.items():
        assert expected.positions and len(expected.positions) >= 2
        assert any(p.is_reentry for p in expected.positions)
        assert actual.results[key].model_dump(exclude={"generated_at"}) == expected.model_dump(
            exclude={"generated_at"}
        )
    assert actual.tables["entry_quality_events"]
    assert any(
        row["status"] == "OPENING_WEAKNESS_VETO" for row in actual.tables["entry_quality_events"]
    )
    assert actual.summary["performance"]["peer_group_sessions_built"] == 0
    assert reference.summary["performance"]["peer_group_sessions_built"] > 0
    print(
        json.dumps(
            {
                "diagnostics_fixture_seconds": {
                    "before": reference.summary["performance"]["diagnostics_seconds"],
                    "after": actual.summary["performance"]["diagnostics_seconds"],
                },
                "validation_phases_seconds": {
                    key: actual.summary["performance"][key]
                    for key in (
                        "qualification_seconds",
                        "coverage_verification_seconds",
                        "i0_seconds",
                        "i1_seconds",
                        "diagnostics_seconds",
                    )
                },
                "entry_quality_candidate_rows": actual.summary["performance"][
                    "entry_quality_candidate_rows"
                ],
                "peer_group_sessions_built": actual.summary["performance"][
                    "peer_group_sessions_built"
                ],
            }
        )
    )
    # Timings alone are nondeterministic; nested qualification and summary rows match.
    for summary in (actual.summary, reference.summary):
        summary.pop("performance")
        summary["intraday_qualification"].pop("performance")
    assert actual.summary == reference.summary


def test_entry_quality_diagnostics_never_build_peer_context(
    monkeypatch, local_market, config, tmp_path
):
    database, sessions, _, path = _temporal_manifest(monkeypatch, local_market, config, tmp_path)
    provider = validation.TechnicalPeerContextProvider

    def forbidden(*args, **kwargs):
        pytest.fail("Entry-quality validation reached lifecycle/peer diagnostics")

    for method in ("_session_groups", "_context", "peer_state", "trend", "_technical"):
        monkeypatch.setattr(provider, method, forbidden)
    original_init = provider.__init__

    def guarded_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.context = forbidden  # context() is an instance-owned LRU, not a class method.

    monkeypatch.setattr(provider, "__init__", guarded_init)
    result = validation.run_f_intraday_entry(
        database, config, sessions[0], sessions[-1], candidate_manifest=path
    )
    assert result.tables["entry_gap_analysis"] and result.tables["entry_quality_events"]
    assert result.summary["performance"]["peer_group_sessions_built"] == 0
    assert result.summary["performance"]["entry_quality_candidate_rows"] == len(
        result.tables["entry_gap_analysis"]
    )
    # Gap CSVs include historical correlation evidence. Compute that evidence only
    # in tables(), against a snapshot of open symbols, never in the entry hooks.
    calls = []
    correlation = {"correlation_pairs_valid": 1, "mean_correlation_to_open_positions": 0.5}
    context = SimpleNamespace(
        correlations=lambda *args: calls.append(args) or correlation,
        complete_history=lambda *args: [],
    )
    observer = lifecycle_diagnostics.LifecycleDiagnostics(context, config, profile="entry_quality")
    report = SimpleNamespace(as_of=sessions[0], f_candidates=((90, fixtures.record("AAA")),))
    positions = {"BBB": None}
    observer.observe_entry_context(report, positions, sessions[1])
    positions.clear()
    assert calls == []
    tables = observer.tables(SimpleNamespace(positions=()))
    assert calls == [("AAA", ("BBB",), sessions[0])]
    assert tables["entry_gap_analysis"][0]["mean_correlation_to_open_positions"] == 0.5
    calls.clear()
    observer.observe_execution_context(sessions[0], "AAA", positions)
    tables = observer.tables(SimpleNamespace(positions=()))
    assert calls == []
    assert tables["entry_gap_analysis"][0]["correlation_pairs_valid"] == 0
