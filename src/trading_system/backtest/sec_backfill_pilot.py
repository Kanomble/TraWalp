"""Bounded SEC refresh experiment; canonical historical features, never a backtest."""

from __future__ import annotations

import csv
import gc
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from trading_system.backtest import f_data_gap_audit as audit
from trading_system.backtest.features import (
    HistoricalFeatureScreenSource,
    _AccountingCache,
    _eligible_fact_count,
)
from trading_system.backtest.research_registry import validate_champion_config
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.data.sec_force import execute_force, resolve_local_companies

STEM = "f_2022_sec_backfill_pilot_v1"
FACTORS = (*audit.QUALITY, *audit.VALUATION)
KEY_COLUMNS = (
    "cik",
    "metric",
    "tag",
    "unit",
    "period_start",
    "period_end",
    "filed",
    "accession_number",
    "frame",
)


def dump_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def dump_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in row.items()
                }
            )


def protected_inventory(database):
    """Exact logical row hashes, streaming existing tables without a DB copy."""
    result = {}
    with database.read_only() as connection:
        tables = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for table, schema in tables:
            if table in {"fundamental_facts", "companies", "sync_state"}:
                continue
            print(f"Hashing protected table {table}", flush=True)
            quoted = '"' + table.replace('"', '""') + '"'
            order = "rowid" if "WITHOUT ROWID" not in schema.upper() else "1"
            digest, count = hashlib.sha256(), 0
            cursor = connection.execute(f"SELECT * FROM {quoted} ORDER BY {order}")
            while rows := cursor.fetchmany(10000):
                for row in rows:
                    digest.update(repr(tuple(row)).encode())
                    digest.update(b"\n")
                count += len(rows)
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def metadata_inventory(database):
    with database.read_only() as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
            for table in ("companies", "sync_state")
        }


def fact_inventory(database, symbols):
    with database.read_only() as connection:
        placeholders = ",".join("?" for _ in symbols)
        rows = connection.execute(
            f"SELECT * FROM fundamental_facts WHERE symbol IN ({placeholders}) ORDER BY id",
            symbols,
        ).fetchall()
    result = {symbol: {"rows": [], "observations": set()} for symbol in symbols}
    for row in rows:
        values = dict(row)
        result[row["symbol"]]["rows"].append(values)
        result[row["symbol"]]["observations"].add(
            tuple(row[column] for column in KEY_COLUMNS) + (row["value"],)
        )
    return result


def pit_observations(observations, as_of=audit.END):
    cutoff = as_of.isoformat()
    return {row for row in observations if row[6] <= cutoff}


def fact_delta(before, after):
    added = after["observations"] - before["observations"]
    pit_added = pit_observations(added)
    # A changed value at the same identity is a revision, not a new observation key.
    before_keys = {row[:-1] for row in before["observations"]}
    distinct_added = {row for row in added if row[:-1] not in before_keys}
    return {
        "facts_before": len(before["rows"]),
        "facts_after": len(after["rows"]),
        "facts_added": len(after["rows"]) - len(before["rows"]),
        "unique_observations_added": len(distinct_added),
        "observations_with_changed_values": len(added) - len(distinct_added),
        "unique_2022_pit_observations_added": len(pit_observations(distinct_added)),
        "pit_2022_input_changes": len(pit_added),
        "pit_2022_facts_before": sum(
            row["filed"] <= audit.END.isoformat() for row in before["rows"]
        ),
        "pit_2022_facts_after": sum(row["filed"] <= audit.END.isoformat() for row in after["rows"]),
        "new_earliest_historical_filing": min((row[6] for row in distinct_added), default=None),
        "new_historical_periods": sorted(
            {row[5] for row in pit_observations(distinct_added)}
            - {row[5] for row in pit_observations(before["observations"])}
        ),
        "new_2022_input_metrics": sorted({row[1] for row in pit_added}),
        "new_input_metrics_all_dates": sorted({row[1] for row in added}),
        "duplicate_row_growth": (
            len(after["rows"])
            - len(after["observations"])
            - len(before["rows"])
            + len(before["observations"])
        ),
    }


def new_state(symbols):
    return {
        symbol: {
            "quality": set(),
            "valuation": set(),
            "evaluated": set(),
            "factors": {factor: set() for factor in FACTORS},
            "gaps": {category: set() for category in audit.CATEGORIES},
            "factor_gap_counts": {factor: Counter() for factor in FACTORS},
            "peer_multiple_sessions": set(),
            "pre_scoring": Counter(),
        }
        for symbol in symbols
    }


