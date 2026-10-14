"""Simulated independent processes, shared files/locks, fake time; no workers/network."""

import io
import json
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_massive_reference import (
    DUMMY_KEY,
    FakeClock,
    assert_rolling_cap,
    response,
)
from test_massive_reference import offline as offline

from trading_system.data import massive_rate_limit as rate_limit
from trading_system.data import massive_reference as massive


class SimulatedBusy(Exception):
    pass


class SimulatedOSLock:
    """Cooperative scheduler seam: an overlapping actor must defer its operation."""

    def __init__(self):
        self.held = False
        self.acquisitions = 0
        self.releases = 0

    @contextmanager
    def __call__(self, path, sleeper):
        if self.held:
            raise SimulatedBusy
        self.held = True
        self.acquisitions += 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "r+b", buffering=0) as handle:
                if not handle.read(1):
                    handle.write(b"0")
                yield handle
        finally:
            self.releases += 1
            self.held = False


@pytest.fixture
def shared(tmp_path):
    clock = FakeClock()
    lock = SimulatedOSLock()
    path = tmp_path / "shared" / "massive-reference-rate-limit.json"

    def process():
        # No shared instance, thread lock or in-memory request history.
        return rate_limit._RollingWindowLimiter(
            clock=clock.monotonic,
            wall_clock=clock.wall_clock,
            sleeper=clock.sleep,
            state_path=path,
            lock_factory=lock,
        )

    return SimpleNamespace(clock=clock, lock=lock, path=path, process=process, times=[])


def send(shared, process, count=1):
    for _ in range(count):
        process.transmit(lambda: shared.times.append(shared.clock.now))


def state(shared):
    return json.loads(shared.path.read_text())


def test_a_five_allowed_and_sixth_waits(shared):
    process = shared.process()
    send(shared, process, 5)
    assert shared.times == [0] * 5 and shared.clock.sleeps == []
    send(shared, process)
    assert shared.times[-1] == 60.5
    assert_rolling_cap(shared.times)


def test_b_new_process_cannot_get_a_fresh_budget(shared):
    send(shared, shared.process(), 5)
    before = state(shared)
    process_b = shared.process()
    send(shared, process_b)
    assert len(before["timestamps"]) == 5
    assert shared.clock.sleeps == [60.5] and shared.times[-1] == 60.5
    assert_rolling_cap(shared.times)


def test_c_three_plus_two_share_one_five_request_budget(shared):
    a, b = shared.process(), shared.process()
    send(shared, a, 3)
    send(shared, b, 2)
    assert shared.times == [0] * 5
    send(shared, a)
    assert shared.clock.sleeps == [60.5]
    assert_rolling_cap(shared.times)


def test_d_process_exit_and_one_second_restart_retains_history(shared):
    a = shared.process()
    send(shared, a, 5)
    del a
    shared.clock.now = 1
    send(shared, shared.process())
    assert shared.clock.sleeps == [59.5] and shared.times[-1] == 60.5
    assert_rolling_cap(shared.times)


def test_e_only_safely_expired_history_is_cleaned(shared):
    a = shared.process()
    for _ in range(5):
        send(shared, a)
        shared.clock.now += 10
    shared.clock.now = 60.5
    send(shared, shared.process())
    timestamps = state(shared)["timestamps"]
    assert [t[1] for t in timestamps] == [10, 20, 30, 40, 60.5]
    assert len(timestamps) == 5 and shared.clock.sleeps == []
    assert_rolling_cap(shared.times)


