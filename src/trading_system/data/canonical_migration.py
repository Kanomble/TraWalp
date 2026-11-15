"""Disabled copy rehearsal entry point. No Source/Backup SQLite opens or copying.

The manifest is an execution input, not an authorization. A separate reviewed
change must enable execution after G0-G2 are satisfied. Validation is useful on
synthetic files today and deliberately cannot create a missing work file.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

from trading_system.data.canonical_facts import (
    COLS,
    CONTRACT,
    atomic,
    create_storage,
    fingerprint,
    import_snapshot_rows,
    publish_snapshot,
)
from trading_system.data.canonical_paths import pinned_files

EXECUTION_ENABLED = False
EXECUTOR_VERSION = "F01_BATCH_EXECUTOR_V1"


def validate_work_manifest(manifest, *, resume=False):
    required = {
        "contract",
        "plan_id",
        "source",
        "backup",
        "work",
        "work_sha256",
        "source_sha256",
        "backup_sha256",
        "expected_counts",
        "input_inventory",
        "consumer_inventory",
        "capacity_budget",
        "restore_evidence",
    }
    if not required <= manifest.keys() or manifest["contract"] != CONTRACT:
        raise ValueError("INCOMPLETE_WORK_MANIFEST")
    if any(not manifest[k] for k in required):
        raise ValueError("EMPTY_MANIFEST_EVIDENCE")
    paths = [Path(manifest[k]) for k in ("source", "backup", "work")]
    if any(not p.is_absolute() or ".." in p.parts for p in paths):
        raise ValueError("ABSOLUTE_UNAMBIGUOUS_MANIFEST_PATHS_REQUIRED")
    for path in paths:
        for part in (path, *path.parents):
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise ValueError("LINKED_PATH_FORBIDDEN")
        if not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError("MISSING_OR_HARDLINKED_FILE")
    if any(
        a.resolve() == b.resolve() or a.samefile(b)
        for i, a in enumerate(paths)
        for b in paths[i + 1 :]
    ):
        raise ValueError("SOURCE_BACKUP_WORK_MUST_DIFFER")
    work = paths[2]
    for path in paths[:2]:
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("PROTECTED_SNAPSHOT_SIDECAR_PRESENT")
    if any(Path(str(work) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise ValueError("WORK_SIDECAR_PRESENT")
    if not resume and Path(str(work) + "-journal").exists():
        raise ValueError("WORK_SIDECAR_PRESENT")
    if not resume:
        if manifest["work_sha256"] != manifest["backup_sha256"]:
            raise ValueError("WORK_BACKUP_HASH_MISMATCH")
        with work.open("rb") as stream:
            if file_hash(stream) != manifest["work_sha256"]:
                raise ValueError("WORK_HASH_MISMATCH")
    return work


def migrate_work(manifest, *, batch_size=10000, resume=False, hook=None):
    # Gate before any path access. No command-line bypass or environment flag.
    if not EXECUTION_ENABLED:
        raise RuntimeError("COPY_REHEARSAL_DISABLED_G0_G2_REQUIRED")
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 50000:
        raise ValueError("BOUNDED_BATCH_SIZE_REQUIRED")
    # Additional immutable execution evidence, beyond the preparation validator.
    for field in ("sql_schema_hash", "fact_stream_hash", "file_identities"):
        if not manifest.get(field):
            raise ValueError("EXECUTION_MANIFEST_BINDING_REQUIRED:" + field)
    paths = {k: Path(manifest[k]).absolute() for k in ("source", "backup", "work")}
    with pinned_files(paths) as pins:
        work = validate_work_manifest(manifest, resume=resume)
        for role, stream in pins.items():
            stat = os.fstat(stream.fileno())
            if [stat.st_dev, stat.st_ino] != manifest["file_identities"][role]:
                raise ValueError("MANIFEST_FILE_IDENTITY_MISMATCH")
            if role != "work" and file_hash(stream) != manifest[role + "_sha256"]:
                raise ValueError("PROTECTED_SNAPSHOT_HASH_MISMATCH")
        if hook:
            hook("paths_pinned", None)
        # Open existing Work only, with all pins still held. DELETE journal mode
        # supports crash recovery on restart without trusting a new Work hash.
        c = sqlite3.connect(work.as_uri() + "?mode=rw", uri=True)
        try:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA journal_mode=DELETE")
            c.execute("PRAGMA synchronous=FULL")
            c.execute("PRAGMA cache_size=-8192")
            c.execute("PRAGMA temp_store=FILE")
            return execute_batches(c, manifest, batch_size=batch_size, resume=resume, hook=hook)
        finally:
            c.close()


def file_hash(stream):
    stream.seek(0)
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def sql_schema_hash(c):
    return fingerprint(
        [
            [
                c.execute("PRAGMA user_version").fetchone()[0],
                c.execute("PRAGMA application_id").fetchone()[0],
            ],
            [
                tuple(r)
                for r in c.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' "
                    "ORDER BY type,name"
                )
            ],
        ]
    )


def fact_stream_hash(c, table):
    if table not in {"fundamental_facts", "canonical_import_archive"}:
        raise ValueError("INVALID_ARCHIVE_RELATION")
    h = hashlib.sha256()
    for row in c.execute("SELECT id," + ",".join(COLS) + " FROM " + table + " ORDER BY id"):
        h.update((fingerprint(tuple(row)) + "\n").encode())
    return h.hexdigest()


def _emit(hook, event, c):
    if hook:
        hook(event, c)


def _mapping_hash(c, low, high):
    h = hashlib.sha256()
    for row in c.execute(
        "SELECT id,fact_id FROM source_occurrence WHERE id>? AND id<=? ORDER BY id", (low, high)
    ):
        h.update((fingerprint(tuple(row)) + "\n").encode())
    return h.hexdigest()


def verify_progress(c, manifest):
    """Verify all committed ranges, not merely an untrusted last_id cursor."""
    meta = c.execute(
        "SELECT binding,state,last_id,rows_done,schema_hash FROM canonical_migration "
        "WHERE singleton=1"
    ).fetchone()
    if not meta or meta[0] != fingerprint([EXECUTOR_VERSION, manifest]):
        raise ValueError("MIGRATION_MANIFEST_DRIFT")
    if meta[1] not in {"BUILDING", "COMPLETE"}:
        raise ValueError("MIGRATION_STATE_DRIFT")
    if meta[4] != sql_schema_hash(c):
        raise ValueError("MIGRATION_SCHEMA_DRIFT")
    if fact_stream_hash(c, "canonical_import_archive") != manifest["fact_stream_hash"]:
        raise ValueError("ARCHIVE_CONTENT_DRIFT")
    minimum = c.execute("SELECT MIN(id) FROM canonical_import_archive").fetchone()[0]
    previous, total, chain = min(0, minimum - 1) if minimum is not None else 0, 0, "0" * 64
    for batch in c.execute(
        "SELECT low_id,high_id,input_count,mapping_hash,chain_hash "
        "FROM canonical_migration_batch ORDER BY batch_id"
    ):
        low, high, count, mapping, digest = batch
        actual = c.execute(
            "SELECT COUNT(*) FROM source_occurrence WHERE id>? AND id<=?", (low, high)
        ).fetchone()[0]
        if (
            low != previous
            or count != actual
            or mapping != _mapping_hash(c, low, high)
            or digest != fingerprint([chain, low, high, count, mapping])
        ):
            raise ValueError("MIGRATION_LEDGER_DRIFT")
        total += count
        previous, chain = high, digest
    if (previous, total) != (meta[2], meta[3]):
        raise ValueError("MIGRATION_CURSOR_DRIFT")
    if c.execute("SELECT COUNT(*) FROM source_occurrence").fetchone()[0] != total:
        raise ValueError("UNLEDGERED_OCCURRENCES")
    return meta


def _validate_final(c, manifest):
    counts = manifest["expected_counts"]
    for key, table in [("occurrences", "source_occurrence"), ("canonical", "canonical_fact")]:
        if (
            key not in counts
            or c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] != counts[key]
        ):
            raise ValueError("MIGRATION_COUNT_MISMATCH:" + key)
    # Full ordered identity/content equivalence, including unknown metrics and IDs.
    from itertools import zip_longest

    for view in ["legacy_occ_v1", "forward_occ_v1"]:
        old = c.execute(
            "SELECT id," + ",".join(COLS) + " FROM canonical_import_archive ORDER BY id"
        )
        new = c.execute("SELECT id," + ",".join(COLS) + " FROM " + view + " ORDER BY id")
        for left, right in zip_longest(old, new):
            if left is None or right is None or tuple(left) != tuple(right):
                raise ValueError("MIGRATION_STREAM_MISMATCH")
    if [tuple(r) for r in c.execute("PRAGMA integrity_check")] != [("ok",)]:
        raise ValueError("MIGRATION_INTEGRITY_FAILURE")
    if c.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("MIGRATION_FOREIGN_KEY_FAILURE")


def execute_batches(c, manifest, *, batch_size, resume=False, hook=None):
    """Connection primitive for synthetic tests and the pinned file entry point.

    It must own the transaction: each durable checkpoint is one atomic batch.
    No CLI registration, runtime discovery, or source/backup connection exists.
    """
    if c.in_transaction or not c.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("MIGRATION_REQUIRES_OWNED_FK_CONNECTION")
    if not 1 <= batch_size <= 50000:
        raise ValueError("BOUNDED_BATCH_SIZE_REQUIRED")
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='canonical_migration'").fetchone()
    if exists:
        if not resume:
            raise ValueError("EXPLICIT_RESUME_REQUIRED")
        meta = verify_progress(c, manifest)
        if meta[1] == "COMPLETE":
            _validate_final(c, manifest)
            return {"state": "COMPLETE", "rows": meta[3]}
    else:
        if resume:
            raise ValueError("RESUME_LEDGER_MISSING")
        if sql_schema_hash(c) != manifest["sql_schema_hash"]:
            raise ValueError("INPUT_SCHEMA_HASH_MISMATCH")
        if fact_stream_hash(c, "fundamental_facts") != manifest["fact_stream_hash"]:
            raise ValueError("INPUT_STREAM_HASH_MISMATCH")
        # Reject unsupported external references before renaming anything.
        for name, sql in c.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('view','trigger')"
        ):
            if sql and "fundamental_facts" in sql.lower():
                raise ValueError("UNREVIEWED_FACT_DEPENDENCY:" + name)
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            if any(
                r[2] == "fundamental_facts"
                for r in c.execute("PRAGMA foreign_key_list(" + quoted + ")")
            ):
                raise ValueError("UNREVIEWED_FACT_FOREIGN_KEY:" + table)
        with atomic(c):
            c.execute(
                "CREATE TABLE canonical_migration(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "binding TEXT NOT NULL,state TEXT NOT NULL,last_id INTEGER NOT NULL,"
                "rows_done INTEGER NOT NULL,schema_hash TEXT NOT NULL)"
            )
            c.execute(
                "CREATE TABLE canonical_migration_batch(batch_id INTEGER PRIMARY KEY,"
                "low_id INTEGER NOT NULL,high_id INTEGER NOT NULL UNIQUE,"
                "input_count INTEGER NOT NULL,"
                "mapping_hash TEXT NOT NULL,chain_hash TEXT NOT NULL)"
            )
            c.execute("ALTER TABLE fundamental_facts RENAME TO canonical_import_archive")
            create_storage(c, dataset_id=manifest["plan_id"], source_hash=manifest["source_sha256"])
            minimum = c.execute("SELECT MIN(id) FROM canonical_import_archive").fetchone()[0]
            cursor = min(0, minimum - 1) if minimum is not None else 0
            c.execute(
                "INSERT INTO canonical_migration VALUES (1,?,'BUILDING',?,0,?)",
                (fingerprint([EXECUTOR_VERSION, manifest]), cursor, sql_schema_hash(c)),
            )
        _emit(hook, "prepared", c)
    while True:
        last, total = c.execute("SELECT last_id,rows_done FROM canonical_migration").fetchone()
        rows = c.execute(
            "SELECT id," + ",".join(COLS) + " FROM canonical_import_archive "
            "WHERE id>? ORDER BY id LIMIT ?",
            (last, batch_size),
        ).fetchall()
        if not rows:
            break
        with atomic(c):
            import_snapshot_rows(c, rows, source_hash=manifest["source_sha256"])
            high = rows[-1][0]
            mapping = _mapping_hash(c, last, high)
            prev = c.execute(
                "SELECT chain_hash FROM canonical_migration_batch ORDER BY batch_id DESC LIMIT 1"
            ).fetchone()
            chain = fingerprint([prev[0] if prev else "0" * 64, last, high, len(rows), mapping])
            c.execute(
                "INSERT INTO canonical_migration_batch VALUES (NULL,?,?,?,?,?)",
                (last, high, len(rows), mapping, chain),
            )
            c.execute(
                "UPDATE canonical_migration SET last_id=?,rows_done=? WHERE singleton=1",
                (high, total + len(rows)),
            )
            _emit(hook, "before_batch_commit", c)
        _emit(hook, "after_batch_commit", c)
    with atomic(c):
        _emit(hook, "before_index_build", c)
        publish_snapshot(c, dataset_id=manifest["plan_id"], source_hash=manifest["source_sha256"])
        _emit(hook, "after_index_build", c)
        _validate_final(c, manifest)
        c.execute(
            "UPDATE canonical_migration SET state='COMPLETE',schema_hash=? WHERE singleton=1",
            (sql_schema_hash(c),),
        )
        _emit(hook, "before_publish_commit", c)
    _emit(hook, "after_publish_commit", c)
    return {"state": "COMPLETE", "rows": total}