def clear_evidence_cache():
    for name in ("flow", "balance", "da", "ebitda", "quality"):
        getattr(audit.AccountingEvidence, name).cache_clear()


def diagnostic_inventory(inventory):
    return {
        symbol: {
            metric: min(
                date.fromisoformat(row["filed"])
                for row in values["rows"]
                if row["metric"] == metric
            )
            for metric in {row["metric"] for row in values["rows"]}
        }
        for symbol, values in inventory.items()
    }


def observe(source, session, candidates, peers, state, first, raw_evidence, evidence_cache):
    for candidate in candidates:
        symbol = candidate.company.symbol
        record = state[symbol]
        if not candidate.fundamentals_evaluated:
            record["pre_scoring"].update(candidate.base_exclusions)
            # Same pre-scoring shares diagnostic used by the original audit.
            if "missing_or_small_market_cap" in candidate.base_exclusions:
                evidence = audit.AccountingEvidence(
                    source._shares.get(symbol, []),
                    session,
                    metric_first=first[symbol],
                    raw_evidence=raw_evidence.get(symbol),
                )
                for issue in evidence.balance("shares_outstanding"):
                    record["gaps"][issue.category].add(session.isoformat())
            continue
        record["evaluated"].add(session.isoformat())
        view, group, medians = source.screener._score_inputs(candidate, peers)
        if view.scores.quality.score is not None:
            record["quality"].add(session.isoformat())
        if view.scores.valuation.score is not None:
            record["valuation"].add(session.isoformat())
        if any(medians.get(metric) is not None for metric in ("pe", "ev_to_ebitda", "ev_to_ebit")):
            record["peer_multiple_sessions"].add(session.isoformat())
        facts = source._facts[symbol].facts if symbol in source._facts else []
        count = _eligible_fact_count(facts, session)
        raw = raw_evidence.get(symbol)
        raw_count = (
            (
                sum(f.filed <= session for f in raw.facts)
                + sum(d <= session for d in raw.metric_first.values())
            )
            if raw
            else 0
        )
        key = (symbol, count, raw_count, bool(candidate.fundamentals.market_cap))
        evidence = evidence_cache.get(key)
        if evidence is None:
            evidence = audit.AccountingEvidence(
                facts[:count],
                session,
                metric_first=first[symbol],
                cache_facts=raw.facts if raw else [],
                raw_evidence=raw,
            )
            evidence_cache[key] = evidence
        diagnoses = audit.diagnose_factors(view, candidate, group, medians, evidence)
        for factor, diagnosis in diagnoses.items():
            if diagnosis["available"]:
                record["factors"][factor].add(session.isoformat())
            else:
                for category in {issue.category for issue in diagnosis["issues"]}:
                    record["factor_gap_counts"][factor][category] += 1
                    record["gaps"][category].add(session.isoformat())


def baseline_replay(database, config, symbols, inventory, raw_evidence):
    """Cache unchanged peers and pilot market features, then reuse production methods."""
    state, context = new_state(symbols), []
    first = diagnostic_inventory(inventory)
    sessions = trading_sessions_between(audit.START, audit.END)
    if set(sessions) - set(database.bar_sessions(audit.START, audit.END)):
        raise ValueError("PILOT_GLOBAL_SESSIONS_INCOMPLETE")
    for month in range(1, 13):
        part = [session for session in sessions if session.month == month]
        print(f"Baseline canonical feature preparation {month:02}/12", flush=True)
        source = HistoricalFeatureScreenSource(database, config, part[0], part[-1])
        companies = {
            company.symbol: company for company in source._companies if company.symbol in symbols
        }
        if set(companies) != set(symbols):
            raise ValueError("PILOT_SYMBOL_NOT_IN_CANONICAL_SOURCE")
        saved = {
            "start": part[0],
            "end": part[-1],
            "companies": companies,
            "market": {s: source._market.get(s, {}) for s in symbols},
            "cheap": {s: source._cheap_exclusions.get(s, {}) for s in symbols},
            "static": {s: v for s, v in source._static_exclusions.items() if s in symbols},
            "sessions": [],
        }
        evidence_cache = {}
        for session in part:
            prepared = [source._candidate(company, session) for company in source._companies]
            peers = source.screener._f_peer_index(prepared)
            pilots = [candidate for candidate in prepared if candidate.company.symbol in symbols]
            observe(source, session, pilots, peers, state, first, raw_evidence, evidence_cache)
            # Excluded rows never participate in _f_peer_index, so dropping them
            # does not alter any group assignment, percentile, or industry median.
            unchanged = [
                c for c in prepared if c.company.symbol not in symbols and not c.base_exclusions
            ]
            saved["sessions"].append((session, unchanged))
        context.append(saved)
        del source, evidence_cache, prepared, peers, pilots
        clear_evidence_cache()
        gc.collect()
    return state, context, sessions


