"""Local, read-only coverage diagnosis of unchanged F/configured/C1 in 2022.

This module never runs a portfolio or a synchronizer. Only new audit files are written.
Run with the project's interpreter: -m trading_system.backtest.f_data_gap_audit.
"""

from __future__ import annotations

import csv
import gc
import hashlib
import importlib
import json
import math
import socket
import sqlite3
import subprocess
import sys
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from statistics import mean, median
from unittest.mock import patch

import numpy as np

from trading_system.backtest.engine import entry_blocking_reasons, evaluate_variant_entry
from trading_system.backtest.features import HistoricalFeatureScreenSource, _eligible_fact_count
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F, validate_champion_config
from trading_system.backtest.screen_strategies import evaluate_strategy_gate
from trading_system.config import StrategyConfig, _read_yaml
from trading_system.data.database import Database
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.data.xbrl_parser import TAG_ALIASES, parse_company_facts
from trading_system.fundamentals.metrics import discrete_quarters, invested_capital
from trading_system.fundamentals.peers import normalize_sic, relative_multiple
from trading_system.fundamentals.quality import accounting_state_as_of

ROOT = Path(__file__).resolve().parents[3]
START, END = date(2022, 1, 3), date(2022, 12, 30)
STEM = "f_2022_data_gap_audit_v1"
RAW = "RAW_SEC_DATA_MISSING"
DERIVED = "RAW_SEC_DATA_PRESENT_BUT_METRIC_UNAVAILABLE"
ECONOMIC = "ECONOMICALLY_UNDEFINED"
PEER = "PEER_CONTEXT_INSUFFICIENT"
IDENTITY = "IDENTITY_CLASSIFICATION_GAP"
UNKNOWN = "UNKNOWN_DIAGNOSTIC_GAP"
CATEGORIES = (RAW, DERIVED, ECONOMIC, PEER, IDENTITY, UNKNOWN)
QUALITY = ("revenue_growth", "eps_growth", "operating_cash_flow", "operating_margin",
           "roic", "debt_to_ebitda")
VALUATION = ("relative_pe", "relative_ev_ebitda", "fcf_yield")
FACTOR_COLUMNS = dict(zip(CATEGORIES, (
    "raw_sec_missing", "raw_present_metric_unavailable", "economically_undefined",
    "peer_context_insufficient", "identity_gap", "unknown"), strict=True))
TECHNICAL_FIELDS = ("price", "sma50", "sma200", "sma20_rising", "momentum126", "drawdown_52w")

# Diagnostic concept evidence only. These concepts NEVER feed production metrics
# or scores; their presence proves that a download-absence claim is too strong.
DIAGNOSTIC_RAW_CONCEPTS = {
    "revenue": ("Revenue",),
    "operating_income": ("ProfitLossFromOperatingActivities",),
    "net_income": ("ProfitLoss",),
    "eps_diluted": ("DilutedEarningsLossPerShare", "BasicAndDilutedEarningsLossPerShare"),
    "operating_cash_flow": ("CashFlowsFromUsedInOperatingActivities",),
    "capital_expenditures": ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",),
    "cash": ("CashAndCashEquivalents",),
    "total_equity": ("Equity", "EquityAttributableToOwnersOfParent"),
    "shares_outstanding": ("NumberOfSharesOutstanding",),
    "tax_expense": ("IncomeTaxExpenseContinuingOperations", "IncomeTaxExpenseBenefit"),
    "interest_expense": ("InterestExpense", "FinanceCosts"),
    "depreciation": ("DepreciationExpense",),
    "amortization": ("AmortisationExpense",),
    "depreciation_amortization": ("AdjustmentsForDepreciationAndAmortisationExpense",),
}


@dataclass
class LocalRawEvidence:
    facts: list
    metric_first: dict
    unsupported_first: date | None
    taxonomy_pit_counts: dict


def raw_payload_evidence(payload, symbol):
    aliases = defaultdict(set)
    for metric, concepts in TAG_ALIASES.items():
        for taxonomy, tag in concepts:
            aliases[(taxonomy, tag)].add(metric)
    for metric, tags in DIAGNOSTIC_RAW_CONCEPTS.items():
        for tag in tags:
            aliases[("ifrs-full", tag)].add(metric)
    first, taxonomy_counts, unsupported = {}, Counter(), []
    for taxonomy, concepts in payload.get("facts", {}).items():
        for tag, concept in concepts.items():
            for observations in concept.get("units", {}).values():
                for row in observations:
                    try:
                        filed = date.fromisoformat(row.get("filed", ""))
                    except ValueError:
                        continue
                    if filed > END:
                        continue
                    taxonomy_counts[taxonomy] += 1
                    if taxonomy == "ifrs-full":
                        unsupported.append(filed)
                    for metric in aliases[(taxonomy, tag)]:
                        first[metric] = min(first.get(metric, filed), filed)
    return LocalRawEvidence([f for f in parse_company_facts(payload, symbol) if f.filed <= END],
                            first, min(unsupported, default=None), dict(taxonomy_counts))


class AuditDatabase(Database):
    """Every inherited read, including legacy connect(), uses SQLite mode=ro."""

    def connect(self):
        return self.read_only()

    def initialize(self):
        raise RuntimeError("AUDIT_DATABASE_INITIALIZE_FORBIDDEN")

    def bar_sessions(self, start, end):
        # Same stored Daily sessions, with an indexable timestamp bound instead of
        # applying substr() to the entire multi-timeframe historical inventory.
        with self.read_only() as connection:
            return [date.fromisoformat(r[0]) for r in connection.execute(
                "SELECT DISTINCT substr(timestamp,1,10) FROM bars WHERE timeframe='1d' "
                "AND timestamp>=? AND timestamp<? ORDER BY 1",
                (start.isoformat(), (end + timedelta(days=1)).isoformat()))]

    def iter_bar_value_batches(self, *args, **kwargs):
        for index, batch in enumerate(super().iter_bar_value_batches(*args, **kwargs)):
            if index % 15 == 0:
                print(f"  Production bar batch {index + 1}", flush=True)
            yield batch

    def iter_fact_batches(self, *args, **kwargs):
        for index, batch in enumerate(super().iter_fact_batches(*args, **kwargs)):
            if index % 5 == 0:
                print(f"  Production fact batch {index + 1}", flush=True)
            yield batch


