"""Generic local liquid-US-equity selection and reproducibility; no strategy signals."""

import hashlib
import json
import math
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from time import perf_counter

from trading_system.data.database import Database
from trading_system.data.market_sessions import daily_warmup_start, trading_sessions_between


@dataclass(frozen=True, slots=True)
class LiquidityCandidate:
    session: date
    symbol: str
    previous_session: date
    daily_universe_rank: int
    previous_close: float
    average_dollar_volume_20d: float

    @property
    def key(self):
        return self.symbol, self.previous_session, self.session


class ResearchReadOnlyDatabase(Database):
    """Force inherited asset helpers onto read-only connections and count actual reads."""

    def __init__(self, path):
        super().__init__(path)
        self.sql_queries = 0

    @contextmanager
    def read_only(self):
        with super().read_only() as connection:

            def trace(statement):
                if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                    self.sql_queries += 1

            connection.set_trace_callback(trace)
            yield connection

    connect = read_only


@dataclass
class LiquidPreparation:
    requested_start: date
    requested_end: date
    sessions: list[date]
    candidates: list[LiquidityCandidate]
    daily_qualification: dict
    coverage: list[dict] = field(default_factory=list)
    performance: dict = field(default_factory=dict)
    source_fingerprint: str | None = None
    candidate_source: str = "REDISCOVERED"
    candidate_manifest_path: str | None = None
    candidate_manifest_fingerprint: str | None = None

    @property
    def ready(self):
        admitted = {"REQUIRED_PRESENT", "PROVIDER_CONFIRMED_ABSENT"}
        return self.daily_qualification["ready"] and all(
            row["status"] in admitted for row in self.coverage
        )


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(payload):
    """Integrity checksum, not a signature; all fields except the checksum itself participate."""
    return hashlib.sha256(
        _canonical({k: v for k, v in payload.items() if k != "fingerprint"})
    ).hexdigest()


def liquid_universe_definition(config, definition):
    return {
        "name": definition.universe_name,
        "asset_universe": "CURRENT_ALPACA_TRADABLE_US_EQUITY",
        "security_scope": "ALPACA_TRADABLE_US_EQUITY",
        "individual_stocks_only": False,
        "etfs_etps_may_be_included": True,
        "direct_crypto_allowed": False,
        "unknown_asset_class_allowed": False,
        "execution_eligibility_germany": "NOT_EVALUATED",
        "membership": "CURRENT_UNIVERSE_ONLY",
        "survivorship": "NOT_SURVIVORSHIP_CLEAN",
        "top_n": definition.top_n,
        "previous_close_min": config.universe.min_price,
        "average_dollar_volume_20d_min": config.universe.min_avg_dollar_volume_20d,
        "ranking": "20 completed Daily sessions through T-1: mean(close * volume) DESC, symbol ASC",
        "sec_identity_required": False,
        "intraday_absence_replacement": False,
    }


def selection_basis(database, config, start, end, definition, *, error_prefix="RESEARCH"):
    sessions = trading_sessions_between(start, end)
    if not sessions:
        raise ValueError(f"{error_prefix}_NO_REQUESTED_TRADING_SESSIONS")
    first = daily_warmup_start(sessions[0], definition.daily_lookback_sessions + 1)
    history = trading_sessions_between(first, sessions[-1])
    # One current asset load per run, before any Daily ranking; no SEC or provider reads.
    assets = database.list_tradable_assets()
    classes = Counter(asset.asset_class for asset in assets)
    return {
        "universe_definition": liquid_universe_definition(config, definition),
        "sessions": [s.isoformat() for s in sessions],
        "history_sessions": [s.isoformat() for s in history],
        "tradable_assets": sorted(
            (asset.symbol, asset.asset_class, asset.tradable) for asset in assets
        ),
        "asset_scope_diagnostics": {
            "tradable_assets_total": len(assets),
            "us_equity_assets_considered": classes["US_EQUITY"],
            "crypto_assets_excluded": classes["CRYPTO"],
            "unknown_asset_class_excluded": classes["UNKNOWN"],
            "other_asset_classes_excluded": sum(
                count
                for key, count in classes.items()
                if key not in {"US_EQUITY", "CRYPTO", "UNKNOWN"}
            ),
            "tradable_assets_by_class": dict(sorted(classes.items())),
        },
        "symbols": sorted(
            asset.symbol for asset in assets if asset.tradable and asset.asset_class == "US_EQUITY"
        ),
    }


