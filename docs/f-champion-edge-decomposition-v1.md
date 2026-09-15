# F Champion Edge Decomposition V1

**This study does not modify or optimize the frozen F champion. It is a descriptive
decomposition of previously observed historical performance.**

`research-f-champion-edge-decomposition-v1` / `F-CHAMPION-EDGE-DECOMPOSITION-V1` is
**ACTIVE**, **DIAGNOSTIC_ONLY** research. It belongs to a separate diagnostic registry,
not the independent-alpha registry, `StrategyVariant`, `PositionManagementPreset`, or
production/champion selection. It creates no trading strategy and no promotion path.

**No finding from this report may be promoted directly into a new strategy rule.
A future complementary strategy requires a separately frozen hypothesis.**

## Frozen subject and anti-tuning boundary

The sole subject is **F/configured/C1**, `StrategyVariant.QUALITY_VALUE_MOMENTUM`,
`PositionManagementPreset.CONFIGURED`. The existing champion guard rejects incompatible
configuration before reading data. The runner does not normalize or rewrite settings.

| Contract | Frozen value |
| --- | --- |
| Capacity / maximum position fraction | 1 / 1.0 |
| Risk per trade | 1% |
| Initial stop | ATR(14) times 2, capped at 10% |
| Target | 12% |
| Hard maximum hold | 10 sessions, existing engine missing-bar behavior preserved |
| Slippage / commission | 5 bps / 0 |
| Re-entry / cooldown | Enabled / 0 |

The existing configured **Quality + Valuation** weights rank F candidates. Opportunity
and Timing do not enter F ranking. The unchanged technical gate requires drawdown from
the 52-week high at or above the configured minimum, momentum126 strictly above its
configured minimum, price above SMA50 and SMA200, and a rising SMA20.

The diagnostic code consumes the canonical evaluator and champion contract. It does not
change eligibility, ranking, entry date/fill, position sizing, capacity, stop, target,
hold, or re-entry. There is no optimization loop, best-parameter output, conditional
rule generator, recommendation file, alternative portfolio, rotation or hedge.

## Local preparation and reproducibility

`analyze-champion-edge-v1` uses existing local Daily bars and PIT fundamentals. It performs
no Alpaca/SEC calls, network request, synchronization, online classification or market-cap
lookup. Missing inputs remain unavailable. F's SEC identity guards and current tradable
company universe remain unchanged; this study does not use the broader ORB asset universe.

The flow is:

1. Validate the frozen champion and resolve its existing represented XNYS sessions.
2. Prepare the batched `HistoricalFeatureScreenSource` once. Screen each signal session
   once, capture full funnel evidence, and spool compact F replay records with the existing
   `ReplaySpool` / `ManifestScreenSource` infrastructure. Retain rejected records from each
   symbol's first F eligibility onward for the engine's held-position/re-entry consumers.
3. Batch-load a Daily panel only for ever-eligible symbols and SPY, with warmup and a local
   diagnostic tail. The final simulation session is not screened, matching the champion.
4. Run the unchanged `BacktestEngine` exactly once, with observational callbacks and a
   prepared Daily facade. No SQLite queries occur inside simulation. The normal missing-
   Daily-bar policy, including end-of-backtest last-available-close liquidation, is preserved.
5. Derive all tables in memory from copied evidence and the engine's actual results.

The summary records dates, frozen identity/contract, F gate and configured weights,
strategy-config fingerprint, existing F discovery source fingerprint, runtime fingerprint,
feed/adjustment, `network_used=false`, `strategy_modified=false`, timings and query counts.
The existing streaming content fingerprint covers current membership/identity/SIC, PIT
facts, Daily OHLCV and session keys. Its end is conservatively extended through the
diagnostic tail. It is checked again before export; source drift fails closed. The tail
never enters pre-entry screen features or the champion simulation.

Reports use a fresh stem and are never overwritten. Empty population tables are omitted,
with reasons in `summary.unavailable`. Fewer than two local represented sessions produce
a DATA_UNAVAILABLE summary rather than a fabricated simulation.

## Populations, funnel and gate failures

Keep these populations separate: all screened instruments; F-eligible/ranked candidates;
occupied-capacity candidates; executed F trades; and actual open-position session marks.
Every table row identifies its population. Candidate forward returns are never trade PnL.

