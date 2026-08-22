# TraWalp Extended Strategy Validation

## Executive Summary

The frozen research-reference interval reproduced exactly. The requested OOS interval was fully usable without shifting its start: **2025-05-01 through 2026-04-30**.

The OOS evidence separates the two primary hypotheses sharply:

- **D1/C-swing-profit-lock: NOT SUPPORTED.** It produced exactly the same -3.627% return, 0.697 profit factor, expectancy, drawdown, positions, and portfolio path as C/configured. Six positions reached at least +1R and one reached +2R, but no profit-lock exit changed economic PnL. The paired direct effect and subsequent path effect were both zero.
- **C/intraday-dynamic: STRONG OOS SUPPORT.** Native return was +3.093%, PF 1.478, and max drawdown -1.802%. It stayed positive at 10 bps slippage (+0.503%) and with 5 bps slippage plus 5 bps commission (+0.346%), but turned negative at 15 bps (-1.582%). All 81 executed position paths were complete; strict full-session sensitivity was also positive (+3.814%). The result is therefore supported but materially transaction-cost sensitive.

D2/C is not supported: its one runner improved total return by only 0.017 percentage points relative to D1, while the strategy remained negative. D1/B marginally improved all four comparison metrics versus B/configured, so it is classified **IMPROVES B**, but both B paths remained economically negative.

No parameters, score definitions, universe filters, or production presets were changed. No data sync, SEC request, Alpaca request, synthetic bar, or network prefetch was performed. This report does not promote a strategy and is not a live-trading recommendation.

## Frozen Environment

- Branch: `main`
- Commit: `ec673ecce22dfc70785b85119445f1b636a75021`
- Working tree: pre-existing D1-D5 research changes and untracked research artifacts were preserved; the extended-validation implementation is uncommitted.
- Config: `config/strategy.yaml`
- Config SHA-256: `1E30FE15D0C3D833616804CE4B4F8E5299A30D67609CBC100698281F33A5AAE9`
- Database: `data/trading_system.sqlite3`
- Database size: 7,217,475,584 bytes
- Assets: 13,441
- Companies: 6,088
- Fundamental facts: 16,391,706
- Daily bars: 3,578,927, from 2024-01-02 through 2026-08-19
- Native 15m bars: 148,691, from 2025-04-16 09:30 ET through 2026-08-12 15:45 ET
- Dataset states at freeze: assets, historical market data, daily, intraday, and snapshots successful; SEC state partial because of the already documented unresolved identity conflicts for EQR, LIDR, and PARA.
- Business-data counts after validation: identical to the frozen counts above. No business-data mutation occurred.

The baseline before extended-validation changes was 273 passing tests. The final suite after the extended implementation contains 286 tests; final verification results are recorded in the handoff output.

## Data Qualification

### Daily

Requested OOS: **2025-05-01 → 2026-04-30**  
Actual qualified OOS: **2025-05-01 → 2026-04-30**

The configured 300-session requirement implied a daily warmup start of 2024-02-21. SPY had the exact warmup. Of 4,627 representative eligible current-universe symbols, 3,901 (84.31%) had all required sessions at the first screen; 726 retained the normal production insufficient-history rejection. The audit found 269 internal daily gaps across 47 symbols and 163,734 edge/lifecycle gaps. The latter are not silently treated as internal data repairs.

The deterministic qualification rule required an exact SPY warmup and at least one genuinely screenable eligible symbol. It did not lower `min_market_history_days` or introduce an arbitrary completeness threshold into strategy semantics.

### Intraday

Local-only PIT discovery identified 16 symbols and 127 C candidate symbol-sessions: ABT, ADBE, BR, EAT, EXE, FSLR, ILMN, LNG, LRCX, MSFT, NOW, PTC, RMD, UBER, UTHR, and VST.

Candidate-session coverage was:

- COMPLETE: 92
- MISSING_SESSION: 16
- UNKNOWN_MARKET_ACTIVITY: 19; these are also counted as technically partial sessions
- Expected/present/missing bars: 3,290 / 2,822 / 468
- Missing 09:30 bars: 17
- Internal missing bars: 50
- Edge/lifecycle missing bars: 418

Across every session for the 16 candidate symbols, 2,038 of 4,016 symbol-sessions were complete, 725 were partial/unknown, and 1,253 were missing. Consequently, whole-period intraday coverage is **not fully qualified**. No missing data was fetched or synthesized. The actionable details are in the missing-data JSON and gap manifest.

### Trade-Path Coverage

All 81 executed native intraday positions had complete expected 15m timestamps from entry through exit:

- Trade-path complete: 81
- Missing bar before exit: 0
- Missing opening bar affecting entry: 0
- Gaps only after exit in the same session: 9

