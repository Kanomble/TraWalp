"""Small synthetic-only checks for the local 2022 coverage audit."""

import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_system.backtest.f_data_gap_audit import (
    DERIVED, ECONOMIC, IDENTITY, PEER, RAW, UNKNOWN, AccountingEvidence, AuditDatabase,
    GapAccumulator, Issue, aggregate_sessions, backfill_rows, diagnose_factors,
    digest, identity_diagnostic_issues, inventory, local_guard, raw_payload_evidence, replay, symbol_rows,
)
from trading_system.backtest.features import HistoricalFeatureScreenSource
from trading_system.config import StrategyConfig, _read_yaml
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.fundamentals.quality import FundamentalAccountingState, attach_market_price
from trading_system.models.fundamentals import (
    BalanceSheetSnapshot, CompanyIdentity, FundamentalFact, FundamentalMetrics, TTMFundamentals,
)
from trading_system.models.market_data import DailyBar, TradableAsset
from trading_system.strategy.scoring import score_quality, score_valuation


AS_OF = date(2022, 1, 3)


def config():
    path = Path(__file__).resolve().parents[1] / "config/strategy.yaml"
    return StrategyConfig.model_validate(_read_yaml(path))


def fact(metric="revenue", filed=AS_OF, value="10", start=date(2021, 1, 1), end=date(2021, 3, 31)):
    return FundamentalFact(cik="0000000001", symbol="AAA", metric=metric, tag=metric,
        value=Decimal(value), unit="USD/shares" if metric == "eps_diluted" else "USD",
        period_start=start, period_end=end, filed=filed, form="10-Q")


def state(**updates):
    current = TTMFundamentals(revenue=100, operating_income=20, eps_diluted=2,
        operating_cash_flow=30, capital_expenditures=5, free_cash_flow=25,
        depreciation_amortization=2, ebit=20, ebitda=22, net_income=15, tax_expense=5)
    prior = TTMFundamentals(revenue=90, eps_diluted=1, operating_cash_flow=25)
    balance = BalanceSheetSnapshot(period_end=date(2021, 9, 30), total_debt=20,
        total_equity=80, cash=10, shares_outstanding=100)
    return FundamentalAccountingState(updates.get("current", current), updates.get("prior", prior),
        updates.get("balance", balance), updates.get("prior_balance", balance))


def diagnoses(evidence, sic="3571", medians=None):
    cfg = config()
    metrics = attach_market_price(evidence.state, Decimal("10"))
    q = score_quality(metrics, {}, cfg.scores)
    v = score_valuation(metrics, medians or {}, {}, cfg.scores)
    view = SimpleNamespace(scores=SimpleNamespace(quality=q, valuation=v))
    candidate = SimpleNamespace(fundamentals=metrics, company=SimpleNamespace(sic=sic))
    return diagnose_factors(view, candidate, None, medians or {}, evidence), view


