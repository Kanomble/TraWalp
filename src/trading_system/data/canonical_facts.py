"""F01 A-V1. Explicit offline construction; never called by initialize or sync.

Fact IDs identify issuer/filing identities; occurrence IDs are the reader order.
Revisions retain old values without changing the filed-PIT contract. Membership
admission is evidence supplied by the caller, never inferred from a ticker.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from trading_system.data.database import _fact_rows

CONTRACT = "F01_CANON_OCC_V1"
COLS = (
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
)
KEY = (
    "cik",
    "metric",
    "tag",
    "unit",
    "period_start",
    "period_end",
    "filed",
    "accession_number",
    "frame",
)
MUTABLE = ("value", "fiscal_year", "fiscal_period", "form")
WRITE_TABLES = frozenset(
    {
        "canonical_fact",
        "fact_revision",
        "source_occurrence",
        "ingestion_receipt",
        "fact_dataset",
        "fact_contract",
        "companies",
        "sync_state",
    }
)


def install_writer_guard(c):
    """Explicit V1 operation allowlist, separate from the legacy SEC guard."""
    require_contract(c)

    def authorize(action, table, column, database, trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            allowed = table in WRITE_TABLES and trigger is None and database == "main"
            if action == sqlite3.SQLITE_DELETE:
                allowed = allowed and table == "sync_state"
            if action == sqlite3.SQLITE_UPDATE:
                allowed = allowed and (
                    table in {"companies", "sync_state", "fact_revision"}
                    or (table == "canonical_fact" and column in MUTABLE)
                    or (table == "fact_contract" and column == "current_version")
                )
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            return (
                sqlite3.SQLITE_OK
                if table == "foreign_keys" and column is None
                else sqlite3.SQLITE_DENY
            )
        return (
            sqlite3.SQLITE_OK
            if action
            in {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_TRANSACTION,
                sqlite3.SQLITE_SAVEPOINT,
                sqlite3.SQLITE_RECURSIVE,
            }
            else sqlite3.SQLITE_DENY
        )

    c.set_authorizer(authorize)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Admission:
    legacy_admitted: bool
    forward_order_admitted: bool
    evidence: str


@dataclass(frozen=True)
class RequestIdentity:
    symbol: str
    cik: str
    evidence: str
    unresolved_conflict: bool = False


def require_contract(c, *, writer=True):
    from trading_system.data.database import _require_complete_migration

    _require_complete_migration(c)
    row = c.execute("SELECT version,schema_hash FROM fact_contract WHERE singleton=1").fetchone()
    if (
        row is None
        or row[0] != CONTRACT
        or row[1] != schema_fingerprint(c)
        or (writer and not c.execute("PRAGMA foreign_keys").fetchone()[0])
    ):
        raise ValueError("CANONICAL_SCHEMA_OR_FK_GUARD")


def schema_fingerprint(c):
    names = sorted(WRITE_TABLES - {"companies", "sync_state"}) + [
        "canonical_duration",
        "canonical_instant",
        "occurrence_membership",
        "occurrence_legacy_reader",
        "canonical_forward_reader",
        "legacy_occ_versions_v1",
        "forward_occ_versions_v1",
        "legacy_occ_v1",
        "forward_occ_v1",
        "fundamental_facts",
    ]
    return fingerprint(
        [
            tuple(r)
            for r in c.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name IN ("
                + ",".join("?" for _ in names)
                + ") ORDER BY name",
                names,
            )
        ]
    )


@contextmanager
def atomic(c):
    """Own a transaction only when none exists; undo our writes even if caught."""
    owner = not c.in_transaction
    if owner:
        c.execute("BEGIN IMMEDIATE")
    if not owner:
        c.execute("SAVEPOINT canonical_company")
    try:
        yield
        if owner:
            c.commit()
        else:
            c.execute("RELEASE canonical_company")
    except BaseException as original:
        try:
            if owner:
                c.rollback()
            elif c.in_transaction:
                c.execute("ROLLBACK TO canonical_company")
                c.execute("RELEASE canonical_company")
        except BaseException as cleanup:
            original.add_note(f"Transaction cleanup also failed: {cleanup!r}")
        raise


def _insert(c, table, values):
    return c.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _lookup(c, row):
    sql, parameters = lookup_query(row)
    found = c.execute(sql, parameters).fetchall()
    if len(found) > 1:
        raise sqlite3.IntegrityError("AMBIGUOUS_FACT_IDENTITY")
    return found[0][0] if found else None


def lookup_query(row):
    keys = [k for k in KEY if k != "period_start"]
    predicate = "period_start IS NULL"
    if row["period_start"] is not None:
        keys = list(KEY)
        predicate = "period_start IS NOT NULL"
    return (
        "SELECT fact_id FROM canonical_fact WHERE "
        + predicate
        + " AND "
        + " AND ".join(k + "=?" for k in keys)
        + " LIMIT 2",
        [row[k] for k in keys],
    )


def create_storage(c, *, dataset_id, source_hash):
    """DDL only; used inside the caller's transaction, never publishes readers."""
    c.execute(
        "CREATE TABLE fact_contract(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
        "version TEXT NOT NULL,current_version INTEGER NOT NULL,schema_hash TEXT NOT NULL)"
    )
    c.execute("INSERT INTO fact_contract VALUES (1,?,1,'pending')", (CONTRACT,))
    c.execute(
        "CREATE TABLE fact_dataset(version INTEGER PRIMARY KEY,dataset_id TEXT NOT NULL,"
        "source_hash TEXT NOT NULL)"
    )
    c.execute("INSERT INTO fact_dataset VALUES (1,?,?)", (dataset_id, source_hash))
    definitions = [
        k
        + (" INTEGER" if k == "fiscal_year" else " TEXT")
        + ("" if k in {"period_start", "fiscal_year", "fiscal_period"} else " NOT NULL")
        for k in COLS
        if k not in {"symbol", "taxonomy"}
    ]
    c.execute(
        "CREATE TABLE canonical_fact(fact_id INTEGER PRIMARY KEY," + ",".join(definitions) + ")"
    )
    for name, keys, predicate in (
        ("canonical_duration", KEY, "period_start IS NOT NULL"),
        ("canonical_instant", [k for k in KEY if k != "period_start"], "period_start IS NULL"),
    ):
        c.execute(
            f"CREATE UNIQUE INDEX {name} ON canonical_fact({','.join(keys)}) WHERE {predicate}"
        )
    c.execute(
        "CREATE TABLE fact_revision(fact_id INTEGER NOT NULL REFERENCES canonical_fact,"
        "version INTEGER NOT NULL REFERENCES fact_dataset,value TEXT NOT NULL,"
        "fiscal_year INTEGER,fiscal_period TEXT,form TEXT NOT NULL,"
        "PRIMARY KEY(fact_id,version))"
    )
    c.execute(
        "CREATE TABLE source_occurrence(id INTEGER PRIMARY KEY,"
        "original_id INTEGER UNIQUE,fact_id INTEGER NOT NULL REFERENCES canonical_fact,"
        "symbol TEXT NOT NULL,taxonomy TEXT NOT NULL,"
        "version INTEGER NOT NULL REFERENCES fact_dataset,"
        "legacy_admitted INTEGER NOT NULL CHECK(legacy_admitted IN (0,1)),"
        "forward_order_admitted INTEGER NOT NULL CHECK(forward_order_admitted IN (0,1)),"
        "evidence TEXT NOT NULL)"
    )
    c.execute(
        "CREATE TABLE ingestion_receipt(request_id TEXT PRIMARY KEY,"
        "input_hash TEXT NOT NULL,payload_hash TEXT NOT NULL,retrieved_at TEXT,"
        "version INTEGER NOT NULL REFERENCES fact_dataset,decision TEXT NOT NULL)"
    )