The nine after-exit gaps correctly do not mark the executed trade path incomplete. This distinction explains why the Native and strict-trade-path views are identical even though whole-session coverage is incomplete.

## Research Reference Regression

The 2026-05-01 through 2026-08-12 reference reproduced with zero reported difference at the configured deterministic tolerance.

| Strategy | Return | PF | Max DD | Positions |
|---|---:|---:|---:|---:|
| B/configured | +1.172% | 1.485 | -1.799% | 8 |
| C/configured | +1.587% | 1.697 | -3.863% | 7 |
| D1/C-swing-profit-lock | +2.532% | 2.258 | -2.968% | 7 |
| C/intraday-dynamic | +3.795% | 1.887 | -1.131% | 48 |
| D2/C-swing-runner | +2.563% | 1.885 | -2.968% | 7 |
| D1/B-swing-profit-lock | +0.788% | 1.327 | -2.302% | 8 |

This gate passed before OOS results were interpreted.

## OOS Period

The OOS interval contains 251 XNYS sessions and precedes the research-reference interval. Headline metrics below are OOS-only; they are not combined with the reference period.

| Strategy | Return | PF | Expectancy | Max DD | Win rate | Positions | Exposure |
|---|---:|---:|---:|---:|---:|---:|---:|
| B/configured | -5.373% | 0.594 | -0.207 | -7.465% | 38.46% | 26 | 13.61% |
| C/configured | -3.627% | 0.697 | -0.165 | -6.124% | 36.36% | 22 | 10.95% |
| D1/C-swing-profit-lock | -3.627% | 0.697 | -0.165 | -6.124% | 36.36% | 22 | 10.95% |
| D2/C-swing-runner | -3.610% | 0.698 | -0.164 | -6.107% | 33.33% | 21 | 11.03% |
| D1/B-swing-profit-lock | -5.311% | 0.596 | -0.197 | -7.405% | 37.04% | 27 | 13.52% |
| C/intraday-dynamic | +3.093% | 1.478 | +0.033 | -1.802% | 37.04% | 81 | 10.77% |

Expectancy follows the existing engine/report convention. Position-level bootstrap means are reported separately.

## C/configured

C/configured opened 22 positions and lost 3.627%. Average win was +6.61%, average loss -4.99%, best position +13.43%, and worst -8.46%. Mean MFE was +4.31%, mean MAE -3.92%, and mean giveback 5.08 percentage points. Its negative OOS result is the direct control for D1/C.

## D1/C

D1 preserved the frozen C selection, configured next-session daily entry, ATR/risk stop, +12% full target, and ten-session hard hold.

- Reached at least +1R: 6 positions
- Reached at least +2R: 1 position
- Break-even lock activations: 5
- +1R lock activations: 1
- Profit-lock exits: 0
- Losses after +1R or +2R: 0

Despite state-machine activation, no exit or portfolio economics changed. OOS return, PF, expectancy, drawdown, costs, and exposure are exactly equal to C/configured. This differs from the positive reference-period D1 effect and is the core reason for **NOT SUPPORTED**.

## D1 Paired Effect Analysis

All 22 positions paired exactly by symbol, signal date, and entry timestamp. Ten pairs had a nominal exit-reason difference, primarily the configured `time_exit` label versus the research `max_hold` label, but their dates and economics were unchanged.

- Paired positions: 22
- Economically changed exits: 0
- Direct exit-management PnL effect: 0.000
- Total closed-position PnL difference: 0.000
- Subsequent portfolio-path effect: 0.000
- Configured-only / D1-only positions: 0 / 0

Thus the decomposition is unambiguous: total D1 difference = zero direct lock effect + zero subsequent `max_positions=1` path effect.

## D2/C

D2 returned -3.610%, only 0.017 percentage points above D1, with PF 0.698. One position reached the +12% partial and created one runner. That runner returned +25.60%, contributed 2.534 units of net PnL, reached 33.48% MFE, and gave back 7.88 percentage points. Because the entire runner evidence is one position and the overall strategy remains negative, the runner hypothesis is **NOT SUPPORTED**, not promoted.

## B/configured

B/configured returned -5.373%, PF 0.594, expectancy -0.207, and max drawdown -7.465% across 26 positions. It is a mechanism robustness control, not a selection candidate for promotion.

## D1/B

D1/B returned -5.311%, PF 0.596, expectancy -0.197, and max drawdown -7.405% across 27 positions. These are marginal improvements over B/configured in all four comparison dimensions, satisfying the declared classification **IMPROVES B**. However, the absolute outcome remains negative and does not rescue the D1/C hypothesis.

