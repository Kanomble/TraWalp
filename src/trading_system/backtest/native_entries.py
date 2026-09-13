"""Run-owned native entry sessions, populated by exact coverage batches, never persisted."""

import pickle
import tempfile
from collections import OrderedDict

from trading_system.models.market_data import BarTimeframe


class NativeEntrySessions:
    """Bounded RAM over candidate/session-only data in an owned temporary spool.

    Pickle is strictly internal: only objects written by this instance are read, never
    user manifests/files. Intraday prices are deliberately absent from the Daily manifest.
    A fresh validation can therefore use newly synchronized native bars.
    """

    def __init__(self, cache_limit=8):
        if cache_limit < 1:
            raise ValueError("cache_limit must be positive")
        self.file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 -- context-owned
        self.offsets = {}
        self.cache = OrderedDict()
        self.cache_limit = cache_limit
        self.peak_cache_size = self.reads = 0

    def add(
        self, symbol, signal, execution, bars, previous_close, *, timeframe=BarTimeframe.MINUTES_15
    ):
        key = symbol, signal, execution, timeframe
        self.file.seek(0, 2)
        self.offsets[key] = self.file.tell()
        pickle.dump((bars, previous_close), self.file, protocol=pickle.HIGHEST_PROTOCOL)

    def entry_session(self, symbol, signal, execution, *, timeframe=BarTimeframe.MINUTES_15):
        key = symbol, signal, execution, timeframe
        if key not in self.cache:
            if key not in self.offsets:
                raise ValueError("Native entry requirement was not coverage-verified")
            self.file.seek(self.offsets[key])
            self.cache[key] = pickle.load(self.file)  # trusted, instance-owned temporary file
            self.reads += 1
            if len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
            self.peak_cache_size = max(self.peak_cache_size, len(self.cache))
        self.cache.move_to_end(key)
        return self.cache[key]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.file.close()
        self.cache.clear()
