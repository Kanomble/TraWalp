"""Read-only local identity, Daily batches and outcome-clean pair discovery."""

import hashlib
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from itertools import combinations, groupby
from sqlite3 import Error as SQLiteError

from trading_system.backtest import pairs_stat_arb_v1_security as security
from trading_system.backtest import research_definitions as definitions
from trading_system.backtest.liquid_universe import ResearchReadOnlyDatabase, _canonical
from trading_system.backtest.pairs_stat_arb_v1 import PairBar, calibration
from trading_system.backtest.pairs_stat_arb_v1_daily_audit import classify_requirement
from trading_system.backtest.pairs_stat_arb_v1_daily_qualification import (
    qualify_daily,
    report_daily,
)
from trading_system.backtest.pairs_stat_arb_v1_identity import (
    CURRENT,
    FAILED,
    POLICY,
    QUARANTINED,
    USABLE,
    qualify_identities,
    session_status,
)
from trading_system.backtest.pairs_stat_arb_v1_manifest import (
    MISMATCH,
    calendar_window,
    load_manifest,
    manifest_contract,
    seal_manifest,
    verify_membership,
)
from trading_system.backtest.pairs_stat_arb_v1_selection import selection_inputs
from trading_system.data.qualification import provider_range_verified
from trading_system.data.security_identity import (
    OUTSIDE,
    UNKNOWN,
    current_identity_epoch,
    identity_epoch,
)
from trading_system.data.security_reference import (
    MAPPING_VERSION,
    cached_sec_ticker_ciks,
    identity_qualification_fingerprint,
    membership_fingerprint,
    pairs_base_company_universe,
    pairs_security_qualification,
)
from trading_system.data.security_reference import (
    digest as reference_digest,
)


@dataclass
class PreparedPairs:
    start: date
    end: date
    sessions: list
    signal_dates: dict
    bars: dict
    manifest: dict
    coverage: list
    requirements: list
    metadata: dict


def company_universe(
    database,
    *,
    with_diagnostics=False,
    reference_snapshot=None,
    base_universe=None,
    current_sec_ciks=None,
):
    base = pairs_base_company_universe(database) if base_universe is None else base_universe
    current_sec_ciks = (
        cached_sec_ticker_ciks(database) if current_sec_ciks is None else current_sec_ciks
    )
    evidence = security.local_security_types(
        database, [r["symbol"] for r in base], reference_snapshot=reference_snapshot
    )
    qualified, diagnostics = [], []
    for identity in base:
        kind = evidence.get(identity["symbol"], security.SecurityTypeEvidence())
        qualification = pairs_security_qualification(
            reference_present=kind.reference_present,
            resolved=kind.resolved,
            security_type=kind.security_type,
            local_cik=identity["cik"],
            current_sec_cik=current_sec_ciks.get(identity["symbol"]),
            massive_cik=kind.provider_cik,
        )
        row = {
            **identity,
            **asdict(kind),
            **qualification,
        }
        if qualification["status"] == "ELIGIBLE_COMMON_STOCK":
            qualified.append(row)
            continue
        diagnostics.append(
            {
                **row,
                "record_kind": "SYMBOL_IDENTITY",
                "input_scope": "COMPANY_SECURITY_TYPE_REQUIREMENT",
                "roles": "COMPANY_COMMON_STOCK_IDENTITY",
            }
        )
    epochs = database.symbol_identity_epochs(row["symbol"] for row in qualified)
    for row in qualified:
        row["identity_epoch_evidence"] = epochs[row["symbol"]]
        row["identity_epoch"] = current_identity_epoch(row, epochs[row["symbol"]])
    return (qualified, diagnostics) if with_diagnostics else qualified


def load_history_anchors(database, symbols, end, *, identity_by_symbol=None):
    """Observation anchors, restricted to verified intervals whenever available."""
    anchors = {}
    identities = identity_by_symbol or {}
    with database.read_only() as connection:
        for symbol in sorted(set(symbols)):
            epoch = identity_epoch(identities.get(symbol, {}))
            ordinary = identities.get(symbol, {}).get("identity_status") == CURRENT
            if (epoch is None and not ordinary) or (epoch and epoch.effective_start > end):
                continue
            stop = min(
                end + timedelta(days=1),
                (epoch.effective_end if epoch else None) or end + timedelta(days=1),
            )
            rows = connection.execute(
                "SELECT timestamp,open,high,low,close,volume FROM bars WHERE symbol=? "
                "AND timeframe=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
                (
                    symbol,
                    definitions.PAIRS_STAT_ARB_V1.timeframe,
                    str(epoch.effective_start) if epoch else "0001-01-01",
                    str(stop),
                ),
            )
            for day, observations in groupby(rows, lambda row: date.fromisoformat(row[0][:10])):
                observations = list(observations)
                if len(observations) == 1 and _usable_bar(*observations[0][1:]) is not None:
                    anchors[symbol] = day
                    break
    return anchors


