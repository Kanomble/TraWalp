"""Immutable Task 9.6 inputs and single-block authorization."""

import re
from pathlib import Path

import run_f01_historical_50_manual as task9
from f01_readiness_inventory import Inputs, digest
from prepare_f01_historical_50_batch import D6_SHA, d6_inputs

from trading_system.data.canonical_facts import fingerprint
from trading_system.data.canonical_paths import pinned_files

REPO = Path(__file__).resolve().parents[1]
RESEARCH = Path("G:/TraWalpResearch")
ARTIFACTS = RESEARCH / "import-artifacts"
OUTPUT = ARTIFACTS / "f01-task96-historical-acquisition-v1"
PARENT = ARTIFACTS / "f01-task95-historical-data-readiness-v2"
STAGING = RESEARCH / "research-databases/f01-companyfacts-staging-historical50-v1"
JOBS = RESEARCH / "sec-originals/f01-task96-acquisition-v1"
MARKET = RESEARCH / "market-data/f01-task96-acquisition-v1"
MANIFEST_SHA = "e535318f554e780554ef204c52eedc323362ce8c299224bb8e7f667043a2a7c2"
PLAN_SHA = "3f0b4faacaa95b99d034654a7398285019f8536a95164b9123f2a7e31a9e7adc"
PLAN_ID = "603f8cb42aec798289e1a7649a4adc16251a25dfd522b01cc5ec0a305401a975"
SOURCE = task9.SOURCE_ROOT / "f01_sec_bulk_source_manifest.json"
GIB = 1024**3
CONTRACT = "F01_TASK96_SINGLE_BLOCK_V1"
ORIGINAL_FORMS = {"10-K", "10-Q", "20-F", "40-F"}
require = task9.require
publish = task9.publish


def key(row):
    return row["cik"], row["accession"]


def identity(row, labels):
    cik, accession = key(row)
    require(
        isinstance(cik, str)
        and len(cik) == 10
        and cik.isascii()
        and cik.isdigit()
        and re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)
        and labels.get(cik) == row["symbol_label"],
        "D6_CIK_ACCESSION_SYMBOL_MISMATCH",
    )


class Plan:
    def __init__(self, inputs=None):
        self.inputs = inputs if inputs is not None else Inputs()
        manifest = self.inputs.json(PARENT / "manifest.json", MANIFEST_SHA)
        require(manifest["outputs"]["acquisition_plan.json"] == PLAN_SHA, "PLAN_MANIFEST_MISMATCH")
        self.raw = self.inputs.json(PARENT / "acquisition_plan.json", PLAN_SHA)
        unsigned = {k: v for k, v in self.raw.items() if k != "plan_fingerprint"}
        require(
            self.raw["contract"] == "F01_TASK96_ACQUISITION_PLAN_V1"
            and self.raw["plan_fingerprint"] == fingerprint(unsigned) == PLAN_ID
            and self.raw["authorization"] == {"network": False, "import": False, "trading": False},
            "PLAN_FINGERPRINT_OR_SCOPE_MISMATCH",
        )
        reports = REPO / "reports"
        universe = self.inputs.read(reports / "f01_historical_50_pilot.csv", D6_SHA[0])
        inventory = self.inputs.read(reports / "f01_historical_50_preflight.json", D6_SHA[1])
        _, _, self.issuers, _ = d6_inputs(universe, inventory)
        self.labels = {item["cik"]: symbol for symbol, item in self.issuers.items()}
        self.source_sha = manifest["input_bindings"][str(SOURCE)]["sha256"]
        source = self.inputs.json(SOURCE, self.source_sha)
        submissions = task9.SOURCE_ROOT / "submissions.zip"
        require(
            task9.bulk.plain(submissions).st_size == source["archives"]["submissions"]["bytes"],
            "SUBMISSIONS_ARCHIVE_SIZE_CHANGED",
        )
        self.groups = {step["step"]: step["blocks"] for step in self.raw["sequence"]}
        self.blocks = {}
        self.filings = {}
        for step in (1, 5, 6):
            for block in self.groups[step]:
                block_id = block["block_id"]
                require(
                    block_id == fingerprint({k: v for k, v in block.items() if k != "block_id"}),
                    "BLOCK_ID_MISMATCH",
                )
                for row in block["accessions"]:
                    identity(row, self.labels)
                    require(key(row) not in self.filings, "DUPLICATE_ACCESSION_IN_PLAN")
                    require(step != 5 or row["form"] in ORIGINAL_FORMS, "SEC_SCOPE_FORM_BLOCKED")
                    self.filings[key(row)] = row
                if step == 5:
                    self.blocks[block_id] = ("SEC", block)
        for block in self.groups[2]:
            require(
                (block["symbol"], block["feed"], block["adjustment"], block["timeframe"])
                == ("SPY", "sip", "all", "1Day"),
                "SPY_SCOPE_MISMATCH",
            )
            self.blocks[block["gap_id"]] = ("SPY", block)
        for block in self.groups[4]:
            self.blocks[block["gap_id"]] = ("ISSUER_DAILY", block)

    def binding(self):
        return {
            "manifest_sha256": MANIFEST_SHA,
            "plan_sha256": PLAN_SHA,
            "plan_fingerprint": PLAN_ID,
            "universe_sha256": D6_SHA[0],
            "submissions_source_sha256": self.source_sha,
        }

    def metadata(self, rows):
        archive = task9.SOURCE_ROOT / "submissions.zip"
        issuers = {
            r["cik"]: {"cik": r["cik"], "submissions_member": f"CIK{r['cik']}.json"} for r in rows
        }
        with pinned_files({"submissions": archive}):
            members = task9.pilot.submissions_members(
                {"submissions": archive}, issuers.values(), set()
            )
        indexes = {}
        for cik, (main, sha, history) in members.items():
            issuer = self.issuers[self.labels[cik]]
            history_sha = {h["member"]: h["sha256"] for h in history}
            require(
                issuer["submissions_member_sha256"] == sha
                and issuer["submissions_history_member_sha256"] == history_sha,
                f"SUBMISSIONS_DATA_CHANGED: {cik}",
            )
            index, conflicts, _ = task9.pilot.filing_index(main, history)
            documents, periods = task9.filing_documents(main, history)
            indexes[cik] = index, conflicts, documents, periods
        output, blocked = {}, {}
        for row in rows:
            cik, accession = key(row)
            index, conflicts, documents, periods = indexes[cik]
            record = index.get(accession)
            primary = documents.get(accession, set())
            report = periods.get(accession, set())
            if (
                not record
                or accession in conflicts
                or not record[2]
                or not row["acceptance_utc"]
                or (record[0].isoformat(), record[1], record[2])
                != (row["filed"], row["form"], task9.review.instant(row["acceptance_utc"]))
                or len(primary) != 1
                or len(report) != 1
                or next(iter(report)) > row["filed"]
            ):
                blocked[cik, accession] = "ARCHIVED_SUBMISSIONS_IDENTITY_OR_PERIOD_UNRESOLVED"
                continue
            output[cik, accession] = row | {
                "symbol": row["symbol_label"],
                "acceptance": row["acceptance_utc"],
                "primary": next(iter(primary)),
                "report_date": next(iter(report)),
                "registrant": self.issuers[row["symbol_label"]][
                    "registrant_in_archived_submissions"
                ],
            }
        return output, blocked


