import json
import sqlite3
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from trading_system import cli
from trading_system.backtest import sec_backfill_pilot as pilot
from trading_system.data import sec_force
from trading_system.data.database import Database
from trading_system.data.sec_client import SecClient
from trading_system.data.sync import DataSynchronizer
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.market_data import TradableAsset


@pytest.fixture
def local_scope(tmp_path):
    database = Database(tmp_path / "fixture.sqlite3")
    database.initialize()
    rows = []
    for index, symbol in enumerate(("AAA", "BBB", "CCC"), 1):
        database.upsert_assets(
            [
                TradableAsset(
                    symbol=symbol,
                    name=f"{symbol} Corp",
                    exchange="NASDAQ",
                    tradable=True,
                    fractionable=False,
                )
            ]
        )
        database.upsert_company(
            CompanyIdentity(cik=f"{index:010}", symbol=symbol, name=f"{symbol} Corp", sic="3571")
        )
        rows.append(
            {
                "symbol": symbol,
                "cik": f"{index:010}",
                "priority": "HIGH",
                "affected_sessions": "251",
                "affected_factor": "roic",
                "relevant_missing_metric_inputs": "operating_income",
            }
        )
    manifest = tmp_path / "candidates.csv"
    pilot.dump_csv(manifest, rows)
    stem = sec_force.AUDIT_STEM
    pilot.dump_json(tmp_path / f"{stem}_summary.json", {})
    pilot.dump_json(tmp_path / f"{stem}_universe_identity_summary.json", {})
    pilot.dump_csv(
        tmp_path / f"{stem}_symbol_coverage.csv",
        [{"symbol": row["symbol"], "identity_review_candidate": False} for row in rows],
    )
    return database, manifest


def deny(*args, **kwargs):
    raise AssertionError("Unexpected network, database initialization, or provider construction")


def test_cli_dry_run_zero_network_zero_database_writes(local_scope, monkeypatch, capsys):
    database, manifest = local_scope
    before = pilot.audit.digest(database.path)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda _: SimpleNamespace(
            strategy=SimpleNamespace(storage=SimpleNamespace(database_path=database.path))
        ),
    )
    monkeypatch.setattr(Database, "initialize", deny)
    monkeypatch.setattr(SecClient, "__init__", deny)
    monkeypatch.setattr(requests.sessions.Session, "request", deny)
    with pilot.audit.local_guard():
        assert (
            cli.main(["sync-sec-force", "--manifest", str(manifest), "--limit", "2", "--dry-run"])
            == 0
        )
    output = json.loads(capsys.readouterr().out)
    assert output["network_calls"] == output["database_writes"] == 0
    assert output["resolved_symbols"] == ["AAA", "BBB"]
    assert output["selected"][0]["cik"] == "0000000001"
    assert before == pilot.audit.digest(database.path)


def test_candidate_order_deterministic_and_factor_counts_not_double_counted():
    rows = [
        {
            "symbol": s,
            "cik": "1",
            "priority": p,
            "affected_sessions": n,
            "affected_factor": f,
            "relevant_missing_metric_inputs": "x",
        }
        for s, p, n, f in [
            ("AAA", "MEDIUM", 251, "roic"),
            ("BBB", "HIGH", 200, "roic"),
            ("DDD", "HIGH", 200, "roic"),
            ("CCC", "HIGH", 200, "roic"),
            ("DDD", "HIGH", 200, "fcf_yield"),
        ]
    ]
    ordered = sec_force.candidate_order(rows)
    assert [row["symbol"] for row in ordered] == ["DDD", "BBB", "CCC", "AAA"]
    assert ordered[0]["affected_sessions"] == 200
    assert sec_force.candidate_order(list(reversed(rows))) == ordered


def test_session_union_ranking_rejects_ambiguous_top_selection():
    rows = [
        {
            "symbol": s,
            "cik": "1",
            "priority": "HIGH",
            "affected_sessions": n,
            "affected_factor": f,
            "relevant_missing_metric_inputs": "x",
        }
        for s, n, f in [("AAA", 10, "roic"), ("BBB", 9, "roic"), ("BBB", 9, "fcf_yield")]
    ]
    ranked = sec_force.candidate_order(rows)
    with pytest.raises(ValueError, match="AMBIGUOUS_TOP_SESSION_UNION"):
        sec_force.verify_session_union_ranking(ranked, rows, {}, {"AAA"}, 251)


