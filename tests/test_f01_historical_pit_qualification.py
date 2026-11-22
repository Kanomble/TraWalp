"""Test historical audit mechanics without opening SQLite or any external artifacts."""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reports"))
import f01_historical_pit_qualification_run as pit  # noqa: E402

from trading_system.models.fundamentals import FundamentalFact


def sample(metric="shares_outstanding", accession="0000000123-26-000001"):
    return FundamentalFact(
        cik="0000000123",
        symbol="TEST",
        metric=metric,
        tag="Test",
        value=Decimal(100),
        unit="shares" if metric == "shares_outstanding" else "USD",
        period_end=date(2026, 9, 30),
        filed=date(2026, 10, 7),
        form="10-Q",
        accession_number=accession,
    )


def test_dual_gate_and_elapsed_time_use_real_timezone():
    accepted = datetime(2026, 10, 7, 1, 13, 15, tzinfo=UTC)
    assert pit.gate(accepted, date(2026, 10, 7), date(2026, 9, 30)) == datetime(
        2026, 10, 7, 4, tzinfo=UTC
    )
    assert pit.elapsed(pit.midnight(date(2026, 3, 8)), pit.midnight(date(2026, 3, 9))) == 82800


def test_events_are_deduplicated_and_include_existing_shares_expiry():
    fact = sample()
    accepted = datetime(2026, 10, 7, 1, 13, 15, tzinfo=UTC)
    reader = SimpleNamespace(
        facts=[fact, fact],
        evidence=SimpleNamespace(acceptance={fact.accession_number: accepted}),
        flows={},
        debts={},
    )
    filings = {
        fact.accession_number: {"filed": str(fact.filed), "acceptance": accepted.isoformat()}
    }
    events = pit.events_for(
        reader,
        filings,
        pit.midnight(date(2026, 1, 1)),
        pit.midnight(date(2029, 1, 1)),
        raw_facts=[sample(accession="unmatched")],
    )
    available = pit.midnight(fact.filed)
    expire = pit.midnight(fact.period_end + pit.MAX_SHARES_OUTSTANDING_AGE + timedelta(days=1))
    assert events[available] == {fact.accession_number, "UNPROVEN_FILED_DATE_DIAGNOSTIC"}
    assert events[expire] == {"SHARES_FRESHNESS_EXPIRY"}


def test_missing_evidence_is_distinct_from_an_accounting_scope_gap():
    debt = sample("debt_current")
    assert (
        pit.missing_class(
            "total_debt",
            "NO_VALUE",
            {"debt_current": [debt]},
            {},
            debt.period_end,
            pit.midnight(debt.filed),
        )
        == "MISSING_BOUND_ORIGINAL"
    )
    assert (
        pit.missing_class(
            "total_debt",
            "NO_VALUE",
            {"debt_current": [debt]},
            {"debt_current": {debt}},
            debt.period_end,
            pit.midnight(debt.filed),
        )
        == "ACCOUNTING_INPUT_NOT_QUALIFIED"
    )


def test_provenance_disallows_filing_before_its_exact_gate():
    fact = sample()
    accepted = datetime(2026, 10, 7, 15, 0, 0, tzinfo=UTC)
    evidence = SimpleNamespace(
        semantic=SimpleNamespace(acceptance={fact.accession_number: accepted}),
        originals={},
    )
    with pytest.raises(ValueError, match="ORIGINAL_FACT_BEFORE_ACCEPTANCE"):
        pit.filing_provenance([fact], evidence, accepted - timedelta(microseconds=1))
    assert pit.filing_provenance([fact], evidence, accepted) == ((fact.accession_number,), ())
