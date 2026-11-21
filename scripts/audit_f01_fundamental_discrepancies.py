"""Trace F01 pilot gaps from verified artifacts, ZIP members and optional read-only DBs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import zipfile
from collections import Counter
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

sys.dont_write_bytecode = True
import validate_f01_sec_bulk_pilot as qualification  # noqa: E402

from trading_system.config import load_strategy_config  # noqa: E402
from trading_system.data.canonical_facts import fingerprint  # noqa: E402
from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.database import Database  # noqa: E402
from trading_system.data.xbrl_parser import TAG_ALIASES, parse_company_facts  # noqa: E402
from trading_system.fundamentals.metrics import (  # noqa: E402
    MAX_SHARES_OUTSTANDING_AGE,
    debt_as_of,
    discrete_quarters,
    shares_outstanding_as_of,
    ttm_value,
)
from trading_system.fundamentals.quality import accounting_state_as_of  # noqa: E402
from trading_system.shadow.inputs import LocalInputs  # noqa: E402

pilot = qualification.pilot
bulk = qualification.bulk
require = pilot.require
ARTIFACTS = qualification.ARTIFACTS
QUALIFICATION_DIR = ARTIFACTS / "f01-pilot-qualification-20261121-213940"
LEGACY_DIR = ARTIFACTS / "f01-pilot-qualification-legacy-20261121-214213"
REPORT_NAME = "f01_sec_bulk_pilot_qualification.json"
QUALIFICATION_SHA = "0284d8b5486a0a84b9628e3fd5bc5b96395057272c27c91f05ede3b5bed9d8ce"
LEGACY_SHA = "05f441c0e9f71269abe36d61a3dfd36bd6f5689bcf08fccee63a9cf4f2559bf7"
QUALIFICATION_CODE = "defd166e22f198891dfadeb8b09c94490a660cfc6f78333126cb113111a67b27"
SCRIPT = "scripts/audit_f01_fundamental_discrepancies.py"
ACN_DEBT = date(2024, 6, 28)
LEGACY_READER = "Database.facts_available_as_of on both inputs"
DEBT_METRICS = {"total_debt", "debt_current", "debt_noncurrent"}
UNMAPPED_WITNESSES = {
    "CAT": {"NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"},
    "COST": {
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "NetIncomeLossAttributableToNoncontrollingInterest",
    },
    "LLY": {"InterestExpenseNonoperating", "PaymentsToAcquireOtherPropertyPlantAndEquipment"},
}
LLY_FLOWS = (
    "operating_income",
    "capital_expenditures",
    "net_income",
    "tax_expense",
    "interest_expense",
    "depreciation_amortization",
    "depreciation",
    "amortization",
)
CONCEPTS = {
    "ACN": r"shares.*(outstanding|issued|treasury)|"
    r"(common|ordinary|class[a-z])stock|debt|borrow|financelease|capitallease",
    "LLY": r"operatingincome|operatingprofit|income(loss)?fromoperation|^profit(loss)?$|"
    r"^netincome|^interest.*expense|^income.*tax.*expense|depreciation|amortization|"
    r"capitalexpenditure|(payments|additions|purchases).*propertyplant",
    "CAT": r"(cash(provided|used)|cashflow).*operat",
    "COST": r"^netincome|^profitloss|netincome.*noncontrolling|"
    r"(cash(provided|used)|cashflow).*operat",
    "AAPL": r"debt|borrow|financelease|capitallease",
    "MSFT": r"debt|borrow|financelease|capitallease",
}


def verified_artifact(directory, expected_hash):
    report = qualification.checked_metadata(directory / REPORT_NAME, expected_hash)
    require(
        report["input_report_sha256"] == qualification.REPORT_SHA
        and report["qualification_candidate"]["fingerprint"] == QUALIFICATION_CODE
        and report["data_errors"] == []
        and report["dataset_version"] == 8,
        "QUALIFICATION_ARTIFACT_BINDING_MISMATCH",
    )
    for name, sha in report["artifact_sha256"].items():
        path = directory / name
        require(
            path.name == name
            and bulk.plain(path).st_size <= bulk.MAX_MANIFEST_BYTES
            and pilot.file_sha(path) == sha,
            f"QUALIFICATION_COMPANION_MISMATCH: {name}",
        )
    return report


def evidence():
    candidate = pilot.code_binding()
    repository = pilot.repository_evidence(pilot.REPO)
    files = dict(repository["files"])
    additive_hash = files.pop(SCRIPT)
    runtime = [repository["runtime_packages"], sys.version, sqlite3.sqlite_version]
    require(
        fingerprint([files, *runtime]) == QUALIFICATION_CODE
        and repository["code_fingerprint"] == candidate["fingerprint"],
        "HISTORICAL_QUALIFICATION_CODE_BINDING_MISMATCH",
    )
    report = qualification.checked_metadata(
        qualification.RESULT / pilot.REPORT, qualification.REPORT_SHA
    )
    qualified = verified_artifact(QUALIFICATION_DIR, QUALIFICATION_SHA)
    comparison = verified_artifact(LEGACY_DIR, LEGACY_SHA)
    require(
        report["binding"] == qualified["snapshot"] == comparison["snapshot"]
        and comparison["legacy_binding"]["reader"] == LEGACY_READER,
        "PILOT_AND_LEGACY_SNAPSHOT_BINDING_MISMATCH",
    )
    scope = pilot.pilot_spec()
    admission_path = ARTIFACTS / "f01-pilot-admission-reviewed-v2.json"
    admission_sha = report["binding"]["admission_sha256"]
    layout = bulk.Layout(
        {
            "import_artifacts": ARTIFACTS,
            "originals": pilot.RESEARCH_ROOT / "sec-originals",
            "research_db": qualification.TARGET.parent.parent,
        },
        {},
    )
    plan = qualification.checked_metadata(admission_path, admission_sha)
    admissions = pilot.load_admission(admission_path, admission_sha, scope, layout)
    identity = {
        cik: qualification.checked_metadata(
            Path(item["identity_evidence"]["path"]), item["identity_evidence"]["sha256"]
        )
        for cik, item in plan["issuers"].items()
    }
    with (QUALIFICATION_DIR / "f01_sec_bulk_pilot_accounting.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        accounting = {(row["symbol"], row["session"]): row for row in csv.DictReader(stream)}
    compared = {(row["symbol"], row["session"]): row for row in comparison["legacy_comparison"]}
    return {
        "candidate": candidate,
        "additive_script_sha256": additive_hash,
        "report": report,
        "scope": scope,
        "layout": layout,
        "admissions": admissions,
        "identity": identity,
        "accounting": accounting,
        "compared": compared,
        "legacy_binding": comparison["legacy_binding"],
    }


def case_flows(symbol, session):
    if symbol == "LLY":
        return LLY_FLOWS
    if symbol == "CAT" and session.year == 2020:
        return ("operating_cash_flow",)
    if symbol == "COST" and session.year <= 2023:
        return ("net_income", "operating_cash_flow") if session.year == 2020 else ("net_income",)
    return ()


def relevant_session(symbol, session, baseline):
    if symbol in {"ACN", "LLY"} or case_flows(symbol, session):
        return True
    accounting = baseline.get("accounting_differences_canonical_then_legacy", {})
    return symbol in {"AAPL", "MSFT"} and any(
        "total_debt" in accounting.get(period, {}) for period in ("balance", "prior_balance")
    )


def fact_json(fact):
    return fact.model_dump(mode="json")


def quarters_trace(facts, metric, as_of):
    quarters = discrete_quarters(facts, metric, as_of)
    recent = [q for q in quarters if q.unit == quarters[-1].unit][-4:] if quarters else []
    value, period_end, filed = ttm_value(facts, metric, as_of)
    ends = [row.period_end for row in recent]
    gaps = [(b - a).days for a, b in zip(ends, ends[1:], strict=False)]
    candidates = [f for f in facts if f.metric == metric and f.filed <= as_of and f.period_start]
    cut = as_of - timedelta(days=570)
    return {
        "ttm": value,
        "period_end": period_end,
        "available_filing_date": filed,
        "gate": (
            "OK"
            if value is not None
            else "NO_DISCRETE_QUARTERS"
            if not recent
            else "FEWER_THAN_FOUR_SAME_UNIT"
            if len(recent) < 4
            else "NONCONTIGUOUS_QUARTERS"
        ),
        "gaps_in_days": gaps,
        "same_unit_last_four": [
            {
                "period_end": row.period_end,
                "filed": row.filed,
                "value": row.value,
                "unit": row.unit,
                "formula": row.formula,
                "inputs": [fact_json(f) for f in row.sources],
            }
            for row in recent
        ],
        "all_recent_discrete_quarter_ends": sorted(
            {q.period_end for q in quarters if q.period_end >= cut}
        ),
        "recent_available_periods_by_tag": {
            tag: sorted(
                {
                    (f.period_start, f.period_end, f.unit)
                    for f in candidates
                    if f.tag == tag and f.period_end >= cut
                }
            )
            for tag in sorted({f.tag for f in candidates})
        },
        "selected_period_competitors": [
            fact_json(f)
            for f in candidates
            if f.period_end in ends
            and any(
                (f.period_start, f.period_end, f.filed, f.unit)
                == (source.period_start, source.period_end, source.filed, source.unit)
                and f.unit == source.unit
                and f != source
                for row in recent
                for source in row.sources
            )
        ],
    }


def debt_trace(facts, as_of, cutoff=None):
    value, sources = debt_as_of(facts, as_of, end_on_or_before=cutoff)
    candidates = [
        f
        for f in facts
        if f.metric in DEBT_METRICS
        and f.filed <= as_of
        and (cutoff is None or f.period_end <= cutoff)
    ]
    recent_ends = sorted({f.period_end for f in candidates})[-2:]
    return {
        "value": value,
        "period_cutoff": cutoff,
        "selected": [fact_json(f) for f in sources],
        "formula": (
            "CURRENT_PLUS_NONCURRENT"
            if len(sources) == 2
            else "DIRECT_COMBINED_CONCEPT"
            if sources
            else "NO_ELIGIBLE_TOTAL"
        ),
        "candidates_in_reader_order": [
            fact_json(f)
            for f in candidates
            if f.period_end in recent_ends or any(f.period_end == s.period_end for s in sources)
        ],
    }


def shares_trace(facts, as_of):
    selected = shares_outstanding_as_of(facts, as_of)
    return {
        "selected": fact_json(selected) if selected else None,
        "max_age_days": MAX_SHARES_OUTSTANDING_AGE.days,
        "filed_candidates": [
            fact_json(f)
            | {
                "period_age_days": (as_of - f.period_end).days,
                "filing_age_days": (as_of - f.filed).days,
                "unit_and_positive": f.unit == "shares" and f.value > 0,
            }
            for f in facts
            if f.metric == "shares_outstanding" and f.filed <= as_of
        ],
    }


def trace(symbol, facts, session, config, *, accounting_phase):
    state = accounting_state_as_of(facts, session)
    result = {
        "flows": {
            metric: quarters_trace(facts, metric, session) for metric in case_flows(symbol, session)
        }
    }
    if symbol in {"AAPL", "MSFT"} or symbol == "ACN" and session == ACN_DEBT:
        result["debt"] = debt_trace(facts, session)
        if state.balance.period_end:
            cutoff = state.balance.period_end - timedelta(days=300)
            result["prior_debt"] = debt_trace(facts, session, cutoff)
    if symbol == "ACN":
        result["shares"] = shares_trace(facts, session)
    if accounting_phase:
        observed = qualification.accounting(facts, session, config)
        result["accounting"] = {
            key: observed[key]
            for key in (
                "current_ttm",
                "prior_ttm",
                "balance",
                "prior_balance",
                "metrics",
                "selected_shares",
                "quality_without_peer_context",
            )
        }
    return result


def concept_catalog(payload, symbol, parsed, admitted):
    aliases = {pair: metric for metric, pairs in TAG_ALIASES.items() for pair in pairs}
    normalized = Counter((f.taxonomy, f.tag) for f in parsed)
    allowed = Counter((f.taxonomy, f.tag) for f in admitted)
    pattern = re.compile(CONCEPTS[symbol], re.IGNORECASE)
    result = []
    for taxonomy, tags in sorted(payload["facts"].items()):
        for tag, concept in sorted(tags.items()):
            key = (taxonomy, tag)
            if not pattern.search(tag):
                continue
            units = {}
            for unit, rows in sorted(concept["units"].items()):
                dates = [row["filed"] for row in rows if row.get("filed")]
                periods = [row["end"] for row in rows if row.get("end")]
                units[unit] = {
                    "observations": len(rows),
                    "first_filed": min(dates, default=None),
                    "last_filed": max(dates, default=None),
                    "last_period_end": max(periods, default=None),
                    "observations_with_period_end_2020_or_later": sum(
                        day >= "2020-01-01" for day in periods
                    ),
                    "forms": dict(Counter(row.get("form", "") for row in rows)),
                }
            result.append(
                {
                    "taxonomy": taxonomy,
                    "tag": tag,
                    "label": concept.get("label"),
                    "description": concept.get("description"),
                    "units": units,
                    "mapped_metric": aliases.get(key),
                    "normalized": normalized[key],
                    "admitted": allowed[key],
                }
            )
    return result


def targeted_raw(payload, symbol, tags, *, start, end):
    """Keep raw keys and values for explicit concepts, bounded by economic period end."""
    return [
        {"taxonomy": taxonomy, "tag": tag, "unit": unit, **item}
        for taxonomy, concepts in sorted(payload["facts"].items())
        for tag in tags
        if tag in concepts and re.search(CONCEPTS[symbol], tag, re.IGNORECASE)
        for unit, rows in sorted(concepts[tag]["units"].items())
        for item in rows
        if start <= item.get("end", "") <= end
    ]


def filing_witnesses(cik, filings, submissions, history, accessions):
    documents, periods = qualification.filing_documents(submissions, history)
    witnesses = {}
    for acc in sorted(accessions):
        if not pilot.ACCESSION.fullmatch(acc):
            continue
        row = filings.get(acc)
        doc = next(iter(documents[acc])) if len(documents[acc]) == 1 else None
        prefix = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"
        witnesses[acc] = {
            "filing_date": row[0] if row else None,
            "form": row[1] if row else None,
            "accepted": row[2] if row else None,
            "report_dates": sorted(periods[acc]),
            "index_url": f"{prefix}{acc}-index.html",
            "primary_document_url": (
                prefix + doc if doc and qualification.FILENAME.fullmatch(doc) else None
            ),
        }
    return witnesses


def verify_reconstruction(observed, qualified_row):
    for key, value in observed["accounting"].items():
        sealed = qualified_row[key]
        if (json.loads(sealed) if sealed else None) != value:
            return False
    return True


def probe_sessions(bundle, symbol, facts, config):
    probes = []
    for (issuer, day), baseline in sorted(bundle["compared"].items()):
        session = date.fromisoformat(day)
        if issuer != symbol or not relevant_session(symbol, session, baseline):
            continue
        sealed = bundle["accounting"][symbol, day]
        reconstructed = trace(symbol, facts, session, config, accounting_phase=True)
        require(
            verify_reconstruction(reconstructed, sealed),
            f"SOURCE_RECONSTRUCTION_DISAGREES_WITH_QUALIFICATION: {symbol}:{day}",
        )
        probes.append(
            {
                "session": day,
                "sealed_legacy_differences": {
                    field: baseline.get(field)
                    for field in (
                        "accounting_differences_canonical_then_legacy",
                        "metric_differences_canonical_then_legacy",
                        "canonical_facts_selected",
                        "legacy_facts_selected",
                    )
                },
                "canonical_from_verified_members": reconstructed,
                "reconstructed_output_matches_qualification": True,
            }
        )
        if symbol == "COST":
            parent = [
                fact
                for fact in facts
                if fact.metric == "net_income" and fact.tag == "NetIncomeLoss"
            ]
            probes[-1]["parent_only_counterfactual"] = quarters_trace(parent, "net_income", session)
    return probes


def source_diagnostics(bundle, config):
    scope = bundle["scope"]
    _, paths, excluded = pilot.source_inputs(bundle["layout"], scope)
    issuers = [item for item in scope.active if item["symbol"] in CONCEPTS]
    results = {}
    with pinned_files(paths):
        sources = pilot.submissions_members(paths, issuers, excluded["submissions"])
        with zipfile.ZipFile(paths["companyfacts"]) as companyfacts:
            for issuer in issuers:
                cik, symbol = issuer["cik"], issuer["symbol"]
                raw, sha = pilot.zip_member(
                    companyfacts,
                    issuer["companyfacts_member"],
                    "companyfacts",
                    excluded["companyfacts"],
                )
                submissions, submissions_sha, history = sources[cik]
                expected = bundle["identity"][cik]["sources"]
                require(
                    pilot.provider_cik(raw) == cik
                    and sha == expected["companyfacts"]["member_sha256"]
                    and submissions_sha == expected["submissions"]["member_sha256"]
                    and {row["member"]: row["sha256"] for row in history}
                    == {
                        row["member"]: row["member_sha256"]
                        for row in expected["submissions"]["history_members"]
                    },
                    f"SOURCE_MEMBER_OR_CIK_BINDING_MISMATCH: {symbol}",
                )
                filings, conflicts, _ = pilot.filing_index(submissions, history)
                _, rules = bundle["admissions"][cik]
                admitted, counts, exclusions, exceptions = pilot.classify_facts(
                    raw, symbol, rules, filings, conflicts
                )
                admitted.sort(key=lambda fact: (fact.filed, fact.period_end))
                parsed = parse_company_facts(raw, symbol, preserve_observations=True)
                sealed = next(row for row in bundle["report"]["issuers"] if row["cik"] == cik)
                require(
                    counts["admitted_facts"]
                    == sealed["admitted_facts"]
                    == sealed["imported_occurrences"]
                    and sealed["duplicate_occurrence_reduction"] == 0,
                    f"SOURCE_RECONSTRUCTION_COVERAGE_MISMATCH: {symbol}",
                )
                probes = probe_sessions(bundle, symbol, admitted, config)
                tags = sorted(
                    UNMAPPED_WITNESSES.get(symbol, set())
                    | {
                        tag
                        for pairs in TAG_ALIASES.values()
                        for _, tag in pairs
                        if re.search(CONCEPTS[symbol], tag, re.IGNORECASE)
                    }
                )
                windows = {
                    "ACN": ("2023-01-01", "2024-06-30"),
                    "CAT": ("2017-01-01", "2020-12-31"),
                    "COST": ("2017-01-01", "2023-12-31"),
                    "LLY": ("2023-01-01", "2026-11-20"),
                    "AAPL": ("2021-01-01", "2026-11-20"),
                    "MSFT": ("2017-01-01", "2020-12-31"),
                }
                start, end = windows[symbol]
                result = {
                    "cik": cik,
                    "companyfacts_member_sha256": sha,
                    "submissions_member_sha256": submissions_sha,
                    "scope": "CURRENT_PROVIDER_RECONSTRUCTION",
                    "raw_concept_catalog": concept_catalog(raw, symbol, parsed, admitted),
                    "gross_exclusion_counts": exclusions,
                    "relevant_rejected_observations": [
                        record
                        for record in exceptions
                        if re.search(
                            CONCEPTS[symbol],
                            (record.get("observation") or record.get("key", {})).get("tag", ""),
                            re.IGNORECASE,
                        )
                    ],
                    "raw_witness_window": [start, end],
                    "raw_witness_observations": targeted_raw(
                        raw, symbol, tags, start=start, end=end
                    ),
                    "probes": probes,
                }
                witness_ids = {
                    row["accn"] for row in result["raw_witness_observations"] if row.get("accn")
                }
                witness_ids.update(
                    f.accession_number
                    for f in admitted
                    if f.accession_number and (f.metric == "shares_outstanding" and symbol == "ACN")
                )
                result["filing_witnesses"] = filing_witnesses(
                    cik, filings, submissions, history, witness_ids
                )
                results[symbol] = result
    return results


def legacy_matches(connection, cik, symbol):
    row = connection.execute("SELECT cik FROM companies WHERE symbol=?", (symbol,)).fetchone()
    return row is not None and row[0] == cik


def trace_selected(trace_result):
    selected = {}
    for metric, entry in trace_result["flows"].items():
        selected[metric] = [
            source for row in entry["same_unit_last_four"] for source in row["inputs"]
        ]
    for key in ("debt", "prior_debt"):
        if key in trace_result:
            selected[key] = trace_result[key]["selected"]
    shares = trace_result.get("shares", {}).get("selected")
    if shares is not None:
        selected["shares"] = [shares]
    return selected


def compare_inputs(selected, other_facts):
    keys = (
        "cik",
        "taxonomy",
        "tag",
        "unit",
        "period_start",
        "period_end",
        "filed",
        "accession_number",
        "frame",
    )
    other = {}
    for fact in other_facts:
        row = fact_json(fact)
        key = tuple(row[field] for field in keys)
        other.setdefault(key, set()).add((row["metric"], row["value"]))
    return {
        metric: [
            {
                "selected_fact": source,
                "same_source_observation_in_other_reader": [
                    {"normalized_metric": normalized_metric, "value": value}
                    for normalized_metric, value in sorted(
                        other.get(tuple(source[field] for field in keys), ())
                    )
                ],
            }
            for source in facts
        ]
        for metric, facts in selected.items()
    }


def database_diagnostics(bundle, config, sources, legacy_path):
    target = qualification.TARGET
    legacy = bundle["legacy_binding"]
    external, manifest = pilot.bootstrap_target(bundle["layout"], target)
    require(
        legacy_path.is_absolute()
        and str(legacy_path) == legacy["path"]
        and legacy_path.resolve(strict=True) == legacy_path,
        "SEALED_LEGACY_PATH_REQUIRED",
    )
    bulk.directory(legacy_path.parent)
    with ExitStack() as stack:
        pins = stack.enter_context(pinned_files({"research": target, "legacy": legacy_path}))
        for role, path in (("research", target), ("legacy", legacy_path)):
            pin = pins[role]
            info = os.fstat(pin.fileno())
            require(os.path.samestat(bulk.plain(path), info), "TARGET_FILE_REPLACED")
            qualification.no_sidecars(path)
            qualification.sqlite_header(pin, allow_closed_wal=role == "legacy")
            if role == "research":
                expected_identity = bundle["report"]["binding"]["target_file_identity"]
                size = bundle["report"]["database_bytes_after"]
            else:
                expected_identity = legacy["file_identity"]
                size = legacy["size_bytes"]
                require(info.st_mtime_ns == legacy["mtime_ns"], "SEALED_LEGACY_MTIME_MISMATCH")
            require(
                [info.st_dev, info.st_ino] == expected_identity and info.st_size == size,
                f"SEALED_FILE_BINDING_MISMATCH: {role}",
            )
        forward = stack.enter_context(LocalInputs(target, capture_provenance=False).snapshot())
        connection = stack.enter_context(forward.connect())
        old = stack.enter_context(qualification.legacy_connection(legacy_path))
        canonical_reader, legacy_reader = Database(target), Database(legacy_path)
        for issuer, envelope, _, _ in qualification.read_snapshot(
            connection,
            bundle["report"],
            bundle["identity"],
            bundle["admissions"],
            external,
            manifest,
        ):
            cik, symbol = issuer["cik"], issuer["symbol"]
            if symbol not in sources:
                continue
            errors = []
            qualification.check_facts(forward, connection, issuer, envelope, errors)
            require(
                not errors and legacy_matches(old, cik, symbol), f"READER_BINDING_ERROR: {symbol}"
            )
            for probe in sources[symbol]["probes"]:
                session = date.fromisoformat(probe["session"])
                canonical = canonical_reader.facts_available_as_of(
                    symbol, session, connection=connection
                )
                previous = legacy_reader.facts_available_as_of(symbol, session, connection=old)
                require(
                    all(f.cik == cik for f in canonical + previous),
                    f"READER_RETURNED_MIXED_CIK: {symbol}:{session}",
                )
                left = trace(symbol, canonical, session, config, accounting_phase=True)
                require(
                    qualification.json_data(left)
                    == qualification.json_data(probe["canonical_from_verified_members"]),
                    f"ACTUAL_CANONICAL_TRACE_MISMATCH: {symbol}:{session}",
                )
                actual = qualification.comparison(canonical, previous, session, config)
                require(
                    all(
                        actual[field] == value
                        for field, value in probe["sealed_legacy_differences"].items()
                    ),
                    f"ACTUAL_LEGACY_DIFFERENCE_MISMATCH: {symbol}:{session}",
                )
                probe["actual_legacy_reader_trace"] = trace(
                    symbol, previous, session, config, accounting_phase=True
                )
                probe["selected_inputs_crosscheck"] = {
                    "canonical_in_legacy": compare_inputs(trace_selected(left), previous),
                    "legacy_in_canonical": compare_inputs(
                        trace_selected(probe["actual_legacy_reader_trace"]), canonical
                    ),
                }
                probe["actual_canonical_and_legacy_differences_match_qualification"] = True
        qualification.no_sidecars(target)
        qualification.no_sidecars(legacy_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New diagnostic JSON file")
    parser.add_argument("--legacy-db", type=Path, help="Exactly the sealed Legacy reference path")
    parser.add_argument("--authorize-read-only-databases", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.legacy_db) != args.authorize_read_only_databases:
        parser.error("use --legacy-db and --authorize-read-only-databases together")
    try:
        path = args.output
        require(
            path.is_absolute()
            and path.suffix == ".json"
            and ":" not in path.name
            and not bulk.exists(path),
            "NEW_ABSOLUTE_JSON_OUTPUT_REQUIRED",
        )
        require(
            path.parent == ARTIFACTS or path.parent == pilot.REPO,
            "OUTPUT_PARENT_MUST_BE_RESEARCH_ARTIFACTS_OR_WORKSPACE",
        )
        bulk.directory(path.parent)
        bundle = evidence()
        config = load_strategy_config(pilot.REPO / "config" / "strategy.yaml")
        cases = source_diagnostics(bundle, config)
        if args.authorize_read_only_databases:
            database_diagnostics(bundle, config, cases, args.legacy_db)
        result = {
            "contract": "F01_FUNDAMENTAL_DISCREPANCY_AUDIT_V1",
            "qualification_sha256": QUALIFICATION_SHA,
            "legacy_comparison_sha256": LEGACY_SHA,
            "source_manifest_sha256": pilot.SOURCE_SHA,
            "qualification_fingerprint": QUALIFICATION_CODE,
            "candidate": bundle["candidate"],
            "additive_script_sha256": bundle["additive_script_sha256"],
            "databases_opened_read_only": args.authorize_read_only_databases,
            "original_filing_context_verified": False,
            "historical_pit_qualified": False,
            "legacy_file_binding_from_qualification": bundle["legacy_binding"],
            "cases": cases,
        }
        stage = path.parent / f".fundamental-audit-{uuid4().hex[:10]}.incomplete"
        bulk.write_new(stage, (qualification.json_data(result) + "\n").encode("utf-8"))
        stage.rename(path)  # Windows same-volume no-replace publication.
        print(
            qualification.json_data(
                {
                    "report": str(path),
                    "probes": {symbol: len(value["probes"]) for symbol, value in cases.items()},
                    "databases_opened_read_only": args.authorize_read_only_databases,
                }
            )
        )
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, sqlite3.Error) as error:
        print(f"AUDIT_BLOCKED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
