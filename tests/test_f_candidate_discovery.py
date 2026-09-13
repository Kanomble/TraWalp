"""Exact reference regressions and bounded-memory F discovery, entirely offline."""

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
from benchmark_f_discovery import cross_section
from reference_intraday_screener import ReferenceScreener
from test_backtest_features import _config, _database

from trading_system.backtest.engine import CachedScreenSource, evaluate_variant_entry
from trading_system.backtest.f_candidates import ManifestScreenSource, ReplaySpool
from trading_system.backtest.features import (
    HistoricalFeatureScreenSource,
    HistoricalPerformanceDiagnostics,
)
from trading_system.backtest.lifecycle_validation import iter_f_candidates
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.config import load_settings
from trading_system.data.database import Database
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.strategy.scoring import PeerPercentiles, percentile_score
from trading_system.strategy.screener import Screener


class FixtureSource(HistoricalFeatureScreenSource):
    def __init__(self, candidates, config, *, reference=False):
        self.config = config
        self.screener = (ReferenceScreener if reference else Screener)(Database("unused"), config)
        self.discovery_diagnostics = self.screener.diagnostics
        self.diagnostics = HistoricalPerformanceDiagnostics()
        self.templates = {c.company.symbol: c for c in candidates}
        self._companies = [c.company for c in candidates]
        self._conflicted = [CompanyIdentity(symbol="CONFLICT", cik="9999999999", name="Conflict")]

    def _candidate(self, company, session):
        candidate = self.templates[company.symbol]
        return replace(
            candidate, analysis_date=session, data_warnings=list(candidate.data_warnings)
        )


def varied_cross_section():
    candidates = cross_section(25)
    sics = ["3571"] * 8 + ["3572"] * 3 + ["3573"] * 3 + ["3581"] * 2 + ["3591"] * 2
    sics += [None, "bad", "1234", "1235", "1236", "2800", "2800"]
    for i, candidate in enumerate(candidates):
        candidate.company = candidate.company.model_copy(update={"sic": sics[i]})
        candidate.fundamentals = candidate.fundamentals.model_copy(
            update={
                "pe": None if i % 7 == 0 else -1 if i % 5 == 0 else 12.0 + i % 3,
                "roic": None if i % 3 == 0 else 0.2,
                "fcf_yield": None if i % 9 == 0 else 0.08,
            }
        )
        if i % 5 == 0:
            candidate.technical = candidate.technical.model_copy(update={"sma20_rising": False})
    candidates[-1].base_exclusions = ["insufficient_liquidity"]
    candidates[-2].base_exclusions = ["no_point_in_time_fundamentals"]
    return candidates


def config_for_fixture():
    config = load_settings().strategy.model_copy(deep=True)
    config.peers.min_peer_count = 4
    config.backtest.min_quality_score = 0
    config.backtest.min_valuation_score = 0
    return config


def test_full_screen_peer_scores_and_ranks_are_exact():
    config = config_for_fixture()
    session = date(2024, 8, 1)
    reference = FixtureSource(varied_cross_section(), config, reference=True).screen(session)
    optimized = FixtureSource(varied_cross_section(), config).screen(session)
    assert optimized.model_dump(exclude={"generated_at"}) == reference.model_dump(
        exclude={"generated_at"}
    )
    assert {record.peer_group for record in optimized.records} >= {
        "sic4:3571",
        "sic3:357",
        "sic2:35",
        None,
    }


def test_f_stream_evaluations_scores_reasons_order_and_count_are_exact():
    config = config_for_fixture()
    sessions = [date(2024, 8, 1) + timedelta(days=i) for i in range(6)]
    reference_source = FixtureSource(varied_cross_section(), config, reference=True)
    optimized_source = FixtureSource(varied_cross_section(), config)
    cached = CachedScreenSource(optimized_source)
    expected = list(iter_f_candidates(reference_source, config, sessions))
    spool = ReplaySpool()
    actual = list(iter_f_candidates(cached, config, sessions, replay_spool=spool))
    assert actual == expected
    assert actual
    assert cached.cache == {}
    replay = ManifestScreenSource(None, spool.offsets, stream=spool.file)
    for session in sessions[:-1]:
        before = {
            r.symbol: evaluate_variant_entry(r, FROZEN_CHAMPION_F.variant, config)
            for r in reference_source.screen(session).records
        }
        after = {
            r.symbol: evaluate_variant_entry(r, FROZEN_CHAMPION_F.variant, config)
            for r in replay.screen(session).records
        }
        assert after == before  # includes every rejected record's blocking reason/evidence
        assert len(replay.cache) <= 1
    spool.file.close()