def test_read_only_guard_and_no_mutation(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE fixture (value INTEGER)")
        c.execute("INSERT INTO fixture VALUES (7)")
    before = digest(path)
    with local_guard() as safety:
        db = AuditDatabase(path)
        with db.connect() as c:
            assert c.execute("SELECT value FROM fixture").fetchone()[0] == 7
            assert c.execute("PRAGMA query_only").fetchone()[0] == 1
            for sql in ("INSERT INTO fixture VALUES (8)", "UPDATE fixture SET value=9",
                        "DELETE FROM fixture", "ALTER TABLE fixture ADD COLUMN x INT",
                        "PRAGMA journal_mode=WAL"):
                with pytest.raises(sqlite3.DatabaseError):
                    c.execute(sql)
        with pytest.raises(RuntimeError, match="MODE_RO"):
            sqlite3.connect(path)
        with pytest.raises(RuntimeError, match="INITIALIZE"):
            db.initialize()
    assert digest(path) == before
    assert safety["database_writes"] == 0
    assert safety["blocked_database_writes"] >= 6


@pytest.mark.parametrize("module,name", [
    ("sec_client", "SecClient"), ("alpaca_client", "AlpacaDataClient"),
    ("massive_reference", "MassiveReferenceClient"),
    ("massive_identity_research", "MassiveIdentityResearchClient"),
])
def test_no_provider_or_massive_client_construction(module, name):
    import importlib
    with local_guard() as safety:
        cls = getattr(importlib.import_module("trading_system.data." + module), name)
        with pytest.raises(RuntimeError, match="CONSTRUCTION_FORBIDDEN"):
            cls()
    assert safety["blocked_provider_constructions"] == 1
    assert safety["provider_calls"] == safety["network_calls"] == 0


def test_network_guard_blocks_before_transport():
    import socket
    with local_guard() as safety:
        with pytest.raises(RuntimeError, match="NETWORK_FORBIDDEN"):
            socket.getaddrinfo("invalid.test", 443)
    assert safety["blocked_network_attempts"] == 1
    assert safety["network_calls"] == 0


def test_pit_filed_cutoff_and_no_future_cache_facts():
    future = fact(filed=AS_OF + timedelta(days=1))
    before = AccountingEvidence([future], AS_OF, cache_facts=[future])
    assert not before.facts and not before.cache_facts
    assert {i.category for i in before.flow("revenue")} == {RAW}
    at_filing = AccountingEvidence([future], future.filed)
    assert {i.category for i in at_filing.flow("revenue")} == {DERIVED}


def test_raw_vs_derived_ttm_and_local_cache_evidence():
    assert AccountingEvidence([], AS_OF).flow("revenue")[0].category == RAW
    present = AccountingEvidence([fact()], AS_OF)
    issue = present.flow("revenue")[0]
    assert issue.category == DERIVED
    assert "fewer_than_four" in issue.detail
    cached = AccountingEvidence([], AS_OF, cache_facts=[fact()])
    assert cached.flow("revenue")[0].category == DERIVED


def ifrs_payload(filed="2021-12-01"):
    return {"cik": 1, "facts": {"ifrs-full": {"Revenue": {"units": {"USD": [
        {"start": "2021-01-01", "end": "2021-09-30", "filed": filed,
         "val": 100, "form": "20-F"}]}}}}}


def test_unmapped_local_ifrs_is_derived_or_unknown_not_download_absence():
    raw = raw_payload_evidence(ifrs_payload(), "AAA")
    evidence = AccountingEvidence([], AS_OF, raw_evidence=raw)
    assert evidence.flow("revenue")[0].category == DERIVED
    assert evidence.balance("total_debt")[0].category == UNKNOWN
    assert RAW not in {i.category for i in evidence.quality("roic")}


def test_unmapped_ifrs_future_filing_cannot_supply_historical_raw_evidence():
    raw = raw_payload_evidence(ifrs_payload("2022-01-04"), "AAA")
    assert AccountingEvidence([], AS_OF, raw_evidence=raw).flow("revenue")[0].category == RAW
    assert AccountingEvidence([], date(2022, 1, 4), raw_evidence=raw).flow("revenue")[0].category == DERIVED


def test_funds_and_unverified_historical_issuer_mapping_are_separate_review(tmp_path):
    path = tmp_path / "identity.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE security_reference(symbol TEXT,security_type TEXT)")
        connection.execute("INSERT INTO security_reference VALUES ('ETF','ETF')")
    metrics = defaultdict(dict, {"NEW": {"revenue": {"first_filed": "2024-01-01"}},
                                "OLD": {"revenue": {"first_filed": "2020-01-01"}}})
    bars = {s: {"daily_bars_in_2022": 100} for s in ("ETF", "NEW", "OLD")}
    with local_guard():
        issues = identity_diagnostic_issues(AuditDatabase(path), bars, bars, metrics)
    assert set(issues) == {"ETF", "NEW"}
    assert "FUND_CLASSIFICATION" in issues["ETF"]
    assert "UNVERIFIED" in issues["NEW"]


@pytest.mark.parametrize("factor", ["revenue_growth", "eps_growth", "operating_cash_flow",
    "operating_margin", "roic", "debt_to_ebitda"])
def test_quality_factor_reason_classification(factor):
    evidence = AccountingEvidence([], AS_OF)
    diag, view = diagnoses(evidence)
    assert not diag[factor]["available"]
    assert factor.upper() + "_UNAVAILABLE" in diag[factor]["reasons"]
    assert RAW in {i.category for i in diag[factor]["issues"]}
    assert view.scores.quality.score is None


def test_quality_peer_absence_and_positive_ocf_fallback_are_available():
    s = state(prior=TTMFundamentals(revenue=90, eps_diluted=1))
    diag, view = diagnoses(AccountingEvidence([], AS_OF, state=s))
    assert view.scores.quality.score is not None
    assert all(diag[f]["available"] for f in ("revenue_growth", "eps_growth", "operating_cash_flow", "roic"))
    ocf = next(f for f in view.scores.quality.factors if f.name == "operating_cash_flow")
    assert ocf.score == 50


def test_negative_ocf_is_available_score_zero_and_negative_fcf_is_valid():
    s = state()
    s = state(current=s.current.model_copy(update={"operating_cash_flow": Decimal(-10),
                                                   "free_cash_flow": Decimal(-15)}))
    diag, view = diagnoses(AccountingEvidence([], AS_OF, state=s))
    assert diag["operating_cash_flow"]["available"]
    assert next(f for f in view.scores.quality.factors if f.name == "operating_cash_flow").score == 0
    assert diag["fcf_yield"]["available"]