`candidate_funnel` retains the existing candidate audit's ordered stages: identity, static
filters, history, price, liquidity, market cap, PIT fundamentals, positive operating cash
flow, score availability/thresholds, and the F gate. Non-applicable Opportunity/Timing and
legacy recovery stages are pass-through diagnostics, not additional F filters. Counts
reconcile to canonical F eligibility. Aliases identify history/data-quality eligibility,
fundamental-universe eligibility, final/ranked candidates, occupied-slot blocks, capacity
reserved by a selected order, and subsequent executed entries.

`gate_failures` retains exact canonical reason strings, both overall first failure and all
observable blocking reasons. It separately reports technical first/all failures, including
not-near-high, non-positive momentum, SMA50/SMA200 failures, non-rising SMA20, and missing
technical inputs. Gate evaluation occurs for every record in the canonical evaluator;
`f_technical_gate_reached` distinguishes the sequential denominator after earlier gates.
Unavailable values cannot establish unobservable hypothetical threshold failures.

## Candidate forward returns and attribution

For each eligible candidate at signal close T, the reference is the open of the next
represented portfolio Daily session. If that symbol lacks that bar, the reference remains
unavailable; no delayed symbol-specific replacement is made. Fixed horizons **1, 3, 5,
10, 20** count official XNYS sessions starting with the entry session: 1d is that session's
close, 3d is the third session's close. Return is horizon close / reference open - 1.
Only the exact endpoint prices are needed; absent endpoints remain null. Existing local
data up to 20 sessions after the requested end may complete diagnostic horizons. No
future value feeds back into the screen, ranking or champion decisions.

`candidate_forward_returns` records rank, weighted F/Quality/Valuation scores, technical
state, PIT provenance, selection/execution disposition, reference and horizon dates.
Fixed rank groups are **1**, **2-3**, **4-5**, **6-10**, **11+**. Candidate rows report count,
available forward observations, means/medians and positive rates. Separate executed-trade
rows report actual count, return/PnL means/medians, win rate and PnL-based profit factor.

Quality, Valuation, weighted F score, SMA distances, ATR/price and available normalized
fundamental components use sample-wide quintiles. Boundaries are linear 20/40/60/80th
percentiles of eligible-candidate observations, reused unchanged for executed trades.
They require at least 25 available observations and distinct boundaries; ties stay
together. Insufficient or tied samples report unavailable, without changing bucket counts
or fitting a different subgroup. All buckets are **POST_HOC_DIAGNOSTICS**.

Drawdown buckets are 0 to -2.5%, -2.5 to -5%, -5 to -7.5%, -7.5 to -10%; other values
have an explicit outside-range bucket. Momentum126 buckets are (0,10%], (10%,20%],
(20%,40%], >40%, with non-positive/unavailable cases explicit. SMA20 rising is recorded
as the existing Boolean state; no alternate slope is reconstructed.

Normalized score components come directly from existing Quality/Valuation factor records,
including revenue/EPS/cash-flow growth, operating margin, ROIC, debt-to-EBITDA, relative
PE/EV-EBITDA and FCF yield when available. No alternate fundamental model is built.

## Executed outcomes, exits and holding paths

`trades` joins the actual champion position to its signal-session candidate evidence.
Entry/exit prices, gross/net PnL, return, holding count and canonical exit reason come
directly from the engine. Totals reconcile to engine execution legs. The configured
champion has one execution leg per position; unexpected partial legs fail closed.
The current engine does not export a realized R multiple for this contract, so that
field is explicitly unavailable rather than independently recalculated.

`exit_attribution` distinguishes canonical profit-target, stop, time/max-hold and
end-of-backtest values without renaming historical reasons. It reports count, return
means/medians, win rate, holding time, total PnL and shares of total positive/negative PnL.

`holding_path` marks positions only while open through the sampled close. Return is
Daily close / actual entry fill - 1, excluding hypothetical exit costs. Cumulative
MFE/MAE use reference-price extrema. A missing bar leaves that day's mark unavailable
and cumulative path completeness false; no missing extreme is invented. Official elapsed
session number is distinct from the engine's preserved holding counter.

Checkpoints are days 1, 2, 3, 5, 7 and 10. Day groups are DAY_1, DAY_2, DAY_3, DAY_4_5,
DAY_6_7 and DAY_8_10, plus an explicit after-day-10 category for legacy missing-bar cases.
Multi-day groups report unique positions and position-session counts; averages and
eventual-profit/stop/target fractions are session-weighted. Eventual outcomes are labeled
diagnostics and never used for decisions. Intrabar stop/target exit days are omitted:
Daily OHLC cannot establish which extremes occurred before exit. Close exits can contribute
their final mark. No post-exit price enters an open-position path.

