from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from trading_system.config import DataQualityConfig, FilterConfig, PeerConfig, load_settings
from trading_system.data.database import Database
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact
from trading_system.models.market_data import DailyBar, MarketSnapshot, TradableAsset
from trading_system.strategy import screener as screener_module
from trading_system.strategy.reporting import (
    export_report,
    format_explanation,
    format_screen_table,
    load_report,
)
from trading_system.strategy.screener import Screener


def _quarter_ends(year: int) -> list[tuple[date, date]]:
    return [
        (date(year, 1, 1), date(year, 3, 31)),
        (date(year, 4, 1), date(year, 6, 30)),
        (date(year, 7, 1), date(year, 9, 30)),
        (date(year, 10, 1), date(year, 12, 31)),
    ]


def _facts(symbol: str, cik: str, scale: Decimal) -> list[FundamentalFact]:
    facts: list[FundamentalFact] = []
    values = {
        "revenue": Decimal("250000000"),
        "operating_income": Decimal("50000000"),
        "net_income": Decimal("30000000"),
        "eps_diluted": Decimal("0.50"),
        "operating_cash_flow": Decimal("45000000"),
        "capital_expenditures": Decimal("10000000"),
        "depreciation_amortization": Decimal("5000000"),
        "tax_expense": Decimal("10000000"),
        "interest_expense": Decimal("2000000"),
    }
    for year in (2023, 2024):
        growth = Decimal("1.20") if year == 2024 else Decimal(1)
        for quarter, (start, end) in enumerate(_quarter_ends(year), start=1):
            filed = end + timedelta(days=45)
            for metric, base_value in values.items():
                unit = "USD/shares" if metric == "eps_diluted" else "USD"
                facts.append(
                    FundamentalFact(
                        cik=cik,
                        symbol=symbol,
                        metric=metric,
                        tag=metric,
                        value=base_value * scale * growth,
                        unit=unit,
                        period_start=start,
                        period_end=end,
                        filed=filed,
                        fiscal_year=year,
                        fiscal_period=f"Q{quarter}" if quarter < 4 else "FY",
                        form="10-Q" if quarter < 4 else "10-K",
                        accession_number=f"{symbol}-{metric}-{year}-{quarter}",
                    )
                )
    for year, filed in ((2023, date(2024, 2, 15)), (2024, date(2025, 2, 14))):
        balance_values = {
            "cash": Decimal("100000000"),
            "total_debt": Decimal("200000000"),
            "total_equity": Decimal("1000000000"),
            "current_assets": Decimal("500000000"),
            "current_liabilities": Decimal("250000000"),
            "total_assets": Decimal("1500000000"),
            "shares_outstanding": Decimal("20000000"),
        }
        for metric, value in balance_values.items():
            facts.append(
                FundamentalFact(
                    cik=cik,
                    symbol=symbol,
                    metric=metric,
                    tag=metric,
                    value=value * scale if metric != "shares_outstanding" else value,
                    unit="shares" if metric == "shares_outstanding" else "USD",
                    period_end=date(year, 12, 31),
                    filed=filed,
                    fiscal_year=year,
                    fiscal_period="FY",
                    form="10-K",
                    accession_number=f"{symbol}-{metric}-{year}",
                )
            )
    return facts


def _bars(symbol: str, offset: Decimal) -> list[DailyBar]:
    start = datetime(2024, 4, 1, tzinfo=UTC)
    output: list[DailyBar] = []
    for index in range(320):
        close = Decimal("90") + offset + Decimal(index) / Decimal("20")
        output.append(
            DailyBar(
                symbol=symbol,
                timestamp=start + timedelta(days=index),
                open=close - 1,
                high=close + 2,
                low=close - 2,
                close=close,
                volume=250_000 + index,
            )
        )
    return output


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "screen.sqlite3")
    database.initialize()
    for index, (symbol, scale) in enumerate(
        (("AAA", Decimal("1.0")), ("BBB", Decimal("1.1"))), start=1
    ):
        cik = f"{index:010d}"
        database.upsert_assets(
            [
                TradableAsset(
                    symbol=symbol,
                    name=f"{symbol} Corp",
                    exchange="NASDAQ",
                    tradable=True,
                    fractionable=True,
                )
            ]
        )
        database.upsert_company(
            CompanyIdentity(cik=cik, symbol=symbol, name=f"{symbol} Corp", sic="3571")
        )
        database.upsert_facts(_facts(symbol, cik, scale))
        database.upsert_bars(_bars(symbol, Decimal(index)))
    return database