def test_session_union_ranking_uses_scoring_gate_upper_bound():
    rows = [
        {
            "symbol": s,
            "cik": "1",
            "priority": "HIGH",
            "affected_sessions": n,
            "affected_factor": f,
            "relevant_missing_metric_inputs": "x",
        }
        for s, n, f in [
            ("AAA", 250, "roic"),
            ("AAA", 250, "fcf_yield"),
            ("BBB", 155, "roic"),
            ("BBB", 35, "fcf_yield"),
        ]
    ]
    ranked = sec_force.candidate_order(rows)
    proof = sec_force.verify_session_union_ranking(
        ranked, rows, {"AAA": {"fundamentals_evaluated_session_count": 250}}, {"AAA"}, 251
    )
    assert proof["top_selection_proven"]
    assert proof["bounds"]["AAA"] == {"lower": 250, "upper": 250}
    assert proof["bounds"]["BBB"] == {"lower": 155, "upper": 190}


@pytest.mark.parametrize(
    "symbols,limit", [(["UNKNOWN"], 15), (["AAA", "AAA"], 15), (["AAA", "BBB"], 1), (None, 16)]
)
def test_invalid_scope_fails_closed(local_scope, symbols, limit):
    database, manifest = local_scope
    with pytest.raises(ValueError):
        sec_force.prepare_plan(database.path, manifest, symbols=symbols, limit=limit)


def test_manifest_skips_identity_ifrs_and_funds(local_scope):
    database, manifest = local_scope
    pilot.dump_json(
        manifest.parent / f"{sec_force.AUDIT_STEM}_summary.json",
        {"local_raw_cache_diagnostics": {"AAA": {"taxonomy_PIT_counts": {"ifrs-full": 10}}}},
    )
    pilot.dump_json(
        manifest.parent / f"{sec_force.AUDIT_STEM}_universe_identity_summary.json",
        {"historical_current_CIK_conflicts": [{"symbol": "BBB"}]},
    )
    plan = sec_force.prepare_plan(database.path, manifest, limit=2)
    assert plan["resolved_symbols"] == ["CCC"]
    assert [r["reason"] for r in plan["skipped"]] == [
        "KNOWN_IFRS_MAPPING_CASE",
        "AUDIT_IDENTITY_REVIEW_REQUIRED",
    ]
    with pytest.raises(ValueError, match="EXPLICIT_SYMBOL_REJECTED"):
        sec_force.prepare_plan(database.path, manifest, symbols=["BBB"])
    database.upsert_assets(
        [
            TradableAsset(
                symbol="CCC", name="CCC ETF", exchange="NASDAQ", tradable=True, fractionable=False
            )
        ]
    )
    with pytest.raises(ValueError, match="NO_VALID_CANDIDATES"):
        sec_force.prepare_plan(database.path, manifest)


def test_local_cik_conflict_unresolved_and_empty_scope_fail(local_scope):
    database, _ = local_scope
    database.set_sync_value("sec_reference", "ticker_to_cik", {"AAA": "0000000009"})
    for symbols in (["AAA"], ["UNKNOWN"], []):
        with pytest.raises(ValueError):
            sec_force.resolve_local_companies(database.path, symbols)


def test_execute_passes_exact_symbols_never_assets_bars_or_massive(local_scope, monkeypatch):
    database, _ = local_scope
    called = []

    def sync(self, symbols, *, companyfacts_only=False):
        called.append((symbols, companyfacts_only))
        assert self.alpaca is None
        return {"companies_checked": 2}

    monkeypatch.setattr(DataSynchronizer, "sync_sec_full", sync)
    for name in ("sync_assets", "sync_historical_bars", "sync_daily_history", "sync_intraday"):
        monkeypatch.setattr(DataSynchronizer, name, deny)
    from trading_system.data.massive_reference import MassiveReferenceClient

    monkeypatch.setattr(MassiveReferenceClient, "__init__", deny)
    result, counts = sec_force.execute_force(database.path, ["BBB", "AAA"], "Test test@example.com")
    assert called == [(["BBB", "AAA"], True)]
    assert result["companies_checked"] == 2
    assert counts["sec_requests"] == counts["other_provider_requests"] == 0


