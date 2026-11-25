"""Coverage and acquisition rules for F01 historical readiness."""

from collections import Counter, defaultdict

from f01_alpaca_historical_sip_coverage import gaps, retained_for_plan
from f01_bulk_staging_rules import METRICS
from f01_readiness_inventory import (
    LEGACY,
    REQUIRED_RESEARCH_METRICS,
    SIP,
    START,
    U0,
    encoding,
    period_key,
)

from trading_system.data.canonical_facts import fingerprint

PIT_BLOCKERS = (
    "HISTORICAL_SECURITY_AND_UNIVERSE_UNVERIFIED",
    "ORIGINAL_FUNDAMENTAL_AND_ACCOUNTING_PIT_NOT_PROVEN",
    "ADJUSTMENT_CORPORATE_ACTION_AND_EXIT_CONTINUITY_UNVERIFIED",
)
PRIORITY_METRICS = {
    "operating_cash_flow",
    "operating_cf",
    "shares_outstanding",
    "eps_diluted",
    "total_debt",
    "revenue",
    "capital_expenditure",
    "capex",
    "operating_income",
    "stockholders_equity",
    "cash",
    "net_income",
    "interest_expense",
    "depreciation_amortization",
    "income_tax",
    "debt_current",
    "debt_noncurrent",
}


def fundamental_rows(labels, years, published, cells, canonical_cells):
    for cik, symbol in sorted(labels.items()):
        for year in years:
            for metric in (*METRICS, *REQUIRED_RESEARCH_METRICS):
                key = cik, str(year), metric
                row, cell = published.get(key, {}), cells[key]
                raw = int(row.get("observed", cell["raw_tag_only"]))
                usable = cell["candidates"]
                yield {
                    "cik": cik,
                    "symbol": symbol,
                    "year": year,
                    "metric": metric,
                    "raw_observations": raw,
                    "raw_available": bool(raw),
                    "research_usable": bool(usable),
                    "retrospective_candidates": usable,
                    "acceptance_bound_candidates": cell["acceptance"],
                    "annual_form_candidates": cell["annual_form_candidates"],
                    "quarter_form_candidates": cell["quarter_form_candidates"],
                    "instant_candidates": cell["instant_candidates"],
                    "amendment_candidates": cell["amendments"],
                    "distinct_candidate_periods": cell["distinct_periods"],
                    "distinct_candidate_period_end_dates": cell["distinct_period_ends"],
                    "first_candidate_period_end": cell.get("first_period_end", ""),
                    "last_candidate_period_end": cell.get("last_period_end", ""),
                    "stored_canonical_facts_pilot": canonical_cells.get(key, {}).get(
                        "f01-sec-isolated-research-pilot01", 0
                    ),
                    "stored_canonical_facts_e11": canonical_cells.get(key, {}).get(
                        "f01-sec-isolated-research-accounting-e11", 0
                    ),
                    "stored_canonical_facts_historical50": canonical_cells.get(key, {}).get(
                        "f01-sec-isolated-research-historical50-v1", 0
                    ),
                    "ambiguous": int(row.get("ambiguous", 0)),
                    "excluded": int(row.get("excluded", 0)),
                    "staging_quality_score": row.get("quality_score_0_to_100", ""),
                    "historical_pit_qualified": False,
                    "expected_period_model": (
                        "FISCAL_CONTEXT_REVIEW_REQUIRED"
                        if row
                        else "OUTSIDE_NORMALIZATION_CONTRACT"
                    ),
                    "status": (
                        "RETROSPECTIVE_ONLY"
                        if usable
                        else "RAW_ONLY"
                        if raw
                        else "NO_LOCAL_CANDIDATE"
                    ),
                }


def security_rows(cohort, d8, start, end):
    benchmark = d8.get("benchmark_security_interval") or {}
    for row in cohort:
        symbol, cik = row["symbol"], row["cik"]
        record = d8.get("fixed_symbol_points", {}).get(symbol, {})
        blockers = row.get("blocking_reasons", []) or record.get("blockers", [])
        yield {
            "cik": cik,
            "symbol": symbol,
            "from": start,
            "until_exclusive": "",
            "audited_to": end,
            "security_id": "",
            "status": "SECURITY_EPOCH_UNRESOLVED",
            "corporate_action_status": "NOT_QUALIFIED",
            "reference": ";".join(blockers) if isinstance(blockers, list) else blockers,
        }
        for point in row.get("dated_security_points", []):
            yield {
                "cik": cik,
                "symbol": symbol,
                "from": point["valid_from"],
                "until_exclusive": point["valid_to"],
                "audited_to": end,
                "security_id": point["epoch_id"],
                "status": "DATED_WITNESS_ONLY",
                "corporate_action_status": "NOT_QUALIFIED",
                "reference": point["evidence_hash"],
            }
    yield {
        "cik": "",
        "symbol": "SPY",
        "from": start,
        "until_exclusive": "",
        "audited_to": end,
        "security_id": benchmark.get("security_id", "") if isinstance(benchmark, dict) else "",
        "status": "BENCHMARK_EPOCH_UNRESOLVED",
        "corporate_action_status": "NOT_QUALIFIED",
        "reference": encoding(benchmark).decode().strip().replace("\n", ""),
    }