def _test_config():
    load_settings.cache_clear()
    strategy = load_settings().strategy
    return strategy.model_copy(
        update={
            "peers": PeerConfig(min_peer_count=2),
            "filters": FilterConfig(min_quality_score=0, min_valuation_score=0, min_total_score=0),
            "data_quality": DataQualityConfig(
                min_available_quality_metrics=4,
                min_available_valuation_metrics=2,
                min_market_history_days=300,
            ),
        }
    )


def test_screener_ranks_and_preserves_raw_metrics(tmp_path) -> None:
    report = Screener(_database(tmp_path), _test_config()).run(date(2025, 2, 14))
    assert report.analyzed_count == report.eligible_count == 2
    assert [record.rank for record in report.records] == [1, 2]
    assert all(record.peer_group == "sic4:3571" for record in report.records)
    assert all(record.scores.total is not None for record in report.records)
    assert all(record.fundamentals.revenue_growth is not None for record in report.records)
    assert all(record.technical.sma200 is not None for record in report.records)


def test_reports_include_factor_details_and_explanation(tmp_path) -> None:
    report = Screener(_database(tmp_path), _test_config()).run(date(2025, 2, 14))
    csv_path, json_path = export_report(report, tmp_path / "reports")
    restored = load_report(json_path)
    csv = pd.read_csv(csv_path)
    explanation = format_explanation(restored.records[0])

    assert restored == report
    assert "factor_quality_revenue_growth_score" in csv.columns
    assert "industry_median_pe" in csv.columns
    assert "QUALITY:" in explanation
    assert "Industry Median P/E:" in explanation
    assert "TOTAL:" in explanation
    assert "Rank" in format_screen_table(report)


def test_historical_screen_excludes_future_filings_and_bars(tmp_path) -> None:
    database = _database(tmp_path)
    early = Screener(database, _test_config()).run(date(2024, 12, 31))
    assert early.eligible_count == 0
    assert all(record.fundamentals.revenue_growth is None for record in early.records)
    assert all(
        "insufficient_market_history" in record.exclusion_reasons for record in early.records
    )


def test_raw_cache_cleanup_does_not_change_screening_results(tmp_path) -> None:
    database = _database(tmp_path)
    database.cache_sec_payload("0000000001", "companyfacts", {"raw": "AAA"})
    database.cache_sec_payload("0000000002", "companyfacts", {"raw": "BBB"})
    screener = Screener(database, _test_config())
    before = screener.run(date(2025, 2, 14))

    cleanup = database.cleanup_raw_sec_cache(dry_run=False)
    after = screener.run(date(2025, 2, 14))

    assert cleanup["deleted_rows"] == 2
    assert before.model_dump(exclude={"generated_at"}) == after.model_dump(exclude={"generated_at"})


