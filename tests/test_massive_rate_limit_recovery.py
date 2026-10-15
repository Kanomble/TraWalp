"""Manual recovery with shared locks and synthetic clocks; never real waiting or HTTP."""

import json

import pytest
from test_massive_rate_limit import SimulatedBusy, send, state
from test_massive_rate_limit import shared as shared
from test_massive_reference import offline as offline

from trading_system import cli
from trading_system.data import massive_reference as massive


def anomalous_process(shared, *, in_flight=False):
    send(shared, shared.process(), 5)
    if in_flight:
        data = state(shared)
        data["in_flight"] = True
        shared.path.write_text(json.dumps(data))
    shared.clock.now = 60
    actor = shared.process()
    actor._wall_clock = lambda: shared.clock.wall_clock() + 10
    return actor


def test_normal_transmit_rejects_seventy_wall_vs_sixty_monotonic(shared):
    actor = anomalous_process(shared)
    original = shared.path.read_bytes()
    with pytest.raises(massive.MassiveReferenceError, match="CLOCK_ANOMALY"):
        send(shared, actor)
    assert shared.path.read_bytes() == original and shared.times == [0] * 5


@pytest.mark.parametrize("in_flight", [False, True])
def test_safe_recovery_expires_full_window_then_normal_requests_remain_limited(shared, in_flight):
    actor = anomalous_process(shared, in_flight=in_flight)
    result = actor.recover_clock_anomaly()
    assert result == {
        "status": "RECOVERED",
        "reason": "CLOCK_ANOMALY",
        "waited_seconds": 60.5,
        "previous_slots": 5,
        "previous_in_flight": in_flight,
        "state_reset": True,
    }
    assert sum(shared.clock.sleeps) == 60.5
    assert state(shared) == {
        "timestamps": [],
        "last_clock": [actor._wall_clock(), actor._clock()],
        "in_flight": False,
    }
    send(shared, actor, 6)
    assert shared.times[5:] == [120.5] * 5 + [181.0]


def test_old_five_slots_survive_while_same_os_lock_blocks_an_independent_actor(shared):
    actor = anomalous_process(shared)
    competitor = shared.process()
    competitor._wall_clock = actor._wall_clock
    original = shared.path.read_bytes()
    checks = []

    def inspect_wait(seconds):
        assert shared.lock.held
        assert shared.path.read_bytes() == original
        with pytest.raises(SimulatedBusy):
            send(shared, competitor)
        checks.append(shared.clock.now)
        shared.clock.sleep(seconds)

    actor._sleeper = inspect_wait
    actor.recover_clock_anomaly()
    assert checks == [60, 90, 120]
    assert shared.times == [0] * 5
    assert not shared.lock.held
    send(shared, competitor)
    assert shared.times[-1] == 120.5


@pytest.mark.parametrize("in_flight", [False, True])
def test_consistent_state_requires_no_recovery_or_mutation(shared, in_flight):
    send(shared, shared.process(), 3)
    if in_flight:
        data = state(shared)
        data["in_flight"] = True
        shared.path.write_text(json.dumps(data))
    original = shared.path.read_bytes()
    assert shared.process().recover_clock_anomaly()["status"] == "RECOVERY_NOT_REQUIRED"
    assert shared.path.read_bytes() == original and shared.clock.sleeps == []


@pytest.mark.parametrize("damage", ["json", "schema", "missing", "negative", "nan", "future"])
def test_damaged_or_future_state_is_never_repaired(shared, damage):
    actor = anomalous_process(shared)
    if damage == "missing":
        shared.path.unlink()
    elif damage in {"json", "schema"}:
        shared.path.write_text("{" if damage == "json" else "{}")
    else:
        data = state(shared)
        value = {"negative": -1, "nan": float("nan"), "future": actor._wall_clock() + 120}[damage]
        data["last_clock"][0] = value
        shared.path.write_text(json.dumps(data))
    original = shared.path.read_bytes() if shared.path.exists() else None
    with pytest.raises(massive.MassiveReferenceError, match="STATE_(INVALID|UNAVAILABLE)"):
        actor.recover_clock_anomaly()
    assert (shared.path.read_bytes() if shared.path.exists() else None) == original
    assert shared.clock.sleeps == [] and not shared.lock.held


