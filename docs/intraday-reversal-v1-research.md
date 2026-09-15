# Intraday Reversal V1: frozen independent research hypothesis

`research-intraday-reversal-v1` / `INTRADAY-REVERSAL-V1-15M-LONG` is **REJECTED** research.
This hypothesis was frozen before any Intraday Reversal V1 trade outcomes were evaluated.
Synthetic specification tests are not historical outcome evaluation.

## Final research decision

Intraday Reversal V1 does not demonstrate a robust tradeable cross-sectional
mean-reversion edge. The aggregate gross edge is approximately zero and does not
survive the frozen transaction-cost assumptions.

The completed validation for **2024-01-02 through 2026-08-12**, supplied with this
research decision, used **65,500 candidate symbol-sessions**:

| Measure | Result |
| --- | --- |
| Signals | 6,096 |
| Executed trades | 5,790 |
| Gross expectancy | Approximately +0.00515% per trade |
| Gross profit factor | Approximately 1.0067 |
| Net expectancy | Approximately -0.09480% per trade |
| Net profit factor | Approximately 0.8836 |
| Win rate | Approximately 48.12% |
| Modeled cost drag | Approximately 0.09996% per trade |

Signal disposition: `EXECUTED_SESSION_CLOSE=5,790`,
`PROVIDER_CONFIRMED_ABSENT=297`, `NO_ENTRY_NO_NEXT_BAR=9`.
`SESSION_UNOBSERVABLE_CROSS_SECTION` affected 100 candidate rows in one session.
All calendar-year results and all three chronological thirds were net negative.
Stronger first-hour loser extremity did not produce a clean monotonic reversal effect.

No parameters were changed after outcome inspection. No profitable symbol, rank or
month slice is promoted into Reversal V1.1. There is no bottom-5%, 5%-10%, selected-symbol,
May-2026 or lower-slippage variant. The implementation and completed candidate manifest,
coverage, events, sessions, trades, monthly/yearly tables, chronological thirds,
signal-rank buckets, symbol concentration and summary remain unchanged research evidence.

Explicit reproduction remains available, without recommendation or champion promotion.
Pre-rejection manifests with `status=ACTIVE` remain compatible when all other contract
and source inputs match; no historical artifact is rewritten or migrated. The definition
and workflow below record the original hypothesis and support reproduction, not a new
recommendation to continue this rejected family. No historical job was rerun for this decision.

**Research question:** Among the day's most liquid US-listed instruments, extreme
relative losers during the first trading hour may exhibit short-term mean reversion
during the remainder of the regular session. Does that raw signal have positive
intraday expectancy after modeled costs?

This is an independent cross-sectional reversal study. ORB V1 is separately
[REJECTED](orb-v1-research.md); its implementation and completed evidence remain
reproducible. Reversal does not modify ORB or the frozen **F/configured/C1** champion.
Positive results would not automatically promote this family to champion or live trading.

## Frozen universe

`REVERSAL_LIQUID_TOP100_US_EQUITY` uses `CURRENT_ALPACA_TRADABLE_US_EQUITY`:

1. Load current locally stored Alpaca tradable assets once. Require `tradable=true`
   and stored `asset_class=US_EQUITY`. CRYPTO, UNKNOWN and all other classes are excluded.
2. Require 20 valid, completed official Daily sessions through **T-1**, including a
   previous close at least `universe.min_price` (currently $5) and
   ADV20 at least `universe.min_avg_dollar_volume_20d` (currently $10M).
   ADV20 is the arithmetic mean of Daily `close * volume` over those 20 sessions.
3. Sort ADV20 descending, then symbol ascending. Freeze the first 100 per session
   before reading intraday coverage or prices. Fewer than 100 may qualify.

T's Daily bar, current snapshots, future volume and intraday outcomes never enter
Daily selection. ETFs, ETPs and other Alpaca US_EQUITY instruments may participate.
There are no symbol/name, SEC identity, CIK, SIC, REIT, sector or fundamental filters.
There is no replacement by Daily rank 101 when a frozen candidate is unobservable.

`CURRENT_UNIVERSE_ONLY` and `NOT_SURVIVORSHIP_CLEAN` remain explicit limitations.
The study does not reconstruct historical listing membership. Later listings may
legitimately lack the required Daily history.

