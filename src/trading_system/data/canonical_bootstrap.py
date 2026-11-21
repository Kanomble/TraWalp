"""Explicit, offline construction of an empty Canonical A-V1 research dataset."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from trading_system.data.canonical_facts import (
    CONTRACT,
    _publish_readers,
    create_storage,
    require_contract,
    schema_fingerprint,
)
from trading_system.data.canonical_paths import pinned_files
from trading_system.data.database import _initial_schema, _migrate_daily_bars
from trading_system.data.forward_market import migrate as create_forward_market_schema

BOOTSTRAP_CONTRACT = "F01_CLEAN_CANONICAL_RESEARCH_BOOTSTRAP_V1"
CLASSIFICATION = "RESEARCH_ONLY"
DB_NAME = "f01-sec-candidate.sqlite3"
MIN_FREE_BYTES = 64 * 1024**2  # Empty schema only, not an import or backup budget.
MANIFEST_MAX_BYTES = 64 * 1024
REPO = Path(__file__).resolve().parents[3]
_RESEARCH_NAME = re.compile(r"f01-sec-isolated-research-[a-z0-9][a-z0-9-]{0,47}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class BootstrapBlocked(ValueError):
    """An authorization, isolation, resource or evidence check failed."""


@dataclass(frozen=True)
class _Plan:
    target: Path
    staging: Path
    free_bytes: int
    research_scope: str
    manifest_sha256: str
    code_fingerprint: str

    def record(self) -> dict:
        return {
            "contract": BOOTSTRAP_CONTRACT,
            "classification": CLASSIFICATION,
            "schema_version": CONTRACT,
            "target": str(self.target),
            "staging": str(self.staging),
            "free_bytes": self.free_bytes,
            "minimum_free_bytes": MIN_FREE_BYTES,
            "research_scope": self.research_scope,
            "manifest_sha256": self.manifest_sha256,
            "code_fingerprint": self.code_fingerprint,
        }


def _entry(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_redirect(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _plain_directory(path: Path) -> os.stat_result:
    info = _entry(path)
    if info is None or _is_redirect(info) or not stat.S_ISDIR(info.st_mode):
        raise BootstrapBlocked(f"BOOTSTRAP_UNSAFE_DIRECTORY: {path}")
    return info


def _plain_staged_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if _is_redirect(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BootstrapBlocked(f"BOOTSTRAP_UNSAFE_STAGING_FILE: {path}")
    return info


def _local_drive(path: Path) -> None:
    if os.name != "nt":
        raise BootstrapBlocked("BOOTSTRAP_REQUIRES_WINDOWS_NO_REPLACE_PUBLICATION")

    import ctypes
    from ctypes import wintypes

    drive_type = ctypes.WinDLL("kernel32").GetDriveTypeW
    drive_type.argtypes = [wintypes.LPCWSTR]
    drive_type.restype = wintypes.UINT
    if drive_type(path.anchor) != 3:  # DRIVE_FIXED
        raise BootstrapBlocked(f"BOOTSTRAP_REQUIRES_LOCAL_FIXED_VOLUME: {path.anchor}")


def _validate_target(target: Path) -> tuple[Path, int]:
    if (
        not target.is_absolute()
        or not re.fullmatch(r"[A-Za-z]:", target.drive)
        or any(
            part == ".." or ":" in part or part.endswith((".", " ")) for part in target.parts[1:]
        )
        or target.name != DB_NAME
        or not _RESEARCH_NAME.fullmatch(target.parent.name)
    ):
        raise BootstrapBlocked("BOOTSTRAP_RESEARCH_TARGET_REQUIRED")
    root = target.parent.parent
    _local_drive(root)
    # Check every component, including junctions and other Windows reparse points.
    for component in reversed((root, *root.parents)):
        _plain_directory(component)
    if root.resolve(strict=True) != root or any(
        root.is_relative_to(REPO / protected) for protected in ("data", "reports")
    ):
        raise BootstrapBlocked(f"BOOTSTRAP_PROTECTED_OR_REDIRECTED_PATH: {root}")
    if _entry(target.parent) is not None:
        raise BootstrapBlocked(f"BOOTSTRAP_TARGET_ALREADY_EXISTS: {target.parent}")
    staging = root / f".{target.parent.name}.bootstrap-incomplete"
    if _entry(staging) is not None:
        raise BootstrapBlocked(f"BOOTSTRAP_STAGING_REQUIRES_MANUAL_REVIEW: {staging}")
    free_bytes = shutil.disk_usage(root).free
    if free_bytes < MIN_FREE_BYTES:
        raise BootstrapBlocked(f"BOOTSTRAP_INSUFFICIENT_SPACE: {free_bytes} < {MIN_FREE_BYTES}")
    return staging, free_bytes


def _manifest_scope(raw: bytes, expected_sha256: str) -> str:
    if (
        not _SHA256.fullmatch(expected_sha256)
        or len(raw) > MANIFEST_MAX_BYTES
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
        raise BootstrapBlocked("BOOTSTRAP_MANIFEST_BINDING_MISMATCH")

    def unique_fields(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise BootstrapBlocked("BOOTSTRAP_MANIFEST_DUPLICATE_FIELD")
        return fields

    try:
        manifest = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_fields)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BootstrapBlocked("BOOTSTRAP_MANIFEST_INVALID_JSON") from error
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"contract", "classification", "research_scope"}
        or manifest["contract"] != BOOTSTRAP_CONTRACT
        or manifest["classification"] != CLASSIFICATION
        or not isinstance(manifest["research_scope"], str)
        or not manifest["research_scope"].strip()
        or len(manifest["research_scope"]) > 1024
    ):
        raise BootstrapBlocked("BOOTSTRAP_MANIFEST_SCOPE_INVALID")
    return manifest["research_scope"]


def preflight(
    target: Path, *, manifest: bytes, manifest_sha256: str, code_fingerprint: str
) -> _Plan:
    """Check metadata and bindings without opening a database or creating paths."""
    if not _SHA256.fullmatch(code_fingerprint):
        raise BootstrapBlocked("BOOTSTRAP_CODE_FINGERPRINT_REQUIRED")
    scope = _manifest_scope(manifest, manifest_sha256)
    staging, free_bytes = _validate_target(target)
    return _Plan(target, staging, free_bytes, scope, manifest_sha256, code_fingerprint)


def _write_database(path: Path, manifest: bytes, lineage: dict) -> str:
    with path.open("xb"):
        pass
    _plain_staged_file(path)
    with (
        pinned_files({"work": path}),
        closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as connection,
    ):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=EXTRA")
        with connection:
            # executescript would commit an existing transaction. Start our transaction
            # inside the script and run subsequent statements with execute().
            connection.executescript("BEGIN IMMEDIATE;\n" + _initial_schema(legacy_facts=False))
            _migrate_daily_bars(connection)
            create_forward_market_schema(connection)
            create_storage(
                connection, dataset_id=lineage["dataset_id"], source_hash=lineage["manifest_sha256"]
            )
            connection.execute(
                """CREATE TABLE research_bootstrap (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    initial_version INTEGER NOT NULL CHECK(initial_version=1)
                        REFERENCES fact_dataset(version),
                    evidence TEXT NOT NULL,
                    manifest BLOB NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO research_bootstrap VALUES (1,1,?,?)",
                (json.dumps(lineage, sort_keys=True), manifest),
            )
            _publish_readers(connection)
            require_contract(connection)
        # Verify the committed, unpublished result before preparing its external report.
        require_contract(connection)
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or (
            connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise BootstrapBlocked("BOOTSTRAP_INTEGRITY_CHECK_FAILED")
        return schema_fingerprint(connection)


def _write_artifact(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _prepare_publication(plan: _Plan, manifest: bytes, verify_code: Callable[[], str]) -> dict:
    lineage = {
        "bootstrap_contract": BOOTSTRAP_CONTRACT,
        "classification": CLASSIFICATION,
        "dataset_id": f"f01-research-{uuid4().hex}",
        "dataset_origin": "EMPTY_CANONICAL_RESEARCH_BOOTSTRAP",
        "initial_version": 1,
        "schema_version": CONTRACT,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "code_fingerprint": plan.code_fingerprint,
        "manifest_sha256": plan.manifest_sha256,
        "research_scope": plan.research_scope,
    }
    staged_db = plan.staging / DB_NAME
    schema_hash = _write_database(staged_db, manifest, lineage)
    if set(plan.staging.iterdir()) != {staged_db}:
        raise BootstrapBlocked("BOOTSTRAP_UNEXPECTED_STAGING_ENTRIES_OR_SQLITE_SIDECARS")
    info = _plain_staged_file(staged_db)
    with staged_db.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise BootstrapBlocked("BOOTSTRAP_STAGING_DATABASE_REPLACED")
        database_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {
        "status": "PUBLISHED_EMPTY_RESEARCH_DATASET",
        "database": str(plan.target),
        "database_sha256_at_bootstrap": database_sha256,
        "schema_fingerprint": schema_hash,
        "initial_ingestion_receipts": 0,
        "lineage": lineage,
    }
    _write_artifact(plan.staging / "bootstrap_manifest.json", manifest)
    _write_artifact(
        plan.staging / "bootstrap_result.json",
        (json.dumps(report, indent=2, sort_keys=True) + "\n").encode(),
    )
    if verify_code() != plan.code_fingerprint:
        raise BootstrapBlocked("BOOTSTRAP_CODE_DRIFT_BEFORE_PUBLICATION")
    for name in (DB_NAME, "bootstrap_manifest.json", "bootstrap_result.json"):
        _plain_staged_file(plan.staging / name)
    return report


def bootstrap(
    target: Path,
    *,
    manifest: bytes,
    manifest_sha256: str,
    expected_code_fingerprint: str,
    verify_code: Callable[[], str],
    authorized: bool = False,
    progress: Callable[[str], None] = lambda _message: None,
) -> dict:
    """Build and publish a new directory. A retained incomplete directory requires review."""
    if not authorized:
        raise BootstrapBlocked("BOOTSTRAP_EXECUTION_AUTHORIZATION_REQUIRED")
    if verify_code() != expected_code_fingerprint:
        raise BootstrapBlocked("BOOTSTRAP_CODE_FINGERPRINT_MISMATCH")
    plan = preflight(
        target,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        code_fingerprint=expected_code_fingerprint,
    )
    root = target.parent.parent
    with pinned_files({}, directories=(root,)):
        # Recheck all paths and capacity after acquiring mandatory parent pins.
        _validate_target(target)
        plan.staging.mkdir()
        staged_directory = _plain_directory(plan.staging)
        progress(f"Creating and validating isolated staging directory: {plan.staging}")
        with pinned_files({}, directories=(plan.staging,)):
            if not os.path.samestat(staged_directory, _plain_directory(plan.staging)):
                raise BootstrapBlocked("BOOTSTRAP_STAGING_DIRECTORY_REPLACED")
            report = _prepare_publication(plan, manifest, verify_code)
        if not os.path.samestat(staged_directory, _plain_directory(plan.staging)):
            raise BootstrapBlocked("BOOTSTRAP_STAGING_DIRECTORY_REPLACED")
        # Windows rename is a same-volume, no-replace operation. Publish the database,
        # manifest and result together. Any existing destination causes failure.
        progress(f"Publishing the empty research dataset: {target.parent}")
        plan.staging.rename(target.parent)
    return report
