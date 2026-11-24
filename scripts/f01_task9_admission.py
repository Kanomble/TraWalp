"""Hash-bound reviews and Canonical A-V1 publication for Task 9 filings."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

import prepare_f01_filing_evidence as filing
from prepare_f01_historical_50_batch import plain, plain_tree, sha
from pydantic import BaseModel, ConfigDict

from trading_system.data import canonical_bootstrap as bootstrap
from trading_system.data.canonical_facts import (
    COLS,
    Admission,
    RequestIdentity,
    install_writer_guard,
    require_contract,
    write_company,
)
from trading_system.data.canonical_paths import pinned_files
from trading_system.data.sec_persistence import json_bytes
from trading_system.fundamentals.canonical_research import CanonicalResearchReader
from trading_system.fundamentals.semantic import Evidence, ReportingContext
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact

SOURCE = "F01_TASK9_SOURCE_ADMISSION_V1"
ACCOUNTING = "F01_TASK9_ACCOUNTING_ADMISSION_V1"
AUTH = "F01_TASK9_OPERATION_AUTHORIZATION_V1"
SELECTION = "F01_TASK9_SELECTION_V1"
IMPORT = "F01_TASK9_CANONICAL_FILING_IMPORT_V1"
DECISIONS = ("APPROVED", "REJECTED", "BLOCKED", "UNREVIEWED")
METRICS = {"Assets": "total_assets", "CashAndCashEquivalentsAtCarryingValue": "cash"}
require = filing.require

Decision = Literal["APPROVED", "REJECTED", "BLOCKED", "UNREVIEWED"]


class ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding: dict[str, str]
    decision: Decision
    reviewer: str
    reviewed_at: str


class OperationApproval(ReviewModel):
    contract: Literal["F01_TASK9_OPERATION_AUTHORIZATION_V1"]
    operation: Literal["download", "taxonomy", "import"]


class FilingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Decision
    issuer_identity: str
    completeness: str
    amendment: str
    reference: str
    source_memberships: list[str]


class SourceReview(ReviewModel):
    contract: Literal["F01_TASK9_SOURCE_ADMISSION_V1"]
    filings: dict[str, FilingDecision]


class FactDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Decision
    metric: Literal["cash", "total_assets"]
    scope: str
    version: str
    fallback: Literal["NONE"]
    expected_value: str
    reference: str
    original: dict


class FactDecisions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: dict[str, FactDecision]


class AccountingReview(ReviewModel):
    contract: Literal["F01_TASK9_ACCOUNTING_ADMISSION_V1"]
    filings: dict[str, FactDecisions]


def instant(stamp):
    value = datetime.fromisoformat(stamp)
    require(value.utcoffset() is not None, "TIMEZONE_REQUIRED")
    return value


def reviewer(record):
    require(isinstance(record["reviewer"], str) and record["reviewer"].strip(), "REVIEWER_REQUIRED")
    instant(record["reviewed_at"])


def authorization(record, operation, binding):
    OperationApproval.model_validate(record)
    require(
        record["contract"] == AUTH
        and record["operation"] == operation
        and record["decision"] == "APPROVED"
        and record["binding"] == binding,
        "OPERATION_AUTHORIZATION_MISMATCH",
    )
    reviewer(record)


def approved_review(record, contract, binding):
    (SourceReview if contract == SOURCE else AccountingReview).model_validate(record)
    require(
        record["contract"] == contract
        and record["binding"] == binding
        and record["decision"] == "APPROVED",
        f"ADMISSION_REQUIRED: {contract}",
    )
    reviewer(record)


def filing_key(row):
    return f"{row['cik']}/{row['accession']}"


def observation_id(item):
    return sha(json_bytes(item))


def fact_from_original(row, observation, metric):
    taxonomy, tag = filing.taxonomy(observation["concept"])
    context = observation["context"]
    require(
        METRICS.get(tag) == metric
        and metric in METRICS.values()
        and taxonomy == "us-gaap"
        and context
        and not context["dimensions"]
        and not context.get("opaque_context")
        and context["instant"] == row["report_date"]
        and not context["startDate"]
        and not context["endDate"]
        and observation.get("value") is not None
        and not observation.get("is_nil")
        and not observation.get("evidence_issue"),
        "ORIGINAL_CONTEXT_OR_METRIC_NOT_SUPPORTED",
    )
    value = Decimal(observation["value"])
    fact = FundamentalFact(
        cik=row["cik"],
        symbol=row["symbol"],
        metric=metric,
        taxonomy=taxonomy,
        tag=tag,
        unit="USD",
        value=value,
        period_end=context["instant"],
        filed=row["filed"],
        accession_number=row["accession"],
        form=row["form"],
    )
    require(
        value.is_finite() and filing.matches(observation, fact.model_dump(mode="json"), row["cik"]),
        "ORIGINAL_ENTITY_UNIT_OR_CONTEXT_MISMATCH",
    )
    return fact


def require_source(row, source):
    src = source["filings"][filing_key(row)]
    require(
        src["decision"] == "APPROVED"
        and src["issuer_identity"] == "VERIFIED_REPORTING_ENTITY"
        and src["completeness"] == "VERIFIED"
        and src["amendment"] == "ORIGINAL_FILING"
        and row["form"] in {"10-Q", "10-K"}
        and src["source_memberships"] == ["us-gaap"]
        and src["reference"].strip(),
        "SOURCE_FILING_OR_MEMBERSHIP_BLOCKED",
    )


def admitted_facts(row, shard, source, accounting, binding):
    key = filing_key(row)
    require(shard["status"] == "EXTRACTION_COMPLETE", "EXTRACTION_INCOMPLETE")
    approved_review(source, SOURCE, binding)
    approved_review(
        accounting, ACCOUNTING, binding | {"source_review_sha256": sha(json_bytes(source))}
    )
    require_source(row, source)
    observations = {observation_id(item): item for item in shard["observations"]}
    facts = {}
    decisions = accounting["filings"][key]["facts"]
    require(isinstance(decisions, dict), "ACCOUNTING_FACT_DECISIONS_REQUIRED")
    for digest, decision in decisions.items():
        require(digest in observations and decision["decision"] in DECISIONS, "INVALID_FACT_REVIEW")
        if decision["decision"] != "APPROVED":
            continue
        require(
            decision["scope"] == "CONSOLIDATED"
            and decision["version"] == "AS_FILED"
            and decision["fallback"] == "NONE"
            and decision["reference"].strip(),
            "ACCOUNTING_CONTEXT_BLOCKED",
        )
        observation = observations[digest]
        require(
            observation["source"] in shard["source_documents"],
            "APPROVED_FACT_ORIGINAL_UNBOUND",
        )
        fact = fact_from_original(row, observation, decision["metric"])
        require(
            Decimal(decision["expected_value"]) == fact.value,
            "ACCOUNTING_EXPECTED_VALUE_MISMATCH",
        )
        # Independently reviewed original witnesses stay in the immutable shard and review.
        facts[digest] = (fact, decision["reference"])
    require(facts, "NO_APPROVED_ACCOUNTING_FACTS")
    return facts


def lineage(connection, expected_manifest, code):
    require_contract(connection, writer=False)
    evidence, raw = connection.execute(
        "SELECT evidence,manifest FROM research_bootstrap WHERE singleton=1"
    ).fetchone()
    scope = bootstrap._manifest_scope(raw, expected_manifest)
    record = json.loads(evidence)
    initial = connection.execute(
        "SELECT dataset_id,source_hash FROM fact_dataset WHERE version=1"
    ).fetchone()
    require(
        record["classification"] == bootstrap.CLASSIFICATION
        and record["bootstrap_contract"] == bootstrap.BOOTSTRAP_CONTRACT
        and record["code_fingerprint"] == code
        and record["research_scope"] == scope
        and record["manifest_sha256"] == expected_manifest
        and initial == (record["dataset_id"], expected_manifest),
        "WRONG_TASK9_LINEAGE",
    )
    return record["dataset_id"]


def request_id(dataset, row):
    # Neither approvals nor payload bytes may generate an alternate ID for this filing.
    return sha(json_bytes([IMPORT, dataset, row["cik"], row["accession"]]))


def receipt(connection, dataset, row, facts, binding):
    identifier = request_id(dataset, row)
    previous = connection.execute(
        "SELECT version FROM ingestion_receipt WHERE request_id=?", (identifier,)
    ).fetchone()
    version = connection.execute("SELECT current_version FROM fact_contract").fetchone()[0]
    boundary = previous[0] if previous else version + 1
    accessions = {
        accession
        for (accession,) in connection.execute(
            """SELECT DISTINCT f.accession_number FROM canonical_fact f
               JOIN fact_revision r USING (fact_id) WHERE f.cik=? AND r.version<?
               AND f.accession_number<>''""",
            (row["cik"], boundary),
        )
    } | {row["accession"]}
    evidence = json_bytes(binding).decode()
    result = write_company(
        connection,
        CompanyIdentity(cik=row["cik"], symbol=row["symbol"], name=row["registrant"]),
        [fact for fact, _ in facts.values()],
        sorted(accessions),
        request_id=identifier,
        payload_hash=sha(json_bytes(binding)),
        retrieved_at=binding["retrieved_at"],
        identity=RequestIdentity(row["symbol"], row["cik"], evidence),
        admissions={(row["symbol"], "us-gaap"): Admission(False, True, evidence)},
        expected_version=version,
    )
    return {"request_id": identifier, "dataset_version": result, "replayed": bool(previous)}


def check_target(path, expected_target):
    require(path == expected_target and path.is_absolute(), "TASK9_EXACT_TARGET_REQUIRED")
    plain_tree(path.parent)
    return plain(path)


def open_target(path, *, read_only):
    mode = "?mode=ro&immutable=1" if read_only else "?mode=rw"
    return closing(sqlite3.connect(path.as_uri() + mode, uri=True, isolation_level=None))


def require_predecessors(connection, dataset, predecessors):
    for cik, accession in predecessors:
        identifier = request_id(dataset, {"cik": cik, "accession": accession})
        require(
            connection.execute(
                "SELECT 1 FROM ingestion_receipt WHERE request_id=?", (identifier,)
            ).fetchone(),
            "INITIAL_REVIEW_BATCH_MUST_COMPLETE_BEFORE_APPEND",
        )


def execute(path, expected_target, manifest, code, items, *, write=False, predecessors=()):
    initial = check_target(path, expected_target)
    role = "work" if write else "read"
    reports = []
    with pinned_files({role: path}) as pins:
        require(os.path.samestat(initial, os.fstat(pins[role].fileno())), "TARGET_REPLACED")
        for suffix in ("-wal", "-shm"):
            require(
                not filing.bulk.exists(path.with_name(path.name + suffix)), "TARGET_NOT_QUIESCENT"
            )
        journal = path.with_name(path.name + "-journal")
        if filing.bulk.exists(journal):
            require(write, "ROLLBACK_JOURNAL_REQUIRES_AUTHORIZED_IMPORT_RESUME")
            plain(journal)
            # Recovery is limited to the exact authorized target and manifest.
            raw, _ = filing.bulk.read_metadata(
                path.parent / "bootstrap_manifest.json", max_bytes=bootstrap.MANIFEST_MAX_BYTES
            )
            bootstrap._manifest_scope(raw, manifest)
        with open_target(path, read_only=not write) as connection:
            dataset = lineage(connection, manifest, code)
            require_predecessors(connection, dataset, predecessors)
            if write:
                connection.execute("PRAGMA foreign_keys=ON")
                install_writer_guard(connection)
                for row, facts, binding in items:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        report = receipt(connection, dataset, row, facts, binding)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    reports.append(report | {"filing": filing_key(row)})
            else:
                for row, _, binding in items:
                    identifier = request_id(dataset, row)
                    evidence = connection.execute(
                        "SELECT version,payload_hash FROM ingestion_receipt WHERE request_id=?",
                        (identifier,),
                    ).fetchone()
                    require(
                        evidence is None or evidence[1] == sha(json_bytes(binding)),
                        "RECEIPT_INPUT_CONFLICT",
                    )
                    reports.append(
                        {
                            "request_id": identifier,
                            "filing": filing_key(row),
                            "existing_receipt": list(evidence) if evidence else None,
                        }
                    )
    return {"dataset_id": dataset, "receipts": reports}


def reader_boundary(connection, row, facts, version):
    witnesses = {fact: {"reference": ref} for fact, ref in facts.values()}
    contexts = {
        fact: ReportingContext(row["cik"], "ISSUER_AGGREGATE", ref) for fact, ref in facts.values()
    }
    # Load actual version-bound rows; exact model equality supplies original provenance.
    observed = []
    for values in connection.execute(
        f"SELECT {','.join(COLS)} FROM forward_occ_versions_v1 "
        "WHERE dataset_version=? AND cik=? AND accession_number=?",
        (version, row["cik"], row["accession"]),
    ):
        fields = dict(zip(COLS, values, strict=True))
        fields["frame"] = fields["frame"] or None
        observed.append(FundamentalFact(**fields))
    require(set(witnesses) == set(observed), "E13_EXACT_ORIGINAL_SET_MISMATCH")
    accepted = instant(row["acceptance"])
    reader = CanonicalResearchReader(
        observed,
        cik=row["cik"],
        evidence=Evidence(contexts=contexts, acceptance={row["accession"]: accepted}),
        originals=witnesses,
        flows={},
        debts={},
        amendments={},
        reviewed_rules=frozenset(),
    )
    before = reader.read(accepted - timedelta(microseconds=1)).accounting.current_balance
    after = reader.read(accepted).accounting.current_balance
    for fact in witnesses:
        require(
            before[fact.metric].value is None
            and after[fact.metric].value == fact.value
            and fact in after[fact.metric].sources,
            "E13_FILING_BOUNDARY_MISMATCH",
        )
    return {
        "filing": filing_key(row),
        "dataset_version": version,
        "accepted_at": accepted.isoformat(),
        "matched_original_facts": len(witnesses),
    }
