# Strategy F Lifecycle V2 research

## 1. Motivation and scope

Study whether holding duration, selective extension and allowing an already profitable trend
to continue add understandable, stable, cost-robust value over F/configured. The repository
audit is [research-code-audit.md](research-code-audit.md). No old research family was removed.
This is an opt-in research family, never a production strategy or automatic champion selection.
The frozen F/configured champion, its configuration, screen and regime-capacity runner remain
unchanged. All new family runs use `max_positions=1`.

The screen gates (quality, valuation, momentum126, SMA50/200, SMA20 slope and distance to the
52-week high), ranking, sizing, risk-per-trade, position/sector limits, initial stop and
configured target price are inherited unchanged. The runner refuses a noncanonical control
(Daily hard max hold 10, canonical risk/cost settings and no additional management rules).

## 2. The seven fixed lifecycle hypotheses

Family: `research-f-lifecycle-v2`. Identities live in `backtest/lifecycle.py` and are exposed by
`research_registry.F_ISOLATED_RESEARCH_FAMILIES`, independently of production management enums.

| ID | Label | Holding / profit semantics |
|---|---|---|
| F-LIFECYCLE-L0 | F-lifecycle-control | Existing configured max hold (currently 10), unchanged target; normal manager, exact C1 control |
| F-LIFECYCLE-L1 | F-lifecycle-hold15 | Only configured max-hold days becomes 15 |
| F-LIFECYCLE-L2 | F-lifecycle-hold20 | Only configured max-hold days becomes 20 |
| F-LIFECYCLE-L3 | F-lifecycle-hold30 | Only configured max-hold days becomes 30 |
| F-LIFECYCLE-L4 | F-lifecycle-conditional-hold20 | At configured day-10 time exit, extend once if own trend healthy; hard maximum 20 |
| F-LIFECYCLE-L5 | F-lifecycle-hold20-peer-confirmed | L4 also requires confirmed peers at day 10 |
| F-LIFECYCLE-L6 | F-lifecycle-dynamic-profit-peer | Hard max 20 from entry; defer the existing target only with healthy trend and confirmed peers |

Entry session counts as holding day 1. L1–L3 have no new early exit. L4/L5 have no trend exit
before day 10; an extension once granted continues to the hard maximum, with the configured
stop and target still active. They do not repeatedly revoke the extension on days 11–19.
L6 has its own 20-session hard maximum; it is not combined with conditional day-10 extension.

## 3. Trend health

`TrendHealthState` is immutable: `HEALTHY`, `WEAKENING`, `UNAVAILABLE`.
Healthy means strictly `close > SMA20` and `sma20_rising == True`. The latter uses the existing
canonical SMA20/slope semantics via the already regression-tested `_fast_technical_snapshot`
(equivalent to `technical_snapshot`/`indicator_frame`) and configured slope lookback
(currently five sessions). Equality is weakening. Missing/incomplete required XNYS history
is unavailable; observations are never forward-filled. No fitted threshold or new indicator.

## 4. Peer context and provenance

Use locally stored companies and the existing SIC normalization and 4/3/2-digit grouping.
Choose the narrowest basket with three other valid peers; when no such basket exists, report
the available two-digit basket with `PEER_UNAVAILABLE`. Exclude the stock itself, SPY and
unresolved identity conflicts. Group widths are selected using valid histories at the observed
session, so a future listing/bar cannot enter a past price basket.

Each peer needs a complete trailing 20-session price window (including the observed session),
which supplies SMA20 and 1d/5d returns. Own trend additionally needs the full slope window.
Export count, above-SMA20 ratio, positive 1d/5d ratios, median/best/worst 1d returns, median 5d
return, stock 5d return and `stock_5d_return - peer_median_5d_return`.

`PEER_CONFIRMED` requires count >=3 and above-SMA20 ratio strictly >0.50. With count >=3 and
ratio <=0.50, state is `PEER_WEAK`; count <3 is `PEER_UNAVAILABLE`. No threshold tuning.

**Provenance limit:** the repository stores current tradable membership and current SIC,
not historical SIC effective dates. Price observations are PIT, but historical membership
cannot be certified. Every bundle explicitly records
`historical_peer_membership_verified=false` and
`CURRENT_LOCAL_SIC_AND_TRADABLE_UNIVERSE_NOT_HISTORICALLY_VERSIONED`, alongside the existing
universe provenance audit. This implementation does not invent historical industry data or
claim a survivorship-clean peer universe. A fully historical membership study requires new
local provenance data before such a claim is possible.

## 5. Dynamic profit target and execution ordering

`LifecyclePositionManager` adapts the central `PositionManager` only for L4–L6. It never moves
the configured target price or disables stops. Default/L0 engine paths use the original manager.

| Event | Executable behavior |
|---|---|
| Overnight gap through stop | Existing open-price stop before any target or pending trend exit |
| Both stop and target in Daily range | Existing conservative stop-first behavior |
| Target reached before a close-based time exit | Existing target exit takes priority unless deferred |
| L6 target deferral | Uses trend and peer state from the **previous completed Daily session** |
| Deferred position stops later | Existing stop semantics remain active |
| Deferred position loses own/peer health | Detect at completed Daily close; queue sale at next real Daily open, gap stops first |
| Hard max reached | Configured Daily close time exit (day 20 for L6) |

