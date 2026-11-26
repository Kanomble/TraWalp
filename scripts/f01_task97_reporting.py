"""Incremental coverage summaries from the existing raw shards and accounting decisions."""

import csv
import io
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from f01_readiness_inventory import digest
from f01_task96_contract import REPO, key, publish, require, task9
from f01_task96_network import replace_checkpoint

from trading_system.data.canonical_facts import fingerprint
from trading_system.data.xbrl_parser import TAG_ALIASES

ALIASES = {alias: metric for metric, aliases in TAG_ALIASES.items() for alias in aliases}


def extraction_path(item):
    return Path(item["offline_evidence_path"]) / f"{item['accession']}-extraction.json"


def shard_summary(shard, *, technical, accounting):
    facts = shard["observations"]
    fiscal_years = set()
    metrics = {}
    for fact in facts:
        concept = task9.filing.taxonomy(fact.get("concept"))
        if concept == ("dei", "DocumentFiscalYearFocus"):
            value = str(fact.get("text_value") or fact.get("raw_text") or "").strip()
            context = fact.get("context") or {}
            if value.isdecimal() and len(value) == 4 and not context.get("dimensions"):
                fiscal_years.add(value)
        metric = ALIASES.get(concept)
        if not metric:
            continue
        row = metrics.setdefault(metric, {"raw": 0, "technical": 0, "accounting": 0, "pit": 0})
        row["raw"] += 1
        row["technical"] += int(
            bool(
                technical
                and fact.get("source")
                and fact.get("context")
                and not fact.get("evidence_issue")
                and fact.get("unit")
                and fact.get("value") is not None
            )
        )
    decisions = accounting.get("accounting", {})
    for metric, decision in decisions.items():
        row = metrics.setdefault(metric, {"raw": 0, "technical": 0, "accounting": 0, "pit": 0})
        row["accounting"] += int(bool(decision.get("admitted")))
    return {
        "fiscal_year": next(iter(fiscal_years)) if len(fiscal_years) == 1 else "",
        "raw_facts": len(facts),
        "metrics": metrics,
        "accounting_decisions": {
            metric: decision["reason"] for metric, decision in decisions.items()
        },
        "document_issues": len(shard.get("issues", ())),
    }


def read_summary(root, path, sha, *, technical, accounting_path=None, accounting_sha=None):
    mapping = digest(REPO / "src/trading_system/data/xbrl_parser.py")
    rule = digest(Path(__file__))
    inputs = {
        "shard_sha256": sha,
        "accounting_sha256": accounting_sha,
        "mapping_sha256": mapping,
        "reporter_sha256": rule,
        "technical": technical,
    }
    path_key = fingerprint(inputs)
    target = root / f"{path_key}.json"
    if target.exists():
        _, summary = task9.checked(target)
        require(summary["inputs"] == inputs and digest(path) == sha, "COVERAGE_SHARD_CHANGED")
        if accounting_path:
            require(digest(accounting_path) == accounting_sha, "COVERAGE_ACCOUNTING_CHANGED")
        return summary
    _, shard = task9.checked(path, sha, limit=task9.filing.SHARD_LIMIT)
    accounting = {}
    if accounting_path:
        _, policy = task9.checked(accounting_path, accounting_sha, limit=task9.filing.SHARD_LIMIT)
        accounting = policy["filings"][shard["filing"]]
    summary = shard_summary(shard, technical=technical, accounting=accounting) | {"inputs": inputs}
    publish(target, summary)
    return summary


def write_csv(path, rows, fields):
    buffer = io.StringIO(newline="")
    csv_writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    csv_writer.writeheader()
    csv_writer.writerows(rows)
    raw = buffer.getvalue().encode("utf-8")
    task9.bulk.write_new(path, raw)
    return task9.sha(raw)