def selection_digest(basis):
    return hashlib.sha256(_canonical(basis) + b"\n")


def iter_selection_batches(database, basis, digest):
    """Hash the exact rows consumed by discovery, or stream them only to verify reuse.

    Include all eligible membership histories, even those below rank 100/thresholds:
    correcting any of them could change selection. Exclude T/end and intraday data.
    """
    history = basis["history_sessions"]
    for batch in database.iter_bar_value_batches(
        basis["symbols"],
        date.fromisoformat(history[0]),
        date.fromisoformat(history[-2]),
        batch_size=100,
    ):
        for row in batch:
            digest.update(_canonical(row) + b"\n")
        yield batch


def candidate_manifest(prepared, contract):
    candidates = [
        {
            **asdict(row),
            "session": row.session.isoformat(),
            "previous_session": row.previous_session.isoformat(),
        }
        for row in prepared.candidates
    ]
    payload = {
        **contract,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "daily_qualification": prepared.daily_qualification,
        "source_fingerprint": prepared.source_fingerprint,
    }
    payload["fingerprint"] = fingerprint(payload)
    return payload


def load_candidate_manifest(
    path, database, config, start, end, definition, contract, *, mismatch, allowed_statuses=()
):
    """Fail closed. Rehash source inputs, never rediscover/rerank or fetch native data."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint(payload):
            raise ValueError("fingerprint")
        for key, expected in contract.items():
            actual = payload.get(key)
            # Research disposition is not an economic input. Retain pre-rejection evidence.
            if (
                key == "strategy_definition"
                and isinstance(actual, dict)
                and actual.get("status") in allowed_statuses
            ):
                actual = {**actual, "status": expected["status"]}
            if _canonical(actual) != _canonical(expected):
                raise ValueError(key)
        basis = selection_basis(database, config, start, end, definition)
        digest = selection_digest(basis)
        for _ in iter_selection_batches(database, basis, digest):
            pass
        if payload.get("source_fingerprint") != digest.hexdigest():
            raise ValueError("local asset membership/class/Daily source fingerprint")
        sessions = [date.fromisoformat(s) for s in basis["sessions"]]
        session_set = set(sessions)
        history = [date.fromisoformat(s) for s in basis["history_sessions"]]
        previous = dict(zip(history[1:], history, strict=False))
        raw_candidates = payload["candidates"]
        if (
            not isinstance(raw_candidates, list)
            or type(payload["candidate_count"]) is not int
            or payload["candidate_count"] != len(raw_candidates)
        ):
            raise ValueError("candidate_count")
        candidates = []
        ranks = Counter()
        seen = set()
        symbols = set(basis["symbols"])
        for row in raw_candidates:
            candidate = LiquidityCandidate(
                **{
                    **row,
                    "session": date.fromisoformat(row["session"]),
                    "previous_session": date.fromisoformat(row["previous_session"]),
                }
            )
            ranks[candidate.session] += 1
            if (
                candidate.session not in session_set
                or candidate.previous_session != previous[candidate.session]
                or candidate.symbol not in symbols
                or (candidate.symbol, candidate.session) in seen
                or type(candidate.daily_universe_rank) is not int
                or candidate.daily_universe_rank != ranks[candidate.session]
                or not 1 <= candidate.daily_universe_rank <= definition.top_n
                or not math.isfinite(candidate.previous_close)
                or not math.isfinite(candidate.average_dollar_volume_20d)
                or candidate.previous_close < config.universe.min_price
                or candidate.average_dollar_volume_20d < config.universe.min_avg_dollar_volume_20d
            ):
                raise ValueError("candidate row")
            seen.add((candidate.symbol, candidate.session))
            candidates.append(candidate)
        if candidates != sorted(candidates, key=lambda row: (row.session, row.daily_universe_rank)):
            raise ValueError("candidate ordering")
        evidence = payload["daily_qualification"]
        if evidence["ready"] is not True or evidence["required_completed_sessions"] != 20:
            raise ValueError("Daily qualification")
        if any(
            evidence.get(key) != value for key, value in basis["asset_scope_diagnostics"].items()
        ):
            raise ValueError("asset scope diagnostics")
        if [row["session"] for row in evidence["sessions"]] != basis["sessions"] or any(
            row["selected"] != ranks[date.fromisoformat(row["session"])]
            for row in evidence["sessions"]
        ):
            raise ValueError("Daily qualification candidate counts")
        return payload, candidates, sessions
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError(f"{mismatch}: {exc}") from exc


def discover_liquid_universe(
    database, config, start, end, definition, *, error_prefix="RESEARCH"
) -> LiquidPreparation:
    """20 consecutive official Daily sessions, ending at T-1. No current snapshots.

    Bounded symbol batches and rolling sums; only the top 100 per session survive
    each batch. Missing/duplicate/invalid Daily observations cannot supply a window.
    """
    started = perf_counter()
    basis = selection_basis(database, config, start, end, definition, error_prefix=error_prefix)
    digest = selection_digest(basis)
    sessions = [date.fromisoformat(s) for s in basis["sessions"]]
    history = [date.fromisoformat(s) for s in basis["history_sessions"]]
    first = history[0]
    session_set = set(sessions)
    next_session = dict(zip(history, history[1:], strict=False))
    symbols = basis["symbols"]
    top = {session: [] for session in sessions}
    complete = dict.fromkeys(sessions, 0)
    survivors = dict.fromkeys(sessions, 0)
    invalid_daily_bars = 0
    minimum_price = Decimal(str(config.universe.min_price))
    minimum_volume = Decimal(str(config.universe.min_avg_dollar_volume_20d))
    for batch in iter_selection_batches(database, basis, digest):
        for symbol, rows in groupby(batch, key=lambda row: row[0]):
            daily = {}
            for _, timestamp, high, low, close, volume in rows:
                session = date.fromisoformat(timestamp[:10])
                close, high, low = Decimal(str(close)), Decimal(str(high)), Decimal(str(low))
                if session in daily or not (
                    close.is_finite()
                    and high.is_finite()
                    and low.is_finite()
                    and 0 < low <= close <= high
                    and volume >= 0
                ):
                    daily[session] = None
                    invalid_daily_bars += 1
                else:
                    daily[session] = (close, close * volume)
            window = deque()
            total = Decimal(0)
            missing = 0
            for previous in history[:-1]:
                observation = daily.get(previous)
                window.append(observation)
                total += observation[1] if observation else 0
                missing += observation is None
                if len(window) > definition.daily_lookback_sessions:
                    removed = window.popleft()
                    total -= removed[1] if removed else 0
                    missing -= removed is None
                execution = next_session[previous]
                if (
                    execution not in session_set
                    or len(window) < definition.daily_lookback_sessions
                    or missing
                ):
                    continue
                complete[execution] += 1
                close = observation[0]
                average = total / definition.daily_lookback_sessions
                if close >= minimum_price and average >= minimum_volume:
                    survivors[execution] += 1
                    top[execution].append((average, symbol, close, previous))
        for session in sessions:
            top[session] = sorted(top[session], key=lambda item: (-item[0], item[1]))[
                : definition.top_n
            ]
    candidates = [
        LiquidityCandidate(session, symbol, previous, rank, float(close), float(average))
        for session in sessions
        for rank, (average, symbol, close, previous) in enumerate(top[session], 1)
    ]
    daily_qualification = {
        "ready": bool(symbols) and all(complete.values()),
        "warmup_start": first.isoformat(),
        "required_completed_sessions": definition.daily_lookback_sessions,
        **basis["asset_scope_diagnostics"],
        "invalid_or_duplicate_daily_bars": invalid_daily_bars,
        "sessions": [
            {
                "session": session.isoformat(),
                "complete_daily_windows": complete[session],
                "eligible_after_daily_window": complete[session],
                "unavailable_daily_windows": len(symbols) - complete[session],
                "liquid_survivors": survivors[session],
                "selected": len(top[session]),
            }
            for session in sessions
        ],
    }
    seconds = perf_counter() - started
    return LiquidPreparation(
        start,
        end,
        sessions,
        candidates,
        daily_qualification,
        performance={
            "daily_universe_discovery_seconds": seconds,
            "candidate_discovery_seconds": seconds,
            "candidate_manifest_load_seconds": 0.0,
        },
        source_fingerprint=digest.hexdigest(),
    )
