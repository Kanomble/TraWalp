from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.backtest.engine import HistoricalScreenSource
from trading_system.backtest.features import (
    HistoricalFeatureScreenSource,
    HistoricalPerformanceDiagnostics,
    _AccountingCache,
    _fast_technical_snapshot,
)
from trading_system.config import DataQualityConfig, FilterConfig, PeerConfig, load_settings
from trading_system.data.database import Database
from trading_system.fundamentals.quality import analyze_fundamentals
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact
from trading_system.models.market_data import DailyBar, TradableAsset
from trading_system.strategy.screener import _bar_frame
from trading_system.technical.momentum import technical_snapshot


def _bars(symbol: str, count: int = 321) -> list[DailyBar]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        DailyBar(
            symbol=symbol,
            timestamp=start + timedelta(days=index),
            open=Decimal("90") + Decimal(index) / 20,
            high=Decimal("93") + Decimal(index) / 20,
            low=Decimal("89") + Decimal(index) / 20,
            close=Decimal("91") + Decimal(index) / 20 + Decimal(index % 7) / 10,
            volume=2_000_000 + index * 1_000,
        )
        for index in range(count)
    ]


def _facts(symbol: str, cik: str) -> list[FundamentalFact]:
    output: list[FundamentalFact] = []
    flow_values = {
        "revenue": "300000000",
        "operating_income": "60000000",
        "net_income": "40000000",
        "eps_diluted": "1.00",
        "operating_cash_flow": "55000000",
        "capital_expenditures": "10000000",
        "depreciation_amortization": "5000000",
        "tax_expense": "10000000",
        "interest_expense": "2000000",
    }
    for index in range(8):
        period_start = date(2022 + index // 4, (index % 4) * 3 + 1, 1)
        period_end = period_start + timedelta(days=89)
        filed = period_end + timedelta(days=40)
        for metric, value in flow_values.items():
            output.append(
                FundamentalFact(
                    cik=cik,
                    symbol=symbol,
                    metric=metric,
                    tag=metric,
                    value=Decimal(value) * (Decimal("1.1") if index >= 4 else Decimal(1)),
                    unit="USD/shares" if metric == "eps_diluted" else "USD",
                    period_start=period_start,
                    period_end=period_end,
                    filed=filed,
                    form="10-Q",
                    accession_number=f"{symbol}-{metric}-{index}",
                )
            )
    for metric, value, unit in (
        ("cash", "100000000", "USD"),
        ("total_debt", "150000000", "USD"),
        ("total_equity", "900000000", "USD"),
        ("current_assets", "400000000", "USD"),
        ("current_liabilities", "200000000", "USD"),
        ("total_assets", "1300000000", "USD"),
        ("shares_outstanding", "20000000", "shares"),
    ):
        output.append(
            FundamentalFact(
                cik=cik,
                symbol=symbol,
                metric=metric,
                tag=metric,
                value=Decimal(value),
                unit=unit,
                period_end=date(2023, 12, 31),
                filed=date(2024, 2, 15),
                form="10-K",
                accession_number=f"{symbol}-{metric}-balance",
            )
        )
    return output


def _database(tmp_path) -> tuple[Database, date]:
    database = Database(tmp_path / "features.sqlite3")
    database.initialize()
    bars = _bars("AAA")
    database.upsert_assets(
        [TradableAsset(symbol="AAA", name="AAA", tradable=True, fractionable=True)]
    )
    database.upsert_company(CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA", sic="3571"))
    database.upsert_bars(bars)
    database.upsert_facts(_facts("AAA", "0000000001"))
    return database, bars[-2].timestamp.date()


def _config():
    load_settings.cache_clear()
    strategy = load_settings().strategy
    return strategy.model_copy(
        update={
            "peers": PeerConfig(min_peer_count=2),
            "filters": FilterConfig(min_quality_score=0, min_valuation_score=0, min_total_score=0),
            "data_quality": DataQualityConfig(
                min_available_quality_metrics=1,
                min_available_valuation_metrics=1,
                min_market_history_days=300,
            ),
        }
    )


@pytest.mark.parametrize("length", [14, 15, 20, 50, 62, 63, 125, 126, 199, 200, 252, 320])
def test_fast_technical_snapshot_matches_canonical_prefix(length: int) -> None:
    bars = _bars("AAA", length)
    config = _config()

    legacy = technical_snapshot(_bar_frame(bars), config.technical)
    optimized = _fast_technical_snapshot(bars, config)

    for name in type(legacy).model_fields:
        expected = getattr(legacy, name)
        actual = getattr(optimized, name)
        if isinstance(expected, float):
            assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12)
        else:
            assert actual == expected


@pytest.mark.parametrize(
    "closes",
    [
        [100.0] * 300,
        [50.0 + index * 0.5 for index in range(300)],
        [300.0 - index * 0.5 for index in range(300)],
        [100.0] * 150 + [70.0] * 30 + [70.0 + index * 0.5 for index in range(120)],
        [100.0] * 250 + [100.0 - index * 0.3 for index in range(50)],
    ],
    ids=["flat", "uptrend", "downtrend", "decline-recovery", "pullback"],
)
def test_new_technical_features_match_on_synthetic_price_paths(closes: list[float]) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bars = [
        DailyBar(
            symbol="AAA",
            timestamp=start + timedelta(days=index),
            open=Decimal(str(close)),
            high=Decimal(str(close + 1)),
            low=Decimal(str(close - 1)),
            close=Decimal(str(close)),
            volume=2_000_000,
        )
        for index, close in enumerate(closes)
    ]
    config = _config()
    canonical = technical_snapshot(_bar_frame(bars), config.technical)
    optimized = _fast_technical_snapshot(bars, config)

    for name in (
        "drawdown_63d",
        "recovery_from_63d_low",
        "max_drawdown_126d",
        "sma200_distance",
    ):
        assert getattr(optimized, name) == pytest.approx(
            getattr(canonical, name), rel=1e-12, abs=1e-12
        )


def test_accounting_cache_is_filing_aware_and_cannot_leak_later_filing() -> None:
    facts = _facts("AAA", "0000000001")
    later = facts[0].model_copy(
        update={
            "value": Decimal("9999999999"),
            "filed": date(2025, 1, 10),
            "accession_number": "later-amendment",
        }
    )
    ordered = sorted([*facts, later], key=lambda fact: (fact.filed, fact.period_end))
    cache = _AccountingCache(ordered)
    diagnostics = HistoricalPerformanceDiagnostics()
    early = date(2024, 12, 31)

    before = cache.metrics(early, Decimal("100"), diagnostics)
    expected = analyze_fundamentals(
        [fact for fact in ordered if fact.filed <= early], early, Decimal("100")
    )
    after = cache.metrics(date(2025, 1, 10), Decimal("100"), diagnostics)
    repeated = cache.metrics(early, Decimal("100"), diagnostics)

    assert before == expected == repeated
    assert after != before
    assert diagnostics.feature_cache_misses == 2
    assert diagnostics.feature_cache_hits == 1


def test_optimized_historical_screen_matches_legacy_and_batches_queries(tmp_path) -> None:
    database, session = _database(tmp_path)
    config = _config()

    legacy = HistoricalScreenSource(database, config).screen(session)
    optimized_source = HistoricalFeatureScreenSource(database, config, session, session)
    optimized = optimized_source.screen(session)

    assert [record.symbol for record in optimized.records] == [
        record.symbol for record in legacy.records
    ]
    left = legacy.records[0]
    right = optimized.records[0]
    assert right.eligible == left.eligible
    assert right.exclusion_reasons == left.exclusion_reasons
    assert right.peer_group == left.peer_group
    assert right.fundamentals == left.fundamentals
    for name in type(left.technical).model_fields:
        expected = getattr(left.technical, name)
        actual = getattr(right.technical, name)
        if isinstance(expected, float):
            assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12)
        else:
            assert actual == expected
    assert right.scores.total == pytest.approx(left.scores.total or 0, abs=1e-12)
    for component in ("quality", "valuation", "opportunity", "timing"):
        expected_component = getattr(left.scores, component)
        actual_component = getattr(right.scores, component)
        assert actual_component.name == expected_component.name
        assert actual_component.score == pytest.approx(expected_component.score or 0, abs=1e-12)
        assert [factor.name for factor in actual_component.factors] == [
            factor.name for factor in expected_component.factors
        ]
        for actual_factor, expected_factor in zip(
            actual_component.factors, expected_component.factors, strict=True
        ):
            if expected_factor.raw_value is not None:
                assert actual_factor.raw_value == pytest.approx(
                    expected_factor.raw_value, abs=1e-12
                )
            assert actual_factor.score == pytest.approx(expected_factor.score, abs=1e-12)
    assert optimized_source.diagnostics.sqlite_query_count <= 8
    assert optimized_source.diagnostics.companies_after_market == 1


def test_future_bar_in_prepared_run_does_not_change_earlier_screen(tmp_path) -> None:
    database, early = _database(tmp_path)
    future = early + timedelta(days=1)
    config = _config()

    early_only = HistoricalFeatureScreenSource(database, config, early, early).screen(early)
    through_future = HistoricalFeatureScreenSource(database, config, early, future).screen(early)

    assert through_future.records[0].technical == early_only.records[0].technical
    assert through_future.records[0].scores == early_only.records[0].scores
