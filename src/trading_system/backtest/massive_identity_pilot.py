"""Bounded five-symbol identity research. No screening, ingestion or intervals."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import subprocess
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch

import requests
from dotenv import load_dotenv

from trading_system.backtest import f_data_gap_audit as prior
from trading_system.backtest import historical_universe_audit as universe
from trading_system.config import StrategyConfig, _read_yaml
from trading_system.data import massive_identity_research as research
from trading_system.data.massive_rate_limit import _state_path
from trading_system.data.security_reference import canonical_type, normalize_cik

ROOT = prior.ROOT
STEM = "f_2022_massive_identity_pilot_v1"
PREVIOUS = universe.STEM
SYMBOLS = ("QRVO", "BG", "XOM", "SAIL", "FERG")
MAX_ATTEMPTS = 25
ACCESS = {401, 402, 403}
FIGI_FIELDS = ("share_class_figi", "composite_figi")


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def output(suffix):
    return ROOT / "reports" / f"{STEM}_{suffix}"


def midpoint_probes(sessions, count=3):
    days = sorted(set(sessions))
    if len(days) < count:
        raise ValueError("INSUFFICIENT_LOCAL_PROBE_SESSIONS")
    return [days[0], days[len(days) // 2], days[-1]] if count == 3 else [days[0], days[-1]]


def make_plan(relevant):
    if set(relevant) != set(SYMBOLS) or len(relevant["QRVO"]) != 39:
        raise ValueError("EXACT_FIVE_SYMBOLS_AND_39_QRVO_SESSIONS_REQUIRED")
    rows = []
    for symbol in SYMBOLS:
        role = "PRIMARY_CASE" if symbol == "QRVO" else "CONTROL_OR_IDENTITY_REVIEW_CASE"
        days = midpoint_probes(relevant[symbol], 3 if symbol == "QRVO" else 2)
        question = (
            "Verify active US common stock and issuer/security consistency at first/middle/last "
            "of the 39 previously identified Q1 sessions"
            if symbol == "QRVO"
            else "Compare issuer/security at first/last locally price/liquidity/history-ready "
            "2022 session; local mapped SEC facts start after 2022"
        )
        for day in days:
            rows.append(
                dict(
                    symbol=symbol,
                    role=role,
                    probe_date=str(day),
                    operation="details",
                    selection_reason=question,
                )
            )
        rows.append(
            dict(
                symbol=symbol,
                role=role,
                probe_date="",
                operation="events",
                selection_reason="One ticker-change point query; missing event is inconclusive",
            )
        )
    return [dict(sequence=i + 1, **row) for i, row in enumerate(rows)]


def hashes(paths):
    return {str(p): prior.digest(p) if p.exists() else None for p in paths}


def db_hashes():
    p = ROOT / "data/trading_system.sqlite3"
    return hashes([p, Path(str(p) + "-wal")])


def prepare():
    """Local baseline is completely persisted before credential loading or transport."""
    if any(output(s).exists() for s in ("manifest.csv", "local_baseline.csv", "summary.json")):
        raise RuntimeError("FRESH_STEM_REQUIRED_EXISTING_PILOT_WILL_NOT_BE_OVERWRITTEN")
    protected = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "config").glob("*"))
    protected += sorted((ROOT / "reports").glob(f"{PREVIOUS}_*"))
    protected = [p for p in protected if p.is_file()]
    before = db_hashes()
    protected_hashes = hashes(protected)
    candidates = {
        r["symbol"]: r
        for r in read_csv(ROOT / "reports" / f"{PREVIOUS}_massive_pilot_candidates.csv")
    }
    if set(candidates) != set(SYMBOLS):
        raise RuntimeError("PREVIOUS_PILOT_MANIFEST_DRIFT")
    evidence = json.loads(
        (ROOT / "reports" / f"{PREVIOUS}_identity_evidence.json").read_text(encoding="utf-8")
    )
    session_rows = read_csv(ROOT / "reports" / f"{PREVIOUS}_session_bias.csv")
    qrvo_days = [
        date.fromisoformat(r["session"])
        for r in session_rows
        if int(r["potentially_F_relevant_missing_symbols"]) == 1
    ]
    if len(qrvo_days) != 39 or any(day.month > 3 for day in qrvo_days):
        raise RuntimeError("PREVIOUS_QRVO_SESSION_EVIDENCE_DRIFT")
    db = prior.AuditDatabase(ROOT / "data/trading_system.sqlite3")
    baseline, fact_info, metadata = [], {}, {}
    with prior.local_guard():
        current = {c.symbol for c in db.list_tradable_companies()}
        cached = db.sync_value("sec_reference", "ticker_to_cik") or {}
        with db.read_only() as con:
            snapshots = [
                json.loads(r[0])
                for r in con.execute("SELECT metadata FROM security_reference_snapshots")
            ]
            catalogs = [s["type_catalog"] for s in snapshots if s.get("type_catalog")]
            if not catalogs:
                raise RuntimeError("LOCAL_REVIEWED_TYPE_CATALOG_REQUIRED")
            catalog = catalogs[-1]
            for symbol in SYMBOLS:
                company = dict(
                    con.execute("SELECT * FROM companies WHERE symbol=?", (symbol,)).fetchone()
                )
                asset = dict(
                    con.execute("SELECT * FROM assets WHERE symbol=?", (symbol,)).fetchone()
                )
                refrow = con.execute(
                    "SELECT * FROM security_reference WHERE symbol=?", (symbol,)
                ).fetchone()
                reference = dict(refrow) if refrow else {}
                bar = dict(
                    con.execute(
                        "SELECT MIN(timestamp) first_daily_bar, MAX(timestamp) last_daily_bar, "
                        "SUM(timestamp<'2022-01-03') bars_before_2022, "
                        "SUM(timestamp>='2022-01-03' AND timestamp<'2022-12-31') daily_bars_2022, "
                        "MIN(CASE WHEN timestamp>='2022-01-03' AND timestamp<'2022-12-31' THEN "
                        "timestamp END) first_2022_bar, MAX(CASE WHEN timestamp>='2022-01-03' "
                        "AND timestamp<'2022-12-31' THEN timestamp END) last_2022_bar "
                        "FROM bars WHERE symbol=? AND timeframe='1d'",
                        (symbol,),
                    ).fetchone()
                )
                fact = dict(
                    con.execute(
                        "SELECT COUNT(*) fact_count, MIN(filed) first_filed, "
                        "MAX(filed) last_filed, "
                        "SUM(filed<='2022-12-30') pit_facts_2022, "
                        "MIN(CASE WHEN filed<='2022-12-30' THEN filed END) first_pit_filed "
                        "FROM fundamental_facts WHERE symbol=?",
                        (symbol,),
                    ).fetchone()
                )
                fact_info[symbol] = fact
                actions = [
                    dict(r)
                    for r in con.execute(
                        "SELECT provider,provider_action_id,action_type,old_symbol,new_symbol,"
                        "old_cusip,new_cusip,process_date,effective_date,observed_at "
                        "FROM security_corporate_actions WHERE old_symbol=? OR new_symbol=?",
                        (symbol, symbol),
                    )
                ]
                epochs = [
                    dict(r)
                    for r in con.execute(
                        "SELECT * FROM symbol_identity_epochs WHERE symbol=?", (symbol,)
                    )
                ]
                old = evidence["symbols"][symbol]
                row = dict(
                    symbol=symbol,
                    role="PRIMARY_CASE" if symbol == "QRVO" else "CONTROL_OR_IDENTITY_REVIEW_CASE",
                    current_source_member=symbol in current,
                    current_tradable=bool(asset["tradable"]),
                    current_exchange=asset["exchange"],
                    local_cik=company["cik"],
                    cached_sec_cik=cached.get(symbol),
                    current_massive_cik=reference.get("cik"),
                    current_reference_observed_at=reference.get("observed_at"),
                    current_share_class_figi=reference.get("share_class_figi"),
                    current_composite_figi=reference.get("composite_figi"),
                    current_massive_type=reference.get("security_type"),
                    current_massive_exchange=reference.get("primary_exchange"),
                    **bar,
                    warmup_300=bar["bars_before_2022"] >= 300,
                    **fact,
                    local_fact_ciks=[
                        r[0]
                        for r in con.execute(
                            "SELECT DISTINCT cik FROM fundamental_facts WHERE symbol=?", (symbol,)
                        )
                    ],
                    corporate_actions=actions,
                    local_identity_epochs=epochs,
                    previous_identity_issues=old["reason"],
                    existing_historical_massive_evidence=old["massive_observations"],
                    previous_f_relevance=candidates[symbol]["f_relevance"],
                    reason_for_inclusion=candidates[symbol]["reason"],
                )
                for key in ("local_cik", "cached_sec_cik", "current_massive_cik"):
                    if normalize_cik(row[key]) != normalize_cik(old.get(key)):
                        raise RuntimeError("LOCAL_IDENTITY_BASELINE_DRIFT")
                baseline.append(row)
                metadata[symbol] = {"company": company, "asset": asset, "reference": reference}
        config = StrategyConfig.model_validate(_read_yaml(ROOT / "config/strategy.yaml"))
        sessions = [date.fromisoformat(r["session"]) for r in session_rows]
        gates = universe.market_gates(db, config, list(SYMBOLS), sessions, fact_info)
    if sorted(gates["QRVO"]["joint"]) != sorted(qrvo_days):
        raise RuntimeError("QRVO_39_SESSION_LOCAL_REPRODUCTION_DIFFERENCE")
    relevant = {s: qrvo_days if s == "QRVO" else sorted(gates[s]["cheap"]) for s in SYMBOLS}
    plan = make_plan(relevant)
    for row in baseline:
        row["probe_selection_sessions"] = [str(d) for d in relevant[row["symbol"]]]
        row["probe_selection_session_count"] = len(relevant[row["symbol"]])
    context = dict(
        db_before=before,
        protected_before=protected_hashes,
        catalog=catalog,
        local_metadata=metadata,
        relevant_sessions=relevant,
        local_head=git("rev-parse", "HEAD"),
        branch=git("branch", "--show-current"),
        plan=plan,
        baseline=baseline,
        status="BASELINE_SAVED_NO_NETWORK_YET",
        limiter_state_path=str(_state_path()),
        limiter_before=hashes([_state_path()]),
    )
    universe.write_csv(output("manifest.csv"), plan)
    universe.write_csv(output("local_baseline.csv"), baseline)
    universe.write_json(output("baseline_context.json"), context)
    print(
        json.dumps(
            {
                "baseline_saved": True,
                "logical_probes": len(plan),
                "dates": {
                    s: [
                        r["probe_date"]
                        for r in plan
                        if r["symbol"] == s and r["operation"] == "details"
                    ]
                    for s in SYMBOLS
                },
            }
        ),
        flush=True,
    )
    return context


def git(*args):
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", *args], cwd=ROOT, text=True
    ).strip()


class PilotStop(RuntimeError):
    pass


class TransportBudget:
    """Wrap the existing limiter and session; never perform independent HTTP."""

    def __init__(self, plan, maximum=MAX_ATTEMPTS):
        if maximum > MAX_ATTEMPTS or maximum < 1:
            raise ValueError("INVALID_PILOT_BUDGET")
        self.maximum = maximum
        self.urls = {
            f"https://api.massive.com/v3/reference/tickers/{r['symbol']}?date={r['probe_date']}"
            if r["operation"] == "details"
            else f"https://api.massive.com/vX/reference/tickers/{r['symbol']}/events?types=ticker_change"
            for r in plan
        }
        self.attempts = []
        self.access_blocked = False

    @contextmanager
    def guard(self, client):
        original_request = requests.sessions.Session.request
        original_transmit = research._REQUEST_LIMITER.transmit

        def transmit(operation, **kwargs):
            if self.access_blocked:
                raise PilotStop("ACCESS_FAIL_CLOSED")
            if len(self.attempts) >= self.maximum:
                raise PilotStop("REQUEST_BUDGET_EXHAUSTED")
            return original_transmit(operation, **kwargs)

        def request(session, method, url, **kwargs):
            if (
                session is not client._session
                or method.upper() != "GET"
                or url not in self.urls
                or kwargs.get("allow_redirects") is not False
            ):
                raise PilotStop("UNSUPPORTED_PROVIDER_OR_ENDPOINT")
            if self.access_blocked or len(self.attempts) >= self.maximum:
                raise PilotStop("TRANSPORT_FAIL_CLOSED")
            record = {"url": url, "http_status": None}
            self.attempts.append(record)
            try:
                response = original_request(session, method, url, **kwargs)
                record["http_status"] = response.status_code
                if response.status_code in ACCESS:
                    self.access_blocked = True
                return response
            except requests.RequestException:
                record["error"] = "TRANSPORT_ERROR"
                raise

        def deny_sql(*args, **kwargs):
            raise PilotStop("DATABASE_ACCESS_FORBIDDEN_DURING_NETWORK_PHASE")

        with (
            patch.object(research._REQUEST_LIMITER, "transmit", transmit),
            patch.object(requests.sessions.Session, "request", request),
            patch.object(sqlite3, "connect", deny_sql),
        ):
            yield self


def run_probes(client, plan, budget, checkpoint=None):
    observations = []
    with budget.guard(client):
        stopped = None
        for row in plan:
            before = len(budget.attempts)
            if stopped:
                observation = {"error": stopped, "not_attempted": True, "http_status": None}
            else:
                try:
                    observation = (
                        client.historical_ticker_details(
                            row["symbol"], date.fromisoformat(row["probe_date"])
                        )
                        if row["operation"] == "details"
                        else client.ticker_events(row["symbol"])
                    )
                except (PilotStop, ValueError, OSError) as exc:
                    # Never store request/exception text, which may contain credentials.
                    observation = {
                        "error": "LOCAL_SAFETY_OR_ACCESS_BLOCK",
                        "error_type": type(exc).__name__,
                        "http_status": None,
                    }
                    stopped = "NOT_ATTEMPTED_AFTER_LOCAL_SAFETY_STOP"
                if observation.get("http_status") in ACCESS:
                    stopped = "NOT_ATTEMPTED_AFTER_ACCESS_DENIAL"
            record = {**row, **observation, "requests_attempted": len(budget.attempts) - before}
            observations.append(record)
            if checkpoint:
                checkpoint(observations, budget)
            print(
                json.dumps(
                    {
                        "symbol": row["symbol"],
                        "operation": row["operation"],
                        "date": row["probe_date"],
                        "http_status": observation.get("http_status"),
                        "attempts_total": len(budget.attempts),
                        "error": observation.get("error"),
                    }
                ),
                flush=True,
            )
    return observations


def compare_cik(baseline, historical):
    current = {c for v in baseline for c in [normalize_cik(v)] if c}
    old = {c for v in historical for c in [normalize_cik(v)] if c}
    if len(current) > 1 or len(old) > 1:
        return "CIK_CONFLICT"
    if not old:
        return "CIK_MISSING"
    if not current:
        return "CIK_UNRESOLVED"
    return "CIK_MATCH" if current == old else "CIK_CHANGED"


def compare_figi(current, historical):
    old, now = set(filter(None, historical)), set(filter(None, current))
    if not old:
        return "FIGI_MISSING"
    if not now:
        return "FIGI_UNRESOLVED"
    return "FIGI_MATCH" if old == now and len(old) == 1 else "FIGI_CHANGED"


def security_type(code, catalog):
    value = canonical_type(code, catalog)
    if value in {"ETF", "ETP", "ETN", "FUND"}:
        return "ETF/ETP"
    if value == "PREFERRED_STOCK":
        return "PREFERRED"
    return (
        value
        if value in {"COMMON_STOCK", "ADR", "WARRANT", "UNIT", "RIGHT", "UNKNOWN"}
        else "OTHER"
    )


def evidence_strength(details, baseline, catalog):
    if not details:
        return "UNRESOLVED"
    complete = [
        r
        for r in details
        if normalize_cik(r.get("cik"))
        and r.get("share_class_figi")
        and r.get("composite_figi")
        and security_type(r.get("type"), catalog) != "UNKNOWN"
    ]
    # Multiple consistent dated CIK+both-FIGI points complement local SEC issuer identity.
    ciks = {normalize_cik(r.get("cik")) for r in complete}
    consistent = all(len({r.get(k) for r in complete}) == 1 for k in FIGI_FIELDS)
    if (
        len(complete) >= 2
        and len(ciks) == 1
        and consistent
        and normalize_cik(baseline.get("local_cik")) in ciks
        and normalize_cik(baseline.get("cached_sec_cik")) in ciks
    ):
        return "STRONG"
    return "MODERATE" if any(normalize_cik(r.get("cik")) for r in details) else "WEAK"


def analyze_symbol(symbol, baseline, observations, catalog):
    rows = [r for r in observations if r["symbol"] == symbol]
    successful = [
        r
        for r in rows
        if r["operation"] == "details" and r.get("http_status") == 200 and not r.get("error")
    ]
    details = [r.get("results", {}) for r in successful]
    ciks = sorted({normalize_cik(d.get("cik")) for d in details if normalize_cik(d.get("cik"))})
    comparison = compare_cik(
        [baseline.get(k) for k in ("local_cik", "cached_sec_cik", "current_massive_cik")], ciks
    )
    figis = {k: sorted({d[k] for d in details if d.get(k)}) for k in FIGI_FIELDS}
    figi_classes = {k: compare_figi([baseline.get("current_" + k)], figis[k]) for k in FIGI_FIELDS}
    types = sorted({security_type(d.get("type"), catalog) for d in details})
    exchanges = sorted({d["primary_exchange"] for d in details if d.get("primary_exchange")})
    mismatch = comparison in {"CIK_CHANGED", "CIK_CONFLICT"} or any(
        v == "FIGI_CHANGED" for v in figi_classes.values()
    )
    mismatch = mismatch or any(len(figis[k]) > 1 for k in FIGI_FIELDS)
    mismatch = mismatch or any(d.get("ticker") != symbol for d in details)
    current_type = baseline.get("current_massive_type")
    if current_type and types and current_type not in types:
        mismatch = True
    stable = (
        len(ciks) == 1
        and all(len(figis[k]) == 1 for k in FIGI_FIELDS)
        and len(types) == 1
        and len(exchanges) == 1
    )
    active_us_common = bool(details) and all(
        d.get("active") is True
        and d.get("market") == "stocks"
        and d.get("locale") == "us"
        and security_type(d.get("type"), catalog) == "COMMON_STOCK"
        for d in details
    )
    strength = evidence_strength(details, baseline, catalog)
    events_row = next((r for r in rows if r["operation"] == "events"), {})
    events = events_row.get("results", {}).get("events", [])
    changes = [e for e in events if isinstance(e, dict) and e.get("type") == "ticker_change"]
    actions = baseline.get("corporate_actions", [])
    corroborated = [
        a
        for a in actions
        if a.get("old_symbol")
        and a.get("new_symbol")
        and any(
            e.get("ticker_change", {}).get("ticker") == a["new_symbol"]
            and e.get("date") in {a.get("process_date"), a.get("effective_date")}
            for e in changes
        )
    ]
    if symbol == "QRVO":
        if mismatch:
            result = "QRVO_IDENTITY_CONFLICT"
        elif (
            len(successful) == 3
            and stable
            and active_us_common
            and comparison == "CIK_MATCH"
            and strength == "STRONG"
            and events_row.get("http_status") not in ACCESS
        ):
            result = "QRVO_IDENTITY_VERIFIED_FOR_F_REPLAY"
        elif details and comparison == "CIK_MATCH" and not mismatch:
            result = "QRVO_IDENTITY_PLAUSIBLE_BUT_INCOMPLETE"
        else:
            result = "QRVO_IDENTITY_UNRESOLVED"
    elif mismatch:
        result = "IDENTITY_CONFLICT"
    elif corroborated:
        result = "IDENTITY_CHANGE_CONFIRMED"
    elif len(successful) >= 2 and stable and comparison == "CIK_MATCH" and active_us_common:
        result = (
            "IDENTITY_RESOLVED_LOCAL_COVERAGE_ISSUE"
            if not baseline["pit_facts_2022"]
            else "IDENTITY_RESOLVED_NO_ACTION"
        )
    else:
        result = "IDENTITY_UNRESOLVED"
    next_action = (
        "Targeted QRVO F-Eligibility Replay for the previously identified 39 Q1-2022 sessions"
        if result == "QRVO_IDENTITY_VERIFIED_FOR_F_REPLAY"
        else "Review dated issuer/security mismatch; no repair and no F replay"
        if mismatch
        else "No automatic action; review identity evidence and remaining limitations"
    )
    return dict(
        symbol=symbol,
        role=baseline["role"],
        requests_attempted=sum(r["requests_attempted"] for r in rows),
        successful_detail_probes=len(successful),
        not_found_probes=sum(r.get("http_status") == 404 for r in rows),
        event_probe_status=events_row.get("http_status") or events_row.get("error"),
        historical_ciks=ciks,
        historical_figis=figis,
        historical_security_types=types,
        historical_exchanges=exchanges,
        local_cik=baseline["local_cik"],
        current_reference_cik=baseline["current_massive_cik"],
        cik_classification=comparison,
        figi_classification=figi_classes,
        active_us_common_at_all_successful_points=active_us_common,
        stable_at_successful_points=stable,
        ticker_reuse_evidence=mismatch,
        ticker_change_evidence=bool(corroborated),
        identity_result=result,
        evidence_strength=strength,
        recommended_next_action=next_action,
        events=events,
        f_replay_recommended=result == "QRVO_IDENTITY_VERIFIED_FOR_F_REPLAY",
    )


def probe_rows(observations, baselines, catalog):
    rows = []
    for observation in observations:
        b = baselines[observation["symbol"]]
        d = observation.get("results", {}) if observation["operation"] == "details" else {}
        status = observation.get("http_status")
        cik = compare_cik(
            [b.get(k) for k in ("local_cik", "cached_sec_cik", "current_massive_cik")],
            [d.get("cik")],
        )
        figi = {k: compare_figi([b.get("current_" + k)], [d.get(k)]) for k in FIGI_FIELDS}
        identity = (
            "MASSIVE_POINT_NOT_FOUND"
            if status == 404
            else "ACCESS_DENIED"
            if status in ACCESS
            else "POINT_OBSERVATION"
            if status == 200 and not observation.get("error")
            else "NOT_ATTEMPTED"
            if observation.get("not_attempted")
            else "PROBE_FAILED"
        )
        rows.append(
            dict(
                symbol=b["symbol"],
                role=b["role"],
                probe_date=observation["probe_date"],
                operation=observation["operation"],
                http_status=status,
                historical_ticker=d.get("ticker"),
                historical_cik=normalize_cik(d.get("cik")),
                share_class_figi=d.get("share_class_figi"),
                composite_figi=d.get("composite_figi"),
                provider_security_type=d.get("type"),
                security_type=security_type(d.get("type"), catalog),
                active=d.get("active"),
                market=d.get("market"),
                locale=d.get("locale"),
                primary_exchange=d.get("primary_exchange"),
                list_date=d.get("list_date"),
                delisted_utc=d.get("delisted_utc"),
                last_updated_utc=d.get("last_updated_utc"),
                local_cik=b["local_cik"],
                cached_sec_cik=b["cached_sec_cik"],
                current_massive_cik=b["current_massive_cik"],
                cik_classification=cik,
                figi_classification=figi,
                identity_classification=identity,
                evidence_strength=evidence_strength([d] if d else [], b, catalog),
                requests_attempted=observation["requests_attempted"],
                error=observation.get("error"),
                event_fields=observation.get("results", {})
                if observation["operation"] == "events"
                else {},
                response_sha256=observation.get("response_sha256"),
            )
        )
    return rows


def finalize(context, observations, attempts, diagnostics, access_blocked):
    baselines = {b["symbol"]: b for b in context["baseline"]}
    summaries = [analyze_symbol(s, baselines[s], observations, context["catalog"]) for s in SYMBOLS]
    after = db_hashes()
    protected_after = hashes([Path(p) for p in context["protected_before"]])
    intact = after == context["db_before"] and protected_after == context["protected_before"]
    qrvo = summaries[0]
    decision = (
        "MASSIVE_IDENTITY_PILOT_INCONCLUSIVE_ACCESS"
        if access_blocked
        else "HISTORICAL_UNIVERSE_BIAS_REQUIRES_TARGETED_F_REPLAY"
        if qrvo["f_replay_recommended"]
        else "HISTORICAL_UNIVERSE_IDENTITY_REMAINS_UNRESOLVED"
    )
    counts = Counter(r.get("http_status") for r in attempts)
    summary = dict(
        local_head=context["local_head"],
        branch=context["branch"],
        pilot_symbols=list(SYMBOLS),
        period=["2022-01-03", "2022-12-30"],
        logical_probes_planned=len(context["plan"]),
        massive_requests=len(attempts),
        successful_responses=counts[200],
        not_found_responses=counts[404],
        failed_responses=len(attempts) - counts[200] - counts[404],
        retries=diagnostics["research_retries"],
        client_diagnostics=diagnostics,
        request_budget=MAX_ATTEMPTS,
        rate_limit="5 attempts per rolling 60 seconds; existing 60.5s guard",
        other_provider_calls=0,
        sec_calls=0,
        alpaca_calls=0,
        openfigi_calls=0,
        broker_calls=0,
        asset_sync_calls=0,
        bar_sync_calls=0,
        reference_sync_calls=0,
        database_writes=0,
        strategy_changes=0,
        f_eligibility_executions=0,
        db_before=context["db_before"],
        db_after=after,
        integrity_verified=intact,
        protected_files_unchanged=context["protected_before"] == protected_after,
        limiter_state_path=context["limiter_state_path"],
        limiter_before=context["limiter_before"],
        limiter_after=hashes([_state_path()]),
        limiter_state_note=(
            "Expected durable rate-limit reservations outside the trading database; "
            "no reset or recovery"
        ),
        qrvo_relevant_sessions=context["relevant_sessions"]["QRVO"],
        symbols=summaries,
        qrvo_decision=qrvo["identity_result"],
        universe_decision=decision,
        bias_flags=universe.BIAS_FLAGS,
        limitations=[
            "Massive historical details are point observations, not verified security intervals.",
            "404 means no provider result for the query, not security nonexistence.",
            "Missing ticker event does not prove absence of ticker changes.",
            "No productive identity epochs or historical universe reconstructed.",
            "SEC coverage remediation does not repair CURRENT_UNIVERSE_ONLY / survivorship bias.",
            "The existing 2022 result remains conditional on the current "
            "historical-universe evidence.",
        ],
    )
    universe.write_csv(
        output("probes.csv"), probe_rows(observations, baselines, context["catalog"])
    )
    universe.write_csv(output("symbol_summary.csv"), summaries)
    universe.write_json(output("summary.json"), summary)
    output("audit.md").write_text(render_audit(summary, context, observations), encoding="utf-8")
    if not intact:
        raise RuntimeError("DATABASE_OR_PROTECTED_FILE_INTEGRITY_CHANGED_REVIEW_REQUIRED")
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "massive_requests",
                    "successful_responses",
                    "not_found_responses",
                    "failed_responses",
                    "retries",
                    "integrity_verified",
                    "qrvo_decision",
                    "universe_decision",
                )
            }
        ),
        flush=True,
    )
    return summary


def render_audit(summary, context, observations):
    q = summary["symbols"][0]
    text = [
        "# F 2022 Massive Historical Identity Pilot v1",
        "",
        f"Local HEAD: `{summary['local_head']}`; branch: `{summary['branch']}`.",
        "",
        "Exakt QRVO, BG, XOM, SAIL, FERG. Research evidence only. Keine F-Scores, "
        "Momentum-Gates, Ranks, Trades oder Renditen berechnet; keine Datenreparatur.",
        "",
        f"Entscheidung QRVO: **{summary['qrvo_decision']}**.",
        "",
        f"Universe-Entscheidung: **{summary['universe_decision']}**.",
        "",
        "## Auswahl und Methode",
        "",
        "QRVO ist der einzige fehlende Fall mit F_RELEVANCE_HIGH aus dem lokalen Audit. "
        "Die 39 Sessions werden aus session_bias.csv gelesen und gegen dieselben lokalen "
        "Price-/History-/ADV-/Market-Cap-/PIT-Voraussetzungen reproduziert. Mitte = Index n//2 "
        "der sortierten echten Sessionliste; keine Intervallauffuellung. Controls: erster und "
        "letzter Price-/History-/ADV-bereiter Tag, um die offene historische SEC-Zuordnung "
        "zu pruefen. Keine Auswahl anhand von Returns oder F-Trade-Outcomes.",
        "",
        "STRONG fuer konsistente mehrere datierte Punkte mit CIK und beiden FIGIs plus "
        "uebereinstimmender lokaler/cached SEC-Identitaet; MODERATE fuer klare datierte "
        "CIK-Evidenz ohne diese Ergaenzung. Dies verifiziert keine lueckenlose Lifecycle-Kette. "
        "CIK_CHANGED ist ein zeitlicher Unterschied; konkurrierende Werte innerhalb einer "
        "Zeitgruppe sind CIK_CONFLICT. Abweichungen bleiben ohne Reparatur bestehen.",
        "",
        "Security Types verwenden ausschliesslich canonical_type() und den bereits lokal "
        "gespeicherten Provider-Typenkatalog. Kein neuer Katalog-Endpoint. Exchanges werden "
        "als Provider-MIC erfasst; unbekannte Codes nicht als US-Exchange geraten.",
        "",
        "## Ergebnisse je Symbol",
        "",
        "| Symbol | Details 200 | CIK historisch | Typ | Exchange | Ergebnis | Staerke |",
        "|---|---:|---|---|---|---|---|",
    ]
    for s in summary["symbols"]:
        text.append(
            f"| {s['symbol']} | {s['successful_detail_probes']} | "
            f"{', '.join(s['historical_ciks'])} | "
            f"{', '.join(s['historical_security_types'])} | "
            f"{', '.join(s['historical_exchanges'])} | "
            f"{s['identity_result']} | {s['evidence_strength']} |"
        )
    text += [
        "",
        "## QRVO: Antworten A bis F",
        "",
        "A: Aktive US Common Stocks an allen erfolgreichen Punkten: "
        f"{q['active_us_common_at_all_successful_points']}; "
        f"erfolgreiche Details: {q['successful_detail_probes']}/3.",
        "",
        f"B: Historische CIKs: {q['historical_ciks']}; Stabilitaet nur fuer erfolgreiche Punkte.",
        "",
        f"C: Vergleich mit lokalem/cached SEC/current Massive: {q['cik_classification']}.",
        "",
        f"D: FIGIs: {json.dumps(q['historical_figis'])}; aktuelle Referenzvergleiche: "
        f"{json.dumps(q['figi_classification'])}. Fehlende aktuelle FIGIs sind keine Aenderung.",
        "",
        f"E: Potentielle Reuse-/Replacement-/Issuer-Differenz: {q['ticker_reuse_evidence']}; "
        f"korroborierter Tickerwechsel: {q['ticker_change_evidence']}. "
        "Delisting-/Relisting-Felder und Events stehen in probes.csv. Leere Felder oder "
        "fehlende Events beweisen keine Ereignisfreiheit zwischen den Punkten.",
        "",
        f"F: Gezielter spaeterer F-Replay gerechtfertigt: {q['f_replay_recommended']}.",
        "",
        "## Requests und Safety",
        "",
        f"HTTP-Versuche: {summary['massive_requests']}; "
        f"HTTP200: {summary['successful_responses']}; "
        f"HTTP404: {summary['not_found_responses']}; "
        f"sonstige Fehler: {summary['failed_responses']}; "
        f"Retries: {summary['retries']}. Maximales Budget inklusive Retries: 25. "
        "Alle Requests sequenziell ueber die beiden bestehenden Research-Methoden und den "
        "unveraenderten gemeinsamen durable Limiter (5/60s, 60.5s Guard). "
        "401/402/403 stoppt alle weiteren Versuche, ohne Fallback oder Plan-Aenderung.",
        "",
        "SEC/Alpaca/OpenFIGI/Broker/andere Provider: 0. Asset-/Bar-/Reference-Sync: 0. "
        "Trading-DB-Writes: 0. Strategieaenderungen: 0. Die Netzwerkphase verbietet jede "
        "SQLite-Verbindung; lokale Analyse benutzt mode=ro und einen Schreib-Authorizer.",
        "",
        f"SQLite/WAL vorher: `{json.dumps(summary['db_before'])}`.",
        "",
        f"SQLite/WAL nachher: `{json.dumps(summary['db_after'])}`.",
        "",
        f"Integritaet verifiziert: {summary['integrity_verified']}. Bestehende Source-/Config-/"
        "Historical-Audit-Dateien ebenfalls per SHA-256 unveraendert. "
        "Nur der bestehende Massive-Limiter-State ausserhalb der Trading-DB darf normale "
        "Reservierungen speichern; kein Reset/Recovery.",
        "",
        "## Grenzen und naechster Schritt",
        "",
    ]
    text.extend(s + "\n" for s in summary["limitations"])
    text += [
        "Flags: " + ", ".join(summary["bias_flags"]),
        "",
        q["recommended_next_action"] + ". Dieser Task fuehrt den Folgeschritt nicht aus.",
        "",
        "STOP nach diesen fuenf Symbolen. Keine Universe-Aenderung oder Portfolio-Neuberechnung.",
        "",
    ]
    return "\n".join(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--execute-prepared", action="store_true")
    args = parser.parse_args()
    if args.prepare_only and args.execute_prepared:
        parser.error("Choose preparation or execution")
    if args.execute_prepared:
        context = json.loads(output("baseline_context.json").read_text(encoding="utf-8"))
        if output("observations.json").exists() or output("summary.json").exists():
            raise RuntimeError("PILOT_ALREADY_ATTEMPTED_NO_AUTOMATIC_RERUN")
        if (
            db_hashes() != context["db_before"]
            or hashes([Path(p) for p in context["protected_before"]]) != context["protected_before"]
        ):
            raise RuntimeError("BASELINE_DRIFT_BEFORE_NETWORK")
    else:
        context = prepare()
    if args.prepare_only:
        return
    # Exactly the existing research client, using the existing credential and shared limiter.
    load_dotenv(ROOT / ".env")
    client = research.MassiveIdentityResearchClient(os.getenv("MASSIVE_API_KEY", ""))
    budget = TransportBudget(context["plan"])

    def checkpoint(observations, guard):
        universe.write_json(
            output("observations.json"),
            dict(
                observations=observations, attempts=guard.attempts, diagnostics=client.diagnostics
            ),
        )

    try:
        observations = run_probes(client, context["plan"], budget, checkpoint)
    finally:
        client.close()
    finalize(context, observations, budget.attempts, client.diagnostics, budget.access_blocked)


if __name__ == "__main__":
    main()
