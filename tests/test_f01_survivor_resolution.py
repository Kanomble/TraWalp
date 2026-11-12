"""8C resolution probes. Fixture projections only: no migration or production patch."""

import sqlite3
from collections import defaultdict
from datetime import date
from decimal import Decimal
from itertools import permutations

import pytest
from test_champion_consolidation import offline as offline
from test_f01_duplicate_impact import ASOF, current, inputs, ranked, raw_insert, scored, seeded
from test_f01_duplicate_impact import (
    test_operational_blocker_reaches_dataset_error_and_readiness_guard as operational_probe,
)

from trading_system.data.database import Database
from trading_system.data.security_identity import current_identity_epoch
from trading_system.fundamentals.debug import debug_fundamentals
from trading_system.fundamentals.metrics import (
    balance_sheet_as_of,
    build_ttm,
    shares_outstanding_as_of,
)
from trading_system.fundamentals.quality import accounting_state_as_of
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.shadow.contracts import digest
from trading_system.shadow.inputs import LocalInputs

LAST_WINS = {"debt_current", "debt_noncurrent"}
KNOWN = {
    "cash",
    "total_equity",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_debt",
    "shares_outstanding",
    "debt_current",
    "debt_noncurrent",
    "revenue",
    "operating_income",
    "net_income",
    "eps_diluted",
    "operating_cash_flow",
    "capital_expenditures",
    "depreciation_amortization",
    "depreciation",
    "amortization",
    "tax_expense",
    "interest_expense",
}


def key(f):
    return (
        f.cik,
        f.metric,
        f.tag,
        f.unit,
        f.period_start,
        f.period_end,
        f.filed,
        f.accession_number or "",
        f.frame or "",
    )


def fixture_projection(facts):
    """Small-list audit oracle. Preserve whole uncertain groups, including aliases."""
    groups = defaultdict(list)
    for i, f in enumerate(facts):
        groups[key(f)].append((i, f))
    keep = []
    for rows in groups.values():
        signatures = {
            (f.symbol, f.taxonomy, f.value, f.fiscal_year, f.fiscal_period, f.form) for _, f in rows
        }
        if (
            rows[0][1].period_start is not None
            or len(signatures) != 1
            or rows[0][1].metric not in KNOWN
        ):
            keep.extend(rows)
        else:
            keep.append(rows[-1] if rows[0][1].metric in LAST_WINS else rows[0])
    return [f for _, f in sorted(keep)]


def read(db, reader, day=ASOF, symbol="TEST"):
    if reader == "legacy":
        return db.facts_available_as_of(symbol, day)
    with LocalInputs(db.path).snapshot() as local:
        return local.facts_available_as_of(symbol, day)


@pytest.mark.parametrize("reader", ["legacy", "forward"])
@pytest.mark.parametrize(
    "metric",
    [
        "cash",
        "total_equity",
        "current_assets",
        "current_liabilities",
        "total_assets",
        "total_debt",
        "debt_current",
        "debt_noncurrent",
        "shares_outstanding",
    ],
)
def test_reader_class_projection_across_all_two_identity_interleavings(tmp_path, reader, metric):
    base = inputs() + [current("debt_current", 10)]
    base = [
        f
        for f in base
        if not (
            f.metric == metric and f.period_end == date(2024, 12, 31) and f.period_start is None
        )
    ]
    a, b = current(metric, 10, "A"), current(metric, 200, "B")
    if metric == "shares_outstanding":
        a = a.model_copy(
            update={
                "unit": "shares",
                "taxonomy": "dei",
                "tag": "EntityCommonStockSharesOutstanding",
            }
        )
        b = b.model_copy(
            update={
                "unit": "shares",
                "taxonomy": "dei",
                "tag": "EntityCommonStockSharesOutstanding",
            }
        )
    for i, order in enumerate(sorted(set(permutations("AABB")))):
        folder = tmp_path / str(i)
        folder.mkdir()
        facts = read(seeded(folder, base + [a if x == "A" else b for x in order]), reader)
        projected = fixture_projection(facts)
        assert len(projected) == len(facts) - 2
        assert accounting_state_as_of(facts, ASOF) == accounting_state_as_of(projected, ASOF)
        assert debug_fundamentals("TEST", facts, ASOF) == debug_fundamentals(
            "TEST", projected, ASOF
        )
        assert scored(facts) == scored(projected)
        assert digest(facts) != digest(projected)


