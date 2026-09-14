# ORB V1 signal-level research

Family `research-orb-v1`, ID `ORB-V1-15M-LONG`, status **ACTIVE** pending a later human
research decision. The single definition is `research_definitions.py::ORB_V1`.
The independent family registry does not add a Daily StrategyVariant or a management
preset. F/configured/C1 remains the frozen champion. Results never promote ORB.

## Frozen hypothesis and execution

Each requested XNYS session uses the current locally stored tradable **company**
membership, excluding unresolved identity conflicts. For each symbol, require 20
consecutive valid completed Daily sessions ending at T-1, previous close at least
`universe.min_price`, and mean(close × volume) at least
`universe.min_avg_dollar_volume_20d`. Sort that dollar-volume mean descending, symbol
ascending; select the first 100 (`ORB_LIQUID_TOP100`), or all survivors if fewer qualify.
No current snapshots, market cap, sector/SIC/REIT, F scores or technical gates enter
this selection. Missing Daily windows are counted explicitly; no stale close or
forward fill supplies a window. Preflight is not ready if a requested session has
no complete Daily windows. Zero survivors after valid liquidity checks is a valid
empty universe. Daily qualification details are in the summary.

Only provider-native 15m regular-session bars are admitted. The single 09:30–09:45
America/New_York bar defines the opening-range high and low. The first subsequent
completed bar with **close strictly above** that high consumes the sole daily signal
attempt. Intrabar highs and equality do not trigger. Decision time is bar completion;
entry is the next **actual native bar's open**, never the confirming close. No next
bar means `NO_ENTRY_NO_NEXT_BAR`. An entry open at or below the range low means
`INVALID_RISK_GEOMETRY`; there is no retry.

The stop stays at the opening-range low; initial R is entry reference minus that low.
From the entry bar onward, open <= stop exits at the actual open; otherwise low <=
stop exits at the stop. Stop precedence applies on the final bar too. With no stop,
exit at the final regular native bar's close. XNYS holidays, DST and official early
closes are respected. No overnight hold, target, trailing, delay filter, or second
attempt exists. Entry costs multiply reference by 1.0005; exit costs multiply it by
0.9995. Commission is zero. These constants have no CLI switches.

## Local qualification and remediation

Preflight discovers the Daily universe and exact full regular-session native 15m
requirements, without evaluating ORB outcomes. It exports a summary, coverage CSV,
and sync-compatible `_intraday_requirements.json`. Coverage uses existing durable
provider observations, matched to symbol, date, timeframe, configured feed,
adjustment, regular hours and the exact missing timestamps.

| Status | Local validation |
| --- | --- |
| `REQUIRED_PRESENT` | Simulate the complete native session |
| `PROVIDER_CONFIRMED_ABSENT` | Retain an unobservable event; never substitute rank 101 |
| `LOCAL_MISSING_FETCHABLE` | Refuse until explicit remediation qualifies it |
| `PROVIDER_CHECK_FAILED` | Refuse; inspect the recorded error |

Duplicate, off-grid or invalid OHLC rows also block qualification. No resampling,
synthetic bars, interpolation, Daily substitution or provider fallback occurs.
A partially present provider-absent session is wholly unobservable for this study;
using its partial outcomes would change the observation set. Coverage ratio is
fully present selected symbol-sessions divided by all selected symbol-sessions.
Coverage by symbol/month and provider absence counts remain visible in the summary.

The following commands are for **manual execution after review**. Substitute dates
and use fresh stems; existing report files are refused before discovery.

```powershell
python -m trading_system.cli preflight-orb-v1 `
  --start YYYY-MM-DD `
  --end YYYY-MM-DD `
  --output-stem orb_v1_preflight
```

Only if the requirements report identifies gaps or failed provider checks:

```powershell
python -m trading_system.cli sync-intraday `
  --start YYYY-MM-DD `
  --end YYYY-MM-DD `
  --timeframes 15m `
  --candidates-report reports/orb_v1_preflight_intraday_requirements.json `
  --candidate-gaps-only `
  --output-stem orb_v1_intraday_sync
```

ORB manifests explicitly require zero intraday warmup and regular hours; the sync
dispatch checks their feed/adjustment and 15m contract. Other candidate reports keep
their existing warmup behavior. Explicit extended-hours remediation is refused for
ORB. The configured feed is never changed. After qualification:

```powershell
python -m trading_system.cli validate-orb-v1 `
  --start YYYY-MM-DD `
  --end YYYY-MM-DD `
  --output-stem orb_v1_validation
```