def import_snapshot_rows(c, rows, *, source_hash):
    """Bounded, ID-ordered import; one caller-owned atomic batch."""
    columns = ("id", *COLS)
    conflicts = []
    for raw in rows:
        row = dict(zip(columns, raw, strict=True))
        fid = _lookup(c, row)
        if fid is None:
            fid = row["id"]
            _insert(
                c,
                "canonical_fact",
                {
                    "fact_id": fid,
                    **{k: row[k] for k in COLS if k not in {"symbol", "taxonomy"}},
                },
            )
            _insert(
                c,
                "fact_revision",
                {"fact_id": fid, "version": 1, **{k: row[k] for k in MUTABLE}},
            )
        else:
            old = c.execute(
                "SELECT value,fiscal_year,fiscal_period,form FROM canonical_fact WHERE fact_id=?",
                (fid,),
            ).fetchone()
            if tuple(old) != tuple(row[k] for k in MUTABLE):
                conflicts.append({"fact_id": fid, "canonical": tuple(old), "occurrence": row})
        _insert(
            c,
            "source_occurrence",
            dict(
                id=row["id"],
                original_id=row["id"],
                fact_id=fid,
                symbol=row["symbol"],
                taxonomy=row["taxonomy"],
                version=1,
                legacy_admitted=1,
                forward_order_admitted=1,
                evidence=source_hash,
            ),
        )
    if conflicts:
        raise ValueError("UNRESOLVED_CONTENT_CONFLICT:" + json.dumps(conflicts))


