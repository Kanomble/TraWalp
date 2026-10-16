"""Explicit bounded provider audit/repair; persists source-scoped qualification evidence."""

import json
from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta

from trading_system.backtest.pairs_stat_arb_v1_daily_audit import (
    audit_daily_requirements,
    consecutive_ranges,
    repair_plan,
)
from trading_system.backtest.pairs_stat_arb_v1_daily_qualification import SAFE_ROLES, STORE, scope
from trading_system.data.qualification import provider_range_verified
from trading_system.data.security_reference import digest


def validate_requirement_roles(manifest, rows):
    """Prove requested roles from frozen membership, without calibration results."""
    indexes = {day: i for i, day in enumerate(manifest["history_sessions"])}
    symbols = {r["symbol"] for r in rows}
    selection, calibration = defaultdict(set), defaultdict(set)
    for signal, groups in manifest["selected"].items():
        for names in groups.values():
            for name in names:
                if name["symbol"] in symbols:
                    selection[name["symbol"]].add(indexes[signal])
    for candidate in manifest["candidates"]:
        for key in ("symbol_a", "symbol_b"):
            if candidate[key] in symbols:
                calibration[candidate[key]].add(indexes[candidate["signal_session"]])
    selection = {s: sorted(days) for s, days in selection.items()}
    calibration = {s: sorted(days) for s, days in calibration.items()}
    gaps = {
        (r["symbol"], r["session"]) for r in manifest["discovery_counts"]["internal_selection_gaps"]
    }
    definition = manifest["strategy_definition"]

    def has_signal(values, lower, upper):
        i = bisect_left(values, lower)
        return i < len(values) and values[i] <= upper

    for row in rows:
        index = indexes[row["session"]]
        selected = selection.get(row["symbol"], [])
        paired = calibration.get(row["symbol"], [])
        valid = {
            "LIQUIDITY_SELECTION_INTERNAL_GAP": (row["symbol"], row["session"]) in gaps,
            "SELECTED_PREVIOUS_CLOSE": has_signal(selected, index + 1, index + 1),
            "SELECTED_ADV_WINDOW": has_signal(
                selected, index, index + definition["adv_lookback_sessions"] - 1
            ),
            "PAIR_CALIBRATION_AND_OBSERVATION": has_signal(
                paired, index, index + definition["calibration_sessions"]
            ),
        }
        if row["session"] > manifest["signal_end"] or any(
            not valid[role] for role in row["roles"].split(";")
        ):
            raise ValueError("DAILY_QUALIFICATION_REQUIREMENT_ROLE_NOT_PROVEN")


def provider_failure(exc):
    """Sanitized outcome taxonomy; exceptions never establish source absence."""
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    if code in {401, 403} or isinstance(exc, PermissionError):
        return "AUTH_ERROR"
    if code == 429:
        return "RATE_LIMIT_ERROR"
    if isinstance(exc, (TypeError, AttributeError)):
        return "PARSER_ERROR"
    if isinstance(exc, ValueError):
        return (
            "INVALID_RESPONSE"
            if str(exc)
            in {
                "PROVIDER_BAR_VALIDATION_FAILED",
                "PROVIDER_RESPONSE_UNEXPECTED",
                "ALPACA_UNEXPECTED_BAR_RESPONSE",
            }
            else "PARSER_ERROR"
        )
    return "TRANSIENT_ERROR"