def _usable_bar(op, high, low, close, volume):
    try:
        op, high, low, close = map(float, (op, high, low, close))
        if (
            all(math.isfinite(v) for v in (op, high, low, close))
            and 0 < low <= min(op, close) <= max(op, close) <= high
            and isinstance(volume, int)
            and volume >= 0
        ):
            return PairBar(op, high, low, close, volume)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def load_daily(database, symbols, start, end, *, identity_by_symbol=None):
    """Batch-load compact bars; invalid/duplicate sessions are unusable, never filled."""
    bars = {}
    epochs = (
        {s: identity_epoch(row) for s, row in identity_by_symbol.items()}
        if identity_by_symbol is not None
        else None
    )
    symbols = sorted(set(symbols))
    if epochs is not None:
        symbols = [
            symbol
            for symbol in symbols
            if epochs.get(symbol) is not None
            or identity_by_symbol.get(symbol, {}).get("identity_status") == CURRENT
        ]
    with database.read_only() as connection:
        for offset in range(0, len(symbols), 300):
            batch = symbols[offset : offset + 300]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT symbol,timestamp,open,high,low,close,volume FROM bars "
                f"WHERE symbol IN ({placeholders}) AND timeframe=? "
                "AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp",
                [
                    *batch,
                    definitions.PAIRS_STAT_ARB_V1.timeframe,
                    str(start),
                    str(end + timedelta(days=1)),
                ],
            )
            for symbol, timestamp, op, high, low, close, volume in rows:
                day = date.fromisoformat(timestamp[:10])
                if epochs is not None and (
                    epochs.get(symbol) is not None and not epochs[symbol].contains(day)
                ):
                    continue
                key = symbol, day
                bars[key] = _usable_bar(op, high, low, close, volume) if key not in bars else None
    return bars


def source_digest(universe, bars, sessions, history_anchors=None):
    digest = hashlib.sha256(_canonical(universe))
    digest.update(_canonical({s: str(d) for s, d in sorted((history_anchors or {}).items())}))
    # Includes missing inputs and all potential members, not only selected winners.
    for identity in universe:
        symbol = identity["symbol"]
        for day in sessions:
            bar = bars.get((symbol, day))
            values = [bar.open, bar.high, bar.low, bar.close, bar.volume] if bar else None
            digest.update(_canonical([symbol, str(day), values]))
    return digest.hexdigest()


def discover_pairs(universe, bars, sessions, days, config, history_anchors=None):
    definition = definitions.PAIRS_STAT_ARB_V1
    selected, candidates = {}, []
    indexes = {day: index for index, day in enumerate(sessions)}
    identities = {row["symbol"]: row for row in universe}

    def freeze_session(day, groups):
        index = indexes[day]
        retained = {
            sic2: sorted(names, key=lambda row: (-row["adv20"], row["symbol"]))[: definition.top_n]
            for sic2, names in sorted(groups.items())
        }
        selected[str(day)] = retained
        for sic2, names in retained.items():
            by_symbol = {row["symbol"]: row for row in names}
            for a, b in combinations(sorted(by_symbol), 2):
                candidates.append(
                    {
                        "signal_session": str(day),
                        "symbol_a": a,
                        "symbol_b": b,
                        "sic2": sic2,
                        "adv20_a": by_symbol[a]["adv20"],
                        "adv20_b": by_symbol[b]["adv20"],
                        **calibration(bars, sessions, index, a, b, identity_by_symbol=identities),
                    }
                )

    _, counts = selection_inputs(
        universe, bars, sessions, days, config, history_anchors, consume_session=freeze_session
    )
    return selected, candidates, counts


