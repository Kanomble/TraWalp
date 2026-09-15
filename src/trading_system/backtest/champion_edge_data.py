"""PIT evidence capture and a local Daily panel; no alternative trading decisions."""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime

from trading_system.backtest.candidate_audit import (
    FUNNEL_STAGES,
    _diagnostic_failure_reason,
    _failure_stage,
)
from trading_system.backtest.engine import evaluate_variant_entry
from trading_system.backtest.f_candidates import compact_record, replay_session
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.models.market_data import BarTimeframe


def ratio(value, denominator):
    return value / denominator if value is not None and denominator not in (None, 0) else None


def candidate_record(record, evaluation, rank):
    technical = record.technical
    cap_is_pit = (
        record.estimated_market_cap is not None
        and record.pit_fact_count > 0
        and record.latest_pit_filing_date is not None
        and record.latest_pit_filing_date <= record.as_of
    )
    row = {
        "population": "F_ELIGIBLE_CANDIDATES",
        "session": record.as_of.isoformat(),
        "symbol": record.symbol,
        "f_rank": rank,
        "weighted_f_score": evaluation.weighted_score,
        "quality_score": evaluation.quality_score,
        "valuation_score": evaluation.valuation_score,
        "price": technical.price,
        "drawdown_52w": technical.drawdown_52w,
        "momentum126": technical.momentum126,
        "sma50_distance": ratio(technical.price, technical.sma50),
        "sma200_distance": ratio(technical.price, technical.sma200),
        "sma20_rising": technical.sma20_rising,
        "atr_price": ratio(technical.atr14, technical.price),
        "sic": record.sic,
        "sector": (record.sic or "UNKNOWN")[:2] if record.sic else "UNKNOWN",
        "sector_basis": "CURRENT_LOCAL_SIC_2_DIGIT_NOT_PIT",
        "pit_market_cap": record.estimated_market_cap if cap_is_pit else None,
        "latest_pit_filing_date": str(record.latest_pit_filing_date)
        if record.latest_pit_filing_date
        else None,
        "selection_outcome": None,
        "selection_reason": None,
        "execution_session": None,
        "entry_executed": False,
        "execution_reason": None,
    }
    for field in ("sma50_distance", "sma200_distance"):
        if row[field] is not None:
            row[field] -= 1
    for component in (record.scores.quality, record.scores.valuation):
        for factor in component.factors:
            row[f"component_{factor.name}"] = factor.score
    return row


class EdgeCollector:
    """Read callbacks only; immutable screen evidence and copied position values."""

    def __init__(self, config):
        self.config = config
        self.funnel = {}
        self.gate_failures = []
        self.candidates = {}
        self.context = {}
        self.blocked = []
        self.seen_symbols = set()

    def prepare_screen(self, report):
        if report.as_of in self.funnel:
            raise ValueError("Duplicate PIT screen session")
        stage_failures, first, all_reasons, gate_first, gate_all = (Counter() for _ in range(5))
        eligible, rejected = [], []
        omitted = Counter()
        gate_passed = 0
        for record in report.records:
            if (
                record.as_of != report.as_of
                or (
                    record.technical.market_session is not None
                    and record.technical.market_session > report.as_of
                )
                or (
                    record.latest_pit_filing_date is not None
                    and record.latest_pit_filing_date > report.as_of
                )
            ):
                raise ValueError("CHAMPION_EDGE_NON_PIT_SCREEN")
            evaluation = evaluate_variant_entry(record, FROZEN_CHAMPION_F.variant, self.config)
            reason = _diagnostic_failure_reason(record, evaluation, self.config)
            if reason:
                stage_failures[_failure_stage(reason)] += 1
                first[reason] += 1
            blockers = evaluation.variant_blocking_reasons
            gate_passed += not blockers
            if blockers:
                gate_first[blockers[0]] += 1
                gate_all.update(set(blockers))
            all_reasons.update(
                set(evaluation.blocking_reasons) | set(blockers) | ({reason} if reason else set())
            )
            if evaluation.eligible:
                eligible.append((evaluation.score, record, evaluation))
                self.seen_symbols.add(record.symbol)
            elif record.symbol in self.seen_symbols:
                rejected.append(compact_record(record.symbol, record))
            else:
                omitted[evaluation.first_failure] += 1
        eligible.sort(key=lambda item: (-item[0], item[1].symbol))
        for rank, (_, record, evaluation) in enumerate(eligible, 1):
            self.candidates[(report.as_of, record.symbol)] = candidate_record(
                record, evaluation, rank
            )
        count = len(report.records)
        row = {
            "population": "ALL_SCREENED_INSTRUMENTS",
            "session": str(report.as_of),
            "tradable_current_universe": count,
        }
        for stage, label in FUNNEL_STAGES:
            row[f"reached_{stage}"] = count
            count -= stage_failures[stage]
            row[label] = count
        if count != len(eligible):
            raise ValueError("CHAMPION_EDGE_FUNNEL_DOES_NOT_RECONCILE")
        row.update(
            history_data_quality_eligible=row["valid_price"],
            fundamental_universe_eligible=row["positive_operating_cash_flow_pass"],
            f_technical_gate_evaluated=len(report.records),
            f_technical_gate_passed=gate_passed,
            f_technical_gate_reached=row["reached_variant_gate"],
            f_final_eligible=count,
            ranked_f_candidates=count,
            entry_blocked_by_occupied_capacity=0,
            entry_capacity_reserved=0,
            entry_executed=0,
        )
        self.funnel[report.as_of] = row
        for scope, counts in (
            ("FIRST_FAILURE", first),
            ("ALL_OBSERVABLE_BLOCKING_REASONS", all_reasons),
            ("TECHNICAL_FIRST_FAILURE", gate_first),
            ("TECHNICAL_ALL_FAILURES", gate_all),
        ):
            for reason, count in sorted(counts.items()):
                self.gate_failures.append(
                    {
                        "population": "ALL_SCREENED_INSTRUMENTS",
                        "session": str(report.as_of),
                        "scope": scope,
                        "reason": reason,
                        "count": count,
                        "missing_required_technical_data": reason.startswith("qv_momentum_")
                        and reason.endswith("unavailable"),
                    }
                )
        return replay_session(
            report.as_of, [(score, record) for score, record, _ in eligible], rejected, omitted
        )

    def observe_screen(self, report, variant, config):
        # Engine reads the same already-audited compact PIT screen once, without rescoring sources.
        if report.as_of not in self.funnel or variant is not FROZEN_CHAMPION_F.variant:
            raise ValueError("CHAMPION_EDGE_UNPREPARED_SCREEN")

    def observe_entry_context(self, report, positions, execution_session):
        self.context[report.as_of] = {
            "execution_session": execution_session,
            "positions": tuple(
                {
                    "symbol": p.symbol,
                    "position_id": p.position_id,
                    "entry_session": str(p.entry_date),
                    "holding_day": p.holding_days,
                    "unrealized_return": p.last_price / p.entry_price - 1,
                    "sector": p.sector,
                }
                for p in positions.values()
            ),
        }

    def observe_portfolio_decision(self, signal_date, symbol, outcome, reason=None):
        row = self.candidates[(signal_date, symbol)]
        row.update(selection_outcome=outcome, selection_reason=reason)
        context = self.context[signal_date]
        row["execution_session"] = str(context["execution_session"])
        if reason != "max_positions_reached":
            return
        held = context["positions"]
        if not held:
            self.funnel[signal_date]["entry_capacity_reserved"] += 1
            return  # selected orders are not an already occupied champion slot
        if (
            sum(p["sector"] == (row["sic"] or "unknown")[:2] for p in held)
            >= self.config.portfolio.max_sector_positions
        ):
            row["additional_observable_selection_block"] = "sector_limit"
            return  # not prevented solely by capacity
        position = held[0]
        self.funnel[signal_date]["entry_blocked_by_occupied_capacity"] += 1
        self.blocked.append(
            {
                "session": str(signal_date),
                "symbol": symbol,
                "blocking_held_symbol": position["symbol"],
                "held_position_id": position["position_id"],
                "held_entry_session": position["entry_session"],
                "held_current_holding_day": position["holding_day"],
                "held_unrealized_return": position["unrealized_return"],
                "interpretation": "EX_POST_DIAGNOSTIC_ONLY",
            }
        )

    def observe_execution(self, signal_date, execution_date, symbol, executed, reason=None):
        row = self.candidates[(signal_date, symbol)]
        row.update(
            entry_executed=executed, execution_reason=reason, execution_session=str(execution_date)
        )
        self.funnel[signal_date]["entry_executed"] += executed


