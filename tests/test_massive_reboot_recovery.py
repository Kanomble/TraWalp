"""Offline Windows-reboot recovery tests with an isolated state and boot witness."""

import base64
import hashlib
import json
import threading
from datetime import UTC, datetime

from trading_system.data.massive_rate_limit import (
    MassiveReferenceError,
    _RollingWindowLimiter,
)
from trading_system.data.massive_reboot_recovery import reboot_recovery

WALL = 1_790_000_000
PRIOR = [WALL - 600, 5000]


def seeded(tmp_path, *, in_flight=False, wall=WALL):
    state_path = tmp_path / "rate-limit.json"
    state_path.with_suffix(".lock").write_bytes(b"1")
    raw = json.dumps({"timestamps": [PRIOR], "last_clock": PRIOR, "in_flight": in_flight}).encode()
    state_path.write_bytes(raw)
    return _RollingWindowLimiter(
        state_path=state_path, clock=lambda: 120, wall_clock=lambda: wall
    ), raw


def reboot():
    return {
        "event_id": 12,
        "event_start": datetime.fromtimestamp(WALL - 125, UTC).isoformat(),
        "os_start": datetime.fromtimestamp(WALL - 120, UTC).isoformat(),
        "uptime_seconds": 120,
    }


def test_valid_reboot_dry_run_then_atomic_receipt_and_recovery(tmp_path):
    limiter, raw = seeded(tmp_path)
    dry = reboot_recovery(limiter, read_boot=reboot)
    assert dry["status"] == "REBOOT_RECOVERY_ELIGIBLE"
    assert limiter._state_path.read_bytes() == raw
    assert len(list(tmp_path.iterdir())) == 2

    applied = reboot_recovery(limiter, apply=True, read_boot=reboot)
    assert applied["status"] == "RECOVERED"
    results = {json.loads(p.read_bytes())["status"]: p for p in tmp_path.glob("*.reboot-*.json")}
    prepared = json.loads(results["PREPARED"].read_bytes())
    completed = json.loads(results["COMMITTED"].read_bytes())
    assert base64.b64decode(prepared["previous_state_base64"]) == raw
    assert prepared["previous_state_sha256"] == hashlib.sha256(raw).hexdigest()
    assert completed["state_sha256"] == hashlib.sha256(limiter._state_path.read_bytes()).hexdigest()
    assert limiter._state_path.with_suffix(".lock").read_bytes() == b"1"
    assert (
        reboot_recovery(limiter, apply=True, read_boot=reboot)["status"] == "RECOVERY_NOT_REQUIRED"
    )


def test_absent_or_inconsistent_boot_and_clock_or_state_fail_closed(tmp_path):
    limiter, raw = seeded(tmp_path)

    def absent():
        raise MassiveReferenceError("WINDOWS_BOOT_EVIDENCE_UNAVAILABLE")

    for witness, reason in (
        (absent, "WINDOWS_BOOT_EVIDENCE_UNAVAILABLE"),
        (lambda: reboot() | {"uptime_seconds": 42}, "WINDOWS_BOOT_EVIDENCE_UNCLEAR"),
        (
            lambda: reboot()
            | {
                "event_start": datetime.fromtimestamp(WALL - 25, UTC).isoformat(),
                "os_start": datetime.fromtimestamp(WALL - 20, UTC).isoformat(),
                "uptime_seconds": 20,
            },
            "RECOVERY_EXPIRY_WINDOW_NOT_PROVEN",
        ),
    ):
        outcome = reboot_recovery(limiter, apply=True, read_boot=witness)
        assert outcome["reason"] == reason
    assert limiter._state_path.read_bytes() == raw

    rolled_back, _ = seeded(tmp_path, wall=WALL - 800)
    assert reboot_recovery(rolled_back, apply=True, read_boot=absent)["reason"] == (
        "RECOVERY_WALL_CLOCK_ROLLBACK"
    )
    limiter._state_path.write_bytes(b"{")
    assert reboot_recovery(limiter, apply=True, read_boot=reboot)["reason"] == (
        "MASSIVE_RATE_LIMIT_STATE_INVALID"
    )
    limiter._state_path.with_suffix(".lock").write_bytes(b"0")
    assert reboot_recovery(limiter, apply=True, read_boot=reboot)["reason"] == (
        "RECOVERY_LOCK_MARKER_INVALID"
    )
    assert len(list(tmp_path.iterdir())) == 2


def test_in_flight_state_cannot_be_recovered(tmp_path):
    limiter, raw = seeded(tmp_path, in_flight=True)
    assert reboot_recovery(limiter, apply=True, read_boot=reboot)["reason"] == "RECOVERY_IN_FLIGHT"
    assert limiter._state_path.read_bytes() == raw
    assert len(list(tmp_path.iterdir())) == 2


def test_competing_recovery_cannot_acquire_the_shared_os_lock(tmp_path):
    first, _ = seeded(tmp_path)
    second = _RollingWindowLimiter(
        state_path=first._state_path, clock=lambda: 120, wall_clock=lambda: WALL
    )
    acquired = threading.Event()
    release = threading.Event()
    results = []

    def wait_for_competitor():
        acquired.set()
        if not release.wait(5):
            raise AssertionError("Competing attempt did not finish")
        return reboot()

    def recover():
        results.append(reboot_recovery(first, apply=True, read_boot=wait_for_competitor))

    thread = threading.Thread(target=recover)
    thread.start()
    try:
        assert acquired.wait(5)
        assert (
            reboot_recovery(second, apply=True, read_boot=reboot)["reason"] == "RECOVERY_LOCK_BUSY"
        )
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert results[0]["status"] == "RECOVERED"
    assert len(list(tmp_path.glob("*.reboot-*.json"))) == 2
