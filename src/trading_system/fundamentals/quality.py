"""Point-in-time orchestration for a company's fundamental metric snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from trading_system.fundamentals.metrics import (
    balance_sheet_as_of,
    calculate_fundamental_metrics,
    current_and_prior_ttm,
)
from trading_system.models.fundamentals import (
    BalanceSheetSnapshot,
    FundamentalFact,
    FundamentalMetrics,
    TTMFundamentals,
)


@dataclass(frozen=True)
class FundamentalAccountingState:
    """Price-independent accounting state for one SEC filing information set."""

    current: TTMFundamentals
    prior: TTMFundamentals
    balance: BalanceSheetSnapshot
    prior_balance: BalanceSheetSnapshot


def analyze_fundamentals(
    facts: list[FundamentalFact], as_of: date, price: Decimal | None
) -> FundamentalMetrics:
    """Build a complete metric snapshot using only facts filed by ``as_of``."""

    return attach_market_price(accounting_state_as_of(facts, as_of), price)


def accounting_state_as_of(facts: list[FundamentalFact], as_of: date) -> FundamentalAccountingState:
    """Build the reusable accounting state for facts actually filed by ``as_of``."""

    current, prior = current_and_prior_ttm(facts, as_of)
    balance = balance_sheet_as_of(facts, as_of)
    prior_cutoff = balance.period_end - timedelta(days=300) if balance.period_end else None
    prior_balance = (
        balance_sheet_as_of(facts, as_of, end_on_or_before=prior_cutoff)
        if prior_cutoff
        else BalanceSheetSnapshot()
    )
    return FundamentalAccountingState(current, prior, balance, prior_balance)


def attach_market_price(
    state: FundamentalAccountingState, price: Decimal | None
) -> FundamentalMetrics:
    """Attach a session-specific price without rebuilding filing-driven accounting state."""

    return calculate_fundamental_metrics(
        state.current,
        state.prior,
        state.balance,
        state.prior_balance,
        price,
    )


__all__ = [
    "FundamentalAccountingState",
    "accounting_state_as_of",
    "analyze_fundamentals",
    "attach_market_price",
]
