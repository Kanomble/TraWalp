"""Explicit six-symbol free-evidence research. No production writes or epoch promotion.

Run manually with python -m trading_system.data.free_identity_research. Parsed
point observations never manufacture the optional, separately reviewed complete
interval proof. All results remain blocked until a separate production promotion.
"""

import json
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import requests

from trading_system.config import load_settings
from trading_system.data.nasdaq_lifecycle_research import NasdaqLifecycleResearchClient
from trading_system.data.openfigi_research import OpenFigiResearchClient
from trading_system.data.sec_client import SecClient, SecResourceNotFound
from trading_system.data.sec_identity_research import fetch_sec_filing
from trading_system.data.security_identity import LIFECYCLE_EVIDENCE, UNKNOWN

RESEARCH_SAMPLES = ("CAI", "FLY", "JATT", "ALPS", "BLUW", "DSAC")


def stable_ids(row):
    return sorted(
        {
            f"{field.upper()}:{row[field]}"
            for field in ("share_class_figi", "composite_figi")
            if row.get(field)
        }
    )


def classify_graph(graph):
    """Validate a scoped graph, not ticker-name similarity or historical continuity.

    A reviewed_interval must cite an existing Nasdaq node, cover COMPLETE symbol
    membership, and link its security to a CUSIP bridge plus SEC issuer evidence.
    No parser in this module creates such coverage from a single announcement.
    """
    current = graph["current"]
    ids = stable_ids(current)
    conflicts = list(graph.get("conflicts", []))
    claims = [
        r
        for r in graph.get("current_security_claims", [])
        if r.get("symbol") == graph["symbol"] and r.get("cik") == current.get("cik")
    ]
    for field in ("share_class_figi", "composite_figi"):
        if len({r[field] for r in [current, *claims] if r.get(field)}) > 1:
            conflicts.append({"reason": "CURRENT_SECURITY_ID_DISAGREEMENT", "field": field})
    result = {
        "identity_status": UNKNOWN,
        "recommendation": "KEEP_BLOCKED",
        "candidate_epoch": None,
        "conflicts": conflicts,
    }
    if conflicts:
        return {
            **result,
            "classification": "CONFLICT",
            "reason": "UNRESOLVED_EVIDENCE_DISAGREEMENT",
        }
    if not current.get("cik") or not ids or current.get("ticker") != graph["symbol"]:
        return {
            **result,
            "classification": "INSUFFICIENT",
            "reason": "CURRENT_CIK_AND_BOUND_SECURITY_ID_REQUIRED",
        }
    sec_bound = any(
        r.get("issuer_binding_confirmed")
        and r.get("document_ciks") == [current["cik"]]
        and graph["symbol"] in r.get("trading_symbols", [])
        for r in graph.get("sec", [])
    )
    interval = graph.get("reviewed_interval")
    if interval and sec_bound:
        try:
            start = date.fromisoformat(interval["effective_start"])
            end = (
                date.fromisoformat(interval["effective_end"])
                if interval.get("effective_end")
                else None
            )
            matching = [
                r
                for r in graph.get("nasdaq", [])
                if r["source_reference"] == interval["source_reference"]
                and r["new_symbol"] == graph["symbol"]
                and r["effective_date"] == str(start)
            ]
            cusips = {r["new_cusip"] for r in matching if r.get("new_cusip")}
            bridges = [
                r["mappings"][0]
                for r in graph.get("openfigi", [])
                if r["query"].get("idType") == "ID_CUSIP"
                and r["query"]["idValue"] in cusips
                and len(r["mappings"]) == 1
            ]
            for bridge in bridges:
                for local, remote in (
                    ("share_class_figi", "shareClassFIGI"),
                    ("composite_figi", "compositeFIGI"),
                ):
                    if (
                        current.get(local)
                        and bridge.get(remote)
                        and bridge[remote] != current[local]
                    ):
                        return {
                            **result,
                            "classification": "CONFLICT",
                            "reason": "SECURITY_BRIDGE_ID_DISAGREEMENT",
                            "conflicts": [
                                {"reason": "SECURITY_BRIDGE_ID_DISAGREEMENT", "field": local}
                            ],
                        }
            bridge_bound = any(
                bridge.get("securityType2") == "Common Stock"
                and interval["stable_security_identity"]
                in {
                    "SHARE_CLASS_FIGI:" + str(bridge.get("shareClassFIGI")),
                    "COMPOSITE_FIGI:" + str(bridge.get("compositeFIGI")),
                }
                for bridge in bridges
            )
            if (
                interval["coverage"] == "COMPLETE_SECURITY_SYMBOL_MEMBERSHIP"
                and interval["source"] == "NASDAQ_TRADER"
                and interval["evidence_type"] == LIFECYCLE_EVIDENCE
                and interval["cik"] == current["cik"]
                and interval["symbol"] == graph["symbol"]
                and current.get("type") == "CS"
                and interval["security_type"] == "COMMON_STOCK"
                and interval["stable_security_identity"] in ids
                and matching
                and bridge_bound
                and (end is None or start < end)
                and datetime.fromisoformat(interval["observed_at"]).tzinfo is not None
                and interval["review_reference"].strip()
            ):
                return {
                    **result,
                    "classification": "VERIFIED_EPOCH_CANDIDATE",
                    "reason": "REVIEWED_COMPLETE_INTERVAL_WITH_SECURITY_AND_ISSUER_BRIDGES",
                    "recommendation": "PROMOTE_IN_SEPARATE_TASK",
                    "candidate_epoch": dict(interval),
                }
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    return {
        **result,
        "classification": "CORROBORATING_ONLY",
        "reason": "COMPLETE_REVIEWED_INTERVAL_AND_SECURITY_CONTINUITY_UNPROVEN",
    }


