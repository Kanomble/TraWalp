"""Task 8B audit probes: no production changes, providers or backtests."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from test_champion_consolidation import offline as offline
from test_champion_consolidation import record
from test_xbrl_fact_upsert import instant

from trading_system.champion import champion_ranking
from trading_system.config import load_strategy_config
from trading_system.data.database import Database, _fact_rows
from trading_system.fundamentals.metrics import balance_sheet_as_of, build_ttm
from trading_system.fundamentals.quality import analyze_fundamentals
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.screening import ScreenReport
from trading_system.shadow.contracts import digest
from trading_system.shadow.inputs import LocalInputs
from trading_system.strategy.scoring import score_quality, score_valuation

ASOF = date(2025, 3, 3)


def raw_insert(db, facts):
    with db.connect() as c:
        c.executemany(
            """INSERT INTO fundamental_facts
            (cik,symbol,metric,taxonomy,tag,value,unit,period_start,period_end,filed,
             fiscal_year,fiscal_period,form,accession_number,frame)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            _fact_rows(facts),
        )


def reduce_facts(facts, keep_last=False):
    chosen = {}
    for index, f in enumerate(facts):
        key = (
            f.cik,
            f.metric,
            f.tag,
            f.unit,
            f.period_start,
            f.period_end,
            f.filed,
            f.accession_number,
            f.frame,
        )
        if keep_last or key not in chosen:
            chosen[key] = (index, f)
    return [f for _, f in sorted(chosen.values())]


def inputs():
    facts = []
    values = dict(
        revenue=100,
        operating_income=30,
        net_income=24,
        eps_diluted=2,
        operating_cash_flow=35,
        capital_expenditures=5,
        depreciation_amortization=3,
        tax_expense=6,
        interest_expense=1,
    )
    for year in (2023, 2024):
        for month, day, start_month in ((3, 31, 1), (6, 30, 4), (9, 30, 7), (12, 31, 10)):
            end = date(year, month, day)
            for metric, value in values.items():
                facts.append(
                    instant(
                        metric=metric,
                        tag=metric,
                        value=Decimal(value) * (Decimal("0.75") if year == 2023 else 1),
                        unit="USD/shares" if metric == "eps_diluted" else "USD",
                        period_start=date(year, start_month, 1),
                        period_end=end,
                        filed=end + timedelta(days=40),
                        accession_number=str(end),
                    )
                )
    for year in (2023, 2024):
        for metric, value in dict(
            cash=50,
            total_equity=200,
            total_assets=400,
            current_assets=200,
            current_liabilities=50,
            shares_outstanding=10,
            debt_noncurrent=10,
        ).items():
            facts.append(
                instant(
                    metric=metric,
                    tag=metric,
                    value=Decimal(value),
                    unit="shares" if metric == "shares_outstanding" else "USD",
                    period_end=date(year, 12, 31),
                    filed=date(year + 1, 2, 9),
                    accession_number="annual-" + str(year),
                )
            )
    facts.append(
        instant(
            metric="debt_current",
            tag="debt_current",
            value=Decimal(10),
            period_end=date(2023, 12, 31),
            filed=date(2024, 2, 9),
            accession_number="prior",
        )
    )
    return facts


def current(metric, value, accession="A"):
    return instant(
        metric=metric,
        tag=metric,
        value=Decimal(value),
        period_end=date(2024, 12, 31),
        filed=date(2025, 2, 9),
        accession_number=accession,
    )


def scored(facts, asof=ASOF, symbol="AAA"):
    config = load_strategy_config()
    metrics = analyze_fundamentals(facts, asof, Decimal(100))
    quality = score_quality(metrics, {}, config.scores)
    valuation = score_valuation(
        metrics, {"pe": 50, "ev_to_ebitda": 20, "ev_to_ebit": 20}, {}, config.scores
    )
    row = record(symbol).model_copy(
        update={
            "as_of": asof,
            "fundamentals": metrics,
            "market_history_count": 300,
            "current_market_observation": "OBSERVATION_PRESENT",
            "technical": record(symbol).technical.model_copy(update={"market_session": asof}),
            "scores": record(symbol).scores.model_copy(
                update={"quality": quality, "valuation": valuation}
            ),
        }
    )
    return metrics, row


def ranked(rows):
    assert rows and all(row.as_of == rows[0].as_of for row in rows)
    raw = ScreenReport(
        as_of=rows[0].as_of,
        generated_at="synthetic",
        analyzed_count=len(rows),
        eligible_count=0,
        records=tuple(rows),
    )
    return champion_ranking(raw, load_strategy_config())


def seeded(tmp_path, facts):
    db = Database(tmp_path / "facts.sqlite3")
    db.initialize()
    db.upsert_company(CompanyIdentity(cik="0000001234", symbol="TEST", name="Fixture"))
    raw_insert(db, facts)
    return db