Alpaca `tradable=true` is a data/research universe property and does not prove that an
instrument can be purchased through a German/EU broker. German/EU execution eligibility
is **NOT_EVALUATED**. A future execution-universe layer is outside this hypothesis.

## Native data and the decision

Only actual native **15m** regular-session bars are admitted. The official XNYS calendar
in `America/New_York` defines regular trading hours (normally 09:30-16:00 ET), including
holidays, daylight-saving changes and official early closes. No resampling,
interpolation, synthetic bars, Daily substitution or extended-hours substitution occurs.
The configured market-data feed and adjustment are used unchanged.

The first hour is exactly the four native bars starting at 09:30, 09:45, 10:00 and
10:15 ET. All four must be available and valid. The decision is made at the fourth
bar's completion, normally **10:30 ET**:

```text
first_hour_return = close_of_10:15_10:30_bar / open_of_09:30_09:45_bar - 1
```

An instrument missing any required first-hour bar is unobservable; the window is
never reconstructed. Early-close sessions use the same first four bars.

Rank first-hour-observable members of the frozen Daily Top-100 by
`first_hour_return ASC, symbol ASC`. Let `cross_section_size` be their count.
Require **at least 80**; otherwise label the entire session
`SESSION_UNOBSERVABLE_CROSS_SECTION` and create no signals.

For a qualifying session, select the weakest ranks:

```text
signal_count = max(1, floor(cross_section_size * 0.10))
100 -> 10; 97 -> 9; 83 -> 8
cross_section_percentile = 1-based cross-sectional rank / cross_section_size
```

There is no absolute-return threshold: even positive-return instruments can be
relative losers. Ranking does not inspect later prices, session-close data, MFE/MAE,
ORB signals, F scores, fundamentals or market benchmarks.

## Frozen execution and costs

Signals are **long only**, with one attempt per instrument-session. Enter at the
next actual native 15m bar open after the fourth bar completes (normally the 10:30
bar open). The fourth bar's close is not a fill. No next native bar produces
`NO_ENTRY_NO_NEXT_BAR`; there is no delayed retry or alternate fill.

Exit at the final actual native regular-session bar close, including official
early closes. `SESSION_CLOSE` is the only exit. There is **no stop**, profit target,
trailing stop, partial exit, intermediate time exit, VWAP exit or overnight hold.
The ORB stop is not reused.

```text
entry_fill = entry_reference * 1.0005
exit_fill  = exit_reference  * 0.9995
commission = 0
gross_return = exit_reference / entry_reference - 1
net_return   = exit_fill / entry_fill - 1
modeled_cost = gross_return - net_return
```

Economics have no CLI overrides. Every execution is an independent unit-entry-notional
observation. No position sizing, allocation, portfolio equity curve, compounding,
portfolio drawdown, CAGR or portfolio Sharpe is defined.

## Local coverage, manifests and performance

`preflight-intraday-reversal-v1` is strictly local and read-only. It freezes the
Daily candidates and ranks, checks exact full-session native 15m timestamp grids,
and exports summary/coverage, a dedicated `*_reversal_candidates.json` manifest and
sync-compatible `*_intraday_requirements.json`. It does **not** rank first-hour
returns, generate signals or evaluate trade outcomes. Existing outputs are never overwritten.

Coverage uses `REQUIRED_PRESENT`, `LOCAL_MISSING_FETCHABLE`,
`PROVIDER_CONFIRMED_ABSENT` and `PROVIDER_CHECK_FAILED`. Unresolved fetchable gaps or
failed checks block validation. Provider absence must be supported by compatible
locally recorded provider observations; missing data is never assumed absent.
Malformed/off-grid/duplicate native rows cannot be excused by absence evidence.

Full native session coverage is required for an executable outcome. A candidate
whose first hour is complete remains in the decision cross-section even if a later
bar is provider-confirmed absent. If selected, its signal and rank are retained,
but the incomplete outcome is reported as unexecuted `PROVIDER_CONFIRMED_ABSENT`
(or `NO_ENTRY_NO_NEXT_BAR` if no next native bar exists). There is no signal replacement.
This prevents future observability from altering first-hour selection.

