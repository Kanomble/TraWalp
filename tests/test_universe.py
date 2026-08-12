from decimal import Decimal

from trading_system.config import UniverseConfig
from trading_system.data.universe import UniverseSnapshot, passes_universe_filters


def snapshot(**changes) -> UniverseSnapshot:
    values = dict(
        symbol="TEST",
        latest_price=Decimal("50"),
        average_price_20d=Decimal("50"),
        average_volume_20d=Decimal("300000"),
        shares_outstanding=Decimal("30000000"),
        sic="3571",
    )
    values.update(changes)
    return UniverseSnapshot(**values)


def test_market_cap_fallback_and_dollar_volume_filter() -> None:
    assert passes_universe_filters(snapshot(), UniverseConfig())
    assert not passes_universe_filters(snapshot(average_volume_20d=Decimal("10")), UniverseConfig())


def test_missing_shares_are_not_treated_as_zero_or_invented() -> None:
    assert not passes_universe_filters(snapshot(shares_outstanding=None), UniverseConfig())


def test_financials_and_reits_are_excluded() -> None:
    assert not passes_universe_filters(snapshot(sic="6021"), UniverseConfig())
    assert not passes_universe_filters(snapshot(sic="6798"), UniverseConfig())
    assert not passes_universe_filters(
        snapshot(sic="6798"), UniverseConfig(exclude_financials=False, exclude_reits=True)
    )