def test_no_candidate_dataframe_scans_or_non_f_record_construction(monkeypatch):
    config = config_for_fixture()
    source = FixtureSource(varied_cross_section(), config)
    original_index = source.screener._peer_index
    materialized = []
    original_materialize = source.screener._materialize

    def index(frame):
        result = original_index(frame)
        # Any candidate-level DataFrame operation now fails, after session preparation.
        monkeypatch.setattr(pd.DataFrame, "loc", property(lambda _: pytest.fail("candidate scan")))
        return result

    def materialize(*args):
        record = original_materialize(*args)
        materialized.append(record.symbol)
        assert evaluate_variant_entry(record, FROZEN_CHAMPION_F.variant, config).eligible
        return record

    monkeypatch.setattr(source.screener, "_peer_index", index)
    monkeypatch.setattr(source.screener, "_materialize", materialize)
    result = source.f_candidates(date(2024, 8, 1), config)
    assert set(materialized) == {record.symbol for _, record in result.records}
    assert len(materialized) < len(source._companies)


@pytest.mark.parametrize(
    "values",
    [
        [],
        [None] * 4,
        [1.0] * 10,
        [None, -5.0, -1.0, 0.0, 0.0, 1.0, 2.0, 1000.0],
        [float("inf"), 1.0, 2.0, 3.0],
    ],
)
@pytest.mark.parametrize("higher", [False, True])
def test_precomputed_percentiles_are_bit_exact(values, higher):
    config = load_settings().strategy.scores
    prepared = PeerPercentiles.prepare(values, config)
    for value in [None, float("nan"), -100.0, -1.0, 0.0, 0.2, 1.0, 3.0, 10000.0]:
        assert prepared.score(value, higher_is_better=higher) == percentile_score(
            value,
            values,
            higher_is_better=higher,
            lower_quantile=config.winsor_lower_quantile,
            upper_quantile=config.winsor_upper_quantile,
        )


def test_historical_f_path_matches_reference_and_is_causal(tmp_path):
    database, session = _database(tmp_path)
    config = _config()
    config.backtest.min_quality_score = 0
    config.backtest.min_valuation_score = 0
    source = HistoricalFeatureScreenSource(database, config, session, session)
    reference = ReferenceScreener(database, config)
    prepared = [source._candidate(company, session) for company in source._companies]
    report = reference.report_from_prepared(
        prepared, source._conflicted, requested_as_of=session, market_session=session
    )
    expected = sorted(
        (e.score, r.symbol)
        for r in report.records
        if (e := evaluate_variant_entry(r, FROZEN_CHAMPION_F.variant, config)).eligible
    )
    actual = source.f_candidates(session, config)
    assert actual.records
    assert sorted((score, r.symbol) for score, r in actual.records) == expected
    # Loading another future bar/filing cannot enter a T decision.
    from test_backtest_features import _bars, _facts

    database.upsert_bars(
        [
            _bars("AAA")[-1].model_copy(
                update={
                    "close": Decimal("999999"),
                    "high": Decimal("999999"),
                }
            )
        ]
    )
    fact = _facts("AAA", "0000000001")[0].model_copy(
        update={
            "filed": session + timedelta(days=1),
            "accession_number": "future-amendment",
            "value": Decimal("999999999999"),
        }
    )
    database.upsert_facts([fact])
    later = HistoricalFeatureScreenSource(database, config, session, session + timedelta(days=2))
    assert later.f_candidates(session, config).records == actual.records


def test_discovery_diagnostics_and_eta_are_measured():
    config = config_for_fixture()
    source = FixtureSource(varied_cross_section(), config)
    updates = []
    progress = SimpleNamespace(update=lambda **values: updates.append(values))
    sessions = [date(2024, 8, 1) + timedelta(days=i) for i in range(5)]
    list(iter_f_candidates(source, config, sessions, progress=progress))
    assert "estimated_remaining_seconds" not in updates[0]
    assert updates[-1]["estimated_remaining_seconds"] == 0
    diagnostics = source.discovery_diagnostics.as_dict()
    assert diagnostics["sessions_processed"] == 4
    assert diagnostics["companies_processed"] == 4 * 26
    assert diagnostics["eligible_f_candidates"] == updates[-1]["candidate_sessions_discovered"]
    assert all(value >= 0 for name, value in diagnostics.items() if name.endswith("_seconds"))
    json.dumps(diagnostics, allow_nan=False)