def report(root, binding, scope, records, metrics, status, reason):
    totals, by_metric = Counter(), {}
    coverage = []
    blocked = []
    receipts, taxonomy, accounting = {}, {}, {}
    for row in sorted(scope.values(), key=key):
        pair = key(row)
        ident = "/".join(pair)
        result = records.get(pair, {})
        summary = result.get("summary") or {}
        row_coverage = {
            "cik": pair[0],
            "accession": pair[1],
            "issuer": row["symbol_label"],
            "form": row["form"],
            "filed": row["filed"],
            "acceptance_utc": row["acceptance_utc"],
            "fiscal_year": summary.get("fiscal_year", ""),
            "report_period": result.get("report_date"),
            "scope": row["scope"],
            "original_status": result.get("original_status", "ORIGINAL_MISSING"),
            "dts_status": result.get("dts_status", "DTS_UNVERIFIED"),
            "extraction_status": result.get("extraction_status", "NOT_COMPLETE"),
            "raw_facts": summary.get("raw_facts", 0),
            "runtime": (result.get("runtime") or {}).get("version"),
            "original_receipt_sha256": (result.get("original_receipt") or {}).get("sha256"),
            "closure_sha256": result.get("closure_sha256"),
        }
        coverage.append(row_coverage)
        totals[row_coverage["original_status"]] += 1
        totals[row_coverage["extraction_status"]] += 1
        totals["raw_facts"] += row_coverage["raw_facts"]
        if receipt := result.get("original_receipt"):
            receipts[ident] = receipt
        if result.get("closure_sha256") or result.get("reused_dts"):
            taxonomy[ident] = {
                "closure_sha256": result.get("closure_sha256"),
                "path": result.get("closure_path"),
                "archived_binding": result.get("reused_dts"),
            }
        if result.get("accounting_sha256") or summary.get("accounting_decisions"):
            accounting[ident] = {
                "sha256": result.get("accounting_sha256"),
                "path": result.get("accounting_path"),
                "decisions": summary.get("accounting_decisions", {}),
            }
        if row_coverage["extraction_status"] != "EXTRACTION_COMPLETE":
            blocked.append(
                {
                    "filing": ident,
                    "scope": row["scope"],
                    "reason": result.get("reason") or row_coverage["original_status"],
                    "uris": result.get("missing_taxonomy_uris", []),
                }
            )
        for metric, counts in summary.get("metrics", {}).items():
            bucket = row["symbol_label"], summary["fiscal_year"], metric
            by_metric.setdefault(bucket, Counter()).update(counts)
    fact_coverage = [
        {"issuer": issuer, "fiscal_year": year, "metric": metric, **counts}
        for (issuer, year, metric), counts in sorted(by_metric.items())
    ]
    docs = {
        "acquisition_progress.json": {
            "status": status,
            "reason": reason,
            "totals": dict(totals),
            "scope_fingerprint": binding["scope_fingerprint"],
            **metrics,
        },
        "download_receipts.json": receipts,
        "taxonomy_coverage.json": taxonomy,
        "arelle_extraction_summary.json": {
            "/".join(pair): {k: v for k, v in info.items() if k != "summary"}
            for pair, info in records.items()
            if info.get("extraction_status")
        },
        "accounting_qualification_summary.json": accounting,
        "blocked_filings.json": blocked,
        "storage_and_provider_metrics.json": metrics,
    }
    snapshots = task9.filing.safe_directory(root / "reports")
    with TemporaryDirectory(dir=snapshots, prefix=".report-") as temporary:
        stage = Path(temporary) / "snapshot"
        stage.mkdir()
        hashes = {name: publish(stage / name, data) for name, data in docs.items()}
        hashes["filing_coverage.csv"] = write_csv(
            stage / "filing_coverage.csv",
            coverage,
            tuple(coverage[0]) if coverage else ("cik",),
        )
        hashes["fundamental_fact_coverage.csv"] = write_csv(
            stage / "fundamental_fact_coverage.csv",
            fact_coverage,
            ("issuer", "fiscal_year", "metric", "raw", "technical", "accounting", "pit"),
        )
        final = (
            f"# Task 9.7 Historical SEC Backfill\n\n**{status}**\n\n"
            f"- {totals['ORIGINAL_VERIFIED']} verifizierte Originalsets in der Kohorte\n"
            f"- {totals['EXTRACTION_COMPLETE']} vollständige Extraktionen\n"
            f"- {totals['raw_facts']} Rohbeobachtungen in den ausgewiesenen Shards\n"
            f"- {len(blocked)} Filings ohne vollständige Extraktion\n\n"
            f"{reason or 'Der eingeschlossene technische Scope ist abgeschlossen.'}\n\n"
            "Das Geschäftsjahr stammt aus eindeutiger DEI-Evidenz. Leere Jahre sind unbelegt. "
            "Kennzahl-Aliase sind reine Coverage. Accounting-Kandidaten folgen der bestehenden "
            "Zulassungsregel. Canonical-Import und PIT-Trading bleiben gesperrt.\n"
        ).encode()
        task9.bulk.write_new(stage / "final_report.md", final)
        hashes["final_report.md"] = task9.sha(final)
        manifest = binding | {
            "status": status,
            "files": hashes,
            "canonical_import": False,
            "pit_trading": False,
        }
        sha = publish(stage / "manifest.json", manifest)
        target = snapshots / sha[:24]
        if not target.exists():
            stage.rename(target)
        else:
            task9.checked(target / "manifest.json", sha)
            require(
                all(digest(target / name) == value for name, value in hashes.items()),
                "TASK97_PUBLISHED_OUTPUT_CHANGED",
            )
    latest = manifest | {"directory": str(target), "manifest_sha256": sha}
    replace_checkpoint(root / "manifest.json", latest)
    return latest