## Capacity and idle capital

`blocked_candidates` includes eligible candidates blocked at the PIT selection step while
the C1 slot was actually held, excluding the already-held symbol and any independently
binding sector limit observable at that step. Canonical selection reasons are preserved.
A lower-ranked candidate blocked by an order newly selected from cash is counted separately
as reserved capacity, not an occupied-slot opportunity. Future execution feasibility is
not assumed. Copied context records held symbol, entry date, engine holding day and return
at the engine's last known mark, which may be stale when a Daily bar is missing.

Forward diagnostics use the same horizons and reference rules as all candidates.
`blocked_opportunity_cost` summarizes all blocked, rank-1, rank-2/3 and rank-4+ candidates.
It compares the hindsight-best blocked return with the held instrument's open-to-close
return over exactly aligned endpoints, only if the actual position remained open through
the horizon. Later prices after an actual exit cannot describe held-position opportunity
cost. **EX_POST_DIAGNOSTIC_ONLY**: hindsight selection is not a feasible rotation rule and
blocked candidates never become synthetic trades.

`idle_capital` classifies each executable session as IN_POSITION,
CASH_ELIGIBLE_F_ENTRY_EXECUTED, CASH_NO_ELIGIBLE_F, CASH_ENTRY_UNAVAILABLE_DATA, or the
explicit residual CASH_ENTRY_BLOCKED_OTHER / CASH_NO_PRIOR_SIGNAL_SESSION. Invested/cash
totals use the engine's session exposure, including positions opened and closed that day;
end-of-day exposure is reported separately. Streaks count executable sessions. No-candidate
cash sessions receive local SPY +1/+5/+10/+20 forward diagnostics without timing rules.

## Market dependence and concentration

SPY diagnostics use completed Daily observations only: 200-session SMA, 126-session
close momentum, 20-return sample standard deviation annualized by sqrt(252), and close
drawdown from the highest Daily high over 252 sessions. Each requires a complete official
window; gaps are unavailable, never filled. Fixed regime groups are above versus at/below
SMA200, positive versus non-positive momentum, and 0-5/5-10/10-20/>20% drawdown. Volatility
quintiles use one observation per screened session, not repeated candidate observations.

Trade attribution uses SPY state at the signal close. Exposure attribution uses state
at the preceding official close, before that executable session. Regime tables report
actual trade returns/holding/target/stop rates and separate invested/cash counts. No
capacity provider or regime gate is passed to the engine.

`market_beta` reports equal-weight trade return versus SPY entry-open-to-exit-close return,
and champion equity interval returns versus matching represented-session SPY close returns,
including cash. Pearson, average-tie-rank Spearman, OLS beta/intercept and R-squared are
diagnostics; insufficient (<3 paired observations) or constant samples are explicit.
Different holding durations, selection, serial dependence and an in-sample interval limit
interpretation. SPY's exit-session close is a Daily-resolution proxy, not an observed
SPY price at an intrabar F stop/target instant. No significance claim or hedge is generated.

Sector uses existing current local two-digit SIC as a proxy, explicitly **not PIT-safe**.
Sector and symbol tables reconcile actual trade count and PnL and report concentrations.
Top-1/5/10 PnL contributors and trade-count shares are ranked separately. Net-PnL shares
can be negative or exceed 100% when offset by losses; absolute-PnL shares are also reported.
No whitelist, blacklist or sector filter is produced.

Market-cap groups are below $1B, $1-5B, $5-20B, $20-100B and >$100B. Only an existing
screen cap with PIT filing provenance is used. Otherwise the summary reports
`MARKET_CAP_ATTRIBUTION_UNAVAILABLE_PIT`; future shares are never reconstructed.
Optional component/cap files are omitted when their source is unavailable.

## Limits and manual workflow

The study is **CURRENT_UNIVERSE_ONLY**, **NOT_SURVIVORSHIP_CLEAN**. The 2024-2026 period
has already been used extensively for development. Decomposition is descriptive and
in-sample; all slice results are post-hoc and provide no independent OOS evidence.
Missing data may select which observations have measurable outcomes. No statistical
attribution proves causation. Source/config fingerprints aid reproduction, not external
validity. Summary JSON contains measured evidence and availability, not economic conclusions
or machine-applicable recommended settings.

The next manual command is:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli analyze-champion-edge-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --output-stem f_champion_edge_decomposition_2024-01-02_2026-08-12_v1
```

Review the generated decomposition manually before discussing any complementary strategy.
No historical decomposition or provider job was run during implementation.