def test_growth_nonpositive_prior_is_economic():
    e = AccountingEvidence([], AS_OF, state=state(prior=TTMFundamentals(revenue=-1, eps_diluted=0)))
    for f in ("revenue_growth", "eps_growth"):
        assert {i.category for i in e.quality(f)} == {ECONOMIC}


def test_valuation_pe_denominator_and_peer_reasons():
    s = state()
    e = AccountingEvidence([], AS_OF, state=state(current=s.current.model_copy(update={"eps_diluted": Decimal(-1)})))
    diag, _ = diagnoses(e)
    pe = diag["relative_pe"]
    assert {ECONOMIC, PEER} <= {i.category for i in pe["issues"]}
    assert {"PE_INVALID_DENOMINATOR", "PEER_PE_MEDIAN_UNAVAILABLE"} <= set(pe["reasons"])


def test_identity_and_peer_not_download_gaps_when_company_metrics_present():
    e = AccountingEvidence([], AS_OF, state=state())
    diag, _ = diagnoses(e, sic=None)
    assert {i.category for i in diag["relative_pe"]["issues"]} == {IDENTITY}
    diag, _ = diagnoses(e)
    assert {i.category for i in diag["relative_ev_ebitda"]["issues"]} == {PEER}


def test_ev_ebit_fallback_prevents_false_missing_factor():
    s = state()
    e = AccountingEvidence([], AS_OF, state=state(current=s.current.model_copy(update={"ebitda": None})))
    diag, view = diagnoses(e, medians={"ev_to_ebit": 5})
    assert diag["relative_ev_ebitda"]["available"]
    assert not diag["relative_ev_ebitda"]["issues"]


def test_fcf_alignment_is_derived_not_missing():
    s = state()
    e = AccountingEvidence([], AS_OF, state=state(current=s.current.model_copy(update={"free_cash_flow": None})))
    issues = e.valuation_input("fcf_yield", FundamentalMetrics(market_cap=Decimal(100)))
    assert {i.category for i in issues} == {DERIVED}
    assert issues[0].detail == "OCF_capex_TTM_period_alignment"


def test_multireason_counted_once_and_economic_not_backfill():
    acc = GapAccumulator()
    issues = (Issue(RAW, "missing", ("cash",)), Issue(RAW, "missing2", ("equity",)),
              Issue(ECONOMIC, "denominator"), Issue(PEER, "median"))
    acc.add("AAA", AS_OF, "roic", {"available": False, "issues": issues, "reasons": ()}, "quality", True)
    row = acc.factor_rows()[0]
    assert row["raw_sec_missing"] == 1
    assert row["economically_undefined"] == row["peer_context_insufficient"] == 1
    assert not acc.backfill
    assert len(acc.evidence) == 4


def test_backfill_only_raw_missing_with_concrete_inputs():
    acc = GapAccumulator()
    for category, factor_name in ((RAW, "roic"), (DERIVED, "eps_growth"), (PEER, "relative_pe")):
        acc.add("AAA", AS_OF, factor_name, {"available": False,
            "issues": (Issue(category, "proof", ("cash",)),), "reasons": ()}, "quality", True)
    metrics = defaultdict(dict, {"AAA": {"revenue": {"fact_count": 12, "pit_fact_count": 8,
        "first_filed": "2020-01-01", "last_filed": "2025-01-01"}}})
    rows = backfill_rows(acc, {"AAA": {"cik": "0000000001"}}, metrics, set(), [AS_OF])
    assert len(rows) == 1 and rows[0]["affected_factor"] == "roic"
    assert rows[0]["existing_fact_count"] == 12
    assert rows[0]["relevant_missing_metric_inputs"] == "cash"


def test_session_aggregation_keeps_zero_sessions_and_quantiles():
    result = aggregate_sessions([{"session": "a", "final": 0}, {"session": "b", "final": 2}])
    assert result["final"]["mean"] == result["final"]["median"] == 1
    assert result["final"]["minimum"] == 0
    assert result["final"]["p10"] == pytest.approx(.2)


