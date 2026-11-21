from datetime import date
from decimal import Decimal

from trading_system.data.xbrl_parser import parse_company_facts
from trading_system.fundamentals.metrics import discrete_quarters


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


def test_same_filing_preserves_discrete_quarter_and_year_to_date_contexts() -> None:
    payload = _payload()
    observations = payload["facts"]["us-gaap"][
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]["units"]["USD"]
    observations[:] = [
        {
            **observations[0],
            "start": "2024-04-01",
            "end": "2024-06-30",
            "val": 120,
            "fp": "Q2",
        },
        {
            **observations[0],
            "start": "2024-01-01",
            "end": "2024-06-30",
            "val": 220,
            "fp": "Q2",
        },
    ]

    revenue = [
        fact for fact in parse_company_facts(payload, "X") if fact.metric == "revenue"
    ]

    assert [(fact.period_start.isoformat(), fact.value) for fact in revenue] == [
        ("2024-04-01", Decimal("120")),
        ("2024-01-01", Decimal("220")),
    ]


def test_dei_shares_outstanding_preserves_taxonomy_and_concept() -> None:
    payload = _payload()
    payload["facts"]["dei"] = {
        "EntityCommonStockSharesOutstanding": {
            "units": {
                "shares": [
                    {
                        "end": "2024-04-25",
                        "val": 239_228_849,
                        "accn": "shares-1",
                        "fy": 2024,
                        "fp": "Q1",
                        "form": "10-Q",
                        "filed": "2024-04-30",
                    }
                ]
            }
        }
    }
    payload["facts"]["us-gaap"]["CommonStockSharesOutstanding"] = {
        "units": {
            "shares": [
                {
                    "end": "2020-04-10",
                    "val": 1_957_000_000,
                    "accn": "shares-old",
                    "fy": 2020,
                    "fp": "Q2",
                    "form": "10-Q",
                    "filed": "2020-08-10",
                }
            ]
        }
    }

    facts = parse_company_facts(payload, "exe")
    share_facts = [fact for fact in facts if fact.metric == "shares_outstanding"]
    shares = next(fact for fact in share_facts if fact.taxonomy == "dei")
    assert shares.taxonomy == "dei"
    assert shares.tag == "EntityCommonStockSharesOutstanding"
    assert shares.unit == "shares"
    assert shares.value == Decimal("239228849")
    assert {(fact.taxonomy, fact.tag) for fact in share_facts} == {
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
    }
    assert {fact.metric for fact in facts} >= {"revenue", "eps_diluted", "shares_outstanding"}


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


def test_nonoperating_interest_maps_exact_tag_with_original_identity() -> None:
    payload = _payload()
    observation = {
        "start": "2024-04-01",
        "end": "2024-06-30",
        "val": "183600000",
        "accn": "0000001234-24-000001",
        "fy": 2024,
        "fp": "Q2",
        "form": "10-Q",
        "filed": "2024-08-08",
        "frame": "CY2024Q2",
    }
    concept = {"units": {"USD": [observation]}}
    payload["facts"]["us-gaap"]["InterestExpenseNonoperating"] = concept
    payload["facts"]["us-gaap"]["InterestExpenseNONOPERATING"] = concept
    payload["facts"]["ifrs-full"] = {"InterestExpenseNonoperating": concept}

    facts = [
        fact for fact in parse_company_facts(payload, "x") if fact.metric == "interest_expense"
    ]
    assert [fact.model_dump(mode="json") for fact in facts] == [
        {
            "cik": "0000001234",
            "symbol": "X",
            "metric": "interest_expense",
            "taxonomy": "us-gaap",
            "tag": "InterestExpenseNonoperating",
            "unit": "USD",
            "value": "183600000",
            "period_start": observation["start"],
            "period_end": observation["end"],
            "filed": observation["filed"],
            "accession_number": observation["accn"],
            "fiscal_year": observation["fy"],
            "fiscal_period": observation["fp"],
            "form": observation["form"],
            "frame": observation["frame"],
        }
    ]


def test_interest_aliases_preserve_legacy_precedence_and_count_each_quarter_once() -> None:
    tags = (
        "InterestExpenseNonOperating",
        "InterestAndDebtExpense",
        "InterestExpense",
        "InterestExpenseNonoperating",
    )
    payload = _payload()
    gaap = payload["facts"]["us-gaap"]
    quarter = gaap["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"][0]
    for tag in tags:
        gaap[tag] = {"units": {"USD": [quarter]}}
    gaap[tags[-1]]["units"]["USD"] += [quarter]

    for tag in tags:
        facts = parse_company_facts({"cik": 1234, "facts": {"us-gaap": {tag: gaap[tag]}}}, "X")
        assert [(fact.metric, fact.tag) for fact in facts] == [("interest_expense", tag)]
    assert [
        fact.tag for fact in parse_company_facts(payload, "X") if fact.metric == "interest_expense"
    ] == [tags[0]]

    next_quarter = quarter | {
        "start": "2024-04-01",
        "end": "2024-06-30",
        "filed": "2024-08-08",
        "accn": "0002",
        "val": 200,
    }
    cumulative = next_quarter | {"start": "2024-01-01", "val": 323}
    gaap[tags[-1]]["units"]["USD"] += [next_quarter, cumulative]
    preserved = parse_company_facts(payload, "X", preserve_observations=True)
    assert {fact.tag for fact in preserved if fact.metric == "interest_expense"} == set(tags)
    assert discrete_quarters(preserved, "interest_expense", date(2024, 5, 4)) == []
    quarters = discrete_quarters(preserved, "interest_expense", date(2024, 8, 8))
    assert [(item.period_end, item.value) for item in quarters] == [
        (date(2024, 3, 31), Decimal("123")),
        (date(2024, 6, 30), Decimal("200")),
    ]
