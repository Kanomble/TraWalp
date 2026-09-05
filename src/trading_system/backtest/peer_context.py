"""Causal technical peer observations using the existing local SIC universe.

Price windows are PIT; SIC/tradable membership is the repository's current snapshot,
not a historical industry-membership database. Forward labels live only in reporting.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from functools import lru_cache
from statistics import mean, median, pstdev

import pandas as pd

from trading_system.backtest.features import _fast_technical_snapshot
from trading_system.backtest.lifecycle import PeerTrendState, TrendHealthState
from trading_system.config import StrategyConfig
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.fundamentals.peers import assign_peer_groups, normalize_sic

PEER_MEMBERSHIP_BASIS = "CURRENT_LOCAL_SIC_AND_TRADABLE_UNIVERSE_NOT_HISTORICALLY_VERSIONED"


def peer_confirmation(count: int, above_ratio: float | None) -> PeerTrendState:
    if count < 3 or above_ratio is None:
        return PeerTrendState.UNAVAILABLE
    return PeerTrendState.CONFIRMED if above_ratio > 0.5 else PeerTrendState.WEAK


@dataclass(frozen=True, slots=True)
class PeerContext:
    symbol: str
    session: date
    peer_count_valid: int = 0
    peer_above_sma20_ratio: float | None = None
    peer_positive_1d_ratio: float | None = None
    peer_positive_5d_ratio: float | None = None
    peer_median_1d_return: float | None = None
    peer_median_5d_return: float | None = None
    peer_best_1d_return: float | None = None
    peer_worst_1d_return: float | None = None
    stock_5d_return: float | None = None
    relative_strength_vs_peers_5d: float | None = None
    largest_peer_5d_return: float | None = None
    peer_dispersion: float | None = None
    peer_group: str | None = None
    peer_symbols: tuple[str, ...] = ()
    membership_basis: str = PEER_MEMBERSHIP_BASIS

    @property
    def state(self) -> PeerTrendState:
        return peer_confirmation(self.peer_count_valid, self.peer_above_sma20_ratio)

    def row(self) -> dict:
        return {
            **asdict(self),
            "session": self.session.isoformat(),
            "peer_state": self.state.value,
            "best_peer_1d_return": self.peer_best_1d_return,
            "largest_peer_1d_return": self.peer_best_1d_return,
            "median_peer_1d_return": self.peer_median_1d_return,
            "median_peer_5d_return": self.peer_median_5d_return,
        }


class TechnicalPeerContextProvider:
    """Run-local cache; future loaded bars are sliced out before any decision calculation."""

    def __init__(self, database: Database, config: StrategyConfig, end: date):
        self.database = database
        self.config = config
        self.end = end
        conflicts = database.unresolved_sec_identity_conflict_symbols()
        self.companies = tuple(
            c
            for c in database.list_tradable_companies()
            if c.symbol not in conflicts and c.symbol != "SPY"
        )
        self._histories: dict = {}
        self._dates: dict = {}
        # Instance-owned caches are released with the runner, not retained globally.
        self.technical = lru_cache(maxsize=100_000)(self._technical)
        self.context = lru_cache(maxsize=100_000)(self._context)
        self._groups = lru_cache(maxsize=8)(self._session_groups)

    def history(self, symbol: str, session: date, count: int | None = None):
        if symbol not in self._histories:
            bars = self.database.bars_available_as_of(symbol, self.end)
            self._histories[symbol] = bars
            self._dates[symbol] = [bar.timestamp.date() for bar in bars]
        index = bisect_right(self._dates[symbol], min(session, self.end))
        return self._histories[symbol][max(0, index - count) if count else 0 : index]

    def complete_history(self, symbol: str, session: date, count: int):
        bars = self.history(symbol, session, count)
        if len(bars) != count or bars[-1].timestamp.date() != session:
            return []
        expected = trading_sessions_between(bars[0].timestamp.date(), session)
        return bars if [b.timestamp.date() for b in bars] == expected else []

    def _technical(self, symbol: str, session: date):
        count = max(25, 20 + self.config.technical.sma_slope_lookback)
        bars = self.complete_history(symbol, session, count)
        if not bars:
            # Peer confirmation needs SMA20/1d/5d only. Own trend remains UNAVAILABLE
            # when the longer, complete slope window is absent.
            bars = self.complete_history(symbol, session, 20)
            if not bars:
                return None
        return _fast_technical_snapshot(bars, self.config)

    def trend(self, symbol: str, session: date) -> TrendHealthState:
        technical = self.technical(symbol, session)
        if (
            technical is None
            or technical.price is None
            or technical.sma20 is None
            or technical.sma20_rising is None
        ):
            return TrendHealthState.UNAVAILABLE
        return (
            TrendHealthState.HEALTHY
            if technical.price > technical.sma20 and technical.sma20_rising
            else TrendHealthState.WEAKENING
        )

    def _session_groups(self, session: date):
        # Exclude not-yet-listed / unavailable members before selecting SIC width. The stock
        # is included in group-size selection but always excluded from the peer statistics.
        available = [c for c in self.companies if self.technical(c.symbol, session) is not None]
        if not available:
            return {}, {}
        frame = assign_peer_groups(
            pd.DataFrame([{"symbol": c.symbol, "sic": c.sic} for c in available]), min_peer_count=4
        )
        groups, members = {}, {}
        for row in frame.itertuples():
            sic = normalize_sic(row.sic)
            group = row.peer_group or (f"sic2:{sic[:2]}" if sic else None)
            groups[row.symbol] = group
            if sic:
                # A broad basket includes members whose own selected group is narrower.
                for width in (4, 3, 2):
                    members.setdefault(f"sic{width}:{sic[:width]}", []).append(row.symbol)
        return groups, members

    def _context(self, symbol: str, session: date) -> PeerContext:
        own = self.technical(symbol, session)
        stock_return = own.momentum5 if own is not None else None
        groups, members = self._groups(session)
        group = groups.get(symbol)
        peers = [peer for peer in members.get(group, ()) if peer != symbol]
        if not peers:
            return PeerContext(symbol, session, stock_5d_return=stock_return, peer_group=group)
        one_day, five_day, above = [], [], []
        for peer in peers:
            bars = self.history(peer, session, 2)
            tech = self.technical(peer, session)
            one_day.append(float(bars[-1].close / bars[-2].close) - 1)
            five_day.append(tech.momentum5)
            above.append(tech.price > tech.sma20)
        median_5 = median(five_day)
        return PeerContext(
            symbol,
            session,
            len(peers),
            mean(above),
            mean(r > 0 for r in one_day),
            mean(r > 0 for r in five_day),
            median(one_day),
            median_5,
            max(one_day),
            min(one_day),
            stock_return,
            stock_return - median_5 if stock_return is not None else None,
            max(five_day),
            pstdev(one_day),
            group,
            tuple(sorted(peers)),
        )

    def peer_state(self, symbol: str, session: date) -> PeerTrendState:
        return self.context(symbol, session).state

    def correlations(self, symbol: str, open_symbols, session: date) -> dict:
        correlations = []
        own = self.complete_history(symbol, session, 61)
        if own:
            series = pd.Series([float(bar.close) for bar in own]).pct_change().iloc[1:]
            for peer in open_symbols:
                if peer == symbol:
                    continue
                bars = self.complete_history(peer, session, 61)
                if bars:
                    other = pd.Series([float(bar.close) for bar in bars]).pct_change().iloc[1:]
                    value = series.corr(other) if series.std() > 0 and other.std() > 0 else None
                    if value is not None and pd.notna(value):
                        correlations.append(float(value))
        return {
            "correlation_window_sessions": 60,
            "correlation_pairs_valid": len(correlations),
            "mean_correlation_to_open_positions": mean(correlations) if correlations else None,
            "max_correlation_to_open_positions": max(correlations) if correlations else None,
        }

    def forward_bars(self, symbol: str, session: date, count: int):
        """Reporting only: exact next XNYS sessions, censored at the run end."""
        end = min(self.end, session + timedelta(days=max(30, count * 3)))
        expected = trading_sessions_between(session, end)[1 : count + 1]
        if len(expected) < count:
            return []
        bars = self.history(symbol, expected[-1], count)
        return bars if [b.timestamp.date() for b in bars] == expected else []