Using today's closing health at an earlier target touch would be lookahead. Yesterday's state
therefore governs deferral; at the completed close health can schedule the next opening exit.
Unavailable own/peer context also prevents deferral or queues an exit after deferral.
No unknown high/low ordering is inferred. Existing configured time exits retain their existing
same-close simulation convention. The final backtest session still liquidates at its close.

`*_dynamic_profit_events.csv` links the first target decision to eventual position exit and
records target price, reached/deferred flags, observed context session, health/states, original
executable reference, original-target net return, final net return and their difference.
An opening gap above target uses its original executable open rather than inventing a target fill.
Post-target MFE/MAE are relative to that reference and use only complete sessions strictly after
the touch session and before the exit session; unknown portions of touch/exit bars are excluded.
Empty observation windows stay null and their count/basis is explicit. This is a conservative
observable excursion measure, not a claim to know intrabar post-touch extremes.

## 6. Holding extension diagnostics

`*_holding_duration_analysis.csv` has one row per completed position, flags/count contributions
for exits before/on day 10, holding beyond 10, reaching 15/20/30, return on day 10 when held,
actual post-day-10 MFE/MAE and final return. Summary/metrics aggregate the counts by variant.
Returns on day 10 are gross mark-to-entry-fill returns; final returns include modeled costs.
Post-day-10 excursions are relative to the day-10 close and exclude uncertain exit-day extremes.

Separate `counterfactual_day11_20_MFE/MAE` measure the next ten exact sessions after day 10,
bounded by the research end. In particular, original day-10 time exits expose whether additional
positive MFE or negative MAE followed; count fields are also summarized. These are forward labels,
not executed trades or counterfactual portfolio reruns. Incomplete windows remain null.

## 7. Separate intraday entry veto

Family: `research-f-intraday-entry-quality`, exactly:

- I0 / `F-INTRADAY-ENTRY-I0` / `F-entry-control`: existing F/configured entry and Daily management.
- I1 / `F-INTRADAY-ENTRY-I1` / `F-entry-opening-weakness-veto`: same daily F candidate,
  wait for the opening two completed native 15m bars on the next XNYS session.

Provider timestamps mark bar starts. At regular open +30 minutes, veto only when the second
close is strictly below both the previous Daily close and the volume-weighted native VWAP of
the first two bars. No unfinished bars, future bars, HLC3 substitution or synthetic confirmation.
Zero aggregate volume or missing positive-volume VWAP is explicitly unavailable.
If passed, execute at the next actually present native bar's open at/after the decision time.
The preceding bar close and the morning opening price are not retrospective execution prices.
If vetoed, the signal is consumed; there is no lower-ranked substitute chosen after the veto.

There is no intraday exit strategy, trailing logic or altered holding duration. Stop/target
checks remain the central conservative Daily checks. On a delayed entry day, the observed
post-entry native range supplies that one Daily check, so prices from before the entry cannot
stop out the new position. This ephemeral Daily range does not fabricate/store native bars.
Full native coverage of the entry session is required to avoid hiding post-entry stop/target
touches. From the next session the unchanged Daily bars drive management.

The local preflight discovers **all** PIT-eligible F candidates, regardless of current capacity,
qualifies native entry sessions and exports `_preflight.json`, `_missing_symbol_sessions.csv`
and `_intraday_candidates.json`. The latter uses the existing `sync-intraday --candidates-report`
schema, with native 15m requirements and no holding-session intraday ranges. A complete veto
is qualified data; missing required data is `INTRADAY_UNAVAILABLE`, never a passed veto.
The validation runner repeats qualification and refuses an incomplete dataset. A direct
engine fixture explicitly skips unavailable signals and records the status.

## 8. Overnight gaps

`*_entry_gap_analysis.csv` includes all eligible potential entries, selection/execution status,
candidate rank and realized position return/MFE/MAE/holding/exit reason when actually executed.
Unexecuted candidates have null realized outcomes, not invented simulated profits.
`gap_return = next_open / signal_close - 1`;
`gap_in_ATR = (next_open - signal_close) / signal_session_ATR`.
ATR comes from the existing F signal technical snapshot. Missing ATR stays unavailable.
`*_entry_gap_summary.csv` reports positive/negative/flat gaps and descriptive within-run
ATR quintiles, including observed ranges, counts and realized expectancy. These are reporting
groups only, never a filter, sweep or new optimized strategy threshold.

## 9. Peer leader / spillover diagnostics

`*_peer_context.csv` records signal, known-at-entry, exit-close descriptive and pre-exit-session
contexts. The entry state uses the preceding completed Daily session. Exit-close state is
explicitly descriptive and must not be mistaken for information known at an intrabar fill.
`*_peer_summary.csv` groups signal contexts and realized outcomes.

