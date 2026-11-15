"""Disabled copy rehearsal entry point. No source/backup opening or copying.

The manifest is an execution input, not an authorization. A separate reviewed
change must enable execution after G0-G2 are satisfied. Validation is useful on
synthetic files today and deliberately cannot create a missing work file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from trading_system.data.canonical_facts import CONTRACT

EXECUTION_ENABLED = False


def validate_work_manifest(manifest):
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
    paths = [Path(manifest[k]).absolute() for k in ("source", "backup", "work")]
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
    if any(Path(str(work) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("WORK_SIDECAR_PRESENT")
    h = hashlib.sha256()
    with work.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != manifest["work_sha256"]:
        raise ValueError("WORK_HASH_MISMATCH")
    return work


def migrate_work(manifest):
    # Gate before any path access. No command-line bypass or environment flag.
    if not EXECUTION_ENABLED:
        raise RuntimeError("COPY_REHEARSAL_DISABLED_G0_G2_REQUIRED")
    raise RuntimeError("COPY_REHEARSAL_EXECUTOR_REQUIRES_SEPARATE_REVIEW")