def test_interrupted_wait_preserves_old_state_and_releases_lock(shared):
    actor = anomalous_process(shared, in_flight=True)
    original = shared.path.read_bytes()

    def interrupted(seconds):
        shared.clock.sleep(seconds)
        raise KeyboardInterrupt

    actor._sleeper = interrupted
    with pytest.raises(KeyboardInterrupt):
        actor.recover_clock_anomaly()
    assert shared.path.read_bytes() == original and not shared.lock.held
    with pytest.raises(massive.MassiveReferenceError, match="CLOCK_ANOMALY"):
        send(shared, actor)
    actor._sleeper = shared.clock.sleep
    assert actor.recover_clock_anomaly()["waited_seconds"] == 60.5


def test_new_clock_correction_during_recovery_refuses_reset(shared):
    actor = anomalous_process(shared)
    original = shared.path.read_bytes()

    def unstable(seconds):
        shared.clock.sleep(seconds)
        actor._wall_clock = lambda: shared.clock.wall_clock() + 20

    actor._sleeper = unstable
    with pytest.raises(massive.MassiveReferenceError, match="CLOCK_ANOMALY"):
        actor.recover_clock_anomaly()
    assert shared.path.read_bytes() == original and not shared.lock.held


def test_small_corrections_cannot_accumulate_into_an_unstable_recovery_window(shared):
    actor = anomalous_process(shared)
    original = shared.path.read_bytes()
    offset = [10.0]
    actor._wall_clock = lambda: shared.clock.wall_clock() + offset[0]

    def drifting(seconds):
        shared.clock.sleep(seconds)
        offset[0] += 0.3

    actor._sleeper = drifting
    with pytest.raises(massive.MassiveReferenceError, match="CLOCK_ANOMALY"):
        actor.recover_clock_anomaly()
    assert shared.path.read_bytes() == original and not shared.lock.held


def test_early_wakeup_must_not_shorten_expiry(shared):
    actor = anomalous_process(shared)
    wakeups = []

    def early(seconds):
        actual = min(seconds, 10)
        wakeups.append(actual)
        shared.clock.sleep(actual)

    actor._sleeper = early
    assert actor.recover_clock_anomaly()["waited_seconds"] == 60.5
    assert sum(wakeups) == 60.5 and len(wakeups) == 7


def test_cli_needs_no_settings_credentials_database_or_transport(shared, monkeypatch, capsys):
    actor = anomalous_process(shared)
    monkeypatch.setattr(massive, "_REQUEST_LIMITER", actor)

    def forbidden(*args, **kwargs):
        pytest.fail("Recovery must not load settings, touch a database or create a provider client")

    monkeypatch.setattr(cli, "load_settings", forbidden)
    monkeypatch.setattr(cli, "Database", forbidden)
    monkeypatch.setattr(massive, "MassiveReferenceClient", forbidden)
    assert cli.main(["recover-massive-rate-limit"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "RECOVERED" and result["waited_seconds"] == 60.5


def test_cli_interrupt_is_structured_and_returns_130(shared, monkeypatch, capsys):
    actor = anomalous_process(shared)

    def interrupt(seconds):
        raise KeyboardInterrupt

    actor._sleeper = interrupt
    monkeypatch.setattr(massive, "_REQUEST_LIMITER", actor)
    original = shared.path.read_bytes()
    assert cli.main(["recover-massive-rate-limit"]) == 130
    assert json.loads(capsys.readouterr().out)["status"] == "RECOVERY_INTERRUPTED"
    assert shared.path.read_bytes() == original and not shared.lock.held
