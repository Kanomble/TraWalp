"""Durable local Massive request budget; no credentials, responses or URLs."""

import errno
import json
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class MassiveReferenceError(ValueError):
    """Fixed status only, without provider bodies, URLs, headers or credentials."""


def _validate_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 5:
        raise MassiveReferenceError("MASSIVE_RATE_LIMIT_EXCEEDS_FREE_TIER")


def _state_path():
    # Fixed across checkouts/invocations; no API-key-derived names or CLI/env override.
    root = Path.home() / ("AppData/Local" if os.name == "nt" else ".local/state")
    return root / "TraWalp" / "state" / "massive-reference-rate-limit.json"


def _lock_operation(handle, *, acquire, platform=None):
    handle.seek(0)
    if (platform or os.name) == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


@contextmanager
def _exclusive_file_lock(path, sleeper):
    """Lock a stable file inode/byte through waiting, state writes and transport.

    Closing the handle releases the OS lock even on exceptions or termination.
    The single byte marks initialization, so missing state after initialization
    cannot silently restore five fresh slots. The lock file is never replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        while True:
            try:
                _lock_operation(handle, acquire=True)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                sleeper(0.1)
        try:
            if not handle.read(1):
                handle.seek(0)
                handle.write(b"0")
                os.fsync(handle.fileno())
            yield handle
        finally:
            _lock_operation(handle, acquire=False)


class _RollingWindowLimiter:
    """Share <=5 durable reservations across all local sync invocations.

    Python >=3.11 monotonic readings are shared across processes on a boot.
    Paired wall/monotonic observations guard persisted history against clock
    adjustments and counter resets. Both ages must reach 60.5 seconds before
    expiry. All injectable dependencies are private test seams; production has
    one fixed location and no bypass or alternate process-local implementation.
    """

    _expiry_seconds = 60.5

    def __init__(
        self,
        *,
        clock=time.monotonic,
        wall_clock=time.time,
        sleeper=time.sleep,
        state_path=None,
        lock_factory=_exclusive_file_lock,
    ):
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._state_path = Path(state_path) if state_path is not None else None
        self._lock_factory = lock_factory
        self._lock = threading.Lock()

    @staticmethod
    def _valid_pair(pair):
        return (
            isinstance(pair, list)
            and len(pair) == 2
            and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in pair)
        )

    def _now(self, previous=None):
        now = [self._wall_clock(), self._clock()]
        if not self._valid_pair(now) or (
            previous is not None
            and (
                now[0] < previous[0]
                or now[1] < previous[1]
                or abs((now[0] - previous[0]) - (now[1] - previous[1])) > 0.5
            )
        ):
            raise MassiveReferenceError("MASSIVE_RATE_LIMIT_CLOCK_ANOMALY")
        return now

    def _load(self, path, handle):
        handle.seek(0)
        marker = handle.read(1)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if marker != b"0":
                raise MassiveReferenceError("MASSIVE_RATE_LIMIT_STATE_UNAVAILABLE") from None
            return {"timestamps": [], "last_clock": self._now(), "in_flight": False}
        except (ValueError, UnicodeError):
            raise MassiveReferenceError("MASSIVE_RATE_LIMIT_STATE_INVALID") from None
        if (
            not isinstance(state, dict)
            or set(state) != {"timestamps", "last_clock", "in_flight"}
            or not isinstance(state["timestamps"], list)
            or len(state["timestamps"]) > 5
            or not self._valid_pair(state["last_clock"])
            or type(state["in_flight"]) is not bool
            or (state["in_flight"] and not state["timestamps"])
            or any(not self._valid_pair(t) for t in state["timestamps"])
        ):
            raise MassiveReferenceError("MASSIVE_RATE_LIMIT_STATE_INVALID")
        pairs = [*state["timestamps"], state["last_clock"]]
        if any(a[0] > b[0] or a[1] > b[1] for a, b in zip(pairs, pairs[1:], strict=False)):
            raise MassiveReferenceError("MASSIVE_RATE_LIMIT_STATE_INVALID")
        return state

    @staticmethod
    def _save(path, state, handle):
        # Atomic replacement protects the last complete state from interruption.
        # The separate, stable lock file remains held throughout replacement.
        descriptor, temporary = tempfile.mkstemp(
            prefix=path.stem + ".", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(state, output, separators=(",", ":"), allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            handle.seek(0)
            handle.write(b"1")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            Path(temporary).unlink(missing_ok=True)

    def transmit(self, operation, *, limit=5, not_before=0):
        _validate_limit(limit)
        path = self._state_path if self._state_path is not None else _state_path()
        transport_error = None
        try:
            with self._lock, self._lock_factory(path.with_suffix(".lock"), self._sleeper) as handle:
                state = self._load(path, handle)
                now = self._now(state["last_clock"])
                if state["in_flight"]:
                    # Acquiring the OS lock proves the old process no longer owns
                    # transport. Its potentially transmitted slot lasts from NOW.
                    state["timestamps"][-1] = now
                    state.update(last_clock=now, in_flight=False)
                    self._save(path, state, handle)
                previous = now
                while True:
                    now = self._now(previous)
                    previous = now
                    state["timestamps"] = [
                        t
                        for t in state["timestamps"]
                        if now[0] - t[0] < self._expiry_seconds
                        or now[1] - t[1] < self._expiry_seconds
                    ]
                    delay = max(0, not_before - now[1])
                    if len(state["timestamps"]) >= limit:
                        oldest = state["timestamps"][0]
                        delay = max(
                            delay,
                            oldest[0] + self._expiry_seconds - now[0],
                            oldest[1] + self._expiry_seconds - now[1],
                        )
                    if delay <= 0:
                        break
                    self._sleeper(delay)
                now = self._now(now)
                state["timestamps"].append(now)
                state.update(last_clock=now, in_flight=True)
                self._save(path, state, handle)  # Durable reservation BEFORE transport.
                try:
                    return operation()
                except BaseException as exc:
                    transport_error = exc
                    raise
                finally:
                    # If this write/clock check fails, the durable in-flight
                    # reservation survives and the next owner recovers it safely.
                    completed = self._now(now)
                    state["timestamps"][-1] = completed
                    state.update(last_clock=completed, in_flight=False)
                    self._save(path, state, handle)
        except OSError as exc:
            if exc is transport_error:
                raise  # Requests' transient network errors remain eligible for bounded retry.
            # No path, original exception or credential-bearing context escapes.
            raise MassiveReferenceError("MASSIVE_RATE_LIMIT_STATE_UNAVAILABLE") from None
