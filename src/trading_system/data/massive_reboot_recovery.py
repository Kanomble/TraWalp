"""Explicit Windows reboot recovery for the shared Massive limiter."""

import base64
import ctypes
import errno
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from trading_system.data.massive_rate_limit import (
    MassiveReferenceError,
    _lock_operation,
    _state_path,
)

# Kernel event, CIM and tick uptime can describe slightly different boot phases.
BOOT_MARGIN = 15.0
BOOT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$entry = Get-WinEvent -FilterHashtable @{
    LogName='System'; ProviderName='Microsoft-Windows-Kernel-General'; Id=12
} -MaxEvents 1
$start = @(([xml]$entry.ToXml()).Event.EventData.Data |
    Where-Object Name -eq 'StartTime')
if ($start.Count -ne 1 -or -not $entry.RecordId) { throw 'Missing boot event' }
$cim = Get-CimInstance -ClassName Win32_OperatingSystem
@{
    event_id = [long]$entry.RecordId
    event_start = [string]$start[0].'#text'
    os_start = $cim.LastBootUpTime.ToUniversalTime().ToString('o')
} | ConvertTo-Json -Compress
"""


def _windows_boot_evidence():
    if os.name != "nt":
        raise MassiveReferenceError("WINDOWS_BOOT_EVIDENCE_UNAVAILABLE")
    shell = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        result = subprocess.run(
            [str(shell), "-NoProfile", "-NonInteractive", "-Command", BOOT_SCRIPT],
            capture_output=True,
            check=True,
            timeout=20,
        )
        evidence = json.loads(result.stdout)
        ticks = ctypes.windll.kernel32.GetTickCount64
        ticks.restype = ctypes.c_ulonglong
        return evidence | {"uptime_seconds": ticks() / 1000}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        raise MassiveReferenceError("WINDOWS_BOOT_EVIDENCE_UNAVAILABLE") from None


def _timestamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError
    return stamp.timestamp()


def _snapshot(limiter, path, lock):
    lock.seek(0)
    if lock.read(2) != b"1":
        raise MassiveReferenceError("RECOVERY_LOCK_MARKER_INVALID")
    with path.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise MassiveReferenceError("MASSIVE_RATE_LIMIT_STATE_INVALID")
    try:
        state = limiter._decode_state(raw.decode("utf-8"))
    except UnicodeError:
        raise MassiveReferenceError("MASSIVE_RATE_LIMIT_STATE_INVALID") from None
    return raw, state


def _assess(limiter, state, read_boot):
    if state["in_flight"]:
        raise MassiveReferenceError("RECOVERY_IN_FLIGHT")
    previous = state["last_clock"]
    now = limiter._now()
    if now[0] < previous[0]:
        raise MassiveReferenceError("RECOVERY_WALL_CLOCK_ROLLBACK")
    try:
        limiter._now(previous)
    except MassiveReferenceError as exc:
        if str(exc) != "MASSIVE_RATE_LIMIT_CLOCK_ANOMALY":
            raise
    else:
        return None, now
    boot = read_boot()
    now = limiter._now(now)
    try:
        event, cim = (_timestamp(boot[key]) for key in ("event_start", "os_start"))
        uptime = boot["uptime_seconds"]
        if (
            type(boot["event_id"]) is not int
            or boot["event_id"] <= 0
            or not limiter._valid_pair([event, uptime])
            or abs(event - cim) > BOOT_MARGIN
            or abs(now[0] - uptime - cim) > BOOT_MARGIN
        ):
            raise ValueError
    except (TypeError, ValueError, KeyError, OverflowError, OSError):
        raise MassiveReferenceError("WINDOWS_BOOT_EVIDENCE_UNCLEAR") from None
    if previous[0] >= min(event, cim) - BOOT_MARGIN:
        raise MassiveReferenceError("RECOVERY_STATE_NOT_PROVEN_PREBOOT")
    elapsed = min(uptime, now[0] - max(event, cim) - BOOT_MARGIN, now[0] - previous[0])
    if elapsed < limiter._expiry_seconds:
        raise MassiveReferenceError("RECOVERY_EXPIRY_WINDOW_NOT_PROVEN")
    return boot | {"minimum_elapsed_seconds": elapsed}, now


@contextmanager
def _lock_for_recovery(limiter, path):
    if not limiter._lock.acquire(blocking=False):
        raise MassiveReferenceError("RECOVERY_LOCK_BUSY")
    try:
        # Open existing inode only. The normal initialization path can create a lock.
        with path.open("r+b", buffering=0) as lock:
            try:
                _lock_operation(lock, acquire=True)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise MassiveReferenceError("RECOVERY_LOCK_BUSY") from None
                raise
            try:
                yield lock
            finally:
                _lock_operation(lock, acquire=False)
    finally:
        limiter._lock.release()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _publish(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".massive-recovery-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        # Same-volume no-clobber publication. An existing receipt is never overwritten.
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def reboot_recovery(limiter, *, apply=False, read_boot=_windows_boot_evidence):
    """Advisory read-only by default. Apply rechecks all conditions under the shared OS lock."""
    path = limiter._state_path if limiter._state_path is not None else _state_path()
    lock_path = path.with_suffix(".lock")
    prepared = None
    state_reset = False
    try:
        context = _lock_for_recovery(limiter, lock_path) if apply else lock_path.open("rb")
        with context as lock:
            raw, state = _snapshot(limiter, path, lock)
            boot, now = _assess(limiter, state, read_boot)
            if not apply and _snapshot(limiter, path, lock)[0] != raw:
                raise MassiveReferenceError("RECOVERY_DRY_RUN_STATE_CHANGED")
            if boot is None:
                return {"status": "RECOVERY_NOT_REQUIRED", "state_reset": False}
            evidence = {
                "boot": boot,
                "previous_slots": len(state["timestamps"]),
                "previous_state_sha256": _sha(raw),
                "lock_marker": "1",
            }
            if not apply:
                return {"status": "REBOOT_RECOVERY_ELIGIBLE", "state_reset": False, **evidence}
            replacement = {"timestamps": [], "last_clock": now, "in_flight": False}
            expected_sha = _sha(json.dumps(replacement, separators=(",", ":")).encode())
            receipt = path.with_name(f"{path.stem}.reboot-{uuid4().hex}.prepared.json")
            _publish(
                receipt,
                {
                    "contract": "MASSIVE_WINDOWS_REBOOT_RECOVERY_V1",
                    "status": "PREPARED",
                    "prepared_at_utc": datetime.fromtimestamp(now[0], UTC).isoformat(),
                    "previous_state_base64": base64.b64encode(raw).decode(),
                    "expected_state_sha256": expected_sha,
                    **evidence,
                },
            )
            prepared = receipt
            limiter._now(now)
            limiter._save(path, replacement, lock)
            state_reset = True
            if _sha(path.read_bytes()) != expected_sha:
                raise MassiveReferenceError("RECOVERY_STATE_VERIFICATION_FAILED")
            committed = receipt.with_name(receipt.name.replace(".prepared.json", ".committed.json"))
            _publish(
                committed,
                {
                    "contract": "MASSIVE_WINDOWS_REBOOT_RECOVERY_V1",
                    "status": "COMMITTED",
                    "prepared_receipt": prepared.name,
                    "prepared_receipt_sha256": _sha(prepared.read_bytes()),
                    "state_sha256": expected_sha,
                },
            )
            return {
                "status": "RECOVERED",
                "state_reset": True,
                "prepared_receipt": str(prepared),
                "committed_receipt": str(committed),
            }
    except (OSError, MassiveReferenceError) as exc:
        reason = (
            str(exc) if isinstance(exc, MassiveReferenceError) else "RECOVERY_STORAGE_UNAVAILABLE"
        )
        return {
            "status": "RECOVERY_INCOMPLETE_REVIEW_REQUIRED" if prepared else "RECOVERY_REFUSED",
            "reason": reason,
            "state_reset": state_reset if not prepared or state_reset else None,
            **({"prepared_receipt": str(prepared)} if prepared else {}),
        }