`*_peer_spillover.csv` records largest positive signed peer 1d/5d returns, medians, and population
standard deviation of peer 1d returns. `largest_peer_move_previous_session` uses T-1's basket;
candidate next-session and next-five-session returns are T-close-to-T+1/T+5-close forward labels.
Exact official session alignment and end-of-run censoring apply. No news model, leader threshold,
leader-based entry or spillover trading rule is introduced.

## 10. Correlation diagnostics

`*_correlation.csv` and candidate gap rows include mean/max Pearson correlation against open
positions, using exactly 60 aligned daily returns (61 complete closes) through the signal close.
Potential candidates initially describe the signal-close portfolio; actual attempted entries
refresh the open-position set after overnight exits, before sizing, without using next-day closes.
The observation basis and valid-pair count are explicit. Empty portfolios, constant returns and
insufficient/incomplete history produce unavailable values. No correlation-based entry filter.

## 11. PIT, isolation and report architecture

- Shared cached F screens and immutable per-run config copies; no screen or sizing fork.
- Decision contexts slice all price histories at the observation date. Unavailable history
  stays unavailable; no stale latest bar is silently treated as today's close.
  The new runners refuse a missing Daily bar on any held-position session, so missing execution
  data cannot silently carry an extended position beyond its hard maximum.
- A separate observer/report module calculates gaps, forward returns and post-exit labels.
  Its return values cannot influence trading decisions.
- Local preflight/research do not call providers, sync or database initialization/migration.
- New report stems are checked before CLI work and at export; existing bundles are not overwritten.
- Daily bundle exports: summary JSON/CSV, metrics, positions, execution legs, monthly, yearly,
  chronological subperiods, holding duration, trend events, peer context/summary/spillover,
  dynamic profit events, entry gap analysis/summary, correlation, symbol concentration and cost stress.
  Entry-quality events have a separate CSV; empty diagnostic tables retain required headers.
- Existing central SPY benchmark, return/MaxDD/Sharpe/Sortino/PF/exposure/turnover, calendar/thirds
  and symbol-concentration calculations are reused. This task does not add an Exact LOSO run.

Daily costs use FULL_PORTFOLIO_RERUN: BASELINE 5bps/0bps, 2X_SLIPPAGE 10/0,
3X_SLIPPAGE 15/0, COMMISSION_SENSITIVITY 5/5. Seven baseline plus 21 stress runs = 28 portfolio
runs, sharing F screens. Intraday validation runs exactly I0/I1 at baseline costs.

## 12. Development versus OOS

All already analyzed historical dates, including 2022–2026, are **DEVELOPMENT / RESEARCH**.
Every new summary states `clean_oos=false`, independently of the requested date range. Older
report OOS labels are historical records, not certification for these new hypotheses. Repeated
use of the same history, current-universe survivorship and unversioned SIC remain limitations.
There is no automatic winner selection, statistical significance claim or champion promotion.

## 13. Why capacity remains separate

Lifecycle and entry-quality are not combined automatically. `EntryCapacityProvider` remains an
independent engine hook, and lifecycle config copies preserve the caller's portfolio fields.
This supports a later manually selected composition with C1, C5, SPY-SMA200 or
SPY-SMA200-MOM126. The present CLI families provide no capacity, hold, SMA, peer, target, stop
or momentum tuning flags and never construct a Cartesian product. The current regime-capacity
runner is retained without changes.

## Manual commands — not executed during implementation

Implementation verification: **173 distinct focused tests passed**, including 44 new lifecycle /
entry-quality tests and 129 retained regressions. The final groups completed with 132 passes
(engine/champion/F-entry/features plus the first 43 new tests) and 84 passes (all 44 new tests
plus capacity/regime/position-manager). The overlapping 43 tests are counted once. The only
remaining test warning is the existing third-party `websockets.legacy` deprecation.

L0 entries, exits, positions, equity, return and MaxDD match the normal C1 engine on deterministic
fixtures. Tests also cover the four genuine cost cases, exact family sizes, next-bar entry,
future peer/entry-bar perturbations, unavailable data, gap math, report schemas, sync-compatible
preflight, no network, and a real three-session preparation/runner smoke with identical SQLite
business data before/after. Ruff check and format check passed for all ten touched Python files;
`git diff --check` passed. No full suite, profiling, historical 2022–2026 run, provider/SEC/Daily/
intraday sync, production or paper trading was executed.

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli validate-f-lifecycle-v2 `
  --start 2022-01-03 `
  --end 2026-08-12 `
  --output-stem f_lifecycle_v2_2022-01-03_2026-08-12_v1

.\.venv\Scripts\python.exe -m trading_system.cli preflight-f-intraday-entry `
  --start 2022-01-03 `
  --end 2026-08-12 `
  --output-stem f_intraday_entry_preflight_2022-01-03_2026-08-12_v1
```

After manual data qualification/synchronization with the existing intraday infrastructure:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli validate-f-intraday-entry `
  --start 2022-01-03 `
  --end 2026-08-12 `
  --output-stem f_intraday_entry_2022-01-03_2026-08-12_v1
```

The task's command names are implemented exactly. Preflight itself is a potentially long local
candidate-discovery job and is therefore also left to the user for the full historical period.