def test_sync_companyfacts_only_uses_existing_parser_and_sec_upsert(local_scope):
    database, _ = local_scope
    sec = Mock()
    sec.request_counts = {}
    sec.ticker_to_cik.side_effect = deny
    sec.submissions.side_effect = deny
    sec.company_facts.side_effect = lambda cik: {
        "cik": int(cik),
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2021-01-01",
                                "end": "2021-03-31",
                                "filed": "2021-05-01",
                                "val": 100,
                                "form": "10-Q",
                                "accn": "old",
                                "fy": 2021,
                                "fp": "Q1",
                            }
                        ]
                    }
                }
            }
        },
    }
    guarded = sec_force.SecWriteDatabase(database.path)
    result = DataSynchronizer(guarded, None, sec).sync_sec_full(
        ["BBB", "AAA"], companyfacts_only=True
    )
    assert result["companies_updated"] == 2
    assert sec.company_facts.call_count == 2
    assert result["submissions_requests"] == result["ticker_map_requests"] == 0
    assert len(database.facts_available_as_of("AAA", date(2022, 1, 3))) == 1
    assert guarded.authorized_write_tables == {"companies", "fundamental_facts", "sync_state"}
    assert len(database.facts_available_as_of("CCC", date(2022, 1, 3))) == 0


def test_mismatched_response_cik_is_not_ingested(local_scope):
    database, _ = local_scope
    sec = Mock(request_counts={})
    sec.company_facts.return_value = {"cik": 9999, "facts": {}}
    result = DataSynchronizer(sec_force.SecWriteDatabase(database.path), None, sec).sync_sec_full(
        ["AAA"], companyfacts_only=True
    )
    assert result["parse_failures"] == 1
    assert result["symbol_results"][0]["status"] == "failed"
    assert not database.facts_available_as_of("AAA", date(2022, 1, 3))


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM fundamental_facts",
        "DELETE FROM assets",
        "UPDATE assets SET tradable=0",
        "CREATE TABLE forbidden(x)",
        "ALTER TABLE companies ADD COLUMN forbidden TEXT",
    ],
)
def test_sql_write_guard_blocks_protected_and_destructive_operations(local_scope, sql):
    database, _ = local_scope
    guarded = sec_force.SecWriteDatabase(database.path)
    with pytest.raises(sqlite3.DatabaseError), guarded.connect() as connection:
        connection.execute(sql)
    assert guarded.denied_writes > 0


def test_http_boundary_blocks_other_hosts_resources_and_sessions():
    with (
        requests.Session() as allowed,
        requests.Session() as other,
        sec_force.companyfacts_boundary(allowed, ["0000000001"]) as counts,
    ):
        for session, url in (
            (allowed, "https://data.sec.gov/submissions/CIK0000000001.json"),
            (allowed, "https://example.com"),
            (allowed, "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000002.json"),
            (other, "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json"),
        ):
            with pytest.raises(RuntimeError):
                session.get(url)
    assert counts["blocked_attempts"] == 4
    assert counts["sec_requests"] == 0


def test_pit_fact_delta_excludes_future_and_duplicates():
    def observation(filed, value="1"):
        return (
            "0000000001",
            "revenue",
            "Revenues",
            "USD",
            "2021-01-01",
            "2021-03-31",
            filed,
            "old",
            "frame",
            value,
        )

    original = observation("2021-05-01")
    future = observation("2023-01-03")
    historical = observation("2022-05-01")
    before = {"observations": {original}, "rows": [{"filed": original[6]}]}
    after = {
        "observations": {original, future, historical},
        "rows": [{"filed": r[6]} for r in (original, original, future, historical)],
    }
    delta = pilot.fact_delta(before, after)
    assert delta["facts_added"] == 3
    assert delta["unique_observations_added"] == 2
    assert delta["unique_2022_pit_observations_added"] == 1
    assert delta["duplicate_row_growth"] == 1
    assert pilot.pit_observations({original, historical, future}, date(2022, 1, 3)) == {original}