def transition_discrepancies(nasdaq, alpaca):
    """Flag unreconciled raw CUSIP assertions; never equate process/effective dates.

    Scope is the historical directed ticker edge with the same resulting CUSIP.
    This does not assert that both records are the same action, nor bind either
    to the security now using the ticker. A disagreement requires review.
    """
    results = []
    for n in nasdaq:
        for a in alpaca:
            if (
                (n["old_symbol"], n["new_symbol"], n.get("new_cusip"))
                == (a["old_symbol"], a["new_symbol"], a.get("new_cusip"))
                and n.get("old_cusip")
                and a.get("old_cusip")
                and n["old_cusip"] != a["old_cusip"]
            ):
                results.append(
                    {
                        "scope": "HISTORICAL_TRANSITION_UNRECONCILED_EVENT_ALIGNMENT",
                        "reason": "CUSIP_BEFORE_DISAGREEMENT",
                        "nasdaq_cusip": n["old_cusip"],
                        "alpaca_cusip": a["old_cusip"],
                        "nasdaq_reference": n["source_reference"],
                        "alpaca_action_id": a["provider_action_id"],
                    }
                )
    return results


def _choose_filing(payload, as_of):
    recent = payload.get("filings", {}).get("recent", payload)
    candidates = [
        {
            k: recent.get(k, [None] * len(recent.get("form", [])))[i]
            for k in ("form", "filingDate", "accessionNumber", "primaryDocument", "reportDate")
        }
        for i, form in enumerate(recent.get("form", []))
        if form in {"10-K", "10-Q", "20-F", "8-K"} and recent["filingDate"][i] <= as_of
    ]
    preferred = [r for r in candidates if r["form"] in {"10-K", "10-Q", "20-F"}]
    return max(preferred or candidates, key=lambda r: r["filingDate"], default=None)


