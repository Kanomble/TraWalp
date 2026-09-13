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

Both intraday commands log phase starts and completions at INFO level. During a long phase,
`intraday_preflight_progress` / `intraday_validation_progress` emits a heartbeat every 1,800
seconds measured with `time.monotonic()`, including while a single query or preparation call
is still running. Counters describe completed work; an unchanged counter during a heartbeat
means that operation has not returned. Candidate discovery counts signal sessions (including
sessions with no candidates); I0/I1 backtests count portfolio sessions. Percentages are bounded
at 100 and omitted for unknown or empty totals. There is no per-session or per-candidate log.

The final timing summary and JSON `performance` object contain stage durations and workload
counters. Validation's `coverage_verification_seconds` includes its nested discovery, load and
evaluation timings, so those fields must not be summed again. `diagnostics_seconds` includes
preparing the peer context and building the final diagnostic tables. Coverage currently loads
one requirement per candidate-session: `coverage_batches` counts those requirements and
`sqlite_query_count_coverage` counts both native intraday and preceding Daily data SELECTs.
`daily_rows_loaded` reuses the screen source's loaded-bar counter plus coverage Daily rows;
`coverage_daily_rows_loaded` isolates the latter. These are loaded rows, not distinct rows.

Persisted `export_seconds` and `total_seconds` are measured through payload staging; the
`export_timing_scope` field identifies this boundary. Final timing-metadata writing and atomic
publication occur after that snapshot and are included in the export phase completion log
and final terminal timing summary (`export_timing_scope=through_publication`).
All reports are staged together, then the summary is published last. Ctrl+C exits with status
130, stops heartbeat workers, and removes this run's staged or partially published files.
Previously completed reports remain protected by the fresh-output-stem check. Instrumentation
does not change F ranking, I0/I1 decisions, coverage rules, or research CSV contents.

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

## Daily Lifecycle Preflight

`preflight-f-lifecycle-daily` checks **Daily execution coverage** before a manually started
`validate-f-lifecycle-v2`. It is separate from `preflight-f-intraday-entry`, which checks
**native 15m entry-session coverage**. Neither preflight substitutes for the other.
The Daily CLI accepts only `--start`, `--end` and `--output-stem` as command-specific arguments.

The Daily preflight reuses the runner's shared frozen-control validation and central
`qualify_historical_screen_start(..., allow_start_shift=False)` Daily/warmup qualification,
including the requirement that every requested portfolio session exists locally. If that
qualification fails, it exports the reasons with `daily_qualified=false`,
`lifecycle_daily_qualified=false` and `candidate_discovery_complete=false`; empty diagnostic
tables in that case are not evidence of complete coverage. No start shift is performed.

Candidate discovery uses the same cached historical PIT screen source and shared
`iter_f_candidates` helper as the intraday preflight, with `evaluate_variant_entry` and the
frozen F identity. All eligible candidates are ranked by the existing F score and symbol tie
break, before capacity/allocation. It includes candidates that C1 would not execute, because
different L0–L6 holding durations can change allocation. Only signal sessions with a next
official XNYS session within the research interval produce entry requirements.

For a signal on T, entry is the next official XNYS session and counts as holding day 1.
Require up to 30 official sessions starting at entry, inclusive, capped at research end.
Weekends and exchange holidays are never required. Stops, targets, peer states and hypothetical
early exits do not reduce this conservative horizon. The per-row `required_by_variants` is:

| Holding days | Potentially requiring variants |
|---|---|
| 1–10 | L0,L1,L2,L3,L4,L5,L6 |
| 11–15 | L1,L2,L3,L4,L5,L6 |
| 16–20 | L2,L3,L4,L5,L6 |
| 21–30 | L3 |

Discovery finishes before future coverage is inspected. Coverage is data qualification only;
its result cannot feed back into candidate eligibility, score, ranking or trading decisions.
Future prices and filings cannot enter a screen on T. The existing current-universe and
unversioned-SIC provenance limitations still apply.

