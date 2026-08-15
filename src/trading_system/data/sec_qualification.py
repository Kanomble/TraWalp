"""Read-only diagnosis of SEC Company Facts period-context preservation."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trading_system.data.database import Database
from trading_system.data.xbrl_parser import parse_company_facts
from trading_system.models.fundamentals import FundamentalFact


class SecContextQualificationDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    cik: str
    raw_cache_available: bool
    parsed_facts: int = Field(default=0, ge=0)
    existing_facts: int = Field(default=0, ge=0)
    missing_context_facts: int = Field(default=0, ge=0)
    missing_period_start_contexts: int = Field(default=0, ge=0)
    additional_discrete_quarter_contexts: int = Field(default=0, ge=0)
    parse_error: str | None = None


class SecContextQualificationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbols_requested: int = Field(ge=0)
    facts_in_database: int = Field(ge=0)
    cached_symbols_analyzed: int = Field(ge=0)
    symbols_without_raw_cache: int = Field(ge=0)
    parsed_facts: int = Field(ge=0)
    missing_context_facts: int = Field(ge=0)
    missing_period_start_contexts: int = Field(ge=0)
    additional_discrete_quarter_contexts: int = Field(ge=0)
    existing_multi_start_groups: int = Field(ge=0)
    parse_errors: int = Field(ge=0)
    reconstruction_complete: bool
    repair_recommended: bool
    detail_records: int = Field(ge=0)
    details_truncated: bool = False
    details: tuple[SecContextQualificationDetail, ...] = ()
    warnings: tuple[str, ...] = ()


def qualify_sec_contexts(
    database: Database,
    symbols: Iterable[str] | None = None,
    *,
    detail_limit: int = 500,
) -> SecContextQualificationReport:
    """Reparse retained raw payloads and compare their normalized fact identities."""

    if detail_limit < 0:
        raise ValueError("detail_limit must not be negative")
    requested = None if symbols is None else {
        item.strip().upper() for item in symbols if item.strip()
    }
    details: list[SecContextQualificationDetail] = []
    facts_in_database = 0
    parsed_facts = 0
    missing_context_facts = 0
    missing_period_start_contexts = 0
    additional_quarter_contexts = 0
    existing_multi_start_groups = 0
    parse_errors = 0
    cached_analyzed = 0
    detail_records = 0
    with database.read_only() as connection:
        company_rows = connection.execute(
            "SELECT cik,symbol FROM companies ORDER BY symbol"
        ).fetchall()
        companies = {
            str(row["symbol"]): str(row["cik"]).zfill(10) for row in company_rows
        }
        selected = (
            companies
            if requested is None
            else {symbol: companies[symbol] for symbol in sorted(requested & companies.keys())}
        )
        for symbol, cik in selected.items():
            rows = connection.execute(
                """SELECT metric,tag,unit,period_start,period_end,filed,
                accession_number,frame FROM fundamental_facts WHERE cik=?""",
                (cik,),
            ).fetchall()
            facts_in_database += len(rows)
            existing = {_row_key(row) for row in rows}
            existing_starts: dict[tuple[str, ...], set[str]] = defaultdict(set)
            for key in existing:
                existing_starts[_base_key(key)].add(key[3])
            existing_multi_start_groups += sum(
                len(starts) > 1 for starts in existing_starts.values()
            )
            cache = connection.execute(
                """SELECT payload FROM raw_sec_cache
                WHERE cik=? AND endpoint='companyfacts'""",
                (cik,),
            ).fetchone()
            if cache is None:
                detail_records += 1
                detail = SecContextQualificationDetail(
                    symbol=symbol,
                    cik=cik,
                    raw_cache_available=False,
                    existing_facts=len(existing),
                )
                _retain_detail(details, detail, detail_limit)
                continue
            cached_analyzed += 1
            try:
                payload = json.loads(str(cache["payload"]))
                if not isinstance(payload, dict):
                    raise ValueError("raw companyfacts payload is not a JSON object")
                parsed = parse_company_facts(payload, symbol)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                parse_errors += 1
                detail_records += 1
                detail = SecContextQualificationDetail(
                    symbol=symbol,
                    cik=cik,
                    raw_cache_available=True,
                    existing_facts=len(existing),
                    parse_error=f"{type(exc).__name__}: {exc}",
                )
                _retain_detail(details, detail, detail_limit)
                continue
            parsed_facts += len(parsed)
            missing = [fact for fact in parsed if _fact_key(fact) not in existing]
            missing_contexts = [
                fact
                for fact in missing
                if fact.period_start is not None
                and _base_key(_fact_key(fact)) in existing_starts
                and fact.period_start.isoformat()
                not in existing_starts[_base_key(_fact_key(fact))]
            ]
            discrete = [
                fact
                for fact in missing_contexts
                if fact.fiscal_period in {"Q1", "Q2", "Q3"}
            ]
            missing_context_facts += len(missing)
            missing_period_start_contexts += len(missing_contexts)
            additional_quarter_contexts += len(discrete)
            if missing:
                detail_records += 1
                detail = SecContextQualificationDetail(
                    symbol=symbol,
                    cik=cik,
                    raw_cache_available=True,
                    parsed_facts=len(parsed),
                    existing_facts=len(existing),
                    missing_context_facts=len(missing),
                    missing_period_start_contexts=len(missing_contexts),
                    additional_discrete_quarter_contexts=len(discrete),
                )
                _retain_detail(details, detail, detail_limit)

    symbols_requested = len(selected)
    symbols_without_cache = symbols_requested - cached_analyzed
    reconstruction_complete = symbols_without_cache == 0 and parse_errors == 0
    repair_recommended = bool(
        missing_context_facts or symbols_without_cache or parse_errors
    )
    warnings = []
    if symbols_without_cache:
        warnings.append(
            "Raw Company Facts are unavailable for part of the requested universe; historical "
            "parser losses cannot be reconstructed from the local database alone."
        )
    if missing_context_facts:
        warnings.append(
            "Retained raw Company Facts contain normalized contexts not present in the fact table."
        )
    return SecContextQualificationReport(
        symbols_requested=symbols_requested,
        facts_in_database=facts_in_database,
        cached_symbols_analyzed=cached_analyzed,
        symbols_without_raw_cache=symbols_without_cache,
        parsed_facts=parsed_facts,
        missing_context_facts=missing_context_facts,
        missing_period_start_contexts=missing_period_start_contexts,
        additional_discrete_quarter_contexts=additional_quarter_contexts,
        existing_multi_start_groups=existing_multi_start_groups,
        parse_errors=parse_errors,
        reconstruction_complete=reconstruction_complete,
        repair_recommended=repair_recommended,
        detail_records=detail_records,
        details_truncated=detail_records > len(details),
        details=tuple(details),
        warnings=tuple(warnings),
    )


def _fact_key(fact: FundamentalFact) -> tuple[str, ...]:
    return (
        fact.metric,
        fact.tag,
        fact.unit,
        fact.period_start.isoformat() if fact.period_start else "",
        fact.period_end.isoformat(),
        fact.filed.isoformat(),
        fact.accession_number or "",
        fact.frame or "",
    )


def _row_key(row: Any) -> tuple[str, ...]:
    return tuple(str(row[index] or "") for index in range(8))


def _base_key(key: tuple[str, ...]) -> tuple[str, ...]:
    return (*key[:3], *key[4:])


def _retain_detail(
    details: list[SecContextQualificationDetail],
    detail: SecContextQualificationDetail,
    limit: int,
) -> None:
    if len(details) < limit:
        details.append(detail)
        return
    if not detail.raw_cache_available:
        return
    for index in range(len(details) - 1, -1, -1):
        if not details[index].raw_cache_available:
            details[index] = detail
            return
