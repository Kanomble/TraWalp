# Historical Daily backfill and audit workflow

TraWalp uses one market-data pipeline and one bar identity for every timeframe:
`(symbol, timeframe, timestamp)`. Historical Daily bars remain `timeframe="1d"` and use the
configured Alpaca feed and adjustment mode. The backfill never deletes existing bars and writes
each provider batch through the existing SQLite bulk upsert.

## Backward and forward coverage

`sync-daily-history` accepts an inclusive date interval. The first verification for a symbol,
feed, adjustment mode, and target interval deliberately requests the complete provider window.
This repairs internal gaps even when local first and last bars already cover both edges. After that
successful verification, incremental runs request only the configured correction overlap. Expanding
the target interval or changing feed/adjustment triggers another complete verification.

Before an interval is verified, local edge diagnostics still describe backward and forward gaps:

```text
requested  2024-01-01 ----------------------------- 2026-08-12
local                    2025-04-21 ---------------- 2026-08-12
backward   2024-01-01 --- 2025-04-20
```

A small overlap permits later provider corrections without creating duplicates. Successful complete
verification ranges are recorded per symbol together with feed and adjustment mode. Disjoint ranges
are never represented as one continuous verified interval. Consequently, an interrupted run resumes
from durable batches, and known empty pre-listing ranges are not treated as failed requests on every
run. `--full-window` deliberately requests the complete interval again. IPOs and other young symbols
can validly start after the requested first session; they are reported as
`symbols_without_older_data`, not as sync errors.

The current tradable, SEC-identified company universe is selected automatically when `--symbols`
is omitted. SPY is always added independently for benchmark coverage. Explicit comma-separated
symbols are useful for a smoke test.

```powershell
# Small real-provider smoke
python -m trading_system.cli sync-daily-history `
  --start 2024-01-01 `
  --end 2025-04-20 `
  --symbols AAPL,MSFT,SPY

# Full current-company universe plus SPY
python -m trading_system.cli sync-daily-history `
  --start 2024-01-01 `
  --end 2026-08-12
```

The result reports requested/data-bearing symbols, absent older history, received/inserted/
updated bars, provider and SQLite timings, database sizes, global Daily and SPY bounds, and a
compact integrity check around the most common old/new boundaries. The integrity check verifies
ordering, duplicate timestamps, adjacent expected sessions, and extreme close discontinuities
that could indicate inconsistent adjustment modes.

## Warmup versus the tested period

Warmup bars do not expand a backtest. For this historical run the roles are:

```text
market data  2024-01-01 -------------------------------- 2026-08-12
warmup       2024-01-01 -------- 2025-04-20
backtest                         2025-04-21 ------------ 2026-08-12
```

`required_daily_warmup_sessions` centrally takes the maximum of
`data_quality.min_market_history_days` and the technical lookbacks for SMA, RSI, ATR, momentum,
52-week drawdown, and relative volume. These are trading sessions/bars, not calendar days. The
configured 300-session market-history gate remains unchanged. Historical features may read the
warmup prefix, while the BacktestEngine still derives trading sessions, entries, the equity curve,
returns, and benchmark metrics only from the requested test interval.

Measure the stricter safety diagnostic of bars strictly before the first test session with:

```powershell
python -m trading_system.cli daily-history-coverage --as-of 2025-04-21
```

It reports the current-company universe split into at least the required prior sessions, 250-299,
fewer than 250, and no prior history. A young listing is not expected to pass the 300-session gate.

## SPY benchmark history

The Daily backfill always requests SPY even when it is not a normal company-universe member.
`data-status` shows its first and last Daily dates separately. Backtests continue to slice SPY to
their actual start and end before calculating close-to-close return, CAGR, and drawdown; older SPY
warmup bars therefore cannot alter the requested-period benchmark return.

## Reproducible candidate-audit workflow

Run these steps in order after the provider smoke succeeds:

```powershell
# 1. Fill Daily history for the full current universe and SPY
python -m trading_system.cli sync-daily-history `
  --start 2024-01-01 `
  --end 2026-08-12

# 2. Inspect all timeframe bounds and the independent SPY bounds
python -m trading_system.cli data-status

# 3. Verify prior-session coverage at the test start
python -m trading_system.cli daily-history-coverage --as-of 2025-04-21

# 4. Re-run the unchanged production candidate funnel
python -m trading_system.cli audit-candidates `
  --start 2025-04-21 `
  --end 2026-08-12

# 5. Inspect the newly generated candidate set, then fetch only its 15m data
python -m trading_system.cli sync-intraday `
  --start 2025-04-21 `
  --end 2026-08-12 `
  --timeframes 15m `
  --candidates-report reports/candidate_audit_2025-04-21_2026-08-12_intraday_candidates.json

# 6. Only after the targeted 15m sync, run the long Strategy-F backtest
python -m trading_system.cli backtest `
  --start 2025-04-21 `
  --end 2026-08-12 `
  --variant C
```

The 15m command is intentionally separate: the Daily backfill and candidate audit never launch a
large intraday sync. A candidate report made before the completed Daily warmup must not be reused.

## Known limits

- The current tradable universe introduces survivorship bias and is not historical membership.
- IPOs naturally have less history; no listing dates or bars are invented.
- Daily OHLC does not determine the ordering of intrabar events.
- Point-in-time fundamentals may exist while Quality or Valuation inputs remain incomplete.
- This workflow does not reconstruct historical delistings, ticker changes, or issuer ownership.
- Provider availability and account entitlements can still limit individual histories.