Manifest **version 1** belongs exclusively to the reversal family. It binds the
research family/ID, exact dates, complete frozen strategy/universe definitions,
first-hour formula, cross-section threshold, signal fraction, costs, feed,
adjustment and regular-hours flag. It records all selected candidates and Daily ranks.
The source digest covers current asset symbol/class/tradability, calendar basis and
the exact Daily selection rows for every eligible asset, including unselected assets
that could change Top-100 membership. Missing/changed sources or configuration fail
closed with `REVERSAL_CANDIDATE_MANIFEST_MISMATCH`. Integrity hashes detect accidental
changes; they are not cryptographic signatures from a trusted third party.

`validate-intraday-reversal-v1` requires exactly one of `--candidate-manifest PATH`
(normal reproducible use) or `--rediscover-candidates` (explicit research-only discovery).
Manifest reuse verifies source evidence without rediscovering/reranking Daily candidates.
Native data and provider-absence observations are requalified, allowing explicit gap
remediation after preflight. Validation never constructs a provider or synchronizes data.

Shared primitives cover local liquid Daily selection, source hashing, cached timestamp
grids and native coverage, not trading economics. Asset membership loads once; Daily
reads use symbol batches and rolling ADV20. Native coverage retains one indexed native
SELECT per batch plus a single provider-observation load. A run-owned temporary
`NativeEntrySessions` spool has a bounded cache. Simulation holds at most one Top-100
session's native cross-section in memory and performs **zero SQLite queries**.

## Reports and diagnostic boundaries

Validation exports terminal events (one per Daily candidate), executed trades,
session observations, monthly/yearly tables, three chronological thirds, symbol
concentration and signal-rank buckets. All events carry Daily rank, cross-section
size/rank/percentile, first-hour open/close/return, decision timestamp, signal flag,
entry/exit references/fills/timestamps, gross/net returns, modeled cost, MFE/MAE,
holding minutes and terminal status/reason. Unavailable fields remain null.

Summary metrics include eligible and first-hour-observable symbol-sessions,
qualified/unobservable cross-section sessions, signals, executions, gross/net expectancy
and medians, gross/net profit factors, net win rate and average winner/loser,
first-hour return mean/median, mean rank percentile, MFE/MAE, holding time and cost drag.
Trade metrics use executed observations; signal and unexecuted counts remain explicit.
Profit factors are null without losses. Returns, rates and percentiles are fractions.
MFE/MAE use all holding-bar reference-price extrema, clamped around zero.
Holding time ends at the final native bar's completion.

Chronological thirds partition requested XNYS sessions, including zero-trade sessions.
Symbol concentration includes trade shares and signed unit-notional return contributions;
return shares are null when aggregate net return is nonpositive. Diagnostic rank buckets
are `(0, 2%]`, `(2%, 5%]`, `(5%, 10%]` of the observable cross-section.
The session CSV reports first-hour cross-sectional mean, median and population standard
deviation. These are explanatory diagnostics, never filters or market-regime gates.

IEX is a **single-venue** feed, not consolidated US market coverage. Coverage and
first-hour relative returns depend on that observation scope. Provider absence,
current-membership survivorship bias and signal-level execution assumptions must be
considered when interpreting future results.

No ORB outcome slices enter this hypothesis: no favorable symbols, 12:00-14:00 window,
months such as April 2025, exit subsets or rank-decile findings. No VWAP, gap, SPY,
sector, volatility, news or fundamental filters are added. There are no alternate
timeframes, observation windows, quantiles, management variants or short-winner leg.
Evaluate V1 unchanged before proposing a separate future hypothesis.

## Historical reproduction workflow

For explicitly requested reproduction of rejected V1, use a fresh preflight stem:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-intraday-reversal-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --output-stem intraday_reversal_v1_preflight_2024-01-02_2026-08-12_v1
```

Review the candidate manifest, Daily qualification, feed and coverage output before
deciding on remediation or validation. Subsequent workflow, contingent on that review:
explicit candidate-gap remediation if needed using the exported requirements; local
validation using the frozen reversal manifest; then analysis before any V2 proposal.
The existing remediation path accepts `--candidate-gaps-only --candidates-report PATH`
with native 15m, regular hours and zero intraday warmup for this dedicated report.
There is no automatic synchronization. No historical reversal outcome was generated
during implementation.