class LocalDailyPanel:
    """Prepared Daily-only engine facade. Unknown methods cannot fall back to SQL."""

    def __init__(self, bars, sessions, bounds, companies=(), conflicts=()):
        grouped = defaultdict(list)
        for bar in bars:
            grouped[bar.symbol].append(bar)
        self.bars = {
            symbol: sorted(rows, key=lambda b: b.timestamp) for symbol, rows in grouped.items()
        }
        self.days = {
            symbol: [b.timestamp.date() for b in rows] for symbol, rows in self.bars.items()
        }
        self.by_day = {
            symbol: dict(zip(self.days[symbol], rows, strict=True))
            for symbol, rows in self.bars.items()
        }
        self.sessions, self.bounds = sessions, bounds
        self.companies, self.conflicts = companies, conflicts

    def bar_sessions(self, start, end):
        return [day for day in self.sessions if start <= day <= end]

    def bar_date_bounds(self):
        return self.bounds

    def list_tradable_companies(self):
        return self.companies

    def unresolved_sec_identity_conflict_symbols(self):
        return self.conflicts

    def bars_on_session(self, symbols, session):
        return {s: self.by_day[s][session] for s in symbols if session in self.by_day.get(s, {})}

    def bars_available_as_of(self, symbol, as_of, *, timeframe=BarTimeframe.DAY_1, limit=None):
        if BarTimeframe(timeframe) is not BarTimeframe.DAY_1:
            raise ValueError("CHAMPION_EDGE_REQUIRES_DAILY")
        days = self.days.get(symbol, [])
        end = (
            bisect_left(days, as_of.date())
            if isinstance(as_of, datetime)
            else bisect_right(days, as_of)
        )
        return self.bars.get(symbol, [])[max(0, end - limit) if limit is not None else 0 : end]

    def forward_returns(self, symbol, session, official_sessions, horizons):
        """Next represented portfolio session open; official-session horizon endpoints."""
        offset = bisect_right(self.sessions, session)
        entry = self.sessions[offset] if offset < len(self.sessions) else None
        opening = self.by_day.get(symbol, {}).get(entry)
        output = {
            "forward_entry_session": str(entry) if entry else None,
            "forward_entry_reference": float(opening.open) if opening else None,
        }
        for horizon in horizons:
            target = None
            if entry is not None:
                index = bisect_left(official_sessions, entry) + horizon - 1
                if index < len(official_sessions):
                    target = official_sessions[index]
            closing = self.by_day.get(symbol, {}).get(target)
            output[f"forward_return_{horizon}d"] = (
                float(closing.close / opening.open - 1) if opening and closing else None
            )
            output[f"forward_exit_session_{horizon}d"] = str(target) if target else None
        return output