## C/intraday-dynamic

The frozen immediate-trail strategy opened 81 positions and 93 execution legs. Native return was +3.093%, PF 1.478, max drawdown -1.802%, and turnover 54.12 times capital. It generated 12 partial targets/runners.

There were 49 same-entry-bar final exits, all losses. The 32 first-bar survivors had a 93.75% position win rate. This is consistent with the prior finding that immediate trail losses are numerous but economically small, while the survivor tail supplies the edge; no D3 trail guard was introduced.

The 12 runners contributed 4.098 units of net PnL, with mean runner return +1.74%, MFE +2.77%, and giveback 1.03 percentage points. No re-entry or production behavior was changed.

## Intraday Coverage Sensitivity

| View | Positions | Return | PF | Expectancy | Max DD |
|---|---:|---:|---:|---:|---:|
| Native | 81 | +3.093% | 1.478 | +0.033 | -1.802% |
| Strict full session | 72 | +3.814% | 1.674 | +0.045 | -1.255% |
| Strict trade path, post hoc | 81 | +3.093% | 1.478 | +0.038 | N/A |

All views have the same positive sign. Strict full-session is a data-quality sensitivity and is not ranked as a strategy. Strict trade path is post-hoc and does not rerun portfolio state; it equals Native because every executed path was complete.

## Monthly Stability

D1/C had 2 positive and 9 negative active months (18.18% positive; one zero-trade month). C/intraday-dynamic had 6 positive and 5 negative active months (54.55% positive; one zero-trade month).

Intraday gains were not confined to one month: May, June, November, December, January, and March contributed positively. August, September, October, February, and April were negative. The largest positive month was November (+1.493% of initial capital); the most negative was September (-0.719%). This is better distributed than D1 but still regime-variable.

## Quarterly Stability

D1/C had 2 positive and 3 negative quarters. Its return contributions were +1.939% in 2025-Q2, -2.158% in Q3, +0.902% in Q4, -3.775% in 2026-Q1, and -0.535% in the partial 2026-Q2.

C/intraday-dynamic had 3 positive and 2 negative quarters: +1.219%, -1.164%, +2.103%, +1.364%, and -0.429%, respectively. The positive result therefore spans three separate quarters, while Q3 2025 and partial Q2 2026 remain negative.

## Symbol Concentration

For D1/C, LRCX was the best contributor and ADBE the worst; removing the best contributor leaves an already-negative -4.484% post-hoc contribution. Concentration ratios for a negative total are not economically intuitive and are not used to claim robustness.

For C/intraday-dynamic, EAT was the best contributor and NOW the worst. EAT supplied 49.34% of total net PnL. The top three supplied 114.74%, meaning other symbols offset part of their gains. The strategy remains positive after removing EAT (+1.567%) and after removing the top two positive contributors EAT and LRCX (+0.511%). Profitability is therefore not entirely dependent on one symbol, although concentration remains material.

## Leave-One-Symbol-Out

The leave-one-symbol-out results are post-hoc diagnostics, not portfolio reruns. They do not reconstruct slot availability or future sizing.

- D1/C without best contributor: -4.484%; without worst: -1.240%; without top two positive contributors: -4.682%.
- C/intraday-dynamic without best contributor: +1.567%, PF 1.263; without worst: +3.379%; without top two positive contributors: +0.511%, PF 1.086.

No symbol blacklist or exclusion was derived.

## Cost Stress

| Strategy | Base 5 bps | 10 bps | 15 bps | 5 bps + 5 bps commission | 25 bps |
|---|---:|---:|---:|---:|---:|
| D1/C | -3.627% | -3.908% | -4.188% | -4.008% | N/A |
| D2/C | -3.610% | -3.884% | -4.156% | -3.979% | N/A |
| C/intraday-dynamic | +3.093% | +0.503% | -1.582% | +0.346% | -7.858% |
| D1/B | -5.311% | -5.621% | -5.930% | -5.747% | N/A |

For intraday, PF falls from 1.478 at base to 1.067 at 10 bps, 0.816 at 15 bps, 1.043 with commission, and 0.328 at 25 bps. The edge is therefore present under the mandatory 10-bps test but does not survive 15 bps.

## Path-Preserving Cost Stress

The fixed baseline execution path remained byte-for-byte stable for every diagnostic row.

- D1/C: -4.021% at 10 bps, -4.414% at 15 bps, and -4.021% at 5+5 bps.
- C/intraday-dynamic: +0.349% at 10 bps, -2.395% at 15 bps, and +0.349% at 5+5 bps.

