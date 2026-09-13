"""Small immutable views for the frozen F/configured replay consumers only."""

from dataclasses import dataclass
from datetime import date
from typing import NamedTuple


class ScoreValue(NamedTuple):
    score: float | None


class ReplayScores(NamedTuple):
    quality: ScoreValue
    valuation: ScoreValue
    opportunity: ScoreValue
    timing: ScoreValue


@dataclass(frozen=True, slots=True)
class ReplayTechnical:
    # Same fields/defaults as TechnicalSnapshot, without per-replay Pydantic work.
    market_session: date | None = None
    price: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    sma20_rising: bool | None = None
    rsi14: float | None = None
    rsi_recovery: bool | None = None
    momentum5: float | None = None
    momentum20: float | None = None
    momentum20_improving: bool | None = None
    momentum63: float | None = None
    momentum126: float | None = None
    volatility: float | None = None
    atr14: float | None = None
    relative_volume: float | None = None
    drawdown_52w: float | None = None
    drawdown_63d: float | None = None
    recovery_from_63d_low: float | None = None
    max_drawdown_126d: float | None = None
    sma200_distance: float | None = None


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    symbol: str
    sic: str | None
    as_of: date
    scores: ReplayScores
    technical: ReplayTechnical
    exclusion_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayReport:
    as_of: date
    records: tuple[ReplayRecord, ...]
    f_candidates: tuple[tuple[float, ReplayRecord], ...]
    omitted_rejections: tuple[tuple[str, int], ...]


def replay_record(row, session):
    technical = dict(row["technical"])
    if technical.get("market_session") is not None:
        technical["market_session"] = date.fromisoformat(technical["market_session"])
    return ReplayRecord(
        row["symbol"],
        row.get("sic"),
        session,
        ReplayScores(*(ScoreValue(value) for value in row["scores"])),
        ReplayTechnical(**technical),
        tuple(row["exclusion_reasons"]),
    )


def replay_report(payload):
    session = date.fromisoformat(payload["session"])
    eligible = tuple(replay_record(row, session) for row in payload["eligible"])
    rejected = tuple(replay_record(row, session) for row in payload["rejected"])
    return ReplayReport(
        session,
        eligible + rejected,
        tuple(
            (row["evaluation_score"], record)
            for row, record in zip(payload["eligible"], eligible, strict=True)
        ),
        tuple(payload["omitted_rejections"].items()),
    )