@contextmanager
def local_guard():
    """Fail closed before network transport, client construction or SQLite mutation."""
    import requests

    counters = dict.fromkeys(("network_calls", "provider_calls", "database_writes",
                             "strategy_changes", "blocked_network_attempts",
                             "blocked_database_writes", "blocked_provider_constructions"), 0)
    original = sqlite3.connect
    write_actions = {getattr(sqlite3, name) for name in (
        "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_ALTER_TABLE",
        "SQLITE_CREATE_TABLE", "SQLITE_DROP_TABLE", "SQLITE_CREATE_INDEX", "SQLITE_DROP_INDEX",
        "SQLITE_CREATE_TRIGGER", "SQLITE_DROP_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DROP_VIEW",
        "SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE", "SQLITE_ATTACH", "SQLITE_DETACH",
        "SQLITE_REINDEX", "SQLITE_ANALYZE")}

    def authorize(action, arg1, arg2, db, trigger):
        # Read-only PRAGMA arguments such as table_info(table) are allowed explicitly.
        if action in write_actions or (action == sqlite3.SQLITE_PRAGMA and
                (str(arg1).lower() not in {"query_only", "table_info", "index_list",
                 "index_info", "page_count", "page_size", "freelist_count", "data_version"}
                 or (str(arg1).lower() == "query_only" and arg2 not in (None, "ON", "1")))):
            counters["blocked_database_writes"] += 1
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect(database, *args, **kwargs):
        if not kwargs.get("uri") or "mode=ro" not in str(database):
            counters["blocked_database_writes"] += 1
            raise RuntimeError("AUDIT_REQUIRES_MODE_RO")
        connection = original(database, *args, **kwargs)
        connection.execute("PRAGMA query_only=ON")
        connection.set_authorizer(authorize)
        return connection

    def deny_network(*args, **kwargs):
        counters["blocked_network_attempts"] += 1
        raise RuntimeError("AUDIT_NETWORK_FORBIDDEN")

    def deny_client(*args, **kwargs):
        counters["blocked_provider_constructions"] += 1
        raise RuntimeError("AUDIT_PROVIDER_CONSTRUCTION_FORBIDDEN")

    with ExitStack() as stack:
        stack.enter_context(patch.object(sqlite3, "connect", connect))
        for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                             (socket.socket, "sendto"), (socket, "create_connection"),
                             (socket, "getaddrinfo"), (requests.sessions.Session, "request")):
            stack.enter_context(patch.object(target, name, deny_network))
        # Imports are local source inspection; constructors are blocked, never invoked.
        for module_name in ("sec_client", "alpaca_client", "massive_reference",
                            "massive_identity_research", "openfigi_research",
                            "alpaca_corporate_actions"):
            module = importlib.import_module("trading_system.data." + module_name)
            for name, cls in vars(module).items():
                if (isinstance(cls, type) and "Client" in name and
                        cls.__module__ == module.__name__):
                    stack.enter_context(patch.object(cls, "__init__", deny_client))
        yield counters


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def batches(values, size=80):
    values = sorted(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


@dataclass(frozen=True)
class Issue:
    category: str
    detail: str
    inputs: tuple[str, ...] = ()


def unique_issues(issues):
    return tuple(sorted(set(issues), key=lambda item: (item.category, item.detail, item.inputs)))


class AccountingEvidence:
    """Diagnostics of existing accounting results; never supplies replacement values.

    A missing input means no observation for the *mapped* SEC concept filed by T,
    checked against the full normalized inventory and any surviving raw cache. A
    present concept with unusable periods is conservatively a construction gap.
    """

    def __init__(self, facts, as_of, state=None, metric_first=None, cache_facts=(), raw_evidence=None):
        self.as_of = as_of
        self.facts = [f for f in facts if f.filed <= as_of]
        self.state = state or accounting_state_as_of(self.facts, as_of)
        self.metric_first = metric_first or {}
        self.raw_evidence = raw_evidence
        self.cache_facts = [f for f in cache_facts if f.filed <= as_of]
        self.by_metric = defaultdict(list)
        for fact in [*self.facts, *self.cache_facts]:
            self.by_metric[fact.metric].append(fact)

    def present(self, metric):
        return bool(self.by_metric[metric]) or self.metric_first.get(metric, date.max) <= self.as_of

    def absent(self, metric):
        if not self.present(metric):
            if self.raw_evidence and self.raw_evidence.metric_first.get(metric, date.max) <= self.as_of:
                return [Issue(DERIVED, "local_raw_concept_present_but_production_mapping_unavailable", (metric,))]
            if (self.raw_evidence and self.raw_evidence.unsupported_first is not None
                    and self.raw_evidence.unsupported_first <= self.as_of):
                return [Issue(UNKNOWN, "unsupported_local_taxonomy_input_semantics_unresolved", (metric,))]
            return [Issue(RAW, "no_local_mapped_PIT_observation", (metric,))]
        return [Issue(DERIVED, "mapped_PIT_input_present_but_selected_value_unavailable", (metric,))]

    @cache
    def flow(self, metric, *, prior=False):
        selected = self.state.prior if prior else self.state.current
        if getattr(selected, metric) is not None:
            return []
        if not self.present(metric):
            return self.absent(metric)
        # Reuse canonical quarter reconstruction to substantiate why TTM is absent.
        rows = self.by_metric[metric]
        cutoff = (self.state.current.metric_period_ends.get(metric) - timedelta(days=300)
                  if prior and metric in self.state.current.metric_period_ends else None)
        if cutoff is not None and not any(f.period_end <= cutoff for f in rows):
            return [Issue(RAW, "no_local_PIT_comparative_period_observation", (metric,))]
        quarters = discrete_quarters(rows, metric, self.as_of)
        if cutoff:
            quarters = [q for q in quarters if q.period_end <= cutoff]
        if not quarters:
            detail = "no_discrete_or_reconstructable_quarters"
        else:
            same_unit = [q for q in quarters if q.unit == quarters[-1].unit]
            if len(same_unit) < 4:
                detail = "fewer_than_four_reconstructed_same_unit_quarters"
            else:
                gaps = [(b.period_end - a.period_end).days
                        for a, b in zip(same_unit[-4:-1], same_unit[-3:], strict=True)]
                detail = ("reconstructed_quarter_spacing_outside_60_130_days"
                          if any(g < 60 or g > 130 for g in gaps)
                          else "bounded_source_or_period_selection_difference")
        return [Issue(DERIVED, ("prior_" if prior else "current_") + detail, (metric,))]

    @cache
    def balance(self, metric, *, prior=False):
        selected = self.state.prior_balance if prior else self.state.balance
        if getattr(selected, metric) is not None:
            return []
        if metric == "total_debt":
            # Both existing production paths: a direct total OR aligned debt parts.
            if not any(self.present(m) for m in ("total_debt", "debt_current", "debt_noncurrent")):
                if (self.raw_evidence and self.raw_evidence.unsupported_first is not None
                        and self.raw_evidence.unsupported_first <= self.as_of):
                    return [Issue(UNKNOWN, "unsupported_local_taxonomy_debt_presentation_unresolved",
                                  ("total_debt|debt_current+debt_noncurrent",))]
                return [Issue(RAW, "no_local_PIT_direct_or_component_debt", (
                    "total_debt|debt_current+debt_noncurrent",))]
            if not self.present("total_debt"):
                missing_parts = [m for m in ("debt_current", "debt_noncurrent") if not self.present(m)]
                if missing_parts:
                    return sum((self.absent(m) for m in missing_parts), [])
            return [Issue(DERIVED, "debt_inputs_present_but_direct_or_aligned_pair_unavailable",
                          ("total_debt|debt_current+debt_noncurrent",))]
        if metric == "shares_outstanding" and self.present(metric):
            rows = self.by_metric[metric]
            if rows and not any(f.value > 0 for f in rows):
                return [Issue(ECONOMIC, "non_positive_share_counts", (metric,))]
            return [Issue(DERIVED, "share_selection_unit_age_460_days_or_period_cutoff", (metric,))]
        if prior and self.present(metric) and self.state.balance.period_end:
            cutoff = self.state.balance.period_end - timedelta(days=300)
            if not any(f.period_end <= cutoff for f in self.by_metric[metric]):
                return [Issue(RAW, "no_local_PIT_prior_balance_observation", (metric,))]
        return self.absent(metric)

    @cache
    def da(self):
        c = self.state.current
        if c.depreciation_amortization is not None:
            return []
        if not any(self.present(m) for m in (
                "depreciation_amortization", "depreciation", "amortization")):
            if self.raw_evidence and self.raw_evidence.metric_first.get("depreciation_amortization", date.max) <= self.as_of:
                return [Issue(DERIVED, "local_raw_DA_concept_present_but_mapping_unavailable", ("depreciation_amortization",))]
            if (self.raw_evidence and self.raw_evidence.unsupported_first is not None
                    and self.raw_evidence.unsupported_first <= self.as_of):
                return [Issue(UNKNOWN, "unsupported_local_taxonomy_DA_presentation_unresolved", ("depreciation_amortization",))]
            return [Issue(RAW, "no_local_PIT_combined_or_split_DA", (
                "depreciation_amortization|depreciation+amortization",))]
        result = []
        if self.present("depreciation_amortization"):
            result += self.flow("depreciation_amortization")
        else:
            result += self.flow("depreciation") + self.flow("amortization")
        if not result:
            result = [Issue(DERIVED, "split_DA_period_alignment", ("depreciation", "amortization"))]
        return result

    @cache
    def ebitda(self):
        c = self.state.current
        if c.ebitda is not None:
            return []
        # Show both supported branches. Missing bridge facts are not independently
        # download candidates: they can be irrelevant when the EBIT branch works.
        primary = self.flow("operating_income") + self.da()
        bridge = sum((self.flow(m) for m in ("net_income", "interest_expense", "tax_expense")), [])
        if not primary:
            return [Issue(DERIVED, "EBIT_DA_period_alignment", ("operating_income", "depreciation_amortization"))]
        if not bridge and not self.da():
            return [Issue(DERIVED, "net_income_bridge_period_alignment", (
                "net_income", "interest_expense", "tax_expense", "depreciation_amortization"))]
        return primary

    @cache
    def quality(self, factor):
        c, p, b, pb = (self.state.current, self.state.prior,
                       self.state.balance, self.state.prior_balance)
        if factor in {"revenue_growth", "eps_growth"}:
            metric = "revenue" if factor == "revenue_growth" else "eps_diluted"
            result = self.flow(metric) + self.flow(metric, prior=True)
            if getattr(p, metric) is not None and getattr(p, metric) <= 0:
                result += [Issue(ECONOMIC, "non_positive_comparison_base", (metric,))]
            return unique_issues(result)
        if factor == "operating_cash_flow":
            # Negative OCF scores zero; positive OCF without growth scores 50.
            return unique_issues(self.flow("operating_cash_flow"))
        if factor == "operating_margin":
            result = self.flow("operating_income") + self.flow("revenue")
            if c.revenue == 0:
                result += [Issue(ECONOMIC, "zero_revenue_denominator", ("revenue",))]
            return unique_issues(result)
        if factor == "debt_to_ebitda":
            result = self.balance("total_debt") + self.ebitda()
            if c.ebitda is not None and c.ebitda <= 0:
                result += [Issue(ECONOMIC, "non_positive_EBITDA_denominator", ("ebitda",))]
            return unique_issues(result)
        if factor == "roic":
            result = self.flow("operating_income") + self.flow("tax_expense") + self.flow("net_income")
            for metric in ("total_debt", "total_equity", "cash"):
                result += self.balance(metric) + self.balance(metric, prior=True)
            if c.tax_expense is not None and c.net_income is not None:
                pretax = c.tax_expense + c.net_income
                if pretax <= 0 or not 0 <= c.tax_expense / pretax <= 1:
                    result += [Issue(ECONOMIC, "effective_tax_rate_invalid", ("tax_expense", "net_income"))]
            capital, prior_capital = invested_capital(b), invested_capital(pb)
            if capital is not None and prior_capital is not None and (capital + prior_capital) / 2 <= 0:
                result += [Issue(ECONOMIC, "non_positive_average_invested_capital", ("invested_capital",))]
            return unique_issues(result)
        raise ValueError(factor)

    def valuation_input(self, factor, metrics):
        c = self.state.current
        if factor == "pe":
            result = self.flow("eps_diluted")
            if c.eps_diluted is not None and c.eps_diluted <= 0:
                result += [Issue(ECONOMIC, "non_positive_EPS_denominator", ("eps_diluted",))]
        elif factor in {"ev_to_ebitda", "ev_to_ebit"}:
            result = self.balance("shares_outstanding") + self.balance("total_debt") + self.balance("cash")
            metric = "ebitda" if factor == "ev_to_ebitda" else "ebit"
            result += self.ebitda() if metric == "ebitda" else self.flow("operating_income")
            value = getattr(c, metric)
            if value is not None and value <= 0:
                result += [Issue(ECONOMIC, "non_positive_" + metric.upper() + "_denominator", (metric,))]
            multiple = getattr(metrics, factor)
            if multiple is not None and multiple <= 0:
                result += [Issue(ECONOMIC, "non_positive_company_EV_multiple", (factor,))]
        elif factor == "fcf_yield":
            result = (self.flow("operating_cash_flow") + self.flow("capital_expenditures")
                      + self.balance("shares_outstanding"))
            if c.free_cash_flow is None and c.operating_cash_flow is not None and c.capital_expenditures is not None:
                result += [Issue(DERIVED, "OCF_capex_TTM_period_alignment", ("operating_cash_flow", "capital_expenditures"))]
            if metrics.market_cap is not None and metrics.market_cap <= 0:
                result += [Issue(ECONOMIC, "non_positive_market_cap", ("shares_outstanding",))]
        else:
            raise ValueError(factor)
        return unique_issues(result)


def diagnose_factors(view, candidate, group, medians, evidence):
    """Return actual factor availability plus all evidenced independent deficits."""
    metrics = candidate.fundamentals
    result = {}
    for score in (view.scores.quality, view.scores.valuation):
        for factor in score.factors:
            issues, reasons = [], []
            if factor.score is None:
                if score.name == "quality":
                    issues = list(evidence.quality(factor.name))
                    reasons = [factor.name.upper() + "_UNAVAILABLE"]
                elif factor.name == "fcf_yield":
                    issues = list(evidence.valuation_input("fcf_yield", metrics))
                    reasons = ["FCF_YIELD_UNAVAILABLE"]
                    if metrics.market_cap is None:
                        reasons.append("MARKET_CAP_INPUT_UNAVAILABLE")
                else:
                    names = ("pe",) if factor.name == "relative_pe" else ("ev_to_ebitda", "ev_to_ebit")
                    for name in names:
                        value = getattr(metrics, name)
                        prefix = {"pe": "PE", "ev_to_ebitda": "EV_EBITDA", "ev_to_ebit": "EV_EBIT"}[name]
                        if value is None or value <= 0:
                            issues += list(evidence.valuation_input(name, metrics))
                            reasons.append(prefix + "_UNAVAILABLE")
                            if name == "pe" and evidence.state.current.eps_diluted is not None and evidence.state.current.eps_diluted <= 0:
                                reasons.append("PE_INVALID_DENOMINATOR")
                        if medians.get(name) is None or medians[name] <= 0:
                            reasons.append("PEER_PE_MEDIAN_UNAVAILABLE" if name == "pe" else "PEER_EV_MEDIAN_UNAVAILABLE")
                            issues.append(Issue(IDENTITY if normalize_sic(candidate.company.sic) is None else PEER,
                                                "SIC_unavailable" if normalize_sic(candidate.company.sic) is None
                                                else "fewer_than_configured_positive_multiples_in_selected_group", (name,)))
                    if factor.name == "relative_ev_ebitda":
                        reasons.append("EV_MULTIPLE_FALLBACK_UNAVAILABLE")
                if not issues:
                    issues = [Issue(UNKNOWN, "factor_unavailable_without_resolved_evidence")]
            result[factor.name] = {"available": factor.score is not None,
                                   "issues": unique_issues(issues), "reasons": tuple(sorted(set(reasons)))}
    return result


def aggregate_sessions(rows):
    result = {}
    for key in rows[0] if rows else ():
        if key == "session":
            continue
        values = [row[key] for row in rows]
        result[key] = {"mean": mean(values), "median": median(values),
                       "minimum": min(values), "maximum": max(values),
                       **{f"p{q}": float(np.percentile(values, q)) for q in (10, 25, 75, 90)}}
    return result


def dominant(counter):
    return sorted(counter, key=lambda key: (-counter[key], key))[0] if counter else "NONE"


def write_json(out, suffix, value):
    with (out / f"{STEM}_{suffix}.json").open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False, default=str)
        stream.write("\n")


