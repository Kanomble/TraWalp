"""Local-only historical-universe provenance diagnostics."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from trading_system.backtest.report import _atomic_text
from trading_system.data.database import Database
from trading_system.models.market_data import BarTimeframe

CURRENT_UNIVERSE_ONLY = "CURRENT_UNIVERSE_ONLY"
NOT_SURVIVORSHIP_CLEAN = "NOT_SURVIVORSHIP_CLEAN"


def audit_universe_provenance(
    database: Database,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Quantify local evidence without treating bars or SEC identity as membership."""

    if start > end:
        raise ValueError("Universe audit start must not be after end")
    with database.read_only() as connection:
        current_assets = _count(
            connection, "SELECT COUNT(*) FROM assets WHERE tradable=1"
        )
        current_companies = _count(
            connection,
            """SELECT COUNT(*) FROM companies c JOIN assets a ON a.symbol=c.symbol
            WHERE a.tradable=1""",
        )
        inactive_assets = _count(
            connection, "SELECT COUNT(*) FROM assets WHERE tradable=0"
        )
        persisted_companies = _count(connection, "SELECT COUNT(*) FROM companies")
        parameters = (BarTimeframe.DAY_1.value, start.isoformat(), end.isoformat())
        assets_with_bar_evidence = _count(
            connection,
            """SELECT COUNT(DISTINCT b.symbol) FROM bars b JOIN assets a ON a.symbol=b.symbol
            WHERE b.timeframe=? AND substr(b.timestamp,1,10) BETWEEN ? AND ?""",
            parameters,
        )
        current_with_bar_evidence = _count(
            connection,
            """SELECT COUNT(DISTINCT b.symbol) FROM bars b JOIN assets a ON a.symbol=b.symbol
            WHERE a.tradable=1 AND b.timeframe=?
            AND substr(b.timestamp,1,10) BETWEEN ? AND ?""",
            parameters,
        )
        inactive_with_bar_evidence = _count(
            connection,
            """SELECT COUNT(DISTINCT b.symbol) FROM bars b JOIN assets a ON a.symbol=b.symbol
            WHERE a.tradable=0 AND b.timeframe=?
            AND substr(b.timestamp,1,10) BETWEEN ? AND ?""",
            parameters,
        )
        bar_only_symbols = _count(
            connection,
            """SELECT COUNT(DISTINCT b.symbol) FROM bars b LEFT JOIN assets a ON a.symbol=b.symbol
            WHERE a.symbol IS NULL AND b.timeframe=?
            AND substr(b.timestamp,1,10) BETWEEN ? AND ?""",
            parameters,
        )
        conflict_rows = connection.execute(
            "SELECT value FROM sync_state WHERE source='sec_identity_conflicts'"
        ).fetchall()
    unresolved_conflicts = sum(_is_unresolved(row[0]) for row in conflict_rows)
    return {
        "report_type": "historical_universe_provenance_audit",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "local_only": True,
        "universe_provenance": CURRENT_UNIVERSE_ONLY,
        "survivorship_status": NOT_SURVIVORSHIP_CLEAN,
        "survivorship_clean": False,
        "historical_membership_source": "current Alpaca active/tradable asset snapshot",
        "historical_membership_authoritative": False,
        "delisted_assets_supported": False,
        "symbol_history_supported": False,
        "pit_membership_coverage": "UNSUPPORTED",
        "counts": {
            "current_tradable_assets": current_assets,
            "current_tradable_companies_used_by_backtests": current_companies,
            "persisted_company_identities": persisted_companies,
            "inactive_assets_known_locally": inactive_assets,
            "inactive_assets_with_daily_bar_evidence_in_period": inactive_with_bar_evidence,
            "assets_with_daily_bar_evidence_in_period": assets_with_bar_evidence,
            "current_assets_with_daily_bar_evidence_in_period": current_with_bar_evidence,
            "bar_symbols_without_asset_record_in_period": bar_only_symbols,
            "symbols_with_only_inferred_history": assets_with_bar_evidence,
            "symbols_with_bars_before_authoritative_first_seen": None,
            "delisted_assets_known_locally": None,
            "symbols_with_identity_transitions": None,
            "symbols_with_authoritative_first_membership_date": 0,
            "symbols_with_authoritative_last_membership_date": 0,
            "unresolved_sec_identity_conflicts": unresolved_conflicts,
        },
        "unsupported_fields": {
            "symbols_with_bars_before_authoritative_first_seen": (
                "assets.updated_at is the latest reconciliation time, not first_seen"
            ),
            "delisted_assets_known_locally": (
                "tradable=0 does not distinguish delisting from inactive or non-tradable status"
            ),
            "symbols_with_identity_transitions": (
                "companies stores one current symbol per CIK and has no ticker-history table"
            ),
        },
        "historical_membership_sources": [
            "current Alpaca active/tradable snapshot (current-state authority only)",
            "Daily bar availability (non-authoritative historical evidence)",
            "SEC CIK-symbol mapping (company identity only; not universe membership)",
        ],
        "methodology_note": (
            "Historical screens begin with the currently tradable, SEC-identified company set. "
            "Bar availability is reported only as evidence and is never interpreted as listing, "
            "delisting, or historical universe membership. True PIT reconstruction requires a "
            "separate authoritative asset-lifecycle dataset."
        ),
    }


def export_universe_provenance_audit(
    report: dict[str, Any],
    output_directory: Path,
    *,
    stem: str,
) -> Path:
    output_directory.mkdir(parents=True, exist_ok=True)
    path = output_directory / f"{stem}_universe_provenance.json"
    if path.exists():
        raise FileExistsError(f"Universe provenance audit already exists: {path}")
    _atomic_text(path, json.dumps(report, indent=2))
    return path


def _count(connection, query: str, parameters: tuple[str, ...] = ()) -> int:
    return int(connection.execute(query, parameters).fetchone()[0])


def _is_unresolved(raw_value: str | None) -> bool:
    try:
        value = json.loads(raw_value or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(value, dict) and value.get("status") == "unresolved"
