"""Metadateninventar für den umfassenden F01-Audit unter Windows."""

import csv
import ctypes
import os
import re
import shutil
import stat
from collections import Counter
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path

REPARSE = 0x400
DB = re.compile(r"\.(?:sqlite3?|db)(?:$|-(?:wal|shm|journal)$)", re.I)
REPO = Path(__file__).resolve().parents[2]
RESEARCH = Path("G:/TraWalpResearch")


def csv_out(path, rows, fields=None):
    rows = iter(rows)
    first = next(rows, None)
    with path.open("x", encoding="utf-8", newline="") as stream:
        if first is not None or fields:
            writer = csv.DictWriter(stream, fieldnames=fields or list(first), lineterminator="\n")
            writer.writeheader()
            if first is not None:
                writer.writerow(first)
                writer.writerows(rows)


def category(root, rel):
    parts = rel.lower().split("/")
    folder = parts[0]
    if root == "repo":
        if folder == "data":
            return "PRODUCTIVE_DATA"
        if folder in {".git", ".idea", ".agents", ".codex", ".aws"}:
            return "REPO_ADMIN"
        if folder == ".venv":
            return "REPO_RUNTIME"
        if folder.startswith((".tmp_pytest_", ".pytest_cache", ".ruff_cache")):
            return "TEST_SCRATCH"
        if folder == "reports":
            group = parts[1] if len(parts) > 1 else ""
            if group.startswith("f01_copy_rehearsal") or ("migration" in group and len(parts) > 2):
                return "MIGRATION_EVIDENCE"
            if group.startswith("f01-reboot-test-"):
                return "TEST_SCRATCH"
            if group == "baselines":
                return "BASELINE_EVIDENCE"
            return "REPO_REPORTS"
        return "REPO_SOURCE_AND_OTHER"
    if folder == "research-databases":
        return "PROTECTED_RESEARCH_DB"
    if folder == "sec-originals":
        if "filing-evidence" in parts:
            return "ARELLE_EVIDENCE" if any("shard" in p for p in parts) else "ORIGINAL_FILINGS"
        return "SEC_BULK_ORIGINALS"
    return {
        "sec-staging": "SEC_STAGING",
        "sec-backups": "RESEARCH_BACKUP",
        "arelle": "ARELLE_RUNTIME_TAXONOMY",
        "edgartools": "EDGARTOOLS_ENV",
        "historical-market": "HISTORICAL_MARKET",
        "import-artifacts": "RESEARCH_ARTIFACTS",
    }.get(folder, "RESEARCH_OTHER")


def windows_allocation():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    info.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]

    class Standard(ctypes.Structure):
        _fields_ = [
            ("allocation", ctypes.c_int64),
            ("end", ctypes.c_int64),
            ("links", wintypes.DWORD),
            ("delete_pending", ctypes.c_bool),
            ("directory", ctypes.c_bool),
        ]

    def allocated(path, expected):
        handle = create(str(path), 0x80, 7, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            return None
        try:
            data = Standard()
            if (
                info(handle, 1, ctypes.byref(data), ctypes.sizeof(data))
                and data.end == expected
                and not data.delete_pending
            ):
                return data.allocation
            return None
        finally:
            close(handle)

    return allocated


def census(roots, output):
    allocation = windows_allocation()
    stats = Counter()
    blocked = []

    def error(path, exc):
        blocked.append(
            {
                "path": str(path),
                "error_type": type(exc).__name__,
                "winerror": getattr(exc, "winerror", None),
            }
        )

    def visit(root_key, top, path):
        rel = path.relative_to(top).as_posix()
        if path == output:
            return 0, 0
        kind = category(root_key, rel)
        try:
            item = path.lstat()
        except OSError as exc:
            error(path, exc)
            return 0, 0
        attrs = getattr(item, "st_file_attributes", 0)
        is_link = bool(attrs & REPARSE) or stat.S_ISLNK(item.st_mode)
        is_dir = stat.S_ISDIR(item.st_mode)
        allocated = None
        if is_link:
            file_type, length, count = "REPARSE_NOT_TRAVERSED", 0, 0
        elif is_dir:
            file_type, length, count = "DIRECTORY", 0, 0
            try:
                with os.scandir(path) as children:
                    names = sorted((entry.name for entry in children), key=str.casefold)
            except OSError as exc:
                names = []
                error(path, exc)
            for name in names:
                child_bytes, child_count = yield from visit(root_key, top, path / name)
                length += child_bytes
                count += child_count
        else:
            length, count = item.st_size, 1
            file_type = "SQLITE_OR_SIDECAR" if DB.search(path.name) else "FILE"
            if length >= 1024**2 or kind == "TEST_SCRATCH":
                allocated = allocation(path, length)
        stats[file_type] += 1
        yield {
            "path": str(path),
            "root": root_key,
            "relative": rel,
            "category": kind,
            "file_type": file_type,
            "logical_bytes": length,
            "allocated_bytes": allocated,
            "descendant_files": count if is_dir and not is_link else "",
            "modified_utc": datetime.fromtimestamp(item.st_mtime, UTC).isoformat(),
            "volume_id": item.st_dev,
            "file_id": item.st_ino,
            "hardlink_count": item.st_nlink,
            "windows_attributes_hex": hex(attrs),
            "reparse_tag_hex": hex(getattr(item, "st_reparse_tag", 0)),
            "sha256": "",
            "sha256_status": "NOT_READ_METADATA_ONLY",
        }
        return length, count

    def rows():
        for key, path in roots.items():
            yield from visit(key, path, path)

    csv_out(output / "storage_inventory.csv", rows())
    disk = shutil.disk_usage(RESEARCH)
    return {
        "counts": dict(stats),
        "unreadable": blocked,
        "g_total_bytes": disk.total,
        "g_free_bytes": disk.free,
        "g_used_bytes": disk.used,
        "allocation_method": "FileStandardInfo; >=1 MiB and all scoped test scratch files",
    }