def market_rows(stores, sessions, by_symbol):
    for (series, symbol), (seen, good, invalid, _) in sorted(stores.items()):
        source, provider, feed, adjustment = series
        days = sorted(seen.intersection(sessions)) if source == "probe" else sessions
        streak = 0
        for session in days:
            prior = streak
            streak = streak + 1 if session in good else 0
            yield {
                "cik": by_symbol[symbol],
                "symbol": symbol,
                "session": session,
                "source": source,
                "provider": provider,
                "feed": feed,
                "adjustment": adjustment,
                "granularity": "1d",
                "window": "PRE2010_PREROLL" if session < START else "HISTORY",
                "raw_available": session in seen,
                "research_usable": session in good,
                "historical_pit_qualified": False,
                "qualifying_prior_sessions": prior,
                "invalid_or_duplicate_rows": invalid.get(session, 0),
                "status": (
                    "VALID_BAR_UNQUALIFIED"
                    if session in good
                    else "INVALID_OR_DUPLICATE"
                    if session in seen
                    else "NO_BAR_REFERENCE_CALENDAR"
                ),
            }


def market_proxy(stores, sessions, symbols, warmup):
    """Intersect uninterrupted benchmark/issuer series without combining provenances."""
    result = {}
    for source in (LEGACY, U0, SIP):
        usable = {s: stores[source, s][1] for s in (*symbols, "SPY")}
        streak, counts = Counter(), {}
        for session in sessions:
            counts[session] = (
                sum(s in usable and session in usable[s] and streak[s] >= warmup for s in symbols)
                if session in usable["SPY"] and streak["SPY"] >= warmup
                else 0
            )
            for s, days in usable.items():
                streak[s] = streak[s] + 1 if session in days else 0
        result[source[0]] = counts
    return result


def readiness_rows(market_counts, sessions, baseline, qualification):
    grouped = defaultdict(list)
    for session in sessions:
        if session >= START:
            grouped["MONTH", period_key(session)].append(session)
            grouped["YEAR", session[:4]].append(session)
    for (grain, period), days in sorted(grouped.items()):
        for scope, source in (
            ("FROZEN_U0_RECONSTRUCTION", "u0_snapshot"),
            ("EXPANDED_D6_50", "legacy"),
            ("EXPANDED_D6_50", "forward"),
        ):
            count = market_counts[source]
            overlap = sum(
                baseline["historical_start"] <= s <= baseline["historical_end"] for s in days
            )
            u0 = scope == "FROZEN_U0_RECONSTRUCTION"
            active = u0 and overlap > 0 and qualification["qualified"]
            yield {
                "scope": scope,
                "period_kind": grain,
                "period": period,
                "source": source,
                "first_session": days[0],
                "last_session": days[-1],
                "reference_sessions": len(days),
                "market_proxy_first_count_d6_overlap": count[days[0]],
                "market_proxy_peak_count_d6_overlap": max(count[d] for d in days),
                "market_proxy_session_count": sum(count[d] > 0 for d in days),
                "prior_published_reconstruction_sessions": overlap if active else 0,
                "proven_full_expanded_technical_count": "" if u0 else 0,
                "proven_historical_pit_security_count": 0,
                "status": "PUBLISHED_U0_WINDOW" if active else "FULL_READINESS_UNPROVEN",
                "blockers": ";".join(PIT_BLOCKERS),
            }


def gap_row(
    priority,
    category,
    reason,
    *,
    cik="",
    symbol="",
    item="",
    start="",
    end="",
    count="",
    provider="",
    detail="",
):
    body = {
        "priority": priority,
        "category": category,
        "reason": reason,
        "cik": cik,
        "symbol": symbol,
        "item": item,
        "start": start,
        "end": end,
        "count": count,
        "candidate_provider": provider,
        "detail": detail,
    }
    return {"gap_id": fingerprint(body), **body}


