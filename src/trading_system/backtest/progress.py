"""Observability for historical jobs; all elapsed measurements use a monotonic clock."""

from __future__ import annotations

import logging
import time
from threading import Event, RLock, Thread

LOGGER = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 30 * 60


class ProgressPhase:
    """Log boundaries and a time-based heartbeat, including during blocking work.

    Only scalar counters are copied by ``update``. The worker never touches research
    objects or the database. It wakes once per interval and is stopped on every exit,
    including KeyboardInterrupt; an interrupted phase never emits ``completed``.
    """

    def __init__(
        self, event, phase, *, message=None, percentage=None, interval=HEARTBEAT_SECONDS,
        **counters,
    ):
        if interval <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.event = event
        self.phase = phase
        self.message = message
        self.percentage = percentage
        self.interval = interval
        self.counters = counters
        self.seconds = 0.0
        self._lock = RLock()
        self._stop = Event()

    def __enter__(self):
        self.started = self.last_progress_log = time.monotonic()
        self._emit("starting", self.started)
        self._worker = Thread(target=self._watch, name="research-heartbeat", daemon=True)
        self._worker.start()
        return self

    def _watch(self):
        while True:
            with self._lock:
                remaining = max(0.0, self.interval - (time.monotonic() - self.last_progress_log))
            if self._stop.wait(remaining):
                return
            self.heartbeat()

    def update(self, **counters):
        with self._lock:
            self.counters.update(counters)
            self.heartbeat()

    def heartbeat(self):
        with self._lock:
            now = time.monotonic()
            if not self._stop.is_set() and now - self.last_progress_log >= self.interval:
                self._emit("running", now)
                self.last_progress_log = now

    def _emit(self, status, now):
        fields = dict(self.counters)
        if self.percentage:
            processed, total = (fields.get(key) for key in self.percentage)
            if processed is not None and total is not None and total > 0:
                fields["progress_pct"] = f"{min(100.0, max(0.0, 100 * processed / total)):.1f}"
        LOGGER.info(
            "%s phase=%s status=%s elapsed_seconds=%.3f%s%s",
            self.event, self.phase, status, now - self.started,
            " " + " ".join(f"{key}={value}" for key, value in fields.items()) if fields else "",
            f" message={self.message!r}" if self.message else "",
        )

    def __exit__(self, exc_type, exc_value, traceback):
        with self._lock:
            self._stop.set()
            now = time.monotonic()
            self.seconds = now - self.started
            if exc_type is None:
                self._emit("completed", now)
        self._worker.join()
        return False


class ResearchProgress:
    """Collect stage durations without putting mutable progress state in research results."""

    def __init__(self, *, validation=False):
        self.title = (
            "F intraday entry validation" if validation else "Intraday entry preflight"
        )
        self.event = "intraday_validation_progress" if validation else "intraday_preflight_progress"
        self.started = time.monotonic()
        self.performance = {}
        LOGGER.info("%s: starting", self.title)

    def phase(self, name, message=None, **kwargs):
        return ProgressPhase(self.event, name, message=message, **kwargs)

    def record(self, name, phase):
        self.performance[f"{name}_seconds"] = phase.seconds

    def snapshot(self):
        self.performance["total_seconds"] = time.monotonic() - self.started
        return dict(self.performance)


def log_completion(title, performance, **counters):
    counters["elapsed_seconds"] = f"{performance['total_seconds']:.6f}"
    LOGGER.info("%s: completed %s", title, " ".join(f"{k}={v}" for k, v in counters.items()))
    LOGGER.info(
        "%s timings: %s", title,
        " ".join(
            f"{key}={value:.6f}" if key.endswith("_seconds") else f"{key}={value}"
            for key, value in performance.items()
        ),
    )