@pytest.mark.parametrize("reader", ["legacy", "forward"])
@pytest.mark.parametrize("asof", [date(2024, 5, 31), date(2025, 2, 8), ASOF])
def test_identical_repetitions_preserve_metrics_scores_ranking_but_not_input_digest(
    tmp_path, reader, asof
):
    base = inputs() + [current("debt_current", 10)]
    repeated = base + [f for f in base if f.period_start is None]
    db = seeded(tmp_path, repeated)
    if reader == "legacy":
        facts = db.facts_available_as_of("TEST", asof)
        manifest = digest(facts)
    else:
        with LocalInputs(db.path).snapshot() as local:
            facts = local.facts_available_as_of("TEST", asof)
            manifest = local.provenance["fundamental_inputs"][f"TEST|{asof.isoformat()}|None"]
    reduced = reduce_facts(facts)
    assert manifest == digest(facts) and manifest != digest(reduced)
    assert build_ttm(facts, asof) == build_ttm(reduced, asof)
    assert balance_sheet_as_of(facts, asof) == balance_sheet_as_of(reduced, asof)
    a, ra = scored(facts, asof)
    b, rb = scored(reduced, asof)
    assert a == b and ra.scores == rb.scores
    assert ranked([ra, ra.model_copy(update={"symbol": "BBB"})]) == ranked(
        [rb, rb.model_copy(update={"symbol": "BBB"})]
    )
    if asof == ASOF:
        report = ranked([ra, ra.model_copy(update={"symbol": "BBB"})])
        assert report.eligible_count == 2, ra.scores
        assert [r.symbol for r in report.records] == ["AAA", "BBB"]
        assert ra.scores.quality.available_factor_count >= 4
        assert ra.scores.valuation.available_factor_count >= 2


@pytest.mark.parametrize("reader", ["legacy", "forward"])
def test_interleaved_identical_duplicates_change_debt_selection_and_f_score(tmp_path, reader):
    a, b = current("debt_current", 10, "A"), current("debt_current", 200, "B")
    db = seeded(tmp_path, inputs() + [a, b, a])
    if reader == "legacy":
        facts = db.facts_available_as_of("TEST", ASOF)
    else:
        with LocalInputs(db.path).snapshot() as local:
            facts = local.facts_available_as_of("TEST", ASOF)
    before_last_repetition = facts[:-1]
    reduced = reduce_facts(facts)
    original, ro = scored(facts)
    before, rb = scored(before_last_repetition)
    after, rd = scored(reduced)
    assert original.debt_to_ebitda != before.debt_to_ebitda == after.debt_to_ebitda
    assert ro.scores.quality.score != rb.scores.quality.score == rd.scores.quality.score
    # The repeated A fact has identical full content; different accession B remains legitimate.
    assert facts[-1] == facts[-3]
    assert original != after
    assert ranked([ro]).records[0].scores.total != ranked([rd]).records[0].scores.total
    assert scored(reduce_facts(facts, keep_last=True))[0] == original


def test_no_universal_min_or_max_id_survivor_for_mixed_reader_tie_rules(tmp_path):
    # Cash max() chooses FIRST tied row; debt parts overwrite with the LAST tied row.
    base = [f for f in inputs() if not (f.metric == "cash" and f.period_end.year == 2024)]
    stream = base + [
        current("cash", 50, "A"),
        current("cash", 150, "B"),
        current("cash", 50, "A"),
        current("debt_current", 10, "A"),
        current("debt_current", 200, "B"),
        current("debt_current", 10, "A"),
    ]
    facts = seeded(tmp_path, stream).facts_available_as_of("TEST", ASOF)
    original = balance_sheet_as_of(facts, ASOF)
    earliest = balance_sheet_as_of(reduce_facts(facts), ASOF)
    latest = balance_sheet_as_of(reduce_facts(facts, keep_last=True), ASOF)
    assert original.cash == earliest.cash != latest.cash
    assert original.total_debt == latest.total_debt != earliest.total_debt


def test_source_symbol_variants_can_remove_legacy_metric_availability(tmp_path):
    old = current("cash", 50).model_copy(update={"symbol": "OLD"})
    renamed = old.model_copy(update={"symbol": "TEST"})
    facts = [old, renamed]
    original_path = tmp_path / "original"
    original_path.mkdir()
    reduced_path = tmp_path / "reduced"
    reduced_path.mkdir()
    original = seeded(original_path, facts)
    reduced = seeded(reduced_path, reduce_facts(facts))
    a = original.facts_available_as_of("TEST", ASOF)
    b = reduced.facts_available_as_of("TEST", ASOF)
    assert balance_sheet_as_of(a, ASOF).cash == Decimal(50)
    assert balance_sheet_as_of(b, ASOF).cash is None
    with LocalInputs(original.path).snapshot() as local:
        forward_a = local.facts_available_as_of("TEST", ASOF)
    with LocalInputs(reduced.path).snapshot() as local:
        forward_b = local.facts_available_as_of("TEST", ASOF)
    assert balance_sheet_as_of(forward_a, ASOF) == balance_sheet_as_of(forward_b, ASOF)


