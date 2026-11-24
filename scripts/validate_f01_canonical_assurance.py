#!/usr/bin/env python3
"""Inspect an explicitly authorized, quiescent Canonical A-V1 research target."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True

from f01_research_evidence import REPO  # noqa: E402
from prepare_f01_historical_50_batch import HASH, plain, plain_tree, read_bound  # noqa: E402

from trading_system.data.canonical_bootstrap import (  # noqa: E402
    BOOTSTRAP_CONTRACT,
    CLASSIFICATION,
    DB_NAME,
    _manifest_scope,
)
from trading_system.data.canonical_facts import (  # noqa: E402
    KEY,
    MUTABLE,
    require_contract,
)
from trading_system.data.canonical_paths import pinned_files  # noqa: E402

CONTRACT = "F01_CANONICAL_DUPLICATE_ASSURANCE_V1"
NY = ZoneInfo("America/New_York")
PROTECTED = {"f01-sec-isolated-research-accounting-e11", "f01-sec-isolated-research-pilot01"}
COUNTS = (
    "canonical_fact",
    "source_occurrence",
    "fact_revision",
    "fact_dataset",
    "ingestion_receipt",
)
CHECKS = {
    "canonical_identity_collision_groups": (
        "SELECT COUNT(*) FROM (SELECT 1 FROM canonical_fact GROUP BY "
        + ",".join(KEY)
        + " HAVING count(*)>1)"
    ),
    "invalid_fact_periods": """
        SELECT count(*) FROM canonical_fact
        WHERE period_end='' OR filed='' OR period_end > filed
           OR (period_start IS NOT NULL AND period_start > period_end)
    """,
    "forward_anchor_violations": """
        SELECT count(*) FROM canonical_fact f WHERE 1 <> (
            SELECT count(*) FROM source_occurrence o
            WHERE o.fact_id=f.fact_id AND o.forward_order_admitted=1
        )
    """,
    "repeat_membership_groups": """
        SELECT count(*) FROM (
            SELECT 1 FROM source_occurrence WHERE original_id IS NULL
            GROUP BY fact_id,symbol,taxonomy
            HAVING count(*)>2 OR count(*)=2 AND (
                min(legacy_admitted)=max(legacy_admitted)
                OR min(CASE WHEN legacy_admitted=1 THEN version END)
                    <= min(CASE WHEN legacy_admitted=0 THEN version END)
            )
        )
    """,
    "missing_source_bindings": """
        SELECT count(*) FROM source_occurrence o
        JOIN fact_dataset d ON d.version=o.version
        LEFT JOIN ingestion_receipt i ON i.version=o.version
        WHERE trim(o.evidence)='' OR i.request_id IS NULL
    """,
    "invalid_occurrence_version": """
        SELECT count(*) FROM source_occurrence o
        WHERE NOT EXISTS (SELECT 1 FROM fact_revision r
            WHERE r.fact_id=o.fact_id AND r.version<=o.version)
    """,
    "invalid_dataset_receipts": """
        SELECT count(*) FROM fact_dataset d
        WHERE d.version>1 AND (
            1<>(SELECT count(*) FROM ingestion_receipt i WHERE i.version=d.version)
            OR NOT EXISTS (SELECT 1 FROM ingestion_receipt i WHERE i.version=d.version
                AND i.input_hash=d.source_hash AND i.request_id=d.dataset_id)
        )
    """,
    "invalid_dataset_ledger": """
        SELECT count(*) FROM fact_contract
        WHERE current_version<>(SELECT max(version) FROM fact_dataset)
           OR current_version<>(SELECT count(*) FROM fact_dataset)
           OR 1<>(SELECT min(version) FROM fact_dataset)
    """,
    "duplicate_request_id_groups": """
        SELECT count(*) FROM (
            SELECT 1 FROM ingestion_receipt GROUP BY request_id HAVING count(*)>1
        )
    """,
    "unbound_revisions": """
        SELECT count(*) FROM fact_revision r WHERE NOT EXISTS (
            SELECT 1 FROM ingestion_receipt i WHERE i.version=r.version
        )
    """,
    "revision_current_mismatch": """
        SELECT count(*) FROM canonical_fact f
        LEFT JOIN fact_revision r ON r.fact_id=f.fact_id AND r.version=(
            SELECT max(version) FROM fact_revision WHERE fact_id=f.fact_id
        ) WHERE r.fact_id IS NULL OR """
    + " OR ".join(f"r.{column} IS NOT f.{column}" for column in MUTABLE),
}

EXTERNAL_GATES = {
    "source_membership_and_accounting_review": "Review admission against content-hashed originals.",
    "pit_original_witness": "Compare PIT selections with independently reviewed originals.",
    "security_epoch_and_corporate_actions": "Require security and effective/known-at intervals.",
    "market_provenance": "Bind provider, feed, adjustment, security and session.",
}


def pit_failures(connection, witnesses):
    failures = []
    for witness in witnesses:
        cutoff = datetime.fromisoformat(witness["decision_at"])
        accepted = datetime.fromisoformat(witness["accepted_at"])
        if cutoff.utcoffset() is None or accepted.utcoffset() is None:
            raise ValueError("PIT_WITNESS_MUST_BE_TIMEZONE_AWARE")
        row = connection.execute(
            """SELECT v.filed, v.accession_number, v.value
               FROM forward_occ_versions_v1 v JOIN source_occurrence o ON o.id=v.id
               WHERE o.fact_id=? AND v.dataset_version=?""",
            (witness["fact_id"], witness["dataset_version"]),
        ).fetchall()
        if (
            accepted > cutoff
            or len(row) != 1
            or row[0][1:] != (witness["accession"], witness["value"])
            or date.fromisoformat(row[0][0]) > cutoff.astimezone(NY).date()
        ):
            failures.append(witness)
    return failures


def inspect(connection, *, witnesses=()):
    schema_error = None
    try:
        require_contract(connection, writer=False)
    except (sqlite3.DatabaseError, ValueError) as error:
        schema_error = str(error)
    counts = {
        name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in COUNTS
    }
    violations = {name: connection.execute(sql).fetchone()[0] for name, sql in CHECKS.items()}
    variants = connection.execute(
        "SELECT count(*) FROM (SELECT fact_id FROM fact_revision GROUP BY fact_id "
        "HAVING count(*)>1)"
    ).fetchone()[0]
    foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    pit = pit_failures(connection, witnesses)
    if schema_error or foreign_keys or any(violations.values()) or pit:
        status = "FINDINGS"
    else:
        status = "STORAGE_CHECKS_PASS"
    return {
        "contract": CONTRACT,
        "status": status,
        "counts": counts,
        "violations": violations,
        "schema_error": schema_error,
        "foreign_key_issues": foreign_keys,
        "facts_with_multiple_revisions_for_review": variants,
        "pit": {
            "witnesses_checked": len(witnesses),
            "failures": pit,
            "status": "FINDINGS" if pit else "CHECKED" if witnesses else "UNVERIFIED",
        },
        "remaining_evidence_gates": EXTERNAL_GATES,
    }


def _validate_path(database):
    if (
        not database.is_absolute()
        or ".." in database.parts
        or database.name != DB_NAME
        or not database.parent.name.startswith("f01-sec-isolated-research-")
        or database.parent.name in PROTECTED
        or any(database.is_relative_to(REPO / name) for name in ("data", "reports"))
    ):
        raise ValueError("EXPLICIT_NEW_ISOLATED_RESEARCH_TARGET_REQUIRED")
    plain_tree(database.parent)
    return plain(database)


def _check_quiescent(database):
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            Path(str(database) + suffix).lstat()
        except FileNotFoundError:
            continue
        raise ValueError(f"SQLITE_SIDECAR_PRESENT: {suffix}")


def inspect_target(database, *, expected_manifest, witnesses=()):
    if not HASH.fullmatch(expected_manifest):
        raise ValueError("EXPECTED_BOOTSTRAP_MANIFEST_SHA256_REQUIRED")
    initial = _validate_path(database)
    with pinned_files({"assurance": database}) as pins:
        if not os.path.samestat(initial, os.fstat(pins["assurance"].fileno())):
            raise ValueError("ASSURANCE_TARGET_REPLACED")
        _validate_path(database)
        _check_quiescent(database)
        uri = database.as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA query_only=ON")
            raw_lineage, manifest = connection.execute(
                "SELECT evidence,manifest FROM research_bootstrap WHERE singleton=1"
            ).fetchone()
            scope = _manifest_scope(manifest, expected_manifest)
            lineage = json.loads(raw_lineage)
            bound = connection.execute(
                "SELECT dataset_id,source_hash FROM fact_dataset WHERE version=1"
            ).fetchone()
            if (
                lineage["bootstrap_contract"] != BOOTSTRAP_CONTRACT
                or lineage["classification"] != CLASSIFICATION
                or lineage["manifest_sha256"] != expected_manifest
                or lineage["research_scope"] != scope
                or bound != (lineage["dataset_id"], expected_manifest)
            ):
                raise ValueError("ASSURANCE_LINEAGE_MISMATCH")
            report = inspect(connection, witnesses=witnesses)
        _check_quiescent(database)
    return report | {
        "target": str(database),
        "dataset_id": lineage["dataset_id"],
        "bootstrap_manifest_sha256": expected_manifest,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--expect-bootstrap-manifest-sha256", required=True)
    parser.add_argument("--authorize-isolated-read", action="store_true", required=True)
    parser.add_argument("--pit-witnesses", type=Path)
    args = parser.parse_args(argv)
    try:
        witnesses, binding = [], None
        if args.pit_witnesses:
            raw, binding = read_bound(args.pit_witnesses)
            witnesses = json.loads(raw)
            if not isinstance(witnesses, list):
                raise ValueError("PIT_WITNESSES_MUST_BE_A_LIST")
        report = inspect_target(
            args.target,
            expected_manifest=args.expect_bootstrap_manifest_sha256,
            witnesses=witnesses,
        )
        print(json.dumps(report | {"pit_witnesses_binding": binding}, indent=2, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError) as error:
        parser.exit(2, f"{error}\n")
    return int(report["status"] == "FINDINGS")


if __name__ == "__main__":
    raise SystemExit(main())