def after_replay(database, config, symbols, inventory, raw_evidence, context):
    from trading_system.backtest.features import HistoricalPerformanceDiagnostics
    from trading_system.strategy.screener import Screener

    state = new_state(symbols)
    first = diagnostic_inventory(inventory)
    for month, saved in enumerate(context, 1):
        print(f"After canonical pilot/peer replay {month:02}/12", flush=True)
        # Preserve the identical bar-derived features and unchanged peer inputs.
        # Reload only changed SEC streams with production's original PIT bounds.
        source = HistoricalFeatureScreenSource.__new__(HistoricalFeatureScreenSource)
        source.database, source.config = database, config
        source.screener = Screener(database, config)
        source.diagnostics = HistoricalPerformanceDiagnostics()
        source._market, source._cheap_exclusions = saved["market"], saved["cheap"]
        source._static_exclusions = saved["static"]
        grouped, shares = defaultdict(list), defaultdict(list)
        for batch in database.iter_fact_batches(
            symbols, saved["end"], metrics=("shares_outstanding",)
        ):
            for fact in batch:
                shares[fact.symbol].append(fact)
        for batch in database.iter_fact_batches(
            symbols,
            saved["end"],
            period_end_on_or_after=saved["start"] - timedelta(days=4 * 366),
            retain_latest_periods=24,
        ):
            for fact in batch:
                grouped[fact.symbol].append(fact)
        source._shares = dict(shares)
        source._facts = {symbol: _AccountingCache(facts) for symbol, facts in grouped.items()}
        evidence_cache = {}
        for session, unchanged in saved["sessions"]:
            pilots = [source._candidate(saved["companies"][symbol], session) for symbol in symbols]
            peers = source.screener._f_peer_index([*unchanged, *pilots])
            observe(source, session, pilots, peers, state, first, raw_evidence, evidence_cache)
        del source, evidence_cache
        clear_evidence_cache()
        gc.collect()
    return state


def snapshot_rows(plan, inventory, state):
    rows = []
    for candidate in plan["selected"]:
        symbol = candidate["symbol"]
        facts, record = inventory[symbol]["rows"], state[symbol]
        counts = {factor: len(record["factors"][factor]) for factor in FACTORS}
        rows.append(
            {
                "symbol": symbol,
                "cik": candidate["cik"],
                "priority": candidate["priority"],
                "local_fact_count": len(facts),
                "unique_observation_count": len(inventory[symbol]["observations"]),
                "earliest_filed": min((row["filed"] for row in facts), default=None),
                "latest_filed": max((row["filed"] for row in facts), default=None),
                "pit_2022_fact_count": sum(row["filed"] <= audit.END.isoformat() for row in facts),
                "quality_factors_available": [f for f in audit.QUALITY if counts[f]],
                "quality_factors_never_available": [f for f in audit.QUALITY if not counts[f]],
                "valuation_factors_available": [f for f in audit.VALUATION if counts[f]],
                "valuation_factors_never_available": [f for f in audit.VALUATION if not counts[f]],
                "factor_available_session_counts": counts,
                "factor_unavailable_evaluated_session_counts": {
                    f: len(record["evaluated"]) - counts[f] for f in FACTORS
                },
                "quality_score_ever_available_2022": bool(record["quality"]),
                "valuation_score_ever_available_2022": bool(record["valuation"]),
                "quality_available_session_count": len(record["quality"]),
                "valuation_available_session_count": len(record["valuation"]),
                "fundamentals_evaluated_session_count": len(record["evaluated"]),
                "affected_2022_sessions": candidate["affected_sessions"],
                "missing_input_classes": candidate["missing_input_classes"],
                "gap_category_session_counts": {c: len(v) for c, v in record["gaps"].items()},
                "factor_gap_category_counts": record["factor_gap_counts"],
                "pre_scoring_reason_counts": record["pre_scoring"],
                "peer_multiple_available_session_count": len(record["peer_multiple_sessions"]),
            }
        )
    return rows