def test_f_retry_persists_another_slot(shared, monkeypatch):
    pending = iter([response(503), response()])
    client = massive.MassiveReferenceClient(DUMMY_KEY)

    def get(*args, **kwargs):
        shared.times.append(shared.clock.now)
        return next(pending)

    monkeypatch.setattr(client._session, "get", get)
    monkeypatch.setattr(massive, "_REQUEST_LIMITER", shared.process())
    try:
        client.get_ticker_types()
        assert client.diagnostics["massive_retries"] == 1
        assert len(state(shared)["timestamps"]) == 2
        send(shared, shared.process(), 4)
        assert shared.times[-1] == 60.5
        assert_rolling_cap(shared.times)
    finally:
        client.close()


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "MASSIVE_AUTH_FAILED"),
        (402, "MASSIVE_PAYMENT_OR_PLAN_REQUIRED"),
        (403, "MASSIVE_REFERENCE_ACCESS_DENIED"),
    ],
)
def test_g_access_failure_consumes_slot_without_retry(shared, monkeypatch, status, expected):
    client = massive.MassiveReferenceClient(DUMMY_KEY)

    def get(*args, **kwargs):
        shared.times.append(shared.clock.now)
        return response(status)

    monkeypatch.setattr(client._session, "get", get)
    monkeypatch.setattr(massive, "_REQUEST_LIMITER", shared.process())
    try:
        with pytest.raises(massive.MassiveReferenceError, match=expected):
            client.get_ticker_types()
        assert len(state(shared)["timestamps"]) == 1
        assert client.diagnostics["massive_retries"] == 0
        send(shared, shared.process(), 5)
        assert shared.times[-1] == 60.5
        assert_rolling_cap(shared.times)
    finally:
        client.close()


def test_h_keyboard_interrupt_releases_lock_and_retains_transmitted_slot(shared):
    def interrupted():
        shared.times.append(shared.clock.now)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        shared.process().transmit(interrupted)
    assert not shared.lock.held and state(shared)["in_flight"] is False
    assert len(state(shared)["timestamps"]) == 1
    send(shared, shared.process(), 5)
    assert_rolling_cap(shared.times)


def test_h_killed_owner_pending_reservation_is_recovered_conservatively(shared, monkeypatch):
    a = shared.process()
    original_save = a._save
    saves = []

    def save_or_simulate_termination(*args):
        saves.append(True)
        if len(saves) == 2:
            raise KeyboardInterrupt  # Completion could not be committed before termination.
        original_save(*args)

    def delayed_transport():
        shared.clock.now = 50
        shared.times.append(50)

    monkeypatch.setattr(a, "_save", save_or_simulate_termination)
    with pytest.raises(KeyboardInterrupt):
        a.transmit(delayed_transport)
    assert state(shared)["in_flight"] is True
    assert not shared.lock.held
    shared.clock.now = 51
    send(shared, shared.process(), 5)
    assert shared.times == [50, 51, 51, 51, 51, 111.5]
    assert state(shared)["in_flight"] is False
    assert_rolling_cap(shared.times)


