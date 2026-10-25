"""Offline 2022 universe/identity evidence audit. No screen, backtest or ingestion."""

from __future__ import annotations

import csv
import json
import re
import subprocess
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import mean, median

from trading_system.backtest import f_data_gap_audit as prior
from trading_system.backtest.features import (
    _cheap_market_exclusions,
    _cheap_market_features,
    _HistoricalBar,
)
from trading_system.backtest.research_registry import validate_champion_config
from trading_system.config import StrategyConfig, _read_yaml
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.data.security_reference import normalize_cik
from trading_system.data.universe import is_financial_or_reit, is_reit
from trading_system.fundamentals.metrics import shares_outstanding_as_of

ROOT, START, END = prior.ROOT, prior.START, prior.END
STEM = "f_2022_historical_universe_audit_v1"
HIGH, MEDIUM, LOW, UNKNOWN = (
    "F_RELEVANCE_" + name for name in ("HIGH", "MEDIUM", "LOW", "UNKNOWN")
)
BIAS_FLAGS = [
    "CURRENT_UNIVERSE_ONLY",
    "NOT_SURVIVORSHIP_CLEAN",
    "CURRENT_SIC_NOT_FULLY_PIT",
    "HISTORICAL_IDENTITY_INCOMPLETE",
]
EVIDENCE_FILES = [
    ROOT / "docs/research/massive-historical-identity-probes-2026-10-15.json",
    ROOT / "docs/research/free_identity_evidence_probe.json",
]
ETF_TYPES = {"ETF", "ETP", "ETN", "FUND", "MUTUAL_FUND", "ETS", "ETV"}
NON_COMMON = {
    "PREFERRED_STOCK",
    "PFD",
    "WARRANT",
    "UNIT",
    "RIGHT",
    "BOND",
    "STRUCTURED_PRODUCT",
    "SP",
    "ADRP",
    "ADRR",
    "ADRW",
}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def write_csv(path, rows, fields=None):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: json.dumps(v, sort_keys=True) if isinstance(v, (list, dict)) else v
                    for k, v in row.items()
                }
            )


def file_hashes(paths):
    return {
        str(path.relative_to(ROOT)): prior.digest(path) if path.exists() else None for path in paths
    }


def universe_sets(current, bars, fact_info, historical_symbols):
    daily = {s for s, r in bars.items() if r["daily_bars_2022"]}
    return {
        "CURRENT_SOURCE_UNIVERSE": set(current),
        "DAILY_2022_UNIVERSE": daily,
        "WARMUP_READY_2022": {s for s in daily if bars[s]["bars_before_2022"] >= 300},
        "PIT_SEC_2022": {s for s, r in fact_info.items() if r["pit_facts_2022"]},
        "HISTORICAL_IDENTITY_EVIDENCE": set(historical_symbols),
        "MISSING_FROM_CURRENT": daily - set(current),
    }


def inventory(database):
    """Index-bounded scans; no symbol-by-session database queries."""
    with database.read_only() as connection:
        companies = {r["symbol"]: dict(r) for r in connection.execute("SELECT * FROM companies")}
        assets = {r["symbol"]: dict(r) for r in connection.execute("SELECT * FROM assets")}
        reference = {
            r["symbol"]: dict(r) for r in connection.execute("SELECT * FROM security_reference")
        }
        epochs = [dict(r) for r in connection.execute("SELECT * FROM symbol_identity_epochs")]
        actions = [
            dict(r)
            for r in connection.execute(
                "SELECT provider,event_key,provider_action_id,action_type,old_symbol,new_symbol,"
                "old_cusip,new_cusip,process_date,effective_date,observed_at,request_reference "
                "FROM security_corporate_actions"
            )
        ]
        snapshots = [
            dict(r) for r in connection.execute("SELECT * FROM security_reference_snapshots")
        ]
        print("Reading indexed Daily inventory", flush=True)
        bars = {
            r["symbol"]: dict(r)
            for r in connection.execute(
                "SELECT symbol,MIN(timestamp) first_daily_date,MAX(timestamp) last_daily_date,"
                "SUM(timestamp<?) bars_before_2022, SUM(timestamp>=? AND timestamp<?) "
                "daily_bars_2022,"
                "MIN(CASE WHEN timestamp>=? AND timestamp<? THEN substr(timestamp,1,10)"
                " END) first_2022_bar,"
                "MAX(CASE WHEN timestamp>=? AND timestamp<? THEN substr(timestamp,1,10)"
                " END) last_2022_bar "
                "FROM bars INDEXED BY ix_bars_symbol_timeframe_timestamp "
                "WHERE timeframe='1d' GROUP BY symbol",
                (
                    str(START),
                    str(START),
                    str(END + timedelta(days=1)),
                    str(START),
                    str(END + timedelta(days=1)),
                    str(START),
                    str(END + timedelta(days=1)),
                ),
            )
        }
        print("Reading compact SEC/CIK inventory", flush=True)
        facts = defaultdict(
            lambda: {
                "fact_count": 0,
                "pit_facts_2022": 0,
                "ciks": [],
                "pit_ciks": [],
                "first_pit_filed": None,
                "first_filed": None,
            }
        )
        for r in connection.execute(
            "SELECT symbol,cik,COUNT(*) fact_count,MIN(filed) first_filed,MAX(filed) last_filed,"
            "SUM(filed<=?) pit_facts_2022,MIN(CASE WHEN filed<=? THEN filed END) first_pit_filed "
            "FROM fundamental_facts GROUP BY symbol,cik",
            (str(END), str(END)),
        ):
            info = facts[r["symbol"]]
            info["fact_count"] += r["fact_count"]
            info["pit_facts_2022"] += r["pit_facts_2022"]
            info["ciks"].append(r["cik"])
            if r["pit_facts_2022"]:
                info["pit_ciks"].append(r["cik"])
                info["first_pit_filed"] = min(
                    info["first_pit_filed"] or r["first_pit_filed"], r["first_pit_filed"]
                )
            info["first_filed"] = min(info["first_filed"] or r["first_filed"], r["first_filed"])
        dates = defaultdict(list)
        for symbol, day in connection.execute(
            "SELECT symbol,substr(timestamp,1,10) FROM bars WHERE timeframe='1d' "
            "AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp",
            (str(START), str(END + timedelta(days=1))),
        ):
            dates[symbol].append(date.fromisoformat(day))
    return {
        "companies": companies,
        "assets": assets,
        "reference": reference,
        "epochs": epochs,
        "actions": actions,
        "snapshots": snapshots,
        "bars": bars,
        "facts": dict(facts),
        "dates": dict(dates),
        "cached_sec": database.sync_value("sec_reference", "ticker_to_cik") or {},
        "current_conflicts": database.unresolved_sec_identity_conflict_symbols(),
    }


def action_map(actions):
    mapped = defaultdict(list)
    for action in actions:
        for symbol in {action.get("old_symbol"), action.get("new_symbol")} - {None, ""}:
            if action not in mapped[symbol]:
                mapped[symbol].append(action)
    return dict(mapped)


def load_artifacts():
    massive = (
        json.loads(EVIDENCE_FILES[0].read_text(encoding="utf-8"))
        if EVIDENCE_FILES[0].exists()
        else {}
    )
    free = (
        json.loads(EVIDENCE_FILES[1].read_text(encoding="utf-8"))
        if EVIDENCE_FILES[1].exists()
        else {}
    )
    observations = defaultdict(list)
    for row in massive.get("observations", []):
        # Do not count nested archived/recovery attempts as additional observations.
        if row.get("operation") in {"details", "events"}:
            observations[row["identifier"]].append(row)
    free_by_symbol = {row["symbol"]: row for row in free.get("symbols", [])}
    for check in free.get("source_checks", []):
        if check.get("source") == "NASDAQ_TRADER":
            for record in check.get("records", []):
                for symbol in {record.get("old_symbol"), record.get("new_symbol")} - {None, ""}:
                    bucket = free_by_symbol.setdefault(symbol, {}).setdefault("nasdaq", [])
                    if record not in bucket:
                        bucket.append(record)
    return dict(observations), free_by_symbol


def cik_classification(current_values, historical_values):
    current = {c for value in current_values if (c := normalize_cik(value))}
    historical = {c for value in historical_values if (c := normalize_cik(value))}
    if not current and not historical:
        return "CIK_MISSING"
    if len(current) > 1 or len(historical) > 1:
        return "CIK_CONFLICT"
    if current and historical:
        return "CIK_STABLE" if current == historical else "CIK_CHANGED"
    return "CIK_UNRESOLVED"