def classify_symbol(*, synced, score_recovered, factor_recovered, new_relevant_data):
    if not synced:
        return "SYNC_FAILED"
    if score_recovered:
        return "RECOVERED_SCORE_AVAILABILITY"
    if factor_recovered:
        return "RECOVERED_FACTOR_ONLY"
    if new_relevant_data:
        return "NEW_RAW_FACTS_NO_F_IMPROVEMENT"
    return "NO_NEW_SEC_DATA"


def decision(successful, recovered):
    if successful < 10:
        return "SEC_BACKFILL_PILOT_INCONCLUSIVE"
    rate = recovered / successful
    if rate >= 0.5:
        return "FULL_158_SEC_BACKFILL_JUSTIFIED"
    if rate >= 0.2:
        return "EXPANDED_SEC_BACKFILL_PILOT_JUSTIFIED"
    if rate > 0:
        return "FULL_SEC_BACKFILL_LOW_EXPECTED_VALUE"
    return "FULL_SEC_BACKFILL_NOT_JUSTIFIED"


def delta_rows(plan, before, after, before_state, after_state, sync_results):
    results = {row["symbol"]: row for row in sync_results}
    rows = []
    for candidate in plan["selected"]:
        symbol = candidate["symbol"]
        previous, current = before_state[symbol], after_state[symbol]
        factors = {
            factor: len(current["factors"][factor] - previous["factors"][factor])
            for factor in FACTORS
        }
        score_recovery = {
            score: len(current[score] - previous[score]) for score in ("quality", "valuation")
        }
        facts = fact_delta(before[symbol], after[symbol])
        synced = results.get(symbol, {}).get("status") == "success"
        rows.append(
            {
                "symbol": symbol,
                "cik": candidate["cik"],
                "priority": candidate["priority"],
                **facts,
                "quality_available_sessions_before": len(previous["quality"]),
                "quality_available_sessions_after": len(current["quality"]),
                "valuation_available_sessions_before": len(previous["valuation"]),
                "valuation_available_sessions_after": len(current["valuation"]),
                "quality_factors_recovered": [f for f in audit.QUALITY if factors[f]],
                "valuation_factors_recovered": [f for f in audit.VALUATION if factors[f]],
                "factor_recovered_session_counts": factors,
                "quality_recovered_sessions": score_recovery["quality"],
                "valuation_recovered_sessions": score_recovery["valuation"],
                "quality_lost_sessions": len(previous["quality"] - current["quality"]),
                "valuation_lost_sessions": len(previous["valuation"] - current["valuation"]),
                "synced": synced,
                "classification": classify_symbol(
                    synced=synced,
                    score_recovered=any(score_recovery.values()),
                    factor_recovered=any(factors.values()),
                    new_relevant_data=(
                        facts["unique_observations_added"] > 0
                        or facts["observations_with_changed_values"] > 0
                    ),
                ),
            }
        )
    return rows


