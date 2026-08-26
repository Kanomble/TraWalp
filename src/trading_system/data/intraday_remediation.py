"""Candidate-path intraday qualification and targeted provider remediation."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    intraday_session_bounds,
    intraday_warmup_start,
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.models.market_data import BarTimeframe

PROVIDER_OBSERVATION_SOURCE = "intraday_provider_observations"
PROVIDER_OBSERVATION_VERSION = 1


class IntradayRequirementStatus(StrEnum):
    REQUIRED_PRESENT = "REQUIRED_PRESENT"
    LOCAL_MISSING_FETCHABLE = "LOCAL_MISSING_FETCHABLE"
    PROVIDER_CONFIRMED_ABSENT = "PROVIDER_CONFIRMED_ABSENT"
    PROVIDER_CHECK_FAILED = "PROVIDER_CHECK_FAILED"
    NOT_REQUIRED = "NOT_REQUIRED"


class IntradayQualificationStatus(StrEnum):
    READY = "READY"
    READY_WITH_PROVIDER_ABSENCE = "READY_WITH_PROVIDER_ABSENCE"
    NOT_READY_LOCAL_GAPS = "NOT_READY_LOCAL_GAPS"
    NOT_READY_PROVIDER_VERIFICATION_FAILURE = "NOT_READY_PROVIDER_VERIFICATION_FAILURE"


@dataclass(frozen=True, slots=True)
class CandidateIntradayRequirement:
    symbol: str
    session: date
    timeframe: BarTimeframe
    candidate_paths: tuple[str, ...] = ()
    requirement_type: str = "candidate_session"


class IntradaySynchronizer(Protocol):
    def sync_intraday(
        self,
        requested_symbols: Iterable[str],
        timeframes: Iterable[BarTimeframe | str],
        start: datetime,
        end: datetime,
        *,
        incremental: bool | None = None,
        extended_hours: bool | None = None,
    ) -> dict[str, Any]: ...


def candidate_requirements_from_report(
    payload: Mapping[str, Any],
    *,
    start: date,
    end: date,
    timeframes: Iterable[BarTimeframe | str],
) -> tuple[CandidateIntradayRequirement, ...]:
    """Validate a PIT candidate manifest and return its bounded execution sessions."""

    if not isinstance(payload, Mapping):
        raise ValueError("candidate report must contain a JSON object")
    if payload.get("discovery_complete") is not True:
        raise ValueError("candidate report discovery is not complete")
    report_start = _parse_date(payload.get("requested_start"), "requested_start")
    report_end = _parse_date(payload.get("requested_end"), "requested_end")
    if start < report_start or end > report_end:
        raise ValueError(
            "candidate report does not cover the requested remediation interval "
            f"{start.isoformat()} through {end.isoformat()}"
        )
    normalized_timeframes = tuple(dict.fromkeys(BarTimeframe(item) for item in timeframes))
    if not normalized_timeframes or any(not item.intraday for item in normalized_timeframes):
        raise ValueError("candidate remediation requires 5m, 15m, or 1h")
    default_paths = tuple(str(item) for item in payload.get("strategies", ()) if str(item))
    requirements: list[CandidateIntradayRequirement] = []
    raw_candidate_sessions = payload.get("candidate_sessions", ())
    if not isinstance(raw_candidate_sessions, list):
        raise ValueError("candidate report candidate_sessions must be a list")
    candidate_pairs: set[tuple[str, date]] = set()
    for item in raw_candidate_sessions:
        if not isinstance(item, Mapping):
            raise ValueError("candidate report contains a malformed candidate session")
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("candidate report contains an empty candidate symbol")
        candidate_pairs.add(
            (symbol, _parse_date(item.get("execution_session"), "execution_session"))
        )
    raw_ranges = payload.get("potential_position_ranges")
    if raw_ranges is not None:
        if not isinstance(raw_ranges, list):
            raise ValueError("candidate report potential_position_ranges must be a list")
        for item in raw_ranges:
            if not isinstance(item, Mapping):
                raise ValueError("candidate report contains a malformed potential position range")
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                raise ValueError("candidate report contains an empty range symbol")
            range_start = max(
                start, _parse_date(item.get("first_execution_session"), "first_execution_session")
            )
            range_end = min(
                end, _parse_date(item.get("last_potential_session"), "last_potential_session")
            )
            if range_start > range_end:
                continue
            paths = tuple(str(value) for value in item.get("candidate_paths", ()) if str(value))
            for session in trading_sessions_between(range_start, range_end):
                for timeframe in normalized_timeframes:
                    requirements.append(
                        CandidateIntradayRequirement(
                            symbol=symbol,
                            session=session,
                            timeframe=timeframe,
                            candidate_paths=paths or default_paths,
                            requirement_type=(
                                "candidate_session"
                                if (symbol, session) in candidate_pairs
                                else "potential_open_position_session"
                            ),
                        )
                    )
        return _merge_requirements(requirements)

    raw_sessions = payload.get("required_sessions", payload.get("candidate_sessions"))
    if not isinstance(raw_sessions, list):
        raise ValueError("candidate report candidate_sessions must be a list")
    for item in raw_sessions:
        if not isinstance(item, Mapping):
            raise ValueError("candidate report contains a malformed candidate session")
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("candidate report contains an empty candidate symbol")
        session = _parse_date(item.get("execution_session"), "execution_session")
        if session < start or session > end:
            continue
        paths = tuple(str(value) for value in item.get("candidate_paths", ()) if str(value))
        requirement_type = str(item.get("requirement_type", "candidate_session"))
        for timeframe in normalized_timeframes:
            requirements.append(
                CandidateIntradayRequirement(
                    symbol=symbol,
                    session=session,
                    timeframe=timeframe,
                    candidate_paths=paths or default_paths,
                    requirement_type=requirement_type,
                )
            )
    return _merge_requirements(requirements)


def qualify_candidate_intraday_coverage(
    database: Database,
    requirements: Iterable[CandidateIntradayRequirement],
    *,
    validation_start: date,
    validation_end: date,
    strategies_considered: Iterable[str],
    feed: str,
    adjustment: str,
    extended_hours: bool,
    warmup_bars: int,
    fetch_metrics: Mapping[str, int] | None = None,
    irrelevant_gap_count: int = 0,
    include_present_details: bool = True,
) -> dict[str, Any]:
    """Classify local candidate sessions against durable provider observations."""

    normalized = _merge_requirements(requirements)
    observations = database.sync_values(PROVIDER_OBSERVATION_SOURCE)
    bars_by_key = _candidate_bars(database, normalized, extended_hours=extended_hours)
    details: list[dict[str, Any]] = []
    counts = {status.value: 0 for status in IntradayRequirementStatus}
    for requirement in normalized:
        expected = _expected_timestamps(
            requirement.session,
            requirement.timeframe,
            extended_hours=extended_hours,
        )
        present = bars_by_key.get(
            (requirement.symbol, requirement.timeframe, requirement.session), set()
        )
        missing = tuple(timestamp for timestamp in expected if timestamp not in present)
        observation = observations.get(_observation_key(requirement))
        status, reason = _classify_missing(
            requirement,
            missing,
            observation,
            feed=feed,
            adjustment=adjustment,
            extended_hours=extended_hours,
        )
        counts[status.value] += 1
        detail = {
            "symbol": requirement.symbol,
            "session": requirement.session.isoformat(),
            "timeframe": requirement.timeframe.value,
            "candidate_paths": list(requirement.candidate_paths),
            "requirement_type": requirement.requirement_type,
            "classification": status.value,
            "reason": reason,
            "expected_bar_count": len(expected),
            "actual_bar_count": len(set(expected) & present),
            "missing_timestamp_count": len(missing),
            "missing_timestamps": [item.isoformat() for item in missing],
            "blocking": status
            in {
                IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE,
                IntradayRequirementStatus.PROVIDER_CHECK_FAILED,
            },
        }
        if include_present_details or status is not IntradayRequirementStatus.REQUIRED_PRESENT:
            details.append(detail)

    warmup_details = _warmup_details(
        database,
        normalized,
        observations,
        feed=feed,
        adjustment=adjustment,
        extended_hours=extended_hours,
        warmup_bars=warmup_bars,
    )
    session_counts = dict(counts)
    for detail in warmup_details:
        counts[detail["classification"]] += 1
    blocking_reasons: list[str] = []
    if counts[IntradayRequirementStatus.PROVIDER_CHECK_FAILED.value]:
        qualification = IntradayQualificationStatus.NOT_READY_PROVIDER_VERIFICATION_FAILURE
        blocking_reasons.append("one or more required provider checks failed")
    elif counts[IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE.value]:
        qualification = IntradayQualificationStatus.NOT_READY_LOCAL_GAPS
        blocking_reasons.append("required local intraday gaps have not been successfully checked")
    elif counts[IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT.value]:
        qualification = IntradayQualificationStatus.READY_WITH_PROVIDER_ABSENCE
    else:
        qualification = IntradayQualificationStatus.READY

    metrics = {
        "fetch_attempted_count": 0,
        "fetch_success_count": 0,
        "fetch_failed_count": 0,
        "provider_request_count": 0,
        "bars_inserted": 0,
        **dict(fetch_metrics or {}),
    }
    required_session_details = [item for item in details]
    return {
        "report_type": "candidate_intraday_qualification",
        "validation_start": validation_start.isoformat(),
        "validation_end": validation_end.isoformat(),
        "timeframes": sorted({item.timeframe.value for item in normalized}),
        "strategies_considered": sorted(set(str(item) for item in strategies_considered)),
        "candidate_symbol_sessions_required": len(normalized),
        "required_present_count": session_counts[IntradayRequirementStatus.REQUIRED_PRESENT.value],
        "required_local_missing_count": session_counts[
            IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE.value
        ],
        "provider_confirmed_absent_count": session_counts[
            IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT.value
        ],
        "provider_check_failed_count": session_counts[
            IntradayRequirementStatus.PROVIDER_CHECK_FAILED.value
        ],
        "warmup_local_missing_count": sum(
            item["classification"] == IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE.value
            for item in warmup_details
        ),
        "warmup_provider_confirmed_absent_count": sum(
            item["classification"] == IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT.value
            for item in warmup_details
        ),
        "warmup_provider_check_failed_count": sum(
            item["classification"] == IntradayRequirementStatus.PROVIDER_CHECK_FAILED.value
            for item in warmup_details
        ),
        "irrelevant_gap_count": irrelevant_gap_count,
        **metrics,
        "qualification_status": qualification.value,
        "blocking_reasons": blocking_reasons,
        "provider_observation_source": PROVIDER_OBSERVATION_SOURCE,
        "provider_feed": feed,
        "provider_adjustment": adjustment,
        "extended_hours": extended_hours,
        "synthetic_bars_created": False,
        "details": required_session_details,
        "warmup_details": warmup_details,
        "classification_counts": counts,
    }


def remediate_candidate_intraday_coverage(
    database: Database,
    requirements: Iterable[CandidateIntradayRequirement],
    *,
    validation_start: date,
    validation_end: date,
    strategies_considered: Iterable[str],
    feed: str,
    adjustment: str,
    extended_hours: bool,
    warmup_bars: int,
    synchronizer_factory: Callable[[], IntradaySynchronizer],
) -> dict[str, Any]:
    """Fetch only unverified candidate gaps and persist provider-cause evidence."""

    normalized = _merge_requirements(requirements)
    before = qualify_candidate_intraday_coverage(
        database,
        normalized,
        validation_start=validation_start,
        validation_end=validation_end,
        strategies_considered=strategies_considered,
        feed=feed,
        adjustment=adjustment,
        extended_hours=extended_hours,
        warmup_bars=warmup_bars,
        include_present_details=False,
    )
    targets = [
        item
        for item in before["details"]
        if item["classification"]
        in {
            IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE.value,
            IntradayRequirementStatus.PROVIDER_CHECK_FAILED.value,
        }
    ]
    warmup_targets = [
        item
        for item in before["warmup_details"]
        if item["classification"]
        in {
            IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE.value,
            IntradayRequirementStatus.PROVIDER_CHECK_FAILED.value,
        }
    ]
    metrics = {
        "fetch_attempted_count": len(targets) + len(warmup_targets),
        "fetch_success_count": 0,
        "fetch_failed_count": 0,
        "provider_request_count": 0,
        "bars_inserted": 0,
    }
    if not targets and not warmup_targets:
        return before | metrics | {"network_accessed": False, "pre_remediation": _summary(before)}

    synchronizer = synchronizer_factory()
    grouped: dict[tuple[BarTimeframe, date], list[dict[str, Any]]] = defaultdict(list)
    by_identity = {(item.symbol, item.session, item.timeframe): item for item in normalized}
    for item in targets:
        grouped[(BarTimeframe(item["timeframe"]), date.fromisoformat(item["session"]))].append(item)
    for (timeframe, session), target_details in sorted(
        grouped.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        symbols = tuple(sorted({item["symbol"] for item in target_details}))
        missing_timestamps = [
            datetime.fromisoformat(timestamp)
            for item in target_details
            for timestamp in item["missing_timestamps"]
        ]
        opening = min(missing_timestamps)
        closing = max(missing_timestamps) + timeframe.duration
        _fetch_and_record(
            database,
            synchronizer,
            symbols,
            timeframe,
            session,
            opening,
            closing,
            feed=feed,
            adjustment=adjustment,
            extended_hours=extended_hours,
            metrics=metrics,
            requirements=by_identity,
        )

    for item in warmup_targets:
        timeframe = BarTimeframe(item["timeframe"])
        session = date.fromisoformat(item["first_candidate_session"])
        opening, _ = regular_session_bounds(session)
        warmup_start = intraday_warmup_start(
            session,
            timeframe,
            warmup_bars,
            extended_hours=extended_hours,
        )
        result, error = _sync_window(
            synchronizer,
            (item["symbol"],),
            timeframe,
            warmup_start,
            opening,
            extended_hours=extended_hours,
        )
        metrics["provider_request_count"] += int(result.get("request_batches", 0))
        metrics["bars_inserted"] += int(result.get("bars_inserted", 0))
        observation_key = _warmup_observation_key(item["symbol"], timeframe, session)
        if error is not None:
            metrics["fetch_failed_count"] += 1
            database.set_sync_value(
                PROVIDER_OBSERVATION_SOURCE,
                observation_key,
                _observation_payload(
                    item["symbol"],
                    timeframe,
                    session,
                    feed=feed,
                    adjustment=adjustment,
                    extended_hours=extended_hours,
                    status=IntradayRequirementStatus.PROVIDER_CHECK_FAILED,
                    missing_timestamps=(),
                    error=error,
                    requirement_type="warmup",
                ),
            )
        else:
            metrics["fetch_success_count"] += 1
            available = database.bars_available_as_of(
                item["symbol"],
                opening,
                timeframe=timeframe,
                limit=warmup_bars,
            )
            if len(available) >= warmup_bars:
                database.delete_sync_value(PROVIDER_OBSERVATION_SOURCE, observation_key)
            else:
                database.set_sync_value(
                    PROVIDER_OBSERVATION_SOURCE,
                    observation_key,
                    _observation_payload(
                        item["symbol"],
                        timeframe,
                        session,
                        feed=feed,
                        adjustment=adjustment,
                        extended_hours=extended_hours,
                        status=IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT,
                        missing_timestamps=(),
                        error=None,
                        requirement_type="warmup",
                    ),
                )

    after = qualify_candidate_intraday_coverage(
        database,
        normalized,
        validation_start=validation_start,
        validation_end=validation_end,
        strategies_considered=strategies_considered,
        feed=feed,
        adjustment=adjustment,
        extended_hours=extended_hours,
        warmup_bars=warmup_bars,
        fetch_metrics=metrics,
        include_present_details=False,
    )
    return after | {"network_accessed": True, "pre_remediation": _summary(before)}


def export_intraday_remediation_report(
    report: Mapping[str, Any], output_directory: Path, *, stem: str
) -> dict[str, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_directory / f"{stem}.json",
        "csv": output_directory / f"{stem}_gaps.csv",
    }
    existing = next((path for path in paths.values() if path.exists()), None)
    if existing is not None:
        raise FileExistsError(f"Intraday remediation report already exists: {existing}")
    paths["json"].write_text(json.dumps(report, indent=2), encoding="utf-8")
    fieldnames = (
        "symbol",
        "session",
        "timeframe",
        "candidate_paths",
        "requirement_type",
        "classification",
        "reason",
        "expected_bar_count",
        "actual_bar_count",
        "missing_timestamp_count",
        "missing_timestamps",
        "blocking",
    )
    with paths["csv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for detail in report.get("details", ()):
            writer.writerow(
                {
                    **{key: detail.get(key) for key in fieldnames},
                    "candidate_paths": ";".join(detail.get("candidate_paths", ())),
                    "missing_timestamps": ";".join(detail.get("missing_timestamps", ())),
                }
            )
    return paths


def _candidate_bars(
    database: Database,
    requirements: tuple[CandidateIntradayRequirement, ...],
    *,
    extended_hours: bool,
) -> dict[tuple[str, BarTimeframe, date], set[datetime]]:
    output: dict[tuple[str, BarTimeframe, date], set[datetime]] = defaultdict(set)
    by_timeframe: dict[BarTimeframe, list[CandidateIntradayRequirement]] = defaultdict(list)
    for requirement in requirements:
        by_timeframe[requirement.timeframe].append(requirement)
    for timeframe, items in by_timeframe.items():
        symbols = tuple(sorted({item.symbol for item in items}))
        sessions_by_timestamp = {
            (item.symbol, timestamp): item.session
            for item in items
            for timestamp in _expected_timestamps(
                item.session,
                timeframe,
                extended_hours=extended_hours,
            )
        }
        first = min(
            intraday_session_bounds(item.session, extended_hours=extended_hours)[0]
            for item in items
        )
        last = max(
            intraday_session_bounds(item.session, extended_hours=extended_hours)[1]
            for item in items
        )
        for bar in database.bars_between(symbols, first, last, timeframe=timeframe):
            timestamp = bar.timestamp.astimezone(UTC)
            session = sessions_by_timestamp.get((bar.symbol, timestamp))
            if session is not None:
                output[(bar.symbol, timeframe, session)].add(timestamp)
    return output


def _merge_requirements(
    requirements: Iterable[CandidateIntradayRequirement],
) -> tuple[CandidateIntradayRequirement, ...]:
    paths: dict[tuple[str, date, BarTimeframe], set[str]] = defaultdict(set)
    requirement_types: dict[tuple[str, date, BarTimeframe], set[str]] = defaultdict(set)
    for item in requirements:
        key = (item.symbol.upper(), item.session, item.timeframe)
        paths[key].update(item.candidate_paths)
        requirement_types[key].add(item.requirement_type)
    return tuple(
        CandidateIntradayRequirement(
            symbol=symbol,
            session=session,
            timeframe=timeframe,
            candidate_paths=tuple(sorted(candidate_paths)),
            requirement_type=(
                "candidate_session"
                if "candidate_session" in requirement_types[(symbol, session, timeframe)]
                else "+".join(sorted(requirement_types[(symbol, session, timeframe)]))
            ),
        )
        for (symbol, session, timeframe), candidate_paths in sorted(
            paths.items(), key=lambda item: (item[0][2].value, item[0][1], item[0][0])
        )
    )


def _warmup_details(
    database: Database,
    requirements: tuple[CandidateIntradayRequirement, ...],
    observations: Mapping[str, Any],
    *,
    feed: str,
    adjustment: str,
    extended_hours: bool,
    warmup_bars: int,
) -> list[dict[str, Any]]:
    if warmup_bars <= 0:
        return []
    first_by_symbol: dict[tuple[str, BarTimeframe], date] = {}
    paths_by_symbol: dict[tuple[str, BarTimeframe], set[str]] = defaultdict(set)
    for requirement in requirements:
        key = (requirement.symbol, requirement.timeframe)
        first_by_symbol[key] = min(
            first_by_symbol.get(key, requirement.session), requirement.session
        )
        paths_by_symbol[key].update(requirement.candidate_paths)
    details: list[dict[str, Any]] = []
    for (symbol, timeframe), session in sorted(
        first_by_symbol.items(), key=lambda item: (item[0][1].value, item[0][0])
    ):
        opening, _ = regular_session_bounds(session)
        available = database.bars_available_as_of(
            symbol,
            opening,
            timeframe=timeframe,
            limit=warmup_bars,
        )
        available = [bar for bar in available if bar.timestamp < opening]
        if len(available) >= warmup_bars:
            continue
        observation = observations.get(_warmup_observation_key(symbol, timeframe, session))
        status = IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE
        reason = "insufficient native pre-entry warmup"
        if _observation_matches(
            observation,
            symbol=symbol,
            timeframe=timeframe,
            session=session,
            feed=feed,
            adjustment=adjustment,
            extended_hours=extended_hours,
            requirement_type="warmup",
        ):
            observed_status = observation.get("status")
            if observed_status == IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT.value:
                status = IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT
                reason = (
                    "provider successfully checked the warmup interval; fewer native bars exist"
                )
            elif observed_status == IntradayRequirementStatus.PROVIDER_CHECK_FAILED.value:
                status = IntradayRequirementStatus.PROVIDER_CHECK_FAILED
                reason = str(observation.get("error") or "provider warmup check failed")
        details.append(
            {
                "symbol": symbol,
                "first_candidate_session": session.isoformat(),
                "timeframe": timeframe.value,
                "candidate_paths": sorted(paths_by_symbol[(symbol, timeframe)]),
                "classification": status.value,
                "reason": reason,
                "required_native_bars": warmup_bars,
                "available_native_bars": len(available),
                "blocking": status
                in {
                    IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE,
                    IntradayRequirementStatus.PROVIDER_CHECK_FAILED,
                },
                "requirement_type": "warmup",
            }
        )
    return details


def _fetch_and_record(
    database: Database,
    synchronizer: IntradaySynchronizer,
    symbols: tuple[str, ...],
    timeframe: BarTimeframe,
    session: date,
    start: datetime,
    end: datetime,
    *,
    feed: str,
    adjustment: str,
    extended_hours: bool,
    metrics: dict[str, int],
    requirements: Mapping[tuple[str, date, BarTimeframe], CandidateIntradayRequirement],
) -> None:
    result, error = _sync_window(
        synchronizer,
        symbols,
        timeframe,
        start,
        end,
        extended_hours=extended_hours,
    )
    metrics["provider_request_count"] += int(result.get("request_batches", 0))
    metrics["bars_inserted"] += int(result.get("bars_inserted", 0))
    if error is not None:
        metrics["fetch_failed_count"] += len(symbols)
    else:
        metrics["fetch_success_count"] += len(symbols)
    expected = _expected_timestamps(session, timeframe, extended_hours=extended_hours)
    session_start, session_end = intraday_session_bounds(session, extended_hours=extended_hours)
    bars = database.bars_between(symbols, session_start, session_end, timeframe=timeframe)
    present: dict[str, set[datetime]] = defaultdict(set)
    for bar in bars:
        present[bar.symbol].add(bar.timestamp.astimezone(UTC))
    for symbol in symbols:
        requirement = requirements[(symbol, session, timeframe)]
        missing = tuple(item for item in expected if item not in present[symbol])
        key = _observation_key(requirement)
        if error is not None:
            database.set_sync_value(
                PROVIDER_OBSERVATION_SOURCE,
                key,
                _observation_payload(
                    symbol,
                    timeframe,
                    session,
                    feed=feed,
                    adjustment=adjustment,
                    extended_hours=extended_hours,
                    status=IntradayRequirementStatus.PROVIDER_CHECK_FAILED,
                    missing_timestamps=missing,
                    error=error,
                    requirement_type=requirement.requirement_type,
                ),
            )
        elif missing:
            database.set_sync_value(
                PROVIDER_OBSERVATION_SOURCE,
                key,
                _observation_payload(
                    symbol,
                    timeframe,
                    session,
                    feed=feed,
                    adjustment=adjustment,
                    extended_hours=extended_hours,
                    status=IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT,
                    missing_timestamps=missing,
                    error=None,
                    requirement_type=requirement.requirement_type,
                ),
            )
        else:
            database.delete_sync_value(PROVIDER_OBSERVATION_SOURCE, key)


def _sync_window(
    synchronizer: IntradaySynchronizer,
    symbols: tuple[str, ...],
    timeframe: BarTimeframe,
    start: datetime,
    end: datetime,
    *,
    extended_hours: bool,
) -> tuple[dict[str, Any], str | None]:
    try:
        result = synchronizer.sync_intraday(
            symbols,
            (timeframe,),
            start,
            end,
            incremental=False,
            extended_hours=extended_hours,
        )
        if not isinstance(result, Mapping):
            raise TypeError(
                "provider check returned a non-mapping result "
                f"({type(result).__name__})"
            )
        normalized_result = dict(result)
        errors = int(normalized_result.get("errors", 0))
        invalid = int(normalized_result.get("invalid_bars", 0))
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if errors or invalid:
        return (
            normalized_result,
            f"provider check returned errors={errors}, invalid_bars={invalid}",
        )
    return normalized_result, None


def _classify_missing(
    requirement: CandidateIntradayRequirement,
    missing: tuple[datetime, ...],
    observation: Any,
    *,
    feed: str,
    adjustment: str,
    extended_hours: bool,
) -> tuple[IntradayRequirementStatus, str]:
    if not missing:
        return (
            IntradayRequirementStatus.REQUIRED_PRESENT,
            "all required native candidate-session timestamps are stored locally",
        )
    if not _observation_matches(
        observation,
        symbol=requirement.symbol,
        timeframe=requirement.timeframe,
        session=requirement.session,
        feed=feed,
        adjustment=adjustment,
        extended_hours=extended_hours,
        requirement_type=requirement.requirement_type,
    ):
        return (
            IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE,
            "required native timestamps are locally missing and have no matching provider check",
        )
    observed_status = observation.get("status")
    if observed_status == IntradayRequirementStatus.PROVIDER_CHECK_FAILED.value:
        return (
            IntradayRequirementStatus.PROVIDER_CHECK_FAILED,
            str(observation.get("error") or "provider check failed"),
        )
    observed_missing = set(str(item) for item in observation.get("missing_timestamps", ()))
    current_missing = {item.isoformat() for item in missing}
    if (
        observed_status == IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT.value
        and current_missing <= observed_missing
    ):
        return (
            IntradayRequirementStatus.PROVIDER_CONFIRMED_ABSENT,
            "provider request succeeded and did not return the missing native timestamps",
        )
    return (
        IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE,
        "provider evidence does not cover the current missing timestamps",
    )


def _expected_timestamps(
    session: date,
    timeframe: BarTimeframe,
    *,
    extended_hours: bool,
) -> tuple[datetime, ...]:
    opening, closing = intraday_session_bounds(session, extended_hours=extended_hours)
    current = opening
    if timeframe is BarTimeframe.HOUR_1 and current.minute:
        current = current.replace(minute=0, second=0, microsecond=0) + timeframe.duration
    output: list[datetime] = []
    while current < closing:
        output.append(current)
        current += timeframe.duration
    return tuple(output)


def _observation_key(requirement: CandidateIntradayRequirement) -> str:
    return f"{requirement.timeframe.value}|{requirement.symbol}|{requirement.session.isoformat()}"


def _warmup_observation_key(symbol: str, timeframe: BarTimeframe, session: date) -> str:
    return f"warmup|{timeframe.value}|{symbol}|{session.isoformat()}"


def _observation_payload(
    symbol: str,
    timeframe: BarTimeframe,
    session: date,
    *,
    feed: str,
    adjustment: str,
    extended_hours: bool,
    status: IntradayRequirementStatus,
    missing_timestamps: Iterable[datetime],
    error: str | None,
    requirement_type: str = "candidate_session",
) -> dict[str, Any]:
    return {
        "version": PROVIDER_OBSERVATION_VERSION,
        "symbol": symbol,
        "timeframe": timeframe.value,
        "session": session.isoformat(),
        "feed": feed,
        "adjustment": adjustment,
        "extended_hours": extended_hours,
        "requirement_type": requirement_type,
        "status": status.value,
        "missing_timestamps": [item.isoformat() for item in missing_timestamps],
        "error": error,
        "checked_at": datetime.now(UTC).isoformat(),
    }


def _observation_matches(
    observation: Any,
    *,
    symbol: str,
    timeframe: BarTimeframe,
    session: date,
    feed: str,
    adjustment: str,
    extended_hours: bool,
    requirement_type: str,
) -> bool:
    return bool(
        isinstance(observation, Mapping)
        and observation.get("version") == PROVIDER_OBSERVATION_VERSION
        and observation.get("symbol") == symbol
        and observation.get("timeframe") == timeframe.value
        and observation.get("session") == session.isoformat()
        and observation.get("feed") == feed
        and observation.get("adjustment") == adjustment
        and observation.get("extended_hours") is extended_hours
        and observation.get("requirement_type") == requirement_type
    )


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"candidate report {field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"candidate report {field} must be an ISO date") from exc


def _summary(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: report[key]
        for key in (
            "candidate_symbol_sessions_required",
            "required_present_count",
            "required_local_missing_count",
            "provider_confirmed_absent_count",
            "provider_check_failed_count",
            "qualification_status",
        )
    }