def daily_requirements(
    selected, sessions, candidates, bars, history_anchors=None, *, identity_by_symbol
):
    """Only frozen selection/pair inputs, never the source-fingerprint Cartesian product.

    Calibration prefixes outside the verified epoch are insufficient history.
    Every required session inside it remains blocking when missing. An outcome
    outside it is an explicit identity terminal, never a price from another epoch.
    """
    definition = definitions.PAIRS_STAT_ARB_V1
    required = defaultdict(set)
    insufficient_calibration = set()
    epoch_requirements = {}
    indexes = {str(day): i for i, day in enumerate(sessions)}
    for signal, groups in selected.items():
        i = indexes[signal]
        for names in groups.values():
            for name in names:
                symbol = name["symbol"]
                for day in sessions[i - definition.adv_lookback_sessions + 1 : i + 1]:
                    required[symbol, day].add("SELECTED_ADV_WINDOW")
                required[symbol, sessions[i - 1]].add("SELECTED_PREVIOUS_CLOSE")
    for candidate in candidates:
        i = indexes[candidate["signal_session"]]
        for symbol in (candidate["symbol_a"], candidate["symbol_b"]):
            epoch = identity_epoch(identity_by_symbol.get(symbol, {}))
            for day in sessions[i - definition.calibration_sessions : i + 1]:
                if epoch is not None and not epoch.contains(day):
                    insufficient_calibration.add((symbol, day))
                elif session_status(identity_by_symbol.get(symbol, {}), day) not in USABLE:
                    epoch_requirements[symbol, day] = UNKNOWN
                else:
                    required[symbol, day].add("PAIR_CALIBRATION_AND_OBSERVATION")
            if candidate["calibration_status"] == "ELIGIBLE":
                for day in sessions[i + 1 : i + definition.max_hold_sessions + 1]:
                    if session_status(identity_by_symbol.get(symbol, {}), day) not in USABLE:
                        epoch_requirements[symbol, day] = (
                            UNKNOWN if epoch is None else "IDENTITY_EPOCH_CROSSING"
                        )
                    else:
                        required[symbol, day].add("POSSIBLE_OUTCOME_ONLY")
    return required, insufficient_calibration, epoch_requirements


def compact_coverage(rows, sessions):
    """Compress adjacent official-session records; keep every actionable missing bar explicit."""
    indexes = {str(day): i for i, day in enumerate(sessions)}
    result = []
    for row in rows:
        comparable = {
            k: v for k, v in row.items() if k not in {"session", "session_end", "sessions"}
        }
        if result and row["status"] != "LOCAL_MISSING":
            last = result[-1]
            if (
                comparable
                == {
                    k: v for k, v in last.items() if k not in {"session", "session_end", "sessions"}
                }
                and indexes[row["session"]] == indexes[last["session_end"]] + 1
            ):
                last["session_end"] = row["session"]
                last["sessions"] += 1
                continue
        result.append({**row, "session_end": row["session"], "sessions": 1})
    return result