def figi_classification(current_values, historical_values):
    current, historical = set(filter(None, current_values)), set(filter(None, historical_values))
    if not current and not historical:
        return "FIGI_MISSING"
    if len(current) > 1 or len(historical) > 1:
        return "FIGI_UNRESOLVED"
    if current and historical:
        return "FIGI_STABLE" if current == historical else "FIGI_CHANGED"
    return "FIGI_UNRESOLVED"


def security_scope(asset, reference, historical):
    """Explicit local type/name evidence only, with its temporal scope retained."""
    types = {
        r.get("type")
        for r in historical
        if START.isoformat() <= r.get("as_of", "") <= END.isoformat()
    }
    current_type = reference.get("security_type") or reference.get("provider_security_type")
    name = asset.get("name", "").upper()
    evidence = []
    if current_type in ETF_TYPES or re.search(r"\b(ETF|ETN|FUND|PORTFOLIO)\b", name):
        code, positive, strength = "ETF_OR_ETP", False, "WEAK"
        evidence.append("explicit_current_fund_type_or_name; historical_type_not_proven")
    elif current_type in {"ADR", "ADRC", "GDR", "NYRS"} or re.search(
        r"\b(AMERICAN DEPOSITARY|AMERICAN DEPOSITORY|DEPOSITARY SHARES|"
        r"DEPOSITORY SHARES|ADR|ADS)\b",
        name,
    ):
        code, positive, strength = "ADR_OR_OTHER_NON_COMMON", False, "WEAK"
        evidence.append("explicit_current_depositary_type_or_name; historical_type_not_proven")
    elif current_type in NON_COMMON or re.search(
        r"\b(WARRANTS?|PREFERRED|PREFERENCE|UNITS?|RIGHTS?)\b", name
    ):
        code, positive, strength = "KNOWN_NON_COMMON_SECURITY", False, "WEAK"
        evidence.append("explicit_current_non_common_type_or_name; historical_type_not_proven")
    elif current_type == "COMMON_STOCK" or re.search(
        r"\b(COMMON STOCK|COMMON SHARES|ORDINARY SHARES?)\b", name
    ):
        code, positive, strength = "POSSIBLE_HISTORICAL_COMMON_STOCK", True, "WEAK"
        evidence.append(
            "positive_current_common_or_ordinary_share_evidence; not_a_historical_interval"
        )
    else:
        code, positive, strength = "INSUFFICIENT_IDENTITY_EVIDENCE", False, "UNRESOLVED"
        evidence.append("no_explicit_security_type_evidence")
    if types:
        if types == {"CS"}:
            code, positive, strength = "POSSIBLE_HISTORICAL_COMMON_STOCK", True, "MODERATE"
            evidence.append("dated_2022_Massive_common_stock_point_observation")
        elif types <= ETF_TYPES:
            code, positive, strength = "ETF_OR_ETP", False, "MODERATE"
        elif "CS" in types:
            code, positive, strength = "HISTORICAL_SECURITY_TYPE_CHANGE", False, "UNRESOLVED"
    return {
        "class": code,
        "common_plausible": positive,
        "strength": strength,
        "historical_types_2022": sorted(t for t in types if t),
        "reasons": evidence,
    }