@pytest.mark.parametrize(
    "synced,score,factor,raw,expected",
    [
        (False, False, False, False, "SYNC_FAILED"),
        (True, True, True, True, "RECOVERED_SCORE_AVAILABILITY"),
        (True, False, True, True, "RECOVERED_FACTOR_ONLY"),
        (True, False, False, True, "NEW_RAW_FACTS_NO_F_IMPROVEMENT"),
        (True, False, False, False, "NO_NEW_SEC_DATA"),
    ],
)
def test_recovery_classification(synced, score, factor, raw, expected):
    assert (
        pilot.classify_symbol(
            synced=synced, score_recovered=score, factor_recovered=factor, new_relevant_data=raw
        )
        == expected
    )


@pytest.mark.parametrize(
    "successful,recovered,expected",
    [
        (9, 9, "SEC_BACKFILL_PILOT_INCONCLUSIVE"),
        (10, 5, "FULL_158_SEC_BACKFILL_JUSTIFIED"),
        (15, 7, "EXPANDED_SEC_BACKFILL_PILOT_JUSTIFIED"),
        (10, 2, "EXPANDED_SEC_BACKFILL_PILOT_JUSTIFIED"),
        (15, 2, "FULL_SEC_BACKFILL_LOW_EXPECTED_VALUE"),
        (15, 0, "FULL_SEC_BACKFILL_NOT_JUSTIFIED"),
    ],
)
def test_predefined_decision_thresholds(successful, recovered, expected):
    assert pilot.decision(successful, recovered) == expected


def test_score_recovery_uses_new_sessions_even_if_count_unchanged():
    plan = {"selected": [{"symbol": "AAA", "cik": "0000000001", "priority": "HIGH"}]}
    before, after = pilot.new_state(["AAA"]), pilot.new_state(["AAA"])
    before["AAA"]["quality"].add("2022-01-03")
    after["AAA"]["quality"].add("2022-01-04")
    after["AAA"]["factors"]["roic"].add("2022-01-04")
    inventory = {"AAA": {"rows": [], "observations": set()}}
    rows = pilot.delta_rows(
        plan, inventory, inventory, before, after, [{"symbol": "AAA", "status": "success"}]
    )
    assert rows[0]["quality_recovered_sessions"] == rows[0]["quality_lost_sessions"] == 1
    assert rows[0]["quality_factors_recovered"] == ["roic"]
    assert rows[0]["classification"] == "RECOVERED_SCORE_AVAILABILITY"


def test_sql_before_after_pit_cutoff(tmp_path):
    from trading_system.data.xbrl_parser import parse_company_facts

    database = Database(tmp_path / "pit.sqlite3")
    database.initialize()
    for filed, value in (("2022-05-01", 100), ("2023-05-01", 200)):
        database.upsert_facts(
            parse_company_facts(
                {
                    "cik": 1,
                    "facts": {
                        "us-gaap": {
                            "Revenues": {
                                "units": {
                                    "USD": [
                                        {
                                            "start": "2021-01-01",
                                            "end": "2021-03-31",
                                            "filed": filed,
                                            "form": "10-Q",
                                            "accn": filed,
                                            "val": value,
                                        }
                                    ]
                                }
                            }
                        }
                    },
                },
                "AAA",
            )
        )
    assert not database.facts_available_as_of("AAA", date(2022, 1, 3))
    assert len(database.facts_available_as_of("AAA", date(2022, 12, 30))) == 1
    batches = list(
        sec_force.ReadOnlyDatabase(database.path).iter_fact_batches(
            ["AAA"],
            pilot.audit.END,
            period_end_on_or_after=date(2018, 1, 1),
            retain_latest_periods=24,
        )
    )
    assert [fact.value for fact in batches[0]] == [100]