def market_gaps(stores, sessions, by_symbol, minimum):
    buckets = defaultdict(list)
    for day in sessions:
        buckets["WARMUP" if day < START else day[:4]].append(day)
    plan, ledger, counts = [], [], Counter()
    d8_stores = {
        (series[0], symbol, series[2], series[3]): result
        for (series, symbol), result in stores.items()
        if series[1] == "alpaca"
    }
    for symbol, cik in sorted(by_symbol.items(), key=lambda x: (x[0] != "SPY", x[0])):
        reusable = retained_for_plan(d8_stores, symbol, ("sip", "all"))
        counts["spy_explicit_reusable" if not cik else "issuer_explicit_reusable"] += len(
            reusable.intersection(sessions)
        )
        for period, days in buckets.items():
            for run in gaps(days, set(days) - reusable):
                early = run[0] < "2016"
                label = "spy" if symbol == "SPY" else "issuer"
                counts[f"{label}_missing_reference"] += len(run)
                if early:
                    counts[f"{label}_missing_pre2016"] += len(run)
                if period == "WARMUP" and not cik:
                    counts["spy_preroll_320_missing"] += len(run)
                    counts["spy_warmup_300_missing"] += len(
                        set(run).intersection(buckets["WARMUP"][-minimum:])
                    )
                provider = "UNRESOLVED_PRE2016_SOURCE" if early else "ALPACA_SIP"
                reason = "SPY_DAILY_MISSING" if not cik else "DAILY_GAP_IF_LISTED_AND_ELIGIBLE"
                row = gap_row(
                    "P0",
                    "market",
                    reason,
                    cik=cik,
                    symbol=symbol,
                    item="Daily SIP/all",
                    start=run[0],
                    end=run[-1],
                    count=len(run),
                    provider=provider,
                    detail="XNYS reference; resolve security interval before issuer requests",
                )
                ledger.append(row)
                plan.append(
                    {
                        "gap_id": row["gap_id"],
                        "symbol": symbol,
                        "cik": cik,
                        "feed": "sip",
                        "adjustment": "all",
                        "timeframe": "1Day",
                        "start": run[0],
                        "end": run[-1],
                        "sessions": len(run),
                        "start_inclusive_end_inclusive": True,
                        "provider": provider,
                        "is_warmup": period == "WARMUP",
                        "status": "ALTERNATE_SOURCE_VERIFICATION_REQUIRED"
                        if early
                        else "IDENTITY_AND_ENTITLEMENT_REQUIRED"
                        if cik
                        else "ENTITLEMENT_REQUIRED",
                    }
                )
    return ledger, plan, counts


def filing_gaps(rows, groups, canonical_pairs):
    planned = {
        (row["cik"], row["accession"]): group["batch_id"]
        for group in groups
        for row in group["accessions"]
    }
    ledger, buckets, counts = [], defaultdict(list), Counter()
    for row in rows:
        key = row["cik"], row["accession"]
        status = row["original_status"]
        counts[status] += 1
        already_local = status != "MISSING_IN_SCANNED_CACHES"
        rank = "P1" if row["form"] in {"10-K", "10-Q"} else "P2"
        if status != "ORIGINAL_BYTES_MATCH_PUBLISHED_MANIFEST":
            ledger.append(
                gap_row(
                    rank,
                    "fundamental_original",
                    status,
                    cik=key[0],
                    symbol=row["symbol_label"],
                    item=key[1],
                    start=row["filed"],
                    end=row["filed"],
                    count=1,
                    provider="LOCAL" if already_local else "SEC_EDGAR",
                    detail=";".join(row["problems"]),
                )
            )
        item = {
            field: row[field]
            for field in (
                "cik",
                "accession",
                "symbol_label",
                "form",
                "filed",
                "acceptance_utc",
                "problems",
            )
        }
        item |= {
            "original_status": status,
            "published_dts_ready": row["published_dts_ready"],
            "published_extraction_complete": row["published_extraction_complete"],
            "in_existing_canonical_storage": key in canonical_pairs,
            "cache_directories": row["cache_directories"],
            "byte_check": row["byte_check"],
        }
        if already_local:
            buckets["LOCAL_REUSE_AND_REVIEW"].append(item)
        elif key in planned:
            item["parent_task94_batch_id"] = planned[key]
            buckets[f"{rank}_SEC_{row['filed'][:4]}"].append(item)
        else:
            buckets["METADATA_OR_AMENDMENT_REVIEW"].append(item)
    result = []
    for key, items in sorted(
        buckets.items(),
        key=lambda kv: (
            kv[0] != "LOCAL_REUSE_AND_REVIEW",
            not kv[0].startswith(("P1", "P2")),
            kv[0],
        ),
    ):
        items.sort(key=lambda r: (r["filed"], r["cik"], r["accession"]))
        for offset in range(0, len(items), 20):
            subset = items[offset : offset + 20]
            body = {
                "category": key,
                "accessions": subset,
                "status": "PLAN_ONLY",
                "authorization": "SEPARATE_HASH_BOUND_SCOPE_REQUIRED",
            }
            result.append(body | {"block_id": fingerprint(body)})
    return ledger, result, counts
