"""Targeted offline checks for the E.13 reader and approval contract."""

import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import read_f01_canonical_research as cli  # noqa: E402

from trading_system.fundamentals.accounting_fallback import DEBT_BRIDGE, OriginalAmount
from trading_system.fundamentals.canonical_research import RULES, CanonicalResearchReader, values
from trading_system.fundamentals.semantic import TOTAL_DEBT, Amendment, Evidence
from trading_system.models.fundamentals import FundamentalFact

ACCEPTANCE = datetime(2026, 10, 29, 15, 35, 46, tzinfo=UTC)
ACCESSION = "0000000123-26-000007"
PERIOD = date(2026, 9, 30)


def fact(tag, value, metric, *, accession=ACCESSION, form="10-Q", filed=date(2026, 10, 29)):
    return FundamentalFact(
        cik="0000000123",
        symbol="TEST",
        metric=metric,
        tag=tag,
        value=Decimal(value),
        unit="USD",
        period_end=PERIOD,
        filed=filed,
        form=form,
        accession_number=accession,
    )


def reader(facts, originals, *, review=True, evidence=None):
    asset = next(item for item in facts if item.metric == "total_assets")
    return CanonicalResearchReader(
        facts,
        cik=asset.cik,
        evidence=evidence or Evidence(acceptance={ACCESSION: ACCEPTANCE}),
        originals=dict.fromkeys(originals, {}),
        flows={},
        debts={asset: OriginalAmount(asset, Decimal(20), ACCEPTANCE, "exact-debt-proof")},
        amendments={},
        reviewed_rules=frozenset({DEBT_BRIDGE}) if review else frozenset(),
    )


def test_arbitrary_timestamp_and_exact_acceptance():
    asset = fact("Assets", "100", "total_assets")
    research = reader([asset], [asset])
    before = research.read(ACCEPTANCE - timedelta(microseconds=1)).accounting
    after = research.read(ACCEPTANCE + timedelta(microseconds=1)).accounting
    assert values(before)["total_debt"] is None
    assert values(after)["total_debt"] == 20
    assert after.current_balance["total_debt"].references == ("exact-debt-proof",)


def test_preserves_existing_zero_and_disabled_fallback():
    asset, debt = fact("Assets", "100", "total_assets"), fact(TOTAL_DEBT, "0", "total_debt")
    assert (
        values(reader([asset, debt], [asset, debt]).read(ACCEPTANCE).accounting)["total_debt"] == 0
    )
    assert (
        values(reader([asset], [asset], review=False).read(ACCEPTANCE).accounting)["total_debt"]
        is None
    )


def test_reader_rejects_unmatched_canonical_value_and_unzoned_timestamp():
    asset = fact("Assets", "100", "total_assets")
    wrong = fact("Assets", "101", "total_assets")
    research = reader([asset], [wrong])
    assert not research.read(ACCEPTANCE).accounting.current_balance["total_assets"].sources
    with pytest.raises(ValueError, match="TIMEZONE_AWARE"):
        research.read(ACCEPTANCE.replace(tzinfo=None))


def test_conflicting_amendment_requires_link_and_its_own_acceptance():
    asset = fact("Assets", "100", "total_assets")
    original = fact(TOTAL_DEBT, "18", "total_debt")
    amended = fact(
        TOTAL_DEBT,
        "25",
        "total_debt",
        accession="0000000123-26-000008",
        form="10-Q/A",
        filed=date(2026, 10, 30),
    )
    accepted = ACCEPTANCE + timedelta(days=1)
    facts = [asset, original, amended]
    metadata = {ACCESSION: ACCEPTANCE, amended.accession_number: accepted}
    unknown = reader(facts, facts, evidence=Evidence(acceptance=metadata))
    assert values(unknown.read(accepted - timedelta(microseconds=1)).accounting)["total_debt"] == 18
    assert values(unknown.read(accepted).accounting)["total_debt"] is None
    known = reader(
        facts,
        facts,
        evidence=Evidence(
            acceptance=metadata,
            amendments={amended.accession_number: Amendment(ACCESSION, "reviewed")},
        ),
    )
    assert values(known.read(accepted).accounting)["total_debt"] == 25


def test_separate_review_cannot_rebind_code_and_respects_deny():
    definition = {
        "contract": cli.RELEASE,
        "binding": {"reader_candidate": "one"},
        "decisions": dict.fromkeys(RULES, "PENDING"),
        "reviewer": "",
    }
    reviewed = definition | {"reviewer": "Independent"}
    with pytest.raises(ValueError, match="INDEPENDENT"):
        cli.reviewed_rules(definition, reviewed)
    reviewed["decisions"] = {
        key: "APPROVE_RESEARCH" if key == DEBT_BRIDGE else "DENY" for key in RULES
    }
    assert cli.reviewed_rules(definition, reviewed) == {DEBT_BRIDGE}
    with pytest.raises(ValueError, match="BINDING_DRIFT"):
        cli.reviewed_rules(definition, reviewed | {"binding": {"reader_candidate": "two"}})
