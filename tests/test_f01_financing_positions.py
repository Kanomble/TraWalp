"""Small offline checks for financing disclosures in the existing Research replay."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import f01_financing_positions as financing  # noqa: E402

PERIOD = "2026-09-30"
CIK = "0000000123"
SOURCE = {
    "sha256": "a" * 64,
    "filename": "test-filing.htm",
    "accession": "test-filing",
    "acceptance": "2026-11-04T12:00:00+00:00",
}
ROW = {**SOURCE, "cik": CIK, "cached_sources": [SOURCE]}
US_GAAP = "{http://fasb.org/us-gaap/2026}"
DECISION = datetime(2026, 11, 4, 21, tzinfo=UTC)


def original(tag, value, *, dimensions=(), role="balance sheet", context_id="balance"):
    concept = US_GAAP + tag
    return {
        "concept": concept,
        "value": value,
        "document_ordinal": 1,
        "context_id": context_id,
        "context": {
            "scheme": "http://www.sec.gov/CIK",
            "entity": CIK,
            "instant": PERIOD,
            "dimensions": dimensions,
            "opaque_context": False,
        },
        "source": SOURCE,
        "unit": {"numerator": ["{http://www.xbrl.org/2003/iso4217}USD"], "denominator": []},
        "relationships": [
            {
                "arcrole": financing.PRESENTATION,
                "linkrole": role,
                "to": concept,
                "source_sha256": "b" * 64,
            }
        ],
    }


def select(*rows, decision=DECISION):
    asset = original("Assets", "200")
    index = financing.index_filing(ROW, "f" * 64, {PERIOD}, (asset, *rows))
    return financing.reported_positions(
        {(SOURCE["accession"], PERIOD): index[PERIOD]}, [asset], decision
    )


def test_separate_positions_keep_zero_and_exclude_unproven_scope():
    positions = select(
        original("CommercialPaper", "0"),
        original("ShortTermBorrowings", "80", dimensions=("financial products",)),
        original("FinanceLeaseLiability", None),
        {**original("DebtCurrent", "90"), "source": SOURCE | {"sha256": "d" * 64}},
        original("LongTermDebtNoncurrent", "100", context_id="note-only"),
    )
    assert positions["CommercialPaper"]["value"] == "0"
    assert positions["CommercialPaper"]["sources"][0]["filing_shard_sha256"] == "f" * 64
    assert set(positions) == {"Assets", "CommercialPaper"}


def test_cutoff_and_conflicting_values_are_not_resolved_by_order():
    first = original("DebtCurrent", "24")
    second = original("DebtCurrent", "25", context_id="second")
    assert select(first, decision=datetime(2026, 11, 3, 21, tzinfo=UTC)) == {}
    for rows in ((first, second), (second, first)):
        assert select(*rows)["DebtCurrent"]["status"] == "CONFLICT"
        assert select(*rows)["DebtCurrent"]["value"] is None
    assert select(original("DebtCurrent", "-24"))["DebtCurrent"]["value"] is None
    assert select(first, original("DebtCurrent", "-24"))["DebtCurrent"]["status"] == "CONFLICT"


def test_term_subtotal_uses_complementary_positions_once():
    positions = select(
        original("LongTermDebtCurrent", "10"),
        original("LongTermDebtNoncurrent", "50"),
        original("LongTermDebt", "60", role="debt note"),
        original("DebtCurrent", "10"),
        original("CommercialPaper", "6"),
        original("FinanceLeaseLiability", "12"),
    )
    result = financing.term_subtotal(positions)
    assert result["value"] == "60"
    assert result["status"] == "VERIFIED_AGGREGATE"
    assert result["reason"] == "RECONCILED_REPORTED_TERM_DEBT"
    assert "total_debt" not in result


def test_missing_conflicting_or_unreconciled_term_positions_stay_open():
    current = original("LongTermDebtCurrent", "10")
    noncurrent = original("LongTermDebtNoncurrent", "50", role="another statement")
    assert financing.term_subtotal(select(current))["value"] is None
    assert financing.term_subtotal(select(current, noncurrent))["value"] is None
    blocked = financing.term_subtotal(select(current, noncurrent, original("LongTermDebt", "59")))
    assert blocked == {
        "value": None,
        "status": "INCOMPLETE_AGGREGATE",
        "reason": "REPORTED_TERM_AMOUNT_CONFLICT",
    }


def test_balance_role_supports_closed_term_subset_without_direct_aggregate():
    positions = select(
        original("LongTermDebtCurrent", "10"), original("LongTermDebtNoncurrent", "50")
    )
    assert financing.term_subtotal(positions)["reason"] == "ALIGNED_BALANCE_COMPONENTS"
    assert financing.term_subtotal(positions)["value"] == "60"