def _publish_readers(c):
    """Publish the V1 Reader schema inside the caller's transaction."""
    c.execute("CREATE INDEX occurrence_membership ON source_occurrence(fact_id,symbol,taxonomy)")
    c.execute(
        "CREATE INDEX occurrence_legacy_reader ON source_occurrence(symbol,fact_id) "
        "WHERE legacy_admitted=1"
    )
    c.execute(
        "CREATE INDEX canonical_forward_reader ON canonical_fact(cik,filed,period_end,fact_id)"
    )
    projection = ["o.id"] + [
        ("o." if k in {"symbol", "taxonomy"} else "r." if k in MUTABLE else "f.") + k for k in COLS
    ]
    for role, flag in (("legacy", "legacy_admitted"), ("forward", "forward_order_admitted")):
        c.execute(
            f"CREATE VIEW {role}_occ_versions_v1 AS SELECT d.version AS dataset_version,"
            + ",".join(projection)
            + " FROM fact_dataset d JOIN source_occurrence o "
            "ON o.version<=d.version JOIN canonical_fact f ON f.fact_id=o.fact_id "
            "JOIN fact_revision r ON r.fact_id=f.fact_id AND r.version=(SELECT MAX(v.version) "
            "FROM fact_revision v WHERE v.fact_id=f.fact_id AND v.version<=d.version) "
            f"WHERE o.{flag}=1"
        )
        current_projection = ["o.id"] + [
            ("o." if k in {"symbol", "taxonomy"} else "f.") + k for k in COLS
        ]
        c.execute(
            f"CREATE VIEW {role}_occ_v1 AS SELECT "
            + ",".join(current_projection)
            + " FROM source_occurrence o JOIN canonical_fact f ON f.fact_id=o.fact_id "
            f"WHERE o.{flag}=1"
        )
    c.execute("CREATE VIEW fundamental_facts AS SELECT * FROM legacy_occ_v1")
    c.execute("UPDATE fact_contract SET schema_hash=?", (schema_fingerprint(c),))


def publish_snapshot(c, *, dataset_id, source_hash):
    """Called only after complete import; reader indexes and views publish atomically."""
    _publish_readers(c)
    _insert(
        c,
        "ingestion_receipt",
        dict(
            request_id="snapshot:" + dataset_id,
            input_hash=source_hash,
            payload_hash=source_hash,
            retrieved_at=None,
            version=1,
            decision="snapshot: all original occurrences admitted",
        ),
    )


