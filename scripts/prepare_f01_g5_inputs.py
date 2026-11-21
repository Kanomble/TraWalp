#!/usr/bin/env python3
"""8F.3C.1. Inspect local evidence, prepare an unreviewed plan, or provision copies."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
JOB = ROOT / "reports/f01_copy_rehearsal/20261115_8f2a"
ARCHIVE = ROOT / "reports/f_forward_193_evidence/direct_sec"
VALIDATOR = ROOT / "scripts/validate_f01_g5_integration.py"
VALIDATOR_SHA = "f977572c704a8c5b67e89967a05b6adbf7327261808dcc1b9f7f59d302bb5c3f"
BASE_FP = "b4ba5b1c7ea1b477841bee6b990d5a19f2b246610cd88ceb42a75773ed4c7c93"
CANDIDATE_FP = "359ea910074ca458f9514147ba06f75e358941f19aa9f1718c2f079c943a1c22"
APPROVAL_FP = "d1a10d358684ca2a2d3962af015e33ea3bb8adcfb66db9fcd9dff82e695a876f"
SHORTLIST = {
    "LLY": "0000059478",
    "ACN": "0001467373",
    "CSX": "0000277948",
    "MA": "0001141391",
    "UNP": "0000100885",
}
DEFAULT_SYMBOLS = ("ACN", "LLY")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_file(path):
    require(path.suffix == ".json", f"JSON_REQUIRED: {path}")
    return json.loads(path.read_bytes())


def repository_candidate():
    def git(*args):
        return subprocess.check_output(
            ["git", "--no-optional-locks", "-c", f"safe.directory={ROOT.as_posix()}", *args],
            cwd=ROOT,
            text=True,
        ).strip()

    require(git("branch", "--show-current") == "SEC-integration", "WRONG_BRANCH")
    raw = VALIDATOR.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == VALIDATOR_SHA, "VALIDATOR_DRIFT")
    # Load the same verified bytes and reuse the 8F.3C shape/validation functions.
    namespace = {"__file__": str(VALIDATOR), "__name__": "_f01_g5_preparation_validator"}
    exec(compile(raw, str(VALIDATOR), "exec"), namespace)
    name = VALIDATOR.relative_to(ROOT).as_posix()
    files = {
        p.relative_to(ROOT).as_posix(): sha(p)
        for folder in ("src", "scripts", "config")
        for p in (ROOT / folder).rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".yaml", ".yml", ".sql", ".json"}
        and "__pycache__" not in p.parts
        and p.resolve() != SELF
    }
    for item in ("pyproject.toml", "requirements.txt", ".env"):
        p = ROOT / item
        if p.is_file():
            files[item] = sha(p)
    packages = {
        d.metadata["Name"]: d.version
        for d in importlib.metadata.distributions()
        if d.metadata["Name"]
    }
    runtime = [packages, sys.version, sqlite3.sqlite_version]
    base = digest([{k: v for k, v in files.items() if k != name}, *runtime])
    candidate = digest([files, *runtime])
    require((base, candidate) == (BASE_FP, CANDIDATE_FP), "8F3C_FROZEN_CODE_OR_RUNTIME_DRIFT")
    return {
        "head": git("rev-parse", "HEAD"),
        "original_8f3b_code_fingerprint": BASE_FP,
        "reviewable_additions_sha256": {name: VALIDATOR_SHA},
        "candidate_code_fingerprint": candidate,
        "files": len(files),
        "packages": len(packages),
    }, namespace


def historical():
    approval = json_file(ROOT / "docs/f01_acceptance_v2_approval.json")
    require(digest(approval) == APPROVAL_FP, "APPROVAL_DRIFT")
    bindings = approval["bindings"]
    manifest_path = JOB / "verified/f01_copy_rehearsal_manifest.json"
    require(sha(manifest_path) == bindings["manifest_sha256"], "MANIFEST_DRIFT")
    manifest = json_file(manifest_path)
    (g3,) = (JOB / "g3_runs").glob("*/result/f01_copy_migration_g3_report.json")
    (g6,) = (JOB / "acceptance_v2_runs").glob("*/result/f01_acceptance_v2_g6.json")
    for report, seal_name in ((g3, "result_seal.json"), (g6, "seal.json")):
        seal = json_file(report.with_name(seal_name))
        require(
            report.name in seal
            and all(
                Path(name).name == name
                and Path(name).suffix in {".md", ".json", ".jsonl"}
                and sha(report.with_name(name)) == checksum
                for name, checksum in seal.items()
            ),
            f"RESULT_SEAL_DRIFT: {report.name}",
        )
    g3_result, g6_result = json_file(g3), json_file(g6)
    body = dict(g3_result["result"])
    fingerprint = body.pop("result_fingerprint")
    require(
        fingerprint == digest(body) == bindings["g3_result_fingerprint"]
        and body["work_sha256"] == bindings["work_sha256"]
        and g3_result["status"] == "G3_COMPLETE",
        "G3_BINDING_DRIFT",
    )
    require(
        g6_result["status"] == g6_result["g6"]["status"] == "PASS"
        and not g6_result["g6"]["gaps"]
        and digest(g6_result) == json_file(g6.with_name("fingerprint.json"))["report_fingerprint"]
        and g6_result["binding"]["historical_bindings"] == bindings,
        "G6_BINDING_DRIFT",
    )
    return manifest, bindings


def real_archive(symbol):
    path = ARCHIVE / (SHORTLIST[symbol] + ".json.gz")
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        response = json.load(stream)
    payload = response["payload"]
    require(
        response["status"] == "PROVIDER_COMPANYFACTS_PRESENT"
        and str(payload["cik"]).zfill(10) == SHORTLIST[symbol],
        f"ARCHIVE_CIK_OR_STATUS_DRIFT: {symbol}",
    )
    return path, response


def capacity(source, work, volume):
    free = shutil.disk_usage(volume).free
    legacy_bytes, canonical_bytes = source.stat().st_size, work.stat().st_size
    minimum = legacy_bytes + 6 * canonical_bytes + 20 * 1024**3
    return {
        "source_bytes": legacy_bytes,
        "canonical_bytes": canonical_bytes,
        "minimum_additional_bytes": minimum,
        "available_bytes": free,
        "minimum_fits": free >= minimum,
    }


def no_sidecars(path):
    return not any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))


def safe_snapshot(path):
    require(
        path.is_file()
        and path.resolve(strict=True) == path
        and path.stat().st_nlink == 1
        and no_sidecars(path),
        f"EXPECTED_UNLINKED_QUIESCENT_SNAPSHOT: {path}",
    )


@contextmanager
def read_snapshot(path):
    safe_snapshot(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 20
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        yield connection


def runtime_imports():
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def fact_values(payload, symbol):
    from trading_system.data.canonical_facts import COLS, KEY
    from trading_system.data.database import _fact_rows
    from trading_system.data.xbrl_parser import parse_company_facts

    facts = parse_company_facts(payload, symbol)
    values = {}
    ambiguous = set()
    for row in _fact_rows(facts):
        item = dict(zip(COLS, row, strict=True))
        key = tuple(item[name] for name in KEY)
        value = Decimal(item["value"])
        if key in values and values[key] != value:
            ambiguous.add(key)
        values[key] = value
    return facts, {key: value for key, value in values.items() if key not in ambiguous}, ambiguous


def compare_observations(earlier, later, symbol):
    _, before, uncertain_before = fact_values(earlier, symbol)
    _, after, uncertain_after = fact_values(later, symbol)
    changed = sorted(
        (key for key in before.keys() & after.keys() if before[key] != after[key]), key=repr
    )
    return {
        "overlap": len(before.keys() & after.keys()),
        "new_keys": len(after.keys() - before.keys()),
        "missing_keys": len(before.keys() - after.keys()),
        "changed_same_keys": len(changed),
        "ambiguous_keys": len(uncertain_before | uncertain_after),
        "examples": [
            {"key": key, "before": str(before[key]), "after": str(after[key])}
            for key in changed[:5]
        ],
    }


def cache_row(connection, cik, endpoint):
    row = connection.execute(
        "SELECT fetched_at,length(CAST(payload AS BLOB)) FROM raw_sec_cache "
        "WHERE cik=? AND endpoint=?",
        (cik, endpoint),
    ).fetchone()
    if row is None:
        return None
    when, size = row
    require(size <= 16 * 1024**2, f"CACHE_PAYLOAD_ABOVE_CENSUS_LIMIT: {cik}/{endpoint}")
    raw = connection.execute(
        "SELECT payload FROM raw_sec_cache WHERE cik=? AND endpoint=?",
        (cik, endpoint),
    ).fetchone()[0]
    return {
        "fetched_at": when,
        "bytes": size,
        "sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "payload": json.loads(raw),
    }


def census(source, work, symbols, export=None):
    runtime_imports()
    rows = []
    with read_snapshot(source) as legacy, read_snapshot(work) as canonical:
        state = {
            (source, key): json.loads(value)
            for source, key, value in canonical.execute(
                "SELECT source,key,value FROM sync_state WHERE "
                "(source='sec_reference' AND key='ticker_to_cik') OR "
                "(source='sec_change_detection' AND key='daily_master_index')"
            )
        }
        tickers = state.get(("sec_reference", "ticker_to_cik"), {})
        for symbol in symbols:
            cik = SHORTLIST[symbol]
            path, direct = real_archive(symbol)
            facts, _, _ = fact_values(direct["payload"], symbol)
            cf, sub = (
                cache_row(legacy, cik, endpoint) for endpoint in ("companyfacts", "submissions")
            )
            conflict = canonical.execute(
                "SELECT value FROM sync_state WHERE source='sec_identity_conflicts' AND key=?",
                (symbol,),
            ).fetchone()
            company = canonical.execute(
                "SELECT cik FROM companies WHERE symbol=?", (symbol,)
            ).fetchone()
            witness = []
            for taxonomy in sorted({fact.taxonomy for fact in facts}):
                row = canonical.execute(
                    "SELECT o.id,o.legacy_admitted,o.forward_order_admitted FROM canonical_fact f "
                    "INDEXED BY canonical_forward_reader CROSS JOIN source_occurrence o "
                    "INDEXED BY occurrence_membership WHERE f.cik=? AND o.fact_id=f.fact_id "
                    "AND o.symbol=? AND o.taxonomy=? "
                    "ORDER BY f.filed,f.period_end,f.fact_id,o.id LIMIT 1",
                    (cik, symbol, taxonomy),
                ).fetchone()
                witness.append(
                    {
                        "cik": cik,
                        "symbol": symbol,
                        "taxonomy": taxonomy,
                        "occurrence_id": row[0] if row else None,
                        "existing_flags": row[1:] if row else None,
                        "decision": "BLOCKED_PENDING_INDEPENDENT_REVIEW",
                    }
                )
            comparison = None
            if cf and str(cf["payload"].get("cik", "")).zfill(10) == cik:
                first = datetime.fromisoformat(cf["fetched_at"])
                second = datetime.fromisoformat(direct["retrieved_at"])
                if (
                    first.utcoffset() is not None
                    and second.utcoffset() is not None
                    and first != second
                ):
                    payloads = (cf["payload"], direct["payload"])
                    comparison = compare_observations(
                        *(payloads if first < second else payloads[::-1]), symbol
                    )
                    comparison["earlier_origin"] = "cache" if first < second else "archive"
            rows.append(
                {
                    "symbol": symbol,
                    "cik": cik,
                    "company_cik": company[0] if company else None,
                    "ticker_reference": tickers.get(symbol),
                    "conflict": json.loads(conflict[0]) if conflict else None,
                    "companyfacts_archive": {
                        "path": str(path),
                        "sha256": sha(path),
                        "retrieved_at": direct["retrieved_at"],
                    },
                    "cache": {
                        key: {k: v for k, v in rec.items() if k != "payload"} if rec else None
                        for key, rec in (("companyfacts", cf), ("submissions", sub))
                    },
                    "complete_submissions_cache": bool(
                        sub
                        and str(sub["payload"].get("cik", "")).zfill(10) == cik
                        and isinstance(sub["payload"].get("filings", {}).get("recent"), dict)
                    ),
                    "source_memberships": witness,
                    "two_payload_comparison": comparison,
                }
            )
            if export is not None:
                records = {
                    "archive-companyfacts": {
                        "endpoint": "companyfacts",
                        "cik": cik,
                        "source_path": str(path),
                        "source_file_sha256": sha(path),
                        "fetched_at": direct["retrieved_at"],
                        "payload": direct["payload"],
                    }
                }
                for endpoint, record in (("companyfacts", cf), ("submissions", sub)):
                    if record:
                        records["cache-" + endpoint] = {
                            **record,
                            "endpoint": endpoint,
                            "cik": cik,
                            "source_path": str(source),
                            "source_table": "raw_sec_cache",
                        }
                for label, record in records.items():
                    with (export / f"{symbol}-{label}.json").open("x", encoding="utf-8") as stream:
                        json.dump(record, stream, indent=2, ensure_ascii=True)
        return {
            "census": rows,
            "daily_cursor_metadata_only": state.get(("sec_change_detection", "daily_master_index")),
        }


def prepare_copies(root, source, work, manifest, bindings, symbols):
    from trading_system.data.canonical_facts import require_contract
    from trading_system.data.canonical_paths import pinned_files
    from trading_system.data.database import Database
    from trading_system.data.sec_sync_persistence import PENDING

    root = root.absolute()
    require(
        root.name.startswith("f01-8f3c-")
        and root.parent.resolve(strict=True) == root.parent
        and not root.exists()
        and not root.is_relative_to(JOB)
        and not root.is_relative_to(ROOT / "data"),
        "NEW_ISOLATED_RUN_ROOT_REQUIRED",
    )
    require(capacity(source, work, root.parent)["minimum_fits"], "COPY_CAPACITY_BLOCKED")
    targets = {
        "legacy": (source, root / "legacy.sqlite3", "source_sha256", manifest["source_sha256"]),
        "canonical": (
            work,
            root / "f01-sec-isolated-candidate/f01-sec-candidate.sqlite3",
            "work_sha256",
            bindings["work_sha256"],
        ),
    }
    root.mkdir()
    result = {}
    with pinned_files({"reference_source": source, "reference_work": work}):
        for role, (origin, target, lineage, checksum) in targets.items():
            safe_snapshot(origin)
            require(sha(origin) == checksum, f"REFERENCE_HASH_DRIFT: {role}")
            target.parent.mkdir(exist_ok=True)
            with origin.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            require(sha(target) == checksum and no_sidecars(origin), f"COPY_HASH_DRIFT: {role}")
            db = Database(target)
            require(
                set(symbols) <= set(db.list_tradable_asset_symbols())
                and not db.sync_values(PENDING),
                "COPY_UNIVERSE_OR_PENDING_DRIFT",
            )
            with closing(sqlite3.connect(target.as_uri() + "?mode=rw", uri=True)) as connection:
                require(
                    connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete",
                    "COPY_JOURNAL_MODE_NOT_QUIESCENT",
                )
                if role == "canonical":
                    require_contract(connection, writer=False)
                placeholders = ",".join("?" for _ in symbols)
                connection.execute(
                    "UPDATE assets SET tradable=0 WHERE tradable=1 AND symbol NOT IN "
                    f"({placeholders})",
                    symbols,
                )
                connection.commit()
            require(
                no_sidecars(target) and sorted(db.list_tradable_asset_symbols()) == symbols,
                "COPY_SCOPE_NOT_ISOLATED",
            )
            result[role] = {
                "relative_path": target.relative_to(root).as_posix(),
                "sha256": sha(target),
                "lineage": {lineage: checksum},
            }
    return {
        "targets": result,
        "universe": symbols,
        "root": str(root),
        "inventory": "Disposable copies; only assets.tradable scoped after byte parity proof",
    }


def aware(value):
    when = datetime.fromisoformat(value)
    require(when.utcoffset() is not None, "EVIDENCE_TIMEZONE_REQUIRED")
    return when


def draft_bundle(spec_path, destination, candidate, validator):
    """Assemble real payloads without certifying identity or admission."""
    spec_path = spec_path.resolve(strict=True)
    destination = destination.absolute()
    review_path = destination.with_name(destination.stem + "-review-template.json")
    require(
        destination.suffix == ".json"
        and destination.parent == spec_path.parent
        and not destination.exists()
        and not review_path.exists(),
        "NEW_LOCAL_DRAFT_REQUIRED",
    )
    spec = json_file(spec_path)
    bundle = validator["input_template"](candidate)
    bundle.update(material="UNREVIEWED_LOCAL_SEC_OBSERVATIONS", reviewer=None, reviewed_at_utc=None)
    objects, evidence = {}, {}
    for key, item in sorted(spec["evidence_files"].items()):
        relative = Path(item["path"])
        require(
            not relative.is_absolute()
            and ".." not in relative.parts
            and relative.suffix in {".json", ".idx", ".txt"}
            and item["origin"],
            "UNSAFE_EVIDENCE_PATH",
        )
        path = spec_path.parent / relative
        require(
            path.resolve(strict=True) == path
            and path.is_file()
            and path.stat().st_size <= 32 * 1024**2,
            "UNSAFE_OR_LARGE_EVIDENCE_FILE",
        )
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        require(item.get("sha256", checksum) == checksum, "EVIDENCE_CHECKSUM_MISMATCH")
        text = raw.decode("utf-8-sig")
        objects[key] = json.loads(text) if relative.suffix == ".json" else text
        evidence[key] = {"path": relative.as_posix(), "origin": item["origin"], "sha256": checksum}
    ticker_key = spec["ticker_evidence"]
    mapping = objects[ticker_key]["ticker_map"]
    require(
        all(SHORTLIST.get(symbol) == cik for symbol, cik in mapping.items()),
        "TICKER_OUTSIDE_REVIEW_SHORTLIST",
    )
    bundle.update(
        symbols=sorted(mapping),
        ticker_map=mapping,
        ticker_evidence=ticker_key,
        evidence_files=evidence,
        targets=spec["targets"],
    )
    observations = {}
    times = {}
    for phase in ("baseline", "corrected", "incremental"):
        batch = spec["observations"][phase]
        indexes = batch["daily_index_evidence"]
        companies = {}
        for symbol, key in sorted(batch["companies"].items()):
            item = objects[key]
            cf, sub = item["companyfacts"], item["submissions"]
            cf_time = aware(item["companyfacts_retrieved_at"])
            sub_time = aware(item["submissions_retrieved_at"])
            require(
                cf_time <= aware(batch["retrieved_at"])
                and sub_time <= aware(batch["retrieved_at"])
                and isinstance(sub.get("filings", {}).get("recent"), dict),
                f"INCOMPLETE_OR_MISDATED_SUBMISSIONS: {phase}/{symbol}",
            )
            times[phase, symbol] = cf_time
            companies[symbol] = {
                "cik": mapping[symbol],
                "evidence": key,
                "submissions": sub,
                "companyfacts": cf,
                "submissions_fingerprint": digest(sub),
                "companyfacts_fingerprint": digest(cf),
            }
        observations[phase] = {
            "retrieved_at": batch["retrieved_at"],
            "companies": companies,
            "daily_index_evidence": indexes,
            "daily_indexes": {name: objects[key] for name, key in indexes.items()},
        }
    require(observations["incremental"]["daily_indexes"], "INCREMENTAL_INDEX_BODY_REQUIRED")
    bundle["observations"] = observations
    bundle["memberships"] = [
        row | {"decision": "BLOCKED", "legacy_admitted": False, "forward_order_admitted": False}
        for row in spec["memberships"]
    ]
    validator["validate_plan"](bundle)
    first = min(mapping, key=mapping.get)
    require(
        times["baseline", first] < times["corrected", first], "REAL_CORRECTION_CHRONOLOGY_REQUIRED"
    )
    comparisons = {}
    for symbol in sorted(mapping):
        require(
            times["baseline", symbol] <= times["corrected", symbol] <= times["incremental", symbol],
            f"OBSERVATION_ORDER_DRIFT: {symbol}",
        )
        comparisons[symbol] = compare_observations(
            observations["baseline"]["companies"][symbol]["companyfacts"],
            observations["corrected"]["companies"][symbol]["companyfacts"],
            symbol,
        )
        require(
            not comparisons[symbol]["new_keys"] and not comparisons[symbol]["ambiguous_keys"],
            f"CORRECTION_STAGE_ADDS_OR_AMBIGUATES_FACTS: {symbol}",
        )
    require(
        comparisons[first]["changed_same_keys"] > 0,
        f"REAL_SAME_KEY_CORRECTION_REQUIRED: {first}",
    )
    bundle["preparation"] = {
        "status": "PENDING_INDEPENDENT_REVIEW",
        "first_company": first,
        "policy": validator["admission_plan"](bundle),
        "corrections": comparisons,
    }
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(bundle, stream, indent=2, ensure_ascii=True)
    with review_path.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "status": "PENDING_INDEPENDENT_REVIEW",
                "draft_sha256": sha(destination),
                "reviewer": None,
                "reviewed_at_utc": None,
                "checks": [
                    "recorded response provenance and chronology",
                    "target lineage and observed bytes",
                    "current identity and occurrence IDs",
                    "per-membership decisions and reason",
                    "unchanged Candidate fingerprint",
                ],
                "material_after_independent_approval": "AUTHORIZED_LOCAL_SEC_OBSERVATIONS",
            },
            stream,
            indent=2,
        )
    return {"draft": str(destination), "separate_review_template": str(review_path)}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument("--census", action="store_true", help="Bounded read-only snapshot probes")
    mode.add_argument("--draft", type=Path, help="Assemble from a local preparation spec")
    mode.add_argument("--copy-targets", type=Path, help="Create new scoped disposable snapshots")
    mode.add_argument(
        "--spec-template", action="store_true", help="Print the preparation spec shape"
    )
    cli.add_argument("--symbols", nargs=2, choices=SHORTLIST, default=DEFAULT_SYMBOLS)
    cli.add_argument("--volume", type=Path, default=ROOT / "reports")
    cli.add_argument("--output", type=Path, help="New JSON draft beside its preparation spec")
    cli.add_argument(
        "--export-cache", type=Path, help="New directory for selected evidence exports"
    )
    cli.add_argument("--authorize-copy", action="store_true")
    return cli


def main(args):
    require(not args.authorize_copy or args.copy_targets, "AUTHORIZE_COPY_REQUIRES_COPY_TARGETS")
    require(not args.export_cache or args.census, "CACHE_EXPORT_REQUIRES_CENSUS")
    candidate, validator = repository_candidate()
    manifest, bindings = historical()
    source, work = (Path(manifest[key]) for key in ("source", "work"))
    symbols = sorted(set(args.symbols))
    require(len(symbols) == 2, "SELECT_TWO_DISTINCT_SYMBOLS")
    if args.spec_template:
        bundle = validator["input_template"](candidate)
        return {
            "targets": bundle["targets"],
            "ticker_evidence": bundle["ticker_evidence"],
            "evidence_files": bundle["evidence_files"],
            "memberships": bundle["memberships"],
            "observations": {
                phase: {
                    "retrieved_at": "<separately recorded batch cutoff>",
                    "companies": {
                        symbol: "<evidence key for paired endpoint JSON>" for symbol in symbols
                    },
                    "daily_index_evidence": {},
                }
                for phase in ("baseline", "corrected", "incremental")
            },
        }
    if args.census:
        runtime_imports()
        from trading_system.data.canonical_paths import pinned_files

        export = args.export_cache.absolute() if args.export_cache else None
        if export is not None:
            require(
                export.parent.resolve(strict=True) == export.parent
                and not export.is_relative_to(JOB)
                and not export.is_relative_to(ROOT / "data"),
                "ISOLATED_EXPORT_DIR_REQUIRED",
            )
            export.mkdir()
        with pinned_files({"reference_source": source, "reference_work": work}):
            return census(source, work, symbols, export)
    if args.copy_targets:
        require(args.authorize_copy, "EXPLICIT_MANUAL_COPY_AUTHORIZATION_REQUIRED")
        runtime_imports()
        return prepare_copies(args.copy_targets, source, work, manifest, bindings, symbols)
    if args.draft:
        require(args.output, "DRAFT_OUTPUT_REQUIRED")
        runtime_imports()
        return draft_bundle(args.draft, args.output, candidate, validator)
    return {
        "status": "INPUT_COLLECTION_REQUIRED",
        "candidate_without_preparation_helper": candidate,
        "preparation_helper_sha256": sha(SELF),
        "selected_for_bounded_review": symbols,
        "capacity": capacity(source, work, args.volume),
        "remaining": [
            "paired full Submissions and Companyfacts with dated provenance",
            "real ordered same-key correction",
            "filing-index bodies and accession delta",
            "independent identity, occurrence and admission review",
        ],
    }


if __name__ == "__main__":
    try:
        print(json.dumps(main(parser().parse_args()), default=str, indent=2))
    except Exception as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        raise SystemExit(2) from None
