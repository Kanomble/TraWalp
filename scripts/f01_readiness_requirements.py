"""Evidence matrix for the unchanged F/configured/C1 consumers."""

from datetime import date, timedelta

from f01_historical_50_preflight_run import preceding_sessions

from trading_system.backtest.research_registry import FROZEN_CHAMPION_F, validate_champion_config
from trading_system.data.market_sessions import daily_warmup_start, required_daily_warmup_sessions

BT = "src/trading_system/backtest/"
FUN = "src/trading_system/fundamentals/"
STR = "src/trading_system/strategy/"
ENGINE = BT + "engine.py"
FEATURES = BT + "features.py"
METRICS = FUN + "metrics.py"
SCORING = STR + "scoring.py"
SCREEN = STR + "screener.py"
GATE = BT + "screen_strategies.py"
REGISTRY = BT + "research_registry.py"
SESSIONS = "src/trading_system/data/market_sessions.py"


def requirements(config):
    validate_champion_config(config)
    minimum = required_daily_warmup_sessions(config)
    load = max(minimum, config.universe.market_data_days)
    research_start = date(2010, 1, 1)
    inputs = []

    def add(kind, name, code, mandatory, window, granularity, source, pit, missing):
        inputs.append(
            {
                "kind": kind,
                "name": name,
                "consumers": code,
                "requirement": mandatory,
                "window": window,
                "granularity": granularity,
                "source": source,
                "pit": pit,
                "missing_impact": missing,
            }
        )

    sec = "Original-qualified SEC observations with exact taxonomy, context, unit and scope"
    pit = "Actual availability at decision; historical legacy path filters by filed day"
    ttm = (
        "Four aligned discrete quarters (derive YTD/Q4 only per code); current and prior for growth"
    )
    add(
        "fundamental",
        "Operating cash flow",
        [SCREEN, METRICS, SCORING],
        "REQUIRED",
        ttm,
        "Fiscal quarter",
        sec,
        pit,
        "Missing or nonpositive TTM OCF blocks eligibility",
    )
    add(
        "fundamental",
        "Issuer shares",
        [FEATURES, METRICS, SCREEN],
        "REQUIRED",
        "Latest admissible, capped at 460 days",
        "Instant",
        sec,
        pit,
        "Market cap unavailable; minimum market cap gate cannot clear",
    )
    quality = {
        "Revenue and growth": ("Revenue", "Quality growth and operating-margin denominator"),
        "Diluted EPS and EPS growth": ("Diluted EPS", "Quality growth and P/E"),
        "Operating margin": ("Operating income and revenue", "Quality operating-margin score"),
        "ROIC and invested capital": (
            "Operating income, tax, net income and debt/equity/cash",
            "ROIC component unavailable",
        ),
        "Debt / EBITDA": ("Debt and EBITDA", "Leverage component unavailable"),
    }
    for name, (parts, impact) in quality.items():
        add(
            "fundamental",
            name,
            [METRICS, SCORING],
            "SCORE_COMPONENT",
            ttm
            + (
                "; current/prior aligned balance, prior at least 300 days back"
                if name.startswith("ROIC")
                else ""
            ),
            "Quarter and/or instant",
            sec + "; " + parts,
            pit,
            impact,
        )
    add(
        "fundamental",
        "Relative P/E",
        [METRICS, FUN + "peers.py", SCORING],
        "SCORE_COMPONENT",
        "Current EPS TTM and session close",
        "Quarter and Daily",
        sec,
        pit,
        "Relative P/E unavailable without usable denominator/peer median",
    )
    add(
        "fundamental",
        "Relative EV/EBITDA or EV/EBIT",
        [METRICS, SCORING],
        "SCORE_COMPONENT",
        "Current operating income, D&A and current debt, cash and market cap",
        "Quarter and instant",
        sec,
        pit,
        "EBIT route only under implemented fallback; missing EV blocks both",
    )
    add(
        "fundamental",
        "Free cash flow yield",
        [METRICS, SCORING],
        "SCORE_COMPONENT",
        "OCF and CapEx on compatible TTM periods plus market cap",
        "Quarter and Daily",
        sec,
        pit,
        "FCF yield component unavailable",
    )
    add(
        "fundamental",
        "Balance-sheet auxiliary fields",
        [METRICS],
        "DIAGNOSTIC",
        "Latest admissible period",
        "Instant",
        sec,
        pit,
        "Total assets/current assets/current liabilities do not constitute a standalone F gate",
    )
    add(
        "scoring",
        "Quality/valuation availability and peer population",
        [SCREEN, SCORING, ENGINE],
        "REQUIRED",
        "At decision; minimum 4/6 quality and 2/3 valuation; peers per config",
        "Session × universe",
        "As-of issuer SIC, eligible peer population and comparable inputs",
        pit,
        "Insufficient score coverage blocks; other missing components affect score weights",
    )
    add(
        "market",
        "Signal history, liquidity and price",
        [FEATURES, SCREEN, GATE, SCORING],
        "REQUIRED",
        f"{minimum} bars minimum; {load} load/preroll; SMA20/50/200, SMA20 slope 5, "
        "252-bar high, momentum 5/20/63/126, volume20, RSI14, volatility20 and ATR14",
        "Completed Daily HLCV",
        "Historical U0 bars retain unknown feed/adjustment; new explicit SIP/all scenario separate",
        "Same complete XNYS session, as-of adjustment",
        "Missing warm-up, stale bars or required technical/score input block candidates",
    )
    add(
        "market",
        "Opportunity and timing score components",
        [SCORING, FEATURES],
        "SCORE_COMPONENT",
        "RSI14/recovery, SMA20/50, slope, 5/20/63 returns, 20-session "
        "volatility and relative volume, and 252-session drawdown",
        "Completed Daily HLCV",
        "Same security, feed and price adjustment across each lookback",
        "Historical session close; volume compared to prior observations",
        "Available weights are normalized; unavailable scores or scores below cutoff gate F",
    )
    add(
        "market",
        "Technical F candidate requirements",
        [GATE, SCREEN, ENGINE],
        "REQUIRED",
        "SMA50 and SMA200 below close; SMA20 rising; positive momentum126; drawdown >= -10%; "
        "price >= 5 USD and ADV20 >= 10 million USD",
        "Completed Daily HLCV",
        "Regular XNYS sessions, complete warm-up and consistent currency and adjustment",
        "Eligibility evaluated at completed decision session",
        "The frozen F technical or universe filter rejects the candidate",
    )
    add(
        "market",
        "SPY validation benchmark",
        [SESSIONS, BT + "data_integrity.py", ENGINE],
        "REQUIRED_VALIDATION",
        f"{minimum} preceding sessions",
        "Completed Daily",
        "Same independently bound feed/adjustment contract",
        "Calendar before first decision",
        "Blocks validation parity; frozen C1 selects no regime overlay",
    )
    add(
        "market",
        "Execution, marks, stops and targets",
        [ENGINE, REGISTRY],
        "REQUIRED",
        "Next open; Daily OHLC path, gap handling and hard hold 10 sessions",
        "Daily OHLC",
        "Same security and economically consistent adjustment; no cross-feed fill",
        "Stop-first for ambiguous same-bar events; no future bar information in signal",
        "Missing execution bar prevents supported fill, valuation or exit",
    )
    add(
        "market",
        "Intraday",
        [ENGINE, REGISTRY],
        "NOT_REQUIRED_FOR_C1",
        "None",
        "15m/other for separate setups only",
        "Separate feed contract",
        "Separate scope",
        "No C1 blockage; cannot independently order Daily intrabar events",
    )
    add(
        "security",
        "Issuer, peer SIC and listing/tradability",
        [FEATURES, SCREEN, ENGINE],
        "REQUIRED",
        "Effective and known-at each decision and exit",
        "Security epoch",
        "Confirmed issuer CIK, listed share class and exchange/session; separate from symbol",
        "Point witnesses do not extend to intervals; membership is time-bounded",
        "Expanded historical universe and survivor-free peer population unqualified",
    )
    add(
        "security",
        "Corporate actions and terminal exits",
        [ENGINE, "src/trading_system/backtest/data_integrity.py"],
        "REQUIRED",
        "Effective and known-at split, symbol change, merger and delisting",
        "Event and security epoch",
        "Listing continuity, adjusted price/EPS/shares and exit terms",
        "Separate effective/announcement times and witnesses",
        "Adjustment leakage, incorrect price/share basis or missing exit",
    )
    return {
        "strategy": FROZEN_CHAMPION_F.production_label,
        "inputs": inputs,
        "minimum_market_history": minimum,
        "daily_load_preroll": load,
        # The frozen helper is intentionally preserved. Bind exact strictly prior XNYS
        # sessions independently for acquisition, since calendar window semantics differ.
        "frozen_loader_start_2010": daily_warmup_start(research_start, load).isoformat(),
        "start_2010_warmup": preceding_sessions(research_start, load)[0].isoformat(),
        "nominal_fundamental_load_start_2010": (
            date(2010, 1, 1) - timedelta(days=4 * 366)
        ).isoformat(),
        "thresholds": {
            name: getattr(config, name).model_dump(mode="json")
            for name in (
                "backtest",
                "data_quality",
                "filters",
                "peers",
                "risk",
                "portfolio",
                "technical",
                "scores",
                "screen_strategies",
            )
        },
        "configured_historical_feed_intent": config.universe.market_data_feed,
        "configured_adjustment_intent": config.universe.market_data_adjustment,
    }