def test_h_interrupted_atomic_reservation_write_leaves_previous_state(shared, monkeypatch):
    send(shared, shared.process(), 3)
    before = shared.path.read_bytes()
    transmitted = []

    def interrupted_replace(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(rate_limit.os, "replace", interrupted_replace)
        with pytest.raises(KeyboardInterrupt):
            shared.process().transmit(lambda: transmitted.append(True))
    assert shared.path.read_bytes() == before and transmitted == []
    assert not shared.lock.held and list(shared.path.parent.glob("*.tmp")) == []
    send(shared, shared.process(), 3)
    assert_rolling_cap(shared.times)


@pytest.mark.parametrize("anomaly", ["rollback", "forward_jump", "monotonic_reset", "future_state"])
def test_i_clock_anomalies_do_not_discard_history_or_transmit(shared, anomaly):
    shared.clock.now = 10
    send(shared, shared.process(), 5)
    b = shared.process()
    if anomaly == "rollback":
        b._wall_clock = lambda: shared.clock.wall_clock() - 2
    elif anomaly == "forward_jump":
        b._wall_clock = lambda: shared.clock.wall_clock() + 3600
    elif anomaly == "monotonic_reset":
        b._clock = lambda: 0
    else:
        data = state(shared)
        data["timestamps"] = [[t[0] + 120, t[1] + 120] for t in data["timestamps"]]
        data["last_clock"] = data["timestamps"][-1]
        shared.path.write_text(json.dumps(data))
    before = shared.path.read_bytes()
    with pytest.raises(massive.MassiveReferenceError, match="MASSIVE_RATE_LIMIT_CLOCK_ANOMALY"):
        send(shared, b)
    assert shared.path.read_bytes() == before and shared.times == [10] * 5
    assert not shared.lock.held


def test_i_small_forward_adjustment_cannot_shorten_monotonic_protection(shared):
    send(shared, shared.process(), 5)
    b = shared.process()
    b._wall_clock = lambda: shared.clock.wall_clock() + 0.4
    send(shared, b)
    assert shared.times[-1] >= 60.5
    assert_rolling_cap(shared.times)


def test_j_simultaneous_actors_are_serialized_before_loading_and_reserving(shared):
    a, b = shared.process(), shared.process()
    for _ in range(6):

        def a_transport():
            shared.times.append(shared.clock.now)
            with pytest.raises(SimulatedBusy):
                # B cannot load/overwrite history or transmit while A owns the lock.
                send(shared, b)

        a.transmit(a_transport)
        send(shared, b)
    assert len(shared.times) == 12
    assert shared.lock.acquisitions == shared.lock.releases == 12
    assert_rolling_cap(shared.times)


@pytest.mark.parametrize("failure", ["deleted", "truncated", "invalid_schema"])
def test_state_damage_fails_closed_instead_of_resetting_budget(shared, failure):
    send(shared, shared.process(), 5)
    if failure == "deleted":
        shared.path.unlink()
    else:
        shared.path.write_text("{" if failure == "truncated" else "{}")
    with pytest.raises(massive.MassiveReferenceError, match="MASSIVE_RATE_LIMIT_STATE_"):
        send(shared, shared.process())
    assert shared.times == [0] * 5 and not shared.lock.held


def test_state_is_timing_only_bounded_and_does_not_use_credentials(shared):
    send(shared, shared.process(), 17)
    data = state(shared)
    assert set(data) == {"timestamps", "last_clock", "in_flight"}
    assert len(data["timestamps"]) <= 5
    assert DUMMY_KEY not in shared.path.read_text()
    assert "http" not in shared.path.read_text()
    assert_rolling_cap(shared.times)


def test_windows_lock_primitive_uses_same_byte_for_acquire_and_release(monkeypatch):
    calls = []
    fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda *args: calls.append(args))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    handle = io.BytesIO(b"0")
    monkeypatch.setattr(handle, "fileno", lambda: 77)
    handle.seek(1)
    rate_limit._lock_operation(handle, acquire=True, platform="nt")
    assert handle.tell() == 0
    rate_limit._lock_operation(handle, acquire=False, platform="nt")
    assert calls == [(77, 2, 1), (77, 0, 1)]


def test_posix_lock_primitive_is_exclusive_and_unlocks(monkeypatch):
    calls = []
    fake = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *args: calls.append(args))
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    handle = io.BytesIO(b"0")
    monkeypatch.setattr(handle, "fileno", lambda: 77)
    rate_limit._lock_operation(handle, acquire=True, platform="posix")
    rate_limit._lock_operation(handle, acquire=False, platform="posix")
    assert calls == [(77, 6), (77, 8)]


def test_os_lock_releases_on_exception_and_can_be_reacquired(tmp_path):
    path = tmp_path / "limiter.lock"
    clock = FakeClock()
    with pytest.raises(KeyboardInterrupt), rate_limit._exclusive_file_lock(path, clock.sleep):
        raise KeyboardInterrupt
    with rate_limit._exclusive_file_lock(path, clock.sleep) as handle:
        assert handle.read(1) in (b"", b"0")
    assert clock.sleeps == []


def test_real_os_lock_rejects_an_overlapping_independent_handle_without_waiting(tmp_path):
    path = tmp_path / "limiter.lock"
    clock = FakeClock()

    def defer_actor(seconds):
        assert seconds == 0.1
        raise SimulatedBusy  # No real contention wait, thread or child process.

    with (
        rate_limit._exclusive_file_lock(path, clock.sleep),
        pytest.raises(SimulatedBusy),
        rate_limit._exclusive_file_lock(path, defer_actor),
    ):
        pytest.fail("Independent handle entered an already-held OS lock")
    with rate_limit._exclusive_file_lock(path, clock.sleep):
        pass
    assert clock.sleeps == []


def test_fixed_default_location_is_independent_of_api_key(monkeypatch):
    first = rate_limit._state_path()
    monkeypatch.setenv("MASSIVE_API_KEY", "another-synthetic-key")
    assert rate_limit._state_path() == first
    assert first.name == "massive-reference-rate-limit.json"
    assert first.parent.parts[-2:] == ("TraWalp", "state")
