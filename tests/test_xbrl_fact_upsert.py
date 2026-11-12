"""F01 regression: synthetic facts and temporary SQLite only, no ingestion clients."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from test_champion_consolidation import offline as offline

from trading_system.data.database import Database, _fact_rows
from trading_system.fundamentals.metrics import balance_sheet_as_of
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact
from trading_system.shadow.inputs import LocalInputs


def instant(**changes):
    values = dict(
        cik="0000001234",
        symbol="TEST",
        metric="cash",
        tag="CashAndCashEquivalentsAtCarryingValue",
        value=Decimal("10"),
        unit="USD",
        period_end=date(2024, 3, 31),
        filed=date(2024, 5, 5),
        form="10-Q",
        accession_number="original",
        fiscal_year=2024,
        fiscal_period="Q1",
    )
    return FundamentalFact(**(values | changes))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "facts.sqlite3")
    database.initialize()
    database.upsert_company(CompanyIdentity(cik="0000001234", symbol="TEST", name="Fixture"))
    return database


def stored(db):
    with db.connect() as connection:
        return [
            tuple(row) for row in connection.execute("SELECT * FROM fundamental_facts ORDER BY id")
        ]


def test_identical_instant_fact_twice(db):
    assert db.upsert_facts([instant(), instant()]) == 2  # API counts inputs, not inserted rows.
    assert len(stored(db)) == 1


def test_same_filing_correction_reaches_both_pit_readers_and_balance_sheet(db):
    db.upsert_facts([instant()])
    first_id = stored(db)[0][0]
    corrected = instant(value=Decimal("20"), fiscal_period="FY", fiscal_year=2023, form="10-K")
    db.upsert_facts([corrected])
    assert balance_sheet_as_of(
        db.facts_available_as_of("TEST", date(2024, 5, 5)), date(2024, 5, 5)
    ).cash == Decimal("20")
    assert len(stored(db)) == 1 and stored(db)[0][0] == first_id
    assert db.facts_available_as_of("TEST", date(2024, 5, 4)) == []
    with LocalInputs(db.path).snapshot() as inputs:
        assert inputs.facts_available_as_of("TEST", date(2024, 5, 4)) == []
        available = inputs.facts_available_as_of("TEST", date(2024, 5, 5))
    assert available == [corrected]
    assert balance_sheet_as_of(available, date(2024, 5, 5)).cash == Decimal("20")


@pytest.mark.parametrize(
    "changes",
    [
        {"accession_number": "amended", "form": "10-Q/A"},
        {"filed": date(2024, 6, 1)},
        {"period_end": date(2023, 12, 31)},
        {"period_start": date(2024, 1, 1)},
        {"unit": "EUR"},
        {"frame": "CY2024Q1I"},
        {"tag": "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"},
        {"metric": "total_assets"},
        {"cik": "0000005678"},
    ],
)
def test_identity_dimensions_remain_distinct(db, changes):
    facts = [instant(), instant(**changes)]
    db.upsert_facts(facts)
    before = stored(db)
    db.upsert_facts(facts)
    assert len(before) == 2
    assert stored(db) == before


def test_complete_batch_restart_duration_and_optional_identity_normalization(db):
    batch = [
        instant(accession_number=None, frame=None),
        instant(unit="EUR"),
        instant(metric="revenue", tag="Revenues", period_start=date(2024, 1, 1)),
    ]
    assert db.upsert_facts(batch) == 3
    before = stored(db)
    assert db.upsert_facts(batch) == 3
    restarted = Database(db.path)
    restarted.initialize()
    restarted.upsert_facts(batch)
    restarted.upsert_facts([instant(accession_number="", frame="")])
    assert stored(restarted) == before
    duration = batch[-1].model_copy(update={"value": Decimal("30")})
    restarted.upsert_facts([duration])
    assert len(stored(restarted)) == 3
    assert restarted.facts_available_as_of("TEST", date(2024, 5, 5), "revenue") == [duration]


def test_amendment_filing_date_still_controls_balance_sheet(db):
    amended = instant(
        value=Decimal("30"), filed=date(2024, 6, 1), accession_number="amended", form="10-Q/A"
    )
    db.upsert_facts([instant(), amended, instant(value=Decimal("20")), amended])
    for day, cash, count in [
        (date(2024, 5, 4), None, 0),
        (date(2024, 5, 31), Decimal("20"), 1),
        (date(2024, 6, 1), Decimal("30"), 2),
    ]:
        with LocalInputs(db.path).snapshot() as inputs:
            forward = inputs.facts_available_as_of("TEST", day)
        legacy = db.facts_available_as_of("TEST", day)
        assert forward == legacy and len(legacy) == count
        assert balance_sheet_as_of(legacy, day).cash == cash


def test_concurrent_ingestion_does_not_race_on_null_identity(db):
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(lambda _: Database(db.path).upsert_facts([instant()]), range(2))) == [
            1,
            1,
        ]
    assert len(stored(db)) == 1


def test_sec_atomic_company_update_uses_same_identity_rule(db):
    company = CompanyIdentity(cik="0000001234", symbol="TEST", name="Fixture")
    db.upsert_sec_company_update(company, [instant()], ["original"])
    with db.connect() as connection:
        db.upsert_sec_company_update(
            company, [instant(value=Decimal("20"))], ["original"], connection=connection
        )
    assert len(stored(db)) == 1
    assert db.facts_available_as_of("TEST", date(2024, 5, 5))[0].value == Decimal("20")


def test_mixed_duration_and_instant_batch_preserves_legacy_reader_order(db):
    batch = [
        instant(),
        instant(period_start=date(2024, 1, 1), value=Decimal("20")),
        instant(unit="EUR"),
        instant(period_start=date(2024, 2, 1)),
    ]
    db.upsert_facts(batch)
    assert db.facts_available_as_of("TEST", date(2024, 5, 5)) == batch
    before = stored(db)
    db.upsert_facts(batch)
    assert stored(db) == before


@pytest.mark.parametrize("conflicting", [False, True])
def test_legacy_duplicate_identity_fails_without_cleanup_and_rolls_back_batch(db, conflicting):
    # Reproduce the old schema's ambiguity without invoking the fixed writer.
    raw = _fact_rows([instant(), instant(value=Decimal("20") if conflicting else Decimal("10"))])
    with db.connect() as connection:
        connection.executemany(
            "INSERT INTO fundamental_facts "
            "(cik,symbol,metric,taxonomy,tag,value,unit,period_start,period_end,filed,"
            "fiscal_year,fiscal_period,form,accession_number,frame) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            raw,
        )
    before = stored(db)
    with pytest.raises(sqlite3.IntegrityError, match="AMBIGUOUS_FACT_IDENTITY"):
        db.upsert_facts([instant(unit="EUR"), instant(value=Decimal("30"))])
    assert stored(db) == before
    company_before = db.company_symbol_to_cik()
    with pytest.raises(sqlite3.IntegrityError, match="AMBIGUOUS_FACT_IDENTITY"):
        db.upsert_sec_company_update(
            CompanyIdentity(cik="0000001234", symbol="RENAMED", name="Changed"),
            [instant(value=Decimal("30"))],
            ["unexpected"],
        )
    assert db.company_symbol_to_cik() == company_before
    assert db.sync_value("sec_accessions", "0000001234") is None
    assert stored(db) == before