def test_identity_conflict_is_excluded_before_fundamental_or_technical_analysis(
    tmp_path, monkeypatch
) -> None:
    database = _database(tmp_path)
    database.set_sync_value(
        "sec_reference",
        "ticker_to_cik",
        {"AAA": "0000009999", "BBB": "0000000002"},
    )
    original_bars = database.bars_available_as_of
    original_facts = database.facts_available_as_of
    original_snapshot = database.latest_market_snapshot
    original_analyze = screener_module.analyze_fundamentals
    original_technical = screener_module.technical_snapshot
    analyzed_symbols: list[str] = []
    technical_calls = 0

    def guarded_bars(symbol, *args, **kwargs):
        assert symbol != "AAA"
        return original_bars(symbol, *args, **kwargs)

    def guarded_facts(symbol, *args, **kwargs):
        assert symbol != "AAA"
        return original_facts(symbol, *args, **kwargs)

    def guarded_snapshot(symbol):
        assert symbol != "AAA"
        return original_snapshot(symbol)

    def tracked_analyze(facts, *args, **kwargs):
        analyzed_symbols.append(facts[0].symbol if facts else "")
        return original_analyze(facts, *args, **kwargs)

    def tracked_technical(*args, **kwargs):
        nonlocal technical_calls
        technical_calls += 1
        return original_technical(*args, **kwargs)

    monkeypatch.setattr(database, "bars_available_as_of", guarded_bars)
    monkeypatch.setattr(database, "facts_available_as_of", guarded_facts)
    monkeypatch.setattr(database, "latest_market_snapshot", guarded_snapshot)
    monkeypatch.setattr(screener_module, "analyze_fundamentals", tracked_analyze)
    monkeypatch.setattr(screener_module, "technical_snapshot", tracked_technical)
    before_facts = original_facts("AAA", date(2025, 2, 14))
    before_bars = original_bars("AAA", date(2025, 2, 14))

    current = Screener(database, _test_config()).run(date(2025, 2, 14))
    historical = Screener(database, _test_config()).run(date(2024, 12, 31))

    current_conflict = next(record for record in current.records if record.symbol == "AAA")
    historical_conflict = next(record for record in historical.records if record.symbol == "AAA")
    assert current.identity_conflicts_excluded == historical.identity_conflicts_excluded == 1
    assert current.identity_conflict_sample == historical.identity_conflict_sample == ("AAA",)
    assert (
        current_conflict.exclusion_reasons
        == historical_conflict.exclusion_reasons
        == ("identity_conflict",)
    )
    assert not current_conflict.eligible and current_conflict.scores.total is None
    assert current.analyzed_count == 2
    assert current.eligible_count == sum(record.eligible for record in current.records)
    assert analyzed_symbols == ["BBB", "BBB"]
    assert technical_calls == 2
    assert original_facts("AAA", date(2025, 2, 14)) == before_facts
    assert original_bars("AAA", date(2025, 2, 14)) == before_bars

    database.set_sync_value(
        "sec_reference",
        "ticker_to_cik",
        {"AAA": "0000000001", "BBB": "0000000002"},
    )
    monkeypatch.setattr(database, "bars_available_as_of", original_bars)
    monkeypatch.setattr(database, "facts_available_as_of", original_facts)
    monkeypatch.setattr(database, "latest_market_snapshot", original_snapshot)
    monkeypatch.setattr(screener_module, "analyze_fundamentals", original_analyze)
    monkeypatch.setattr(screener_module, "technical_snapshot", original_technical)
    resolved = Screener(database, _test_config()).run(date(2025, 2, 14))

    resolved_aaa = next(record for record in resolved.records if record.symbol == "AAA")
    assert resolved.identity_conflicts_excluded == 0
    assert "identity_conflict" not in resolved_aaa.exclusion_reasons


def test_peer_debug_uses_full_local_screening_universe(tmp_path) -> None:
    debug = Screener(_database(tmp_path), _test_config()).debug_peers("AAA", date(2025, 2, 14))
    assert debug is not None
    assert debug.exact_peer_count == 2
    assert debug.selected_group == "sic4:3571"
    assert debug.valid_pe_count == 2


def test_screener_uses_only_snapshot_trade_from_completed_analysis_session(tmp_path) -> None:
    database = _database(tmp_path)
    observed = datetime(2025, 2, 15, 8, tzinfo=UTC)
    database.upsert_market_snapshots(
        [
            MarketSnapshot(
                symbol="AAA",
                observed_at=observed,
                latest_trade_price=Decimal("200"),
                latest_trade_timestamp=datetime(2025, 2, 14, 21, tzinfo=UTC),
            ),
            MarketSnapshot(
                symbol="BBB",
                observed_at=observed,
                latest_trade_price=Decimal("999"),
                latest_trade_timestamp=datetime(2025, 2, 15, 8, tzinfo=UTC),
            ),
        ]
    )

    report = Screener(database, _test_config()).run(date(2025, 2, 14))
    by_symbol = {record.symbol: record for record in report.records}
    assert by_symbol["AAA"].fundamentals.market_cap == Decimal("4000000000")
    assert by_symbol["AAA"].technical.price == 200
    assert by_symbol["BBB"].fundamentals.market_cap != Decimal("19980000000")