The differences from full economic reruns quantify strategy-state/path changes caused by costs. At 15 bps, the full intraday rerun (-1.582%) is less negative than the fixed-path diagnostic (-2.395%); this is a path effect, not evidence that costs help execution.

## Bootstrap Uncertainty

The deterministic bootstrap used seed 20260820 and 10,000 resamples of closed position returns.

| Strategy | Positions | Mean | Median | 95% bootstrap interval for mean | P(mean > 0) |
|---|---:|---:|---:|---:|---:|
| D1/C | 22 | -0.774% | -3.264% | [-3.337%, +2.110%] | 27.56% |
| C/intraday-dynamic | 81 | +0.114% | -0.288% | [-0.051%, +0.295%] | 90.22% |

This is not an independence proof or formal significance test. Position returns are serially dependent and portfolio state, especially `max_positions=1`, matters.

## Benchmark

SPY returned +30.135% over the actual OOS interval, with CAGR +30.252% and max drawdown -8.878%. TraWalp is not required to beat raw SPY return because exposure is far lower.

The descriptive return/average-exposure proxy is -0.331 for D1/C and +0.287 for C/intraday-dynamic. This is a capital-efficiency description, not alpha, and ignores leverage feasibility and path dependence.

## Validation Decisions

- **D1/C: NOT SUPPORTED.** Return, PF, and expectancy are negative; there is no direct lock effect, no path effect, and no 15-bps resilience.
- **C/intraday-dynamic: STRONG OOS SUPPORT.** Return, PF, and expectancy are positive; it survives 10 bps; Native and both coverage sensitivities have the same sign; removing the best symbol leaves positive PnL. Failure at 15 bps is a material caveat.
- **D2/C: NOT SUPPORTED.** The overall result remains negative and the runner evidence is a single position.
- **D1/B: IMPROVES B.** Return, PF, expectancy, and drawdown all improve marginally versus B/configured, but both remain negative.

These classifications apply only to the frozen sample and rules. They do not select a best strategy or authorize promotion.

## Remaining Risks

- Survivorship bias and current-universe historical reconstruction
- Fundamental PIT completeness and incomplete historical metric coverage
- Short overall historical range and only 81 intraday / 22 D1 positions
- Residual whole-session bar gaps despite complete executed trade paths
- Daily OHLC ambiguity for daily-managed exits
- Position dependence and serial correlation
- `max_positions=1` portfolio-path dependence
- Transaction-cost and fill-model uncertainty; the intraday sign flips by 15 bps
- Selection instability after a future data refresh
- Multiple research-hypothesis risk
- D1 reference/OOS regime instability
- Intraday concentration in a small contributor set even though leave-one-out remains positive

## Final Decision

**READY FOR SECOND OOS / PAPER-TRADING DESIGN**

This readiness applies to further validation/design work for **C/intraday-dynamic only**. D1/C is not supported on OOS-A and should not advance on the strength of the research-reference interval. READY does not mean live trading, does not promote a production preset, and does not authorize broker or paper orders in this task.

## Artifacts

- [OOS summary JSON](../reports/old_reports/extended_validation_2025-05-01_2026-04-30_summary.json)
- [OOS summary CSV](../reports/old_reports/extended_validation_2025-05-01_2026-04-30_summary.csv)
- [Positions](../reports/old_reports/extended_validation_2025-05-01_2026-04-30_positions.csv)
- [Execution legs](../reports/old_reports/extended_validation_2025-05-01_2026-04-30_execution_legs.csv)
- [Daily and intraday qualification](../reports/old_reports/extended_validation_data_qualification.json)
- [Intraday missing-data decision file](../reports/old_reports/extended_validation_intraday_missing_data.json)
- [Intraday gap manifest](../reports/old_reports/extended_validation_intraday_gap_manifest.csv)
- [Trade-path coverage](../reports/old_reports/extended_validation_trade_path_coverage.csv)
- [Intraday sensitivity](../reports/old_reports/extended_validation_intraday_sensitivity.csv)
- [Monthly stability](../reports/old_reports/extended_validation_monthly.csv)
- [Quarterly stability](../reports/old_reports/extended_validation_quarterly.csv)
- [Symbol concentration](../reports/old_reports/extended_validation_symbol_concentration.csv)
- [Leave-one-symbol-out](../reports/old_reports/extended_validation_leave_one_symbol_out.csv)
- [Cost stress](../reports/old_reports/extended_validation_cost_stress.csv)
- [Path-preserving cost stress](../reports/old_reports/extended_validation_path_preserving_cost_stress.csv)
- [Bootstrap uncertainty](../reports/old_reports/extended_validation_uncertainty.json)
- [Research-reference regression](../reports/old_reports/research_reference_regression_2026-05-01_2026-08-12.json)