def build_snapshot(c, *, dataset_id, source_hash):
    """Small offline fixtures. File migrations must use the manifest executor."""
    if not dataset_id or len(source_hash) != 64:
        raise ValueError("SNAPSHOT_BINDING_REQUIRED")
    if not c.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("SNAPSHOT_FOREIGN_KEYS_REQUIRED")
    with atomic(c):
        create_storage(c, dataset_id=dataset_id, source_hash=source_hash)
        import_snapshot_rows(
            c,
            c.execute("SELECT id," + ",".join(COLS) + " FROM fundamental_facts ORDER BY id"),
            source_hash=source_hash,
        )
        c.execute("ALTER TABLE fundamental_facts RENAME TO canonical_import_archive")
        publish_snapshot(c, dataset_id=dataset_id, source_hash=source_hash)


def write_company(
    c,
    company,
    facts,
    accessions,
    *,
    request_id,
    payload_hash,
    retrieved_at,
    identity: RequestIdentity,
    admissions,
    expected_version,
):
    """New writer, deliberately unregistered with runtime/SEC sync.

    admissions maps (source symbol, taxonomy) to Admission. Missing evidence is
    an error, including for replays. Retries reuse a request ID and exact input.
    Different request IDs retain provenance but never add replay occurrences.
    An external transaction must acquire its write lock before other reads.
    """
    rows = [dict(zip(COLS, row, strict=True)) for row in _fact_rows(facts)]
    accessions = sorted(set(accessions))
    decisions = {str(k): asdict(v) for k, v in admissions.items()}
    ih = fingerprint(
        [
            company.model_dump(),
            rows,
            accessions,
            asdict(identity),
            decisions,
            payload_hash,
            retrieved_at,
        ]
    )
    with atomic(c):
        require_contract(c)
        # Serialize external deferred transactions too (busy errors propagate).
        c.execute("UPDATE fact_contract SET current_version=current_version WHERE singleton=1")
        old = c.execute(
            "SELECT input_hash,version FROM ingestion_receipt WHERE request_id=?", (request_id,)
        ).fetchone()
        if old:
            if old[0] != ih:
                raise ValueError("RECEIPT_INPUT_CONFLICT")
            return old[1]
        current = c.execute("SELECT current_version FROM fact_contract").fetchone()[0]
        if current != expected_version:
            raise ValueError("STALE_DATASET_VERSION")
        if (
            not request_id
            or not payload_hash
            or not retrieved_at
            or not identity.evidence
            or identity.unresolved_conflict
            or identity.cik != company.cik
            or identity.symbol != company.symbol
        ):
            raise ValueError("IDENTITY_ASSIGNMENT_REJECTED")
        # Do not overwrite a conflicting existing local identity.
        local = c.execute("SELECT cik FROM companies WHERE symbol=?", (company.symbol,)).fetchone()
        conflict = c.execute(
            "SELECT value FROM sync_state WHERE source='sec_identity_conflicts' AND key=?",
            (company.symbol,),
        ).fetchone()
        unresolved = conflict and json.loads(conflict[0]).get("status") == "unresolved"
        reference = c.execute(
            "SELECT value FROM sync_state WHERE source='sec_reference' AND key='ticker_to_cik'"
        ).fetchone()
        mapping = json.loads(reference[0]) if reference else {}
        proposed = mapping.get(company.symbol)
        if (
            (local and local[0] != company.cik)
            or unresolved
            or (proposed and str(proposed).zfill(10) != company.cik)
        ):
            raise ValueError("IDENTITY_ASSIGNMENT_REJECTED")
        request_values = {}
        for row in rows:
            admission = admissions.get((row["symbol"], row["taxonomy"]))
            if row["cik"] != company.cik or not admission or not admission.evidence:
                raise ValueError("SOURCE_MEMBERSHIP_ADMISSION_REQUIRED")
            key = tuple(row[k] for k in KEY)
            content = tuple(row[k] for k in MUTABLE)
            if request_values.setdefault(key, content) != content:
                raise ValueError("UNRESOLVED_CONTENT_CONFLICT_IN_REQUEST")
        version = current + 1
        c.execute("INSERT INTO fact_dataset VALUES (?,?,?)", (version, request_id, ih))
        c.execute(
            "INSERT INTO companies VALUES (?,?,?,?,?,?) ON CONFLICT(cik) DO UPDATE SET "
            "symbol=excluded.symbol,name=excluded.name,sic=excluded.sic,"
            "sic_description=excluded.sic_description,updated_at=excluded.updated_at",
            (
                company.cik,
                company.symbol,
                company.name,
                company.sic,
                company.sic_description,
                retrieved_at,
            ),
        )
        for row in rows:
            admission = admissions[row["symbol"], row["taxonomy"]]
            fid = _lookup(c, row)
            new = fid is None
            changed = new
            if new:
                if not admission.forward_order_admitted:
                    raise ValueError("NEW_FACT_REQUIRES_FORWARD_ANCHOR")
                fid = _insert(
                    c,
                    "canonical_fact",
                    {k: row[k] for k in COLS if k not in {"symbol", "taxonomy"}},
                ).lastrowid
            else:
                previous = c.execute(
                    "SELECT value,fiscal_year,fiscal_period,form "
                    "FROM canonical_fact WHERE fact_id=?",
                    (fid,),
                ).fetchone()
                changed = tuple(previous) != tuple(row[k] for k in MUTABLE)
                if changed:
                    c.execute(
                        "UPDATE canonical_fact SET "
                        + ",".join(k + "=?" for k in MUTABLE)
                        + " WHERE fact_id=?",
                        [row[k] for k in MUTABLE] + [fid],
                    )
            if changed:
                c.execute(
                    "INSERT INTO fact_revision VALUES (?,?,?,?,?,?) ON CONFLICT(fact_id,version) "
                    "DO UPDATE SET value=excluded.value,fiscal_year=excluded.fiscal_year,"
                    "fiscal_period=excluded.fiscal_period,form=excluded.form",
                    (fid, version, *(row[k] for k in MUTABLE)),
                )
            membership = c.execute(
                "SELECT MAX(legacy_admitted) FROM source_occurrence "
                "WHERE fact_id=? AND symbol=? AND taxonomy=?",
                (fid, row["symbol"], row["taxonomy"]),
            ).fetchone()[0]
            if membership is None or (admission.legacy_admitted and not membership):
                # A new label for an existing identity never gets a forward tie anchor.
                _insert(
                    c,
                    "source_occurrence",
                    dict(
                        fact_id=fid,
                        symbol=row["symbol"],
                        taxonomy=row["taxonomy"],
                        version=version,
                        legacy_admitted=int(admission.legacy_admitted),
                        forward_order_admitted=int(new),
                        evidence=admission.evidence,
                    ),
                )
        c.execute(
            "INSERT INTO sync_state VALUES ('sec_accessions',?,?,?) "
            "ON CONFLICT(source,key) DO UPDATE SET updated_at=excluded.updated_at,"
            "value=excluded.value",
            (company.cik, retrieved_at, json.dumps(accessions)),
        )
        c.execute(
            "DELETE FROM sync_state WHERE source='sec_companyfacts_status' AND key=?",
            (company.cik,),
        )
        _insert(
            c,
            "ingestion_receipt",
            dict(
                request_id=request_id,
                input_hash=ih,
                payload_hash=payload_hash,
                retrieved_at=retrieved_at,
                version=version,
                decision=json.dumps(decisions, sort_keys=True),
            ),
        )
        c.execute("UPDATE fact_contract SET current_version=? WHERE singleton=1", (version,))
        return version


def forward_relation(c):
    """Legacy DBs retain the existing SQL. A-V1 requires its exact schema marker."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='fact_contract'").fetchone():
        require_contract(c, writer=False)
        return "forward_occ_v1"
    return "fundamental_facts"