def identity_record(symbol, data, observations, free, actions):
    company, asset, ref = (
        data[name].get(symbol, {}) for name in ("companies", "assets", "reference")
    )
    fact = data["facts"].get(symbol, {})
    details = [
        {"as_of": r["as_of"], **r["results"]}
        for r in observations.get(symbol, [])
        if r.get("operation") == "details" and r.get("http_status") == 200 and r.get("results")
    ]
    historical = [r for r in details if r["as_of"] <= str(END)]
    latest_detail_date = max((r["as_of"] for r in details if r["as_of"] > str(END)), default=None)
    current_details = [r for r in details if r["as_of"] == latest_detail_date]
    research = free.get(symbol, {})
    sec_records = [
        r
        for r in research.get("sec", [])
        if r.get("issuer_binding_confirmed") and symbol in r.get("trading_symbols", [])
    ]
    historical_sec = [r for r in sec_records if r.get("filing_date", "9999") <= str(END)]
    historical_ciks = [r.get("cik") for r in historical] + [
        r.get("cik_context") for r in historical_sec
    ]
    current_ciks = [company.get("cik"), data["cached_sec"].get(symbol), ref.get("cik")]
    current_ciks += [r.get("cik") for r in current_details]
    # Normalized facts bind issuer IDs only; they do not prove common-share identity.
    cik = cik_classification(current_ciks, historical_ciks or fact.get("pit_ciks", []))
    historical_figis, current_figis = {}, {}
    states = {}
    current_figi_maps = [
        mapping
        for job in research.get("openfigi", [])
        if job.get("query", {}).get("idType") == "TICKER"
        and job.get("query", {}).get("idValue") == symbol
        for mapping in job.get("mappings", [])
        if mapping.get("ticker") == symbol
    ]
    for field in ("share_class_figi", "composite_figi"):
        old = [r.get(field) for r in historical]
        new = [ref.get(field)] + [r.get(field) for r in current_details]
        new += [
            r.get({"share_class_figi": "shareClassFIGI", "composite_figi": "compositeFIGI"}[field])
            for r in current_figi_maps
        ]
        historical_figis[field], current_figis[field] = (
            sorted(set(filter(None, old))),
            sorted(set(filter(None, new))),
        )
        states[field] = figi_classification(new, old)
    scope = security_scope(asset, ref, historical)
    relevant_actions = actions.get(symbol, [])
    action_disagreements = []
    for action in relevant_actions:
        for notice in research.get("nasdaq", []):
            if (notice.get("old_symbol"), notice.get("new_symbol")) == (
                action.get("old_symbol"),
                action.get("new_symbol"),
            ):
                for key in ("old_cusip", "new_cusip"):
                    if action.get(key) and notice.get(key) and action[key] != notice[key]:
                        action_disagreements.append(
                            {
                                "field": key,
                                "stored_Alpaca": action[key],
                                "stored_Nasdaq": notice[key],
                                "status": "UNRESOLVED_SOURCE_DISAGREEMENT",
                            }
                        )
    epochs = [r for r in data["epochs"] if r["symbol"] == symbol]
    findings = []

    def add(code, strength, reason):
        findings.append({"class": code, "evidence_strength": strength, "reason": reason})

    if cik == "CIK_CONFLICT" or symbol in data["current_conflicts"]:
        add(
            "CURRENT_IDENTITY_MISMATCH",
            "MODERATE",
            "disagreeing_local_SEC_cached_SEC_or_current_reference_CIKs",
        )
    if cik == "CIK_CHANGED":
        strength = "STRONG" if historical_sec and historical else "MODERATE"
        add(
            "HISTORICAL_CIK_CONFLICT",
            strength,
            "historical_CIK_differs_from_current_consensus; no_transition_date_inferred",
        )
        if research.get("free_sec_different_issuers_proven"):
            add(
                "TICKER_REUSE",
                strength,
                "independent_stored_SEC_filings_and_Massive_observations_bind_same_ticker_to_different_issuers",
            )
    if any(value == "FIGI_CHANGED" for value in states.values()):
        add(
            "CURRENT_IDENTITY_MISMATCH",
            "MODERATE",
            "different_historical_and_current_security_FIGIs",
        )
    historical_types = {r.get("type") for r in historical if r.get("type")}
    current_types = {r.get("type") for r in current_details if r.get("type")}
    if historical_types and current_types and historical_types != current_types:
        add(
            "HISTORICAL_SECURITY_TYPE_CHANGE",
            "MODERATE",
            "different_dated_provider_type_points; transition_interval_unknown",
        )
    if relevant_actions:
        add(
            "TICKER_CHANGE",
            "MODERATE",
            "stored_old_new_symbol_action; process_date_is_not_an_inferred_effective_interval",
        )
    if any(r.get("delisted_utc") for r in details):
        add("DELISTED_SECURITY", "MODERATE", "explicit_provider_delisted_utc_point_evidence")
    if (
        asset.get("exchange") == "OTC"
        or ref.get("market") not in (None, "", "stocks")
        or ref.get("locale") not in (None, "", "us")
    ):
        add(
            "OTC_OR_OUTSIDE_SCOPE",
            "WEAK",
            "explicit_current_exchange_market_or_locale; not_historical_membership_proof",
        )
    if scope["class"] not in {"INSUFFICIENT_IDENTITY_EVIDENCE", "POSSIBLE_HISTORICAL_COMMON_STOCK"}:
        add(scope["class"], scope["strength"], "; ".join(scope["reasons"]))
    if asset.get("tradable") == 0:
        add(
            "CURRENTLY_INACTIVE",
            "WEAK",
            "TraWalp_asset_tradable_is_false; does_not_prove_market_delisting",
        )
    late = bool(fact.get("first_filed") and fact["first_filed"] > str(END))
    if late:
        add(
            "INSUFFICIENT_IDENTITY_EVIDENCE",
            "UNRESOLVED",
            "all_local_normalized_facts_for_mapping_first_filed_after_2022; "
            "not_proof_of_new_issuer",
        )
    if any(r.get("list_date", "") > str(END) for r in details) and data["bars"].get(symbol, {}).get(
        "daily_bars_2022"
    ):
        add(
            "INSUFFICIENT_IDENTITY_EVIDENCE",
            "MODERATE",
            "current_security_list_date_after_2022_bars; no_backdated_membership_assumed",
        )
    if not findings:
        add(scope["class"], scope["strength"], "; ".join(scope["reasons"]))
    order = [
        "TICKER_REUSE",
        "HISTORICAL_CIK_CONFLICT",
        "CURRENT_IDENTITY_MISMATCH",
        "HISTORICAL_SECURITY_TYPE_CHANGE",
        "TICKER_CHANGE",
        "DELISTED_SECURITY",
        "ETF_OR_ETP",
        "ADR_OR_OTHER_NON_COMMON",
        "KNOWN_NON_COMMON_SECURITY",
        "OTC_OR_OUTSIDE_SCOPE",
        "CURRENTLY_INACTIVE",
        "INSUFFICIENT_IDENTITY_EVIDENCE",
        "POSSIBLE_HISTORICAL_COMMON_STOCK",
        "UNKNOWN",
    ]
    primary = min(findings, key=lambda f: order.index(f["class"]))
    # Strength of historical identity evidence, not confidence in today's inactive bit.
    historical_strength = (
        "STRONG"
        if any(f["evidence_strength"] == "STRONG" for f in findings)
        else (
            "MODERATE"
            if historical or historical_sec or relevant_actions or epochs or research.get("nasdaq")
            else "UNRESOLVED"
        )
    )
    conflicts = {
        "TICKER_REUSE",
        "HISTORICAL_CIK_CONFLICT",
        "CURRENT_IDENTITY_MISMATCH",
        "HISTORICAL_SECURITY_TYPE_CHANGE",
    }
    identity_conflict = any(f["class"] in conflicts for f in findings)
    stable_epoch = False
    # Only consume already verified, identity-bound intervals; never manufacture one.
    from trading_system.data.security_identity import current_identity_epoch

    resolved_epoch = current_identity_epoch(
        {
            "symbol": symbol,
            "cik": company.get("cik"),
            "share_class_figi": ref.get("share_class_figi"),
            "composite_figi": ref.get("composite_figi"),
        },
        epochs,
    )
    if resolved_epoch and not identity_conflict:
        stable_epoch = True
    return {
        "symbol": symbol,
        "has_2022_daily_bars": bool(data["bars"].get(symbol, {}).get("daily_bars_2022")),
        "current_source_member": bool(company and asset.get("tradable")),
        "current_details_as_of": latest_detail_date,
        "intermediate_details": [
            r for r in details if str(END) < r["as_of"] < (latest_detail_date or "")
        ],
        "identity_class": primary["class"],
        "evidence_strength": historical_strength,
        "findings": findings,
        "local_cik": company.get("cik"),
        "cached_sec_cik": normalize_cik(data["cached_sec"].get(symbol)),
        "current_massive_cik": ref.get("cik"),
        "historical_massive_ciks": sorted(
            {normalize_cik(r.get("cik")) for r in historical if normalize_cik(r.get("cik"))}
        ),
        "historical_ciks": sorted({normalize_cik(c) for c in historical_ciks if normalize_cik(c)}),
        "normalized_fact_pit_ciks": fact.get("pit_ciks", []),
        "cik_class": cik,
        "current_figis": current_figis,
        "historical_figis": historical_figis,
        "figi_classes": states,
        "scope": scope,
        "historical_identity_evidence": bool(
            historical or historical_sec or relevant_actions or epochs or research.get("nasdaq")
        ),
        "has_2022_dated_details": any(str(START) <= r["as_of"] <= str(END) for r in historical),
        "identity_conflict": identity_conflict,
        "late_first_local_filing": late,
        "identity_review_status": "VERIFIED_EXISTING_INTERVAL"
        if stable_epoch
        else "REQUIRES_IDENTITY_REVIEW",
        "verified_existing_epoch": resolved_epoch,
        "massive_observations": observations.get(symbol, []),
        "stored_corporate_actions": relevant_actions,
        "stored_epochs": epochs,
        "stored_SEC_identity_filings": sec_records,
        "stored_Nasdaq_evidence": research.get("nasdaq", []),
        "stored_OpenFIGI_evidence": research.get("openfigi", []),
        "corporate_action_disagreements": action_disagreements,
        "prior_research_conflicts": research.get("conflicts", []),
        "reason": "; ".join(f["reason"] for f in findings),
    }


def market_gates(database, config, symbols, sessions, fact_info):
    grouped, shares = defaultdict(list), defaultdict(list)
    for batch in database.iter_bar_value_batches(symbols, date(1900, 1, 1), END):
        for symbol, timestamp, high, low, close, volume in batch:
            grouped[symbol].append(
                _HistoricalBar(
                    symbol,
                    date.fromisoformat(str(timestamp)[:10]),
                    float(high),
                    float(low),
                    Decimal(str(close)),
                    int(volume),
                )
            )
    for batch in database.iter_fact_batches(symbols, END, metrics=("shares_outstanding",)):
        for fact in batch:
            shares[fact.symbol].append(fact)
    result = {}
    for symbol in symbols:
        features = _cheap_market_features(grouped.get(symbol, []), sessions, config)
        record = {
            name: set()
            for name in (
                "fresh",
                "warmup",
                "price",
                "adv",
                "cheap",
                "market_cap_input",
                "market_cap",
                "pit",
                "joint",
            )
        }
        first_pit = fact_info.get(symbol, {}).get("first_pit_filed")
        for session, feature in features.items():
            fresh = feature.technical.market_session == session
            if not fresh:
                continue
            record["fresh"].add(session)
            if feature.history_count >= config.data_quality.min_market_history_days:
                record["warmup"].add(session)
            if feature.price >= Decimal(str(config.universe.min_price)):
                record["price"].add(session)
            if (
                feature.average_dollar_volume_20d is not None
                and feature.average_dollar_volume_20d >= config.universe.min_avg_dollar_volume_20d
            ):
                record["adv"].add(session)
            if not _cheap_market_exclusions(feature, config, session):
                record["cheap"].add(session)
            selected = shares_outstanding_as_of(shares.get(symbol, []), session)
            if selected is not None:
                record["market_cap_input"].add(session)
                if feature.price * selected.value >= Decimal(str(config.universe.min_market_cap)):
                    record["market_cap"].add(session)
            if first_pit and first_pit <= str(session):
                record["pit"].add(session)
        record["joint"] = record["cheap"] & record["market_cap"] & record["pit"]
        result[symbol] = record
    return result


def relevance(identity, bar, fact, gates, config, company):
    classes = {f["class"] for f in identity["findings"]}
    if identity["identity_conflict"]:
        return (
            UNKNOWN,
            "historical_identity_conflict; "
            "current_types_and_market_inputs_cannot_resolve_past_security",
        )
    if classes & {
        "ETF_OR_ETP",
        "ADR_OR_OTHER_NON_COMMON",
        "KNOWN_NON_COMMON_SECURITY",
        "OTC_OR_OUTSIDE_SCOPE",
    }:
        return (
            LOW,
            "explicit_local_non_common_or_out_of_scope_evidence; "
            "historical_type_not_fully_verified",
        )
    if (config.universe.exclude_reits and is_reit(company.get("sic"))) or (
        config.universe.exclude_financials and is_financial_or_reit(company.get("sic"))
    ):
        return LOW, "existing_champion_current_SIC_static_gate; CURRENT_SIC_NOT_FULLY_PIT"
    if not gates["warmup"]:
        return LOW, "no_fresh_2022_session_reaches_existing_300_bar_history_gate"
    if not gates["price"] or not gates["adv"] or not gates["cheap"]:
        return LOW, "no_2022_session_passes_existing_price_liquidity_history_gates"
    if (
        normalize_cik(identity.get("local_cik")) is None
        or not identity["scope"]["common_plausible"]
    ):
        return (
            UNKNOWN,
            "historical_identity_or_security_type_requires_review; no_automatic_proxy_inclusion",
        )
    if gates["joint"] and bar["bars_before_2022"] >= 300:
        return (
            HIGH,
            "positive_common_share_indication_and_joint_market_cap_PIT_price_ADV_history_evidence;"
            " identity_still_unverified",
        )
    if gates["joint"]:
        return MEDIUM, "joint_gates_possible_later_in_2022_but_initial_300_bar_warmup_not_ready"
    if not gates["market_cap_input"] or not fact.get("pit_facts_2022"):
        return MEDIUM, "price_liquidity_history_plausible_but_market_cap_or_PIT_input_missing"
    return LOW, "market_cap_input_exists_but_no_joint_2022_F_universe_gate_session"