def test_metric_specific_survivors_preserve_mixed_selector_values(tmp_path):
    base = [f for f in inputs() if not (f.metric == "cash" and f.period_end.year == 2024)]
    stream = base + [
        current("cash", 50, "A"),
        current("cash", 150, "B"),
        current("cash", 50, "A"),
        current("debt_current", 10, "A"),
        current("debt_current", 200, "B"),
        current("debt_current", 10, "A"),
    ]
    facts = seeded(tmp_path, stream).facts_available_as_of("TEST", ASOF)
    # A fixture-only design probe, NOT a migration implementation. Source-symbol
    # variants require a separate alias contract and are deliberately excluded.
    first = reduce_facts(facts)
    last = reduce_facts(facts, keep_last=True)
    chosen = {id(f) for f in first if f.metric not in {"debt_current", "debt_noncurrent"}}
    chosen.update(id(f) for f in last if f.metric in {"debt_current", "debt_noncurrent"})
    survivors = [f for f in facts if id(f) in chosen]
    assert scored(facts) == scored(survivors)
    assert ranked([scored(facts)[1]]) == ranked([scored(survivors)[1]])


def test_amendments_units_and_cik_boundaries_not_collapsed(tmp_path):
    base = inputs() + [current("debt_current", 10)]
    later = current("cash", 500, "amended").model_copy(
        update={"filed": date(2025, 3, 4), "form": "10-Q/A"}
    )
    foreign = current("cash", 900).model_copy(update={"cik": "0000009999"})
    eur = current("cash", 50).model_copy(update={"unit": "EUR"})
    db = seeded(tmp_path, base + [later, later, foreign, foreign, eur, eur])
    legacy = db.facts_available_as_of("TEST", date(2025, 3, 4))
    with LocalInputs(db.path).snapshot() as local:
        forward = local.facts_available_as_of("TEST", date(2025, 3, 4))
        earlier = local.facts_available_as_of("TEST", ASOF)
    assert foreign in legacy and foreign not in forward
    assert later not in earlier and later in forward
    assert balance_sheet_as_of(earlier, ASOF).cash == Decimal(50)
    assert balance_sheet_as_of(forward, date(2025, 3, 4)).cash == Decimal(500)
    reduced = reduce_facts(forward)
    assert {f.unit for f in reduced if f.metric == "cash"} == {"USD", "EUR"}
    assert {f.accession_number for f in reduced if f.metric == "cash"} >= {"annual-2024", "amended"}
    assert analyze_fundamentals(forward, ASOF, Decimal(100)) == analyze_fundamentals(
        reduced, ASOF, Decimal(100)
    )


@pytest.mark.parametrize("mode", ["incremental", "full"])
def test_operational_blocker_reaches_dataset_error_and_readiness_guard(tmp_path, mode):
    from datetime import UTC, datetime

    from test_champion_consolidation import bar
    from test_shadow_execution import Screens, ready_states
    from test_sync import IncrementalSec, _seed_assets

    from trading_system.data.forward_market import persist
    from trading_system.data.market_sessions import latest_completed_trading_session
    from trading_system.data.sync import DataSynchronizer
    from trading_system.shadow.readiness import evaluate_shadow_input_readiness

    db = Database(tmp_path / "operational.sqlite3")
    db.initialize()
    _seed_assets(db, "TEST")
    # Fake Companyfacts are the same inputs as a normal production incremental update.
    provider = IncrementalSec()
    from trading_system.data.xbrl_parser import parse_company_facts

    payload = provider.company_facts("0000001234")
    parsed = parse_company_facts(payload, "TEST")
    # This fake payload is duration-only: add an instant concept to the fake response.
    payload["facts"]["us-gaap"]["CashAndCashEquivalentsAtCarryingValue"] = {
        "units": {
            "USD": [
                {
                    "end": "2024-03-31",
                    "filed": "2024-05-05",
                    "val": 10,
                    "form": "10-Q",
                    "accn": "original",
                }
            ]
        }
    }
    parsed = parse_company_facts(payload, "TEST")
    dup = next(f for f in parsed if f.period_start is None)
    raw_insert(db, [dup, dup])
    provider.company_facts = lambda cik: payload
    now = datetime(2026, 8, 12, 22, tzinfo=UTC)
    ready_states(db, now)
    db.set_sync_value("sec_accessions", "0000001234", ["old"])
    sync = DataSynchronizer(db, None, provider, clock=lambda: now)
    result = sync.sync_sec_incremental() if mode == "incremental" else sync.sync_sec_full(["TEST"])
    assert result["database_failures"] == result["errors"] == 1
    state = db.sync_value("dataset", "sec")
    assert state["status"] == "partial" and state["errors"] == 1
    assert state["last_success_at"] == now.isoformat()  # Old success time alone cannot admit it.
    day = latest_completed_trading_session(now)
    persist(
        db,
        [bar("SPY", day)],
        provider="alpaca",
        feed="sip",
        adjustment="all",
        sync_reference="synthetic",
    )
    with LocalInputs(db.path).snapshot() as local:
        readiness = evaluate_shadow_input_readiness(
            local, load_strategy_config(), now=now, session=day, screen_source=Screens()
        )
    assert "SEC_STATE_PARTIAL" in readiness.details["blocking_reasons"]
    assert "SEC_ERRORS_PRESENT" in readiness.details["blocking_reasons"]
    assert not readiness.ready and not readiness.observation_ready