Validation rebuilds the same deterministic universe from the then-current local
Daily/member data and freshly qualifies coverage. Keep the preflight requirements,
configuration and local dataset snapshot for reproduction; membership, Daily or
identity changes between runs can change requirements. Validation never auto-syncs.

## Reports and interpretation

Validation produces `<stem>_summary.json` and eight CSVs: `_orb_events`, `_trades`,
`_monthly`, `_yearly`, `_chronological_subperiods`, `_symbol_concentration`,
`_entry_time_buckets`, and `_coverage`. Each event is one terminal symbol-session
outcome; `signal_status=BREAKOUT_CONFIRMED` preserves confirmation on consumed
attempts. All requested decision, entry, stop, exit, cost and excursion fields are
present, with empty fields for unavailable observations.

Returns are fractions, not percentages. Gross return is exit reference / entry
reference − 1; net return is exit fill / entry fill − 1. R return is net per-share
PnL / initial R. Expectancy is arithmetic mean net return. Aggregated PnL and profit
factor assume one unit of entry notional per independent trade; there is **no**
compounded equity curve, portfolio capacity, CAGR or portfolio Sharpe. Modeled cost
is gross-minus-net return drag (sum and mean), with per-share costs on events.
Profit factor is null without a negative-return denominator.

MFE and MAE use reference-price return excursions, clamped at zero. Native OHLC
cannot tell whether a stop-bar high preceded the stop; unknown stop-bar extrema
are excluded and only its known open/exit contribute. These are conservative
observed excursions. Intrabar stop timestamps use the bar's start as an explicitly
labelled estimate, so holding minutes are coarse and can be zero on an entry-bar
stop. Open fills and final bar completion timestamps are exact bar times.

Diagnostics group trades by calendar month/year, three contiguous thirds of the
requested trading sessions (including zero-trade days), symbol, and ET entry buckets
09:45–10:30, 10:30–12:00, 12:00–14:00, 14:00–15:45. Buckets are left-inclusive and
right-exclusive except the final bucket includes 15:45. They never alter trading
rules. Signed top-symbol PnL shares can exceed 100% when other symbols lose; shares
are null when total net PnL is nonpositive. Counts per day are included in summary
stability data. Empty groups retain valid headers and null unavailable statistics.

Daily reads use bounded symbol batches and rolling sums. Exact native requirements
use the shared batched loader and a run-owned temporary `NativeEntrySessions` spool,
with a bounded LRU cache. The simulation accepts no database/provider object and
performs **zero SQLite queries**. Reports expose discovery, verification, preparation,
simulation and diagnostics seconds, actual coverage SELECT count, native bars
loaded and cache peak. Preflight records zero for stages it does not execute.
Shared backtest package exports are deferred so importing ORB/shared utilities
does not initialize the F engine; the existing exported objects are unchanged.

## Limitations

- Historical universe uses current locally stored tradable company membership.
  Results are not survivorship-clean (`CURRENT_UNIVERSE_ONLY`, `NOT_SURVIVORSHIP_CLEAN`).
- ORB V1 is a signal-level research study. No realistic multi-position
  capital-allocation policy has yet been defined. It is not production-ready.
- With configured IEX: `market_data_feed=IEX`, `consolidated_market_data=false`.
  IEX is a single-venue feed; intraday price/volume observations may differ from
  consolidated SIP. No feed comparison is part of V1.
- Local native rows are not versioned by feed provenance. The configured feed is
  reported, while old rows' origins cannot be independently certified by this schema.
  Daily adjustments likewise are provider-adjusted history, not corporate-action
  vintages. Missing Daily windows and provider absence can bias observable results.
- Research periods are not clean OOS unless explicitly established later. Neither
  this implementation nor its synthetic tests establish positive ORB expectancy.
- CC-01 remains OPEN / HIGH / PRE_PAPER_BLOCKER for the existing historical champion;
  its missing-Daily-bar policy was not changed. Paper/shadow execution remains a
  separate future engineering milestone.

Implementation verification: 158 focused synthetic/regression tests passed across
`test_orb_v1.py`, `test_screen_strategies.py`, `test_champion_consolidation.py`, and
`test_intraday_remediation.py`. No real historical ORB preflight, sync, validation,
provider requests or network operations were run during implementation.
