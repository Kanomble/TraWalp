#!/usr/bin/env python3
"""Targeted, read-only F01 G4 evidence recovery; no database access on import."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
import types
import uuid
from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))
CONTRACT = "F01_G4_SUPPLEMENT_V1"
ADDITIONS = frozenset({
    "scripts/validate_f01_g4_supplement.py", "docs/f01_g4_supplement.md",
    "tests/test_validate_f01_g4_supplement.py", "reports/f01_g4_evidence_recovery_audit.md",
})


def acceptance_tools():
    path = ROOT / "scripts/validate_f01_migration.py"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != "9800ff57fb82cba24f7c514ca251b231cfe8b8d13a6528342046645cb784f785":
        raise RuntimeError("SEALED_ACCEPTANCE_SCRIPT_DRIFT_BEFORE_IMPORT")
    module = types.ModuleType("_f01_g4_original_acceptance")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(raw, str(path), "exec"), vars(module))
    return module


old = acceptance_tools()
g3, digest, require, equal = old.g3, old.digest, old.require, old.equal
GIB = 1024**3


@dataclass(frozen=True)
class Policy(old.Policy):
    supplement_review: Path = ROOT / "reports/f01_g4_supplement_additions_review.json"


def serial(value):
    return json.loads(json.dumps(old.plain(value), default=str, allow_nan=False))


def verify_metadata(policy, g3_dir, g4_dir, monitor):
    """Reuse the immutable input gate with a private additive union.

    Neither original approval is modified, including in the G4 binding. Only the
    private G3 gate receives a union of independently checked reviews.
    No database hash, connection, census or acceptance phase occurs here.
    """
    seal = g3.read_json(policy.verified / "seal.json")
    mh = seal["f01_copy_rehearsal_manifest.json"]
    result, g3_fp, g3_seal = old.g3_result(g3_dir, mh, monitor)
    g3_review = g3.read_json(policy.approval)
    g4_review = g3.read_json(policy.acceptance_review)
    review = g3.read_json(policy.supplement_review)
    require(set(g3_review["files"]) == g3.ADDITIONS, "ORIGINAL_G3_REVIEW_DRIFT")
    require(set(g4_review["files"]) == old.ADDITIONS
            and g4_review.get("review_status") == "REVIEWED_ADDITIVE_ONLY"
            and g4_review.get("reviewer") and g4_review.get("manifest_sha256") == mh
            and g4_review.get("base_git_head") == g3_review.get("base_git_head")
            and g4_review.get("g3_result_fingerprint") == g3_fp, "ORIGINAL_G4_REVIEW_DRIFT")
    g4_fp = g3.read_json(g4_dir / "result_fingerprint.json")["report_fingerprint"]
    require(review.get("review_status") == "REVIEWED_ADDITIVE_ONLY" and review.get("reviewer")
            and review.get("manifest_sha256") == mh and review.get("g3_result_fingerprint") == g3_fp
            and review.get("g4_report_fingerprint") == g4_fp
            and review.get("base_git_head") == g3_review.get("base_git_head")
            and set(review.get("files", {})) == ADDITIONS, "SUPPLEMENT_REVIEW_MISSING_OR_DRIFT")
    additions = {**g3_review["files"], **g4_review["files"], **review["files"]}
    for name, expected in additions.items():
        require(g3.sha(g3.bound_repo_path(policy.root, name), monitor) == expected,
                "REVIEWED_ADDITIVE_DRIFT: " + name)
    combined = {**g3_review, "files": additions}
    namespace = dict(vars(g3), ADDITIONS=frozenset(additions))
    namespace["read_json"] = lambda p: combined if p == policy.approval else g3.read_json(p)
    for name in ("git_changes", "verify_bundle"):
        fn = getattr(g3, name)
        namespace[name] = types.FunctionType(fn.__code__, namespace, name, fn.__defaults__, fn.__closure__)
    m, prep, base_binding = namespace["verify_bundle"](policy, monitor)
    require(result["binding"]["reviewed_additions"] == g3_review
            and result["binding"]["seal"] == base_binding["seal"]
            and result["work_path"] == m["work"], "G3_BINDING_DRIFT")
    counts = result["validation"]["counts"]
    require(set(m["expected_counts"]) - {"content_conflicts"} <= set(counts)
            and all(v == m["expected_counts"].get(k) for k, v in counts.items()), "G3_COUNT_DRIFT")
    require(all(result["validation"].get(k) is True for k in (
        "original_ids_preserved", "one_mapping_per_original", "executor_full_legacy_forward_stream_check",
        "executor_integrity_and_fk_check")), "G3_STREAM_PROOF_MISSING")
    original_binding = {
        "contract": old.CONTRACT, "manifest_sha256": mh, "g3_result_fingerprint": g3_fp,
        "g3_seal": g3_seal, "work_sha256": result["work_sha256"],
        "original_code_fingerprint": m["input_inventory"]["repository"]["code_fingerprint"],
        "review_sha256": g3.sha(policy.acceptance_review, monitor),
        "additions": {**g3_review["files"], **g4_review["files"]},
    }
    report = old.read_phase(g4_dir, "g4", original_binding, monitor)
    require(report["status"] in {"PASS", "INSUFFICIENT_EVIDENCE"}, "ORIGINAL_G4_NOT_REUSABLE")
    binding = {
        "contract": CONTRACT, "original_g4_binding": original_binding,
        "g4_report_fingerprint": g4_fp, "g4_seal": g3.read_json(g4_dir / "result_seal.json"),
        "manifest_sha256": mh, "g3_result_fingerprint": g3_fp, "work_sha256": result["work_sha256"],
        "original_code_fingerprint": original_binding["original_code_fingerprint"],
        "additions": additions, "supplement_review_sha256": g3.sha(policy.supplement_review, monitor),
    }
    return m, prep, report, binding


def protected_paths(policy, g3_dir, g4_dir, manifest):
    paths = old.protection_paths(policy, g3_dir, manifest)
    more = [policy.supplement_review, *(policy.root / name for name in ADDITIONS)]
    more += [g4_dir / name for name in ("result_seal.json", "result_fingerprint.json",
        "f01_migration_acceptance_g4.json", "f01_migration_acceptance_audit.md", "progress.jsonl")]
    paths.update({"supplement_" + str(i): g3.safe(p) for i, p in enumerate(more)})
    return paths


def reused_evidence(report):
    require(report.get("matrix") and report.get("non_fact_tables") and report.get("full_stream_proof"),
            "MISSING_SUCCESSFUL_G4_EVIDENCE")
    return {"pit_accounting_comparisons": len(report["matrix"]),
            "non_fact_tables": sorted(report["non_fact_tables"]),
            "observed_metrics": report.get("observed_metrics", []), "witnesses": report.get("witnesses", []),
            "original_g4_report_fingerprint": digest(report),
            "reran_pit_accounting_matrix": False, "reran_non_fact_inventory": False,
            "reran_g3_streams_or_ledger": False}


def original_session(report, mode):
    sessions = {r["session"] for r in report["scores"] if r["reader"] == mode}
    require(len(sessions) == 1, "ORIGINAL_G4_SESSION_UNCLEAR: " + mode)
    return date.fromisoformat(sessions.pop())


def report_data(report):
    return report.model_dump(mode="json", exclude={"generated_at"})


def score_context(prepared, conflicted, config, session, *, forward, history=None):
    """Actual production peers, scorer, F evaluator and fail-closed qualification."""
    from trading_system.backtest.engine import evaluate_variant_entry
    from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
    from trading_system.champion import champion_ranking
    from trading_system.shadow.qualification import qualified_forward_screen
    from trading_system.strategy.screener import Screener

    prepared_inputs = {p.company.symbol: serial(p) for p in prepared}
    scorer = Screener(None, config)
    peer_table = scorer._peer_table(prepared)
    peer_rows = peer_table.astype(object).where(peer_table.notna(), None).to_dict("records")
    raw = scorer.report_from_prepared(prepared, conflicted, requested_as_of=session, market_session=session)
    if forward:
        require(history is not None, "FORWARD_HISTORY_EVIDENCE_REQUIRED")
        ranked, qualification = qualified_forward_screen(raw, session, config, history_coverage=history)
        ranking_valid = qualification["unresolved_rank_relevant_count"] == 0
    else:
        ranked, qualification, ranking_valid = champion_ranking(raw, config), None, True
    evaluations = {r.symbol: serial(evaluate_variant_entry(r, FROZEN_CHAMPION_F.variant, config)) for r in raw.records}
    def ranking(report):
        return [{"symbol": r.symbol, "rank": r.rank, "score": r.scores.total}
                for r in report.records if r.rank is not None]
    return {
        "prepared": prepared_inputs,
        "peer_table": serial(peer_rows), "quality_value_comparison": report_data(raw),
        "f_evaluations_diagnostic": evaluations, "f_comparison": report_data(ranked),
        "qualification": qualification, "history": serial(history), "ranking_valid": ranking_valid,
        "quality_value_ranking": ranking(raw) if ranking_valid else [],
        "f_ranking": ranking(ranked) if ranking_valid else [],
        "raw_records_are_diagnostic_when_unqualified": not ranking_valid,
        "exclusions": dict(sorted(Counter(reason for row in raw.records for reason in row.exclusion_reasons).items())),
    }


def compare_context(left, right, mode):
    require(set(left) == set(right), "PARITY_DIFFERENCE: " + mode + "/context_fields")
    for key in left:
        if digest(left[key]) != digest(right[key]):
            detail = first_difference(left[key], right[key])
            raise g3.Blocked(f"PARITY_DIFFERENCE: {mode}/{key}/{detail}")


def first_difference(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return path + "/keys"
        for key in sorted(left):
            if digest(left[key]) != digest(right[key]):
                return first_difference(left[key], right[key], path + "/" + key)
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path}/length: {len(left)} != {len(right)}"
        for i, (a, b) in enumerate(zip(left, right, strict=True)):
            if digest(a) != digest(b):
                return first_difference(a, b, path + "/" + str(i))
    return path + ": " + repr(left)[:160] + " != " + repr(right)[:160]


def forward_diagnostics(c, context, session, config):
    from trading_system.data.market_sessions import full_history_request_window
    start, _ = full_history_request_window(session, config.data_quality.min_market_history_days)
    diagnostics = []
    by_symbol = {r["symbol"]: r for r in context["quality_value_comparison"]["records"]}
    present = c.execute("SELECT 1 FROM main.sqlite_master WHERE name='forward_market_observations'").fetchone()
    for missing in context["qualification"]["incomplete_inputs"]:
        symbol = missing["symbol"]
        if symbol is None:
            diagnostics.append(missing)
            continue
        raw = by_symbol[symbol]
        groups = [dict(r) for r in c.execute(
            "SELECT provider,feed,adjustment,timeframe,COUNT(*) AS rows,MIN(session) AS first_session,"
            "MAX(session) AS last_session,SUM(volume=0) AS zero_volume_rows,"
            "SUM(NOT (session=substr(timestamp,1,10) AND retrieved_at<>'' AND sync_reference<>'' "
            "AND forward_provenance_valid(timestamp,session,retrieved_at)=1)) AS invalid_provenance_rows "
            "FROM main.forward_market_observations WHERE symbol=? AND session>=? AND session<=? "
            "GROUP BY provider,feed,adjustment,timeframe ORDER BY provider,feed,adjustment,timeframe",
            (symbol, start.date().isoformat(), session.isoformat()))] if present else []
        diagnostics.append({**missing, "history": context["history"].get(symbol),
            "prepared": context["prepared"].get(symbol), "raw_observation_groups": groups,
            "quality_availability": raw["scores"]["quality"], "valuation_availability": raw["scores"]["valuation"],
            "peer_group": raw["peer_group"], "industry_medians": raw["industry_medians"],
            "classification": "FROZEN_INPUT_OR_QUALIFICATION_GAP; not a migration difference when parity holds"})
    return diagnostics


def ranking_phase(m, original, monitor):
    from trading_system.config import load_strategy_config
    from trading_system.fundamentals.peers import peer_diagnostics
    from trading_system.strategy.screener import Screener

    config = load_strategy_config()
    modes, clears, gaps = {}, [], []
    for forward in (False, True):
        mode = "forward_cik_sip" if forward else "legacy_ticker"
        session = original_session(original, mode)
        contexts, peer_debug = [], []
        universe = None
        source_diagnostics = []
        for side in ("source", "work"):
            with old.reader(Path(m[side]), monitor, forward) as c:
                db = old.frozen_database(c, Path(m[side]), forward)
                companies = sorted(db.list_tradable_companies(), key=lambda x: (x.symbol, x.cik))
                conflicts = db.unresolved_sec_identity_conflict_symbols()
                current_universe = {"companies": [serial(x) for x in companies], "conflicts": sorted(conflicts),
                                    "session": str(session), "selection": "ALL_PRODUCTION_TRADABLE_COMPANIES"}
                if universe is None:
                    universe = current_universe
                    if not forward:
                        monitor.expected_probes = 4 * sum(co.symbol not in conflicts for co in companies)
                else:
                    equal(universe, current_universe, mode + "/universe")
                scorer, prepared = Screener(db, config), []
                for company in companies:
                    monitor.tick(f"Ranking {mode}/{side}: {company.symbol}")
                    if company.symbol in conflicts:
                        continue
                    candidate = scorer._prepare(company, session, use_market_snapshots=False, connection=c)
                    require("analysis_error" not in candidate.base_exclusions,
                            "PRODUCTION_ANALYSIS_ERROR: " + company.symbol)
                    prepared.append(candidate)
                    monitor.completed_probes += 1
                monitor.tick(f"Ranking {mode}/{side}: vollständige Peers und SIP-History")
                history = db.history_coverage([co.symbol for co in companies], session,
                        config.data_quality.min_market_history_days) if forward else None
                context = score_context(prepared, [co for co in companies if co.symbol in conflicts],
                                        config, session, forward=forward, history=history)
                if side == "source":
                    frame = scorer._peer_table(prepared)
                    full_rows = {r["symbol"]: r for r in context["quality_value_comparison"]["records"]}
                    sample = next(r for r in original["scores"] if r["reader"] == mode and r["kind"] == "quality_value")
                    for row in sample["records"]:
                        debug = peer_diagnostics(frame, row["symbol"], row["sic"], config.peers.min_peer_count)
                        full = full_rows[row["symbol"]]
                        base_exclusions = context["prepared"].get(row["symbol"], {}).get("base_exclusions", [])
                        classification = ("SAMPLE_CONTEXT_LIMITATION"
                            if row["peer_group"] is None and full["peer_group"] is not None
                            else "FROZEN_BASE_EXCLUSION" if base_exclusions or row["symbol"] in conflicts
                            else "FULL_CONTEXT_PEERS_OR_METRICS_INSUFFICIENT")
                        peer_debug.append({"symbol": row["symbol"], "prior_peer_group": row["peer_group"],
                            "actual_full_peer_group": full["peer_group"], "base_exclusions": base_exclusions,
                            "full_context": serial(debug), "classification": classification,
                            "prior_quality_unavailable": row["scores"]["quality"]["score"] is None,
                            "full_quality_unavailable": full["scores"]["quality"]["score"] is None,
                            "prior_value_unavailable": row["scores"]["valuation"]["score"] is None,
                            "full_value_unavailable": full["scores"]["valuation"]["score"] is None})
                diagnostics = forward_diagnostics(c, context, session, config) if forward else []
                contexts.append(context)
                if side == "source":
                    source_diagnostics = diagnostics
                else:
                    equal(source_diagnostics, diagnostics, mode + "/forward_input_diagnostics")
        compare_context(*contexts, mode)
        left = contexts[0]
        for kind, key in (("quality_value", "quality_value_ranking"), ("F", "f_ranking")):
            if left["ranking_valid"] and left[key] and left[key][0]["rank"] == 1:
                clears.append(f"{mode}/{kind}: no rank-1 witness in fixed cross-section")
            else:
                gaps.append(f"{mode}/{kind}: no qualified real rank-1 witness at bound session {session}")
        if forward and left["ranking_valid"]:
            clears.append("forward_cik_sip: frozen forward inputs are unresolved for ranking")
        elif forward:
            gaps.append("forward_cik_sip: unresolved ranking inputs persist in complete frozen universe")
        modes[mode] = {"universe": universe, "universe_fingerprint": digest(universe), "parity": True,
            "source_context_fingerprint": digest(left), "work_context_fingerprint": digest(contexts[1]),
            "evidence": left, "forward_input_diagnostics": source_diagnostics,
            "original_sample_peer_diagnostics": peer_debug}
    return {"status": "PASS" if not gaps else "INSUFFICIENT_EVIDENCE", "clears": clears, "gaps": gaps,
            "modes": modes, "config_fingerprint": digest(serial(config)), "strategy_changed": False,
            "selection_uses_work_scores": False}


def classify_identities(pairs, epochs):
    """Fact dates are provenance, not effective ticker ownership dates."""
    from trading_system.data.security_identity import SymbolIdentityEpoch, current_identity_epoch
    by_symbol, by_cik, epoch_groups, invalid = defaultdict(list), defaultdict(set), defaultdict(list), set()
    for row in pairs:
        by_symbol[row["symbol"]].append(row)
        by_cik[row["cik"]].add(row["symbol"])
    for row in epochs:
        try:
            epoch = SymbolIdentityEpoch.from_record(row)
            if epoch.verified:
                epoch_groups[epoch.symbol].append(epoch)
        except (ValueError, TypeError, KeyError, AttributeError):
            invalid.add(row.get("symbol"))
    result = []
    for symbol in sorted(set(by_symbol) | set(epoch_groups) | invalid):
        rows = by_symbol[symbol]
        ciks = sorted({r["cik"] for r in rows})
        verified = sorted(epoch_groups[symbol], key=lambda e: (e.effective_start, e.cik))
        transitions = []
        if symbol not in invalid:
            for before, after in zip(verified, verified[1:]):
                identities = [{"symbol": symbol, "cik": e.cik, "stable_security_identity": e.stable_security_identity} for e in (before, after)]
                exact = all(current_identity_epoch(identity, [e.record() for e in verified]) is not None for identity in identities)
                if exact and before.cik != after.cik and before.cik in ciks and after.cik in ciks and before.effective_end and before.effective_end <= after.effective_start:
                    transitions.append({"before": before.record(), "after": after.record()})
        classification = ("VERIFIED_TICKER_REUSE" if transitions else "UNRESOLVED_MULTI_CIK_PROVENANCE"
            if len(ciks) > 1 else "ALIAS_OR_SHARE_CLASS" if len(ciks) == 1 and len(by_cik[ciks[0]]) > 1
            else "SINGLE_CIK_PROVENANCE")
        result.append({"symbol": symbol, "ciks": ciks, "classification": classification,
            "fact_provenance": rows, "verified_transitions": transitions,
            "invalid_epoch_evidence": symbol in invalid})
    return result


def reader_witness(source, work, symbol, days, monitor):
    witnesses = []
    for forward in (False, True):
        with old.reader(source, monitor, forward) as a, old.reader(work, monitor, forward) as b:
            dbs = [old.frozen_database(c, p, forward) for c, p in ((a, source), (b, work))]
            for day in sorted(set(days)):
                monitor.tick(f"Identity {'forward' if forward else 'legacy'}: {symbol}/{day}")
                facts = [db.facts_available_as_of(symbol, day) for db in dbs]
                equal([[serial(f) for f in stream] for stream in facts[:1]],
                      [[serial(f) for f in stream] for stream in facts[1:]], "identity_reader/" + symbol)
                resolved = dbs[0].provenance["identity_inputs"].get("fundamental_cik|" + symbol) if forward else None
                if forward and (resolved is None or symbol in dbs[0].unresolved_sec_identity_conflict_symbols()):
                    require(not facts[0], "FORWARD_CIK_FALLBACK")
                witnesses.append({"reader": "forward_cik" if forward else "legacy_ticker", "symbol": symbol,
                    "as_of": str(day), "resolved_cik": resolved, "facts": len(facts[0]),
                    "observed_ciks": sorted({f.cik for f in facts[0]}), "stream_fingerprint": digest([serial(f) for f in facts[0]])})
    return witnesses


def identity_census(c, monitor, expected):
    pairs = {}
    monitor.completed_probes = 0
    monitor.expected_probes = expected
    monitor.tick("Identity-Census: vollständiger Symbol-/CIK-Index, ohne Fact-Wertvergleich")
    query = ("SELECT o.symbol,f.cik,f.filed FROM source_occurrence o "
        "INDEXED BY occurrence_legacy_reader CROSS JOIN canonical_fact f ON f.fact_id=o.fact_id "
        "WHERE o.legacy_admitted=1 ORDER BY o.symbol,o.fact_id")
    plan = [tuple(r) for r in c.execute("EXPLAIN QUERY PLAN " + query)]
    require(not any("TEMP B-TREE" in str(r).upper() for r in plan), "IDENTITY_CENSUS_REQUIRES_UNBOUNDED_SORT")
    for symbol, cik, filed in c.execute(query):
        key = (symbol, cik)
        if key not in pairs:
            pairs[key] = {"symbol": symbol, "cik": cik, "occurrences": 0, "first_filed": filed, "last_filed": filed}
        item = pairs[key]
        item["occurrences"] += 1
        item["first_filed"], item["last_filed"] = min(item["first_filed"], filed), max(item["last_filed"], filed)
        monitor.completed_probes += 1
        if monitor.completed_probes % 10000 == 0:
            monitor.check()
    require(sum(p["occurrences"] for p in pairs.values()) == expected,
            "IDENTITY_CENSUS_OCCURRENCE_COVERAGE_MISMATCH")
    return pairs, plan


def identity_phase(m, original, monitor):
    with old.reader(Path(m["work"]), monitor) as c:
        # Bounded streaming memory, no full-value comparison or SQL sort.
        pairs, plan = identity_census(c, monitor, m["expected_counts"]["occurrences"])
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        epochs = [dict(r) for r in c.execute("SELECT * FROM symbol_identity_epochs ORDER BY symbol,source,evidence_reference")] if "symbol_identity_epochs" in tables else []
        mapping = {r["symbol"]: r["cik"] for r in c.execute("SELECT symbol,cik FROM companies")}
        sec = c.execute("SELECT value FROM sync_state WHERE source='sec_reference' AND key='ticker_to_cik'").fetchone()
        reference = json.loads(sec[0]) if sec and sec[0] else {}
        conflicts = [dict(r) for r in c.execute("SELECT key,value FROM sync_state WHERE source='sec_identity_conflicts' ORDER BY key")]
        quarantined = old.frozen_database(c, Path(m["work"]), False).unresolved_sec_identity_conflict_symbols()
        actions = [dict(r) for r in c.execute("SELECT * FROM security_corporate_actions ORDER BY provider,event_key")] if "security_corporate_actions" in tables else []
    classified = classify_identities(list(pairs.values()), epochs)
    old_sample = set(original["scores"][0]["symbols"])
    reusable = [r for r in classified if r["classification"] == "VERIFIED_TICKER_REUSE"]
    selected = {r["symbol"]: r for r in reusable}
    for kind in ("UNRESOLVED_MULTI_CIK_PROVENANCE", "ALIAS_OR_SHARE_CLASS"):
        for row in [r for r in classified if r["classification"] == kind and r["symbol"] not in old_sample][:8]:
            selected[row["symbol"]] = row
    disagreements = [{"symbol": s, "persisted_cik": cik, "cached_sec_cik": str(reference[s]).zfill(10)}
        for s, cik in sorted(mapping.items()) if s in reference and cik != str(reference[s]).zfill(10)]
    by_symbol = {r["symbol"]: r for r in classified}
    conflict_labels = quarantined | {r["symbol"] for r in disagreements}
    for symbol in sorted(conflict_labels - old_sample)[:8]:
        selected[symbol] = by_symbol.get(symbol, {"symbol": symbol, "fact_provenance": [], "verified_transitions": []})
    witnesses = []
    fallback_day = original_session(original, "forward_cik_sip")
    for symbol, row in sorted(selected.items()):
        days = {fallback_day}
        days.update(date.fromisoformat(p["first_filed"]) for p in row["fact_provenance"])
        for transition in row["verified_transitions"]:
            for anchor in (transition["before"]["effective_end"], transition["after"]["effective_start"]):
                days.update(date.fromisoformat(anchor) + timedelta(days=d) for d in (-1, 0, 1))
        witnesses.extend(reader_witness(Path(m["source"]), Path(m["work"]), symbol, days, monitor))
    return {"status": "PASS" if reusable else "INSUFFICIENT_EVIDENCE",
        "clears": ["Missing real boundary witness: ticker_reuse"] if reusable else [],
        "gaps": [] if reusable else ["No authoritative real ticker-reuse witness; synthetic evidence cannot waive this requirement"],
        "census": classified, "classification_counts": dict(Counter(r["classification"] for r in classified)),
        "outside_original_sample_symbols": sum(r["symbol"] not in old_sample for r in classified),
        "identity_epochs": epochs, "current_assignment_disagreements": disagreements,
        "persisted_conflicts": conflicts, "corporate_actions_uninterpreted": actions,
        "quarantined_symbols": sorted(quarantined), "census_query_plan": plan,
        "reader_witnesses": witnesses, "real_reuse_witness_count": len(reusable),
        "reuse_policy": "Verified production lifecycle intervals AND both fact CIKs; fact filed ranges/current references never establish ownership intervals",
        "synthetic_regression": synthetic_identity_regression(),
        "census_source_equivalence": "Unchanged Work/G3 full ordered streams; source occurrence census need not be rescanned"}


def synthetic_identity_regression():
    """Tiny in-memory regression, explicitly worth zero real reuse witnesses."""
    from trading_system.data.canonical_facts import COLS, build_snapshot
    from trading_system.data.database import _fact_rows
    from trading_system.models.fundamentals import FundamentalFact
    from trading_system.data.security_identity import LIFECYCLE_EVIDENCE
    a, b = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    try:
        facts = [FundamentalFact(cik=cik, symbol=symbol, metric="cash", tag="Cash", value=value, unit="USD",
            period_end=date(year, 1, 1), filed=date(year, 2, 1), form="10-K", accession_number=str(year))
            for cik, symbol, year, value in (("0000001111", "REUSE", 2010, 10),
                ("0000002222", "REUSE", 2020, 20), ("0000002222", "ALIAS", 2020, 20))]
        for c in (a, b):
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("CREATE TABLE companies(cik TEXT PRIMARY KEY,symbol TEXT,name TEXT,sic TEXT,sic_description TEXT,updated_at TEXT)")
            c.execute("CREATE TABLE sync_state(source TEXT,key TEXT,updated_at TEXT,value TEXT,PRIMARY KEY(source,key))")
            c.execute("INSERT INTO companies VALUES ('0000002222','REUSE','Synthetic',NULL,NULL,'fixture')")
            c.execute("CREATE TABLE fundamental_facts(id INTEGER PRIMARY KEY," + ",".join(k + " TEXT" for k in COLS) + ")")
            c.executemany("INSERT INTO fundamental_facts(" + ",".join(COLS) + ") VALUES (" + ",".join("?" for _ in COLS) + ")", _fact_rows(facts))
            c.commit()
        build_snapshot(b, dataset_id="supplement_synthetic_only", source_hash="a" * 64)
        p = Path("SYNTHETIC_MEMORY_ONLY").absolute()
        for forward in (False, True):
            da, db = old.frozen_database(a, p, forward), old.frozen_database(b, p, forward)
            for symbol in ("REUSE", "ALIAS"):
                for day in (date(2010, 2, 1), date(2020, 2, 1)):
                    left, right = da.facts_available_as_of(symbol, day), db.facts_available_as_of(symbol, day)
                    equal([serial(f) for f in left], [serial(f) for f in right], "synthetic_identity")
                    if forward:
                        require(all(f.cik == "0000002222" for f in left), "SYNTHETIC_CIK_MERGE")
                        if symbol == "ALIAS" or day.year == 2010:
                            require(not left, "SYNTHETIC_CIK_FALLBACK")
        class MemoryMonitor:
            def tick(self, *_):
                pass
            def check(self):
                pass
        pairs, plan = identity_census(b, MemoryMonitor(), len(facts))
        pairs = list(pairs.values())
        epochs = [{"symbol": "REUSE", "cik": cik, "stable_security_identity": "FIXTURE:" + cik,
            "effective_start": start, "effective_end": end, "source": "SYNTHETIC_ONLY", "evidence_type": LIFECYCLE_EVIDENCE,
            "verified": True, "observed_at": "2026-11-15T00:00:00+00:00", "evidence_reference": cik}
            for cik, start, end in (("0000001111", "2009-01-01", "2015-01-01"), ("0000002222", "2019-01-01", None))]
        classified = classify_identities(pairs, epochs)
        require(next(r for r in classified if r["symbol"] == "REUSE")["classification"] == "VERIFIED_TICKER_REUSE", "SYNTHETIC_REUSE_NOT_CLASSIFIED")
        require(next(r for r in classify_identities(pairs, []) if r["symbol"] == "REUSE")["classification"] == "UNRESOLVED_MULTI_CIK_PROVENANCE", "UNPROVEN_REUSE_PROMOTED")
        return {"status": "PASS", "material": "SYNTHETIC_IN_MEMORY_ONLY", "real_reuse_credit": False,
                "legacy_forward_parity": True, "no_cik_merge_or_fallback": True,
                "census_query_plan": plan, "classifications": classified}
    finally:
        a.close()
        b.close()


def combine(original, supplements):
    reused = reused_evidence(original)
    require(len({s["phase"] for s in supplements}) == len(supplements), "DUPLICATE_SUPPLEMENT_PHASE")
    for supplement in supplements:
        validate_clearances(supplement)
    cleared = {gap for s in supplements for gap in s.get("clears", [])}
    allowed = {f"{mode}/{kind}: no rank-1 witness in fixed cross-section"
        for mode in ("legacy_ticker", "forward_cik_sip") for kind in ("quality_value", "F")}
    allowed |= {"forward_cik_sip: frozen forward inputs are unresolved for ranking", "Missing real boundary witness: ticker_reuse"}
    require(cleared <= allowed, "UNKNOWN_REQUIREMENT_CLEARANCE")
    remaining = sorted(set(original.get("gaps", [])) - cleared)
    remaining += [gap for s in supplements for gap in s.get("gaps", [])]
    phases = {s["phase"] for s in supplements}
    for phase in ("ranking", "identity"):
        if phase not in phases:
            remaining.append("Missing sealed supplement: " + phase)
    for s in supplements:
        if s.get("status") != "PASS" and not s.get("gaps"):
            remaining.append(s["phase"] + ": non-PASS supplement without complete evidence")
    status = "INSUFFICIENT_EVIDENCE" if remaining else "PASS"
    if any(s.get("status") in {"BLOCKED", "FAILED", "ABORTED"} for s in supplements):
        status = "NO_GO"
    return {"status": status, "gaps": sorted(set(remaining)), "cleared_original_gaps": sorted(cleared),
            "reused_successful_evidence": reused, "original_status": original["status"],
            "cutover_go": False, "g5_g6_go": False, "real_evidence_requirement_waived": False}


def validate_clearances(report):
    """A synthetic PASS or empty ranking can never erase a real requirement."""
    claimed = set(report.get("clears", []))
    established = set()
    if report["phase"] == "identity":
        real = [r for r in report.get("census", []) if r["classification"] == "VERIFIED_TICKER_REUSE"]
        # Re-run only the tiny evidence classifier, never any database census.
        pairs = [p for r in report.get("census", []) for p in r.get("fact_provenance", [])]
        proven = [r for r in classify_identities(pairs, report.get("identity_epochs", []))
                  if r["classification"] == "VERIFIED_TICKER_REUSE"]
        require({r["symbol"] for r in real} == {r["symbol"] for r in proven}, "REAL_IDENTITY_PROOF_DRIFT")
        if proven and report.get("real_reuse_witness_count") == len(proven):
            for row in proven:
                readers = {r["reader"] for r in report.get("reader_witnesses", []) if r["symbol"] == row["symbol"]}
                require(readers == {"legacy_ticker", "forward_cik"}, "MISSING_REAL_READER_WITNESS")
            established.add("Missing real boundary witness: ticker_reuse")
    elif report["phase"] == "ranking":
        for mode, item in report.get("modes", {}).items():
            evidence = item["evidence"]
            require(item.get("parity") is True
                    and item["source_context_fingerprint"] == digest(evidence)
                    and item["work_context_fingerprint"] == digest(evidence)
                    and item["universe"]["selection"] == "ALL_PRODUCTION_TRADABLE_COMPANIES",
                    "RANKING_CONTEXT_PROOF_DRIFT")
            valid = evidence["ranking_valid"]
            if mode == "forward_cik_sip":
                valid = valid and evidence["qualification"]["unresolved_rank_relevant_count"] == 0
                if valid:
                    established.add("forward_cik_sip: frozen forward inputs are unresolved for ranking")
            for kind, key in (("quality_value", "quality_value_ranking"), ("F", "f_ranking")):
                ranking = evidence[key]
                if valid and ranking and [r["rank"] for r in ranking] == list(range(1, len(ranking) + 1)):
                    established.add(f"{mode}/{kind}: no rank-1 witness in fixed cross-section")
    else:
        raise g3.Blocked("UNKNOWN_SUPPLEMENT_PHASE")
    require(claimed <= established, "UNPROVEN_REAL_EVIDENCE_CLEARANCE")


RESULT_FILES = {"f01_g4_supplement_report.json", "f01_g4_supplement_audit.md", "progress.jsonl", "fingerprint.json"}


def load_supplement(directory, binding, monitor):
    if (directory / "failure.json").is_file():
        failure = g3.read_json(directory / "failure.json")
        require(failure.get("binding") == binding and failure.get("status") in {"BLOCKED", "FAILED", "ABORTED"},
                "FAILURE_EVIDENCE_BINDING_DRIFT")
        return {"phase": failure["phase"], "status": failure["status"], "binding": binding,
            "clears": [], "gaps": [failure.get("error", "Interrupted phase")],
            "failure_sha256": g3.sha(directory / "failure.json", monitor),
            "unsealed_negative_evidence_only": True}
    old.sealed(directory, "seal.json", RESULT_FILES, monitor)
    report = g3.read_json(directory / "f01_g4_supplement_report.json")
    require(report.get("binding") == binding, "SUPPLEMENT_BINDING_DRIFT")
    require(g3.read_json(directory / "fingerprint.json") == {"contract": CONTRACT, "report_fingerprint": digest(report)}, "SUPPLEMENT_FINGERPRINT_DRIFT")
    require(report.get("references_unchanged") is True and report.get("execution_enabled") is False,
            "SUPPLEMENT_REFERENCE_EVIDENCE_MISSING")
    return report


def publish(attempt, report, monitor):
    pending = attempt / "unpublished"
    pending.mkdir()
    monitor.targets = [pending / name for name in RESULT_FILES | {"seal.json"}]
    g3.new_json(pending / "f01_g4_supplement_report.json", report)
    with (pending / "f01_g4_supplement_audit.md").open("x", encoding="utf-8") as out:
        out.write("# F01 G4 Supplement\n\nStatus: **" + report["status"] + "**\n\n")
        out.write("Phase: " + report["phase"] + "; Binding: " + digest(report["binding"]) + "\n\n")
        out.write("Keine ursprüngliche Evidenz ersetzt; kein Cutover-/G5-/G6-GO.\n\n")
        for gap in report.get("gaps", []):
            out.write("- " + gap + "\n")
        out.flush()
        os.fsync(out.fileno())
    with monitor.lock:
        monitor.logfile.flush()
        os.fsync(monitor.logfile.fileno())
        with (pending / "progress.jsonl").open("xb") as out:
            out.write((attempt / "progress.jsonl").read_bytes())
            out.flush()
            os.fsync(out.fileno())
    g3.new_json(pending / "fingerprint.json", {"contract": CONTRACT, "report_fingerprint": digest(report)})
    g3.new_json(pending / "seal.json", {name: g3.sha(pending / name, monitor) for name in RESULT_FILES})
    if hasattr(monitor, "sample"):
        monitor.sample()
    monitor.check()
    pending.rename(attempt / "result")
    return attempt / "result"


def choose_g4(policy, given):
    if given:
        return g3.safe(given, file=False)
    candidates = sorted((policy.job / "acceptance_runs").glob("*/result"))
    candidates = [p for p in candidates if (p / "f01_migration_acceptance_g4.json").is_file()]
    require(len(candidates) == 1, "SPECIFY_UNIQUE_ORIGINAL_--g4-result")
    return candidates[0]


def run(args, policy=None):
    policy = policy or Policy()
    monitor = old.Monitor(policy)
    phase = "ranking" if args.ranking else "identity" if args.identity else "combine" if args.combine else "preflight"
    attempt, binding = None, None
    before_signal = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: monitor.stop(KeyboardInterrupt()))
    monitor.begin()
    try:
        g3_dir, g4_dir = old.result_directory(policy, args.g3_result), choose_g4(policy, args.g4_result)
        m, prep, original, binding = verify_metadata(policy, g3_dir, g4_dir, monitor)
        monitor.manifest, monitor.prep = m, prep
        with ExitStack() as stack:
            stack.enter_context(prep.pinned_files(protected_paths(policy, g3_dir, g4_dir, m)))
            m2, _, original2, binding2 = verify_metadata(policy, g3_dir, g4_dir, monitor)
            equal((m, original, binding), (m2, original2, binding2), "supplement_pinned_metadata_recheck")
            monitor.capacity(0 if phase == "preflight" else GIB)
            # Physical freshness/schema/COMPLETE only: never rerun G3 streams or
            # ledger, G4 accounting or the non-Fact inventory.
            old.verify_files(m, binding["original_g4_binding"], prep, monitor)
            inherited = reused_evidence(original)
            if phase == "preflight":
                print("PREFLIGHT_VERIFIED: " + json.dumps(inherited) + "; no supplement executed", flush=True)
                return 0
            runs = g3.safe(policy.job / "g4_supplement_runs", file=False)
            runs.mkdir(exist_ok=True)
            attempt = runs / (g3.stamp().replace(":", "").replace("+", "_") + "_" + phase + "_" + uuid.uuid4().hex)
            attempt.mkdir()
            monitor.run_dir = attempt
            monitor.logfile = (attempt / "progress.jsonl").open("x", encoding="utf-8")
            stack.enter_context(prep.pinned_files({"work": attempt / "progress.jsonl"}))
            stack.enter_context(g3.execution_lock(policy.job / "f01_acceptance.lock"))
            with old.network_denied():
                if phase == "ranking":
                    result = ranking_phase(m, original, monitor)
                elif phase == "identity":
                    result = identity_phase(m, original, monitor)
                else:
                    supplements = []
                    for kind, directory in (("ranking", args.ranking_result), ("identity", args.identity_result)):
                        if directory:
                            names = {"failure.json"} if (directory / "failure.json").is_file() else RESULT_FILES | {"seal.json"}
                            stack.enter_context(prep.pinned_files({"phase_" + kind + "_" + name: directory / name
                                for name in names}))
                            r = load_supplement(directory, binding, monitor)
                            require(r["phase"] == kind, "WRONG_SUPPLEMENT_PHASE")
                            supplements.append(r)
                    result = combine(original, supplements)
                    result["supplement_fingerprints"] = {s["phase"]: digest(s) for s in supplements}
            old.verify_files(m, binding["original_g4_binding"], prep, monitor)
            monitor.check()
            require(prep.migration.EXECUTION_ENABLED is False, "EXECUTOR_ENABLED_UNEXPECTEDLY")
            report = {**result, "phase": phase, "binding": binding, "at": g3.stamp(),
                "reused_successful_evidence": inherited, "references_unchanged": True,
                "execution_enabled": False, "cutover_go": False,
                "elapsed_seconds": time.monotonic() - monitor.start,
                "resources": prep.memory_usage(), "capacity": monitor.budget()}
            print(f"{report['status']}: {publish(attempt, report, monitor)}", flush=True)
            return 0 if report["status"] == "PASS" else 2
    except BaseException as exc:
        status = "ABORTED" if isinstance(exc, KeyboardInterrupt) else "BLOCKED"
        print(f"[{g3.stamp()}] {status} ({monitor.phase}): {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if attempt:
            g3.new_json(attempt / "failure.json", {"status": status, "phase": phase,
                "binding": binding, "error": f"{type(exc).__name__}: {exc}", "cutover_go": False})
            print("Failure artifacts: " + str(attempt), file=sys.stderr, flush=True)
        return 130 if status == "ABORTED" else 2
    finally:
        monitor.close()
        if monitor.logfile:
            monitor.logfile.close()
        signal.signal(signal.SIGINT, before_signal)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_mutually_exclusive_group()
    for flag in ("ranking", "identity", "combine"):
        modes.add_argument("--" + flag, action="store_true")
    for flag in ("g3-result", "g4-result", "ranking-result", "identity-result"):
        p.add_argument("--" + flag, type=Path)
    return p


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
