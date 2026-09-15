# Market Intraday Momentum V1: frozen independent hypothesis

`research-market-intraday-momentum-v1` / `MARKET-INTRADAY-MOMENTUM-V1-15M-SPY`
is **ACTIVE** research.

**This hypothesis was frozen before any Market Intraday Momentum V1 trade outcomes
were evaluated.** Synthetic specification tests are not historical outcome evaluation.

The single question is: **Does the direction of SPY during the first 30 minutes of
the regular US trading session predict continuation in the final 30 minutes of
the same session?** A positive morning predicts positive closing-half-hour returns;
a negative morning predicts negative closing-half-hour returns.

This family is independent of F, ORB, Intraday Reversal, lifecycle, intraday-entry and
intraday-risk research. ORB and Reversal V1 remain rejected, with their original economics
and evidence preserved. F/configured/C1 remains the frozen champion.
**Positive results do not automatically promote this family to champion, paper trading,
or live trading.**

## Proxy and data

V1 uses exactly **SPY**. Local Alpaca metadata must contain `symbol=SPY`,
`asset_class=US_EQUITY`, `tradable=true`. Otherwise discovery fails closed with
`SPY_MARKET_PROXY_UNAVAILABLE`. There is no substitute or fallback proxy, no dynamic
liquidity choice, and no QQQ/IWM/DIA/VOO/IVV or UCITS variant.

Candidates are official XNYS sessions in the requested interval. There is no Daily
history prerequisite, ADV20, Top-100 universe, SEC identity, fundamental, F score,
Daily momentum or cross-sectional ranking. Only session T's native intraday observations
drive the signal. No overnight information is used.

Use native **15m**, regular-session data on the official **XNYS** calendar in
`America/New_York`. `extended_hours=false`; no resampling, interpolation, synthetic
bars, Daily substitution or extended-hours substitution. The configured research
feed (currently **IEX**) and existing adjustment convention (currently `all`) remain
unchanged and are bound in the manifest. IEX is a **single venue**, not consolidated
US market coverage; predictive measurements and observability depend on that feed.

SPY is a US market research proxy. `execution_eligibility_germany=NOT_EVALUATED`.
Alpaca `tradable=true` does not prove that a German/EU retail broker permits purchasing
SPY. No UCITS substitution or jurisdiction filter changes this research hypothesis.

Current local SPY membership is required; historical tradability is not reconstructed.
Unlike the earlier Top-100 studies, this family has no changing cross-sectional universe.
That distinction does not establish a survivorship-clean or clean out-of-sample claim.

## Morning observation and frozen direction

Require the first two actual native regular bars, normally:

```text
09:30-09:45
09:45-10:00

first_30m_open   = open of the first bar
first_30m_close  = close of the second bar
first_30m_return = first_30m_close / first_30m_open - 1
```

Direction becomes known at completion of the second bar, **10:00 ET** on a normal
session. A strictly positive return produces LONG; a strictly negative return produces
SHORT; exactly zero produces `NO_SIGNAL_ZERO_FIRST_30M_RETURN`. Decimal price comparisons
preserve the sign without an epsilon band. There is no magnitude or volatility threshold.

Direction is fixed at that decision. Prices after 10:00, including the entire middle
of the day and the closing window, cannot alter the signal or direction.

## Deliberately delayed execution

There is **no entry at 10:00**. Wait until the final 30 minutes of the same official
regular session. The final two expected native 15m bars define the execution window:

| Session | Entry | Exit |
| --- | --- | --- |
| Normal 16:00 close | 15:30 penultimate bar open | 16:00 final bar close |
| Official 13:00 early close | 12:30 penultimate bar open | 13:00 final bar close |

The calendar defines both timestamps; 15:30/16:00 are not hardcoded. Daylight-saving
changes and official early closes are respected. Neither missing closing bar can be
replaced with an earlier/later bar or last available price.

The sole exit is `FINAL_REGULAR_SESSION_CLOSE`. There is no stop, target, trailing
stop, partial exit, confirmation, retest, VWAP condition or intermediate exit. Holding
time is 30 minutes, with at most one signal and execution per session and no overnight hold.

## Frozen long and short economics

Both sides use 5 bps adverse slippage per fill and zero commission:

```text
LONG:
entry_fill = entry_reference * 1.0005
exit_fill  = exit_reference  * 0.9995
gross_return = exit_reference / entry_reference - 1
net_return   = exit_fill / entry_fill - 1

SHORT:
short_entry_fill = entry_reference * 0.9995
cover_fill      = exit_reference  * 1.0005
gross_return = (entry_reference - exit_reference) / entry_reference
net_return   = (short_entry_fill - cover_fill) / short_entry_fill

commission = 0
borrow_fee = 0
modeled_cost = gross_return - net_return
```

Short borrow availability and execution eligibility are **NOT_MODELED**. Zero modeled
borrow fees do not establish executable shorts. Separate execution validation is required
before interpreting research observations as real trading opportunities, including at
the user's German/EU broker. There are no CLI overrides for V1 economics.

Each executed session is an independent unit-entry-notional observation. This remains
research without a production portfolio layer: no compounded equity, CAGR, portfolio
Sharpe or portfolio maximum drawdown is reported.

## Four-bar coverage and local preflight

Only four timestamps are required: first, second, penultimate and final regular native
bars. Missing or invalid **unused midday bars do not invalidate** a session. They are
not loaded by this family's exact-timestamp database query.

`preflight-market-intraday-momentum-v1` resolves official sessions, verifies local SPY
membership, freezes those four timestamps and checks local coverage. It exports a
`*_momentum_candidates.json` manifest, `*_coverage.csv`, `*_intraday_requirements.json`
and `*_summary.json`. It does not compute first/final-window returns, direction or PnL.
Existing output files are never overwritten.