def fixture_database(tmp_path, last_session=date(2022, 1, 4)):
    path = tmp_path / "market.sqlite3"
    db = Database(path)
    db.initialize()
    db.upsert_assets([TradableAsset(symbol="AAA", name="AAA", tradable=True, fractionable=True)])
    db.upsert_company(CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA", sic="3571"))
    days = trading_sessions_between(date(2020, 7, 1), last_session)
    db.upsert_bars([DailyBar(symbol="AAA", timestamp=datetime.combine(d, datetime.min.time(), tzinfo=UTC),
        open=Decimal(100), high=Decimal(102), low=Decimal(99), close=Decimal(100), volume=2_000_000) for d in days])
    facts = []
    for year in (2020, 2021):
        for month in (1, 4, 7, 10):
            begin = date(year, month, 1)
            end = date(year + (month == 10), 1 if month == 10 else month + 3, 1) - timedelta(days=1)
            for metric in ("revenue", "eps_diluted", "operating_cash_flow", "operating_income"):
                facts.append(fact(metric, filed=end, start=begin, end=end))
    facts.append(fact("shares_outstanding", start=None, end=date(2021, 12, 31)).model_copy(
        update={"unit": "shares", "value": Decimal(20_000_000)}))
    facts.append(fact("tax_expense", filed=date(2022, 2, 1), value="999999"))
    db.upsert_facts(facts)
    return path


def test_small_production_screen_replay_and_symbol_aggregation(tmp_path):
    path = fixture_database(tmp_path)
    before = digest(path)
    sessions = [AS_OF, date(2022, 1, 4)]
    with local_guard() as safety:
        ro = AuditDatabase(path)
        companies, assets, bars, relevant, metrics, dates, caches = inventory(ro, {"AAA"})
        source = HistoricalFeatureScreenSource(ro, config(), sessions[0], sessions[-1])
        rows, acc, counts, primary, primary_rows, totals = replay(
            source, sessions, relevant, bars, dates, metrics, {}, set())
        symbols = symbol_rows(relevant, bars, companies, metrics, counts, primary, acc,
                              [], set(), set(), set(), {"AAA"})
    assert len(rows) == 2 and rows[0]["source_universe_count"] == 1
    assert len(symbols) == 1
    assert symbols[0]["quality_available_session_count"] == 2
    assert symbols[0]["valuation_available_session_count"] == 0
    assert symbols[0]["final_candidate_session_count"] == 0
    assert sum(totals.values()) == 2
    assert all(f.filed <= AS_OF for cache in source._facts.values() for f in cache.facts)
    assert digest(path) == before
    assert all(v == 0 for v in safety.values())


def test_monthly_chunk_boundary_preserves_canonical_full_screen(tmp_path):
    path = fixture_database(tmp_path, date(2022, 2, 2))
    dates = [AS_OF, date(2022, 1, 31), date(2022, 2, 1), date(2022, 2, 2)]
    with local_guard() as safety:
        db = AuditDatabase(path)
        full = HistoricalFeatureScreenSource(db, config(), dates[0], dates[-1])
        expected = {day: full.screen(day).records[0] for day in dates}
        for start, end in ((dates[0], dates[1]), (dates[2], dates[3])):
            part = HistoricalFeatureScreenSource(db, config(), start, end)
            for day in (start, end):
                actual = part.screen(day).records[0]
                assert actual.fundamentals == expected[day].fundamentals
                assert actual.technical == expected[day].technical
                assert actual.scores == expected[day].scores
                assert actual.exclusion_reasons == expected[day].exclusion_reasons
    assert all(v == 0 for v in safety.values())


def test_future_load_horizon_can_prune_old_pit_inputs_without_using_future_facts(tmp_path):
    path = tmp_path / "pruning.sqlite3"
    db = Database(path)
    db.initialize()
    facts = []
    for index in range(4):
        start = date(2017, 1 + 3 * index, 1)
        end = date(2018 if index == 3 else 2017, 1 if index == 3 else 4 + 3 * index, 1) - timedelta(days=1)
        facts.append(fact("operating_cash_flow", start=start, end=end, filed=date(2018, 2, 1)))
    for index in range(25):
        year, quarter = 2019 + index // 4, index % 4
        start = date(year, quarter * 3 + 1, 1)
        end = date(year + (quarter == 3), 1 if quarter == 3 else quarter * 3 + 4, 1) - timedelta(days=1)
        facts.append(fact("operating_cash_flow", start=start, end=end, filed=date(2026, 6, 1)))
    db.upsert_facts(facts)
    with local_guard():
        ro = AuditDatabase(path)
        streams = []
        for horizon in (date(2026, 8, 12), date(2022, 12, 30)):
            loaded = [f for batch in ro.iter_fact_batches(["AAA"], horizon,
                period_end_on_or_after=date(2018, 1, 1), retain_latest_periods=24) for f in batch]
            streams.append([f for f in loaded if f.filed <= AS_OF])
        assert not streams[0]
        assert len(streams[1]) == 4
        assert all(f.filed <= AS_OF for f in streams[1])
        assert AccountingEvidence(streams[0], AS_OF).state.current.operating_cash_flow is None
        assert AccountingEvidence(streams[1], AS_OF).state.current.operating_cash_flow == 40
