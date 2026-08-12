"""External-data synchronization; calculations remain in separate domain modules."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from trading_system.data.alpaca_client import AlpacaDataClient
from trading_system.data.database import Database
from trading_system.data.market_sessions import (
    full_history_request_window,
    latest_completed_trading_session,
)
from trading_system.data.sec_client import SecClient
from trading_system.data.xbrl_parser import parse_company_facts
from trading_system.models.fundamentals import CompanyIdentity

LOGGER = logging.getLogger(__name__)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


class DataSynchronizer:
    def __init__(
        self,
        database: Database,
        alpaca: AlpacaDataClient,
        sec: SecClient,
        *,
        market_data_days: int = 320,
        market_data_batch_size: int = 200,
    ) -> None:
        if market_data_batch_size <= 0:
            raise ValueError("market_data_batch_size must be positive")
        self.database = database
        self.alpaca = alpaca
        self.sec = sec
        self.market_data_days = market_data_days
        self.market_data_batch_size = market_data_batch_size

    def sync(self, requested_symbols: list[str] | None = None) -> dict[str, int]:
        assets = self.alpaca.list_tradable_us_equities()
        self.database.upsert_assets(assets)
        available = {asset.symbol for asset in assets}
        symbols = sorted(available if not requested_symbols else available & set(requested_symbols))
        counts = {
            "assets": len(assets),
            "symbols": len(symbols),
            "market_symbols": 0,
            "facts": 0,
            "bars": 0,
            "errors": 0,
        }
        ticker_map = self.sec.ticker_to_cik()

        for symbol in symbols:
            cik = ticker_map.get(symbol)
            if cik is None:
                LOGGER.warning("No SEC CIK mapping symbol=%s", symbol)
                continue
            try:
                submissions = self.database.cached_sec_payload(cik, "submissions")
                if submissions is None:
                    submissions = self.sec.submissions(cik)
                    self.database.cache_sec_payload(cik, "submissions", submissions)
                company = CompanyIdentity(
                    cik=cik,
                    symbol=symbol,
                    name=str(submissions.get("name") or symbol),
                    sic=str(submissions["sic"]) if submissions.get("sic") else None,
                    sic_description=submissions.get("sicDescription"),
                )
                self.database.upsert_company(company)
                payload = self.database.cached_sec_payload(cik, "companyfacts")
                if payload is None:
                    payload = self.sec.company_facts(cik)
                    self.database.cache_sec_payload(cik, "companyfacts", payload)
                fact_count = self.database.upsert_facts(parse_company_facts(payload, symbol))
                counts["facts"] += fact_count
                LOGGER.info("SEC data updated symbol=%s facts=%d", symbol, fact_count)
            except Exception:
                counts["errors"] += 1
                LOGGER.exception("Failed SEC update symbol=%s cik=%s", symbol, cik)

        company_symbols = {company.symbol for company in self.database.list_tradable_companies()}
        market_symbols = sorted(company_symbols & set(symbols))
        counts["market_symbols"] = len(market_symbols)
        LOGGER.info(
            "Market data universe symbols=%d skipped_without_sec_identity=%d",
            len(market_symbols),
            len(symbols) - len(market_symbols),
        )

        completed_session = latest_completed_trading_session()
        full_start, end = full_history_request_window(completed_session, self.market_data_days)
        starts: dict[datetime, list[str]] = defaultdict(list)
        for symbol in market_symbols:
            latest = self.database.latest_bar_timestamp(symbol)
            # Refetch a small overlap so vendor corrections are applied by upsert.
            start = latest - timedelta(days=7) if latest else full_start
            starts[start].append(symbol)
        for start, grouped_symbols in starts.items():
            for batch_symbols in _chunks(grouped_symbols, self.market_data_batch_size):
                try:
                    bars = self.alpaca.daily_bars(batch_symbols, start, end)
                    counts["bars"] += self.database.upsert_bars(bars)
                    LOGGER.info(
                        "Alpaca bars updated symbols=%d bars=%d", len(batch_symbols), len(bars)
                    )
                except Exception:
                    counts["errors"] += 1
                    LOGGER.exception(
                        "Failed Alpaca bars update symbols=%d first_symbol=%s last_symbol=%s",
                        len(batch_symbols),
                        batch_symbols[0],
                        batch_symbols[-1],
                    )
        return counts