def run_free_identity_research(output=None):
    settings = load_settings()
    root = Path(__file__).resolve().parents[3]
    output = Path(output) if output else root / "docs/research/free_identity_evidence_probe.json"
    massive_path = root / "docs/research/massive-historical-identity-probes-2026-10-15.json"
    massive = json.loads(massive_path.read_text(encoding="utf-8"))
    observations = massive["observations"]
    source_checks, sec_records, submissions_records = [], {}, {}
    with sqlite3.connect(
        settings.strategy.storage.database_path.as_uri() + "?mode=ro", uri=True
    ) as c:
        c.row_factory = sqlite3.Row
        epoch_before = c.execute("SELECT COUNT(*) FROM symbol_identity_epochs").fetchone()[0]
        alpaca = [
            dict(r)
            for r in c.execute(
                "SELECT provider_action_id,old_symbol,new_symbol,old_cusip,new_cusip,"
                "process_date,effective_date FROM security_corporate_actions "
                "WHERE provider='ALPACA' AND old_symbol IN (?,?,?,?,?,?)",
                RESEARCH_SAMPLES,
            )
        ]
        cached_rows = list(
            c.execute(
                "SELECT cik,payload,fetched_at FROM raw_sec_cache "
                "WHERE endpoint='submissions' AND cik IN ("
                + ",".join(
                    "?"
                    for _ in {
                        r["results"]["cik"] for r in observations if r.get("results", {}).get("cik")
                    }
                )
                + ")",
                sorted(
                    {r["results"]["cik"] for r in observations if r.get("results", {}).get("cik")}
                ),
            )
        )
        cached = {r["cik"]: json.loads(r["payload"]) for r in cached_rows}
        cache_times = {r["cik"]: r["fetched_at"] for r in cached_rows}
    nasdaq_client, figi_client = NasdaqLifecycleResearchClient(), OpenFigiResearchClient()
    try:
        notices = [nasdaq_client.notice(n) for n in ("ECA2021-196", "dtn2023-7")]
        source_checks.extend({"source": "NASDAQ_TRADER", **n} for n in notices)
        nasdaq = [
            r
            for n in notices
            for r in n["records"]
            if r["old_symbol"] in RESEARCH_SAMPLES or r["new_symbol"] in RESEARCH_SAMPLES
        ]
        cusips = sorted(
            {r[k] for r in [*nasdaq, *alpaca] for k in ("old_cusip", "new_cusip") if r.get(k)}
        )
        jobs = [
            {"idType": "ID_CUSIP", "idValue": v, "exchCode": "US", "includeUnlistedEquities": True}
            for v in cusips
        ]
        jobs += [{"idType": "TICKER", "idValue": s, "exchCode": "US"} for s in RESEARCH_SAMPLES]
        mappings = []
        for start in range(0, len(jobs), 5):
            result = figi_client.mapping(jobs[start : start + 5])
            source_checks.append({"source": "OPENFIGI", **result})
            mappings.extend(result["results"])
            if result.get("http_status") == 429:
                break
    finally:
        nasdaq_client.close()
        figi_client.close()
    # Only CIKs observed in the existing six-symbol artifact; one filing per CIK.
    contexts = {}
    latest_current_dates = {}
    for symbol in RESEARCH_SAMPLES:
        latest = max(
            (r for r in observations if r["identifier"] == symbol and r["operation"] == "details"),
            key=lambda r: r["as_of"],
        )
        if latest.get("results", {}).get("cik"):
            latest_current_dates[latest["results"]["cik"]] = latest["as_of"]
    for row in observations:
        if (
            row["identifier"] in RESEARCH_SAMPLES
            and row["operation"] == "details"
            and row.get("results", {}).get("cik")
        ):
            cik = row["results"]["cik"]
            contexts[cik] = min(contexts.get(cik, row["as_of"]), row["as_of"])
    if len(contexts) > 12:
        raise ValueError("FREE_RESEARCH_SEC_SCOPE_REFUSED")
    sec = SecClient(settings.require_sec_user_agent(), request_interval_seconds=1.0, max_retries=0)
    try:
        for cik, as_of in sorted(contexts.items()):
            payload = cached.get(cik)
            try:
                if payload is None:
                    payload = sec.submissions(cik)
                submissions_records[cik] = {
                    "source": "SEC_SUBMISSIONS",
                    "cik": cik,
                    "entity_name": payload.get("name"),
                    "tickers": payload.get("tickers", []),
                    "source_reference": f"https://data.sec.gov/submissions/CIK{cik}.json",
                    "observed_at": cache_times.get(cik, datetime.now(UTC).isoformat()),
                    "semantics": "CURRENT_STATE_ONLY",
                    "from_local_cache": cik in cached,
                }
                chosen = _choose_filing(payload, as_of)
                # An early IPO 8-K may have no inline DEI facts. Prefer an
                # available periodic filing for a later explicit observation,
                # without inferring anything about the intervening interval.
                if chosen and chosen["form"] == "8-K" and cik in latest_current_dates:
                    periodic = _choose_filing(payload, latest_current_dates[cik])
                    if periodic and periodic["form"] != "8-K":
                        chosen = periodic
                if chosen is None:
                    history = next(
                        (
                            f
                            for f in payload.get("filings", {}).get("files", [])
                            if f["filingFrom"] <= as_of <= f["filingTo"]
                            and re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", f["name"])
                        ),
                        None,
                    )
                    if history:
                        block = sec._get_json(
                            "https://data.sec.gov/submissions/" + history["name"],
                            resource_type="identity_research_history",
                            cik=cik,
                        )
                        chosen = _choose_filing(block, as_of)
                sec_records[cik] = (
                    fetch_sec_filing(
                        sec,
                        cik=cik,
                        accession=chosen["accessionNumber"],
                        document=chosen["primaryDocument"],
                        filing_date=chosen["filingDate"],
                        form=chosen["form"],
                        period=chosen["reportDate"],
                    )
                    if chosen
                    else {
                        "cik_context": cik,
                        "trading_symbols": [],
                        "error": "NO_TARGET_FILING_DISCOVERED",
                    }
                )
            except (requests.RequestException, SecResourceNotFound, ValueError):
                sec_records[cik] = {
                    "cik_context": cik,
                    "trading_symbols": [],
                    "error": "SEC_EVIDENCE_UNAVAILABLE",
                }
    finally:
        sec.session.close()
    graphs = []
    for symbol in RESEARCH_SAMPLES:
        rows = [r for r in observations if r["identifier"] == symbol]
        details = [r for r in rows if r["operation"] == "details" and "results" in r]
        current = max(details, key=lambda r: r["as_of"])["results"]
        transitions = [r for r in nasdaq if r["old_symbol"] == symbol or r["new_symbol"] == symbol]
        actions = [r for r in alpaca if r["old_symbol"] == symbol]
        ids = {
            r[k] for r in [*transitions, *actions] for k in ("old_cusip", "new_cusip") if r.get(k)
        }
        graph = {
            "symbol": symbol,
            "current": current,
            "cik": current.get("cik"),
            "stable_ids": stable_ids(current),
            "nasdaq": transitions,
            "alpaca": actions,
            "openfigi": [r for r in mappings if r["query"]["idValue"] in ids | {symbol}],
            "sec": [sec_records[cik] for cik in {r["results"]["cik"] for r in details}],
            "sec_submissions": [
                submissions_records[cik]
                for cik in {r["results"]["cik"] for r in details}
                if cik in submissions_records
            ],
            "massive": rows,
            "massive_semantics": "CORROBORATING_ONLY;LOCAL_ARTIFACT_REUSED",
            "ticker_transitions": transitions + actions,
            "effective_dates": [
                r["effective_date"] for r in transitions if r.get("effective_date")
            ],
            "sources_checked": [
                "NASDAQ_TRADER",
                "OPENFIGI",
                "SEC_EDGAR",
                "MASSIVE_ARTIFACT",
                "ALPACA_SQLITE",
            ],
            "evidence_found": {
                "nasdaq": len(transitions),
                "openfigi_mapping_jobs": len(
                    [r for r in mappings if r["query"]["idValue"] in ids | {symbol}]
                ),
                "massive_observations": len(rows),
                "alpaca": len(actions),
            },
            "conflicts": transition_discrepancies(transitions, actions),
            "reviewed_interval": None,
        }
        graph.update(classify_graph(graph))
        confirmed = {
            r["cik_context"]
            for r in graph["sec"]
            if r.get("issuer_binding_confirmed") and symbol in r.get("trading_symbols", [])
        }
        graph["free_sec_different_issuers_proven"] = len(confirmed) > 1
        graph["source_coverage"] = "BOUNDED_NONEXHAUSTIVE;NO_INFERRED_INTERVALS"
        graphs.append(graph)
    with sqlite3.connect(
        settings.strategy.storage.database_path.as_uri() + "?mode=ro", uri=True
    ) as c:
        epoch_after = c.execute("SELECT COUNT(*) FROM symbol_identity_epochs").fetchone()[0]
    report = {
        "observed_at": datetime.now(UTC).isoformat(),
        "production_promotion": False,
        "epoch_rows_before": epoch_before,
        "epoch_rows_after": epoch_after,
        "source_checks": source_checks,
        "sec_http_attempts": sec.request_counts,
        "nasdaq_search_scope": (
            "Two discovered public notices; no matching DSAC/ALPS notice found "
            "in bounded search; not exhaustive"
        ),
        "symbols": graphs,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run_free_identity_research()
    print(
        json.dumps(
            {
                "symbols": [
                    {
                        "symbol": g["symbol"],
                        "classification": g["classification"],
                        "recommendation": g["recommendation"],
                    }
                    for g in result["symbols"]
                ],
                "sec_http_attempts": result["sec_http_attempts"],
                "production_promotion": False,
            }
        )
    )
