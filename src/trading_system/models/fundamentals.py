"""SEC fundamental fact models preserving filing availability dates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class FundamentalFact(BaseModel):
    """One normalized XBRL observation; ``filed`` controls point-in-time use."""

    model_config = ConfigDict(frozen=True)
    cik: str
    symbol: str
    metric: str
    taxonomy: str = "us-gaap"
    tag: str
    value: Decimal
    unit: str
    period_start: date | None = None
    period_end: date
    filed: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    form: str
    accession_number: str | None = None
    frame: str | None = None


class CompanyIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    cik: str = Field(pattern=r"^\d{10}$")
    symbol: str
    name: str
    sic: str | None = None
    sic_description: str | None = None


class TTMFundamentals(BaseModel):
    """Flow values built from four discrete, point-in-time quarters."""

    model_config = ConfigDict(frozen=True)
    period_end: date | None = None
    available_date: date | None = None
    metric_period_ends: dict[str, date] = Field(default_factory=dict)
    metric_available_dates: dict[str, date] = Field(default_factory=dict)
    revenue: Decimal | None = None
    operating_income: Decimal | None = None
    net_income: Decimal | None = None
    eps_diluted: Decimal | None = None
    operating_cash_flow: Decimal | None = None
    capital_expenditures: Decimal | None = None
    free_cash_flow: Decimal | None = None
    depreciation_amortization: Decimal | None = None
    depreciation: Decimal | None = None
    amortization: Decimal | None = None
    ebit: Decimal | None = None
    ebitda: Decimal | None = None
    ebitda_formula: str | None = None
    tax_expense: Decimal | None = None
    interest_expense: Decimal | None = None


class BalanceSheetSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    period_end: date | None = None
    cash: Decimal | None = None
    total_debt: Decimal | None = None
    current_assets: Decimal | None = None
    current_liabilities: Decimal | None = None
    total_assets: Decimal | None = None
    total_equity: Decimal | None = None
    shares_outstanding: Decimal | None = None


class FundamentalMetrics(BaseModel):
    """Explainable fundamental and valuation metrics; missing stays ``None``."""

    model_config = ConfigDict(frozen=True)
    revenue_growth: float | None = None
    eps_growth: float | None = None
    operating_cash_flow_growth: float | None = None
    operating_cash_flow_positive: bool | None = None
    operating_margin: float | None = None
    effective_tax_rate: float | None = None
    roic: float | None = None
    debt_to_ebitda: float | None = None
    market_cap: Decimal | None = None
    pe: float | None = None
    enterprise_value: Decimal | None = None
    ev_to_ebitda: float | None = None
    ev_to_ebit: float | None = None
    fcf_yield: float | None = None


class FundamentalDebugItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    value: Decimal | None = None
    xbrl_concepts: tuple[str, ...] = ()
    source_filings: tuple[str, ...] = ()
    fiscal_periods: tuple[str, ...] = ()
    filed_dates: tuple[date, ...] = ()
    unit: str | None = None
    formula: str


class FundamentalDebugReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    as_of: date
    items: tuple[FundamentalDebugItem, ...]
