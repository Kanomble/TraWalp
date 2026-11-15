"""8E: tiny synthetic databases only; no migration entry point or live providers."""

import sqlite3
from datetime import date
from decimal import Decimal
from itertools import permutations

import pytest
from test_champion_consolidation import offline as offline
from test_f01_duplicate_impact import ASOF, current, inputs, ranked, scored, seeded
from test_f01_survivor_resolution import key, read

from trading_system.data.database import _fact_rows
from trading_system.fundamentals.debug import debug_fundamentals
from trading_system.fundamentals.quality import accounting_state_as_of
from trading_system.shadow.contracts import digest

COLS = [
    "cik",
    "symbol",
    "metric",
    "taxonomy",
    "tag",
    "value",
    "unit",
    "period_start",
    "period_end",
    "filed",
    "fiscal_year",
    "fiscal_period",
    "form",
    "accession_number",
    "frame",
]
KEY = [
    "cik",
    "metric",
    "tag",
    "unit",
    "period_start",
    "period_end",
    "filed",
    "accession_number",
    "frame",
]
CANON = [c for c in COLS if c not in {"symbol", "taxonomy"}]


def normalize_fixture(db):
    """Lossless A prototype. Caller has already built a tiny synthetic fixture."""
    with db.connect() as c:
        rows = c.execute("SELECT * FROM fundamental_facts ORDER BY id").fetchall()
        c.execute("ALTER TABLE fundamental_facts RENAME TO oracle_facts")
        c.execute(
            "CREATE TABLE canonical AS SELECT id AS fact_id,"
            + ",".join(CANON)
            + " FROM oracle_facts WHERE 0"
        )
        c.execute("CREATE UNIQUE INDEX canonical_id ON canonical(fact_id)")
        c.execute(
            "CREATE UNIQUE INDEX instant_key ON canonical("
            + ",".join(x for x in KEY if x != "period_start")
            + ") WHERE period_start IS NULL"
        )
        c.execute("CREATE UNIQUE INDEX duration_key ON canonical(" + ",".join(KEY) + ")")
        c.execute("""CREATE TABLE occurrence(
            id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL REFERENCES canonical(fact_id),
            symbol TEXT NOT NULL, taxonomy TEXT NOT NULL)""")
        seen = {}
        for row in rows:
            identity = tuple(row[x] for x in KEY)
            signature = tuple(row[x] for x in CANON)
            if identity in seen:
                fid, previous = seen[identity]
                if signature != previous:
                    raise ValueError("UNRESOLVED_CONTENT_CONFLICT")
            else:
                fid = row["id"]
                seen[identity] = fid, signature
                c.execute(
                    "INSERT INTO canonical VALUES ("
                    + ",".join("?" for _ in range(len(CANON) + 1))
                    + ")",
                    (fid, *signature),
                )
            c.execute(
                "INSERT INTO occurrence VALUES (?,?,?,?)",
                (row["id"], fid, row["symbol"], row["taxonomy"]),
            )
        projection = ["o.id"] + [("o." if x in {"symbol", "taxonomy"} else "f.") + x for x in COLS]
        c.execute(
            "CREATE VIEW fundamental_facts AS SELECT "
            + ",".join(projection)
            + " FROM occurrence o JOIN canonical f ON f.fact_id=o.fact_id"
        )


def ingest_fixture(db, company_cik, facts, *, source_authorized=True):
    """Proposed A transaction model, deliberately unusable on production schema.

    New labels require explicit acquisition authorization; this is NOT evidence
    of a security epoch. Replays/corrections never move existing occurrences.
    """
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE companies SET name='pending' WHERE cik=?", (company_cik,))
        for fact in facts:
            if fact.cik != company_cik or not source_authorized:
                raise ValueError("IDENTITY_ASSIGNMENT_REJECTED")
            row = dict(zip(COLS, _fact_rows([fact])[0], strict=True))
            matches = c.execute(
                "SELECT * FROM canonical WHERE " + " AND ".join(x + " IS ?" for x in KEY),
                [row[x] for x in KEY],
            ).fetchall()
            if len(matches) > 1:
                raise sqlite3.IntegrityError("AMBIGUOUS_FACT_IDENTITY")
            if not matches:
                raise ValueError("POC_SCOPE_NEW_IDENTITIES_NOT_IMPLEMENTED")
            fid = matches[0]["fact_id"]
            c.execute(
                "UPDATE canonical SET value=?,fiscal_year=?,fiscal_period=?,form=? WHERE fact_id=?",
                [row[x] for x in ["value", "fiscal_year", "fiscal_period", "form"]] + [fid],
            )
            exists = c.execute(
                "SELECT 1 FROM occurrence WHERE fact_id=? AND symbol=?", (fid, fact.symbol)
            ).fetchone()
            if not exists:
                c.execute(
                    "INSERT INTO occurrence SELECT COALESCE(MAX(id),0)+1,?,?,? FROM occurrence",
                    (fid, fact.symbol, fact.taxonomy),
                )
        c.execute(
            "INSERT OR REPLACE INTO sync_state VALUES ('sec_accessions',?,?,?)",
            (company_cik, "synthetic", '["updated"]'),
        )