@pytest.mark.parametrize("reader", ["legacy", "forward"])
def test_cross_accession_repeat_changes_rank_one_without_changing_eligibility(tmp_path, reader):
    def scale(facts):
        # Keep ratios/EPS fixed while putting this synthetic issuer at the
        # unchanged USD 1bn market-cap gate (10m shares at price 100).
        return [
            f.model_copy(update={"value": f.value * (1 if f.metric == "eps_diluted" else 1000000)})
            for f in facts
        ]

    a, b = current("debt_current", 10, "A"), current("debt_current", 200, "B")
    db = seeded(tmp_path, scale(inputs() + [a, b]))
    before = read(db, reader)
    raw_insert(db, scale([a]))  # Reconstruct OLD F01 behavior only in a synthetic fixture.
    after = read(db, reader)
    mb, rb = scored(before, symbol="AAA")
    ma, ra = scored(after, symbol="AAA")
    benchmark = Database(tmp_path / "benchmark.sqlite3")
    benchmark.initialize()
    benchmark.upsert_company(CompanyIdentity(cik="0000005678", symbol="BBB", name="Fixture B"))
    raw_insert(
        benchmark,
        [
            f.model_copy(update={"cik": "0000005678", "symbol": "BBB"})
            for f in scale(inputs() + [current("debt_current", 100)])
        ],
    )
    mc, competitor = scored(read(benchmark, reader, symbol="BBB"), symbol="BBB")
    assert mb.market_cap == ma.market_cap == mc.market_cap == Decimal(1000000000)
    rb, ra, competitor = [
        row.model_copy(
            update={
                "exclusion_reasons": (),
                "estimated_market_cap": metrics.market_cap,
                "average_dollar_volume_20d": 100000000,
            }
        )
        for row, metrics in ((rb, mb), (ra, ma), (competitor, mc))
    ]
    rank_before = ranked([rb, competitor])
    rank_after = ranked([ra, competitor])
    assert mb.debt_to_ebitda != ma.debt_to_ebitda
    assert rb.scores.quality.score != ra.scores.quality.score
    assert rb.scores.valuation.score != ra.scores.valuation.score
    assert rank_before.eligible_count == rank_after.eligible_count == 2
    assert rank_before.records[0].symbol == "BBB" and rank_after.records[0].symbol == "AAA"
    assert digest(before) != digest(after)
    assert scored(after) == scored(fixture_projection(after))


def test_aliases_and_ambiguous_groups_are_isolated_from_partial_projection(tmp_path):
    old = current("cash", 50).model_copy(update={"symbol": "OLD"})
    renamed = old.model_copy(update={"symbol": "TEST"})
    safe = current("debt_current", 10)
    conflict_a = current("total_equity", 100)
    conflict_b = conflict_a.model_copy(update={"value": Decimal(200)})
    stream = inputs() + [old, renamed, renamed, safe, safe, conflict_a, conflict_b]
    projected = fixture_projection(stream)
    assert [f for f in projected if key(f) == key(old)] == [old, renamed, renamed]
    assert [f for f in projected if key(f) == key(safe)] == [safe]
    assert [f for f in projected if key(f) == key(conflict_a)] == [conflict_a, conflict_b]
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    original = seeded(a, stream)
    reduced = seeded(b, projected)
    for reader, symbol in [("legacy", "OLD"), ("legacy", "TEST"), ("forward", "TEST")]:
        left = read(original, reader, symbol=symbol)
        right = read(reduced, reader, symbol=symbol)
        assert scored(left) == scored(right)
        assert balance_sheet_as_of(left, ASOF) == balance_sheet_as_of(right, ASOF)


def test_cik_equality_does_not_establish_a_security_epoch():
    identity = {"symbol": "TEST", "cik": "0000001234", "share_class_figi": "CURRENT"}
    records = [
        dict(
            symbol="TEST",
            cik="0000001234",
            stable_security_identity="SHARE_CLASS_FIGI:OTHER",
            effective_start="2020-01-01",
            effective_end=None,
            source="synthetic",
            evidence_type="AUTHORITATIVE_COMMON_STOCK_SYMBOL_LIFECYCLE",
            verified=1,
            observed_at="2026-11-12T12:00:00+00:00",
            evidence_reference="fixture",
        )
    ]
    assert current_identity_epoch(identity, records) is None
    records[0]["stable_security_identity"] = "SHARE_CLASS_FIGI:CURRENT"
    assert current_identity_epoch(identity, records) is not None


