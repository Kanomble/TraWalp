"""Frozen pre-hardening reference workload for deterministic tests/benchmarks only."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from types import SimpleNamespace

from trading_system.config import StrategyConfig
from trading_system.data.database import Database, _bar_from_row, _iso
from trading_system.models.screening import ScreenRecord
from trading_system.models.signals import TechnicalSnapshot


def compact_record(symbol, view):
    return {
        "symbol": symbol,
        "scores": [
            getattr(view.scores, name).score
            for name in ("quality", "valuation", "opportunity", "timing")
        ],
        "technical": view.technical.model_dump(mode="json", exclude_none=True),
        "exclusion_reasons": list(view.exclusion_reasons),
    }


def replay_session(session, eligible, rejected):
    return {
        "session": session.isoformat(),
        "eligible": [record.model_dump(mode="json") for _, record in eligible],
        "rejected": rejected,
    }


def reference_replay_report(payload):
    records = [ScreenRecord.model_validate(row) for row in payload["eligible"]]
    for row in payload["rejected"]:
        records.append(
            SimpleNamespace(
                symbol=row["symbol"],
                scores=SimpleNamespace(
                    **{
                        name: SimpleNamespace(score=value)
                        for name, value in zip(
                            ("quality", "valuation", "opportunity", "timing"),
                            row["scores"],
                            strict=True,
                        )
                    }
                ),
                technical=TechnicalSnapshot.model_validate(row["technical"]),
                exclusion_reasons=tuple(row["exclusion_reasons"]),
            )
        )
    return SimpleNamespace(as_of=date.fromisoformat(payload["session"]), records=tuple(records))


def whole_database_fingerprint(database):
    """Hash actual discovery inputs, including corrections; intraday-only sync is compatible.

    One streaming read transaction, independent of candidate count. No full database
    file hash (a native 15m sync must not invalidate Daily candidate decisions).
    """
    digest = hashlib.sha256()
    with database.read_only() as connection:
        connection.execute("BEGIN")
        for table, where, order in (
            ("assets", "", "symbol"),
            ("companies", "", "symbol"),
            ("fundamental_facts", "", "id"),
            ("bars", "WHERE timeframe='1d'", "symbol,timeframe,timestamp"),
            (
                "sync_state",
                "WHERE source IN ('sec_identity_conflicts','sec_reference')",
                "source,key",
            ),
        ):
            digest.update(table.encode())
            cursor = connection.execute(f"SELECT * FROM {table} {where} ORDER BY {order}")
            while rows := cursor.fetchmany(4096):
                for row in rows:
                    digest.update(json.dumps(tuple(row), separators=(",", ":")).encode())
                    digest.update(b"\n")
    return digest.hexdigest()


class ReferenceDiscovery:
    def reference_f_candidates(self, session: date, config: StrategyConfig):
        """Same PIT cross-section and evaluator, materializing only eligible F entries."""
        from trading_system.backtest.engine import evaluate_variant_entry
        from trading_system.backtest.f_candidates import (
            FSessionCandidates,
        )
        from trading_system.backtest.research_registry import FROZEN_CHAMPION_F

        started = time.perf_counter()
        diagnostics = self.discovery_diagnostics
        prepared = [self._candidate(company, session) for company in self._companies]
        diagnostics.candidate_prepare_seconds += time.perf_counter() - started
        diagnostics.companies_processed += len(self._companies) + len(self._conflicted)
        diagnostics.prepared_candidates += len(prepared)
        peer_started = time.perf_counter()
        table = self.screener._peer_table(prepared)
        diagnostics.peer_table_seconds += time.perf_counter() - peer_started
        diagnostics.peer_table_rows += len(table)
        peers = self.screener._peer_index(table)
        eligible, rejected, screen_ranks = [], [], []
        for candidate in prepared:
            view, group, medians = self.screener._score_inputs(candidate, peers)
            diagnostics.eligible_screen_records += not view.exclusion_reasons
            if not view.exclusion_reasons:
                screen_ranks.append(
                    (
                        -(view.scores.total or 0),
                        -(view.scores.quality.score or 0),
                        -(view.scores.valuation.score or 0),
                        candidate.company.symbol,
                    )
                )
            evaluation_started = time.perf_counter()
            evaluation = evaluate_variant_entry(view, FROZEN_CHAMPION_F.variant, config)
            diagnostics.variant_f_evaluation_seconds += time.perf_counter() - evaluation_started
            if evaluation.eligible:
                record = self.screener._materialize(candidate, view, group, medians)
                eligible.append((evaluation.score, record))
            else:
                rejected.append(compact_record(candidate.company.symbol, view))
        for company in self._conflicted:
            view = self.screener._identity_conflict_inputs()
            rejected.append(compact_record(company.symbol, view))
        eligible.sort(key=lambda pair: (-pair[0], pair[1].symbol))
        ranks = {row[-1]: rank for rank, row in enumerate(sorted(screen_ranks), 1)}
        eligible = [
            (score, record.model_copy(update={"rank": ranks.get(record.symbol)}))
            for score, record in eligible
        ]
        diagnostics.eligible_f_candidates += len(eligible)
        report_started = time.perf_counter()
        result = FSessionCandidates(tuple(eligible), replay_session(session, eligible, rejected))
        diagnostics.report_construction_seconds += time.perf_counter() - report_started
        diagnostics.sessions_processed += 1
        diagnostics.screen_session_seconds += time.perf_counter() - started
        self.diagnostics.sessions_screened += 1
        return result


class ReferenceCoverage(Database):
    def iter_entry_coverage_batches(self, requirements, *, batch_size=200):
        """Two SELECTs per bounded batch of exact (symbol, signal, execution) requirements.

        VALUES joins use the existing symbol/timeframe/timestamp index. Never fetch
        a symbol's unrestricted history; missing previous-session closes stay missing.
        """
        from trading_system.data.market_sessions import regular_session_bounds

        if batch_size < 1 or batch_size > 200:
            raise ValueError("coverage batch_size must be between 1 and 200")
        unique = sorted(set(requirements))
        with self.read_only() as connection:
            connection.execute("BEGIN")
            for offset in range(0, len(unique), batch_size):
                batch = unique[offset : offset + batch_size]
                placeholders = ",".join("(?,?,?,?)" for _ in batch)
                native_parameters, daily_parameters = [], []
                native, previous = {}, {}
                for symbol, signal, execution in batch:
                    opening, closing = regular_session_bounds(execution)
                    native_parameters.extend(
                        (symbol, execution.isoformat(), _iso(opening), _iso(closing))
                    )
                    daily_parameters.extend(
                        (
                            symbol,
                            signal.isoformat(),
                            signal.isoformat(),
                            (signal + timedelta(days=1)).isoformat(),
                        )
                    )
                    native[symbol, execution] = []
                    previous[symbol, signal] = None
                native_rows = connection.execute(
                    f"""WITH requirements(symbol,session,start,end) AS (VALUES {placeholders})
                    SELECT bars.*,requirements.session AS required_session
                    FROM requirements JOIN bars ON bars.symbol=requirements.symbol
                    AND bars.timeframe='15m' AND bars.timestamp>=requirements.start
                    AND bars.timestamp<requirements.end
                    ORDER BY bars.timestamp,bars.symbol""",
                    native_parameters,
                ).fetchall()
                daily_rows = connection.execute(
                    f"""WITH requirements(symbol,session,start,end) AS (VALUES {placeholders})
                    SELECT bars.*,requirements.session AS required_session
                    FROM requirements JOIN bars ON bars.symbol=requirements.symbol
                    AND bars.timeframe='1d' AND bars.timestamp>=requirements.start
                    AND bars.timestamp<requirements.end
                    ORDER BY bars.timestamp,bars.symbol""",
                    daily_parameters,
                ).fetchall()
                for row in native_rows:
                    native[row["symbol"], date.fromisoformat(row["required_session"])].append(
                        _bar_from_row(row)
                    )
                for row in daily_rows:
                    previous[row["symbol"], date.fromisoformat(row["required_session"])] = float(
                        row["close"]
                    )
                yield native, previous, len(native_rows), len(daily_rows)
