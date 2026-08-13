from pathlib import Path

import pytest

from trading_system.config import load_settings


def test_config_loads_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "SEC_USER_AGENT"):
        monkeypatch.setenv(name, "")
    load_settings.cache_clear()
    settings = load_settings(Path("config/strategy.yaml"))
    assert settings.trading_mode == "paper"
    assert settings.enable_order_submission is False
    assert settings.strategy.universe.market_data_days >= 300
    assert settings.strategy.sec.companyfacts_unavailable_ttl_days == 7
    with pytest.raises(ValueError, match="ALPACA_API_KEY"):
        settings.require_alpaca_credentials()


def test_live_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    load_settings.cache_clear()
    with pytest.raises(ValueError):
        load_settings(Path("config/strategy.yaml"))