def allocation(provider, count):
    if provider == "SEC":
        return {
            "max_requests": count * 35,
            "max_bytes": count * 35 * task9.filing.LIMIT,
            "requests_per_minute": 60,
        }
    return {"max_requests": 12, "max_bytes": 12 * 8 * 1024**2, "requests_per_minute": 30}


def authorization(preflight, path, expected_sha):
    require(expected_sha and len(expected_sha) == 64, "AUTHORIZATION_SHA256_REQUIRED")
    _, auth = task9.checked(path, expected_sha)
    draft = preflight["authorization_draft"]
    limit = draft["limits"]
    require(
        set(auth) == set(draft)
        and auth["contract"] == CONTRACT
        and auth["scope"] == draft["scope"]
        and auth["decision"] == "APPROVED"
        and isinstance(auth["authorized_by"], str)
        and auth["authorized_by"].strip()
        and all(
            isinstance(auth[k], str) and auth[k].strip()
            for k in (
                "provider_terms_evidence",
                "retention_rights_evidence",
                "backup_restore_evidence",
            )
        )
        and set(auth["limits"]) == set(limit)
        and all(type(v) is int and 0 < v <= limit[k] for k, v in auth["limits"].items()),
        "EXPLICIT_BLOCK_AUTHORIZATION_REQUIRED",
    )
    task9.review.instant(auth["authorized_at"])
    return auth


def current_code():
    paths = sorted((REPO / "scripts").glob("f01_task96_*.py"))
    paths += [
        REPO / f"scripts/{name}.py"
        for name in (
            "run_f01_historical_acquisition",
            "f01_readiness_inventory",
            "f01_bulk_staging_rules",
        )
    ]
    paths += [
        REPO / f"src/trading_system/{name}.py"
        for name in ("data/canonical_paths", "data/market_sessions", "fundamentals/metrics")
    ]
    paths += [
        REPO / f"reports/{name}.py"
        for name in ("f01_alpaca_historical_sip_coverage", "f01_historical_50_preflight_run")
    ]
    paths += [
        REPO / f"scripts/{name}.py"
        for name in (
            "run_f01_historical_50_manual",
            "prepare_f01_filing_evidence",
            "download_f01_sec_bulk",
            "prepare_f01_arelle_taxonomies",
            "f01_arelle_adapter",
            "f01_task9_auto_admission",
        )
    ]
    return fingerprint({p.relative_to(REPO).as_posix(): digest(p) for p in paths})