def proxy_sessions(identity, gates):
    epoch = identity.get("verified_existing_epoch")
    if not epoch or identity["identity_conflict"] or not identity["scope"]["common_plausible"]:
        return set()
    return {
        session
        for session in gates["warmup"]
        if epoch["effective_start"] <= str(session)
        and (epoch["effective_end"] is None or str(session) < epoch["effective_end"])
    }


def pilot_order(rows, limit=20):
    selected = [
        r
        for r in rows
        if r["f_relevance"] in {HIGH, MEDIUM}
        and r["evidence_strength"] in {"MODERATE", "UNRESOLVED"}
        and r["requires_external_identity_probe"]
    ]
    return sorted(
        selected,
        key=lambda r: (
            r["f_relevance"] != HIGH,
            not r["warmup_ready_before_2022"],
            not bool(r["pit_facts_2022"]),
            -r.get("daily_sessions_2022", r["daily_bars_2022"]),
            r["evidence_strength"] != "MODERATE",
            r["symbol"],
        ),
    )[:limit]


def symbol_row(symbol, source, data, identity, gates, config):
    bar, fact, asset, company = (
        data[name].get(symbol, {}) for name in ("bars", "facts", "assets", "companies")
    )
    f_relevance, reason = relevance(identity, bar, fact, gates, config, company)
    proxy = proxy_sessions(identity, gates)
    return {
        "symbol": symbol,
        "first_2022_bar": bar["first_2022_bar"],
        "last_2022_bar": bar["last_2022_bar"],
        "daily_bars_2022": bar["daily_bars_2022"],
        "bars_before_2022": bar["bars_before_2022"],
        "daily_sessions_2022": len(set(data["dates"].get(symbol, []))),
        "warmup_ready_before_2022": bar["bars_before_2022"] >= 300,
        "current_source_member": symbol in source,
        "current_tradable": bool(asset.get("tradable")),
        "current_exchange": asset.get("exchange"),
        "source_exclusion_reason": (
            "CURRENT_SOURCE_MEMBER"
            if symbol in source
            else "NO_CURRENT_COMPANY"
            if not company
            else "NO_CURRENT_ASSET"
            if not asset
            else "CURRENT_ASSET_NOT_TRADABLE"
        ),
        "current_reference_type": data["reference"].get(symbol, {}).get("security_type"),
        "current_reference_active": data["reference"].get(symbol, {}).get("active"),
        "current_asset_class": asset.get("asset_class"),
        "current_asset_name": asset.get("name"),
        "current_sic": company.get("sic"),
        "local_cik": identity["local_cik"],
        "cached_sec_cik": identity["cached_sec_cik"],
        "historical_cik": "|".join(identity["historical_ciks"]),
        "cik_class": identity["cik_class"],
        "current_massive_cik": identity["current_massive_cik"],
        "historical_massive_cik": "|".join(identity["historical_massive_ciks"]),
        "share_class_figi": "|".join(identity["current_figis"]["share_class_figi"]),
        "composite_figi": "|".join(identity["current_figis"]["composite_figi"]),
        "historical_share_class_figi": "|".join(identity["historical_figis"]["share_class_figi"]),
        "historical_composite_figi": "|".join(identity["historical_figis"]["composite_figi"]),
        "share_class_figi_class": identity["figi_classes"]["share_class_figi"],
        "composite_figi_class": identity["figi_classes"]["composite_figi"],
        "identity_class": identity["identity_class"],
        "evidence_strength": identity["evidence_strength"],
        "historical_identity_evidence": identity["historical_identity_evidence"],
        "historical_common_stock_plausible": identity["scope"]["common_plausible"],
        "historical_identity_status": identity["identity_review_status"],
        "f_relevance": f_relevance,
        "pit_facts_2022": fact.get("pit_facts_2022", 0),
        "market_cap_input": bool(gates["market_cap_input"]),
        "adv20_possible": bool(gates["adv"]),
        "price_possible": bool(gates["price"]),
        "warmup_ready_session_count": len(gates["warmup"]),
        "market_cap_input_sessions": len(gates["market_cap_input"]),
        "market_cap_gate_sessions": len(gates["market_cap"]),
        "price_liquidity_history_sessions": len(gates["cheap"]),
        "joint_potential_F_gate_sessions": len(gates["joint"]),
        "possible_2022_F_universe_proxy": bool(proxy),
        "proxy_session_count": len(proxy),
        "requires_external_identity_probe": f_relevance in {HIGH, MEDIUM} and not proxy,
        "reason": reason + "; " + identity["reason"],
    }


def gap_type(identity_class):
    return {
        "DELISTED_SECURITY": "DELISTED",
        "TICKER_CHANGE": "TICKER_CHANGE",
        "TICKER_REUSE": "TICKER_REUSE",
        "HISTORICAL_CIK_CONFLICT": "IDENTITY_CONFLICT",
        "CURRENT_IDENTITY_MISMATCH": "IDENTITY_CONFLICT",
        "HISTORICAL_SECURITY_TYPE_CHANGE": "IDENTITY_CONFLICT",
        "CURRENTLY_INACTIVE": "CURRENTLY_INACTIVE",
        "ETF_OR_ETP": "NON_COMMON",
        "KNOWN_NON_COMMON_SECURITY": "NON_COMMON",
        "ADR_OR_OTHER_NON_COMMON": "NON_COMMON",
        "OTC_OR_OUTSIDE_SCOPE": "OUTSIDE_SCOPE",
    }.get(identity_class, "UNKNOWN")


