"""Read-only qualification of native Daily and intraday market-data coverage."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    daily_warmup_start,
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.models.market_data import BarTimeframe


class QualificationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    MISSING_SESSION = "MISSING_SESSION"
    PARTIAL_SESSION = "PARTIAL_SESSION"
    EXTRA_OR_INVALID = "EXTRA_OR_INVALID"
    UNKNOWN_MARKET_ACTIVITY = "UNKNOWN_MARKET_ACTIVITY"


class QualificationDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    session: date
    timeframe: BarTimeframe
    expected_bar_count: int = Field(ge=0)
    actual_bar_count: int = Field(ge=0)
    missing_timestamps: tuple[str, ...] = ()
    extra_timestamps: tuple[str, ...] = ()
    invalid_bars: int = Field(default=0, ge=0)
    internal_gap: bool = False
    structural_status: QualificationStatus
    status: QualificationStatus
    reason: str
    source_diagnostic_confidence: str


class DataQualificationReport(BaseModel):
    """Bounded deterministic summary; details contain deviations only."""

    model_config = ConfigDict(frozen=True)

    timeframe: BarTimeframe
    requested_start: date
    requested_end: date
    qualification_start: date
    symbols_checked: int = Field(ge=0)
    calendar_sessions: int = Field(ge=0)
    sessions_expected: int = Field(ge=0)
    expected_bars: int = Field(ge=0)
    bars_present: int = Field(ge=0)
    complete_sessions: int = Field(ge=0)
    missing_sessions: int = Field(ge=0)
    partial_sessions: int = Field(ge=0)
    extra_or_invalid_sessions: int = Field(ge=0)
    unknown_market_activity_sessions: int = Field(ge=0)
    missing_bars: int = Field(ge=0)
    extra_bars: int = Field(ge=0)
    invalid_bars: int = Field(ge=0)
    duplicate_bars: int = Field(ge=0)
    symbols_with_internal_gaps: int = Field(ge=0)
    internal_missing_sessions: int = Field(ge=0)
    edge_or_lifecycle_missing_sessions: int = Field(ge=0)
    coverage_metadata_mismatches: int = Field(ge=0)
    unresolved_gaps: int = Field(ge=0)
    detail_records: int = Field(ge=0)
    details_truncated: bool = False
    details: tuple[QualificationDetail, ...] = ()
    warnings: tuple[str, ...] = ()


def qualify_daily_history(
    database: Database,
    symbols: Iterable[str],
    start: date,
    end: date,
    *,
    warmup_sessions: int,
    detail_limit: int = 1_000,
) -> DataQualificationReport:
    """Compare stored Daily bars with official sessions, ignoring coverage claims."""

    if warmup_sessions < 0:
        raise ValueError("warmup_sessions must not be negative")
    qualification_start = (
        daily_warmup_start(start, warmup_sessions) if warmup_sessions else start
    )
    return _qualify(
        database,
        symbols,
        start,
        end,
        qualification_start=qualification_start,
        timeframe=BarTimeframe.DAY_1,
        detail_limit=detail_limit,
        coverage_state=database.sync_values("daily_history_coverage"),
    )


def qualify_intraday_history(
    database: Database,
    symbols: Iterable[str],
    start: date,
    end: date,
    timeframe: BarTimeframe | str,
    *,
    detail_limit: int = 1_000,
) -> DataQualificationReport:
    """Measure native regular-session timestamps without inventing missing bars."""

    normalized = BarTimeframe(timeframe)
    if not normalized.intraday:
        raise ValueError("intraday qualification requires 5m, 15m, or 1h")
    return _qualify(
        database,
        symbols,
        start,
        end,
        qualification_start=start,
        timeframe=normalized,
        detail_limit=detail_limit,
        coverage_state={},
    )


def _qualify(
    database: Database,
    symbols: Iterable[str],
    requested_start: date,
    requested_end: date,
    *,
    qualification_start: date,
    timeframe: BarTimeframe,
    detail_limit: int,
    coverage_state: dict[str, Any],
) -> DataQualificationReport:
    if requested_start > requested_end:
        raise ValueError("start must not be after end")
    if detail_limit < 0:
        raise ValueError("detail_limit must not be negative")
    normalized_symbols = tuple(sorted({item.strip().upper() for item in symbols if item.strip()}))
    sessions = tuple(trading_sessions_between(qualification_start, requested_end))
    expected_by_session = {
        session: _expected_timestamps(session, timeframe) for session in sessions
    }
    session_set = set(sessions)
    details: list[QualificationDetail] = []
    detail_records = 0
    complete_sessions = 0
    missing_sessions = 0
    partial_sessions = 0
    extra_or_invalid_sessions = 0
    unknown_sessions = 0
    missing_bars = 0
    bars_present = 0
    symbols_with_internal_gaps = 0
    metadata_mismatches = 0
    internal_missing_sessions = 0
    edge_missing_sessions = 0
    duplicate_bars = 0
    total_extra_bars = 0
    total_invalid_bars = 0
    for symbol_batch, raw in _raw_bar_batches(
        database,
        normalized_symbols,
        qualification_start,
        requested_end,
        timeframe,
    ):
        timestamps: dict[tuple[str, date], set[datetime]] = defaultdict(set)
        extras: dict[tuple[str, date], list[datetime]] = defaultdict(list)
        invalid: dict[tuple[str, date], int] = defaultdict(int)
        for row in raw:
            symbol = str(row[0])
            timestamp = datetime.fromisoformat(str(row[1])).astimezone(UTC)
            session = timestamp.date()
            key = (symbol, session)
            if not _valid_raw_bar(row):
                invalid[key] += 1
                total_invalid_bars += 1
                continue
            expected = expected_by_session.get(session)
            is_expected = expected is not None and (
                timeframe is BarTimeframe.DAY_1 or timestamp in expected
            )
            if session not in session_set or not is_expected:
                extras[key].append(timestamp)
                total_extra_bars += 1
                continue
            normalized_timestamp = (
                next(iter(expected)) if timeframe is BarTimeframe.DAY_1 else timestamp
            )
            if normalized_timestamp in timestamps[key]:
                duplicate_bars += 1
            timestamps[key].add(normalized_timestamp)

        for symbol in symbol_batch:
            present_indexes = [
                index
                for index, session in enumerate(sessions)
                if timestamps.get((symbol, session), set())
            ]
            symbol_missing_indexes: list[int] = []
            symbol_has_gap = False
            symbol_has_missing = False
            first_present = min(present_indexes) if present_indexes else None
            last_present = max(present_indexes) if present_indexes else None
            for index, session in enumerate(sessions):
                key = (symbol, session)
                expected = expected_by_session[session]
                actual = timestamps.get(key, set())
                missing = sorted(expected - actual)
                extra = sorted(extras.get(key, ()))
                invalid_count = invalid.get(key, 0)
                bars_present += len(actual)
                missing_bars += len(missing)
                internal_gap = False
                if not actual:
                    structural = QualificationStatus.MISSING_SESSION
                    missing_sessions += 1
                    symbol_missing_indexes.append(index)
                    symbol_has_missing = True
                    internal_gap = (
                        first_present is not None
                        and last_present is not None
                        and first_present < index < last_present
                    )
                    if internal_gap:
                        internal_missing_sessions += 1
                        symbol_has_gap = True
                    else:
                        edge_missing_sessions += 1
                elif missing:
                    structural = QualificationStatus.PARTIAL_SESSION
                    partial_sessions += 1
                    unknown_sessions += 1
                    symbol_has_gap = True
                    symbol_has_missing = True
                else:
                    structural = QualificationStatus.COMPLETE
                if extra or invalid_count:
                    status = QualificationStatus.EXTRA_OR_INVALID
                    extra_or_invalid_sessions += 1
                    reason = "bar timestamp/structure falls outside valid native session data"
                    confidence = "high"
                elif structural is QualificationStatus.PARTIAL_SESSION:
                    status = QualificationStatus.UNKNOWN_MARKET_ACTIVITY
                    reason = (
                        "native timestamps are missing; local data cannot distinguish halt, "
                        "no-trade, provider gap, or storage gap"
                    )
                    confidence = "high_structure_low_cause"
                elif structural is QualificationStatus.MISSING_SESSION:
                    status = structural
                    reason = (
                        "no native bar is stored inside the symbol's observed local history"
                        if internal_gap
                        else "no native bar is stored; listing lifecycle or provider coverage is "
                        "not available locally"
                    )
                    confidence = "high_structure_low_cause"
                else:
                    status = structural
                    reason = "all expected native regular-session bars are present"
                    confidence = "high"
                if status is QualificationStatus.COMPLETE:
                    complete_sessions += 1
                else:
                    detail = QualificationDetail(
                        symbol=symbol,
                        session=session,
                        timeframe=timeframe,
                        expected_bar_count=len(expected),
                        actual_bar_count=len(actual),
                        missing_timestamps=tuple(item.isoformat() for item in missing),
                        extra_timestamps=tuple(item.isoformat() for item in extra),
                        invalid_bars=invalid_count,
                        internal_gap=internal_gap,
                        structural_status=structural,
                        status=status,
                        reason=reason,
                        source_diagnostic_confidence=confidence,
                    )
                    detail_records += 1
                    _retain_detail(details, detail, detail_limit)
            symbols_with_internal_gaps += int(symbol_has_gap)
            if timeframe is BarTimeframe.DAY_1 and _metadata_claims_complete(
                coverage_state.get(symbol), qualification_start, requested_end
            ):
                metadata_mismatches += int(symbol_has_missing)

        orphan_keys = sorted(
            key for key in set(extras) | set(invalid) if key[1] not in session_set
        )
        for symbol, session in orphan_keys:
            extra = sorted(extras.get((symbol, session), ()))
            invalid_count = invalid.get((symbol, session), 0)
            detail = QualificationDetail(
                symbol=symbol,
                session=session,
                timeframe=timeframe,
                expected_bar_count=0,
                actual_bar_count=0,
                extra_timestamps=tuple(item.isoformat() for item in extra),
                invalid_bars=invalid_count,
                structural_status=QualificationStatus.EXTRA_OR_INVALID,
                status=QualificationStatus.EXTRA_OR_INVALID,
                reason="bar belongs to no expected exchange session",
                source_diagnostic_confidence="high",
            )
            detail_records += 1
            _retain_detail(details, detail, detail_limit)
            extra_or_invalid_sessions += 1

    details.sort(key=lambda item: (item.symbol, item.session, item.status.value))
    warnings = ()
    if unknown_sessions:
        warnings = (
            "Missing intraday timestamps are structural gaps only; their market/provider cause "
            "is unresolved and no synthetic bars were created.",
        )
    return DataQualificationReport(
        timeframe=timeframe,
        requested_start=requested_start,
        requested_end=requested_end,
        qualification_start=qualification_start,
        symbols_checked=len(normalized_symbols),
        calendar_sessions=len(sessions),
        sessions_expected=len(normalized_symbols) * len(sessions),
        expected_bars=sum(len(value) for value in expected_by_session.values())
        * len(normalized_symbols),
        bars_present=bars_present,
        complete_sessions=complete_sessions,
        missing_sessions=missing_sessions,
        partial_sessions=partial_sessions,
        extra_or_invalid_sessions=extra_or_invalid_sessions,
        unknown_market_activity_sessions=unknown_sessions,
        missing_bars=missing_bars,
        extra_bars=total_extra_bars,
        invalid_bars=total_invalid_bars,
        duplicate_bars=duplicate_bars,
        symbols_with_internal_gaps=symbols_with_internal_gaps,
        internal_missing_sessions=internal_missing_sessions,
        edge_or_lifecycle_missing_sessions=edge_missing_sessions,
        coverage_metadata_mismatches=metadata_mismatches,
        unresolved_gaps=missing_sessions + unknown_sessions,
        detail_records=detail_records,
        details_truncated=detail_records > detail_limit,
        details=tuple(details[:detail_limit]),
        warnings=warnings,
    )


def _expected_timestamps(session: date, timeframe: BarTimeframe) -> set[datetime]:
    if timeframe is BarTimeframe.DAY_1:
        return {datetime.combine(session, time.min, tzinfo=UTC)}
    opening, closing = regular_session_bounds(session)
    current = opening
    if timeframe is BarTimeframe.HOUR_1 and current.minute:
        current = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    expected: set[datetime] = set()
    while current < closing:
        expected.add(current)
        current += timeframe.duration
    return expected


def _raw_bar_batches(
    database: Database,
    symbols: tuple[str, ...],
    start: date,
    end: date,
    timeframe: BarTimeframe,
) -> Iterator[tuple[tuple[str, ...], list[tuple[Any, ...]]]]:
    if not symbols:
        return
    with database.read_only() as connection:
        for offset in range(0, len(symbols), 400):
            batch = symbols[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            selected = connection.execute(
                f"""SELECT symbol,timestamp,open,high,low,close,volume,trade_count,vwap
                FROM bars WHERE symbol IN ({placeholders}) AND timeframe=?
                AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp""",
                [
                    *batch,
                    timeframe.value,
                    start.isoformat(),
                    (end + timedelta(days=1)).isoformat(),
                ],
            ).fetchall()
            yield batch, [tuple(row) for row in selected]


def _valid_raw_bar(row: tuple[Any, ...]) -> bool:
    try:
        opening, high, low, close = (Decimal(str(row[index])) for index in range(2, 6))
        volume = int(row[6])
        trade_count = int(row[7]) if row[7] is not None else None
        vwap = Decimal(str(row[8])) if row[8] is not None else None
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (
        min(opening, high, low, close) > 0
        and high >= max(opening, close, low)
        and low <= min(opening, close, high)
        and volume >= 0
        and (trade_count is None or trade_count >= 0)
        and (vwap is None or vwap > 0)
    )


def _metadata_claims_complete(value: Any, start: date, end: date) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        marked_start = datetime.fromisoformat(str(value["start"])).date()
        marked_end = datetime.fromisoformat(str(value["end_exclusive"])).date()
    except (KeyError, TypeError, ValueError):
        return False
    return marked_start <= start and marked_end >= end + timedelta(days=1)


def _retain_detail(
    details: list[QualificationDetail],
    detail: QualificationDetail,
    limit: int,
) -> None:
    if len(details) < limit:
        details.append(detail)
        return
    if not detail.internal_gap and detail.status is not QualificationStatus.EXTRA_OR_INVALID:
        return
    for index in range(len(details) - 1, -1, -1):
        current = details[index]
        if not current.internal_gap and current.status is not QualificationStatus.EXTRA_OR_INVALID:
            details[index] = detail
            return