def write_csv(out, suffix, rows, fields=None):
    fields = fields or list(dict.fromkeys(key for row in rows for key in row))
    with (out / f"{STEM}_{suffix}.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def inventory(database, source_symbols):
    """Compact covering-index scans, followed by bounded 2022 bar-date reads."""
    with database.read_only() as connection:
        companies = {r["symbol"]: dict(r) for r in connection.execute("SELECT * FROM companies")}
        assets = {r["symbol"]: dict(r) for r in connection.execute(
            "SELECT symbol,tradable,exchange,asset_class FROM assets")}
        bar_info = {r["symbol"]: dict(r) for r in connection.execute(
            "SELECT symbol,MIN(timestamp) first_daily_date,MAX(timestamp) last_daily_date,"
            "SUM(timestamp<?) daily_bars_before_2022,"
            "SUM(timestamp>=? AND timestamp<?) daily_bars_in_2022 "
            "FROM bars INDEXED BY ix_bars_symbol_timeframe_timestamp "
            "WHERE timeframe='1d' GROUP BY symbol",
            (START.isoformat(), START.isoformat(), (END + timedelta(days=1)).isoformat()))}
        relevant = sorted(set(source_symbols) | {s for s, r in bar_info.items() if r["daily_bars_in_2022"]})
        metric_info = defaultdict(dict)
        for i, batch in enumerate(batches(relevant)):
            marks = ",".join("?" for _ in batch)
            for r in connection.execute(
                f"SELECT symbol,metric,COUNT(*) fact_count,MIN(filed) first_filed,MAX(filed) last_filed,"
                "SUM(filed<=?) pit_fact_count,MIN(CASE WHEN filed<=? THEN filed END) first_pit_filed "
                f"FROM fundamental_facts INDEXED BY ix_facts_pit WHERE symbol IN ({marks}) "
                "GROUP BY symbol,metric", [END.isoformat(), END.isoformat(), *batch]):
                metric_info[r["symbol"]][r["metric"]] = dict(r)
            if i % 15 == 0:
                print(f"Inventory: {min((i + 1) * 80, len(relevant))}/{len(relevant)} symbols", flush=True)
        bar_dates = defaultdict(list)
        for r in connection.execute(
            "SELECT symbol,substr(timestamp,1,10) session FROM bars "
            "WHERE timeframe='1d' AND timestamp>=? AND timestamp<? ORDER BY symbol,timestamp",
            (START.isoformat(), (END + timedelta(days=1)).isoformat())):
            bar_dates[r["symbol"]].append(date.fromisoformat(r["session"]).toordinal())
        cache_keys = {r[0] for r in connection.execute(
            "SELECT cik FROM raw_sec_cache WHERE endpoint='companyfacts'")}
    return companies, assets, bar_info, relevant, metric_info, bar_dates, cache_keys


def inventory_first(metric_info, symbol):
    return {m: date.fromisoformat(r["first_filed"]) for m, r in metric_info[symbol].items()}


def load_raw_caches(database, symbols, companies, cache_keys):
    """Only surviving local cache payloads; never reparse into production storage."""
    result = {}
    with database.read_only() as connection:
        for symbol in sorted(symbols):
            cik = companies[symbol]["cik"]
            if cik not in cache_keys:
                continue
            row = connection.execute(
                "SELECT payload FROM raw_sec_cache WHERE cik=? AND endpoint='companyfacts'", (cik,)).fetchone()
            payload = json.loads(row[0])
            result[symbol] = raw_payload_evidence(payload, symbol)
    return result


def identity_diagnostic_issues(database, relevant, bar_info, metric_info):
    with database.read_only() as connection:
        security_types = {r[0]: r[1] for r in connection.execute(
            "SELECT symbol,security_type FROM security_reference")}
    result = {}
    for symbol in relevant:
        if security_types.get(symbol) in {"ETF", "ETN", "MUTUAL_FUND"}:
            result[symbol] = "LOCAL_REFERENCE_FUND_CLASSIFICATION; issuer fundamentals are not stock-company coverage"
        elif (bar_info.get(symbol, {}).get("daily_bars_in_2022", 0) and metric_info[symbol]
                and min(r["first_filed"] for r in metric_info[symbol].values()) > END.isoformat()):
            result[symbol] = "HISTORICAL_ISSUER_CONTINUITY_UNVERIFIED; 2022 bars but first local filing for current mapping after 2022"
    return result


class GapAccumulator:
    """Count missing factors once per category/session; retain compact evidence groups."""

    def __init__(self):
        self.factors = {}
        self.evidence = {}
        self.score_gaps = {name: Counter() for name in ("quality", "valuation")}
        self.score_symbols = {name: defaultdict(set) for name in ("quality", "valuation")}
        self.symbol_reasons = defaultdict(lambda: {name: Counter() for name in ("quality", "valuation")})
        self.reason_counts = Counter()
        self.raw_score_upper_bound = {name: set() for name in ("quality", "valuation")}
        self.raw_resolvable_sessions = {name: 0 for name in ("quality", "valuation")}
        self.backfill = {}

    def add(self, symbol, session, factor, diagnosis, domain, score_unavailable=False):
        row = self.factors.setdefault(factor, {"domain": domain, "observations": 0, "available": 0,
            "missing": 0, "symbols": set(), "categories": Counter(),
            "unavailable_score_observations": 0, "unavailable_score_categories": Counter()})
        row["observations"] += 1
        if diagnosis["available"]:
            row["available"] += 1
            return
        row["missing"] += 1
        row["symbols"].add(symbol)
        categories = {i.category for i in diagnosis["issues"]}
        row["categories"].update(categories)
        if score_unavailable:
            row["unavailable_score_observations"] += 1
            row["unavailable_score_categories"].update(categories)
        for reason in diagnosis["reasons"]:
            self.reason_counts[(domain, reason)] += 1
        for issue in diagnosis["issues"]:
            key = (symbol, factor, issue.category, issue.detail, issue.inputs)
            evidence = self.evidence.setdefault(key, {"symbol": symbol, "factor": factor,
                "category": issue.category, "detail": issue.detail, "missing_inputs": "|".join(issue.inputs),
                "first_as_of": session.isoformat(), "last_as_of": session.isoformat(), "sessions": 0,
                "score_unavailable_sessions": 0, "economic_coissue_sessions": 0})
            evidence["sessions"] += 1
            evidence["last_as_of"] = session.isoformat()
            evidence["score_unavailable_sessions"] += score_unavailable
            evidence["economic_coissue_sessions"] += ECONOMIC in categories
            if issue.category == RAW and ECONOMIC not in categories:
                candidate = self.backfill.setdefault((symbol, factor), {
                    "sessions": set(), "inputs": set(), "details": set(),
                    "score_unavailable_sessions": set(), "raw_only_sessions": set()})
                candidate["sessions"].add(session)
                candidate["inputs"].update(issue.inputs)
                candidate["details"].add(issue.detail)
                if score_unavailable:
                    candidate["score_unavailable_sessions"].add(session)
                if categories == {RAW}:
                    candidate["raw_only_sessions"].add(session)

    def score(self, symbol, session, domain, breakdown, diagnostics):
        if breakdown.score is not None:
            return
        self.score_gaps[domain]["unavailable_symbol_sessions"] += 1
        categories = {i.category for f in diagnostics.values() for i in f["issues"]}
        for category in categories:
            self.score_gaps[domain][category] += 1
            self.score_symbols[domain][category].add(symbol)
            self.symbol_reasons[symbol][domain][category] += 1
        self.reason_counts[(domain, "INSUFFICIENT_" + domain.upper() + "_FACTORS")] += 1
        # An availability upper bound only: no replacement metric, score or F decision.
        potentially_recoverable = sum(
            not f["available"] and RAW in {i.category for i in f["issues"]}
            and not ({ECONOMIC, PEER, IDENTITY, UNKNOWN, DERIVED} & {i.category for i in f["issues"]})
            for f in diagnostics.values())
        if breakdown.available_factor_count + potentially_recoverable >= breakdown.minimum_required_factor_count:
            self.raw_score_upper_bound[domain].add(symbol)
            self.raw_resolvable_sessions[domain] += 1

    def factor_rows(self):
        result = []
        for factor, r in sorted(self.factors.items()):
            result.append({"factor": factor, "domain": r["domain"], "symbols_affected": len(r["symbols"]),
                "symbol_sessions_affected": r["missing"], "symbol_sessions_observed": r["observations"],
                "symbol_sessions_available": r["available"],
                **{FACTOR_COLUMNS[k]: r["categories"][k] for k in CATEGORIES},
                "score_unavailable_factor_sessions": r["unavailable_score_observations"],
                **{"score_unavailable_" + FACTOR_COLUMNS[k]: r["unavailable_score_categories"][k] for k in CATEGORIES}})
        return result


def universe_evidence(database, bar_info, companies, assets, source_symbols, conflicts, metric_info):
    with database.read_only() as connection:
        references = {r["symbol"]: dict(r) for r in connection.execute("SELECT * FROM security_reference")}
        epochs = [dict(r) for r in connection.execute("SELECT * FROM symbol_identity_epochs")]
        actions = [dict(r) for r in connection.execute(
            "SELECT provider,action_type,old_symbol,new_symbol,process_date,effective_date FROM security_corporate_actions")]
        snapshots = [dict(r) for r in connection.execute("SELECT * FROM security_reference_snapshots")]
    probe_path = ROOT / "docs/research/massive-historical-identity-probes-2026-10-15.json"
    probe = json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else {}
    observations = [r for v in probe.values() if isinstance(v, list) for r in v
                    if isinstance(r, dict) and r.get("operation") == "details"]
    historical = [r for r in observations if str(r.get("as_of", "9999")) <= END.isoformat() and r.get("results")]
    local_2022 = {s for s, b in bar_info.items() if b["daily_bars_in_2022"]}
    omitted = sorted(local_2022 - set(source_symbols))
    evidence_symbols = {r.get("identifier") for r in historical}
    corporate_symbols = {r[k] for r in actions for k in ("old_symbol", "new_symbol") if r[k]}
    identity_conflicts = []
    for row in historical:
        symbol, value = row["identifier"], row["results"]
        current = references.get(symbol, {})
        local = companies.get(symbol, {})
        if value.get("cik") and local.get("cik") and str(value["cik"]).zfill(10) != local["cik"]:
            identity_conflicts.append({"symbol": symbol, "historical_as_of": row["as_of"],
                "historical_cik": value["cik"], "current_company_cik": local["cik"],
                "historical_composite_figi": value.get("composite_figi"),
                "current_composite_figi": current.get("composite_figi"),
                "historical_list_date": value.get("list_date"),
                "historical_delisted_utc": value.get("delisted_utc"),
                "has_2022_daily_bars": symbol in local_2022,
                "in_current_source": symbol in source_symbols})
    excluded_with_pit = [s for s in omitted if sum(r["pit_fact_count"] for r in metric_info[s].values())]
    omitted_rows = [{"symbol": s, "has_company": s in companies,
        "current_tradable": assets.get(s, {}).get("tradable"),
        "asset_class": assets.get(s, {}).get("asset_class"),
        "cik": companies.get(s, {}).get("cik"), "sic": companies.get(s, {}).get("sic"),
        "daily_bars_before_2022": bar_info[s]["daily_bars_before_2022"],
        "pit_fact_count_by_end_2022": sum(r["pit_fact_count"] for r in metric_info[s].values()),
        "local_reference": s in references, "historical_probe": s in evidence_symbols,
        "reason": "NO_CURRENT_COMPANY" if s not in companies else "NOT_CURRENT_TRADABLE_ASSET"}
        for s in omitted]
    decision = ("HISTORICAL_UNIVERSE_BIAS_EVIDENCE_STRONG" if any(r["has_2022_daily_bars"] for r in identity_conflicts)
                else "HISTORICAL_UNIVERSE_BIAS_EVIDENCE_MODERATE" if excluded_with_pit or identity_conflicts
                else "HISTORICAL_UNIVERSE_BIAS_EVIDENCE_WEAK" if omitted
                else "HISTORICAL_UNIVERSE_BIAS_NOT_QUANTIFIABLE")
    summary = {"classification": decision, "current_source_universe_count": len(source_symbols),
        "symbols_with_2022_bars_not_current_source": len(omitted),
        "omitted_symbols_with_PIT_facts": len(excluded_with_pit),
        "omitted_symbols_with_300_prior_bars_and_PIT_facts": sum(
            bar_info[s]["daily_bars_before_2022"] >= 300 for s in excluded_with_pit),
        "unresolved_current_SEC_identity_conflicts": sorted(conflicts),
        "conflicted_symbols_with_2022_bars": sorted(local_2022 & conflicts),
        "local_security_reference_rows": len(references), "security_reference_snapshots": snapshots,
        "identity_epoch_rows": len(epochs), "corporate_action_rows": actions,
        "symbols_with_2022_bars_and_corporate_action_evidence": sorted(local_2022 & corporate_symbols),
        "symbols_with_2022_bars_and_Massive_historical_identity_evidence": sorted(local_2022 & evidence_symbols),
        "historical_current_CIK_conflicts": identity_conflicts,
        "historical_probe_source": str(probe_path.relative_to(ROOT)) if probe else None,
        "probe_sha256": digest(probe_path) if probe else None,
        "historical_probe_note": "Previously collected artifacts only. Their recorded requests are NOT audit requests.",
        "fundamental_source_note": "Massive does not provide F Quality/Valuation fundamentals in current TraWalp production integration.",
        "scope": "HISTORICAL_UNIVERSE_IDENTITY_GAP; separate from FUNDAMENTAL_COVERAGE_GAP",
        "retained_biases": ["CURRENT_UNIVERSE_ONLY", "NOT_SURVIVORSHIP_CLEAN"],
        "limitation": "No complete historical listings/delistings universe; economic magnitude not quantified."}
    return summary, omitted_rows, evidence_symbols, corporate_symbols


def replay(source, sessions, relevant, bar_info, bar_dates, metric_info, raw_caches, conflicts,
           accumulator=None, symbol_counts=None, primary=None, identity_issues=None):
    accumulator = accumulator if accumulator is not None else GapAccumulator()
    symbol_counts = symbol_counts if symbol_counts is not None else {s: Counter() for s in relevant}
    primary = primary if primary is not None else defaultdict(Counter)
    identity_issues = identity_issues or {}
    primary_spans = {}
    primary_totals = Counter()
    rows, diag_cache = [], {}
    source_symbols = {c.symbol for c in source._companies} | {c.symbol for c in source._conflicted}
    first_pit = {s: min((date.fromisoformat(r["first_filed"]) for r in metric_info[s].values()),
                       default=date.max) for s in relevant}
    daily_2022 = {s for s, b in bar_info.items() if b["daily_bars_in_2022"]}

    def add_primary(symbol, session, reason):
        primary[symbol][reason] += 1
        primary_totals[reason] += 1
        key = (symbol, reason)
        span = primary_spans.setdefault(key, [session, session, 0])
        span[1], span[2] = session, span[2] + 1

    for index, session in enumerate(sessions):
        row = Counter()
        row["session"] = session.isoformat()
        row["daily_symbols_any_2022_count"] = len(daily_2022)
        row["source_universe_count"] = len(source_symbols)
        row["identity_ready_count"] = len(source_symbols - conflicts)
        row["static_classification_ready_count"] = len(source._companies) - len(source._static_exclusions)
        for symbol in relevant:
            history = bar_info.get(symbol, {}).get("daily_bars_before_2022", 0) + bisect_right(bar_dates.get(symbol, []), session.toordinal())
            if history >= 300:
                row["all_relevant_history_ready_count"] += 1
                if symbol in source_symbols:
                    row["history_ready_count"] += 1
            if symbol in daily_2022 and first_pit[symbol] <= session:
                row["all_daily_pit_fundamental_count"] += 1
            if symbol not in source_symbols:
                add_primary(symbol, session, "NOT_CURRENT_SOURCE_UNIVERSE")
        prepared = [source._candidate(c, session) for c in source._companies]
        peers = source.screener._f_peer_index(prepared)
        for candidate in prepared:
            symbol = candidate.company.symbol
            counts = symbol_counts[symbol]
            cheap = source._cheap_exclusions.get(symbol, {}).get(session)
            if cheap is not None and not cheap and symbol not in source._static_exclusions:
                row["price_liquidity_history_ready_before_marketcap_count"] += 1
                if "missing_or_small_market_cap" in candidate.base_exclusions:
                    row["marketcap_gate_blocked_count"] += 1
                    if candidate.estimated_market_cap is not None:
                        row["marketcap_below_threshold_count"] += 1
                    else:
                        facts = source._shares.get(symbol, [])
                        raw = raw_caches.get(symbol)
                        evidence = AccountingEvidence(facts, session,
                            metric_first=inventory_first(metric_info, symbol), cache_facts=raw.facts if raw else [], raw_evidence=raw)
                        issues = ((Issue(IDENTITY, identity_issues[symbol], ("shares_outstanding",)),)
                                  if symbol in identity_issues else unique_issues(evidence.balance("shares_outstanding")))
                        accumulator.add(symbol, session, "market_cap_input", {
                            "available": False, "issues": issues,
                            "reasons": ("MARKET_CAP_INPUT_UNAVAILABLE",)}, "pre_scoring")
                        row["marketcap_input_unavailable_count"] += 1
            if not candidate.fundamentals_evaluated:
                add_primary(symbol, session, entry_blocking_reasons(candidate.base_exclusions)[0])
                counts["not_evaluated_before_f_scoring_session_count"] += 1
                continue
            row["price_liquidity_marketcap_ready_count"] += 1
            counts["fundamentals_evaluated_session_count"] += 1
            if candidate.pit_fact_count:
                row["pit_fundamental_count"] += 1
            view, group, medians = source.screener._score_inputs(candidate, peers)
            evaluation = evaluate_variant_entry(view, FROZEN_CHAMPION_F.variant, source.config)
            gate = evaluate_strategy_gate(candidate.technical, FROZEN_CHAMPION_F.variant, source.config)
            q, v = view.scores.quality, view.scores.valuation
            row["quality_score_available_count"] += q.score is not None
            row["quality_threshold_pass_count"] += q.score is not None and q.score >= source.config.backtest.min_quality_score
            row["valuation_score_available_count"] += v.score is not None
            row["valuation_threshold_pass_count"] += v.score is not None and v.score >= source.config.backtest.min_valuation_score
            counts["quality_available_session_count"] += q.score is not None
            counts["valuation_available_session_count"] += v.score is not None
            row["positive_ocf_count"] += candidate.fundamentals.operating_cash_flow_positive is True
            complete = all(getattr(candidate.technical, field) is not None for field in TECHNICAL_FIELDS)
            row["technical_features_available_count"] += complete
            row["momentum_gate_pass_count"] += gate.first_failure is None
            no_hard_block = not entry_blocking_reasons(view.exclusion_reasons)
            fundamental_pass = (no_hard_block and q.score is not None and v.score is not None
                and q.score >= source.config.backtest.min_quality_score
                and v.score >= source.config.backtest.min_valuation_score)
            row["f_fundamental_rules_pass_count"] += fundamental_pass
            row["cumulative_quality_pass_count"] += no_hard_block and q.score is not None and q.score >= 70
            row["cumulative_valuation_pass_count"] += fundamental_pass
            row["cumulative_technical_ready_count"] += fundamental_pass and complete
            row["cumulative_momentum_pass_count"] += fundamental_pass and gate.first_failure is None
            row["final_f_candidate_count"] += evaluation.eligible
            counts["final_candidate_session_count"] += evaluation.eligible
            add_primary(symbol, session, evaluation.first_failure or "F_ELIGIBLE")
            cache = source._facts.get(symbol)
            facts = cache.facts if cache else []
            eligible_count = _eligible_fact_count(facts, session)
            # Filing-state diagnostics are cached independently of prices and peers.
            raw = raw_caches.get(symbol)
            raw_eligible_count = (sum(f.filed <= session for f in raw.facts)
                                  + sum(d <= session for d in raw.metric_first.values())) if raw else 0
            key = (symbol, eligible_count, raw_eligible_count, bool(candidate.fundamentals.market_cap))
            evidence = diag_cache.get(key)
            if evidence is None:
                evidence = AccountingEvidence(facts[:eligible_count], session,
                    metric_first=inventory_first(metric_info, symbol), cache_facts=raw.facts if raw else [], raw_evidence=raw)
                diag_cache[key] = evidence
            diagnoses = diagnose_factors(view, candidate, group, medians, evidence)
            for domain, breakdown, names in (("quality", q, QUALITY), ("valuation", v, VALUATION)):
                diag = {name: diagnoses[name] for name in names}
                for name, diagnosis in diag.items():
                    accumulator.add(symbol, session, name, diagnosis, domain, breakdown.score is None)
                accumulator.score(symbol, session, domain, breakdown, diag)
                if candidate.pit_fact_count == 0 and breakdown.score is None:
                    accumulator.reason_counts[(domain, "NO_PIT_FUNDAMENTALS")] += 1
            counts["peer_context_unavailable_session_count"] += group is None
            counts["peer_valuation_gap_session_count"] += any(
                i.category == PEER for name in VALUATION for i in diagnoses[name]["issues"])
        for company in source._conflicted:
            add_primary(company.symbol, session, "identity_conflict")
        # Explicit zeros are essential for session distributions and reproducibility.
        fields = ("history_ready_count", "price_liquidity_marketcap_ready_count", "pit_fundamental_count",
            "quality_score_available_count", "quality_threshold_pass_count", "valuation_score_available_count",
            "valuation_threshold_pass_count", "positive_ocf_count", "technical_features_available_count",
            "momentum_gate_pass_count", "final_f_candidate_count", "f_fundamental_rules_pass_count",
            "cumulative_quality_pass_count", "cumulative_valuation_pass_count", "cumulative_technical_ready_count",
            "cumulative_momentum_pass_count", "marketcap_below_threshold_count", "marketcap_input_unavailable_count")
        for field in fields:
            row.setdefault(field, 0)
        # G is descriptive, not an extra gate: raw company metrics could support both
        # score minima if peer multiples existed (without changing actual scores).
        row["fundamentals_sufficient_count"] = sum(
            c.fundamentals_evaluated and
            (c.fundamentals.operating_cash_flow_positive is not None) and
            sum(getattr(c.fundamentals, m) is not None for m in (
                "revenue_growth", "eps_growth", "operating_margin", "roic", "debt_to_ebitda")) + 1 >= 4
            and sum((c.fundamentals.pe is not None and c.fundamentals.pe > 0,
                     (c.fundamentals.ev_to_ebitda is not None and c.fundamentals.ev_to_ebitda > 0)
                     or (c.fundamentals.ev_to_ebit is not None and c.fundamentals.ev_to_ebit > 0),
                     c.fundamentals.fcf_yield is not None)) >= 2 for c in prepared)
        rows.append(dict(row))
        if index % 20 == 0 or index + 1 == len(sessions):
            print(f"Screen diagnostics {index + 1}/{len(sessions)} {session}: "
                  f"Q={row['quality_score_available_count']} V={row['valuation_score_available_count']} "
                  f"F={row['final_f_candidate_count']}", flush=True)
    primary_rows = [{"symbol": symbol, "primary_blocking_reason": reason,
        "first_as_of": span[0], "last_as_of": span[1], "sessions": span[2]}
        for (symbol, reason), span in sorted(primary_spans.items())]
    return rows, accumulator, symbol_counts, primary, primary_rows, primary_totals


SYNC_ARCHITECTURE = {
    "targeted_sec_backfill_capability": "PARTIAL",
    "existing_cli": "trading-system sync --incremental --symbols SYMBOL ...",
    "existing_cli_full": "trading-system sync --full --symbols SYMBOL ...",
    "synchronizer": "DataSynchronizer.sync_sec_full(requested_symbols)",
    "source": "src/trading_system/data/sync.py:550; cli.py:309",
    "incremental": "Daily-master-index/accession state selects changed companies. Already-current companies can be skipped even with historical metric gaps.",
    "full_sec_method": "Full SEC method selects all canonical CIKs in requested tradable assets and forces Company Facts; it does not invoke bars/assets.",
    "full_cli": "CLI --full wraps sync_full: asset refresh + SEC full + bars (+ intraday if enabled). Not a SEC-only targeted command.",
    "history": "SecClient.company_facts returns the whole Companyfacts response; parse_company_facts retains all supported historical filed observations, no date filter.",
    "targeting": "Symbol targeting exists, restricted to current tradable assets and resolved canonical CIKs. No direct CIK/date-range CLI targeting.",
    "missing_capability": "TARGETED_SEC_ONLY_FORCE_BACKFILL_CLI_MISSING",
    "minimal_later_extension": "Add an explicit SEC-only --force/--symbols CLI route to existing sync_sec_full; no new downloader. Reviewed CIK manifest and dry-run plan for current-universe exclusions separately.",
    "executed": False,
}


def backfill_rows(accumulator, companies, metric_info, cache_keys, sessions):
    rows = []
    for (symbol, factor), value in sorted(accumulator.backfill.items()):
        company = companies.get(symbol, {})
        if not company.get("cik"):
            continue
        affected = sorted(value["sessions"])
        # Priority ranks local investigation; it does not assert remote facts exist.
        raw_only = len(value["raw_only_sessions"])
        unavailable = len(value["score_unavailable_sessions"])
        cache_present = company["cik"] in cache_keys
        priority = ("HIGH" if not cache_present and raw_only >= math.ceil(len(sessions) / 2)
                    and (unavailable or factor == "market_cap_input")
                    else "MEDIUM" if not cache_present and unavailable else "LOW")
        info = metric_info[symbol]
        rows.append({"symbol": symbol, "cik": company["cik"], "affected_factor": factor,
            "earliest_missing_as_of": affected[0], "latest_missing_as_of": affected[-1],
            "affected_sessions": len(affected), "existing_fact_count": sum(r["fact_count"] for r in info.values()),
            "existing_PIT_fact_count_at_end_2022": sum(r["pit_fact_count"] for r in info.values()),
            "company_has_SEC_facts": bool(sum(r["fact_count"] for r in info.values())),
            "first_local_filing_date": min((r["first_filed"] for r in info.values()), default=None),
            "last_local_filing_date": max((r["last_filed"] for r in info.values()), default=None),
            "metric_coverage": "|".join(sorted(m for m, r in info.items() if r["pit_fact_count"])),
            "relevant_missing_metric_inputs": "|".join(sorted(value["inputs"])),
            "reason": RAW + ":" + "|".join(sorted(value["details"])),
            "raw_companyfacts_cache_present": cache_present,
            "score_unavailable_sessions": unavailable, "raw_only_factor_sessions": raw_only,
            "priority": priority,
            "recoverability": "UNPROVEN_REMOTE_COVERAGE; current Companyfacts may return same omissions"})
    return rows


def symbol_rows(relevant, bar_info, companies, metric_info, counts, primary, accumulator,
                candidates, conflicts, historical_symbols, corporate_symbols, source_symbols):
    backfill_symbols = {r["symbol"] for r in candidates}
    rows = []
    for symbol in relevant:
        bar, company, info, c = bar_info.get(symbol, {}), companies.get(symbol, {}), metric_info[symbol], counts[symbol]
        rows.append({"symbol": symbol,
            "in_current_source_universe": symbol in source_symbols,
            "first_daily_date": str(bar.get("first_daily_date", ""))[:10],
            "last_daily_date": str(bar.get("last_daily_date", ""))[:10],
            "daily_bars_before_2022": bar.get("daily_bars_before_2022", 0),
            "daily_bars_in_2022": bar.get("daily_bars_in_2022", 0),
            "company_identity_available": bool(company.get("cik")) and symbol not in conflicts,
            "cik": company.get("cik"), "sic": company.get("sic"),
            "first_local_filing_date": min((r["first_filed"] for r in info.values()), default=None),
            "last_local_filing_date": max((r["last_filed"] for r in info.values()), default=None),
            "pit_facts_available_in_2022": any(r["pit_fact_count"] for r in info.values()),
            "existing_fact_count": sum(r["fact_count"] for r in info.values()),
            "pit_fact_count_by_end_2022": sum(r["pit_fact_count"] for r in info.values()),
            "quality_ever_available_2022": c["quality_available_session_count"] > 0,
            "valuation_ever_available_2022": c["valuation_available_session_count"] > 0,
            "quality_available_session_count": c["quality_available_session_count"],
            "valuation_available_session_count": c["valuation_available_session_count"],
            "final_candidate_session_count": c["final_candidate_session_count"],
            "fundamentals_evaluated_session_count": c["fundamentals_evaluated_session_count"],
            "not_evaluated_before_f_scoring_session_count": c["not_evaluated_before_f_scoring_session_count"],
            "dominant_quality_gap": dominant(accumulator.symbol_reasons[symbol]["quality"])
                if c["fundamentals_evaluated_session_count"] else "NOT_EVALUATED_BEFORE_F_SCORING",
            "dominant_valuation_gap": dominant(accumulator.symbol_reasons[symbol]["valuation"])
                if c["fundamentals_evaluated_session_count"] else "NOT_EVALUATED_BEFORE_F_SCORING",
            "primary_blocking_reason": dominant(primary[symbol]),
            "raw_sec_backfill_candidate": symbol in backfill_symbols,
            "identity_review_candidate": (symbol in conflicts or symbol in historical_symbols or not company),
            "peer_context_issue": c["peer_valuation_gap_session_count"] > 0,
            "local_Massive_historical_identity_evidence": symbol in historical_symbols,
            "corporate_action_evidence": symbol in corporate_symbols})
    return rows


def score_gap_summary(accumulator):
    result = {}
    for domain, counts in accumulator.score_gaps.items():
        denominator = counts["unavailable_symbol_sessions"]
        result[domain] = {"unavailable_symbol_sessions": denominator,
            "categories": {category: {"symbol_sessions": counts[category],
                "symbols": len(accumulator.score_symbols[domain][category]),
                "share": counts[category] / denominator if denominator else None} for category in CATEGORIES},
            "raw_only_availability_upper_bound_symbols": len(accumulator.raw_score_upper_bound[domain]),
            "raw_only_availability_upper_bound_symbol_sessions": accumulator.raw_resolvable_sessions[domain]}
    return result


def main(out=None):
    started = time.perf_counter()
    out = Path(out).resolve() if out is not None else ROOT / "reports"
    if not out.is_relative_to(ROOT / "reports"):
        raise ValueError("Audit output must remain inside local repository reports")
    out.mkdir(parents=True, exist_ok=True)
    if list(out.glob(STEM + "_summary.json")):
        raise FileExistsError("Fresh audit stem already used; refusing overwrite")
    config = StrategyConfig.model_validate(_read_yaml(ROOT / "config/strategy.yaml"))
    validate_champion_config(config)
    config_before = config.model_dump(mode="json")
    database = AuditDatabase(ROOT / config.storage.database_path)
    source_paths = sorted((ROOT / "src/trading_system").rglob("*.py")) + [ROOT / "config/strategy.yaml"]
    before = {str(p.relative_to(ROOT)): digest(p) for p in source_paths}
    db_files = [p for p in (database.path, Path(str(database.path) + "-wal")) if p.exists()]
    print("Hashing existing DB/WAL read-only", flush=True)
    db_before = {str(p.relative_to(ROOT)): digest(p) for p in db_files}
    with local_guard() as safety:
        source_symbols = {c.symbol for c in database.list_tradable_companies()}
        conflicts = database.unresolved_sec_identity_conflict_symbols()
        companies, assets, bar_info, relevant, metric_info, bar_dates, cache_keys = inventory(database, source_symbols)
        identity_issues = identity_diagnostic_issues(database, relevant, bar_info, metric_info)
        sessions = trading_sessions_between(START, END)
        local_sessions = database.bar_sessions(START, END)
        if set(sessions) - set(local_sessions):
            raise RuntimeError("Missing global official sessions; incomplete audit refused")
        rows, acc, counts, primary = [], GapAccumulator(), {s: Counter() for s in relevant}, defaultdict(Counter)
        primary_totals, primary_compact, preparation = Counter(), {}, Counter()
        raw_caches = {}
        # Monthly canonical sources bound peak memory. The same trailing windows,
        # PIT accounting, complete cross-section, scoring and entry gates are used.
        for month in range(1, 13):
            month_sessions = [s for s in sessions if s.month == month]
            print(f"Preparing production historical source {month:02d}/12", flush=True)
            source = HistoricalFeatureScreenSource(database, config, month_sessions[0], month_sessions[-1])
            diagnostic_symbols = set(source._facts) | {s for s, values in source._cheap_exclusions.items()
                if s not in source._static_exclusions and any(not reasons for reasons in values.values())}
            raw_caches.update(load_raw_caches(database, diagnostic_symbols - set(raw_caches), companies, cache_keys))
            part, _, _, _, reasons, totals = replay(source, month_sessions, relevant, bar_info,
                bar_dates, metric_info, raw_caches, conflicts, acc, counts, primary, identity_issues)
            rows.extend(part)
            primary_totals.update(totals)
            for reason in reasons:
                key = (reason["symbol"], reason["primary_blocking_reason"])
                if key not in primary_compact:
                    primary_compact[key] = reason
                else:
                    primary_compact[key]["last_as_of"] = reason["last_as_of"]
                    primary_compact[key]["sessions"] += reason["sessions"]
            preparation.update(source.diagnostics.as_dict())
            del source
            for method in (AccountingEvidence.flow, AccountingEvidence.balance,
                           AccountingEvidence.da, AccountingEvidence.ebitda, AccountingEvidence.quality):
                method.cache_clear()
            gc.collect()
        primary_rows = list(primary_compact.values())
        universe, omitted, historical_symbols, corporate_symbols = universe_evidence(
            database, bar_info, companies, assets, source_symbols, conflicts, metric_info)
        universe["additional_identity_classification_review"] = identity_issues
        universe["identity_review_scope_note"] = "Late first local filings indicate an unresolved historical mapping, not proof of a different issuer. Funds are positively identified by local reference. Neither class goes to SEC company backfill without review."
        candidates = backfill_rows(acc, companies, metric_info, cache_keys, sessions)
        symbols = symbol_rows(relevant, bar_info, companies, metric_info, counts, primary, acc,
                              candidates, conflicts, historical_symbols, corporate_symbols, source_symbols)
        for row in symbols:
            if row["symbol"] in identity_issues:
                row["identity_review_candidate"] = True
                row["identity_review_reason"] = identity_issues[row["symbol"]]
        factor_rows = acc.factor_rows()
    after = {str(p.relative_to(ROOT)): digest(p) for p in source_paths}
    db_after = {str(p.relative_to(ROOT)): digest(p) for p in db_files}
    if before != after or db_before != db_after or config_before != config.model_dump(mode="json"):
        raise AssertionError("Existing source/config/database changed during read-only audit")
    if any(v for k, v in safety.items() if k.startswith("blocked_")):
        raise AssertionError(f"Unexpected blocked attempt during audit: {safety}")
    unique_q = sum(r["quality_ever_available_2022"] for r in symbols)
    unique_v = sum(r["valuation_ever_available_2022"] for r in symbols)
    gaps = score_gap_summary(acc)
    raw_upper = gaps["quality"]["raw_only_availability_upper_bound_symbol_sessions"] + gaps["valuation"]["raw_only_availability_upper_bound_symbol_sessions"]
    unavailable = sum(r["unavailable_symbol_sessions"] for r in gaps.values())
    candidate_symbols = sorted({r["symbol"] for r in candidates})
    raw_share = raw_upper / unavailable if unavailable else 0
    # Prospective value is availability potential, never an assertion of recoverable remote data.
    decision = ("SEC_BACKFILL_HIGH_VALUE" if len(candidate_symbols) >= 25 and raw_share >= .5
        else "SEC_BACKFILL_MODERATE_VALUE" if len(candidate_symbols) >= 10 and raw_share >= .1
        else "SEC_BACKFILL_LOW_VALUE" if candidate_symbols else "SEC_BACKFILL_NOT_JUSTIFIED")
    old_path = ROOT / "reports/f_c1_regime_sanity_2022-01-03_2026-08-12_v1_candidate_availability.csv"
    old = []
    if old_path.exists():
        with old_path.open(encoding="utf-8") as stream:
            old = [r for r in csv.DictReader(stream) if r["session"][:4] == "2022"]
    comparison = {"quality_unique": {"expected_approximate": 301, "actual": unique_q,
                  "classification": "MATCH" if unique_q == 301 else "UNEXPLAINED_DIFFERENCE"},
        "valuation_unique": {"expected_approximate": 164, "actual": unique_v,
                  "classification": "MATCH" if unique_v == 164 else "UNEXPLAINED_DIFFERENCE"},
        "previous_session_rows_available": len(old)}
    if old:
        old_by_date = {r["session"]: r for r in old}
        mapping = {"quality_score_available_count": "valid_quality",
                   "valuation_score_available_count": "valid_valuation",
                   "final_f_candidate_count": "final_eligible_f_candidates"}
        # Column names in earlier reports are recorded, never assumed silently.
        comparison["previous_columns"] = list(old[0])
        comparison["session_comparison"] = {}
        for new, previous in mapping.items():
            if previous in old[0]:
                different = [r["session"] for r in rows if r["session"] in old_by_date and
                             r[new] != int(old_by_date[r["session"]][previous])]
                comparison["session_comparison"][new] = {"different_sessions": len(different),
                    "classification": "MATCH" if not different else "UNEXPLAINED_DIFFERENCE"}
    summary = {"local_head": subprocess.check_output(
            ["git", "-c", "safe.directory=" + str(ROOT).replace("\\", "/"), "rev-parse", "HEAD"], text=True).strip(),
        "branch": subprocess.check_output(["git", "-c", "safe.directory=" + str(ROOT).replace("\\", "/"),
                                           "branch", "--show-current"], text=True).strip(),
        "champion": FROZEN_CHAMPION_F.production_label, "period": [START, END], "sessions": len(sessions),
        "safety": safety, "database_sha256_before": db_before, "database_sha256_after": db_after,
        "existing_source_and_config_unchanged": before == after,
        "source_and_config_sha256": before,
        "daily_symbols_2022": sum(bool(b["daily_bars_in_2022"]) for b in bar_info.values()),
        "daily_symbols_300_bars_before_start": sum(b["daily_bars_in_2022"] > 0 and b["daily_bars_before_2022"] >= 300 for b in bar_info.values()),
        "daily_symbols_with_PIT_facts_by_end_2022": sum(r["daily_bars_in_2022"] > 0 and r["pit_facts_available_in_2022"] for r in symbols),
        "relevant_symbols": len(relevant), "current_source_universe_count": len(source_symbols),
        "fundamentals_evaluated_unique_symbols": sum(c["fundamentals_evaluated_session_count"] > 0 for c in counts.values()),
        "fundamentals_evaluated_symbol_sessions": sum(c["fundamentals_evaluated_session_count"] for c in counts.values()),
        "quality_available_unique_symbols": unique_q, "valuation_available_unique_symbols": unique_v,
        "sessions_with_zero_quality_scores": sum(r["quality_score_available_count"] == 0 for r in rows),
        "sessions_with_zero_valuation_scores": sum(r["valuation_score_available_count"] == 0 for r in rows),
        "sessions_with_zero_final_candidates": sum(r["final_f_candidate_count"] == 0 for r in rows),
        "session_statistics": aggregate_sessions(rows), "score_unavailability": gaps,
        "primary_blocking_reason_symbol_sessions": dict(primary_totals),
        "factor_reason_symbol_sessions": [{"domain": d, "reason": r, "symbol_sessions": n}
                                          for (d, r), n in sorted(acc.reason_counts.items())],
        "sec_backfill_candidate_symbols": len(candidate_symbols), "sec_backfill_candidate_factor_rows": len(candidates),
        "sec_backfill_decision": decision, "raw_only_availability_upper_bound_share": raw_share,
        "decision_rule": "HIGH >=25 candidate symbols AND >=50% raw-only score availability upper bound; MODERATE >=10 AND >=10%; LOW other positive candidate count; NOT_JUSTIFIED zero. Remote recovery remains unproven.",
        "historical_universe_decision": universe["classification"], "sync_architecture": SYNC_ARCHITECTURE,
        "reproduction": comparison, "feature_preparation_sum_of_monthly_chunks": dict(preparation),
        "execution_chunks": "12 independent monthly production feature sources; complete cross-section and canonical trailing prefixes for each session; no portfolio execution",
        "diagnostic_scope": "Actual production market/cap-qualified fundamental evaluations; supplementary shares diagnosis for cheap-market-qualified cap failures. Other early exits are NOT_EVALUATED, not missing-data assertions.",
        "gap_share_denominator": "Unavailable score symbol-sessions, separately Q/V. Each category counted at most once per score/session. Categories overlap; factor occurrences are a different denominator.",
        "pit": "All fundamental inputs/evidence filed<=session. Cache parsed facts also cut off. Future dates in inventory are descriptive metadata only.",
        "raw_evidence_scope": "Known mapped concepts in normalized facts plus surviving local Companyfacts JSON. No stored full filings or complete historical SEC database versions; missing mapped facts do not establish remote recoverability or prove a missed download.",
        "local_raw_cache_diagnostics": {s: {"taxonomy_PIT_counts": r.taxonomy_pit_counts,
            "diagnostic_metric_first_filed": r.metric_first,
            "unsupported_financial_taxonomy_first_filed": r.unsupported_first}
            for s, r in raw_caches.items()},
        "retained_biases": ["CURRENT_UNIVERSE_ONLY", "NOT_SURVIVORSHIP_CLEAN", "Current SIC/company identity may not be PIT",
            "Current SEC database revisions cannot prove the complete originally observable historical version", "Day-level filed availability"],
        "no_strategy_inference": True, "elapsed_seconds": round(time.perf_counter() - started, 3)}
    plan = {"executed": False, "decision": decision, "number_of_symbols": len(candidate_symbols),
        "symbols": candidate_symbols, "CIKs": sorted({r["cik"] for r in candidates}),
        "required_historical_range": "2019-01-01 through 2022-12-30; retain earlier comparative/stale inputs when selected by existing metrics; filed cutoff per session",
        "existing_path": SYNC_ARCHITECTURE,
        "missing_input_classes": dict(Counter(i for v in acc.backfill.values() for i in v["inputs"])),
        "expected_diagnostic_improvement": gaps,
        "limits": "Upper bounds only; no replacement scores, no portfolio run, no guaranteed new F candidates. Full Companyfacts may return the same gaps. Historical fundamentals improved != historical universe reconstructed.",
        "next_task": "Review prioritized symbol/factor manifest and SEC-only force path; separate local metric-construction samples before authorizing any narrow network backfill."}
    universe["valid_bars_and_PIT_facts_but_excluded_before_F_scoring"] = sum(
        r["daily_bars_in_2022"] > 0 and r["pit_facts_available_in_2022"] and not r["fundamentals_evaluated_session_count"] for r in symbols)
    write_csv(out, "session_funnel", rows)
    write_csv(out, "symbol_coverage", symbols)
    write_csv(out, "factor_gaps", factor_rows)
    write_csv(out, "sec_backfill_candidates", candidates, fields=list(candidates[0]) if candidates else [
        "symbol", "cik", "affected_factor", "earliest_missing_as_of", "latest_missing_as_of", "affected_sessions", "existing_fact_count", "reason", "priority"])
    write_csv(out, "factor_evidence", list(acc.evidence.values()))
    write_csv(out, "primary_blocking_reasons", primary_rows)
    write_csv(out, "universe_exclusions", omitted, fields=list(omitted[0]) if omitted else ["symbol", "reason"])
    write_json(out, "universe_identity_summary", universe)
    write_json(out, "sec_backfill_plan", plan)
    write_json(out, "summary", summary)
    render_audit(out, summary, universe, factor_rows, candidates)
    print(json.dumps({k: summary[k] for k in ("sessions", "daily_symbols_2022", "quality_available_unique_symbols",
        "valuation_available_unique_symbols", "sec_backfill_candidate_symbols", "sec_backfill_decision", "historical_universe_decision")}), flush=True)


def render_audit(out, summary, universe, factors, candidates):
    # Kept in this module so the executed diagnostic and report share one definition.
    def table(headers, values):
        return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
                         + ["| " + " | ".join(str(v) for v in row) + " |" for row in values])

    stats = summary["session_statistics"]
    lines = ["# F/configured/C1: lokaler Datenlücken-Audit 2022", "",
        f"HEAD `{summary['local_head']}`, Branch `{summary['branch']}`. Zeitraum 03.01.–30.12.2022, {summary['sessions']} offizielle XNYS-Sessions.", "",
        f"Entscheidung SEC: **{summary['sec_backfill_decision']}**. Historical Universe: **{summary['historical_universe_decision']}**.", "",
        "Keine Strategie-/Konfigurationsänderung, kein Portfolio-Backtest, keine Optimierung. Netzwerk 0, Provider 0, Datenbankschreiben 0. Bestehende DB und WAL sowie Source/YAML vor/nach identisch per SHA-256 (summary.json). .env nicht gelesen.", "",
        "## Methode und Populationen", "",
        "Wiederverwendet: HistoricalFeatureScreenSource._prepare/_candidate, Screener._f_peer_index/_score_inputs, accounting_state_as_of, bestehende TTM-/Balance-Funktionen, score_quality/score_valuation sowie evaluate_variant_entry und evaluate_strategy_gate mit FROZEN_CHAMPION_F. Nur Screen-/Feature-Replay 2022; keine Orders/Portfolioverwaltung.", "",
        "Die Session-Zahlen für Q/V, Schwellen, OCF und Technik sind **parallele** Diagnosen innerhalb der tatsächlich fundamental ausgewerteten Population. Sie dürfen nicht als künstlich ineinander verschachtelter Funnel gelesen werden. cumulative_* zeigt die echte Schnittmenge. G (fundamentals_sufficient_count) ist eine beschreibende Mindestfaktorprüfung auf Unternehmensmetriken ohne Peer-Median, kein zusätzlicher F-Filter. Das primäre Hindernis folgt ausschließlich der echten Reihenfolge von evaluate_variant_entry: geordnete harte Ausschlüsse (einschließlich positivem OCF), Q, V, danach F-Technik. A–O ist die gewünschte Darstellung, nicht eine geänderte Verarbeitungsreihenfolge.", "",
        "primary_blocking_reasons.csv komprimiert jedes Symbol/Reason-Paar mit Zahl der Sessions und erster/letzter Session. factor_evidence.csv hält alle belegten Defizite pro Symbol/Faktor/Kategorie/Detail/Input, einschließlich Mehrfachursachen. Zeitspannen sind Grenzen der gezählten Sessions und behaupten keine lückenlose Episode. Es gibt keinen Millionen-Zeilen-Export.", "",
        "## A. Price History", "",
        f"{summary['daily_symbols_2022']} Symbole haben Daily-Bars 2022; {summary['daily_symbols_300_bars_before_start']} davon bereits mindestens 300 Bars strikt vor dem 03.01.2022. Das begrenzt die Ausgangsbasis, erklärt aber nicht allein den starken späteren Rückgang.", "",
        table(["Stufe", "Mittel", "Median", "Min", "Max", "p10", "p25", "p75", "p90"],
            [(key, *(round(stats[key][m], 4) for m in ("mean", "median", "minimum", "maximum", "p10", "p25", "p75", "p90")))
             for key in ("source_universe_count", "history_ready_count", "price_liquidity_history_ready_before_marketcap_count",
                         "price_liquidity_marketcap_ready_count", "pit_fundamental_count", "fundamentals_sufficient_count",
                         "quality_score_available_count", "quality_threshold_pass_count", "valuation_score_available_count",
                         "valuation_threshold_pass_count", "positive_ocf_count", "f_fundamental_rules_pass_count",
                         "technical_features_available_count", "momentum_gate_pass_count", "final_f_candidate_count")]), "",
        f"Null-Sessions: Quality {summary['sessions_with_zero_quality_scores']}, Valuation {summary['sessions_with_zero_valuation_scores']}, finale Kandidaten {summary['sessions_with_zero_final_candidates']}.", "",
        "## B. SEC Raw Coverage", "",
        f"{summary['daily_symbols_with_PIT_facts_by_end_2022']} Daily-Symbole mit mindestens einem bis Ende 2022 eingereichten lokalen Fact. Tatsächlich fundamental ausgewertet: {summary['fundamentals_evaluated_unique_symbols']} Symbole / {summary['fundamentals_evaluated_symbol_sessions']} Symbol-Sessions. Quality jemals verfügbar: {summary['quality_available_unique_symbols']}; Valuation: {summary['valuation_available_unique_symbols']}.", "",
        "RAW_SEC_DATA_MISSING wird nur für konkret fehlende lokal gemappte Inputs/vergleichbare Perioden mit filed<=T vergeben. Geprüft werden normalisierte Facts und erhaltene lokale Companyfacts-Payloads. Ein Fact für denselben Faktor ohne passende Rekonstruktion zählt konservativ als Ableitungsproblem. Fehlen gespeicherte vollständige Filings oder alternative unbekannte Tags, ist kein umfassender semantischer Rohdatenbeweis möglich. Die Kategorie beweist eine lokale Inputlücke, keinen versäumten Download und keine Wiederherstellbarkeit bei SEC.", "",
        "Anteile an nicht verfügbaren Scores (pro Score/Session je Kategorie einmal; Kategorien überlappen):", "",
        table(["Ursache", "Q Symbol-Sessions", "Q Anteil", "V Symbol-Sessions", "V Anteil"],
            [(category,
              summary["score_unavailability"]["quality"]["categories"][category]["symbol_sessions"],
              f"{100 * (summary['score_unavailability']['quality']['categories'][category]['share'] or 0):.2f}%",
              summary["score_unavailability"]["valuation"]["categories"][category]["symbol_sessions"],
              f"{100 * (summary['score_unavailability']['valuation']['categories'][category]['share'] or 0):.2f}%") for category in CATEGORIES]), "",
        "## C. Metric Construction", "",
        "Belegte Details stehen im Evidence-CSV: keine rekonstruierbaren diskreten Quartale, weniger als vier Quartale derselben Einheit, Abstände außerhalb 60–130 Tagen, unterschiedliche TTM-Enden für OCF/Capex bzw. EBIT/D&A, nicht verfügbare direkte oder zeitgleich zusammensetzbare Schulden und Share-Recency. Kein automatischer Schluss auf Tag-Mapping-Fehler, widersprüchliche Duplikate oder falsche Einheiten. Quellen bleiben unverändert.", "",
        "## D. Quality Bottleneck", "",
        "Mindestens 4 von 6 Faktoren. Negative OCF ist ein **verfügbarer** Faktor mit Score 0. Positive OCF ohne berechenbares Wachstum erhält nach bestehendem Code 50. Fehlende OCF-Wachstumsbasis ist daher nicht automatisch ein Quality-Availability-Defizit. Absolute Kurven ermöglichen sämtliche Quality-Faktoren auch ohne Peer-Kontext.", "",
        table(["Faktor", "betroffene Symbole", "fehlende Faktor-Sessions", "Raw", "Ableitung", "ökonomisch", "Peer", "Identität", "Unbekannt"],
            [(r["factor"], r["symbols_affected"], r["symbol_sessions_affected"], *(r[c] for c in FACTOR_COLUMNS.values()))
             for r in sorted(factors, key=lambda r: -r["symbol_sessions_affected"]) if r["domain"] == "quality"]), "",
        "## E. Valuation Bottleneck", "",
        "Mindestens 2 von 3 Faktoren. Relative P/E benötigt positives Unternehmens-P/E und positives Industry-Median. EV/EBITDA fällt bei nicht verfügbarem relativem Multiple auf EV/EBIT zurück. Ein fehlender EBITDA-Zweig allein macht den Valuation-Faktor nicht unverfügbar. FCF Yield kann ohne Peers über die absolute Kurve berechnet werden.", "",
        table(["Faktor", "betroffene Symbole", "fehlende Faktor-Sessions", "Raw", "Ableitung", "ökonomisch", "Peer", "Identität", "Unbekannt"],
            [(r["factor"], r["symbols_affected"], r["symbol_sessions_affected"], *(r[c] for c in FACTOR_COLUMNS.values()))
             for r in sorted(factors, key=lambda r: -r["symbol_sessions_affected"]) if r["domain"] == "valuation"]), "",
        "## F. Peer/SIC", "",
        "Unverändert mindestens 8 Peer-Beobachtungen; schmalste ausreichend große SIC4/3/2-Gruppe. Der Median benötigt selbst mindestens 8 positive Multiple-Werte in derselben ausgewählten Gruppe. Eine ausreichende allgemeine Gruppenbreite garantiert keine ausreichenden positiven P/E-/EV-Werte. Fehlende SIC wird getrennt als IDENTITY_CLASSIFICATION_GAP geführt. Historische Facts werden PIT genutzt; die heutige SIC-Zuordnung bleibt eine bekannte Einschränkung.", "",
        "## G. Economic Undefined", "",
        "Ausschließlich bestehende Semantik: nicht positive Wachstumsbasis/EPS/EBITDA/EBIT, Null-Umsatz, ungültiger effektiver Steuersatz, nicht positives durchschnittliches investiertes Kapital und nicht positive relative EV-Multiples. Negative FCF Yield und negative operative Marge bleiben berechenbar. Mehrere Ursachen pro Faktor sind möglich. Faktor-Sessions mit ökonomischem Gegenhindernis gelangen nicht in die Downloadliste, auch wenn zugleich ein Input fehlt.", "",
        "## H. Historical Universe", "",
        f"Heutiger Source-Universe: {universe['current_source_universe_count']}; 2022-Daily-Symbole außerhalb: {universe['symbols_with_2022_bars_not_current_source']}; davon mit PIT-Facts: {universe['omitted_symbols_with_PIT_facts']}; mit 300 Vorlauf-Bars und PIT-Facts: {universe['omitted_symbols_with_300_prior_bars_and_PIT_facts']}. Nicht automatisch fehlende Stammaktien: ETFs, heutige Klassifikation und fehlende SEC-Zuordnung werden im Universe-CSV getrennt sichtbar.", "",
        f"Lokale Identity-Epochen: {universe['identity_epoch_rows']}; Corporate-Action-Einträge: {len(universe['corporate_action_rows'])}. Historisch/heutige CIK-Konflikte: {json.dumps(universe['historical_current_CIK_conflicts'], ensure_ascii=False)}.", "",
        "Massive does not provide F Quality/Valuation fundamentals in current TraWalp production integration.", "",
        "Gespeicherte historische Massive-Probes sind ergänzende Identitätsevidenz. Ihre damaligen HTTP-Zähler sind keine Aufrufe dieses Audits. Weder Referenz-Snapshots noch einzelne Probe-Tage beweisen ein vollständiges historisches Universe. CURRENT_UNIVERSE_ONLY und NOT_SURVIVORSHIP_CLEAN bleiben bestehen.", "",
        "## I. Backfill Value", "",
        f"{summary['sec_backfill_candidate_symbols']} Symbole / {len(candidates)} Faktorzeilen als **plausible Prüf-/Backfill-Kandidaten**, nicht garantierte Reparaturen. Entscheidung: {summary['sec_backfill_decision']}. Deterministische Entscheidungsregel: {summary['decision_rule']}", "",
        "Priorität HIGH: kein erhaltenes Companyfacts-JSON und mindestens die Hälfte der Sessions mit ausschließlich Raw-Faktorhindernis sowie nicht verfügbarem Score oder Cap-Input; MEDIUM: kein JSON und mindestens eine nicht verfügbare Score-Session; sonst LOW. Das bewertet die Prüfpriorität, nicht erwartete Rendite oder garantierte Remote-Verfügbarkeit. Liste enthält CIK, vorhandene Factzahlen, Filing-Spanne, Metric-Coverage und konkret fehlende Inputs. Vorhandenes JSON mit denselben Auslassungen reduziert die Priorität.", "",
        f"Raw-only Availability-Obergrenze: Quality {summary['score_unavailability']['quality']['raw_only_availability_upper_bound_symbol_sessions']} Score-Sessions / {summary['score_unavailability']['quality']['raw_only_availability_upper_bound_symbols']} Symbole; Valuation {summary['score_unavailability']['valuation']['raw_only_availability_upper_bound_symbol_sessions']} / {summary['score_unavailability']['valuation']['raw_only_availability_upper_bound_symbols']}. Dies setzt hypothetisch vollständige valide Inputs voraus, keine Scorehöhe und kein Bestehen der F-Schwellen. Unternehmens-Backfill kann Peermediane indirekt beeinflussen; das wird nicht simuliert.", "",
        "Historical fundamentals improved != historical universe reconstructed. Delistings, Ticker-Reuse, historische Listings und Security-Klassifikationen werden durch SEC-Nachladen heutiger Symbole nicht behoben.", "",
        "## J. Next Step und bestehender Sync", "",
        "Capability PARTIAL: Symbol-Targeting ist bereits vorhanden. `sync --incremental --symbols ...` kann bereits aktuelle Unternehmen trotz historischer Lücken überspringen. `sync --full --symbols ...` startet zusätzlich Assets/Bars; daher kein geeigneter SEC-only-Aufruf. Die vorhandene Methode `DataSynchronizer.sync_sec_full(requested_symbols)` erzwingt ganze historische Companyfacts für die ausgewählten aktuellen tradablen Assets. Kein neuer Downloader nötig. Ein expliziter SEC-only-Force-CLI-Einstieg mit geprüftem Manifest wäre eine kleine spätere Erweiterung; hier nicht implementiert/ausgeführt. Direkte CIK-/Datumsselektion für ausgeschlossene historische Securities fehlt.", "",
        "Nächster begrenzter Task: priorisierte Kandidatenliste und kompakte Ableitungsbeispiele lokal reviewen; erst danach separat einen SEC-only-Pilot für eine kleine geprüfte Symbol-/CIK-Menge freigeben. Benötigt werden typischerweise 2019–2022 inklusive Vergleichsperioden; bestehender Companyfacts-Pfad lädt alle unterstützten historischen Beobachtungen. Kein automatischer Sync folgt aus diesem Audit.", "",
        "## Reproduktion, Grenzen und Tests", "",
        json.dumps(summary["reproduction"], ensure_ascii=False), "",
        "Die Gesamtabdeckung stammt aus heutigen lokalen normalisierten Daten und gegebenenfalls erhaltenen Payloads. Frühere lokale Datenversionen und nachträgliche SEC-Revisionen sind nicht vollständig rekonstruierbar. filed<=as_of wird strikt eingehalten; spätere Filing-/Bar-Grenzen der Symbolmatrix sind ausschließlich Inventarmetadaten. Frühe Marktausschlüsse werden als NOT_EVALUATED_BEFORE_F_SCORING dokumentiert. Sie werden nicht mit fehlender SEC-Coverage gleichgesetzt. Fehlende Peer-Unterstützung eines verfügbaren absoluten Quality-/FCF-Faktors senkt nicht dessen Verfügbarkeitsstatus.", "",
        "Fokussierte temporäre Fixtures: tests/test_f_data_gap_audit.py. Testausführung und Ergebnis werden in einem separaten neuen tests.log dokumentiert. Keine Produktionsdatenbank als Testfixture.", "",
        "STOP: kein Backfill, kein weiterer Backtest, kein Shadow-/Paper-Start."]
    (out / f"{STEM}_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