def aggregate_gaps(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[gap_type(row["identity_class"])].append(row)
    return [
        {
            "gap_type": key,
            "symbols": len(values),
            "symbol_sessions": sum(r["daily_bars_2022"] for r in values),
            **{
                name.lower() + "_relevance": sum(
                    r["f_relevance"] == "F_RELEVANCE_" + name for r in values
                )
                for name in ("HIGH", "MEDIUM", "LOW", "UNKNOWN")
            },
        }
        for key, values in sorted(groups.items())
    ]


def session_bias(sessions, sets, data, rows, gates, identities):
    result = []
    daily = sets["DAILY_2022_UNIVERSE"]
    current = sets["CURRENT_SOURCE_UNIVERSE"]
    for session in sessions:
        ready = {
            s
            for s in daily
            if data["bars"][s]["bars_before_2022"] + bisect_right(data["dates"].get(s, []), session)
            >= 300
        }
        proxy = {
            s
            for s in identities
            if s in gates and session in proxy_sessions(identities[s], gates[s])
        }
        potential = {
            r["symbol"]
            for r in rows
            if r["f_relevance"] in {HIGH, MEDIUM} and session in gates[r["symbol"]]["joint"]
        }
        result.append(
            {
                "session": str(session),
                "current_source_symbols": len(current),
                "current_source_symbols_with_2022_bars": len(current & daily),
                "historical_proxy_symbols": len(proxy),
                "warmup_ready_current": len(ready & current),
                "warmup_ready_proxy": len(ready & proxy),
                "potentially_F_relevant_missing_symbols": len(potential),
                "unresolved_missing_with_price_liquidity_history": sum(
                    r["f_relevance"] in {HIGH, MEDIUM, UNKNOWN}
                    and session in gates[r["symbol"]]["cheap"]
                    for r in rows
                ),
                "missing_symbols_with_fresh_2022_bars": sum(
                    session in data["dates"].get(s, []) for s in sets["MISSING_FROM_CURRENT"]
                ),
            }
        )
    return result


def decisions(rows, session_rows, typical_final_count):
    high = [r for r in rows if r["f_relevance"] == HIGH]
    potential = [r["potentially_F_relevant_missing_symbols"] for r in session_rows]
    months = {r["session"][:7] for r in session_rows if r["potentially_F_relevant_missing_symbols"]}
    if high and len(months) == 12 and median(potential) >= typical_final_count:
        severity = "HISTORICAL_UNIVERSE_BIAS_MATERIAL"
    elif any(r["f_relevance"] in {HIGH, MEDIUM} for r in rows):
        severity = "HISTORICAL_UNIVERSE_BIAS_PRESENT_BUT_LIMITED"
    elif any(r["f_relevance"] == UNKNOWN for r in rows):
        severity = "HISTORICAL_UNIVERSE_BIAS_NOT_QUANTIFIABLE"
    else:
        severity = "HISTORICAL_UNIVERSE_BIAS_LOW"
    result = (
        "2022_F_RESULT_REQUIRES_UNIVERSE_RECONSTRUCTION"
        if severity.endswith("_MATERIAL")
        else "2022_F_RESULT_UNIVERSE_EVIDENCE_INSUFFICIENT"
        if severity.endswith("_NOT_QUANTIFIABLE")
        else "2022_F_RESULT_INTERPRETABLE_WITH_BIAS_CAVEAT"
    )
    pilot = (
        "MASSIVE_HISTORICAL_IDENTITY_PILOT_JUSTIFIED"
        if pilot_order(rows)
        else "MASSIVE_HISTORICAL_IDENTITY_PILOT_LOW_VALUE"
        if any(r["f_relevance"] == UNKNOWN for r in rows)
        else "MASSIVE_HISTORICAL_IDENTITY_PILOT_NOT_NEEDED"
    )
    return severity, result, pilot


def main():
    out = ROOT / "reports"
    if (out / f"{STEM}_summary.json").exists():
        raise FileExistsError("FRESH_HISTORICAL_UNIVERSE_AUDIT_STEM_ALREADY_EXISTS")
    config = StrategyConfig.model_validate(_read_yaml(ROOT / "config/strategy.yaml"))
    validate_champion_config(config)
    database = prior.AuditDatabase(ROOT / config.storage.database_path)
    db_paths = [database.path, Path(str(database.path) + "-wal")]
    source_paths = list((ROOT / "src/trading_system").rglob("*.py")) + [
        ROOT / "config/strategy.yaml"
    ]
    old_paths = [
        p for p in out.glob("f_2022_*_v1_*") if p.is_file() and not p.name.startswith(STEM)
    ]
    unchanged_paths = EVIDENCE_FILES + old_paths + source_paths
    print("Hashing SQLite/WAL before offline audit", flush=True)
    db_before, files_before = file_hashes(db_paths), file_hashes(unchanged_paths)
    with prior.local_guard() as safety:
        data = inventory(database)
        source = {company.symbol for company in database.list_tradable_companies()}
        observations, free = load_artifacts()
        mapped_actions = action_map(data["actions"])
        all_symbols = (
            source
            | {s for s, r in data["bars"].items() if r["daily_bars_2022"]}
            | set(observations)
            | set(free)
            | set(mapped_actions)
        )
        identities = {
            s: identity_record(s, data, observations, free, mapped_actions)
            for s in sorted(all_symbols)
        }
        historical_symbols = {s for s, r in identities.items() if r["historical_identity_evidence"]}
        sets = universe_sets(source, data["bars"], data["facts"], historical_symbols)
        missing = sorted(sets["MISSING_FROM_CURRENT"])
        review_inside = sorted(
            s
            for s in source & sets["DAILY_2022_UNIVERSE"]
            if identities[s]["identity_conflict"]
            or identities[s]["late_first_local_filing"]
            or identities[s]["historical_identity_evidence"]
            or identities[s]["scope"]["class"]
            in {"ETF_OR_ETP", "ADR_OR_OTHER_NON_COMMON", "KNOWN_NON_COMMON_SECURITY"}
        )
        focus = sorted(set(missing) | set(review_inside))
        sessions = trading_sessions_between(START, END)
        print(
            f"Preparing only local market/input gates for {len(focus)} diagnostic symbols",
            flush=True,
        )
        gates = market_gates(database, config, focus, sessions, data["facts"])
        rows = [symbol_row(s, source, data, identities[s], gates[s], config) for s in missing]
        inside_rows = [
            symbol_row(s, source, data, identities[s], gates[s], config) for s in review_inside
        ]
        session_rows = session_bias(sessions, sets, data, rows, gates, identities)
        gaps, pilot = aggregate_gaps(rows), pilot_order([*rows, *inside_rows])
        pilot = [
            {
                **row,
                "proposed_historical_details_as_of": str(
                    min(gates[row["symbol"]]["joint"] or gates[row["symbol"]]["cheap"])
                ),
                "proposed_ticker_events_identifier": row["symbol"],
                "proposed_logical_request_limit": 2,
                "probe_goal": "verify_2022_security_type_CIK_FIGI; no_interval_inference",
            }
            for row in pilot
        ]
        # A strict historical proxy consumes verified existing intervals only.
        # Current classifications and isolated dated points remain in review queues.
        sets["POSSIBLE_2022_F_UNIVERSE"] = {
            s for s in focus if proxy_sessions(identities[s], gates[s])
        }
        with (out / f"{prior.STEM}_session_funnel.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            old_funnel = list(csv.DictReader(handle))
        reference_median = median(int(r["final_f_candidate_count"]) for r in old_funnel)
        severity, result_decision, pilot_decision = decisions(rows, session_rows, reference_median)
        if pilot:
            pilot_decision = "MASSIVE_HISTORICAL_IDENTITY_PILOT_JUSTIFIED"
        potential = [r["potentially_F_relevant_missing_symbols"] for r in session_rows]
        summary = {
            "local_head": subprocess.check_output(
                ["git", "-c", "safe.directory=G:/GitHub/TraWalp", "rev-parse", "HEAD"], text=True
            ).strip(),
            "branch": subprocess.check_output(
                ["git", "-c", "safe.directory=G:/GitHub/TraWalp", "branch", "--show-current"],
                text=True,
            ).strip(),
            "champion": "F/configured/C1",
            "period": [str(START), str(END)],
            "sessions": len(sessions),
            "set_counts": {key: len(value) for key, value in sets.items()},
            "daily_inside_current": len(sets["DAILY_2022_UNIVERSE"] & source),
            "daily_with_PIT_SEC": len(sets["DAILY_2022_UNIVERSE"] & sets["PIT_SEC_2022"]),
            "initial_warmup_all_daily_symbols": sum(
                r["bars_before_2022"] >= 300 for r in data["bars"].values()
            ),
            "initial_warmup_without_2022_bars": sorted(
                s
                for s, r in data["bars"].items()
                if r["bars_before_2022"] >= 300 and not r["daily_bars_2022"]
            ),
            "outside_current": {
                "symbols": len(rows),
                "with_300_bar_warmup": len(
                    sets["MISSING_FROM_CURRENT"] & sets["WARMUP_READY_2022"]
                ),
                "with_PIT_fundamentals": len(sets["MISSING_FROM_CURRENT"] & sets["PIT_SEC_2022"]),
                "with_both": len(
                    sets["MISSING_FROM_CURRENT"] & sets["WARMUP_READY_2022"] & sets["PIT_SEC_2022"]
                ),
                "with_historical_identity_evidence": sum(
                    r["historical_identity_evidence"] for r in rows
                ),
                "possible_historical_common_stocks": sum(
                    r["historical_common_stock_plausible"] for r in rows
                ),
                "identity_unresolved": sum(
                    r["historical_identity_status"] == "REQUIRES_IDENTITY_REVIEW" for r in rows
                ),
                "potential_F_relevance": sum(r["f_relevance"] in {HIGH, MEDIUM} for r in rows),
                "relevance": dict(Counter(r["f_relevance"] for r in rows)),
                "identity_classes": dict(Counter(r["identity_class"] for r in rows)),
                "evidence_strength": dict(Counter(r["evidence_strength"] for r in rows)),
                "source_exclusion_reasons": dict(
                    Counter(r["source_exclusion_reason"] for r in rows)
                ),
                "low_relevance_reasons": dict(
                    Counter(r["reason"].split(";")[0] for r in rows if r["f_relevance"] == LOW)
                ),
                "inactive_asset_but_active_reference": [
                    r["symbol"]
                    for r in rows
                    if not r["current_tradable"] and r["current_reference_active"] == 1
                ],
            },
            "current_identity_review": {
                "symbols": len(inside_rows),
                "historical_CIK_changed_symbols": [
                    r["symbol"] for r in inside_rows if r["cik_class"] == "CIK_CHANGED"
                ],
                "current_CIK_conflict_symbols": [
                    r["symbol"] for r in inside_rows if r["cik_class"] == "CIK_CONFLICT"
                ],
                "late_first_local_filing_symbols": [
                    s for s in review_inside if identities[s]["late_first_local_filing"]
                ],
                "historical_type_change_symbols": [
                    s
                    for s in review_inside
                    if any(
                        f["class"] == "HISTORICAL_SECURITY_TYPE_CHANGE"
                        for f in identities[s]["findings"]
                    )
                ],
                "relevance": dict(Counter(r["f_relevance"] for r in inside_rows)),
                "coverage_or_identity_unresolved_market_gate_cases": [
                    r["symbol"]
                    for r in inside_rows
                    if r["f_relevance"] == MEDIUM
                    and r["pit_facts_2022"] == 0
                    and r["price_liquidity_history_sessions"] > 0
                ],
                "historical_identity_gate_bounds": [
                    {
                        "symbol": r["symbol"],
                        "bars_before_2022": r["bars_before_2022"],
                        "daily_bars_2022": r["daily_bars_2022"],
                        "maximum_local_2022_history": r["bars_before_2022"] + r["daily_bars_2022"],
                        "cheap_gate_sessions": r["price_liquidity_history_sessions"],
                    }
                    for r in inside_rows
                    if r["historical_identity_evidence"]
                ],
                "historical_identity_examples_without_2022_bars": [
                    s
                    for s in observations
                    if identities[s]["historical_identity_evidence"]
                    and not identities[s]["has_2022_daily_bars"]
                ],
                "with_historical_identity_evidence": sum(
                    r["historical_identity_evidence"] for r in inside_rows
                ),
                "strong_historical_evidence": sum(
                    r["evidence_strength"] == "STRONG" for r in inside_rows
                ),
            },
            "potential_missing_session_proxy": {
                "mean": mean(potential),
                "median": median(potential),
                "minimum": min(potential),
                "maximum": max(potential),
                "sessions_nonzero": sum(v > 0 for v in potential),
                "months_nonzero": sorted(
                    {
                        r["session"][:7]
                        for r in session_rows
                        if r["potentially_F_relevant_missing_symbols"]
                    }
                ),
            },
            "previous_audit_typical_final_F_candidate_count": reference_median,
            "massive_pilot_candidates": [r["symbol"] for r in pilot],
            "massive_pilot_candidate_sources": {
                "outside_current_source": sum(not r["current_source_member"] for r in pilot),
                "inside_current_identity_review": sum(r["current_source_member"] for r in pilot),
            },
            "gap_types": gaps,
            "historical_universe_bias_decision": severity,
            "2022_F_result_decision": result_decision,
            "massive_pilot_decision": pilot_decision,
            "retained_bias_flags": BIAS_FLAGS,
            "proxy_semantics": (
                "DIAGNOSTIC PROXY ONLY; verified existing intervals; "
                "all unresolved cases fail closed"
            ),
            "market_gate_thresholds": {
                "min_history": config.data_quality.min_market_history_days,
                "price": config.universe.min_price,
                "ADV20": config.universe.min_avg_dollar_volume_20d,
                "market_cap": config.universe.min_market_cap,
                "adjustment": config.universe.market_data_adjustment,
            },
            "database_sha256_before": db_before,
            "input_artifact_hashes": file_hashes(EVIDENCE_FILES),
            "safety": safety,
        }
        evidence_focus = sorted(set(focus) | set(observations) | set(free))
        evidence = {
            "semantics": "POINT_OBSERVATIONS_NOT_VERIFIED_SECURITY_INTERVALS",
            "sources": [str(p.relative_to(ROOT)) for p in EVIDENCE_FILES if p.exists()],
            "security_reference_snapshots": [
                {
                    "snapshot_id": r["snapshot_id"],
                    "observed_at": json.loads(r["metadata"]).get("observed_at"),
                    "semantics": json.loads(r["metadata"]).get("reference_semantics"),
                }
                for r in data["snapshots"]
            ],
            "stored_epoch_count": len(data["epochs"]),
            "stored_corporate_actions": data["actions"],
            "sets": {
                key: sorted(value)
                for key, value in sets.items()
                if key
                not in {
                    "CURRENT_SOURCE_UNIVERSE",
                    "DAILY_2022_UNIVERSE",
                    "PIT_SEC_2022",
                    "WARMUP_READY_2022",
                }
            },
            "symbols": {s: identities[s] for s in evidence_focus},
        }
    print("Hashing SQLite/WAL after offline audit", flush=True)
    summary["database_sha256_after"] = file_hashes(db_paths)
    summary["database_unchanged"] = summary["database_sha256_after"] == db_before
    summary["existing_source_config_and_audits_unchanged"] = (
        file_hashes(unchanged_paths) == files_before
    )
    summary["safety"].update(
        {"massive_calls": 0, "sec_calls": 0, "alpaca_calls": 0, "broker_calls": 0}
    )
    if (
        not summary["database_unchanged"]
        or not summary["existing_source_config_and_audits_unchanged"]
    ):
        raise RuntimeError("HISTORICAL_UNIVERSE_AUDIT_SAFETY_HASH_MISMATCH")
    paths = {
        name: out / f"{STEM}_{name}.{extension}"
        for name, extension in (
            ("summary", "json"),
            ("symbol_manifest", "csv"),
            ("gap_types", "csv"),
            ("session_bias", "csv"),
            ("massive_pilot_candidates", "csv"),
            ("identity_evidence", "json"),
            ("audit", "md"),
            ("current_identity_review", "csv"),
        )
    }
    summary["files_created"] = [str(p.relative_to(ROOT)) for p in paths.values()]
    write_csv(paths["symbol_manifest"], rows)
    write_csv(paths["gap_types"], gaps)
    write_csv(paths["session_bias"], session_rows)
    write_csv(
        paths["massive_pilot_candidates"],
        [{"rank": i + 1, **r} for i, r in enumerate(pilot)],
        fields=[
            "rank",
            *rows[0],
            "proposed_historical_details_as_of",
            "proposed_ticker_events_identifier",
            "proposed_logical_request_limit",
            "probe_goal",
        ],
    )
    write_csv(paths["current_identity_review"], inside_rows, fields=list(rows[0]))
    write_json(paths["identity_evidence"], evidence)
    write_json(paths["summary"], summary)
    paths["audit"].write_text(render_audit(summary, rows, inside_rows, pilot), encoding="utf-8")
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "set_counts",
                    "outside_current",
                    "potential_missing_session_proxy",
                    "historical_universe_bias_decision",
                    "2022_F_result_decision",
                    "massive_pilot_decision",
                )
            },
            indent=2,
        )
    )
    return summary


