#!/usr/bin/env python3
"""Offline G0-G2 preparation. Default: read-only preflight, no output files.

Never calls the migration executor. Windows mandatory sharing locks are required.
Outputs are exclusive-create; a failed job is retained for manual inspection.
Success is published by one directory rename to <job>/verified, never inferred
from a database file or an incomplete staging directory.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO / "src"))

from trading_system.config import load_strategy_config  # noqa: E402
from trading_system.data import canonical_migration as migration  # noqa: E402
from trading_system.data.canonical_facts import KEY, MUTABLE, fingerprint  # noqa: E402
from trading_system.data.canonical_paths import pinned_files  # noqa: E402

GIB = 1024**3
BASELINE = dict(
    occurrences=18698090,
    instant=13764701,
    duration=4933389,
    canonical=8329908,
    canonical_instant=3396519,
    canonical_duration=4933389,
    alias_groups=180283,
    content_conflicts=0,
)
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


class Stop(RuntimeError):
    """Actionable fail-closed preparation error."""


@dataclass(frozen=True)
class Paths:
    origin: Path
    job: Path

    @classmethod
    def documented(cls):
        if Path(r"G:\GitHub\TraWalp") != REPO:
            raise Stop("Repository liegt nicht am dokumentierten Pfad G:\\GitHub\\TraWalp.")
        configured = load_strategy_config().storage.database_path
        expected = REPO / "data/trading_system.sqlite3"
        if configured != expected:
            raise Stop(f"Konfigurationsabweichung: {configured}; erwartet {expected}. Neu planen.")
        return cls(expected, REPO / "reports/f01_copy_rehearsal/20261115_8f2a")

    def files(self):
        return dict(
            source=self.job / "reference/source.sqlite3",
            backup=self.job / "reference/backup.sqlite3",
            restore=self.job / "restore/restore.sqlite3",
            work=self.job / "work/canonical_rehearsal.sqlite3",
        )


def log(message):
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def safe_path(path, *, must_exist=False):
    if not path.is_absolute() or ".." in path.parts:
        raise Stop(f"Kein eindeutiger absoluter Pfad: {path}")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise Stop(f"Verlinkte Pfadkomponente: {part}")
    if must_exist and (not path.is_file() or path.stat().st_nlink != 1):
        raise Stop(f"Quelle fehlt, ist kein reguläres File oder ist ein Hardlink: {path}")


def metadata(path):
    if not path.exists():
        return None
    stat = path.stat()
    return dict(
        bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        identity=[stat.st_dev, stat.st_ino],
        links=stat.st_nlink,
    )


def origin_state(origin):
    return {str(p): metadata(p) for p in [origin, *[Path(str(origin) + s) for s in SIDE_SUFFIXES]]}


def kernel32():
    if os.name != "nt":
        raise Stop("Sicherer Handle-/Pfadschutz ist nur für Windows implementiert.")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def available(path):
    from ctypes import wintypes

    kernel = kernel32()
    call = kernel.GetDiskFreeSpaceExW
    call.argtypes = [wintypes.LPCWSTR, *[ctypes.POINTER(ctypes.c_ulonglong)] * 3]
    free, total, physical = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not call(str(path), ctypes.byref(free), ctypes.byref(total), ctypes.byref(physical)):
        raise ctypes.WinError(ctypes.get_last_error())
    return free.value


def windows_temp():
    buffer = ctypes.create_unicode_buffer(32768)
    if not kernel32().GetTempPathW(len(buffer), buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value)


def memory_usage():
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (key, ctypes.c_size_t)
            for key in (
                "peak_rss",
                "rss",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    record = Counters()
    record.cb = ctypes.sizeof(record)
    kernel = kernel32()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    call = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    call.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not call(kernel.GetCurrentProcess(), ctypes.byref(record), ctypes.sizeof(record)):
        raise ctypes.WinError(ctypes.get_last_error())
    return dict(rss_bytes=record.rss, process_peak_rss_bytes=record.peak_rss)


def exclusive_probe(path):
    """Read-only, momentary no-sharing probe; held pins below establish the window."""
    from ctypes import wintypes

    kernel = kernel32()
    call = kernel.CreateFileW
    call.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    call.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = call(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise Stop(
            f"Inkompatibler/offener Handle oder Zugriff verweigert: {path}; "
            f"Windows-Code {ctypes.get_last_error()}. Alle DB-Zugriffe schließen."
        )
    kernel.CloseHandle(handle)


def check_origin(paths):
    safe_path(paths.origin, must_exist=True)
    state = origin_state(paths.origin)
    for name, stat in state.items():
        if stat is not None:
            safe_path(Path(name), must_exist=True)
            if name.endswith(("-wal", "-journal")) and stat["bytes"]:
                raise Stop(f"Gefülltes WAL/Journal: {name}. Kein Export/Checkpoint ausgeführt.")
    with paths.origin.open("rb") as stream:
        header = stream.read(100)
    if len(header) != 100 or header[:16] != b"SQLite format 3\0":
        raise Stop("Ungültiger SQLite-Header.")
    page_size = struct.unpack(">H", header[16:18])[0]
    page_size = 65536 if page_size == 1 else page_size
    pages = struct.unpack(">I", header[28:32])[0]
    if (
        header[18:20] not in (b"\1\1", b"\2\2")
        or pages * page_size != state[str(paths.origin)]["bytes"]
        or header[24:28] != header[92:96]
    ):
        raise Stop("Unklare Header-/Dateikonsistenz; immutable-Lesepfad nicht zulässig.")
    return state, dict(
        page_size=page_size,
        pages=pages,
        persisted_journal_mode="WAL" if header[18] == 2 else "ROLLBACK",
    )


@contextmanager
def locked_origin(paths):
    state, header = check_origin(paths)
    protected = {
        f"origin_{i}": Path(name)
        for i, (name, stat) in enumerate(state.items())
        if stat is not None
    }
    for path in protected.values():
        exclusive_probe(path)
    # All roles deny write sharing; parents and files deny delete sharing.
    with pinned_files(protected) as pins:
        if origin_state(paths.origin) != state:
            raise Stop("Quelle/Sidecars beim Erwerb der Sperren verändert.")
        check_origin(paths)
        for name, stream in zip(protected, pins.values(), strict=True):
            if (
                list((os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino))
                != state[str(protected[name])]["identity"]
            ):
                raise Stop("Dateiidentität während der Pfadprüfung verändert.")
        try:
            yield state, header, pins
        finally:
            if origin_state(paths.origin) != state:
                raise Stop("STOP: Quelle/Sidecars verändert; niemals READY.")


def preflight(paths):
    if migration.EXECUTION_ENABLED is not False:
        raise Stop("EXECUTION_ENABLED muss False bleiben.")
    safe_path(paths.job)
    if paths.job.exists():
        raise Stop(f"Jobpfad existiert bereits: {paths.job}. Keine Dateien werden überschrieben.")
    with locked_origin(paths) as (state, header, _):
        size = state[str(paths.origin)]["bytes"]
        ancestor = paths.job.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        free, temp = available(ancestor), windows_temp()
        minimum = 15 * size + 20 * GIB
        if free < minimum or available(temp) < 2 * size + 20 * GIB:
            raise Stop(
                f"Zu wenig freier Speicher. Ziel benötigt {minimum:,} Bytes; "
                f"Temp mindestens {2 * size + 20 * GIB:,} Bytes."
            )
        return dict(
            status="PREFLIGHT_ONLY",
            origin=str(paths.origin),
            paths={k: str(p) for k, p in paths.files().items()},
            source_state=state,
            header=header,
            minimum_initial_free_bytes=minimum,
            available_bytes=free,
            temp_path=str(temp),
            temp_available_bytes=available(temp),
            source_size=size,
            execution_enabled=False,
            python=sys.version,
            sqlite=sqlite3.sqlite_version,
            os=platform.platform(),
        )


class Monitor:
    def __init__(self, paths):
        self.paths, self.start, self.last = paths, time.perf_counter(), 0.0
        self.step, self.failure, self.events = "Start", None, []

    def tick(self, step=None):
        now = time.perf_counter()
        if step is None and now - self.last < 5:
            return
        self.step = step or self.step
        self.last = now
        event = dict(
            at=datetime.now(UTC).isoformat(),
            step=self.step,
            elapsed_seconds=now - self.start,
            free_bytes=available(self.paths.job),
            **memory_usage(),
        )
        self.events.append(event)
        log(
            f"{self.step} | {event['elapsed_seconds']:.1f}s | "
            f"RSS {event['rss_bytes'] / GIB:.3f} GiB | frei {event['free_bytes'] / GIB:.2f} GiB"
        )
        if event["free_bytes"] < 20 * GIB:
            raise Stop("Speicherplatz-Untergrenze 20 GiB unterschritten.")

    def sql_progress(self):
        try:
            self.tick()
            return 0
        except BaseException as exc:
            self.failure = exc
            return 1


@contextmanager
def readonly(path, monitor=None):
    """Caller must hold native pins; never use this on an unprotected live WAL DB."""
    c = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
    try:
        c.execute("PRAGMA query_only=ON")
        c.execute("PRAGMA cache_size=-32768")
        if monitor:
            c.set_progress_handler(monitor.sql_progress, 10000)
        yield c
    finally:
        c.close()


class ProgressReader:
    def __init__(self, stream, monitor):
        self.stream, self.monitor = stream, monitor

    def seek(self, *args):
        return self.stream.seek(*args)

    def read(self, *args):
        self.monitor.tick()
        return self.stream.read(*args)


def digest(path, monitor):
    with path.open("rb") as stream:
        return migration.file_hash(ProgressReader(stream, monitor))


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def inventory(c, monitor):
    objects = [
        tuple(r)
        for r in c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
    ]
    tables = {}
    for kind, name, _, sql in objects:
        if kind != "table":
            continue
        monitor.tick("Tabelleninventar: " + name)
        columns = [tuple(r) for r in c.execute("PRAGMA table_info(" + quote(name) + ")")]
        pk = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
        order = ",".join(map(quote, pk)) if sql and "WITHOUT ROWID" in sql.upper() else "_rowid_"
        h, count = hashlib.sha256(), 0
        for row in c.execute("SELECT * FROM " + quote(name) + " ORDER BY " + order):
            h.update((fingerprint(tuple(row)) + "\n").encode())
            count += 1
        tables[name] = dict(
            rows=count,
            row_stream_sha256=h.hexdigest(),
            columns=columns,
            foreign_keys=[
                tuple(r) for r in c.execute("PRAGMA foreign_key_list(" + quote(name) + ")")
            ],
        )
    return dict(
        objects=objects,
        tables=tables,
        pragmas={
            k: c.execute("PRAGMA " + k).fetchone()[0]
            for k in ("page_size", "user_version", "application_id", "encoding")
        },
        sql_schema_hash=migration.sql_schema_hash(c),
    )


def verify_database(path, monitor):
    monitor.tick("Integrität und Inventar: " + path.name)
    with readonly(path, monitor) as c:
        if [tuple(r) for r in c.execute("PRAGMA integrity_check")] != [("ok",)]:
            raise Stop("integrity_check fehlgeschlagen: " + str(path))
        if c.execute("PRAGMA foreign_key_check").fetchone():
            raise Stop("foreign_key_check fehlgeschlagen: " + str(path))
        result = inventory(c, monitor)
        result["fact_stream_hash"] = migration.fact_stream_hash(c, "fundamental_facts")
        return result


def check_dependencies(database):
    for kind, name, _, sql in database["objects"]:
        if kind in {"view", "trigger"} and sql and "fundamental_facts" in sql.lower():
            raise Stop("Nicht vom Executor unterstützte Fact-Abhängigkeit: " + name)
    for name, table in database["tables"].items():
        if any(row[2] == "fundamental_facts" for row in table["foreign_keys"]):
            raise Stop("Nicht vom Executor unterstützter Fact-Fremdschlüssel: " + name)


def reserve(path):
    with path.open("xb") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    return metadata(path)["identity"]


def backup_export(origin, target, monitor):
    identity = reserve(target)
    with pinned_files({"source": origin, "work": target}):
        if metadata(target)["identity"] != identity:
            raise Stop("Zieldatei ausgetauscht.")
        with readonly(origin, monitor) as source:
            target_connection = sqlite3.connect(target.as_uri() + "?mode=rw", uri=True, timeout=0)
            try:
                target_connection.execute("PRAGMA synchronous=FULL")
                source.backup(
                    target_connection, pages=512, progress=lambda *_: monitor.tick(), sleep=0
                )
                # Only the NEW destination is normalized, never the production origin.
                mode = target_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if mode.lower() != "delete":
                    raise Stop("Exportziel lässt sich nicht sidecarfrei normalisieren.")
            finally:
                target_connection.close()
    if any(Path(str(target) + s).exists() for s in SIDE_SUFFIXES):
        raise Stop("Exportziel hat unerwartete Sidecars.")


def copy_new(source, target, monitor):
    identity = reserve(target)
    with pinned_files({"source": source, "work": target}):
        if metadata(target)["identity"] != identity:
            raise Stop("Kopierziel ausgetauscht.")
        with source.open("rb") as reader, target.open("r+b") as writer:
            while chunk := reader.read(1024 * 1024):
                monitor.tick()
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if digest(source, monitor) != digest(target, monitor):
            raise Stop("Kopie ist nicht bytegleich: " + str(target))


def census(source, scratch, conflicts_path, monitor):
    monitor.tick("Census: NULL-sichere Identitäten, Memberships und Inhaltsvarianten")
    keys, signature = ",".join(KEY), ",".join((*KEY, *MUTABLE))
    reserve(scratch)
    c = sqlite3.connect(scratch.as_uri() + "?mode=rw", uri=True, timeout=0)
    try:
        c.set_progress_handler(monitor.sql_progress, 10000)
        c.execute("PRAGMA temp_store=FILE")
        c.execute("PRAGMA cache_size=-32768")
        c.execute("ATTACH DATABASE ? AS snapshot", (source.as_uri() + "?mode=ro&immutable=1",))
        # Scratch is the only writable SQLite database used by the census.
        c.execute(
            "CREATE TABLE identities AS SELECT " + keys + ",COUNT(*) AS n,"
            "COUNT(DISTINCT symbol) AS symbols FROM snapshot.fundamental_facts GROUP BY " + keys
        )
        c.execute(
            "CREATE TABLE variants AS SELECT "
            + signature
            + " FROM snapshot.fundamental_facts GROUP BY "
            + signature
        )
        c.execute(
            "CREATE TABLE conflicts AS SELECT "
            + keys
            + " FROM variants GROUP BY "
            + keys
            + " HAVING COUNT(*)>1"
        )
        c.execute("CREATE INDEX conflict_identity ON conflicts(" + keys + ")")
        counts = dict(
            occurrences=c.execute("SELECT COALESCE(SUM(n),0) FROM identities").fetchone()[0],
            instant=c.execute(
                "SELECT COALESCE(SUM(n),0) FROM identities WHERE period_start IS NULL"
            ).fetchone()[0],
            duration=c.execute(
                "SELECT COALESCE(SUM(n),0) FROM identities WHERE period_start IS NOT NULL"
            ).fetchone()[0],
            canonical=c.execute("SELECT COUNT(*) FROM identities").fetchone()[0],
            canonical_instant=c.execute(
                "SELECT COUNT(*) FROM identities WHERE period_start IS NULL"
            ).fetchone()[0],
            canonical_duration=c.execute(
                "SELECT COUNT(*) FROM identities WHERE period_start IS NOT NULL"
            ).fetchone()[0],
            alias_groups=c.execute("SELECT COUNT(*) FROM identities WHERE symbols>1").fetchone()[0],
            content_conflicts=c.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0],
        )
        for label, extras in [
            ("source_memberships", "symbol"),
            ("source_taxonomy_memberships", "symbol,taxonomy"),
        ]:
            monitor.tick("Census: " + label)
            counts[label] = c.execute(
                "SELECT COUNT(*) FROM (SELECT "
                + keys
                + ","
                + extras
                + " FROM snapshot.fundamental_facts GROUP BY "
                + keys
                + ","
                + extras
                + ")"
            ).fetchone()[0]
        with conflicts_path.open("x", encoding="utf-8") as stream:
            if counts["content_conflicts"]:
                sql = (
                    "SELECT f.* FROM snapshot.fundamental_facts f JOIN conflicts g ON "
                    + " AND ".join("f." + k + " IS g." + k for k in KEY)
                    + " ORDER BY f.id"
                )
                cursor = c.execute(sql)
                columns = [r[0] for r in cursor.description]
                for row in cursor:
                    stream.write(
                        json.dumps(dict(zip(columns, row, strict=True)), ensure_ascii=False) + "\n"
                    )
        c.commit()
        return counts
    finally:
        c.close()


def repository_evidence(root):
    head = subprocess.run(
        ["git", "-c", "safe.directory=" + root.as_posix(), "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    files, consumers = {}, []
    candidates = sorted(
        {
            p
            for folder in ("src", "scripts", "config")
            for p in (root / folder).rglob("*")
            if p.is_file()
            and p.suffix in (".py", ".yaml", ".yml", ".sql", ".json")
            and "__pycache__" not in p.parts
        }
    )
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        files[relative] = hashlib.sha256(raw).hexdigest()
        lines = [
            i
            for i, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1)
            if any(
                token in line
                for token in ("fundamental_facts", "canonical_fact", "source_occurrence")
            )
        ]
        if lines:
            consumers.append(
                dict(
                    path=relative,
                    sha256=files[relative],
                    lines=lines,
                    classification="repository SQL/schema consumer candidate",
                )
            )
    for path in (root / "pyproject.toml", root / "requirements.txt", root / ".env"):
        if path.exists():
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    packages = {
        d.metadata["Name"]: d.version
        for d in importlib.metadata.distributions()
        if d.metadata["Name"]
    }
    return dict(
        git_head=head,
        files=files,
        code_fingerprint=fingerprint([files, packages, sys.version, sqlite3.sqlite_version]),
        runtime_packages=packages,
        consumers=consumers,
        external_coverage="requires explicit reviewed declaration",
    )


def check_review(path, repo):
    if path is None:
        raise Stop(
            "Externe Consumer-/Input-Evidenz fehlt: --consumer-evidence <JSON>. "
            "Vorlage und Code-Fingerprint stehen im Preflight."
        )
    safe_path(path, must_exist=True)
    review = json.loads(path.read_text(encoding="utf-8-sig"))
    verify_review(review, repo)
    return review


def verify_review(review, repo, monitor=None):
    if not isinstance(review, dict):
        raise Stop("Consumer-Review muss ein JSON-Objekt sein.")
    if (
        review.get("git_head") != repo["git_head"]
        or review.get("code_fingerprint") != repo["code_fingerprint"]
        or review.get("external_inventory_complete") is not True
        or not isinstance(review.get("reviewer"), str)
        or not review["reviewer"].strip()
        or review.get("repository_consumers_reviewed") is not True
        or review.get("inputs_complete") is not True
        or review.get("unknown_consumers") != []
        or not isinstance(review.get("external_files"), list)
    ):
        raise Stop(
            "Consumer-/Input-Review unvollständig, ungeklärt oder nicht an diesen Code gebunden."
        )
    for entry in review["external_files"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("sha256"), str)
        ):
            raise Stop("Jeder externe Input benötigt einen absoluten path und sha256.")
        file = Path(entry["path"])
        safe_path(file, must_exist=True)
        with file.open("rb") as stream:
            reader = ProgressReader(stream, monitor) if monitor else stream
            if migration.file_hash(reader) != entry["sha256"]:
                raise Stop("Externer Input verändert: " + str(file))


def write_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(
            value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
        )
        stream.flush()
        os.fsync(stream.fileno())


def prepare(paths, repo, review, baseline=BASELINE):
    verify_review(review, repo)
    pre = preflight(paths)
    # Never replace the earlier Phase-A reports, draft manifest, or a failed job.
    paths.job.mkdir(parents=True, exist_ok=False)
    job_identity = metadata(paths.job)["identity"]
    lock = paths.job / "preparation.lock"
    lock_identity = reserve(lock)
    with pinned_files({"job_lock": lock}):
        safe_path(paths.job)
        if (
            metadata(paths.job)["identity"] != job_identity
            or metadata(lock)["identity"] != lock_identity
        ):
            raise Stop("Jobverzeichnis oder Reservierung ausgetauscht.")
        return prepare_locked(paths, repo, review, pre, baseline)


def prepare_locked(paths, repo, review, pre, baseline):
    for folder in ("reference", "restore", "work", "scratch", "publication.pending"):
        (paths.job / folder).mkdir(exist_ok=False)
    monitor = Monitor(paths)
    files = paths.files()
    old_env = {k: os.environ.get(k) for k in ("TEMP", "TMP")}
    try:
        os.environ["TEMP"] = os.environ["TMP"] = str(paths.job / "scratch")
        if windows_temp().resolve() != (paths.job / "scratch").resolve():
            raise Stop("Windows-/SQLite-Temp ist nicht auf den genehmigten Scratch-Pfad gebunden.")
        with locked_origin(paths) as (state, header, pins):
            monitor.tick("Unveränderlichen Origin-Zustand hashen")
            original_hashes = {
                str(p): migration.file_hash(ProgressReader(pins[k], monitor))
                for k, p in zip(
                    pins, [Path(n) for n, s in state.items() if s is not None], strict=True
                )
            }
            monitor.tick("Konsistenter SQLite-Source-Export (Origin immutable + native Pins)")
            backup_export(paths.origin, files["source"], monitor)
            with pinned_files({"source": files["source"]}):
                source_inventory = verify_database(files["source"], monitor)
                check_dependencies(source_inventory)
                monitor.tick("Unabhängiges Backup erstellen und verifizieren")
                copy_new(files["source"], files["backup"], monitor)
                with pinned_files({"backup": files["backup"]}):
                    backup_inventory = verify_database(files["backup"], monitor)
                    if backup_inventory != source_inventory:
                        raise Stop("Source-/Backup-Inventar unterscheiden sich.")
                    monitor.tick("Unabhängige SQLite-Restore-Probe")
                    backup_export(files["backup"], files["restore"], monitor)
                    with pinned_files({"restore": files["restore"]}):
                        restored = verify_database(files["restore"], monitor)
                    if restored != backup_inventory:
                        raise Stop("Restore-Probe nicht logisch identisch zum Backup.")
                    monitor.tick("Bytegleiche Work-Kopie")
                    copy_new(files["backup"], files["work"], monitor)
                    with pinned_files({"work_reference": files["work"]}):
                        counts = census(
                            files["source"],
                            paths.job / "scratch/census.sqlite3",
                            paths.job / "content_conflicts.jsonl",
                            monitor,
                        )
                        if counts["content_conflicts"]:
                            raise Stop(
                                "Inhaltskonflikte: vollständige Zeilen in content_conflicts.jsonl."
                            )
                        differences = {
                            k: dict(expected=v, actual=counts.get(k))
                            for k, v in baseline.items()
                            if counts.get(k) != v
                        }
                        if differences:
                            write_new(paths.job / "count_differences.json", differences)
                            raise Stop(
                                "Snapshot-Zähler weichen vom Audit ab. Keine automatische Toleranz."
                            )
                        current_repo = repository_evidence(REPO)
                        if (
                            current_repo["code_fingerprint"] != repo["code_fingerprint"]
                            or current_repo["git_head"] != repo["git_head"]
                        ):
                            raise Stop("Code/Konfiguration während Vorbereitung verändert.")
                        verify_review(review, repo, monitor)
                        manifest = dict(
                            contract=migration.CONTRACT,
                            plan_id="F01_8F2A_20261115_VERIFIED",
                            executor_version=migration.EXECUTOR_VERSION,
                            **{k: str(files[k]) for k in ("source", "backup", "work")},
                            **{
                                k + "_sha256": digest(files[k], monitor)
                                for k in ("source", "backup", "work")
                            },
                            file_identities={
                                k: metadata(files[k])["identity"]
                                for k in ("source", "backup", "work")
                            },
                            sql_schema_hash=source_inventory["sql_schema_hash"],
                            fact_stream_hash=source_inventory["fact_stream_hash"],
                            expected_counts=counts,
                            input_inventory=dict(
                                database=source_inventory, repository=repo, external_review=review
                            ),
                            consumer_inventory=dict(
                                repository=repo["consumers"], external_review=review
                            ),
                            capacity_budget=dict(
                                preflight=pre,
                                reserve_bytes=20 * GIB,
                                interval_seconds=5,
                                scratch=str(paths.job / "scratch"),
                                initial_factor=15,
                            ),
                            restore_evidence=dict(
                                path=str(files["restore"]),
                                sha256=digest(files["restore"], monitor),
                                verified_inventory_fingerprint=fingerprint(restored),
                            ),
                            production_origin=dict(
                                path=str(paths.origin), state=state, hashes=original_hashes
                            ),
                            migration_executed=False,
                            execution_enabled=False,
                        )
                        monitor.tick("Executor-Manifest validieren (keine Migration)")
                        migration.validate_work_manifest(manifest)
                        # Verify origin and sidecars before publication under held pins.
                        for k, p in zip(
                            pins, [Path(n) for n, s in state.items() if s is not None], strict=True
                        ):
                            if (
                                migration.file_hash(ProgressReader(pins[k], monitor))
                                != original_hashes[str(p)]
                            ):
                                raise Stop("Origin/Sidecar-Hash verändert.")
                        if (
                            origin_state(paths.origin) != state
                            or migration.EXECUTION_ENABLED is not False
                        ):
                            raise Stop("Origin-Zustand oder Executor-Aktivierung verändert.")
                        publish(paths, manifest, monitor)
        log("G0_G2_READY: " + str(paths.job / "verified"))
        return manifest
    except BaseException as exc:
        actual = monitor.failure or exc
        failure = dict(
            status="ABORTED" if isinstance(actual, KeyboardInterrupt) else "BLOCKED",
            error=str(actual) or "Strg+C",
            step=monitor.step,
            events=monitor.events,
            elapsed_seconds=time.perf_counter() - monitor.start,
            incomplete_artifacts_retained=True,
            migration_executed=False,
        )
        try:
            write_new(paths.job / "failure.json", failure)
            write_new(
                paths.job / "failure.md",
                f"# Vorbereitung {failure['status']}\n\n"
                f"Schritt: {monitor.step}\n\nFehler: {failure['error']}\n\n"
                "Keine Freigabe. Unvollständige Dateien unverändert zur Prüfung behalten.\n",
            )
        except OSError:
            log("Fehlerreport konnte nicht sicher neu geschrieben werden; siehe Konsolenausgabe.")
        if actual is exc:
            raise
        raise actual from exc
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def publish(paths, manifest, monitor):
    monitor.tick("Abnahmeberichte veröffentlichen")
    staging = paths.job / "publication.pending"
    report = dict(
        status="G0_G2_READY",
        recognition_rule="valid only in job/verified with matching seal",
        elapsed_seconds=time.perf_counter() - monitor.start,
        events=monitor.events,
        expected_counts=manifest["expected_counts"],
        migration_executed=False,
        execution_enabled=False,
        **memory_usage(),
    )
    write_new(staging / "f01_copy_rehearsal_manifest.json", manifest)
    write_new(staging / "f01_preparation_report.json", report)
    text = (
        "# F01 reale Migrationsvorbereitung\n\nStatus: **G0_G2_READY**. "
        "Gültig ausschließlich im atomar veröffentlichten Verzeichnis `verified` "
        "mit passendem `seal.json`; `publication.pending` ist nie eine Freigabe.\n\n"
        "Keine Migration, kein Checkpoint/Write an der produktiven Quelle. "
        "`EXECUTION_ENABLED` bleibt False.\n\n"
        f"Laufzeit: {report['elapsed_seconds']:.2f} s; Prozess-Peak-RSS: "
        f"{report['process_peak_rss_bytes']} Bytes.\n\n"
        "## Gebundene Dateien\n\n"
        + "".join(f"- {k}: `{manifest[k]}`\n" for k in ("source", "backup", "work"))
        + "\n## Tatsächliche Counts\n\n```json\n"
        + json.dumps(manifest["expected_counts"], indent=2)
        + "\n```\n\nInventare, Restore-Evidenz und unveränderte Origin-/Sidecar-Hashes: "
        "siehe Manifest. Consumer-Review ist eine explizite lokale Erklärung, "
        "keine automatische Behauptung über unbekannte externe Anwendungen.\n\n"
        "Freiplatz vor späterem Lauf erneut messen; 20 GiB Reserve, fünfsekündige "
        "Überwachung, STOP bei Drift/Budgetverletzung. Kein Cutover-/Migrations-GO.\n"
    )
    write_new(staging / "f01_real_migration_preparation_audit.md", text)
    seal = {p.name: digest(p, monitor) for p in staging.iterdir() if p.is_file()}
    write_new(staging / "seal.json", seal)
    # Windows rename fails if destination exists. No replace(), no overwrite.
    staging.rename(paths.job / "verified")


def main(argv=None, *, paths=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Kopien und G0-G2-Evidenz erstellen")
    parser.add_argument(
        "--confirm-no-writers", action="store_true", help="Reader-/writerfreies Fenster bestätigen"
    )
    parser.add_argument(
        "--consumer-evidence", type=Path, help="Überprüfte lokale Consumer-/Input-Erklärung"
    )
    args = parser.parse_args(argv)
    try:
        paths = paths or Paths.documented()
        log("Read-only Preflight; keine Kopien, Hash-Census oder DB-SQL an der Produktivquelle")
        pre = preflight(paths)
        repo = repository_evidence(REPO)
        template = dict(
            git_head=repo["git_head"],
            code_fingerprint=repo["code_fingerprint"],
            reviewer="",
            external_inventory_complete=False,
            inputs_complete=False,
            repository_consumers_reviewed=False,
            unknown_consumers=["ZU PRÜFEN"],
            external_files=[],
        )
        print(
            json.dumps(
                dict(
                    preflight=pre,
                    consumer_review_template=template,
                    repository_consumers=repo["consumers"],
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
        if not (args.prepare and args.confirm_no_writers):
            log("Nur Preflight. Vorbereitung erfordert --prepare UND --confirm-no-writers.")
            return 0
        # Local paths and operational data must remain ignored by Git.
        ignored = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=" + REPO.as_posix(),
                "check-ignore",
                str(paths.job / "verified/f01_copy_rehearsal_manifest.json"),
            ],
            cwd=REPO,
            capture_output=True,
        )
        if ignored.returncode != 0:
            raise Stop("Sensible Ausgabe ist nicht durch .gitignore geschützt.")
        review = check_review(
            args.consumer_evidence.absolute() if args.consumer_evidence else None, repo
        )
        prepare(paths, repo, review)
        return 0
    except KeyboardInterrupt:
        log("ABORTED: Strg+C. Keine Freigabe; eventuell vorhandene Teilartefakte prüfen.")
        print(json.dumps(dict(status="ABORTED", exit_code=130, migration_executed=False)))
        return 130
    except (Stop, OSError, sqlite3.Error, ValueError, subprocess.SubprocessError) as exc:
        log(f"STOP / BLOCKED: {exc}")
        print(
            json.dumps(
                dict(status="BLOCKED", error=str(exc), migration_executed=False), ensure_ascii=False
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