def prepare_pairs_data(database, config, start, end, *, candidate_manifest=None, preflight=False):
    contract = manifest_contract(config, start, end)
    payload = (
        load_manifest(candidate_manifest, contract) if candidate_manifest is not None else None
    )
    database = ResearchReadOnlyDatabase(database.path)
    sessions, days, tail = calendar_window(start, end)
    reference_snapshot = database.security_reference_snapshot()
    reference_records, snapshot_metadata = reference_snapshot
    base_universe = pairs_base_company_universe(database)
    reference_symbols = [row["symbol"] for row in base_universe]
    contract.update(
        security_reference_fingerprint=membership_fingerprint(reference_records, reference_symbols),
        provider_type_catalog_fingerprint=(
            reference_digest(snapshot_metadata["type_catalog"]) if snapshot_metadata else None
        ),
    )
    if snapshot_metadata and snapshot_metadata["mapping_version"] != MAPPING_VERSION:
        raise ValueError("SECURITY_REFERENCE_MAPPING_VERSION_MISMATCH")
    universe, security_diagnostics = company_universe(
        database,
        with_diagnostics=True,
        reference_snapshot=reference_snapshot,
        base_universe=base_universe,
    )
    contract["identity_qualification_fingerprint"] = identity_qualification_fingerprint(
        [*universe, *security_diagnostics]
    )
    contract["identity_epoch_fingerprint"] = reference_digest(
        {row["symbol"]: row["identity_epoch_evidence"] for row in universe}
    )
    try:
        universe, identity_diagnostics = qualify_identities(database, universe, start, end)
    except (SQLiteError, OSError, ValueError, KeyError, TypeError, AttributeError):
        # A failed qualification is a system blocker, never a resolved quarantine.
        identity_diagnostics = [
            {
                "symbol": row["symbol"],
                "identity_status": FAILED,
                "status": FAILED,
                "reason": "IDENTITY_INPUT_READ_OR_CLASSIFICATION_FAILED",
                "evidence_source": [],
                "run_excluded": False,
                "validation_blocking": True,
                "record_kind": "SYMBOL_IDENTITY",
                "input_scope": "RUN_IDENTITY_QUALIFICATION",
                "roles": "HISTORICAL_SECURITY_IDENTITY",
            }
            for row in universe
        ] or [
            {
                "symbol": None,
                "identity_status": FAILED,
                "status": FAILED,
                "reason": "IDENTITY_INPUT_READ_OR_CLASSIFICATION_FAILED",
                "evidence_source": [],
                "run_excluded": False,
                "validation_blocking": True,
                "record_kind": "SYMBOL_IDENTITY",
                "input_scope": "RUN_IDENTITY_QUALIFICATION",
                "roles": "HISTORICAL_SECURITY_IDENTITY",
            }
        ]
        universe = []
    # Existing CIK conflicts remain their established nonblocking exclusion;
    # expose them in the unified run-level identity report as well.
    identity_diagnostics.extend(
        {
            "symbol": row["symbol"],
            "identity_status": QUARANTINED,
            "status": QUARANTINED,
            "reason": row["identity_conflict_class"],
            "evidence_source": ["LOCAL_SEC_MASSIVE_CURRENT_IDENTITY"],
            "evidence_fingerprint": identity_qualification_fingerprint([row]),
            "run_excluded": True,
            "validation_blocking": False,
            "record_kind": "SYMBOL_IDENTITY",
            "input_scope": "RUN_IDENTITY_QUALIFICATION",
            "roles": "HISTORICAL_SECURITY_IDENTITY",
        }
        for row in security_diagnostics
        if row.get("quarantine_status")
    )
    identity_diagnostics.sort(key=lambda row: row["symbol"] or "")
    contract["run_identity_qualification_fingerprint"] = reference_digest(
        {"policy": POLICY, "symbols": identity_diagnostics}
    )
    identity_complete = not any(row["validation_blocking"] for row in identity_diagnostics)
    identity_summary = {
        "eligible": len(universe),
        "quarantined": sum(row["identity_status"] == QUARANTINED for row in identity_diagnostics),
        "verified_outside_run": sum(
            row["identity_status"] == OUTSIDE for row in identity_diagnostics
        ),
        "unresolved_blocking": sum(row["validation_blocking"] for row in identity_diagnostics),
    }
    universe, daily_diagnostics, daily_summary, daily_complete, daily_fingerprint = qualify_daily(
        database, universe, config, start, end
    )
    contract["run_daily_qualification_fingerprint"] = daily_fingerprint
    symbols = [row["symbol"] for row in universe]
    identities = {row["symbol"]: row for row in universe}
    if candidate_manifest is not None:
        payload = load_manifest(candidate_manifest, contract)
    security_complete = not any(r["validation_blocking"] for r in security_diagnostics)
    if payload is not None and (
        payload["discovery_counts"]["company_security_types_complete"] != security_complete
        or payload["discovery_counts"]["security_type_diagnostics"] != security_diagnostics
    ):
        raise ValueError(f"{MISMATCH}: security identity qualification changed")
    if not preflight and not security_complete:
        raise ValueError(security.UNAVAILABLE)
    if not preflight and not identity_complete:
        raise ValueError(FAILED)
    if not preflight and not daily_complete:
        raise ValueError("DAILY_QUALIFICATION_FAILED")
    qualification_complete = security_complete and identity_complete and daily_complete
    # Resolve the entire static identity boundary before touching historical prices.
    bars = (
        load_daily(database, symbols, sessions[0], days[-1], identity_by_symbol=identities)
        if qualification_complete
        else {}
    )
    history_anchors = (
        load_history_anchors(database, symbols, days[-1], identity_by_symbol=identities)
        if qualification_complete
        else {}
    )
    digest = source_digest(universe, bars, [s for s in sessions if s <= days[-1]], history_anchors)
    if payload is None:
        if qualification_complete:
            selected, candidates, counts = discover_pairs(
                universe, bars, sessions, days, config, history_anchors
            )
        else:
            selected, candidates = {}, []
            _, counts = selection_inputs([], {}, sessions, days, config)
            counts["selection_membership_resolved"] = False
        counts.update(
            company_security_types_complete=security_complete,
            security_type_diagnostics=security_diagnostics,
            identity_qualification_complete=identity_complete,
            identity_qualification=identity_summary,
            identity_qualification_diagnostics=identity_diagnostics,
            daily_data_qualification_complete=daily_complete,
            daily_data_qualification=daily_summary,
            daily_data_qualification_diagnostics=daily_diagnostics,
        )
        payload = seal_manifest(contract, universe, selected, candidates, counts, digest)
    else:
        _, counts = selection_inputs(
            universe,
            bars,
            sessions,
            days,
            config,
            history_anchors,
            consume_session=lambda day, groups: None,
        )
        counts.update(
            company_security_types_complete=security_complete,
            security_type_diagnostics=security_diagnostics,
            identity_qualification_complete=identity_complete,
            identity_qualification=identity_summary,
            identity_qualification_diagnostics=identity_diagnostics,
            daily_data_qualification_complete=daily_complete,
            daily_data_qualification=daily_summary,
            daily_data_qualification_diagnostics=daily_diagnostics,
        )
    if not preflight and not payload["discovery_counts"]["selection_membership_resolved"]:
        raise ValueError("PAIRS_STAT_ARB_SELECTION_MEMBERSHIP_UNRESOLVED")
    required, insufficient_calibration, epoch_requirements = daily_requirements(
        payload["selected"],
        sessions,
        payload["candidates"],
        bars,
        history_anchors,
        identity_by_symbol=identities,
    )
    selection_gaps = {
        (row["symbol"], date.fromisoformat(row["session"])): row
        for row in payload["discovery_counts"]["internal_selection_gaps"]
    }
    for key in selection_gaps:
        required[key].add("LIQUIDITY_SELECTION_INTERNAL_GAP")
    tail_symbols = {symbol for symbol, day in required if day > end}
    # Discovery and source hashing above never receive post-T outcome data.
    if tail_symbols:
        bars.update(
            load_daily(database, tail_symbols, tail[0], tail[-1], identity_by_symbol=identities)
        )
    if not preflight:
        bars.update(
            load_daily(
                database, [definitions.PAIRS_STAT_ARB_V1.benchmark_symbol], days[0], tail[-1]
            )
        )
    coverage_state = database.sync_values("daily_history_coverage")
    conflicts = [
        symbol
        for symbol in symbols
        if isinstance(coverage_state.get(symbol), dict)
        and (
            coverage_state[symbol].get("adjustment")
            not in (None, config.universe.market_data_adjustment)
            or coverage_state[symbol].get("feed")
            not in (None, config.universe.market_data_feed.lower())
        )
    ]
    if conflicts:
        raise ValueError(f"PAIRS_STAT_ARB_DAILY_PROVENANCE_MISMATCH: {conflicts[:10]}")
    coverage = []
    for (symbol, day), roles in sorted(required.items()):
        present = bars.get((symbol, day)) is not None
        verified = provider_range_verified(
            coverage_state.get(symbol),
            day,
            day,
            feed=config.universe.market_data_feed.lower(),
            adjustment=config.universe.market_data_adjustment,
        )
        coverage.append(
            {
                "symbol": symbol,
                "session": str(day),
                "timeframe": definitions.PAIRS_STAT_ARB_V1.timeframe,
                "record_kind": "SESSION_RANGE",
                "input_scope": "VALIDATION_BLOCKING_REQUIREMENT",
                "validation_blocking": True,
                "roles": ";".join(sorted(roles)),
                "status": "REQUIRED_PRESENT" if present else "LOCAL_MISSING",
                "provider_range_verified": verified,
                "provider_range_status": "PROVIDER_RANGE_CHECKED" if verified else "NOT_CHECKED",
                "provider_session_absence": "NOT_ESTABLISHED",
                "provider_session_absence_status": "PROVIDER_SESSION_ABSENCE_UNKNOWN",
                "fetchability": "UNKNOWN" if not present else "NOT_APPLICABLE",
                "identity_epoch_status": session_status(identities[symbol], day),
                **(
                    {
                        "sic2": selection_gaps[symbol, day]["sic2"],
                        "first_observed_session_on_or_before_T": selection_gaps[symbol, day][
                            "first_observed_session_on_or_before_T"
                        ],
                        "affected_signal_sessions": selection_gaps[symbol, day][
                            "affected_signal_sessions"
                        ],
                    }
                    if (symbol, day) in selection_gaps
                    else {}
                ),
            }
        )
    # Keep missing requirements explicit and auditable. Successfully checked
    # ranges do not establish listing boundaries or per-session bar availability.
    coverage = [
        classify_requirement(
            row,
            identities[row["symbol"]],
            local_present=False,
            range_checked=row["provider_range_verified"],
        )
        if row["status"] == "LOCAL_MISSING"
        else row
        for row in coverage
    ]
    coverage = compact_coverage(coverage, sessions)
    coverage.extend(
        {
            "symbol": symbol,
            "session": str(day),
            "session_end": str(day),
            "sessions": 1,
            "timeframe": definitions.PAIRS_STAT_ARB_V1.timeframe,
            "record_kind": "SESSION_RANGE",
            "roles": "POSSIBLE_OUTCOME_IDENTITY",
            "input_scope": "IDENTITY_EPOCH_REQUIREMENT",
            "validation_blocking": True,
            "status": status,
        }
        for (symbol, day), status in sorted(epoch_requirements.items())
    )
    requirements = list(coverage)
    daily_report, daily_report_summary = report_daily(
        daily_diagnostics,
        required,
        [row for row in coverage if row["status"] == "LOCAL_MISSING"],
    )
    counts.update(
        daily_data_gate_complete=daily_complete,
        daily_data_qualification_complete=(
            daily_complete and not daily_report_summary["unresolved_blocking"]
        ),
        daily_data_qualification=daily_report_summary,
        daily_data_qualification_diagnostics=daily_report,
    )
    if candidate_manifest is not None:
        verify_membership(payload, universe, digest, bars, sessions, counts)
    else:
        payload = seal_manifest(contract, universe, selected, candidates, counts, digest)
    coverage.extend(
        compact_coverage(
            [
                {
                    "symbol": symbol,
                    "session": str(day),
                    "record_kind": "SESSION_RANGE",
                    "timeframe": definitions.PAIRS_STAT_ARB_V1.timeframe,
                    "input_scope": "CALIBRATION_DIAGNOSTIC",
                    "validation_blocking": False,
                    "roles": "PAIR_CALIBRATION",
                    "status": "INSUFFICIENT_CALIBRATION_HISTORY",
                }
                for symbol, day in sorted(insufficient_calibration)
            ],
            sessions,
        )
    )
    coverage.extend(payload["discovery_counts"]["selection_history_diagnostics"])
    coverage.extend(payload["discovery_counts"]["identity_epoch_diagnostics"])
    coverage.extend(security_diagnostics)
    coverage.extend(
        row for row in identity_diagnostics if row["run_excluded"] or row["validation_blocking"]
    )
    observed = [day for (symbol, day), bar in bars.items() if bar and (symbol, day) in required]
    return PreparedPairs(
        start,
        end,
        sessions,
        {str(day): day for day in days},
        bars,
        payload,
        coverage,
        requirements,
        {
            "preparation_sql_queries": database.sql_queries,
            "daily_data_qualification": daily_report_summary,
            "daily_data_qualification_diagnostics": daily_report,
            "simulation_sql_queries": 0,
            "candidate_source": "MANIFEST" if candidate_manifest is not None else "DISCOVERED",
            "outcome_data_end": str(max(observed)) if observed else None,
            "blocking_required_symbol_sessions": len(required),
            "insufficient_calibration_symbol_sessions": len(insufficient_calibration),
            "identity_epoch_outcome_requirements": len(epoch_requirements),
            "company_security_type_status": "AVAILABLE"
            if security_complete
            else security.UNAVAILABLE,
            "company_only_universe_satisfied": security_complete,
            "metadata_sync_requirement": "EXPLICIT_SYNC_SECURITY_REFERENCE_AND_SEC_IDENTITY_REVIEW"
            if not security_complete
            else None,
            "source_fingerprint_inputs": {
                "input_scope": "SOURCE_FINGERPRINT_INPUT",
                "validation_blocking": False,
                "symbols": len(universe),
                "session_start": str(sessions[0]),
                "session_end": str(days[-1]),
                "sessions": len(sessions) - len(tail),
                "fingerprint": digest,
                "note": "All current-universe selection-period inputs, including missing markers; "
                "only separately qualified candidate requirements block validation.",
            },
            "daily_provenance": "CONFIG_BOUND_NO_PER_BAR_PROVENANCE",
            "symbols_without_coverage_provenance": [s for s in symbols if s not in coverage_state],
            "provider_confirmed_absent": None,
            "provider_check_failed": None,
            "provider_session_status_support": "UNAVAILABLE_IN_DAILY_RANGE_METADATA",
        },
    )
