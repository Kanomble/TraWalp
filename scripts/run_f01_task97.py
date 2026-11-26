#!/usr/bin/env python3
"""Run the bound 50-issuer historical SEC backfill with automatic resume."""

import argparse
import math
import shutil
import time
import urllib.error
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import run_f01_historical_acquisition as workflow

# The Task-9.6 runner initializes the project and script imports.
# isort: split
import f01_task96_taxonomy as tax
import f01_task97_reporting as reporting
from f01_readiness_inventory import digest
from f01_task96_contract import ARTIFACTS, REPO, TASK962_OUTPUT, current_code, key, publish, require
from f01_task96_evidence import checked_original
from f01_task96_network import Budget, CountedSecOpener, download_sec_filing, replace_checkpoint
from f01_task96_process import progress_for
from f01_task96_taxonomy_audit import DIAGNOSES
from f01_task97_taxonomy import TaxonomyBatch, missing_sources
from run_f01_task962 import ERRORS

from trading_system.config import load_settings
from trading_system.data.canonical_facts import fingerprint

task9 = workflow.task9
OUTPUT = ARTIFACTS / "f01-task97-historical-sec-backfill-v1"
CONTRACT = "F01_TASK97_ISOLATED_HISTORICAL_SEC_BACKFILL_V1"
MIB = 1024**2
RESERVE = 8 * 1024 * MIB
LIMITS = {"max_requests": 2400, "max_bytes": 12 * 1024 * MIB, "requests_per_minute": 60}
PILOT_SIZE = 24
PILOT_KEYS = {tuple(name.split("/")) for name in DIAGNOSES}
PILOT_MANIFEST = TASK962_OUTPUT / "run-620e8607919d4e66/manifest.json"
PILOT_MANIFEST_SHA = "86fa9fb24515cadbea5b673d5e3dcaeaac1a5adb6ded6e696cca0e5ce1974641"


def scope_for(plan):
    labels = {1: "ARCHIVED", 5: "BACKFILL", 6: "SEPARATE_REVIEW"}
    scope = {
        key(row): row | {"scope": labels[step], "plan_block_id": block["block_id"]}
        for step in labels
        for block in plan.groups[step]
        for row in block["accessions"]
    }
    for pair in PILOT_KEYS:
        require(scope[pair]["scope"] == "BACKFILL", "TASK962_PILOT_PARENT_SCOPE_CHANGED")
        scope[pair]["scope"] = "TASK962_PILOT"
    counts = Counter(row["scope"] for row in scope.values())
    require(
        counts == {"ARCHIVED": 339, "BACKFILL": 2583, "TASK962_PILOT": 2, "SEPARATE_REVIEW": 28}
        and len({row["cik"] for row in scope.values()}) == 50,
        "TASK97_PLAN_SCOPE_CHANGED",
    )
    return scope


def select_pilot(scope):
    pending = {pair for pair, row in scope.items() if row["scope"] == "BACKFILL"}
    issuers, years, forms = Counter(), Counter(), Counter()
    chosen = []
    while len(chosen) < min(PILOT_SIZE, len(chosen) + len(pending)):

        def rank(pair):
            row = scope[pair]
            return (issuers[pair[0]] + years[row["filed"][:4]], forms[row["form"]], pair)

        pair = min(pending, key=rank)
        row = scope[pair]
        chosen.append(pair)
        pending.remove(pair)
        issuers[pair[0]] += 1
        years[row["filed"][:4]] += 1
        forms[row["form"]] += 1
    return chosen


