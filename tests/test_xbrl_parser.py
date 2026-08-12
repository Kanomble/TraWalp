from decimal import Decimal

from trading_system.data.xbrl_parser import parse_company_facts


def _payload() -> dict:
    observation = {
        "start": "2024-01-01",
        "end": "2024-03-31",
        "val": 123,
        "accn": "0001",
        "fy": 2024,
        "fp": "Q1",
        "form": "10-Q",
        "filed": "2024-05-05",
        "frame": "CY2024Q1",
    }
    return {
        "cik": 1234,
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [observation]}
                },
                "EarningsPerShareDiluted": {
                    "units": {"USD/shares": [{**observation, "val": "1.25"}]}
                },
            }
        },
    }


def test_alternative_tags_are_normalized_and_dates_preserved() -> None:
    facts = parse_company_facts(_payload(), "orcl")
    by_metric = {fact.metric: fact for fact in facts}
    assert by_metric["revenue"].value == Decimal("123")
    assert by_metric["revenue"].filed.isoformat() == "2024-05-05"
    assert by_metric["eps_diluted"].unit == "USD/shares"
    assert by_metric["eps_diluted"].value == Decimal("1.25")
    assert by_metric["revenue"].cik == "0000001234"


def test_invalid_and_non_filing_observations_are_skipped() -> None:
    payload = _payload()
    observations = payload["facts"]["us-gaap"][
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]["units"]["USD"]
    observations.extend(
        [
            {"end": "2024-03-31", "filed": "2024-05-05", "form": "8-K", "val": 99},
            {"end": "bad", "filed": "2024-05-05", "form": "10-Q", "val": 99},
        ]
    )
    revenue = [fact for fact in parse_company_facts(payload, "X") if fact.metric == "revenue"]
    assert len(revenue) == 1


def test_missing_metric_is_unavailable_not_zero() -> None:
    metrics = {fact.metric for fact in parse_company_facts(_payload(), "X")}
    assert "net_income" not in metrics


def test_separate_depreciation_and_amortization_tags_are_preserved() -> None:
    payload = _payload()
    observation = {
        "start": "2024-01-01",
        "end": "2024-03-31",
        "val": 10,
        "accn": "da-1",
        "fy": 2024,
        "fp": "Q1",
        "form": "10-Q",
        "filed": "2024-05-05",
    }
    payload["facts"]["us-gaap"]["Depreciation"] = {"units": {"USD": [observation]}}
    payload["facts"]["us-gaap"]["AmortizationOfIntangibleAssets"] = {
        "units": {"USD": [{**observation, "val": 3, "accn": "da-2"}]}
    }
    by_metric = {fact.metric: fact.value for fact in parse_company_facts(payload, "X")}
    assert by_metric["depreciation"] == Decimal("10")
    assert by_metric["amortization"] == Decimal("3")


def test_combined_and_component_debt_tags_are_mapped_semantically() -> None:
    payload = _payload()
    observation = {
        "end": "2024-03-31",
        "val": 100,
        "accn": "debt-1",
        "fy": 2024,
        "fp": "Q1",
        "form": "10-Q",
        "filed": "2024-05-05",
    }
    gaap = payload["facts"]["us-gaap"]
    gaap["DebtLongtermAndShorttermCombinedAmount"] = {"units": {"USD": [observation]}}
    gaap["DebtCurrent"] = {"units": {"USD": [{**observation, "val": 20, "accn": "debt-2"}]}}
    gaap["LongTermNotesAndLoans"] = {
        "units": {"USD": [{**observation, "val": 80, "accn": "debt-3"}]}
    }
    by_metric = {fact.metric: fact.value for fact in parse_company_facts(payload, "X")}
    assert by_metric["total_debt"] == Decimal("100")
    assert by_metric["debt_current"] == Decimal("20")
    assert by_metric["debt_noncurrent"] == Decimal("80")