def render_audit(summary, deltas):
    diagnostics = summary["diagnostics"]
    lines = [
        "# F 2022 SEC Companyfacts Backfill Pilot v1",
        "",
        f"Entscheidung: **{summary['decision']}**.",
        "",
        f"{summary['pilot_symbols_synced']} von {summary['pilot_symbols_requested']} "
        "Symbolen synchronisiert; "
        f"{summary['symbols_with_any_score_recovery']} mit Score-Recovery "
        f"({summary['score_recovery_rate']:.1%}).",
        "",
        "## Vorab festgelegte Auswahl und Methode",
        "",
        "Maximal 15 Manifest-Symbole; HIGH vor MEDIUM vor LOW, absteigende betroffene Sessions, "
        "absteigende verschiedene Faktoren, Symbol alphabetisch. Der Manifestwert je Symbol "
        "ist das "
        "Maximum der Faktor-Sessionzahlen (überlappende Faktoren werden nicht summiert). "
        "Alle CIKs werden ausschließlich lokal gegen companies, tradable assets, Konfliktstatus "
        "und gespeicherte SEC-Tickerreferenz geprüft. Bekannte IFRS-, Identity- und "
        "Fund-Sonderfälle ausgeschlossen.",
        "",
        "Der CLI-Einstieg verwendet DataSynchronizer.sync_sec_full(..., companyfacts_only=True). "
        "Dessen normaler Pfad würde Tickerliste und Submissions abrufen. Der explizite Modus "
        "verwendet geprüfte lokale CIK-/Company-Metadaten und ruft ausschließlich Companyfacts ab. "
        "Parser, Tags, Mappings und Upsert bleiben unverändert. Kein globaler Sync.",
        "",
        "Baseline: monatliche HistoricalFeatureScreenSource für die vollständige lokale "
        "Peer-Cross-Section. Nachher: dieselben _candidate/_f_peer_index/_score_inputs "
        "und f_data_gap_audit-Diagnose; Marktfeatures und Nichtpilot-Peers wiederverwendet, "
        "SEC-Ströme der Pilotsymbole neu geladen. min_peer_count=8 unverändert. Änderungen an "
        "Peer-Multiples wirken natürlich, keine Peer-Regeländerung oder Optimierung. "
        "Nicht vor dem F-Scoring zugelassene Sessions werden als nicht ausgewertet gekennzeichnet.",
        "",
        "## Die fünf Ergebnisfragen",
        "",
        f"1. Erneuter Fetch lieferte {summary['unique_observations_added']} neue eindeutige "
        "normalisierte "
        f"Beobachtungen; {summary['facts_added']} zusätzliche DB-Zeilen, davon "
        f"{summary['duplicate_row_growth']} zusätzliche Duplikatzeilen "
        "nach bestehender NULL-Upsert-Semantik.",
        "",
        f"2. {summary['unique_2022_pit_observations_added']} eindeutige neue Beobachtungen "
        "haben filed <= 2022-12-30. Nur am Sessiondatum bekannte Facts beeinflussen Scores. "
        "Spätere Filings zählen ausschließlich zur heutigen Inventur.",
        "",
        f"3. {summary['symbols_with_recovered_factors']} Symbole mit Faktor-Recovery; "
        f"Faktor-Recovery-Rate {summary['factor_recovery_rate']:.1%}. "
        f"Details: `{json.dumps(summary['factor_recovery'], sort_keys=True)}`.",
        "",
        f"4. Quality-Recovery: {summary['symbols_with_recovered_quality']} Symbole; "
        f"Valuation-Recovery: {summary['symbols_with_recovered_valuation']} Symbole; "
        f"beliebige Score-Recovery: {summary['symbols_with_any_score_recovery']} Symbole.",
        "",
        f"5. Nach den vorab definierten Schwellen: {summary['decision']}. "
        "Der Pilot misst Daten- und Score-Verfügbarkeit; er bewertet keine Rendite "
        "oder Strategieperformance.",
        "",
    ]
    if summary["facts_added"] and not summary["symbols_with_any_score_recovery"]:
        lines += ["SEC refresh added data but did not improve F 2022 score availability.", ""]
    lines += [
        "## Ergebnisse pro Symbol",
        "",
        "| Symbol | Zeilen + | Eindeutig PIT + | Quality Sessions vorher/nachher | "
        "Valuation Sessions vorher/nachher | Ergebnis |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in deltas:
        lines.append(
            f"| {row['symbol']} | {row['facts_added']} | "
            f"{row['unique_2022_pit_observations_added']} | "
            f"{row['quality_available_sessions_before']}/"
            f"{row['quality_available_sessions_after']} | "
            f"{row['valuation_available_sessions_before']}/"
            f"{row['valuation_available_sessions_after']} | "
            f"{row['classification']} |"
        )
    lines += [
        "",
        "## Verfügbarkeit und fortbestehende Lücken",
        "",
        "| Score | Symbole vorher/nachher | Symbol-Sessions vorher/nachher |",
        "|---|---:|---:|",
    ]
    for domain in ("quality", "valuation"):
        previous = [row[f"{domain}_available_sessions_before"] for row in deltas]
        current = [row[f"{domain}_available_sessions_after"] for row in deltas]
        lines.append(
            f"| {domain} | {sum(value > 0 for value in previous)}/"
            f"{sum(value > 0 for value in current)} | {sum(previous)}/{sum(current)} |"
        )
    lines += ["", "| Diagnosekategorie | Symbol-Sessions vorher | nachher |", "|---|---:|---:|"]
    for category in audit.CATEGORIES:
        lines.append(
            f"| {category} | {summary['gap_categories_before'][category]} | "
            f"{summary['gap_categories_after'][category]} |"
        )
    lines += [
        "",
        "Kategorien können sich überlappen; keine künstliche Summe auf 100 %. "
        "Eine Diagnoseverschiebung ohne neue Metrik ist keine Faktor- oder Score-Recovery.",
        "",
    ]
    for note in summary.get("diagnostic_notes", []):
        lines += [note, ""]
    proof = summary.get("session_union_ranking_verification")
    if proof:
        lines += [
            "Die Faktoranzahlen überlappen. Ihre konservativen Unter-/Obergrenzen "
            "beweisen für diese Auswahl die exakte Sessionanzahl aller 15 Symbole und "
            "dieselbe Top-15-Reihenfolge bei jeder zulässigen Überlappung. "
            "Es wurden keine Faktor-Sessions doppelt gezählt.",
            "",
        ]
    lines += [
        "",
        "## Safety und Grenzen",
        "",
        f"HTTP-Diagnostik: `{json.dumps(diagnostics, sort_keys=True)}`.",
        "",
        f"Tatsächlich geänderte Tabellen: {', '.join(summary['database_tables_changed'])}. "
        "Alle übrigen Tabellen wurden vor/nachher vollständig zeilenweise gehasht. "
        "Die SQL-Authorizer-Allowlist lässt nur companies/fundamental_facts/sync_state zu; "
        "DELETE ausschließlich in sync_state für die bestehende SEC-Statussemantik. "
        "Keine Migration, kein Initialize, kein Löschen von Fundamental-Facts.",
        "",
        f"SQLite SHA-256 vorher: `{summary['database_sha256_before']}`; "
        f"nachher: `{summary['database_sha256_after']}`. WAL separat in safety.json dokumentiert.",
        "",
        "Fact-Zeilen und eindeutige Beobachtungen werden getrennt ausgewiesen: bestehende SQLite "
        "UNIQUE-Schlüssel enthalten nullable Felder; identische Instant-Facts können erneut "
        "eingefügt werden. Das ist keine neue historische Evidenz und zählt nicht als Recovery. "
        "Werteänderungen desselben Beobachtungsschlüssels werden separat ausgewiesen.",
        "",
        "Historische Berechnung bewahrt filed <= as_of und die kanonischen Periodenfenster. "
        "Heutige Companyfacts beweisen keine archivierte damalige Datenbankversion; filed ist die "
        "bestehende Verfügbarkeitssemantik, kein Nachweis damaliger Veröffentlichungsrevisionen. "
        "Auch ein heute gelieferter historischer filed-Wert hat diese bekannte Provenienzgrenze.",
        "",
        "SEC coverage remediation does not repair CURRENT_UNIVERSE_ONLY / survivorship bias.",
        "",
        "Massive does not provide F Quality/Valuation fundamentals in current TraWalp "
        "production integration. Keine Massive-/sonstigen Provider-Requests, "
        "kein Asset-/Bar-Sync und kein Portfolio-Backtest.",
        "",
        "## Nächster Schritt",
        "",
        "Pilot und Delta-Liste reviewen; Entscheidung für den nächsten separat beauftragten "
        "SEC-Task verwenden. Keine restlichen Kandidaten wurden synchronisiert. "
        "Schwellen: <10 Erfolge = unklar; sonst Score-Recovery >=50% = voller Backfill, "
        "20–<50% = erweiterter Pilot, >0–<20% = geringer Wert, 0% = nicht gerechtfertigt.",
        "",
    ]
    if summary.get("validation"):
        lines += ["## Validierung", "", json.dumps(summary["validation"], sort_keys=True), ""]
    for note in summary.get("execution_notes", []):
        lines += [note, ""]
    return "\n".join(lines)


def run_pilot(settings, plan):
    config = settings.strategy
    validate_champion_config(config)
    user_agent = settings.require_sec_user_agent()
    out = Path(config.storage.reports_path).resolve()
    if list(out.glob(STEM + "_*")):
        raise ValueError("PILOT_FRESH_STEM_ALREADY_EXISTS_REFUSING_REPEAT_NETWORK")
    symbols = plan["resolved_symbols"]
    database = audit.AuditDatabase(config.storage.database_path)
    resolve_local_companies(database.path, symbols)
    paths = {
        name: out / f"{STEM}_{name}.{extension}"
        for name, extension in (
            ("manifest", "csv"),
            ("before", "csv"),
            ("after", "csv"),
            ("delta", "csv"),
            ("summary", "json"),
            ("audit", "md"),
            ("safety", "json"),
            ("diagnostics", "json"),
        )
    }
    old_artifacts = {str(p): audit.digest(p) for p in out.glob(audit.STEM + "_*") if p.is_file()}
    config_dump = config.model_dump(mode="json")
    print("Hashing SQLite file and protected data before any network", flush=True)
    with audit.local_guard():
        database_hash_before = audit.digest(database.path)
        wal = Path(str(database.path) + "-wal")
        wal_before = audit.digest(wal) if wal.exists() else None
        protected_before = protected_inventory(database)
        metadata_before = metadata_inventory(database)
        before = fact_inventory(database, symbols)
        raw_evidence = {}
        companies = resolve_local_companies(database.path, symbols)
        for symbol, company in companies.items():
            payload = database.cached_sec_payload(company.cik, "companyfacts", max_age=None)
            if payload:
                raw_evidence[symbol] = audit.raw_payload_evidence(payload, symbol)
        before_state, context, sessions = baseline_replay(
            database, config, symbols, before, raw_evidence
        )
        print("Verifying identical replay without changing any SEC inputs", flush=True)
        control_state = after_replay(database, config, symbols, before, raw_evidence, context)
        if control_state != before_state:
            raise RuntimeError("PILOT_UNCHANGED_INPUT_REPLAY_MISMATCH")
        del control_state
        dump_csv(paths["manifest"], [{"rank": i + 1, **r} for i, r in enumerate(plan["selected"])])
        dump_csv(paths["before"], snapshot_rows(plan, before, before_state))
        dump_json(
            paths["safety"],
            {
                "database_sha256_before": database_hash_before,
                "wal_sha256_before": wal_before,
                "protected_before": protected_before,
                "old_audit_artifact_hashes": old_artifacts,
                "selection": plan,
                "baseline_complete_before_network": True,
                "unchanged_input_replay_matches_baseline": True,
            },
        )
    # Ensure no external writer changed the audited baseline before the first request.
    if (
        audit.digest(database.path) != database_hash_before
        or (audit.digest(wal) if wal.exists() else None) != wal_before
    ):
        raise RuntimeError("PILOT_BASELINE_DATABASE_CHANGED_BEFORE_NETWORK")
    print(f"Baseline durable. Companyfacts-only force refresh: {len(symbols)} symbols", flush=True)

    def capture(cik, payload):
        symbol = next(symbol for symbol, company in companies.items() if company.cik == cik)
        raw_evidence[symbol] = audit.raw_payload_evidence(payload, symbol)

    result, diagnostics = execute_force(
        database.path, symbols, user_agent, evidence_callback=capture
    )
    dump_json(paths["diagnostics"], {"sync": result, "network": diagnostics})
    with audit.local_guard():
        after = fact_inventory(database, symbols)
        after_state = after_replay(database, config, symbols, after, raw_evidence, context)
        dump_csv(paths["after"], snapshot_rows(plan, after, after_state))
        deltas = delta_rows(
            plan, before, after, before_state, after_state, result["symbol_results"]
        )
        dump_csv(paths["delta"], deltas)
        protected_after = protected_inventory(database)
        metadata_after = metadata_inventory(database)
        database_hash_after = audit.digest(database.path)
        wal_after = audit.digest(wal) if wal.exists() else None
    changed_protected = [
        table for table in protected_before if protected_before[table] != protected_after.get(table)
    ]
    changed = list(changed_protected)
    for table in metadata_before:
        if metadata_before[table] != metadata_after[table]:
            changed.append(table)
    if any(before[s]["rows"] != after[s]["rows"] for s in symbols):
        changed.append("fundamental_facts")
    safety = json.loads(paths["safety"].read_text())
    safety.update(
        {
            "database_sha256_after": database_hash_after,
            "wal_sha256_after": wal_after,
            "protected_after": protected_after,
            "changed_protected_tables": changed_protected,
            "database_tables_changed": sorted(changed),
            "metadata_before": metadata_before,
            "metadata_after": metadata_after,
            "old_audit_artifacts_unchanged": all(
                audit.digest(Path(p)) == h for p, h in old_artifacts.items()
            ),
            "strategy_config_unchanged": config.model_dump(mode="json") == config_dump,
        }
    )
    # Do not export the entire companies/sync_state inventory; store only changed keys.
    for table in metadata_before:
        key = (
            (lambda row: (row["cik"],))
            if table == "companies"
            else (lambda row: (row["source"], row["key"]))
        )
        old = {key(r): r for r in metadata_before[table]}
        new = {key(r): r for r in metadata_after[table]}
        safety.setdefault("metadata_changes", {})[table] = [
            {"key": list(k), "before": old.get(k), "after": new.get(k)}
            for k in sorted(old.keys() | new.keys())
            if old.get(k) != new.get(k)
        ]
    safety.pop("metadata_before")
    safety.pop("metadata_after")
    dump_json(paths["safety"], safety)
    successful = [row for row in deltas if row["synced"]]
    counts = {
        "symbols_with_new_raw_facts": sum(
            row["unique_observations_added"] > 0 for row in successful
        ),
        "symbols_with_new_2022_raw_facts": sum(
            row["unique_2022_pit_observations_added"] > 0 for row in successful
        ),
        "symbols_with_recovered_factors": sum(
            bool(row["quality_factors_recovered"] or row["valuation_factors_recovered"])
            for row in successful
        ),
        "symbols_with_recovered_quality": sum(
            row["quality_recovered_sessions"] > 0 for row in successful
        ),
        "symbols_with_recovered_valuation": sum(
            row["valuation_recovered_sessions"] > 0 for row in successful
        ),
        "symbols_with_any_score_recovery": sum(
            row["quality_recovered_sessions"] > 0 or row["valuation_recovered_sessions"] > 0
            for row in successful
        ),
    }
    summary = {
        "local_head": subprocess.check_output(
            ["git", "-c", "safe.directory=G:/GitHub/TraWalp", "rev-parse", "HEAD"], text=True
        ).strip(),
        "branch": subprocess.check_output(
            ["git", "-c", "safe.directory=G:/GitHub/TraWalp", "branch", "--show-current"], text=True
        ).strip(),
        "period": [str(audit.START), str(audit.END)],
        "sessions": len(sessions),
        "pilot_symbols_requested": len(symbols),
        "pilot_symbols_synced": len(successful),
        "pilot_symbols_failed": len(symbols) - len(successful),
        "symbols": symbols,
        "diagnostics": diagnostics,
        **counts,
        **{
            key: sum(row[key] for row in deltas)
            for key in (
                "facts_added",
                "unique_observations_added",
                "unique_2022_pit_observations_added",
                "observations_with_changed_values",
                "pit_2022_input_changes",
                "duplicate_row_growth",
            )
        },
        "pit_2022_fact_rows_added": sum(
            row["pit_2022_facts_after"] - row["pit_2022_facts_before"] for row in deltas
        ),
        "factor_recovery_rate": counts["symbols_with_recovered_factors"] / len(successful)
        if successful
        else 0,
        "score_recovery_rate": counts["symbols_with_any_score_recovery"] / len(successful)
        if successful
        else 0,
        "factor_recovery": {
            factor: {
                "symbols": sum(
                    row["factor_recovered_session_counts"][factor] > 0 for row in successful
                ),
                "symbol_sessions": sum(
                    row["factor_recovered_session_counts"][factor] for row in successful
                ),
            }
            for factor in FACTORS
        },
        "gap_categories_before": {
            category: sum(len(before_state[s]["gaps"][category]) for s in symbols)
            for category in audit.CATEGORIES
        },
        "gap_categories_after": {
            category: sum(len(after_state[s]["gaps"][category]) for s in symbols)
            for category in audit.CATEGORIES
        },
        "classification_counts": dict(Counter(row["classification"] for row in deltas)),
        "decision": decision(len(successful), counts["symbols_with_any_score_recovery"]),
        "database_tables_changed": sorted(changed),
        "protected_tables_unchanged": not changed_protected,
        "database_sha256_before": database_hash_before,
        "database_sha256_after": database_hash_after,
        "old_audit_artifacts_unchanged": safety["old_audit_artifacts_unchanged"],
        "strategy_changes": 0,
        "portfolio_backtests": 0,
        "files_created": [str(path.relative_to(audit.ROOT)) for path in paths.values()],
    }
    dump_json(paths["summary"], summary)
    paths["audit"].write_text(render_audit(summary, deltas), encoding="utf-8")
    if changed_protected or not safety["old_audit_artifacts_unchanged"]:
        raise RuntimeError("PILOT_SAFETY_VERIFICATION_FAILED_SEE_REPORTS")
    return summary