def test_pilot_replay_matches_full_source_and_future_filing_cannot_recover_score(tmp_path):
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from trading_system.config import load_settings
    from trading_system.models.fundamentals import FundamentalFact
    from trading_system.models.market_data import DailyBar

    database = Database(tmp_path / "replay.sqlite3")
    database.initialize()
    selected = ["AAA"]
    config = load_settings().strategy
    start, end = date(2022, 1, 3), date(2022, 1, 4)
    for index in range(9):
        symbol, cik = ("AAA" if index == 0 else f"P{index}"), f"{index + 1:010}"
        database.upsert_company(CompanyIdentity(cik=cik, symbol=symbol, name=symbol, sic="3571"))
        database.upsert_assets(
            [TradableAsset(symbol=symbol, name=symbol, tradable=True, fractionable=False)]
        )
        database.upsert_bars(
            [
                DailyBar(
                    symbol=symbol,
                    timestamp=datetime(2020, 11, 1, tzinfo=UTC) + timedelta(days=day),
                    open=100,
                    high=101,
                    low=99,
                    close=100,
                    volume=2_000_000,
                )
                for day in range(430)
            ]
        )
        facts = []
        for quarter in range(8):
            beginning = date(2020 + quarter // 4, quarter % 4 * 3 + 1, 1)
            ending = beginning + timedelta(days=89)
            for metric, value in (
                ("revenue", 300_000_000),
                ("eps_diluted", 1),
                ("operating_income", 60_000_000),
                ("net_income", 40_000_000),
                ("operating_cash_flow", 55_000_000),
                ("capital_expenditures", 10_000_000),
                ("depreciation_amortization", 5_000_000),
                ("tax_expense", 10_000_000),
                ("interest_expense", 2_000_000),
            ):
                facts.append(
                    FundamentalFact(
                        cik=cik,
                        symbol=symbol,
                        metric=metric,
                        tag=metric,
                        value=Decimal(value),
                        unit="USD/shares" if metric == "eps_diluted" else "USD",
                        period_start=beginning,
                        period_end=ending,
                        filed=ending + timedelta(days=2),
                        form="10-Q",
                        accession_number=f"{quarter}-{metric}",
                    )
                )
        for metric, value in (
            ("cash", 100_000_000),
            ("total_debt", 150_000_000),
            ("total_equity", 900_000_000),
            ("total_assets", 1_300_000_000),
            ("current_assets", 400_000_000),
            ("current_liabilities", 200_000_000),
            ("shares_outstanding", 20_000_000),
        ):
            facts.append(
                FundamentalFact(
                    cik=cik,
                    symbol=symbol,
                    metric=metric,
                    tag=metric,
                    value=Decimal(value),
                    unit="shares" if metric == "shares_outstanding" else "USD",
                    period_end=date(2021, 12, 31),
                    filed=date(2023, 1, 3)
                    if symbol == "AAA" and metric == "shares_outstanding"
                    else date(2022, 1, 1),
                    form="10-K",
                    accession_number=metric,
                )
            )
        database.upsert_facts(facts)
    readonly = pilot.audit.AuditDatabase(database.path)
    with pilot.audit.local_guard():
        source = pilot.HistoricalFeatureScreenSource(readonly, config, start, end)
        inventory = pilot.fact_inventory(readonly, selected)
        original = pilot.new_state(selected)
        first = pilot.diagnostic_inventory(inventory)
        companies = {c.symbol: c for c in source._companies}
        saved = {
            "start": start,
            "end": end,
            "companies": {"AAA": companies["AAA"]},
            "market": {"AAA": source._market["AAA"]},
            "cheap": {"AAA": source._cheap_exclusions["AAA"]},
            "static": {},
            "sessions": [],
        }
        for session in (start, end):
            prepared = [source._candidate(c, session) for c in source._companies]
            pilots = [c for c in prepared if c.company.symbol == "AAA"]
            peers = source.screener._f_peer_index(prepared)
            pilot.observe(source, session, pilots, peers, original, first, {}, {})
            saved["sessions"].append(
                (
                    session,
                    [c for c in prepared if c.company.symbol != "AAA" and not c.base_exclusions],
                )
            )
        replay = pilot.after_replay(readonly, config, selected, inventory, {}, [saved])
    assert replay == original
    assert not replay["AAA"]["quality"]
    assert not replay["AAA"]["valuation"]
    # Only a truly PIT-available new shares fact opens the original market-cap gate.
    database.upsert_facts(
        [
            FundamentalFact(
                cik="0000000001",
                symbol="AAA",
                metric="shares_outstanding",
                tag="shares_outstanding",
                value=20_000_000,
                unit="shares",
                period_end=date(2021, 12, 31),
                filed=end,
                form="10-K",
                accession_number="historical",
            )
        ]
    )
    with pilot.audit.local_guard():
        inventory = pilot.fact_inventory(readonly, selected)
        recovered = pilot.after_replay(readonly, config, selected, inventory, {}, [saved])
    assert recovered["AAA"]["quality"] == {"2022-01-04"}
    assert recovered["AAA"]["valuation"] == {"2022-01-04"}
