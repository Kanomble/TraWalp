from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from trading_system.data.database import Database
from trading_system.data.intraday_remediation import (
    CandidateIntradayRequirement,
    IntradayQualificationStatus,
    IntradayRequirementStatus,
    candidate_requirements_from_report,
    qualify_candidate_intraday_coverage,
    remediate_candidate_intraday_coverage,
)
from trading_system.data.market_sessions import regular_session_bounds
from trading_system.models.market_data import BarTimeframe, MarketDataBar

SESSION = date(2025, 5, 2)
TIMEFRAME = BarTimeframe.MINUTES_15


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "intraday-remediation.sqlite3")
    database.initialize()
    return database


def _timestamps(session: date = SESSION) -> tuple[datetime, ...]:
    opening, closing = regular_session_bounds(session)
    output: list[datetime] = []
    current = opening
    while current < closing:
        output.append(current)
        current += TIMEFRAME.duration
    return tuple(output)


def _bar(symbol: str, timestamp: datetime) -> MarketDataBar:
    return MarketDataBar(
        symbol=symbol,
        timeframe=TIMEFRAME,
        timestamp=timestamp.astimezone(UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=1_000,
        trade_count=10,
        vwap=Decimal("100"),
    )


def _requirement(symbol: str = "AAA") -> CandidateIntradayRequirement:
    return CandidateIntradayRequirement(
        symbol=symbol,
        session=SESSION,
        timeframe=TIMEFRAME,
        candidate_paths=("F0/C", "F3/C", "F5/C"),
    )


def _qualify(database: Database, requirements=None) -> dict:
    requirements = requirements or (_requirement(),)
    return qualify_candidate_intraday_coverage(
        database,
        requirements,
        validation_start=SESSION,
        validation_end=SESSION,
        strategies_considered=("F0/C", "F3/C", "F5/C"),
        feed="iex",
        adjustment="all",
        extended_hours=False,
        warmup_bars=0,
    )


def _remediate(database: Database, factory, requirements=None) -> dict:
    requirements = requirements or (_requirement(),)
    return remediate_candidate_intraday_coverage(
        database,
        requirements,
        validation_start=SESSION,
        validation_end=SESSION,
        strategies_considered=("F0/C", "F3/C", "F5/C"),
        feed="iex",
        adjustment="all",
        extended_hours=False,
        warmup_bars=0,
        synchronizer_factory=factory,
    )


class _AvailableProvider:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls: list[tuple] = []

    def sync_intraday(
        self,
        requested_symbols,
        timeframes,
        start,
        end,
        *,
        incremental=None,
        extended_hours=None,
    ):
        symbols = tuple(requested_symbols)
        self.calls.append((symbols, tuple(timeframes), start, end, incremental, extended_hours))
        inserted = 0
        for symbol in symbols:
            existing = {
                bar.timestamp
                for bar in self.database.bars_between([symbol], start, end, timeframe=TIMEFRAME)
            }
            missing = [
                timestamp
                for timestamp in _timestamps()
                if start <= timestamp < end and timestamp not in existing
            ]
            self.database.upsert_bars([_bar(symbol, timestamp) for timestamp in missing])
            inserted += len(missing)
        return {
            "request_batches": 1,
            "bars_inserted": inserted,
            "errors": 0,
            "invalid_bars": 0,
        }


class _AbsentProvider:
    def __init__(self) -> None:
        self.calls = 0

    def sync_intraday(self, *args, **kwargs):
        self.calls += 1
        return {
            "request_batches": 1,
            "bars_inserted": 0,
            "errors": 0,
            "invalid_bars": 0,
        }


class _FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def sync_intraday(self, *args, **kwargs):
        self.calls += 1
        return {
            "request_batches": 1,
            "bars_inserted": 0,
            "errors": 1,
            "invalid_bars": 0,
        }


class _MalformedProvider:
    def __init__(self) -> None:
        self.calls = 0

    def sync_intraday(self, *args, **kwargs):
        self.calls += 1
        return None


class _TimeoutProvider:
    def __init__(self) -> None:
        self.calls = 0

    def sync_intraday(self, *args, **kwargs):
        self.calls += 1
        raise TimeoutError("fixture provider timeout")


def test_required_present_is_ready_without_provider_factory(tmp_path) -> None:
    database = _database(tmp_path)
    database.upsert_bars([_bar("AAA", timestamp) for timestamp in _timestamps()])

    report = _remediate(
        database,
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be created")),
    )

    assert report["qualification_status"] == IntradayQualificationStatus.READY.value
    assert report["required_present_count"] == 1
    assert report["fetch_attempted_count"] == 0
    assert report["network_accessed"] is False


def test_local_missing_available_is_targeted_and_post_sync_ready(tmp_path) -> None:
    database = _database(tmp_path)
    database.upsert_bars([_bar("AAA", timestamp) for timestamp in _timestamps()[:-1]])
    provider = _AvailableProvider(database)

    before = _qualify(database)
    after = _remediate(database, lambda: provider)

    assert before["required_local_missing_count"] == 1
    assert after["qualification_status"] == IntradayQualificationStatus.READY.value
    assert after["required_present_count"] == 1
    assert after["fetch_attempted_count"] == after["fetch_success_count"] == 1
    assert len(provider.calls) == 1
    assert provider.calls[0][0] == ("AAA",)
    assert provider.calls[0][4] is False
    assert provider.calls[0][3] - provider.calls[0][2] == TIMEFRAME.duration


def test_provider_absence_is_persisted_and_second_run_is_idempotent(tmp_path) -> None:
    database = _database(tmp_path)
    database.upsert_bars([_bar("AAA", timestamp) for timestamp in _timestamps()[:-1]])
    provider = _AbsentProvider()

    first = _remediate(database, lambda: provider)
    second = _remediate(
        database,
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be created twice")),
    )

    assert first["qualification_status"] == (
        IntradayQualificationStatus.READY_WITH_PROVIDER_ABSENCE.value
    )
    assert first["provider_confirmed_absent_count"] == 1
    assert second["provider_confirmed_absent_count"] == 1
    assert second["fetch_attempted_count"] == 0
    assert second["network_accessed"] is False
    assert provider.calls == 1


def test_missing_irrelevant_symbol_is_not_scanned_or_fetched(tmp_path) -> None:
    database = _database(tmp_path)
    database.upsert_bars([_bar("AAA", timestamp) for timestamp in _timestamps()])

    report = _remediate(
        database,
        lambda: (_ for _ in ()).throw(AssertionError("provider must not be created")),
    )

    assert report["candidate_symbol_sessions_required"] == 1
    assert report["required_present_count"] == 1
    assert "BBB" not in str(report)
    assert report["fetch_attempted_count"] == 0


def test_partial_session_is_not_misclassified_as_complete(tmp_path) -> None:
    database = _database(tmp_path)
    missing = _timestamps()[7]
    database.upsert_bars(
        [_bar("AAA", timestamp) for timestamp in _timestamps() if timestamp != missing]
    )

    report = _qualify(database)
    detail = report["details"][0]

    assert detail["classification"] == IntradayRequirementStatus.LOCAL_MISSING_FETCHABLE.value
    assert detail["actual_bar_count"] == 25
    assert detail["expected_bar_count"] == 26
    assert detail["missing_timestamps"] == [missing.isoformat()]
    assert detail["blocking"] is True


def test_early_close_uses_calendar_session_length(tmp_path) -> None:
    database = _database(tmp_path)
    early_close = date(2025, 11, 28)
    timestamps = _timestamps(early_close)
    database.upsert_bars([_bar("AAA", timestamp) for timestamp in timestamps])
    requirement = CandidateIntradayRequirement("AAA", early_close, TIMEFRAME)

    report = qualify_candidate_intraday_coverage(
        database,
        (requirement,),
        validation_start=early_close,
        validation_end=early_close,
        strategies_considered=("F0/C",),
        feed="iex",
        adjustment="all",
        extended_hours=False,
        warmup_bars=0,
    )

    assert len(timestamps) == 14
    assert report["details"][0]["expected_bar_count"] == 14
    assert report["qualification_status"] == IntradayQualificationStatus.READY.value


def test_provider_failure_is_blocking_and_not_recorded_as_absence(tmp_path) -> None:
    database = _database(tmp_path)
    database.upsert_bars([_bar("AAA", timestamp) for timestamp in _timestamps()[:-1]])
    provider = _FailingProvider()

    report = _remediate(database, lambda: provider)
    retried = _remediate(database, lambda: provider)

    assert report["qualification_status"] == (
        IntradayQualificationStatus.NOT_READY_PROVIDER_VERIFICATION_FAILURE.value
    )
    assert report["provider_check_failed_count"] == 1
    assert report["provider_confirmed_absent_count"] == 0
    assert report["fetch_failed_count"] == 1
    assert retried["provider_check_failed_count"] == 1
    assert provider.calls == 2


def test_malformed_provider_response_is_retryable_failure_not_absence(tmp_path) -> None:
    database = _database(tmp_path)
    provider = _MalformedProvider()

    report = _remediate(database, lambda: provider)
    retried = _remediate(database, lambda: provider)

    assert report["qualification_status"] == (
        IntradayQualificationStatus.NOT_READY_PROVIDER_VERIFICATION_FAILURE.value
    )
    assert report["provider_check_failed_count"] == 1
    assert report["provider_confirmed_absent_count"] == 0
    assert "non-mapping result" in report["details"][0]["reason"]
    assert retried["provider_check_failed_count"] == 1
    assert provider.calls == 2


def test_provider_exception_is_retryable_failure_not_absence(tmp_path) -> None:
    database = _database(tmp_path)
    provider = _TimeoutProvider()

    report = _remediate(database, lambda: provider)
    retried = _remediate(database, lambda: provider)

    assert report["qualification_status"] == (
        IntradayQualificationStatus.NOT_READY_PROVIDER_VERIFICATION_FAILURE.value
    )
    assert report["provider_check_failed_count"] == 1
    assert report["provider_confirmed_absent_count"] == 0
    assert "fixture provider timeout" in report["details"][0]["reason"]
    assert retried["provider_check_failed_count"] == 1
    assert provider.calls == 2


def test_candidate_manifest_is_bounded_and_does_not_recursively_select_symbols() -> None:
    payload = {
        "discovery_complete": True,
        "requested_start": "2025-05-01",
        "requested_end": "2025-05-05",
        "strategies": ["F0/C", "F3/C", "F5/C"],
        "candidate_symbols": [{"symbol": "IRRELEVANT_METADATA"}],
        "candidate_sessions": [
            {"symbol": "AAA", "execution_session": "2025-05-02"},
            {"symbol": "FUTURE", "execution_session": "2025-05-05"},
        ],
    }

    requirements = candidate_requirements_from_report(
        payload,
        start=date(2025, 5, 1),
        end=date(2025, 5, 2),
        timeframes=(TIMEFRAME,),
    )

    assert [(item.symbol, item.session) for item in requirements] == [("AAA", SESSION)]
    assert requirements[0].candidate_paths == ("F0/C", "F3/C", "F5/C")


def test_candidate_manifest_expands_only_declared_potential_position_range() -> None:
    payload = {
        "discovery_complete": True,
        "requested_start": "2025-05-01",
        "requested_end": "2025-05-05",
        "strategies": ["F0/C", "F3/C", "F5/C"],
        "candidate_sessions": [{"symbol": "AAA", "execution_session": "2025-05-02"}],
        "potential_position_ranges": [
            {
                "symbol": "AAA",
                "first_execution_session": "2025-05-01",
                "last_potential_session": "2025-05-05",
            }
        ],
    }

    requirements = candidate_requirements_from_report(
        payload,
        start=date(2025, 5, 1),
        end=date(2025, 5, 2),
        timeframes=(TIMEFRAME,),
    )

    assert [(item.session, item.requirement_type) for item in requirements] == [
        (date(2025, 5, 1), "potential_open_position_session"),
        (date(2025, 5, 2), "candidate_session"),
    ]