def pilot_gate(pairs, results, http):
    n = len(pairs)
    verified = sum(
        results.get(pair, {}).get("original_status") == "ORIGINAL_VERIFIED" for pair in pairs
    )
    good = sum(
        results.get(pair, {}).get("extraction_status") == "EXTRACTION_COMPLETE" for pair in pairs
    )
    dts = sum(results.get(pair, {}).get("dts_status") == "DTS_COMPLETE" for pair in pairs)
    attempts = sum(http.values())
    successful = sum(v for status, v in http.items() if status.startswith("2"))
    passed = verified * 100 >= n * 85 and dts * 100 >= n * 70 and good * 100 >= dts * 90
    passed = bool(n and passed and (not attempts or successful * 100 >= attempts * 90))
    return {
        "passed": passed,
        "originals": verified,
        "dts_complete": dts,
        "extracted": good,
        "piloted": n,
        "http_attempts": attempts,
        "http_2xx": successful,
    }


def capacity(root, originals, observed_sizes):
    free = shutil.disk_usage(root).free
    samples = sorted(observed_sizes)
    typical = samples[(len(samples) - 1) * 9 // 10] if samples else 0
    per_filing = max(32 * MIB, 3 * typical)
    peak = task9.DOCUMENTS_PER_FILING * task9.filing.LIMIT + task9.filing.SHARD_LIMIT
    available = min(free, shutil.disk_usage(originals).free) - RESERVE - peak
    return {
        "free_bytes": free,
        "reserve_bytes": RESERVE,
        "per_filing_bytes": per_filing,
        "peak_bytes": peak,
        "batch_capacity": max(0, min(40, available // per_filing)),
    }


def pause_status(error):
    reason = str(error)
    if any(
        word in reason
        for word in (
            "STORAGE",
            "FILING_CACHE_SPACE_REQUIRED",
            "INSUFFICIENT_FREE_SPACE",
            "WinError 112",
        )
    ):
        return "STORAGE_BLOCKED"
    if any(
        word in reason
        for word in (
            "HTTP_401",
            "HTTP_403",
            "HTTP_429",
            "Retry-After",
            "RETRY_AFTER",
            "CLOCK_ANOMALY",
            "LIMITER",
            "RATE_LIMIT",
            "SEC_USER_AGENT",
            "SEC user agent",
            "SEC_USER_AGENT_REQUIRED",
        )
    ):
        return "PROVIDER_BLOCKED"
    if "BUDGET_EXHAUSTED" in reason:
        return "PARTIAL_BLOCKED"
    return None


class NetworkGuard:
    """Check time and capacity before handing a request to the unchanged SEC limiter."""

    def __init__(self, path, opener, *, wall=time.time, monotonic=time.monotonic, disk=None):
        self.path, self.opener, self.wall, self.monotonic, self.disk = (
            path,
            opener,
            wall,
            monotonic,
            disk,
        )

    def check(self):
        now, ticks = self.wall(), self.monotonic()
        require(math.isfinite(now) and math.isfinite(ticks), "SEC_CLOCK_ANOMALY")
        if self.path.exists():
            _, prev = task9.checked(self.path)
            wall_delta = now - prev["wall"]
            tick_delta = ticks - prev["monotonic"]
            require(
                wall_delta >= 0 and (tick_delta < 0 or abs(wall_delta - tick_delta) <= 5),
                "SEC_CLOCK_ANOMALY: persistent clock disagrees with current time",
            )
        state_path = self.opener.state
        if state_path.exists():
            _, state = task9.checked(state_path)
            require(math.isfinite(state["until"]), "SEC_CLOCK_ANOMALY: invalid limiter deadline")
            require(not state["blocked"], f"SEC_LIMITER_MANUAL_REVIEW: {state['blocked']}")
            require(
                state["until"] <= now + 60, "SEC_CLOCK_OR_LIMITER_HOLD: deadline over 60 seconds"
            )
        if self.disk is not None:
            require(self.disk() >= RESERVE + tax.MAX_ARCHIVE + tax.MAX_EXPANDED, "STORAGE_BLOCKED")
        replace_checkpoint(self.path, {"wall": now, "monotonic": ticks})

    def open(self, request, *, timeout):
        self.check()
        return self.opener.open(request, timeout=timeout)


class FilingJournal:
    def __init__(self, run, guarded, budget):
        self.run, self.guarded, self.budget = run, guarded, budget

    def open(self, request, *, timeout):
        path = self.run / f"req-{self.budget.state['requests']:02}.json"
        event = {
            "method": request.get_method(),
            "url": request.full_url,
            "status": "IN_FLIGHT",
            "utc": datetime.now(UTC).isoformat(),
        }
        publish(path, event)
        try:
            response = self.guarded.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            event.update(status=exc.code, retry_after=exc.headers.get("Retry-After"))
            raise
        except ERRORS as exc:
            event.update(status="BLOCKED", reason=str(exc))
            raise
        else:
            event.update(status=response.status)
            return response
        finally:
            replace_checkpoint(path, event)


def verified_pilot():
    _, manifest = task9.checked(PILOT_MANIFEST, PILOT_MANIFEST_SHA)
    require(manifest["status"] == "COMPLETE", "TASK962_REAL_PILOT_INCOMPLETE")
    name = "filing_results.json"
    _, filings = task9.checked(PILOT_MANIFEST.parent / name, manifest["outputs"][name])
    require(
        set(filings) == DIAGNOSES.keys()
        and all(
            row["extraction_status"] == "EXTRACTION_COMPLETE"
            and row["dts_status"] == "DTS_COMPLETE"
            for row in filings.values()
        ),
        "TASK962_REAL_PILOT_FILINGS_NOT_COMPLETE",
    )
    return {key(row): row for row in filings.values()}, PILOT_MANIFEST_SHA


class Backfill:
    def __init__(self, root, run, plan):
        self.root, self.run, self.plan = root, run, plan
        self.scope = scope_for(plan)
        self.pilot = select_pilot(self.scope)
        self.dirs = {
            name: task9.filing.safe_directory(root / name)
            for name in ("originals", "receipts", "state", "cov", "responses")
        }
        scope = {
            "contract": CONTRACT,
            "plan": plan.binding(),
            "filings": list(self.scope.values()),
            "pilot": self.pilot,
        }
        self.scope_hash = publish(root / "scope.json", scope)
        paths = [
            Path(__file__),
            Path(reporting.__file__),
            REPO / "scripts/f01_task97_taxonomy.py",
            REPO / "scripts/run_f01_task962.py",
        ]
        code = fingerprint({"task96": current_code(), **{p.name: digest(p) for p in paths}})
        self.binding = {
            "contract": CONTRACT,
            "scope_fingerprint": self.scope_hash,
            "code_fingerprint": code,
            "plan": plan.binding(),
            "arelle": "2.47.5",
            "limits": LIMITS,
            "authorization": "EXPLICIT_TASK97_INVOCATION",
            "fact_alias_sha256": digest(REPO / "src/trading_system/data/xbrl_parser.py"),
        }
        self.budget = Budget(run / "budget.json", self.binding, LIMITS)
        self.records, self.originals, self.metadata = {}, {}, {}
        self.downloaded, self.extracted = set(), set()
        self.sizes, self.batches, self.taxonomy_events = [], [], []
        self.denied = set()
        self.taxonomy_attempted, self.taxonomy_unavailable = set(), set()
        self.agent = None
        self.pilot_result = None

    def state_path(self, pair):
        return self.dirs["state"] / ("-".join(pair) + ".json")

    def checkpoint(self, pair):
        record = self.records[pair]
        replace_checkpoint(
            self.state_path(pair),
            {
                "binding": self.binding,
                "filing": list(pair),
                "record": {k: v for k, v in record.items() if k != "summary"},
            },
        )

    def remember_original(self, pair, source, receipt):
        self.originals[pair] = {"status": "ORIGINAL_VERIFIED", "source": source}
        self.records[pair] = {
            "original_status": "ORIGINAL_VERIFIED",
            "original_receipt": receipt,
            "report_date": self.metadata[pair]["report_date"],
        }
        self.sizes.append(sum(item["bytes"] for item in source["documents"]))

    def attach_processing(self, pair, item, *, new=False):
        record = self.records[pair]
        if item.get("extraction_status") == "EXTRACTION_COMPLETE":
            require(
                not new or item["source_sha256"] == fingerprint(self.originals[pair]["source"]),
                "TASK97_EXTRACTION_ORIGINAL_BINDING_CHANGED",
            )
            _, dts = task9.checked(Path(item["closure_path"]), item["closure_sha256"])
            require(
                dts["status"] == "TAXONOMY_CLOSURE_READY"
                and dts["arelle_version"] == task9.filing.ARELLE_VERSION,
                "TASK97_EXTRACTION_DTS_EVIDENCE_CHANGED",
            )
            summary = reporting.read_summary(
                self.dirs["cov"],
                reporting.extraction_path(item),
                item["shard_sha256"],
                technical=True,
                accounting_path=Path(item["accounting_path"]),
                accounting_sha=item["accounting_sha256"],
            )
            record.update(item | {"summary": summary, "dts_status": "DTS_COMPLETE"})
        else:
            ready = False
            if item.get("closure_path"):
                _, dts = task9.checked(Path(item["closure_path"]), item["closure_sha256"])
                ready = dts["status"] == "TAXONOMY_CLOSURE_READY"
            record.update(
                item
                | {
                    "extraction_status": "NOT_COMPLETE",
                    "dts_status": "DTS_COMPLETE" if ready else "DTS_INCOMPLETE",
                    "reason": item.get("reason", item["status"]),
                }
            )
        if new:
            self.checkpoint(pair)

    def initialize(self):
        self.metadata, blocked = self.plan.metadata(self.plan.filings.values())
        inventory, _, stats = workflow.inventory(
            self.plan,
            progress=workflow.progress,
            metadata=self.metadata,
        )
        self.binding["baseline_inventory"] = stats
        pilot, sha = verified_pilot()
        self.binding["task962_manifest_sha256"] = sha
        roots = self.dirs["originals"]
        for pair, row in self.scope.items():
            item = inventory[pair]
            self.records[pair] = {"original_status": item["status"]}
            if row["scope"] == "SEPARATE_REVIEW":
                self.records[pair]["reason"] = "METADATA_OR_AMENDMENT_REVIEW"
                continue
            if pair in blocked:
                self.records[pair]["reason"] = blocked[pair]
                continue
            receipt_path = self.dirs["receipts"] / ("-".join(pair) + ".json")
            if item["status"] == "ORIGINAL_VERIFIED":
                known = item["source"]
                source = {"directory": known["directory"], "documents": known["documents"]}
                self.remember_original(
                    pair,
                    source,
                    {"sha256": known["original_binding_sha256"], "origin": "TASK96_INVENTORY"},
                )
            elif receipt_path.exists():
                raw, receipt = task9.checked(receipt_path)
                require(
                    receipt["scope_fingerprint"] == self.scope_hash
                    and receipt["filing"] == list(pair),
                    "TASK97_RECEIPT_SCOPE_CHANGED",
                )
                source = checked_original(
                    roots.joinpath(*pair), self.metadata[pair], receipt["original"]["documents"]
                )
                self.remember_original(
                    pair, source, {"sha256": task9.sha(raw), "path": str(receipt_path)}
                )
            if pair in pilot and pair in self.originals:
                require(
                    pilot[pair]["source_sha256"] == fingerprint(self.originals[pair]["source"]),
                    "TASK962_SHARD_SOURCE_CHANGED",
                )
                self.attach_processing(pair, pilot[pair])
            elif item.get("extraction") and pair in self.originals:
                old = item["extraction"]
                binding = old.get("taxonomy_evidence") or {}
                local = binding.get("status") == "LOCAL_BOUND_DEPENDENCIES_MATCH"
                summary = reporting.read_summary(
                    self.dirs["cov"], Path(old["path"]), old["sha256"], technical=False
                )
                self.records[pair].update(
                    extraction_status="EXTRACTION_REUSED_ARCHIVED",
                    summary=summary,
                    dts_status="DTS_ARCHIVED_BOUND" if local else "DTS_UNVERIFIED",
                    reused_dts=binding,
                    reused_shard=old,
                    archived_accounting=item["archived_admission"],
                )
            if row["scope"] == "BACKFILL":
                self.resume_filing(pair)

    def resume_filing(self, pair):
        path = self.state_path(pair)
        if not path.exists():
            return
        _, checkpoint = task9.checked(path)
        require(
            checkpoint["filing"] == list(pair)
            and checkpoint["binding"]["scope_fingerprint"] == self.scope_hash,
            "TASK97_FILING_CHECKPOINT_CHANGED",
        )
        old = checkpoint["record"]
        if old.get("original_terminal") and pair not in self.originals:
            self.records[pair].update(old)
        if (
            pair in self.originals
            and old.get("extraction_status") == "EXTRACTION_COMPLETE"
            and checkpoint["binding"]["code_fingerprint"] == self.binding["code_fingerprint"]
        ):
            require(
                old["source_sha256"] == fingerprint(self.originals[pair]["source"]),
                "TASK97_CHECKPOINT_ORIGINAL_BINDING_CHANGED",
            )
            self.attach_processing(pair, old)

    def progress(self, batch, pair=None):
        targets = [p for p, row in self.scope.items() if row["scope"] == "BACKFILL"]
        have = sum(p in self.originals for p in targets)
        facts = sum(self.records[p].get("summary", {}).get("raw_facts", 0) for p in self.extracted)
        closure = sum(self.records[p].get("dts_status") == "DTS_COMPLETE" for p in targets)
        blocked = sum(bool(self.records[p].get("reason")) for p in targets)
        workflow.progress(
            f"Task 9.7 | Batch {batch} | Ziel {len(targets)} | Originale {have} "
            f"(neu {len(self.downloaded)}) | DTS {closure} "
            f"| Extraktionen neu {len(self.extracted)} "
            f"| Rohfacts neu {facts} | Blocker {blocked} | HTTP {self.budget.state['requests']} "
            f"| Bytes {self.budget.state['received_bytes']} "
            f"| Frei {shutil.disk_usage(self.root).free}"
            + (f" | {self.scope[pair]['symbol_label']} {pair[1]}" if pair else "")
        )

    def user_agent(self):
        if self.agent is None:
            try:
                self.agent = load_settings().require_sec_user_agent()
            except (ValueError, RuntimeError) as exc:
                raise ValueError("SEC_USER_AGENT_CONFIGURATION_REQUIRED") from exc
        return self.agent

    def transport(self, opener):
        return NetworkGuard(
            self.root / "clock.json", opener, disk=lambda: shutil.disk_usage(self.root).free
        )

    def acquisition(self, pairs):
        pending = [
            pair
            for pair in pairs
            if pair in self.metadata
            and pair not in self.originals
            and self.records[pair]["original_status"] == "ORIGINAL_MISSING"
        ]
        if not pending:
            return
        with task9.network(self.user_agent()) as client:
            guard = self.transport(client.opener)
            guard.check()
            client.opener = CountedSecOpener(
                FilingJournal(self.run, guard, self.budget), self.budget
            )
            for index, pair in enumerate(pending):
                if index % 5 == 0:
                    self.progress(len(self.batches), pair)
                try:
                    source = download_sec_filing(
                        client, self.dirs["originals"], self.metadata[pair]
                    )
                except ERRORS as exc:
                    reason = str(exc)
                    terminal = any(
                        token in reason
                        for token in (
                            "SEC_HTTP_404",
                            "SEC_HTTP_410",
                            "INVALID_SEC_DOCUMENT",
                            "CACHE_",
                            "SEC_LISTING_",
                            "SEC_DOCUMENT_COUNT_LIMIT",
                        )
                    )
                    self.records[pair].update(reason=reason, original_terminal=terminal)
                    if terminal:
                        self.records[pair]["original_status"] = "ORIGINAL_SOURCE_BLOCKED"
                    self.checkpoint(pair)
                    if pause_status(exc):
                        raise
                else:
                    receipt = self.dirs["receipts"] / ("-".join(pair) + ".json")
                    sha = publish(
                        receipt,
                        {
                            "contract": CONTRACT,
                            "scope_fingerprint": self.scope_hash,
                            "filing": list(pair),
                            "original": source,
                        },
                    )
                    self.remember_original(pair, source, {"sha256": sha, "path": str(receipt)})
                    self.downloaded.add(pair)

    def processing(self, pairs, cache, *, allow_network=True):
        pairs = [
            p
            for p in pairs
            if p in self.originals
            and self.records[p].get("extraction_status") != "EXTRACTION_COMPLETE"
        ]
        if not pairs:
            return

        def process():
            result = progress_for(
                self.scope_hash,
                [self.metadata[p] for p in pairs],
                self.originals,
                process=True,
                taxonomy_cache=cache,
                shard_root=self.root / "dts",
            )
            for item in result["filings"]:
                pair = key(item)
                self.attach_processing(pair, item, new=True)
                if item.get("extraction_status") == "EXTRACTION_COMPLETE":
                    self.extracted.add(pair)
            return result

        result = process()
        if not allow_network or not missing_sources(result):
            return
        with task9.stage_lock(cache / ".network.lock"), task9.network(self.user_agent()) as client:
            batch = TaxonomyBatch(
                self.run,
                self.dirs["responses"],
                cache,
                self.budget,
                self.transport(client.opener),
                self.agent,
                self.denied,
            )
            batch.attempted, batch.unavailable = self.taxonomy_attempted, self.taxonomy_unavailable
            try:
                while batch.bind(result):
                    result = process()
            finally:
                self.taxonomy_events.extend(batch.events)

    def run_batches(self):
        self.initialize()
        cache = tax.safe_root()
        gate_path = self.root / "pilot.json"
        target = [p for p, row in self.scope.items() if row["scope"] == "BACKFILL"]
        first = [
            p
            for p in self.pilot
            if self.records[p].get("extraction_status") != "EXTRACTION_COMPLETE"
        ]
        pilot_http = Counter()
        if gate_path.exists():
            _, prior = task9.checked(gate_path)
            require(prior["scope_fingerprint"] == self.scope_hash, "TASK97_PILOT_SCOPE_CHANGED")
            pilot_http.update(prior.get("http_statuses", {}))
        sequence = [first, [p for p in target if p not in self.pilot]]
        for phase, remaining in enumerate(sequence):
            remaining = [
                p
                for p in remaining
                if p in self.metadata
                and self.records[p]["original_status"] in {"ORIGINAL_VERIFIED", "ORIGINAL_MISSING"}
                and self.records[p].get("extraction_status") != "EXTRACTION_COMPLETE"
            ]
            while remaining:
                size = capacity(self.root, self.dirs["originals"], self.sizes)
                require(size["batch_capacity"] > 0, "STORAGE_BLOCKED")
                # Process cache hits and same-year dependencies close together after the pilot.
                if phase:
                    remaining.sort(
                        key=lambda p: (
                            p not in self.originals,
                            self.scope[p]["filed"][:4],
                            p,
                        )
                    )
                pairs, remaining = (
                    remaining[: size["batch_capacity"]],
                    remaining[size["batch_capacity"] :],
                )
                self.batches.append(
                    {
                        "phase": "PILOT" if not phase else "BULK",
                        "filings": ["/".join(p) for p in pairs],
                        **size,
                    }
                )
                before = Counter(self.budget.state["statuses"])
                bytes_before = self.budget.state["received_bytes"]
                try:
                    self.acquisition(pairs)
                except ERRORS as exc:
                    # Already verified members of the current batch remain usable offline.
                    if pause_status(exc) != "STORAGE_BLOCKED":
                        self.processing(pairs, cache, allow_network=False)
                    raise
                after = Counter(self.budget.state["statuses"])
                if not phase:
                    pilot_http.update(after - before)
                self.processing(pairs, cache)
                self.batches[-1].update(
                    originals_verified=sum(p in self.originals for p in pairs),
                    raw_facts=sum(
                        self.records[p].get("summary", {}).get("raw_facts", 0) for p in pairs
                    ),
                    extracted=sum(
                        self.records[p].get("extraction_status") == "EXTRACTION_COMPLETE"
                        for p in pairs
                    ),
                    downloaded_bytes=self.budget.state["received_bytes"] - bytes_before,
                    free_bytes_after=shutil.disk_usage(self.root).free,
                )
                self.progress(len(self.batches))
            if not phase:
                gate = pilot_gate(self.pilot, self.records, pilot_http)
                receipt = gate | {
                    "scope_fingerprint": self.scope_hash,
                    "http_statuses": dict(pilot_http),
                }
                self.pilot_result = receipt
                publish(self.run / "pilot.json", receipt)
                replace_checkpoint(gate_path, receipt)
                if not gate["passed"]:
                    return "PARTIAL_BLOCKED", f"PILOT_GATE_NOT_MET: {gate}"
        if any(item.get("receipt") and not item.get("reason") for item in self.taxonomy_events):
            incomplete = [
                p
                for p in target
                if p in self.originals
                and self.records[p].get("extraction_status") != "EXTRACTION_COMPLETE"
            ]
            for start in range(0, len(incomplete), PILOT_SIZE):
                self.processing(incomplete[start : start + PILOT_SIZE], cache, allow_network=False)
        if all(self.records[p].get("extraction_status") == "EXTRACTION_COMPLETE" for p in target):
            return "COMPLETE", None
        if all(p in self.originals for p in target):
            return "ACQUISITION_COMPLETE_PROCESSING_PARTIAL", "PROCESSING_BLOCKERS"
        return "PARTIAL_BLOCKED", "REMAINING_ORIGINAL_OR_DTS_BLOCKERS"

    def finish(self, status, reason):
        metrics = {
            "run_directory": str(self.run),
            "budget": self.budget.state,
            "batches": self.batches,
            "new_originals": len(self.downloaded),
            "new_extractions": len(self.extracted),
            "space": capacity(self.root, self.dirs["originals"], self.sizes),
            "taxonomy_attempts": self.taxonomy_events,
            "pilot": self.pilot_result,
            "denied_taxonomy_hosts": sorted(self.denied),
            "request_journal_sha256": {
                path.name: digest(path) for path in sorted(self.run.glob("req-*.json"))
            },
        }
        return reporting.report(
            self.root, self.binding, self.scope, self.records, metrics, status, reason
        )


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    status, reason = "FAILED", None
    try:
        root = task9.filing.safe_directory(OUTPUT)
        with task9.stage_lock(root / ".run.lock"):
            run = task9.filing.safe_directory(root / f"r-{uuid4().hex[:16]}")
            backfill = Backfill(root, run, workflow.Plan())
            try:
                status, reason = backfill.run_batches()
            except KeyboardInterrupt:
                status, reason = "PARTIAL_BLOCKED", "INTERRUPTED"
            except ERRORS as exc:
                status, reason = pause_status(exc) or "FAILED", str(exc)
            report = backfill.finish(status, reason)
            answer = {
                "status": status,
                "reason": reason,
                "manifest": report["directory"],
                "manifest_sha256": report["manifest_sha256"],
            }
    except ERRORS as exc:
        status = pause_status(exc) or "FAILED"
        answer = {"status": status, "reason": str(exc)}
    print(workflow.encoding(answer).decode(), end="")
    return 0 if status == "COMPLETE" else 3 if status == "FAILED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
