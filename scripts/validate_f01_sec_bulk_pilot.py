"""Qualify the sealed seven-issuer Canonical research pilot without writing SQLite."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import ExitStack, closing, contextmanager
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

sys.dont_write_bytecode = True
import import_f01_sec_bulk_pilot as pilot  # noqa: E402

from trading_system.backtest.features import _eligible_fact_count  # noqa: E402
from trading_system.backtest.screen_strategies import SCREEN_STRATEGY_DEFINITIONS  # noqa: E402
from trading_system.config import load_strategy_config  # noqa: E402
from trading_system.data.canonical_facts import (  # noqa: E402
    KEY,
    MUTABLE,
    fingerprint,
    require_contract,
)
from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.database import Database, _fact_rows  # noqa: E402
from trading_system.data.market_sessions import (  # noqa: E402
    regular_session_bounds,
    trading_sessions_between,
)
from trading_system.data.sec_persistence import (  # noqa: E402
    CanonicalSecPersistence,
    SecRequestArchive,
    digest,
    json_bytes,
)
from trading_system.data.security_reference import SECURITY_TYPES  # noqa: E402
from trading_system.data.universe import is_financial_or_reit, is_reit  # noqa: E402
from trading_system.fundamentals.metrics import (  # noqa: E402
    FLOW_METRICS,
    INSTANT_METRICS,
    discrete_quarters,
    shares_outstanding_as_of,
)
from trading_system.fundamentals.quality import (  # noqa: E402
    accounting_state_as_of,
    attach_market_price,
)
from trading_system.shadow.inputs import LocalInputs  # noqa: E402
from trading_system.strategy.scoring import score_quality, score_valuation  # noqa: E402

bulk = pilot.bulk
require = pilot.require
ARTIFACTS = pilot.RESEARCH_ROOT / "import-artifacts"
DATASET_ID = "f01-research-adb7d5d14b5b4cf889c8a8e5e63fa185"
TARGET = (
    pilot.RESEARCH_ROOT
    / "research-databases"
    / "f01-sec-isolated-research-pilot01"
    / "f01-sec-candidate.sqlite3"
)
RESULT = ARTIFACTS / f"f01-pilot-{DATASET_ID}" / "result"
REPORT_SHA = "6092944e6adec3ecf30878dd57ab5b9915bb000e97c1e697ff72b9381d079805"
IMPORT_CODE = "c34dd3e28a33b132e171ec39300721002b81de3e44cb7ea88b90164935cef0dc"
ADDED_SCRIPT = "scripts/validate_f01_sec_bulk_pilot.py"
CONTRACT = "F01_SEC_BULK_PILOT_QUALIFICATION_V1"
COUNTS = {
    "canonical_facts": 26102,
    "imported_occurrences": 26102,
    "revisions": 26102,
    "source_memberships": 26102,
}
PHASES = ("coverage", "accounting", "pit", "universe")
YEARS = range(2020, 2027)
COVERAGE_END = date(2026, 11, 20)
ANCHORS = (
    date(2020, 1, 2),
    *(
        date(year, month, day)
        for year in YEARS
        for month, day in ((6, 30), (12, 31))
        if date(year, month, day) <= COVERAGE_END
    ),
    COVERAGE_END,
)
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}
CORE_FLOWS = ("revenue", "net_income", "operating_cash_flow", "eps_diluted")
COMPARE = set(CORE_FLOWS) | {"cash", "total_assets", "total_debt", "shares_outstanding"}
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")


class QualificationBlocked(RuntimeError):
    """An operational prerequisite prevents a safe read-only check."""


def checked_metadata(path, expected_sha):
    raw, content = bulk.read_metadata(path, max_bytes=bulk.MAX_MANIFEST_BYTES)
    require(pilot.sha(raw) == expected_sha, f"METADATA_SHA256_MISMATCH: {path.name}")
    return content


def preflight():
    """Only check small artifacts and repository inventory, without opening SQLite."""
    candidate = pilot.code_binding()
    repository = pilot.repository_evidence(pilot.REPO)
    files = dict(repository["files"])
    added = files.pop(ADDED_SCRIPT)
    historical = fingerprint(
        [files, repository["runtime_packages"], sys.version, sqlite3.sqlite_version]
    )
    require(
        historical == IMPORT_CODE and candidate["fingerprint"] == repository["code_fingerprint"],
        "PILOT_IMPORT_CODE_BINDING_MISMATCH",
    )
    scope = pilot.pilot_spec()
    report = checked_metadata(RESULT / pilot.REPORT, REPORT_SHA)
    binding = report["binding"]
    _, stored = bulk.read_metadata(RESULT.parent / "binding.json")
    require(
        stored == binding
        and report["contract"] == pilot.IMPORT_CONTRACT
        and report["status"] == pilot.IMPORTED
        and report["source_quality"] == bulk.QUALITY
        and report["dataset_version"] == 8
        and report["expected_receipts"] == report["confirmed_receipts"] == len(scope.active) == 7
        and len(binding["requests"]) == 7
        and len({row[0] for row in binding["requests"]}) == 7
        and binding["dataset_id"] == DATASET_ID
        and binding["target"] == str(TARGET)
        and binding["generation"] == pilot.GENERATION
        and binding["candidate"]["fingerprint"] == historical
        and binding["manifest_sha256"] == pilot.SOURCE_SHA
        and binding["pilot_spec_sha256"] == pilot.PILOT_SHA
        and binding["source_pilot_spec_sha256"] == pilot.SOURCE_PILOT_SHA
        and binding["active_issuers"]
        == [{"cik": item["cik"], "symbol": item["symbol"]} for item in scope.active]
        and binding["excluded_issuers"] == scope.excluded == report["excluded_issuers"]
        and all(report["totals"][key] == value for key, value in COUNTS.items()),
        "SEALED_PILOT_BINDING_MISMATCH",
    )
    require(
        set(report["artifact_sha256"]) == {pilot.STATS, pilot.AUDIT, pilot.QUARANTINE},
        "SEALED_PILOT_ARTIFACT_SET_MISMATCH",
    )
    for name, expected in report["artifact_sha256"].items():
        path = RESULT / name
        require(
            bulk.plain(path).st_size < bulk.MAX_MANIFEST_BYTES and pilot.file_sha(path) == expected,
            f"SEALED_PILOT_ARTIFACT_MISMATCH: {name}",
        )
    source = checked_metadata(
        pilot.RESEARCH_ROOT / "sec-originals" / pilot.GENERATION / bulk.MANIFEST_NAME,
        pilot.SOURCE_SHA,
    )
    require(
        source["generation"] == pilot.GENERATION
        and source["quality"] == bulk.QUALITY
        and source["contract"] == bulk.MANIFEST_CONTRACT,
        "PILOT_SOURCE_GENERATION_MISMATCH",
    )
    admission_path = ARTIFACTS / "f01-pilot-admission-reviewed-v2.json"
    admissions = pilot.load_admission(
        admission_path,
        binding["admission_sha256"],
        scope,
        bulk.Layout({"import_artifacts": ARTIFACTS}, {}),
    )
    plan = checked_metadata(admission_path, binding["admission_sha256"])
    identity = {
        cik: checked_metadata(
            Path(record["identity_evidence"]["path"]), record["identity_evidence"]["sha256"]
        )
        for cik, record in plan["issuers"].items()
    }
    return (
        report,
        admissions,
        identity,
        {
            "contract": CONTRACT,
            "input_report_sha256": REPORT_SHA,
            "historical_import_candidate": binding["candidate"],
            "qualification_candidate": candidate,
            "additive_files": {ADDED_SCRIPT: added},
            "fingerprint_inventory": {
                "historical_files": len(files),
                "qualification_files": len(files) + 1,
                "runtime_packages": len(repository["runtime_packages"]),
            },
            "source_archive_verification": "SEALED_MANIFEST_BINDING_NO_ZIP_RESCAN",
            "snapshot": binding,
        },
    )


def no_sidecars(path):
    if any(bulk.exists(sidecar) for sidecar in pilot.sqlite_files(path)):
        raise QualificationBlocked(f"SQLITE_SIDECAR_PRESENT: {path}")


def sqlite_header(pin, *, allow_closed_wal=False):
    header = pin.read(100)
    require(header[:16] == b"SQLite format 3\x00", "INVALID_SQLITE_HEADER")
    if header[18:20] == b"\x02\x02" and allow_closed_wal:
        return  # The legacy reader uses immutable mode, with no WAL or sidecars under the pin.
    if header[18:20] != b"\x01\x01":
        raise QualificationBlocked("QUIESCENT_ROLLBACK_JOURNAL_DATABASE_REQUIRED")


@contextmanager
def legacy_connection(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        names = dict(db.execute("SELECT name,type FROM sqlite_master"))
        require(
            names.get("fundamental_facts") == "table"
            and not {"fact_contract", "canonical_migration"} & names.keys(),
            "AUTHORIZED_INPUT_IS_NOT_LEGACY",
        )
        yield db


def read_snapshot(connection, report, identity, admissions, external, manifest):
    require_contract(connection, writer=False)
    binding = report["binding"]
    version = connection.execute(
        "SELECT version,schema_hash,current_version FROM fact_contract WHERE singleton=1"
    ).fetchone()
    origin = connection.execute(
        "SELECT dataset_id,source_hash FROM fact_dataset WHERE version=1"
    ).fetchone()
    lineage = external["lineage"]
    stored = connection.execute("SELECT evidence,manifest FROM research_bootstrap").fetchone()
    require(
        tuple(version) == (pilot.CONTRACT, binding["schema_fingerprint"], 8)
        and tuple(origin) == (DATASET_ID, binding["bootstrap_manifest_sha256"])
        and stored
        and json.loads(stored[0]) == lineage
        and stored[1] == manifest
        and lineage["code_fingerprint"] == binding["bootstrap_code_fingerprint"]
        and external["database_sha256_at_bootstrap"] == binding["bootstrap_database_sha256"],
        "DATABASE_RESEARCH_LINEAGE_MISMATCH",
    )
    expected_receipts = [(pair[0], index) for index, pair in enumerate(binding["requests"], 2)]
    require(
        [
            tuple(row)
            for row in connection.execute(
                "SELECT request_id,version FROM ingestion_receipt ORDER BY version"
            )
        ]
        == expected_receipts
        and [
            tuple(row)
            for row in connection.execute(
                "SELECT dataset_id,version FROM fact_dataset WHERE version>1 ORDER BY version"
            )
        ]
        == expected_receipts
        and connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        and connection.execute("PRAGMA foreign_key_check").fetchone() is None,
        "DATABASE_INTEGRITY_OR_RECEIPT_SEQUENCE_MISMATCH",
    )
    table_counts = {
        "canonical_facts": "SELECT COUNT(*) FROM canonical_fact",
        "imported_occurrences": "SELECT COUNT(*) FROM source_occurrence",
        "revisions": "SELECT COUNT(*) FROM fact_revision",
        "source_memberships": """SELECT COUNT(*) FROM (
            SELECT DISTINCT fact_id,symbol,taxonomy FROM source_occurrence)""",
    }
    require(
        {key: connection.execute(sql).fetchone()[0] for key, sql in table_counts.items()} == COUNTS,
        "DATABASE_PILOT_COUNTS_MISMATCH",
    )
    require(
        [tuple(row) for row in connection.execute("SELECT cik,symbol FROM companies ORDER BY cik")]
        == [(item["cik"], item["symbol"]) for item in binding["active_issuers"]],
        "DATABASE_COMPANY_SCOPE_MISMATCH",
    )
    namespace = digest(
        json_bytes(
            [
                str(TARGET.resolve()),
                binding["target_file_identity"],
                tuple(version[:2]),
                tuple(origin),
            ]
        )
    )
    archive_path = TARGET.with_name(TARGET.name + ".sec-requests") / namespace
    bulk.directory(archive_path)
    archive = SecRequestArchive(archive_path)
    backend = CanonicalSecPersistence(TARGET, archive)
    for index, (issuer, pair) in enumerate(
        zip(binding["active_issuers"], binding["requests"], strict=True), 2
    ):
        cik, symbol = issuer["cik"], issuer["symbol"]
        request_id, input_hash = pair
        bulk.plain(archive.directory / f"{request_id}.json")
        envelope = archive.load(request_id)
        receipt = backend._receipt(connection, envelope)
        expected_identity, rules = admissions[cik]
        require(
            envelope.input_hash == input_hash
            and (envelope.company.cik, envelope.company.symbol) == (cik, symbol)
            and envelope.identity == expected_identity
            and all(
                rules[taxonomy] == rule and source_symbol == symbol
                for (source_symbol, taxonomy), rule in envelope.admission_map().items()
            )
            and receipt
            and receipt.dataset_version == index
            and receipt.replayed,
            f"REQUEST_RECEIPT_BINDING_MISMATCH: {symbol}",
        )
        payload = json.loads(envelope.payload_json)
        submissions = payload["submissions"]
        provenance = submissions["_f01_bulk_evidence"]
        expected_sources = identity[cik]["sources"]
        require(
            pilot.provider_cik(submissions) == cik
            and pilot.provider_cik(payload["companyfacts"]) == cik
            and provenance["manifest_sha256"] == pilot.SOURCE_SHA
            and provenance["source_quality"] == bulk.QUALITY
            and pilot.zone_time(provenance["companyfacts_retrieved_at"]) == envelope.retrieved_at
            and provenance["companyfacts_member_sha256"]
            == expected_sources["companyfacts"]["member_sha256"]
            and provenance["submissions_member_sha256"]
            == expected_sources["submissions"]["member_sha256"]
            and {item["member"]: item["sha256"] for item in provenance["history"]}
            == {
                item["member"]: item["member_sha256"]
                for item in expected_sources["submissions"]["history_members"]
            },
            f"REQUEST_SOURCE_BINDING_MISMATCH: {symbol}",
        )
        yield issuer, envelope, submissions, provenance


def check_facts(reader, connection, issuer, envelope, errors):
    """Compare the physical forward projection with the sealed normalized request."""
    full = reader.facts_available_as_of(issuer["symbol"], date.max)
    if Counter(_fact_rows(full)) != Counter(_fact_rows(envelope.facts)):
        errors.append(issuer | {"issue": "FORWARD_PROJECTION_REQUEST_MISMATCH"})
    compatibility = Database(TARGET).facts_available_as_of(
        issuer["symbol"], date.max, connection=connection
    )
    if Counter(_fact_rows(compatibility)) != Counter(_fact_rows(envelope.facts)):
        errors.append(issuer | {"issue": "COMPATIBILITY_PROJECTION_REQUEST_MISMATCH"})
    return full


def filing_documents(submissions, history):
    documents, periods = defaultdict(set), defaultdict(set)
    for rows in (submissions["filings"]["recent"], *(part["payload"] for part in history)):
        accessions = rows.get("accessionNumber", [])
        for name, result in (("primaryDocument", documents), ("reportDate", periods)):
            values = rows.get(name)
            if not isinstance(values, list) or len(values) != len(accessions):
                continue
            for accession, value in zip(accessions, values, strict=True):
                if isinstance(value, str) and value:
                    result[accession].add(value)
    return documents, periods


def coverage(facts, filings, periods, fiscal_year_end):
    for year in YEARS:
        end = min(date(year, 12, 31), COVERAGE_END)
        rows = [fact for fact in facts if fact.period_end.year == year and fact.filed <= end]
        shares = [fact for fact in rows if fact.metric == "shares_outstanding"]
        annual, quarterly, amendments = (
            sorted(
                acc
                for acc, (filed, form, _) in filings.items()
                if filed.year == year and filed <= end and form in forms
            )
            for forms in ({"10-K"}, {"10-Q"}, {"10-K/A", "10-Q/A"})
        )
        fiscal_periods = {
            acc: sorted(periods[acc]) for acc in annual + quarterly + amendments if periods.get(acc)
        }
        reported_quarters = {period for acc in quarterly for period in fiscal_periods.get(acc, [])}
        expected = set(FLOW_METRICS) | set(INSTANT_METRICS) | {"shares_outstanding"}
        missing = sorted(expected - {fact.metric for fact in rows})
        quarters, gaps, filing_gaps = {}, {}, {}
        for metric in CORE_FLOWS:
            ordered = sorted(
                {q.period_end for q in discrete_quarters(facts, metric, end) if q.period_end <= end}
            )
            quarters[metric] = [day.isoformat() for day in ordered if day.year == year]
            gaps[metric] = [
                [a.isoformat(), b.isoformat()]
                for a, b in zip(ordered, ordered[1:], strict=False)
                if b.year == year and b - a > timedelta(days=130)
            ]
            filing_gaps[metric] = sorted(reported_quarters - {day.isoformat() for day in ordered})
        yield {
            "year": year,
            "window_end": end.isoformat(),
            "fiscal_year_end_mmdd": fiscal_year_end,
            "instant_facts": sum(f.period_start is None for f in rows),
            "duration_facts": sum(f.period_start is not None for f in rows),
            "numeric_zero_facts": sum(f.value == 0 for f in rows),
            "period_ends": sorted({f.period_end.isoformat() for f in rows}),
            "fiscal_labels": sorted({f"{f.fiscal_year}:{f.fiscal_period}" for f in rows}),
            "annual_accessions_by_filing_year": annual,
            "quarterly_accessions_by_filing_year": quarterly,
            "amendment_accessions_by_filing_year": amendments,
            "filing_report_periods": fiscal_periods,
            "reader_quarter_ends": quarters,
            "potential_missing_quarter_intervals": gaps,
            "reported_10q_periods_without_discrete_fact": filing_gaps,
            "period_ends_in_snapshot_including_later_filings": sorted(
                {f.period_end.isoformat() for f in facts if f.period_end.year == year}
            ),
            "shares_by_taxonomy": dict(Counter(f.taxonomy for f in shares)),
            "shares_usable_at_window_end": shares_outstanding_as_of(facts, end) is not None,
            "missing_metrics": missing,
            "evidence_status": "EVIDENCE_MISSING"
            if missing or any(gaps.values()) or any(filing_gaps.values()) or not rows
            else "SUITABILITY_DEMONSTRATED_FOR_COVERAGE",
        }


def accounting(facts, session, config):
    state = accounting_state_as_of(facts, session)
    metrics = attach_market_price(state, None)
    quality = score_quality(
        metrics,
        {},
        config.scores,
        min_available=config.data_quality.min_available_quality_metrics,
    )
    valuation = score_valuation(
        metrics,
        {},
        {},
        config.scores,
        min_available=config.data_quality.min_available_valuation_metrics,
    )
    shares = shares_outstanding_as_of(facts, session)
    share_candidates = [fact for fact in facts if fact.metric == "shares_outstanding"]
    core = {key: getattr(state.current, key) for key in CORE_FLOWS}
    core |= {"total_assets": state.balance.total_assets, "shares_outstanding": shares}
    return {
        "fact_count": len(facts),
        "current_ttm": state.current.model_dump(mode="json"),
        "prior_ttm": state.prior.model_dump(mode="json"),
        "balance": state.balance.model_dump(mode="json"),
        "prior_balance": state.prior_balance.model_dump(mode="json"),
        "metrics": metrics.model_dump(mode="json"),
        "selected_shares": shares.model_dump(mode="json") if shares is not None else None,
        "share_observations_by_taxonomy": dict(Counter(f.taxonomy for f in share_candidates)),
        "latest_share_period_by_taxonomy": {
            taxonomy: max(f.period_end for f in share_candidates if f.taxonomy == taxonomy)
            for taxonomy in sorted({fact.taxonomy for fact in share_candidates})
        },
        "share_reader_status": (
            "USABLE"
            if shares is not None
            else "PRESENT_BUT_NOT_USABLE"
            if share_candidates
            else "NO_OBSERVATIONS"
        ),
        "missing_core": [key for key, value in core.items() if value is None],
        "quality_without_peer_context": quality.model_dump(mode="json"),
        "valuation_without_market_or_peers": valuation.model_dump(mode="json"),
        "strategy_components": {
            definition.variant.value: definition.score_components
            for definition in SCREEN_STRATEGY_DEFINITIONS
        },
        "evidence_status": "EVIDENCE_MISSING"
        if any(value is None for value in core.values())
        else "SUITABILITY_DEMONSTRATED_FOR_CORE_ACCOUNTING",
        "ranking_status": "EVIDENCE_MISSING_MARKET_AND_PEERS",
    }


def pit_issues(facts, filings, conflicted, close):
    groups = defaultdict(set)
    issues = defaultdict(set)
    for fact in facts:
        acc = fact.accession_number
        if not acc or acc in conflicted or acc not in filings:
            issues["MISSING_OR_CONFLICTING_SUBMISSIONS_JOIN"].add(acc)
            continue
        filed, form, acceptance = filings[acc]
        if (filed, form) != (fact.filed, fact.form):
            issues["FACT_FILING_METADATA_MISMATCH"].add(acc)
        if acceptance is None:
            issues["MISSING_ACCEPTANCE"].add(acc)
        elif acceptance > close:
            issues["ACCEPTED_AFTER_SESSION_CLOSE"].add(acc)
        if form.endswith("/A"):
            issues["AMENDMENT_REQUIRES_VERSION_CHAIN"].add(acc)
        key = (fact.taxonomy, fact.tag, fact.unit, fact.period_start, fact.period_end)
        groups[key].add((fact.value, acc))
    for candidates in groups.values():
        if len({value for value, _ in candidates}) > 1:
            issues["COMPARATIVE_VALUES_REQUIRE_ORIGINAL_CONTEXT"].update(
                acc for _, acc in candidates
            )
    return {
        key: sorted(acc for acc in accessions if acc) for key, accessions in sorted(issues.items())
    }


def original_witnesses(cik, filings, documents, periods, facts, matrix):
    """Bounded collection queue. Dates and URLs derive exclusively from local evidence."""
    by_accession = defaultdict(set)
    for fact in facts:
        if fact.accession_number:
            by_accession[fact.accession_number].add(fact.metric)
    ordered = sorted(by_accession.keys() & filings.keys(), key=lambda acc: (filings[acc][0], acc))
    candidates = defaultdict(set)
    for label, rows in (
        ("WARMUP", [acc for acc in ordered if filings[acc][0].year < 2020]),
        ("FIRST_RESEARCH_FILING", [acc for acc in ordered if filings[acc][0].year >= 2020]),
        ("LATEST_ANNUAL", [acc for acc in ordered if filings[acc][1] == "10-K"]),
        ("LATEST_SHARES", [acc for acc in ordered if "shares_outstanding" in by_accession[acc]]),
        ("FIRST_AMENDMENT", [acc for acc in ordered if filings[acc][1].endswith("/A")]),
    ):
        if rows:
            candidates[rows[-1] if label.startswith(("LATEST", "WARMUP")) else rows[0]].add(label)
    for issue in (
        "ACCEPTED_AFTER_SESSION_CLOSE",
        "MISSING_ACCEPTANCE",
        "COMPARATIVE_VALUES_REQUIRE_ORIGINAL_CONTEXT",
    ):
        witnessed = {acc for row in matrix for acc in row["accessions_by_issue"].get(issue, [])}
        sample = [acc for acc in ordered if acc in witnessed][:2]
        for acc in sample:
            candidates[acc].add(issue)
    for acc in list(candidates):
        filed, form, _ = filings[acc]
        report_dates = periods[acc]
        if form.endswith("/A") and report_dates:
            originals = sorted(
                other
                for other, (other_date, other_form, _) in filings.items()
                if other_form == form[:-2]
                and other_date <= filed
                and periods.get(other, set()) & report_dates
            )
            for original in originals[:2]:
                candidates[original].add(f"CANDIDATE_ORIGINAL_FOR_{acc}")
    result = []
    for acc, purposes in sorted(
        candidates.items(), key=lambda pair: (filings[pair[0]][0], pair[0])
    ):
        filed, form, accepted = filings[acc]
        compact = acc.replace("-", "")
        path = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{compact}/"
        names = documents[acc]
        primary = next(iter(names)) if len(names) == 1 else None
        result.append(
            {
                "accession": acc,
                "filed": filed.isoformat(),
                "form": form,
                "accepted": accepted.isoformat() if accepted else None,
                "report_periods": sorted(periods[acc]),
                "purposes": sorted(purposes),
                "metrics": sorted(by_accession[acc]),
                "filing_index_url": path + acc + "-index.html",
                "primary_document_url": path + primary
                if primary and FILENAME.fullmatch(primary)
                else None,
                "retrieve_separately": [
                    "filing index and acceptance metadata",
                    "original iXBRL/XBRL",
                    "amendment and original accession chain where applicable",
                ],
            }
        )
    return result


def universe(issuer, envelope, evidence, config):
    catalog = evidence["identity_checks"]["catalog"]
    kind = catalog.get("security_type")
    kind = kind if kind in SECURITY_TYPES else "UNKNOWN"
    sic = envelope.company.sic
    financial = is_financial_or_reit(sic, exclude_reits=False)
    reit = is_reit(sic)
    return issuer | {
        "instrument_type": kind,
        "current_exchange": catalog.get("exchange"),
        "current_type_observed_at": catalog.get("reference_observed_at"),
        "current_lifecycle_state": catalog.get("lifecycle_state"),
        "instrument_scope": instrument_scope(kind, catalog.get("lifecycle_state")),
        "issuer_sic": sic,
        "issuer_is_financial": financial,
        "issuer_is_reit": reit,
        "research_admission": "REVIEWED_SEALED_PILOT",
        "static_daily_policy": (
            "EXCLUDED_CURRENT_FINANCIAL_OR_REIT_POLICY"
            if (financial and config.universe.exclude_financials)
            or (reit and config.universe.exclude_reits)
            else "POTENTIAL_COMMON_STOCK"
            if kind == "COMMON_STOCK"
            else "EVIDENCE_MISSING_OR_OTHER_TYPE"
        ),
        "historical_trading_admission": "EVIDENCE_MISSING",
        "missing": [
            "dated security-class and listing evidence",
            "symbol/CIK/security epochs",
            "historical universe with delistings",
            "qualified market prices and liquidity",
        ],
    }


def instrument_scope(kind, lifecycle):
    if lifecycle in {"DELISTED", "INACTIVE", "HISTORICAL"}:
        return "DATED_HISTORICAL_LISTING_EVIDENCE_REQUIRED"
    if kind == "COMMON_STOCK":
        return "COMMON_STOCK_CANDIDATE"
    if kind in {"ETF", "ETP", "ETN", "FUND"}:
        return "FUND_OR_EXCHANGE_TRADED_PRODUCT"
    if kind in {"WARRANT", "RIGHT", "PREFERRED_STOCK"}:
        return "NON_COMMON_EQUITY_INSTRUMENT"
    if kind == "INSURANCE_PRODUCT":
        return "INSURANCE_PRODUCT_REQUIRES_SEPARATE_EVIDENCE"
    return "OTHER_OR_UNPROVEN_INSTRUMENT"


def comparison(canonical, legacy, session, config):
    keys = ("taxonomy", *KEY, *MUTABLE)

    def values(rows):
        return Counter(
            tuple(getattr(fact, key) for key in keys) for fact in rows if fact.metric in COMPARE
        )

    a, b = values(canonical), values(legacy)
    left, right = accounting(canonical, session, config), accounting(legacy, session, config)
    metrics = {
        key: [value, right["metrics"][key]]
        for key, value in left["metrics"].items()
        if value != right["metrics"][key]
    }
    accounting_differences = {
        name: {
            key: [value, right[name][key]]
            for key, value in left[name].items()
            if value != right[name][key]
        }
        for name in ("current_ttm", "prior_ttm", "balance", "prior_balance")
    }
    if left["selected_shares"] != right["selected_shares"]:
        accounting_differences["selected_shares"] = [
            left["selected_shares"],
            right["selected_shares"],
        ]
    return {
        "status": "DIFFERENCES_OBSERVED"
        if a != b or metrics or any(accounting_differences.values())
        else ("SELECTED_INPUTS_MATCH" if a and b else "EVIDENCE_MISSING"),
        "canonical_facts_selected": a.total(),
        "legacy_facts_selected": b.total(),
        "canonical_only_count": (a - b).total(),
        "legacy_only_count": (b - a).total(),
        "canonical_only_examples": [list(item) for item in sorted(a - b, key=repr)[:8]],
        "legacy_only_examples": [list(item) for item in sorted(b - a, key=repr)[:8]],
        "selected_fact_columns": keys,
        "metric_differences_canonical_then_legacy": metrics,
        "accounting_differences_canonical_then_legacy": accounting_differences,
        "canonical_missing_core": left["missing_core"],
        "legacy_missing_core": right["missing_core"],
        "canonical_quality_factors": left["quality_without_peer_context"],
        "legacy_quality_factors": right["quality_without_peer_context"],
        "canonical_valuation_factors": left["valuation_without_market_or_peers"],
        "legacy_valuation_factors": right["valuation_without_market_or_peers"],
    }


def inspect_issuer(reader, connection, item, identity, phases, legacy, config, output):
    issuer, envelope, submissions, provenance = item
    cik, symbol = issuer["cik"], issuer["symbol"]
    facts = check_facts(reader, connection, issuer, envelope, output["data_errors"])
    history = provenance["history"]
    filings, conflicts, malformed = pilot.filing_index(submissions, history)
    documents, periods = filing_documents(submissions, history)
    if "coverage" in phases:
        output["coverage"].extend(
            issuer | row
            for row in coverage(facts, filings, periods, submissions.get("fiscalYearEnd"))
        )
        warmup = [fact for fact in facts if fact.filed < date(2020, 1, 1)]
        output["warmup"].append(
            issuer
            | {
                "facts_filed_before_2020": len(warmup),
                "period_ends_before_2018": len(
                    {f.period_end for f in warmup if f.period_end < date(2018, 1, 1)}
                ),
                "earliest_period": min((f.period_end for f in warmup), default=None),
                "earliest_filed": min((f.filed for f in warmup), default=None),
                "distinct_quarters_available_at_first_anchor": {
                    metric: len(
                        {q.period_end for q in discrete_quarters(warmup, metric, ANCHORS[0])}
                    )
                    for metric in CORE_FLOWS
                },
            }
        )
    if "universe" in phases:
        output["universe"].append(universe(issuer, envelope, identity[cik], config))
    if not set(phases) & {"accounting", "pit", "legacy"}:
        return
    legacy_cik = (
        legacy.execute("SELECT cik FROM companies WHERE symbol=?", (symbol,)).fetchone()
        if legacy is not None
        else None
    )
    matrix = []
    for anchor in ANCHORS:
        session = trading_sessions_between(anchor - timedelta(days=10), anchor)[-1]
        key = issuer | {"anchor": anchor.isoformat(), "session": session.isoformat()}
        current = reader.facts_available_as_of(symbol, session)
        feature_count = _eligible_fact_count(facts, session)
        if current != facts[:feature_count] or any(
            fact.filed > session or fact.cik != cik for fact in current
        ):
            output["data_errors"].append(key | {"issue": "OUT_OF_SCOPE_READER_OBSERVATION"})
        if "accounting" in phases:
            output["accounting"].append(
                key
                | {"historical_feature_filing_count": feature_count}
                | accounting(current, session, config)
            )
        if "pit" in phases:
            _, close = regular_session_bounds(session)
            issues = pit_issues(current, filings, conflicts, close)
            accessions = {fact.accession_number for fact in current if fact.accession_number}
            if "FACT_FILING_METADATA_MISMATCH" in issues:
                output["data_errors"].append(
                    key
                    | {
                        "issue": "FACT_FILING_METADATA_MISMATCH",
                        "accessions": issues["FACT_FILING_METADATA_MISMATCH"],
                    }
                )
            matrix.append(
                key
                | {
                    "close_utc": close.isoformat(),
                    "date_reader_facts": len(current),
                    "filing_accessions": len(accessions),
                    "matched_accessions_with_acceptance": sum(
                        filings[acc][2] is not None for acc in accessions & filings.keys()
                    ),
                    "after_close_acceptance_utc": {
                        acc: filings[acc][2].isoformat()
                        for acc in issues.get("ACCEPTED_AFTER_SESSION_CLOSE", [])
                    },
                    "accessions_by_issue": issues,
                    "historical_availability": "EVIDENCE_MISSING",
                }
            )
        if legacy is not None:
            if legacy_cik is None or legacy_cik[0] != cik:
                output["legacy"].append(key | {"status": "EVIDENCE_MISSING_EXACT_SYMBOL_CIK"})
            else:
                common = Database(TARGET).facts_available_as_of(
                    symbol, session, connection=connection
                )
                old = Database(":memory:").facts_available_as_of(symbol, session, connection=legacy)
                if any(fact.cik != cik for fact in old):
                    output["legacy"].append(key | {"status": "EVIDENCE_MISSING_MIXED_LEGACY_CIK"})
                else:
                    output["legacy"].append(key | comparison(common, old, session, config))
    if "pit" in phases:
        output["pit"].append(
            issuer
            | {
                "matrix": matrix,
                "malformed_submissions_rows": malformed,
                "conflicting_submissions_accessions": sorted(conflicts),
                "companyfacts_retrieved_at_utc": envelope.retrieved_at.isoformat(),
                "submissions_retrieved_at_utc": provenance["submissions_retrieved_at"],
                "receipt_dataset_version": next(
                    row["dataset_version"] for row in output["receipts"] if row["cik"] == cik
                ),
                "original_filing_collection_candidates": original_witnesses(
                    cik, filings, documents, periods, facts, matrix
                ),
                "unproven": [
                    "public availability of the original fact context",
                    "earlier exact values",
                    "provider same-key correction timeline",
                    "historical amendment linkage",
                    "reconciled original filing fact by fact",
                ],
            }
        )


def run_phases(report, admissions, identity, phases, legacy_path):
    layout = bulk.Layout({"research_db": TARGET.parent.parent}, {})
    external, manifest = pilot.bootstrap_target(layout, TARGET)
    binding = report["binding"]
    paths = {"research": TARGET}
    if legacy_path is not None:
        bulk.directory(legacy_path.parent)
        bulk.plain(legacy_path)
        require(
            legacy_path != TARGET
            and legacy_path.resolve(strict=True) == legacy_path
            and ":" not in legacy_path.name
            and not legacy_path.name.endswith((".", " ")),
            "LEGACY_MUST_BE_A_SEPARATE_LOCAL_FILE",
        )
        paths["legacy"] = legacy_path
    output = {phase: [] for phase in (*PHASES, "warmup", "legacy", "data_errors")}
    output["receipts"] = report["issuers"]
    output["legacy_binding"] = None
    config = load_strategy_config(pilot.REPO / "config" / "strategy.yaml")
    with ExitStack() as stack:
        pins = stack.enter_context(pinned_files(paths))
        for role, path in paths.items():
            require(
                os.path.samestat(bulk.plain(path), os.fstat(pins[role].fileno())),
                "QUALIFICATION_PINNED_IDENTITY_CHANGED",
            )
            no_sidecars(path)
            sqlite_header(pins[role], allow_closed_wal=role == "legacy")
        target = os.fstat(pins["research"].fileno())
        require(
            [target.st_dev, target.st_ino] == binding["target_file_identity"]
            and target.st_size == report["database_bytes_after"],
            "TARGET_FILE_BINDING_MISMATCH",
        )
        if legacy_path:
            legacy_stat = os.fstat(pins["legacy"].fileno())
            output["legacy_binding"] = {
                "path": str(legacy_path),
                "file_identity": [legacy_stat.st_dev, legacy_stat.st_ino],
                "size_bytes": legacy_stat.st_size,
                "mtime_ns": legacy_stat.st_mtime_ns,
                "reader": "Database.facts_available_as_of on both inputs",
            }
        reader = stack.enter_context(LocalInputs(TARGET, capture_provenance=False).snapshot())
        connection = stack.enter_context(reader.connect())
        legacy = stack.enter_context(legacy_connection(legacy_path)) if legacy_path else None
        for item in read_snapshot(connection, report, identity, admissions, external, manifest):
            inspect_issuer(reader, connection, item, identity, phases, legacy, config, output)
        for path in paths.values():
            no_sidecars(path)
    return output


UNIVERSE_POLICY = {
    "COMMON_STOCK": "Requires dated class, listing, symbol/CIK epochs and qualified market inputs.",
    "ETF/ETP/ETN/FUND": "Separate instrument group. No Daily A–F issuer admission.",
    "WARRANT/RIGHT/PREFERRED_STOCK": "Separate classes. No common-stock mapping.",
    "INSURANCE_PRODUCT": "Require instrument evidence beyond issuer SIC.",
    "BANK/FINANCIAL_COMPANY": "Separate issuer classification. Apply existing SIC guards.",
    "UNRESOLVED_REPORTING_ENTITY": "Blocked for research ingestion and trading.",
    "HISTORICAL_OR_DELISTED_STOCK": "Requires dated historical membership and inactive issuers.",
    "UNKNOWN": "Require corroborated instrument classification. No inferred security type.",
}


def json_data(obj):
    return json.dumps(obj, default=str, ensure_ascii=False, allow_nan=False, sort_keys=True)


def write_csv(path, rows):
    fields = list(dict.fromkeys(("symbol", "cik", *(key for row in rows for key in row))))
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            {
                key: json_data(value) if isinstance(value, (dict, list, tuple)) else value
                for key, value in row.items()
            }
            for row in rows
        )
        stream.flush()
        os.fsync(stream.fileno())


def publish(path, metadata, output, phases):
    if os.name != "nt":
        raise ValueError("WINDOWS_NO_REPLACE_PUBLICATION_REQUIRED")
    require(not bulk.exists(path), "OUTPUT_ALREADY_EXISTS")
    stage = path.parent / f".qualification-{uuid4().hex[:12]}.incomplete"
    stage.mkdir()
    coverage_name = "f01_sec_bulk_pilot_coverage.csv"
    accounting_name = "f01_sec_bulk_pilot_accounting.csv"
    gaps_name = "f01_sec_bulk_pilot_evidence_gaps.json"
    gaps = {
        "contract": CONTRACT,
        "source_quality": bulk.QUALITY,
        "historical_pit_qualified": False,
        "source_report_sha256": metadata["input_report_sha256"],
        "pit": output["pit"],
        "data_errors": output["data_errors"],
        "coverage_missing": [
            row for row in output["coverage"] if row["evidence_status"] == "EVIDENCE_MISSING"
        ],
        "accounting_missing_core": [
            {key: row[key] for key in ("symbol", "cik", "session", "missing_core")}
            for row in output["accounting"]
            if row["missing_core"]
        ],
        "historical_trading_universe": output["universe"],
        "always_required_for_historical_validation": [
            "reconcile original EDGAR filings and XBRL contexts with all relied-upon fact versions",
            "verify first public availability and original/amendment ordering",
            "acquire dated security epochs and listing history including inactive securities",
            "qualify market data, peer context and configured eligibility gates separately",
        ],
    }
    phase_counts = {
        phase: dict(Counter(row["evidence_status"] for row in output[phase]))
        for phase in ("coverage", "accounting")
        if phase in phases
    }
    write_csv(stage / coverage_name, output["coverage"])
    write_csv(stage / accounting_name, output["accounting"])
    bulk.write_new(stage / gaps_name, (json_data(gaps) + "\n").encode())
    status = "DATA_ERROR" if output["data_errors"] else "EVIDENCE_MISSING_FOR_HISTORICAL_USE"
    summary = metadata | {
        "status": status,
        "phases": phases,
        "phase_row_counts": phase_counts,
        "source_quality": bulk.QUALITY,
        "dataset_version": 8,
        "historical_pit_qualified": False,
        "trading_universe_qualified": False,
        "ranking_validated": False,
        "g5_qualified": False,
        "operational_qualification": False,
        "data_errors": output["data_errors"],
        "warmup": output["warmup"],
        "universe_policy": UNIVERSE_POLICY,
        "current_universe": output["universe"],
        "legacy_comparison": output["legacy"],
        "legacy_binding": output["legacy_binding"],
        "coverage_end": COVERAGE_END.isoformat(),
        "artifact_sha256": {
            name: pilot.file_sha(stage / name)
            for name in (coverage_name, accounting_name, gaps_name)
        },
    }
    name = "f01_sec_bulk_pilot_qualification.json"
    bulk.write_new(stage / name, (json_data(summary) + "\n").encode())
    stage.rename(path)  # Same volume, native Windows no-replace publication.
    return status, path / name


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", action="append", choices=(*PHASES, "all", "legacy"))
    parser.add_argument(
        "--output", type=Path, help="Fresh direct child of the research artifact root"
    )
    parser.add_argument(
        "--legacy-db", type=Path, help="Explicit path to a quiescent legacy database"
    )
    parser.add_argument("--authorize-legacy-read", action="store_true")
    args = parser.parse_args(argv)
    selected = set(args.phase or ())
    if "all" in selected:
        selected.update(PHASES)
    phases = [phase for phase in (*PHASES, "legacy") if phase in selected]
    legacy = args.legacy_db
    if ("legacy" in phases) != bool(legacy and args.authorize_legacy_read) or (
        "legacy" not in phases and (legacy or args.authorize_legacy_read)
    ):
        parser.error(
            "legacy comparison requires --phase legacy, --legacy-db and --authorize-legacy-read"
        )
    if bool(args.output) != bool(phases):
        parser.error(
            "--output is required with a phase and is unavailable in metadata-only preflight"
        )
    try:
        report, admissions, identity, metadata = preflight()
        if not phases:
            print(
                json_data(
                    metadata | {"status": "METADATA_PREFLIGHT_VERIFIED", "sqlite_opened": False}
                )
            )
            return 0
        path = args.output
        require(
            path.is_absolute()
            and path.parent == ARTIFACTS
            and re.fullmatch(r"f01-[a-z0-9][a-z0-9-]{0,79}", path.name),
            "FRESH_RESEARCH_ARTIFACT_OUTPUT_REQUIRED",
        )
        require(not legacy or legacy.is_absolute(), "ABSOLUTE_LEGACY_PATH_REQUIRED")
        bulk.directory(path.parent)
        with pinned_files({}, directories=(path.parent,)):
            require(not bulk.exists(path), "OUTPUT_ALREADY_EXISTS")
            output = run_phases(report, admissions, identity, phases, legacy)
            status, artifact = publish(path, metadata, output, phases)
        print(json_data({"status": status, "report": str(artifact)}))
        return 4 if status == "DATA_ERROR" else 0
    except FileNotFoundError as exc:
        print(json_data({"status": "EVIDENCE_MISSING", "reason": str(exc)}), file=sys.stderr)
        return 3
    except (QualificationBlocked, OSError) as exc:
        print(json_data({"status": "CHECK_BLOCKED", "reason": str(exc)}), file=sys.stderr)
        return 5
    except (ValueError, RuntimeError, TypeError, KeyError, sqlite3.Error) as exc:
        print(json_data({"status": "DATA_ERROR", "reason": str(exc)}), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
