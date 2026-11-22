"""Compare EdgarTools statements with the seven bound Arelle filing excerpts."""

import argparse
import json
import os
import re
import socket
import sys
import tempfile
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from importlib.metadata import distribution
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
RESEARCH = Path(r"G:\TraWalpResearch")
VERSION = "6.3.0"
BASELINE_SHA = "949f8cbc7a0d7578fbd9aa748f3aab95e9aed9ec45d33348dcc74a0d89e5948b"
SYMBOLS = {"AAPL", "ACN", "CAT", "COST", "LLY", "MSFT", "NVDA"}
INTEREST = re.compile(
    r"Debt|Borrowing|CommercialPaper|LeaseLiabilit|LeaseObligation|"
    r"OperatingIncome|IncomeLossFromContinuing|NetIncome|ProfitLoss|InterestExpens|"
    r"OperatingActivities|PaymentsToAcquire|CapitalExpenditur|EBITDA|ReturnOnInvestedCapital|"
    r"StockholdersEquity|CashAndCashEquivalents|IncomeTaxExpense|Depreciation|Amortization"
)
METRICS = (
    "total_debt",
    "finance_lease_liability",
    "operating_income",
    "parent_net_income",
    "net_income",
    "interest_expense",
    "operating_cash_flow",
    "capital_expenditures",
    "ebitda",
    "roic",
)


def deny_network(event, _args):
    # Windows asyncio creates a private TCP socketpair for its wakeup pipe.
    fallback = getattr(socket, "_fallback_socketpair", None)
    if (
        event == "socket.connect"
        and fallback is not None
        and sys._getframe(1).f_code is fallback.__code__
        and _args[1][0] in {"127.0.0.1", "::1"}
    ):
        return
    if event in {
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
        "socket.sendto",
    }:
        raise OSError("EDGARTOOLS_OFFLINE_NETWORK_DENIED")


def latest_probes(baseline):
    probes = sorted(baseline["probes"], key=lambda probe: probe["session"])
    selected = {probe["symbol"]: probe for probe in probes}
    if selected.keys() != SYMBOLS:
        raise ValueError("PILOT_SCOPE_MISMATCH")
    for symbol, probe in sorted(selected.items()):
        keys = probe["preview"]["roic"]["current"]["total_assets"]["sources"]
        scope = {
            (fact["cik"], fact["accession_number"], fact["period_end"])
            for key in keys
            for fact in (baseline["source_facts"][key]["fact"],)
            if fact["symbol"] == symbol
        }
        if len(scope) != 1:
            raise ValueError(f"AMBIGUOUS_CURRENT_BALANCE_PERIOD: {symbol}")
        yield probe, scope.pop()