Four fresh files are written under the configured reports directory:

- `<stem>_preflight.json`: qualification, candidate counts, unique required/present/missing
  symbol-session counts, and unique missing counts by symbol, session and variant.
- `<stem>_missing_symbol_sessions.csv`: exactly one row per missing `(symbol, required_session)`.
  It records the earliest requiring signal/entry, the greatest holding day across references,
  the union of their requiring variants and `candidate_requirement_count`.
- `<stem>_daily_requirements.csv`: every candidate's full diagnostic horizon, with signal,
  entry, rank, required session, holding day, variants and local presence status.
- `<stem>_candidate_summary.csv`: one row per candidate, with the final required session and
  required/present/missing counts for its horizon.

Requirement rows use `REQUIRED_PRESENT` or `LOCAL_DAILY_MISSING`. `NOT_REQUIRED` denotes
symbol-sessions outside the requirement set and is not expanded into CSV rows. Local absence
does not establish provider absence; the preflight never claims `PROVIDER_CONFIRMED_ABSENT`.
Missing-by-variant counts deduplicate within each variant; their sum can exceed the total
unique missing count. Overlapping candidates retain separate requirement rows, while summary
symbol-session totals count each key once. No bars are fabricated, filled or interpolated.

SQLite is opened with `mode=ro`, including shared readers that normally use `connect()`.
There is no database initialization, migration, write, provider call or sync. Coverage reads
only Daily symbol/timestamp keys with indexed queries in batches of at most 400 symbols;
there is no SQL query for each candidate/session. The feature/screen cache is released after
discovery, presence sets are kept for one symbol batch, and the requirements CSV is streamed
from compact candidate and missing-key data. There are no baseline or cost-stress backtests.
Historical screen preparation can still be substantial; no full-period timing claim is made.

The command writes reports before returning exit code 1 for incomplete qualification; complete
qualification returns 0. Existing output stems are refused before discovery and again at export.
`validate-f-lifecycle-v2` does not automatically run this preflight and retains its hard
`DAILY_POSITION_DATA_UNAVAILABLE` invariant for any actually held position without a Daily bar.
Strategies, lifecycle rules, allocation and execution behavior are unchanged.

Focused regression fixtures cover the ABBV sequence (2022-03-07 present, 2022-03-08 missing,
2022-03-09 present), all variant boundaries through day 30, calendar/end censoring, overlapping
requirements, batched queries, true PIT screens under future bar/filing changes, read-only
SQLite, blocked network/backtest entry points, CLI exports and the retained engine hard guard.
Verification for this addition: **65 focused tests passed** (21 Daily preflight tests and
44 existing lifecycle/entry regressions). Ruff check passed for all five touched Python files;
format check passed for the four feature/test files and the changed CSV helper signature in
`report.py` (its unrelated existing formatting was preserved). `git diff --check` passed.
No full suite, historical preflight/research run, provider/Daily/intraday sync, paper trading
or production CLI was executed.

Manual Daily command (not executed during implementation):

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-f-lifecycle-daily `
  --start 2022-01-03 `
  --end 2024-08-12 `
  --output-stem f_lifecycle_daily_preflight_2022-01-03_2024-08-12_v1
```

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
  --candidate-manifest .\reports\f_intraday_entry_preflight_2022-01-03_2026-08-12_v1_intraday_candidates.json `
  --output-stem f_intraday_entry_2022-01-03_2026-08-12_v1
```

The task's command names are implemented exactly. Preflight itself is a potentially long local
candidate-discovery job and is therefore also left to the user for the full historical period.

Validation now requires a compatible candidate manifest and its sibling replay file, or an
explicit `--rediscover-candidates`. Earlier manifests lacking fingerprints must be regenerated.
See [candidate discovery performance](f-candidate-discovery-performance.md) for the bounded
cache, peer-statistics reuse, exact coverage batching, and current benchmark/verification report.