Coverage separately labels `MORNING_WINDOW` and `CLOSING_WINDOW` using
`REQUIRED_PRESENT`, `LOCAL_MISSING_FETCHABLE`, `PROVIDER_CONFIRMED_ABSENT` and
`PROVIDER_CHECK_FAILED`. Provider absence must have compatible, locally recorded
evidence for the missing timestamps/feed/adjustment/session. It is never inferred.
Unresolved required gaps or failed checks block outcome validation. Confirmed absence
allows explicit unobservable records:

- Either morning bar missing: `FIRST_30M_UNOBSERVABLE`, no signal.
- Morning signal present but either closing bar missing:
  `CLOSING_WINDOW_UNOBSERVABLE`, signal/direction retained, no trade outcome.
- Exact-zero morning: no signal, even if the closing window is unavailable.

The requirements report retains all frozen candidates for audit, but its sync scope
includes **only sessions with unresolved required-window gaps**. The existing generic
remediation contract qualifies a full-session timestamp grid and requests a bounded span
from the earliest to latest missing bar. Within an affected session it can therefore fetch
unused middle bars, or the whole session if both ends are missing. This limitation is
explicit in the report. It never makes middle bars a research requirement, and sessions
whose four research bars are already qualified are not remediation targets.
There is no new strategy-specific network path and no automatic synchronization.

## Manifest and simulation contract

Dedicated manifest **version 1** binds family/ID, exact dates, SPY symbol/class/tradability,
calendar/timezone/session open and close, all four exact timestamps, native 15m, feed,
adjustment and regular hours. It also binds the complete frozen morning formula,
direction rule, closing-window definition, entry/exit semantics, both slippage assumptions,
zero commission/borrow fees, and absent stops/targets/trailing/partials.

Validation requires `--candidate-manifest PATH` (normal reproduction) or an explicit
`--rediscover-candidates` research path. Manifest reuse reads saved candidates, verifies
current asset and official calendar semantics, and never calls candidate discovery.
Any relevant drift fails closed with `MARKET_INTRADAY_MOMENTUM_MANIFEST_MISMATCH`.
Recomputing a JSON checksum cannot authorize altered semantic fields. Checksums provide
integrity, not a trusted third-party signature. Native prices are requalified separately,
so explicitly acquired missing bars can be used without replacing the manifest.

Both research CLIs are local and read-only. Validation performs no provider calls or
synchronization. Asset metadata loads once. The new generic exact-native-timestamp loader
uses one indexed SELECT per batch (up to 200 required points), without loading Daily or
middle-of-day rows. Calendar grids and the bounded run-owned `NativeEntrySessions` spool
reuse generic infrastructure. Simulation uses prepared records and performs **zero SQL**.
No ORB or Reversal alpha/economics are imported into this family.

## Diagnostics and statistical interpretation

Exports include one session/event row per candidate, executed trades, monthly/yearly
tables, three chronological thirds, LONG/SHORT attribution and fixed absolute-magnitude
buckets: `[0,0.25%]`, `(0.25%,0.50%]`, `(0.50%,1.00%]`, `>1.00%`.
Both sides always receive the same core metrics; no side or bucket is discarded.

Core metrics include candidate/morning-observable sessions, long/short/total signals,
zero no-signals, executions and unobservable closing signals; gross/net expectancy,
medians, profit factors, net win rate and average winner/loser; first/final raw return
means/medians; directional hit rate; modeled drag; MFE/MAE means/medians; and holding time.
Returns/rates use fractions. First-return summaries include all morning-observable rows.
Final-return summaries and predictive diagnostics use sessions with both windows observable,
including exact-zero mornings. Execution statistics use executed sessions only.

Directional hits require final raw return >0 for LONG or <0 for SHORT; zero is not a hit.
MFE/MAE use direction-adjusted reference-price extrema in the two holding bars, clamped
around zero. Profit factors are null without losses. Empty/insufficient samples are explicit.

Predictive diagnostics report Pearson correlation, Spearman correlation with average ranks
for ties, OLS slope/intercept and R-squared. Undefined constant-series correlations are null.
No fitted coefficient controls a trade. Chronological thirds partition requested sessions,
including unobservable and no-signal days.

Gross/net session-return statistics include arithmetic mean, sample standard deviation
(`n-1`), standard error (`SD/sqrt(n)`) and approximate 95% confidence intervals
(`mean +/- 1.96*SE`). Fewer than two executions produce null SD/SE/intervals. These intervals
assume independent sessions; serial dependence and fat tails can weaken their interpretation.
Statistical significance does not replace economic significance after costs.

## Anti-tuning boundary and next manual step

No profitable ORB/Reversal symbol/month slice, noon-entry observation, Reversal rank bucket,
May 2026 behavior, winner whitelist or post-hoc regime enters V1. No VIX, SMA, QQQ confirmation,
gap, volume, ATR, RSI, VWAP, weekday/month or magnitude filter exists. There are no alternate
proxies, bar timeframes, observation windows, long-only or short-only variants.

The next manual step is only the new preflight:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-market-intraday-momentum-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --output-stem market_intraday_momentum_v1_preflight_2024-01-02_2026-08-12_v1
```

Review its candidate/coverage output before deciding on explicit remediation or manifest-based
validation. The later workflow is contingent on that review: remediate only if required,
validate locally with the frozen manifest, analyze results, then freeze the V1 decision
before any V2 proposal. No historical Market Intraday Momentum V1 experiment was run during
implementation.