def render_audit(summary, rows, inside, pilot):
    counts = summary["outside_current"]
    changed_ciks = (
        ", ".join(summary["current_identity_review"]["historical_CIK_changed_symbols"]) or "keine"
    )
    current_conflicts = (
        ", ".join(summary["current_identity_review"]["current_CIK_conflict_symbols"]) or "keine"
    )
    lines = [
        "# F/configured/C1: Historical Universe / Identity Audit 2022",
        "",
        "Rein lokal; kein Screen-Replay, keine Score-Neuberechnung, keine Portfoliosimulation. "
        "Keine produktive Universe- oder Identity-Reparatur.",
        "",
        "## Die neun Auditfragen",
        "",
        f"1. **{counts['symbols']}** 2022-Daily-Symbole fehlen im heutigen Source-Universe. "
        f"{summary['daily_inside_current']} liegen innerhalb; "
        f"{summary['set_counts']['DAILY_2022_UNIVERSE']} haben insgesamt lokale 2022-Bars.",
        "",
        f"2. **{counts['with_300_bar_warmup']}** fehlende Symbole hatten vor "
        "2022-01-03 mindestens 300 Bars.",
        "",
        f"3. **{counts['with_PIT_fundamentals']}** hatten mindestens einen Fact "
        "mit filed <= 2022-12-30; "
        f"**{counts['with_both']}** zusätzlich den initialen Warmup. Fact-Zeilen können bekannte "
        "Duplikate enthalten; hier zählt für die Mengen nur die Existenz.",
        "",
        f"4. **{counts['relevance'].get(LOW, 0)}** sind nach den vorhandenen "
        "Scope-/Marktgates LOW. "
        "Heutige Namen/Typen und heutiger SIC sind keine vollständig PIT-verifizierten "
        "Ausschlüsse.",
        "",
        f"5. **{counts['possible_historical_common_stocks']}** haben positive lokale "
        "Common-/Ordinary-Share-Indizien. "
        f"Davon werden **{counts['relevance'].get(HIGH, 0)} HIGH** und "
        f"**{counts['relevance'].get(MEDIUM, 0)} MEDIUM** priorisiert. "
        "Plausibilität ist keine automatische Aufnahme in das historische Proxy-Universe.",
        "",
        f"6. Unter den fehlenden Symbolen haben **{counts['with_historical_identity_evidence']}** "
        "historische Identity-Evidenz. Historische Befunde innerhalb des heutigen "
        "Universe werden separat gezählt.",
        "",
        f"7. **{counts['identity_unresolved']}** fehlende Symbole besitzen kein verifiziertes "
        "historisches Security-Intervall. Das ist unabhängig von ihrer Marktrelevanz.",
        "",
        f"8. **{summary['historical_universe_bias_decision']}**. "
        f"Potentiell relevante fehlende Symbole pro Session: Median "
        f"{summary['potential_missing_session_proxy']['median']}, Mittel "
        f"{summary['potential_missing_session_proxy']['mean']:.3f}, "
        f"Min/Max {summary['potential_missing_session_proxy']['minimum']}/"
        f"{summary['potential_missing_session_proxy']['maximum']}; "
        f"{summary['potential_missing_session_proxy']['sessions_nonzero']} Sessions "
        "mit mindestens einem. Die frühere finale F-Auswahl hatte Median "
        f"{summary['previous_audit_typical_final_F_candidate_count']}. "
        "Der Vergleich bewertet die mögliche Auswahlwirkung ohne starre Prozentgrenze "
        "oder Renditebehauptung; die Einstufung ist keine Obergrenze für unbekannte "
        "historische Listings außerhalb der lokalen Bar-Population.",
        "",
        f"9. **{summary['massive_pilot_decision']}**; vorgeschlagen werden "
        f"{len(pilot)} Symbole. Kein Request wurde ausgeführt.",
        "",
        "## Mengen und Proxy",
        "",
        "| Menge | Symbole |",
        "|---|---:|",
    ]
    lines += [f"| {key} | {value} |" for key, value in summary["set_counts"].items()]
    lines += [
        "",
        "PIT_SEC_2022 umfasst alle lokal gespeicherten Fact-Symbole; davon "
        f"haben {summary['daily_with_PIT_SEC']} auch 2022-Daily-Bars. "
        f"Initial >=300 Bars über alle lokalen Daily-Symbole: "
        f"{summary['initial_warmup_all_daily_symbols']}; ohne 2022-Bar: "
        f"{', '.join(summary['initial_warmup_without_2022_bars']) or 'keine'}. "
        "WARMUP_READY_2022 verlangt ausdrücklich zusätzlich einen 2022-Bar.",
        "",
    ]
    lines += [
        "",
        "LOW-Ursachen (primär, ohne überlappende Einzeldefizite):",
        "",
        "| Lokaler Ausschlussgrund | Symbole |",
        "|---|---:|",
    ]
    lines += [f"| {key} | {value} |" for key, value in counts["low_relevance_reasons"].items()]
    lines += [
        "",
        "TraWalp nicht tradable, aber aktive aktuelle Massive-Reference: "
        + (", ".join(counts["inactive_asset_but_active_reference"]) or "keine")
        + ". Dieser Widerspruch belegt kein tatsächliches Delisting.",
        "",
    ]
    lines += [
        "",
        "CURRENT_SOURCE_UNIVERSE ist exakt Database.list_tradable_companies(): "
        "companies JOIN assets, tradable=1. Statische SIC-, Preis-, Liquiditäts-, Market-Cap- "
        "und Identity-Gates folgen erst danach. Die Methode wurde nicht ersetzt.",
        "",
        "Der strikte POSSIBLE_2022_F_UNIVERSE-Proxy verwendet ausschließlich bereits vorhandene "
        "verifizierte, CIK-/Security-ID-gebundene Intervalle und positive Common-Share-Evidenz. "
        "Ungeklärte Typen oder Identitäten bleiben REQUIRES_IDENTITY_REVIEW. "
        "Ein leerer Proxy beweist keine leere historische Börsenpopulation; er zeigt "
        "die Beweisgrenze "
        "der lokalen Lifecycle-Daten. Kandidatenpriorität und Proxy-Aufnahme sind getrennt.",
        "",
        "## F-Relevanz ohne Scores oder Outcomes",
        "",
        "Vorhandene _cheap_market_features/_cheap_market_exclusions und shares_outstanding_as_of "
        "liefern nur Markt-/Input-Gates. ADV20 entspricht exakt dem Produkt aus "
        "20-Bar-Durchschnittspreis "
        "und Durchschnittsvolumen. Es werden keine Returns, Momentum-Rankings, "
        "Quality-/Valuation-Scores "
        "oder Trades berechnet. Shares bleiben filed <= session, period_end <= session und unter "
        "der vorhandenen Altersgrenze; nur frische Session-Bars zählen.",
        "",
        "Der konfigurierte Feed ist IEX. Die Bars-Tabelle enthält selbst kein Feed-Feld; "
        "eine vollständige konsolidierte Marktvolumen-Historie ist damit nicht bewiesen. "
        "LOW und die Session-Gates beziehen sich auf die tatsächlich gespeicherten Daten und "
        "bestehenden F-Schwellen, nicht auf nachgewiesene Illiquidität im gesamten Markt.",
        "",
        f"Grenzen: {json.dumps(summary['market_gate_thresholds'])}. "
        "Aktuelle SIC-Zuordnung und gespeicherte adjustierte Bars werden nur gemäß "
        "bestehender Semantik "
        "genutzt. Die Konsistenz historischer Split-/Share-Skalierungen ist damit nicht"
        " neu bewiesen.",
        "",
        "HIGH: initialer Warmup, positive Common-Indizien und mindestens eine "
        "gemeinsame Session mit "
        "allen Markt-/Market-Cap-/PIT-Gates. MEDIUM: gemeinsame Gates erst später oder "
        "einzelne Inputs "
        "fehlen. LOW: explizite Scope-Indizien, aktuelle statische SIC-Gates oder keine passende "
        "Markt-/Warmup-Session. UNKNOWN: Identitätskonflikt, fehlender CIK oder "
        "fehlender positiver Security-Type-Beleg trotz plausibler Markt-Gates. "
        "HIGH/MEDIUM sind Forschungsprioritäten, keine F-Eligibility.",
        "",
        "## Fehlende Symbole nach primärer Gap-Klasse",
        "",
        "| Gap | Symbole | beobachtete 2022 Symbol-Sessions | HIGH | MEDIUM | LOW | UNKNOWN |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {r['gap_type']} | {r['symbols']} | {r['symbol_sessions']} | {r['high_relevance']} | "
        f"{r['medium_relevance']} | {r['low_relevance']} | {r['unknown_relevance']} |"
        for r in summary["gap_types"]
    ]
    lines += [
        "",
        "Gap-Typen sind hier primär/exklusiv; alle unabhängigen Befunde stehen in "
        "identity_evidence.json. "
        "CURRENTLY_INACTIVE heißt nur TraWalp tradable=0. Weder dieser Wert noch ein "
        "historischer 404 "
        "beweisen ein Delisting. Die Symbol-Sessionzahlen sind beobachtete lokale "
        "Daily-Daten, keine "
        "rekonstruierten gültigen Börsenmitgliedschaften.",
        "",
        "## Identity-Risiken innerhalb des heutigen Universe",
        "",
        f"Separat {len(inside)} Review-Symbole. Explizit historisch abweichende CIKs: "
        f"{changed_ciks}. "
        f"Heutige Cross-Source-CIK-Konflikte: "
        f"{current_conflicts}.",
        "",
        "Zusätzlich offene Coverage-/Identity-Abgrenzung innerhalb des heutigen Universe: "
        + (
            ", ".join(
                summary["current_identity_review"][
                    "coverage_or_identity_unresolved_market_gate_cases"
                ]
            )
            or "keine"
        )
        + ". Hier sind Markt-Gates plausibel, aber historische PIT-/Shares-Inputs fehlen. "
        "Ein später erster lokaler Fact beweist weder Ticker-Reuse noch fehlenden Download. "
        "Diese Fälle erhalten nur MEDIUM-Forschungspriorität, keine historische Proxy-Aufnahme "
        "und keine automatische Zählung als bestätigte Universe-Auslassung.",
        "",
        "Für aktuelle Details wird nur die jüngste gespeicherte datierte Probe genutzt; "
        "ältere Zwischenpunkte bleiben separat und sind keine gleichzeitig aktuellen Widersprüche. "
        "CIK_STABLE bezieht sich auf übereinstimmende vorhandene Issuer-Beobachtungen, nicht auf "
        "eine bewiesene Security-/Share-Class-Timeline. CIK_CHANGED ist ein zeitlicher "
        "Unterschied; "
        "CIK_CONFLICT ein ungelöster Widerspruch. FIGI wird pro share_class_figi und "
        "composite_figi "
        "getrennt verglichen. Ein FIGI allein beweist weder Issuer-Kontinuität noch "
        "Zeitintervalle.",
        "",
        "STRONG erfordert unabhängige übereinstimmende historische SEC- und Massive-Belege. "
        "MODERATE kennzeichnet eine klare historische Beobachtung/Aktion ohne vollständige Kette. "
        "Aktuelle Typ-/Namensindizien sind WEAK; ohne historische Identitätsbelege bleibt die "
        "historische Identity-Strength UNRESOLVED. Die Detailbefunde besitzen eigene "
        "Strength-Werte.",
        "",
        "Corporate Actions enthalten Anbieter-ID, old/new Symbol und CUSIP sowie "
        "process/effective date. "
        "Ein Prozessdatum wird nicht als effektive Intervallgrenze verwendet. Lokale Nasdaq-/SEC- "
        "und OpenFIGI-Artefakte werden nur gelesen; dokumentierte "
        "Datums-/CUSIP-Widersprüche bleiben offen.",
        "",
        "| Symbol mit historischer Evidenz | Klasse | historischer CIK | "
        "aktueller CIK | Strength |",
        "|---|---|---|---|---|",
        *[
            f"| {r['symbol']} | {r['identity_class']} | {r['historical_cik']} | "
            f"{r['local_cik']} | {r['evidence_strength']} |"
            for r in inside
            if r["historical_identity_evidence"]
        ],
        "",
        "## Begrenzte zukünftige Probe-Liste",
        "",
        "Die Liste umfasst getrennt fehlende Source-Mitglieder sowie aktuelle Source-Mitglieder "
        "mit ungeklärter historischer Zuordnung oder Coverage. current_source_member in der CSV "
        "kennzeichnet die Herkunft. Für die vier aktuellen Fälle klärt ein möglicher späterer "
        "CIK-/Type-Punktabruf die Ursache; Massive liefert keine fehlenden Fundamentaldaten.",
        "",
        "Die bekannten historischen Identity-Fälle innerhalb des Source-Universe erreichen "
        "in der lokalen 2022-Bar-Historie keinen vollständigen F-Warmup. Einzelne alte Probes "
        "ohne 2022-Bars werden nicht als 2022-Lücke gezählt. Fehlende historische Bars außerhalb "
        "dieses lokalen Bestands sind damit weder ausgeschlossen noch rekonstruiert.",
        "",
        "Sortierung: HIGH vor MEDIUM; initial >=300 Bars; PIT-Facts vorhanden; mehr "
        "2022-Daily-Sessions; "
        "MODERATE vor UNRESOLVED; Symbol alphabetisch. Nur HIGH/MEDIUM mit offener "
        "Identität, maximal 20.",
        "",
        "| Rang | Symbol | Relevanz | Warmup-Bars | PIT-Facts | gemeinsame "
        "Gate-Sessions | Strength |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    lines += [
        f"| {i + 1} | {r['symbol']} | {r['f_relevance']} | {r['bars_before_2022']} | "
        f"{r['pit_facts_2022']} | {r['joint_potential_F_gate_sessions']} | "
        f"{r['evidence_strength']} |"
        for i, r in enumerate(pilot)
    ]
    lines += [
        "",
        "Ziel eines späteren separaten Piloten: Security-Type und historische CIK-/FIGI-Bindung "
        "an vorab gewählten 2022-Daten prüfen und heutige Zuordnung vergleichen. Auch "
        "ein erfolgreicher "
        "Punktabruf würde noch keine verifizierte Timeline liefern. Kein automatischer Import.",
        "",
        "## Lokale Massive-Capabilities: Source-only",
        "",
        "MassiveIdentityResearchClient.historical_ticker_details(ticker, as_of): "
        "GET https://api.massive.com/v3/reference/tickers/{ticker}?date=YYYY-MM-DD, exaktes "
        "Identifier-Format und date-Objekt erforderlich. Rückgabe allowlisted: ticker, CIK, "
        "share_class_figi, composite_figi, type, active, market, locale, primary_exchange, "
        "list_date, delisted_utc, last_updated_utc; Felder können fehlen.",
        "",
        "ticker_events(identifier): GET "
        "/vX/reference/tickers/{identifier}/events?types=ticker_change. "
        "Kein as_of-/date-Parameter; nur zurückgegebene Events, keine Vollständigkeitszusage. "
        "Beide Methoden: einzelne Identifier, keine Pagination, keine Promotion oder "
        "Persistierung. "
        "Gemeinsamer dauerhafter Limiter maximal 5 Versuche pro rollendem 60,5-Sekunden-Fenster, "
        "höchstens 4 HTTP-Versuche, Backoff/Retry-After bei Netz-/429-/5xx-Fehlern; "
        "Timeout (10,30), keine Redirects. Jeder Retry verbraucht Budget. "
        "Historische Entitlements sind nur für frühere getestete Requests belegt, nicht"
        " garantiert.",
        "",
        "Production Massive Reference nutzt nur aktuelle aktive Ticker und Typen. "
        "security_reference_snapshots speichert Metadaten, keine vollständigen historischen "
        "Tickerlisten pro Snapshot. Die aktuellen Reference-Zeilen sind kein 2022-Universe.",
        "",
        "Massive historical details are point observations, not verified security intervals.",
        "",
        "Massive does not provide F Quality/Valuation fundamentals in current TraWalp "
        "production integration.",
        "",
        "## Interpretation des vorgegebenen F/C1-Ergebnisses −7,59 %",
        "",
        f"**{summary['2022_F_result_decision']}**. Der Return wurde weder neu berechnet noch zur "
        "Kandidatenauswahl verwendet. Er beschreibt den damaligen Lauf auf der heutigen lokalen "
        "Auswahlbasis; eine Aussage über ein repräsentatives 2022-Universe braucht entsprechend "
        "der obigen Einstufung weitere Rekonstruktion oder explizite Bias-Einschränkung. "
        "Historical universe incompleteness may materially affect candidate selection. "
        "Ein Einfluss auf Richtung oder Höhe der Rendite ist hier nicht bewiesen.",
        "",
        "## Safety und verbleibende Grenzen",
        "",
        f"Flags bleiben erhalten: {', '.join(BIAS_FLAGS)}.",
        "",
        f"SQLite/WAL vorher: `{json.dumps(summary['database_sha256_before'])}`.",
        "",
        f"SQLite/WAL nachher: `{json.dumps(summary['database_sha256_after'])}`.",
        "",
        f"Unverändert: DB={summary['database_unchanged']}; bestehender Source, Config und Audits="
        f"{summary['existing_source_config_and_audits_unchanged']}. "
        f"Safety: `{json.dumps(summary['safety'])}`.",
        "",
        "Bestehende Provider-Zähler in gelesenen Research-Dateien bezeichnen frühere Tasks, "
        "keine Requests dieses Audits. Die Python-Guard blockiert Netzwerk, Provider-Konstruktion "
        "und SQLite-Schreiboperationen; alle DB-Verbindungen verwenden mode=ro. "
        "Lokale Bars sind selbst kein vollständiges historisches Listings-/Delistings-Universe. "
        "Die Mengen quantifizieren lokale Lücken und prüfbare Risiken, keine unbekannte"
        " Gesamtpopulation.",
        "",
        "STOP nach lokalem Audit. Erst Manifest und maximal 20 Kandidaten reviewen. "
        "Keine Requests, Universe-Rewrites, Identity-Epochen, Backtests oder Forward-Aktivierung.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