@pytest.mark.parametrize("order", sorted(set(permutations("AABB"))))
def test_a_lossless_stream_all_readers_pit_scores_and_batch_order(tmp_path, order):
    base = inputs()
    a = current("cash", 10).model_copy(update={"symbol": "OLD"})
    b = current("cash", 200, "B")
    debt_a, debt_b = current("debt_current", 10), current("debt_current", 200, "B")
    stream = base + [a if x == "A" else b for x in order]
    stream += [debt_a, debt_b, debt_a, a.model_copy(update={"symbol": "TEST"})]
    stream += [b.model_copy(update={"cik": "0000009999", "value": Decimal(900)})]
    stream += [
        a.model_copy(
            update={"filed": date(2025, 3, 4), "accession_number": "AMEND", "value": Decimal(400)}
        )
    ]
    # Unknown consumer must survive too, with no selector-specific skip policy.
    stream += [current("unrecognized_metric", 123)]
    db = seeded(tmp_path, stream)
    queries = [
        (reader, symbol, day)
        for reader, symbol in [("legacy", "OLD"), ("legacy", "TEST"), ("forward", "TEST")]
        for day in [date(2025, 2, 8), ASOF, date(2025, 3, 4)]
    ]
    before = [read(db, r, d, s) for r, s, d in queries]
    batches = list(
        db.iter_fact_batches(
            ["OLD", "TEST"], ASOF, period_end_on_or_after=date(2024, 10, 1), retain_latest_periods=2
        )
    )
    normalize_fixture(db)
    for (reader, symbol, day), expected in zip(queries, before, strict=True):
        actual = read(db, reader, day, symbol)
        assert actual == expected  # multiplicity, source labels and complete order
        assert digest(actual) == digest(expected)
        assert accounting_state_as_of(actual, day) == accounting_state_as_of(expected, day)
        assert debug_fundamentals(symbol, actual, day) == debug_fundamentals(symbol, expected, day)
        assert scored(actual, day) == scored(expected, day)
        assert ranked([scored(actual, day)[1]]) == ranked([scored(expected, day)[1]])
    assert batches == list(
        db.iter_fact_batches(
            ["OLD", "TEST"], ASOF, period_end_on_or_after=date(2024, 10, 1), retain_latest_periods=2
        )
    )
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM occurrence").fetchone()[0] == len(stream)
        assert c.execute("SELECT COUNT(*) FROM canonical").fetchone()[0] == len(
            {key(f) for f in stream}
        )
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_replay_correction_alias_authorization_and_company_rollback(tmp_path):
    a = current("cash", 10)
    db = seeded(tmp_path, [a, a.model_copy(update={"symbol": "OLD"}), a])
    normalize_fixture(db)
    frozen = read(db, "forward")  # B immutable dataset reference, never relabeled
    ingest_fixture(db, a.cik, [a])
    assert read(db, "forward") == frozen
    corrected = a.model_copy(update={"value": Decimal(20)})
    ingest_fixture(db, a.cik, [corrected])
    assert {f.value for f in read(db, "forward")} == {Decimal(20)}
    assert read(db, "legacy", symbol="OLD")[0].value == Decimal(20)
    with db.connect() as c:
        snapshot = {
            t: [tuple(r) for r in c.execute("SELECT * FROM " + t)]
            for t in ["canonical", "occurrence", "companies", "sync_state"]
        }
    bad = a.model_copy(update={"cik": "0000009999"})
    new_source = a.model_copy(update={"symbol": "NEW"})
    for rows, authorized in [([new_source, bad], True), ([a], False)]:
        with pytest.raises(ValueError, match="IDENTITY_ASSIGNMENT_REJECTED"):
            ingest_fixture(db, a.cik, rows, source_authorized=authorized)
        with db.connect() as c:
            assert snapshot == {
                t: [tuple(r) for r in c.execute("SELECT * FROM " + t)] for t in snapshot
            }
    assert frozen[0].value == Decimal(10)


def test_b_canonical_only_is_not_legacy_or_forward_equivalence(tmp_path):
    a = current("debt_current", 10).model_copy(update={"symbol": "OLD"})
    b = current("debt_current", 200, "B")
    db = seeded(tmp_path, inputs() + [a, b, a])
    before = read(db, "forward")
    canonical = list({key(f): f for f in before}.values())
    # Dict insertion order keeps first A position, despite last A winning content.
    assert accounting_state_as_of(before, ASOF) != accounting_state_as_of(canonical, ASOF)
    assert scored(before) != scored(canonical)
    assert digest(before) != digest(canonical)
    assert [
        f
        for f in canonical
        if f.symbol == "TEST" and f.metric == "debt_current" and f.period_end == b.period_end
    ] == [b]


def test_a_conflicting_identity_content_stops_instead_of_merging(tmp_path):
    a = current("cash", 10)
    db = seeded(tmp_path, [a, a.model_copy(update={"value": Decimal(20)})])
    with pytest.raises(ValueError, match="UNRESOLVED_CONTENT_CONFLICT"):
        normalize_fixture(db)


def test_new_alias_requires_separate_forward_order_admission(tmp_path):
    """Expose the concrete blocker; the toy writer is not a production solution."""
    a, b = current("debt_current", 10), current("debt_current", 200, "B")
    db = seeded(tmp_path, inputs() + [a, b])
    normalize_fixture(db)
    before = read(db, "forward")
    ingest_fixture(db, a.cik, [a.model_copy(update={"symbol": "NEW"})])
    after = read(db, "forward")
    assert scored(before) != scored(after)
    assert read(db, "legacy", symbol="NEW")[0].cik == a.cik


def test_normalized_forward_quarantine_still_has_no_ticker_fallback(tmp_path, monkeypatch):
    from trading_system.shadow.inputs import LocalInputs

    db = seeded(tmp_path, [current("cash", 10)])
    normalize_fixture(db)
    with LocalInputs(db.path).snapshot() as local:
        monkeypatch.setattr(local, "unresolved_sec_identity_conflict_symbols", lambda: {"TEST"})
        assert local.facts_available_as_of("TEST", ASOF) == []
    assert len(read(db, "legacy")) == 1