def test_partial_projection_keeps_cik_units_periods_and_legitimate_accessions(tmp_path):
    base = [f for f in inputs() if not (f.metric == "cash" and f.period_end.year == 2024)]
    a = current("cash", 10)
    foreign = a.model_copy(update={"cik": "0000009999", "value": Decimal(200)})
    eur = a.model_copy(update={"unit": "EUR", "value": Decimal(300)})
    amendment = a.model_copy(
        update={"filed": date(2025, 3, 4), "accession_number": "AMEND", "value": Decimal(400)}
    )
    stream = base + [a, foreign, eur, a, foreign, eur, amendment]
    projected = fixture_projection(stream)
    assert len(projected) == len(stream) - 3
    left_path = tmp_path / "left"
    left_path.mkdir()
    right_path = tmp_path / "right"
    right_path.mkdir()
    left, right = seeded(left_path, stream), seeded(right_path, projected)
    for day, cash in [(ASOF, Decimal(10)), (date(2025, 3, 4), Decimal(400))]:
        for reader in ("legacy", "forward"):
            before, after = read(left, reader, day), read(right, reader, day)
            assert balance_sheet_as_of(before, day) == balance_sheet_as_of(after, day)
            assert balance_sheet_as_of(after, day).cash == cash
            assert (foreign in after) == (reader == "legacy")
            assert eur in after and a in after
            assert (amendment in after) == (day >= amendment.filed)


def test_eps_duration_amendment_units_and_null_start_exclusion(tmp_path):
    base = inputs() + [current("debt_current", 10)]
    eps = next(f for f in base if f.metric == "eps_diluted" and f.period_end == date(2024, 12, 31))
    amendment = eps.model_copy(
        update={
            "value": Decimal(3),
            "filed": date(2025, 3, 4),
            "accession_number": "EPS-AMEND",
            "form": "10-K/A",
        }
    )
    eur = eps.model_copy(update={"unit": "EUR/shares", "value": Decimal(1000)})
    ignored = current("eps_diluted", 999999).model_copy(update={"unit": "USD/shares"})
    db = seeded(tmp_path, base + [amendment, eur, ignored, ignored])
    for day, expected in [(ASOF, Decimal(8)), (date(2025, 3, 4), Decimal(9))]:
        facts = read(db, "forward", day)
        reduced = fixture_projection(facts)
        assert build_ttm(facts, day).eps_diluted == expected
        assert build_ttm(facts, day) == build_ttm(reduced, day)
        assert eur in reduced
        assert (amendment in reduced) == (day >= amendment.filed)


def test_shares_priorities_age_and_metadata_ambiguity_are_not_rewritten(tmp_path):
    dei = current("shares_outstanding", 10, "A").model_copy(
        update={"unit": "shares", "taxonomy": "dei", "tag": "EntityCommonStockSharesOutstanding"}
    )
    gaap = dei.model_copy(
        update={
            "taxonomy": "us-gaap",
            "value": Decimal(200),
            "tag": "CommonStockSharesOutstanding",
            "accession_number": "Z",
        }
    )
    db = seeded(tmp_path, [dei, gaap, dei])
    facts = read(db, "forward")
    assert shares_outstanding_as_of(facts, ASOF) == dei
    assert shares_outstanding_as_of(fixture_projection(facts), ASOF) == dei
    assert shares_outstanding_as_of(fixture_projection(facts), date(2027, 1, 1)) is None
    changed = dei.model_copy(update={"taxonomy": "us-gaap"})
    assert fixture_projection([dei, changed]) == [dei, changed]


@pytest.mark.parametrize("mode", ["incremental", "full"])
def test_ambiguous_writer_refresh_is_not_recorded_as_success(tmp_path, mode):
    operational_probe(tmp_path, mode)
    db = Database(tmp_path / "operational.sqlite3")
    state = db.sync_value("dataset", "sec")
    assert state["status"] == "partial" and state["errors"] == 1 and state["database_failures"] == 1
    with db.connect() as c:
        rows = c.execute(
            "SELECT COUNT(*) FROM fundamental_facts WHERE period_start IS NULL"
        ).fetchone()[0]
    assert rows == 2  # Neither skip-as-success nor automatic cleanup.


@pytest.mark.parametrize("alias", [False, True])
def test_writer_must_refuse_both_identical_and_alias_ambiguity(tmp_path, alias):
    a = current("cash", 10)
    b = a.model_copy(update={"symbol": "OLD"}) if alias else a
    db = seeded(tmp_path, [a, b])
    before = read(db, "forward")
    with pytest.raises(sqlite3.IntegrityError, match="AMBIGUOUS_FACT_IDENTITY"):
        db.upsert_facts([a])
    assert read(db, "forward") == before
