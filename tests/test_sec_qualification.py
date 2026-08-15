from trading_system.data.database import Database
from trading_system.data.sec_qualification import qualify_sec_contexts
from trading_system.data.xbrl_parser import parse_company_facts
from trading_system.models.fundamentals import CompanyIdentity


def _payload() -> dict:
    shared = {
        "end": "2026-06-30",
        "filed": "2026-08-01",
        "form": "10-Q",
        "accn": "0001-26-000001",
        "fy": 2026,
        "fp": "Q2",
    }
    return {
        "cik": 1,
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            {**shared, "start": "2026-04-01", "val": 100},
                            {**shared, "start": "2026-01-01", "val": 180},
                        ]
                    }
                }
            }
        },
    }


def test_sec_qualification_detects_period_start_context_missing_from_database(
    tmp_path,
) -> None:
    database = Database(tmp_path / "sec.sqlite3")
    database.initialize()
    database.upsert_company(CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA"))
    payload = _payload()
    parsed = parse_company_facts(payload, "AAA")
    database.upsert_facts(parsed[:1])
    database.cache_sec_payload("0000000001", "companyfacts", payload)

    result = qualify_sec_contexts(database, ["AAA"])

    assert result.symbols_requested == 1
    assert result.cached_symbols_analyzed == 1
    assert result.parsed_facts == 2
    assert result.missing_context_facts == 1
    assert result.missing_period_start_contexts == 1
    assert result.additional_discrete_quarter_contexts == 1
    assert result.reconstruction_complete
    assert result.repair_recommended
    assert result.details[0].symbol == "AAA"
    assert result.details[0].missing_context_facts == 1


def test_sec_qualification_reports_unavailable_reconstruction_without_raw_cache(
    tmp_path,
) -> None:
    database = Database(tmp_path / "sec.sqlite3")
    database.initialize()
    database.upsert_company(CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA"))

    result = qualify_sec_contexts(database, ["AAA"])

    assert result.cached_symbols_analyzed == 0
    assert result.symbols_without_raw_cache == 1
    assert not result.reconstruction_complete
    assert result.repair_recommended