def audit_and_repair(
    database,
    manifest,
    requirements,
    config,
    provider,
    synchronize,
    *,
    save_plan,
    only_requirement_keys=None,
):
    """Probe only actual blocking requirements; sync only positively verified gaps.

    An empty response is resolved only after this complete audit, explicit repair
    decision and local recheck. Any request/parser/ingestion failure remains failed.
    No return, correlation, signal, outcome or performance value is inspected.
    """
    from trading_system.backtest.pairs_stat_arb_v1_daily_fixpoint import requirement_source
    from trading_system.backtest.pairs_stat_arb_v1_data import load_daily

    initial = audit_daily_requirements(database, manifest, requirements, config)
    binding = scope(
        config,
        date.fromisoformat(manifest["requested_start"]),
        date.fromisoformat(manifest["requested_end"]),
    )
    if (
        manifest["strategy_definition"] != binding["definition"]
        or manifest["min_price"] != binding["min_price"]
        or manifest["min_adv20"] != binding["min_adv20"]
    ):
        raise ValueError("DAILY_QUALIFICATION_RESEARCH_CONTRACT_CHANGED")
    old = database.sync_values(STORE).get(digest(binding))
    if old is not None and (
        not isinstance(old, dict)
        or old["scope"] != binding
        or old["fingerprint"] != digest({k: v for k, v in old.items() if k != "fingerprint"})
    ):
        raise ValueError("DAILY_QUALIFICATION_EXISTING_EVIDENCE_CORRUPT")
    evidence = dict(old["symbols"]) if old else {}
    identities = {r["symbol"]: r for r in manifest["universe"]}
    grouped = defaultdict(list)
    role_filter = defaultdict(set)
    if only_requirement_keys is not None:
        for symbol, day, role, source in only_requirement_keys:
            if source != requirement_source(manifest):
                raise ValueError("DAILY_QUALIFICATION_REQUIREMENT_SCOPE_CHANGED")
            role_filter[symbol, day].add(role)
    for row in initial["requirements"]:
        # Outcome-only gaps cannot determine research-universe membership.
        roles = set(row["roles"].split(";")) & SAFE_ROLES
        if only_requirement_keys is not None:
            roles &= role_filter[row["symbol"], row["session"]]
        if roles and row["validation_blocking"] and row["status"] == "LOCAL_MISSING":
            grouped[row["symbol"]].append({**row, "roles": ";".join(sorted(roles))})
    validate_requirement_roles(manifest, [r for rows in grouped.values() for r in rows])
    observations = []
    for symbol, rows in sorted(grouped.items()):
        proof = evidence.get(symbol, {})
        cached = (
            {
                r["session"]: r
                for r in proof.get("sessions", [])
                if r["provider_result"] in {"BAR_RETURNED", "EMPTY_RESPONSE_UNRESOLVED"}
            }
            if (
                proof.get("identity_fingerprint") == digest(identities[symbol])
                and proof.get("audit_completed")
                and proof.get("local_recheck_completed")
                and proof.get("repair_plan_completed")
            )
            else {}
        )
        unprobed = []
        for row in rows:
            known = cached.get(row["session"])
            if known is not None:
                observations.append(
                    {
                        "symbol": symbol,
                        "session": row["session"],
                        "provider_symbol": symbol,
                        "provider": "ALPACA",
                        "feed": binding["feed"],
                        "adjustment": binding["adjustment"],
                        "status": known["provider_result"],
                        "observed_at": known["observed_at"],
                        "reused": True,
                        "provider_outcome": known.get(
                            "provider_outcome",
                            "SUCCESS_NO_REQUIRED_BARS"
                            if known["provider_result"] == "EMPTY_RESPONSE_UNRESOLVED"
                            else "SUCCESS_WITH_BARS",
                        ),
                    }
                )
            else:
                unprobed.append(date.fromisoformat(row["session"]))
        for block in consecutive_ranges(unprobed):
            try:
                bars = provider.daily_bars(
                    [symbol],
                    datetime.combine(block[0], time.min, UTC),
                    datetime.combine(block[-1] + timedelta(days=1), time.min, UTC),
                )
                if getattr(provider, "last_bar_diagnostics", {}).get("invalid_bars", 0):
                    raise ValueError("PROVIDER_BAR_VALIDATION_FAILED")
                keys = [(bar.symbol, bar.timestamp.date()) for bar in bars]
                if len(keys) != len(set(keys)) or any(s != symbol for s, _ in keys):
                    raise ValueError("PROVIDER_RESPONSE_UNEXPECTED")
                statuses = {
                    str(day): "BAR_RETURNED"
                    if (symbol, day) in keys
                    else "EMPTY_RESPONSE_UNRESOLVED"
                    for day in block
                }
            except Exception as exc:
                # Fail closed, including HTTP 404/auth/rate limits. No raw bodies.
                statuses = {str(day): "REQUEST_FAILED" for day in block}
                error_type = type(exc).__name__
                failure = provider_failure(exc)
            else:
                error_type = None
                failure = None
            observations.extend(
                {
                    "symbol": symbol,
                    "session": day,
                    "provider_symbol": symbol,
                    "provider": "ALPACA",
                    "feed": config.universe.market_data_feed.lower(),
                    "adjustment": config.universe.market_data_adjustment,
                    "status": status,
                    "error_type": error_type,
                    "provider_outcome": failure
                    or (
                        "SUCCESS_WITH_BARS"
                        if status == "BAR_RETURNED"
                        else "SUCCESS_NO_REQUIRED_BARS"
                    ),
                    "reused": False,
                    "observed_at": datetime.now(UTC).isoformat(),
                }
                for day, status in statuses.items()
            )
    audited = audit_daily_requirements(
        database, manifest, requirements, config, observations=observations
    )
    planned_keys = {(r["symbol"], r["session"]) for rows in grouped.values() for r in rows}
    plan = repair_plan(
        [r for r in audited["requirements"] if (r["symbol"], r["session"]) in planned_keys]
    )
    save_plan(plan)  # Concrete plan exists before the first mutation/sync.
    repairs, repair_failed, attempted = [], set(), set()
    for item in plan:
        if item["action"] != "SYNC_DAILY":
            continue
        symbol = item["symbol"]
        attempted.add(symbol)
        try:
            result = synchronize(
                symbol, date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
            )
            if result.get("errors", 0) or result.get("invalid_bars", 0):
                repair_failed.add(symbol)
            repairs.append({**item, "result": result})
        except Exception as exc:
            repair_failed.add(symbol)
            repairs.append({**item, "error_type": type(exc).__name__})
    results = {(r["symbol"], r["session"]): r for r in observations}
    coverage = database.sync_values("daily_history_coverage")
    for symbol, rows in grouped.items():
        days = [date.fromisoformat(r["session"]) for r in rows]
        present = load_daily(
            database,
            [symbol],
            min(days),
            max(days),
            identity_by_symbol={symbol: identities[symbol]},
        )
        sessions = [
            {
                "session": r["session"],
                "roles": r["roles"],
                "provider_result": results[symbol, r["session"]]["status"],
                "provider_outcome": results[symbol, r["session"]]["provider_outcome"],
                "observed_at": results[symbol, r["session"]]["observed_at"],
                "local_present_after_recheck": present.get(
                    (symbol, date.fromisoformat(r["session"]))
                )
                is not None,
                "coverage_validated_after_recheck": provider_range_verified(
                    coverage.get(symbol),
                    date.fromisoformat(r["session"]),
                    date.fromisoformat(r["session"]),
                    feed=binding["feed"],
                    adjustment=binding["adjustment"],
                ),
            }
            for r in sorted(rows, key=lambda r: r["session"])
        ]
        previous = evidence.get(symbol, {})
        merged = (
            {r["session"]: r for r in previous.get("sessions", [])}
            if previous.get("identity_fingerprint") == digest(identities[symbol])
            else {}
        )
        for row in sessions:
            existing_roles = set(merged.get(row["session"], {}).get("roles", "").split(";")) - {""}
            merged[row["session"]] = {
                **row,
                "roles": ";".join(sorted(existing_roles | set(row["roles"].split(";")))),
            }
        sessions = [merged[key] for key in sorted(merged)]
        if any(
            r["provider_result"] == "BAR_RETURNED"
            and (
                not r["local_present_after_recheck"]
                or not r.get("coverage_validated_after_recheck", False)
            )
            for r in sessions
        ):
            repair_failed.add(symbol)
        evidence[symbol] = {
            "identity_fingerprint": digest(identities[symbol]),
            "source_manifest_fingerprint": manifest["fingerprint"],
            "sessions": sessions,
            "audit_completed": True,
            "repair_plan_completed": symbol not in repair_failed,
            "local_recheck_completed": True,
            "repair_attempted": symbol in attempted or previous.get("repair_attempted", False),
            "repair_decision": "SYNC_DAILY"
            if symbol in attempted
            else "NO_SYNC_NO_USABLE_PROVIDER_BAR",
        }
    certificate = {"scope": binding, "symbols": evidence}
    certificate["fingerprint"] = digest(certificate)
    database.set_sync_value(STORE, digest(binding), certificate)
    return {
        "certificate_fingerprint": certificate["fingerprint"],
        "provider_observations": observations,
        "repair_plan": plan,
        "repairs": repairs,
    }