def numeric(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return str(result) if result.is_finite() else None


def statement_rows(xbrl, period):
    result = {}
    for name in ("balance_sheet", "income_statement", "cash_flow_statement"):
        statement = getattr(xbrl.statements, name)()
        result[name] = {}
        for view, standard in (("standard", True), ("original", False)):
            if statement is None:
                result[name][view] = None
                continue
            frame = statement.to_dataframe(
                standard=standard,
                view="standard" if standard else "detailed",
                presentation=False,
                include_unit=True,
            )
            columns = [str(column) for column in frame.columns]
            dates = [
                column for column in columns if column == period or column.startswith(f"{period} (")
            ]
            rows = []
            for row in frame.to_dict("records"):
                concept = str(row.get("concept", ""))
                if not INTEREST.search(concept):
                    continue
                values = {column: numeric(row[column]) for column in dates}
                if values and all(value is None for value in values.values()):
                    continue
                label = row.get("standard_concept")
                rows.append(
                    {
                        "concept": concept,
                        "label": str(row.get("label")),
                        "standard_concept": label if isinstance(label, str) else None,
                        "dimensioned": str(row.get("dimension")),
                        "dimension_axis": str(row.get("dimension_axis")),
                        "dimension_member": str(row.get("dimension_member")),
                        "unit": str(row.get("unit")),
                        "values": values,
                    }
                )
            result[name][view] = {"columns": columns, "rows": rows}
    return result


def source_matches(fact, original, namespaces):
    def qname(qualified):
        prefix, _, local = qualified.partition(":")
        namespace = namespaces.get(prefix)
        return f"{{{namespace}}}{local}" if namespace and local else None

    context = original["context"]
    dimension_pairs = {
        (qname(key.removeprefix("dim_").replace("_", ":", 1)), qname(member))
        for key, member in fact.items()
        if key.startswith("dim_") and isinstance(member, str)
    }
    original_pairs = {(item.get("axis"), item.get("member")) for item in context["dimensions"]}
    return (
        not fact.get("is_nil")
        and not context["opaque_context"]
        and qname(fact["concept"]) == original["concept"]
        and fact["context_ref"] == original["context_id"]
        and (fact["entity_scheme"], fact["entity_identifier"])
        == (context["scheme"], context["entity"])
        and (fact.get("period_start"), fact.get("period_end"), fact.get("period_instant"))
        == (context["startDate"], context["endDate"], context["instant"])
        and fact.get("is_dimensioned") == bool(context["dimensions"])
        and all(axis and member for axis, member in dimension_pairs)
        and dimension_pairs == original_pairs
        and fact.get("currency") == "USD"
        and original["unit"]
        == {
            "numerator": ["{http://www.xbrl.org/2003/iso4217}USD"],
            "denominator": [],
        }
        and numeric(fact["value"]) is not None
        and Decimal(str(fact["value"])) == Decimal(original["value"])
        and fact.get("decimals") == original["decimals"]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.addaudithook(deny_network)
    install = RESEARCH / "edgartools" / VERSION
    site = install / "site"
    sys.path.insert(0, str(site))
    sys.path.insert(0, str(REPO / "scripts"))
    package = distribution("edgartools")
    package_path = Path(package.locate_file("edgar/__init__.py")).resolve()
    if package.version != VERSION or not package_path.is_relative_to(site.resolve()):
        raise ValueError("ISOLATED_EDGARTOOLS_INSTALLATION_REQUIRED")
    os.environ["EDGAR_LOCAL_DATA_DIR"] = str(install / "edgar-data")

    import audit_f01_accounting_semantic_qualification as audit
    from edgar.xbrl import XBRL

    f = audit.filings
    output = args.output
    if not output.is_absolute() or not output.is_relative_to(REPO / "reports"):
        raise ValueError("NEW_REPO_REPORT_OUTPUT_REQUIRED")
    f.bulk.directory(output.parent)
    if f.bulk.exists(output):
        raise FileExistsError(output)
    baseline = audit.read_bound(
        REPO / "reports/f01_accounting_policy_qualification.json", BASELINE_SHA, max_bytes=f.LIMIT
    )
    manifest_path = f.ROOT / f"f01-extended-filing-evidence-{audit.RUN}" / "manifest.json"
    manifest = audit.read_bound(manifest_path, audit.FILING_SHA)
    candidate = f.pilot.code_binding()
    stage_root = install / "tmp"
    f.bulk.directory(stage_root)
    rows = []
    for probe, (cik, accession, period) in latest_probes(baseline):
        started = perf_counter()
        entry = next(
            row for row in manifest["filings"] if (row["cik"], row["accession"]) == (cik, accession)
        )
        _, shard = next(f.extended_records(manifest_path, {**manifest, "filings": [entry]}))
        cache = f.ROOT / cik / accession
        size = 0
        cutoff = datetime.combine(
            datetime.fromisoformat(probe["session"]).date(), time(16), ZoneInfo("America/New_York")
        )
        with tempfile.TemporaryDirectory(prefix="filing-", dir=stage_root) as stage:
            for source in shard["cached_sources"]:
                name = source["filename"]
                if not f.NAME.fullmatch(name) or ".." in name:
                    raise ValueError("INVALID_CACHE_MEMBER")
                raw, metadata = f.cache_read(cache / name, entry, name)
                accepted = datetime.fromisoformat(source["acceptance"])
                if source != metadata or accepted.tzinfo is None or accepted > cutoff:
                    raise ValueError(f"SOURCE_OR_CUTOFF_MISMATCH: {name}")
                size += len(raw)
                (Path(stage) / name).write_bytes(raw)
            xbrl = XBRL.from_directory(stage)
            statements = statement_rows(xbrl, period)
            by_prefix = {}
            for namespace, prefix in xbrl.parser.namespace_prefixes.items():
                by_prefix.setdefault(prefix, set()).add(namespace)
            namespaces = {
                prefix: uris.pop() for prefix, uris in by_prefix.items() if len(uris) == 1
            }
            raw_facts = [
                fact
                for fact in xbrl.facts.query().execute()
                if INTEREST.search(str(fact.get("concept", "")))
                and period in (fact.get("period_instant"), fact.get("period_end"))
                and numeric(fact.get("numeric_value")) is not None
            ]
        originals = [
            audit.source_ref(fact)
            for fact in shard["observations"]
            if fact.get("context")
            and (fact["context"]["instant"] or fact["context"]["endDate"]) == period
            and INTEREST.search(fact["concept"])
            and numeric(fact["value"]) is not None
        ]
        by_context = {}
        for index, original in enumerate(originals):
            by_context.setdefault(original["context_id"], []).append(index)
        for fact in raw_facts:
            fact["original_indexes"] = [
                index
                for index in by_context.get(fact["context_ref"], [])
                if source_matches(fact, originals[index], namespaces)
            ]
        concordance = sum(bool(fact["original_indexes"]) for fact in raw_facts)
        rows.append(
            {
                "symbol": probe["symbol"],
                "cik": cik,
                "session": probe["session"],
                "period": period,
                "accession": accession,
                "acceptance_cutoff": cutoff.isoformat(),
                "shard_sha256": entry["sha256"],
                "cached_sources": shard["cached_sources"],
                "staged_bytes": size,
                "elapsed_seconds": round(perf_counter() - started, 2),
                "exact_usd_source_matches": concordance,
                "statements": statements,
                "raw_facts": raw_facts,
                "originals": originals,
                "baseline": {metric: probe["preview"][metric]["value"] for metric in METRICS},
            }
        )
        print(
            f"{probe['symbol']} {accession}: {len(raw_facts)} EdgarTools / {len(originals)} Arelle",
            flush=True,
        )
    result = {
        "contract": "F01_EDGARTOOLS_OFFLINE_COMPARISON_V1",
        "actual_version": package.version,
        "candidate_code": candidate,
        "baseline_sha256": BASELINE_SHA,
        "filing_manifest_sha256": audit.FILING_SHA,
        "review_required": True,
        "created_utc": datetime.now(UTC).isoformat(),
        "filings": rows,
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, default=str, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