def main(argv=None):
    import argparse
    from pathlib import Path

    from trading_system.cli import _synchronizer
    from trading_system.config import load_settings
    from trading_system.data.database import Database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--daily-requirements", type=Path, required=True)
    parser.add_argument("--output-stem", required=True)
    args = parser.parse_args(argv)
    if (
        not args.output_stem
        or Path(args.output_stem).name != args.output_stem
        or any(c in args.output_stem for c in "/\\:")
    ):
        raise ValueError("Output stem must be a plain filename stem")
    settings = load_settings()
    directory = settings.strategy.storage.reports_path
    if any(directory.glob(f"{args.output_stem}_*")):
        raise FileExistsError("Daily qualification audit already exists")
    database = Database(settings.strategy.storage.database_path)
    synchronizer = _synchronizer(settings, database, with_alpaca=True, with_sec=False)
    directory.mkdir(parents=True, exist_ok=True)

    def save_plan(plan):
        (directory / f"{args.output_stem}_repair_plan.json").write_text(
            json.dumps(plan, indent=2), encoding="utf-8"
        )

    result = audit_and_repair(
        database,
        json.loads(args.candidate_manifest.read_text(encoding="utf-8")),
        json.loads(args.daily_requirements.read_text(encoding="utf-8")),
        settings.strategy,
        synchronizer.alpaca,
        lambda symbol, start, end: synchronizer.sync_daily_history(
            [symbol], start, end, incremental=False, include_benchmark=False
        ),
        save_plan=save_plan,
    )
    (directory / f"{args.output_stem}_audit.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "observations": len(result["provider_observations"]),
                "repairs": len(result["repairs"]),
                "certificate_fingerprint": result["certificate_fingerprint"],
            }
        )
    )


if __name__ == "__main__":
    main()
