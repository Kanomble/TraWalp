# TraWalp Post-Audit Baseline Revalidation

## Executive Summary

The repository-level correctness fixes were revalidated against the preserved comparison run for 2026-05-01 through 2026-08-12. No strategy parameter was changed, no market or SEC data was downloaded, no synthetic bar was created, and no database row was repaired or deleted.

The corrected run contains 14 strategy labels. Ten have identical headline results to the pre-audit run. Four labels change materially: the two ATR-trailing labels and the two partial-ATR labels. Their trade-level evidence points to TRW-005 (the highest active long protection stop wins). No trade in this period proves an effect from TRW-006 or TRW-007. Five `C/legacy` positions only receive the canonical TRW-008 exit-reason names; price and P&L are unchanged.

Daily history is complete for every symbol actually traded by either comparison plus SPY, including the 300-session indicator warmup. The broader current-tradable universe contains lifecycle/sparse-instrument gaps which cannot safely be equated with provider loss. The 15m candidate set has no wholly missing session, but 100 of 720 symbol-sessions have one or more missing native timestamps (154 bars in total). Their cause cannot be distinguished locally from halts or no-trade intervals. Local SEC raw payloads cover only 798 of 6,085 company identities, so historical `period_start` loss cannot be excluded universe-wide.

**Decision: NOT READY FOR D1-D5 STRATEGY DEVELOPMENT.** The corrected execution baseline is reproducible, but unresolved intraday market-activity semantics and incomplete universe-neutral SEC reconstruction still prevent a sufficiently strong separation between strategy effect and data uncertainty.

## 1. Environment

| Item | Value |
|---|---|
| Branch | `main` |
| Commit | `d0c7828ff5e871a8a6cfc991ada186475a9178cb` |
| Python | 3.12.7 |
| Config | `G:\GitHub\TraWalp\config\strategy.yaml` |
| Database | `G:\GitHub\TraWalp\data\trading_system.sqlite3` |
| Database size | 5,488,943,104 bytes |
| Validation period | 2026-05-01 through 2026-08-12 |
| Position timeframe under qualification | native 15m, regular XNYS session |
| Daily warmup | 300 exchange sessions; qualification starts 2025-02-21 |

The working tree was already dirty at task start because the completed repository audit was not committed. Those changes and the user's untracked `docs/codex_tasks.md`, `reports/`, and `results/` artifacts were preserved. This task did not reset, revert, commit, or remove them.

The database `mtime` changed from `2026-08-14T21:24:47.625250Z` to `2026-08-15T08:11:52.561667Z`. The existing CLI invokes `Database.initialize()` before comparison commands, which performs idempotent schema/view initialization. File size and all recorded business-data counts remained unchanged, and `PRAGMA quick_check` returned `ok`.

## 2. Baseline

Before new task changes:

- Pytest: 246 passed; one transitive `websockets.legacy` deprecation warning.
- Ruff: passed.
- `git diff --check`: passed, with Windows LF-to-CRLF notices only.
- Reference report hashes: all four exactly matched the audit reference hashes.
- Database counts: 6,085 companies; 11,899,061 facts; 4,619 market snapshots; 12,356 sync-state rows.

Bar inventory remained unchanged throughout qualification:

| Timeframe | Symbols | Bars | First | Last |
|---|---:|---:|---|---|
| 1d | 6,193 | 3,549,963 | 2024-01-02 | 2026-08-12 |
| 15m | 18 | 141,145 | 2025-04-16 13:30Z | 2026-08-12 19:45Z |
| 5m | 5 | 4,775 | 2026-07-27 13:30Z | 2026-08-12 19:55Z |
| 1h | 5 | 390 | 2026-07-27 14:00Z | 2026-08-12 19:00Z |

The preserved reports still have these hashes after all work:

| Report | SHA-256 |
|---|---|
| Comparison | `464f030d151327b845c4e9f1b39cac567b6c0df9771f20ca268697555d268907` |
| Positions | `a8c9fc5b405cc28a19dbd59f7bb1c475cd003ead1033e8667edd72824880d6eb` |
| Execution legs | `67c9fa88a5539deb3c06c774b5352068ef6ed34e156e33518054c90797f82e61` |
| Post-exit | `9fa7f328efca47592443a72fbe1767c2444011326a51ff22bd7deb95a77f7d37` |

## 3. Data Qualification

### Daily

Qualification reads actual bars independently of `daily_history_coverage`; old metadata cannot mask an internal gap.

| Scope | Symbols | Expected symbol-sessions | Present | Missing | Internal missing | Invalid/duplicate |
|---|---:|---:|---:|---:|---:|---:|
| Current-tradable universe + SPY | 6,054 | 2,239,980 | 2,068,452 | 171,528 | 810 across 58 symbols | 0 / 0 |
| All old/new traded symbols + SPY | 11 | 4,070 | 4,070 | 0 | 0 | 0 / 0 |

Of the universe-wide missing observations, 170,718 are before the first or after the last locally observed bar and are classified as edge/listing-lifecycle uncertainty. The 810 bracketed observations occur mainly in sparse warrants/units and cannot safely be called provider gaps without an authoritative trade-status source. There are 881 cases where old coverage metadata claims the interval while actual bars contain some missing session. No metadata claim was accepted as proof of completeness.

### SEC / PIT

The local read-only qualifier reparsed retained raw Company Facts with the corrected TRW-002 identity, including `period_start`, and compared exact normalized keys against `fundamental_facts`.

| Measure | Result |
|---|---:|
| Company identities requested | 6,085 |
| Existing facts inspected | 11,897,875 |
| Raw Company Facts payloads available | 798 |
| Identities without reconstructable raw payload | 5,287 |
| Parsed facts from retained raw payloads | 3,156 |
| Raw facts absent from the fact table | 708 |
| Proven missing alternate `period_start` contexts | 0 |
| Proven additional discrete-quarter contexts | 0 |
| Existing multi-start identity groups | 0 |
| Parse errors | 0 |

All 708 general missing facts belong to `ZSTK`, whose fact-table count is zero; they do not demonstrate the historical QTD/YTD collision that TRW-002 fixed. For the other 5,287 identities, the necessary raw payload is absent, so the pre-fix loss cannot be reconstructed from the database alone. A selective refresh of winners or current candidates would bias the historical cross-section. No such refresh was performed.

PIT availability remains `filed <= as_of`. Fact count, candidate screens, and A/B/C entries were therefore unaffected by a data repair in this task: there was no repair.

### Intraday 15m

Expected timestamps come from official XNYS sessions, regular open/close, and early closes. Only native stored timestamps count.

| Measure | Result |
|---|---:|
| Candidate symbols | 10 |
| Expected symbol-sessions | 720 |
| Expected native bars | 18,720 |
| Present native bars | 18,566 |
| Complete sessions | 620 |
| Fully missing sessions | 0 |
| Partial sessions | 100 |
| `UNKNOWN_MARKET_ACTIVITY` sessions | 100 |
| Missing timestamps | 154 |
| Extra/invalid/duplicate bars | 0 / 0 / 0 |

Seven symbols have at least one structural gap: ADSK 6, BR 31, CF 2, EAT 47, FSLR 5, PTC 29, and RMD 34 missing bars. Local bars cannot distinguish a halt, genuine no-trade interval, provider omission, or storage loss. No gap was filled, and no Daily fallback was used.

The bounded manifest contains one row per affected symbol/session, with expected/actual count, missing timestamps, structural status, cause confidence, and reason. Summary order and details are deterministic.

## 4. Repairs

| Area | Repair | Reason |
|---|---|---|
| Daily | None | Comparison-traded symbols plus SPY are complete; broader sparse/lifecycle gaps are not proven provider loss. |
| SEC | None | Missing historic `period_start` contexts cannot be identified for 5,287 identities; a selective refresh would bias PIT selection. |
| Intraday | None | All 154 gaps have unresolved market-activity cause and therefore fail the safe-repair gate. |

No provider API was called. No sync, SEC refresh, upsert, synthetic bar, delete, migration, broker endpoint, paper-trading endpoint, or order endpoint was invoked.

## 5. Pre-Audit vs Post-Audit Strategy Comparison

The post-audit run used `--include all`, local-only intraday data (`--no-intraday-prefetch`), native 15m bars, and the same configuration and presets as the preserved run.

| Strategy | Old return | New return | Delta | Old DD | New DD | Old/New positions | Old PF | New PF |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A/configured | 9.7803% | 9.7803% | 0.0000% | -1.3348% | -1.3348% | 11 / 11 | 5.825 | 5.825 |
| B/configured | 8.5381% | 8.5381% | 0.0000% | -1.3348% | -1.3348% | 8 / 8 | 26.246 | 26.246 |
| C/configured | 2.2923% | 2.2923% | 0.0000% | -2.5092% | -2.5092% | 7 / 7 | 2.055 | 2.055 |
| C/legacy | 2.2923% | 2.2923% | 0.0000% | -2.5092% | -2.5092% | 7 / 7 | 2.055 | 2.055 |
| C/dynamic-hold | -0.9102% | -0.9102% | 0.0000% | -15.6691% | -15.6691% | 4 / 4 | 0.701 | 0.701 |
| C/take-profit | 0.2437% | 0.2437% | 0.0000% | -5.0817% | -5.0817% | 29 / 29 | 1.021 | 1.021 |
| C/atr-trailing | -1.2277% | -0.7414% | +0.4862% | -4.0738% | -3.9527% | 17 / 17 | 0.815 | 0.880 |
| C/partial-profit | -0.1899% | 0.1174% | +0.3073% | -2.9436% | -2.8210% | 17 / 17 | 0.963 | 1.024 |
| C/intraday-dynamic | 5.5844% | 5.5844% | 0.0000% | -0.8696% | -0.8696% | 46 / 46 | 2.345 | 2.345 |
| C/baseline-fixed-stop | -0.9102% | -0.9102% | 0.0000% | -15.6691% | -15.6691% | 4 / 4 | 0.701 | 0.701 |
| C/fixed-stop-max-hold | 10.6133% | 10.6133% | 0.0000% | -3.1486% | -3.1486% | 11 / 11 | 2.558 | 2.558 |
| C/fixed-stop-take-profit | 0.2437% | 0.2437% | 0.0000% | -5.0817% | -5.0817% | 29 / 29 | 1.021 | 1.021 |
| C/fixed-stop-atr-trailing | -1.2277% | -0.7414% | +0.4862% | -4.0738% | -3.9527% | 17 / 17 | 0.815 | 0.880 |
| C/fixed-stop-partial-atr | -0.1899% | 0.1174% | +0.3073% | -2.9436% | -2.8210% | 17 / 17 | 0.963 | 1.024 |

`fixed-stop-max-hold` has the numerically highest return in both runs (10.6133%); this is a descriptive baseline value, not a parameter or strategy recommendation. `intraday-dynamic` has the numerically smallest drawdown magnitude in the new run (-0.8696%).

## 6. Trade-Level Differences

The generated diff contains 49 position records and 63 execution-leg records because corrected early exits also slightly change later fractional quantities and dollar P&L through the equity path. The economically direct changes are narrower:

| Symbol / entry | Affected labels | Old exit | New exit | Return change | Attribution |
|---|---|---|---|---:|---|
| PTC / 2026-06-02 | ATR, partial-ATR and fixed-stop aliases | `stop_loss` at 138.292865 | `atr_trailing_stop` at 138.688828 | -3.0485% to -2.7709% | Confirmed TRW-005 |
| MSFT / 2026-06-04 | ATR, partial-ATR and fixed-stop aliases | `stop_loss` at 422.735594 | `atr_trailing_stop` at 423.159911 | -3.0485% to -2.9512% | Confirmed TRW-005 |
| ADSK / 2026-07-21 | ATR, partial-ATR and fixed-stop aliases | `stop_loss` at 207.065848 | `atr_trailing_stop` at 209.384212 | full: -3.0485% to -1.9630%; partial position: -0.7996% to -0.2569% | Confirmed TRW-005 |

There are 12 label-level direct position changes: three underlying positions repeated across four configured/alias labels. A further 32 position and 46 leg records contain only downstream quantity/P&L changes after the earlier corrected execution.

Five `C/legacy` positions change only `take_profit -> profit_target` or `max_hold -> time_exit`. Exit timestamp, price, quantity, and P&L are identical; this is confirmed TRW-008 serialization semantics.

- TRW-006: no partial-vs-full-target ordering event changed in this period.
- TRW-007: `C/intraday-dynamic` positions, legs, timestamps, prices, and headline results are unchanged.
- TRW-002/TRW-003: no data repair was performed, so no diff is attributable to them.

Post-exit differences occur for the same three underlying TRW-005 exits across four labels (12 records). Their horizon returns/MFE/MAE change mechanically because the corrected exit reference price is higher; no additional exit-date or future-bar change occurred.

## 7. Audit-Fix Attribution

| Effect class | Evidence | Conclusion |
|---|---|---|
| Strategy effect | Same parameters and same candidate/entry path; ten labels unchanged | No new strategy effect was introduced. |
| Execution-correctness effect | Three stops select the higher active ATR protection; four labels' returns improve | Confirmed TRW-005. |
| Reporting-semantic effect | Five legacy reasons canonicalized with identical economics | Confirmed TRW-008. |
| Datarepair effect | Zero bars/facts inserted or updated; relevant Daily history already complete | None. |
| Unresolved data uncertainty | 100 partial 15m symbol-sessions; 5,287 identities lack raw Company Facts | Material and unresolved. |

## 8. Hypothesis Revalidation

### H0-A — Intraday Opening Failure: SUPPORTED

Of 46 `intraday-dynamic` positions, 25 (54.35%) exit in the entry bar. They have 0% win rate, -0.4583% average return, 0% average MFE, and -0.4085% average MAE. The 21 first-bar survivors have 90.48% win rate, +1.3268% average return, +2.3182% average MFE, and -0.2078% average MAE.

### H0-B — Early ATR-Trail Activation: INCONCLUSIVE

Twenty-five ATR-trail exits occur in the entry bar, but the reports do not persist the first trail-activation timestamp, activation distance, or bars-to-activation. Exit execution alone cannot reconstruct activation timing reliably. No trail rule was changed.

### H0-C — 12% Full Target: PARTIALLY SUPPORTED

Across 15 reported `profit_target` positions, average realized return is 12.0353%. Seven have at least 5% post-exit MFE by day 5, and seven have a positive day-5 post-exit return. Average post-exit returns are +0.2081% (1d), +0.7419% (3d), +4.6956% (5d), and +8.7846% (10d); average post-exit MFE is 7.5349% at 5d and 16.3513% at 10d. Strong continuation exists, but not in a majority under the predefined 5%-MFE diagnostic.

### H0-D — Hard Max Hold: SUPPORTED

Five `fixed-stop-max-hold` positions exit by `max_hold`. Their average realized return is +10.0255%; four of five have a positive 5d post-exit return and three of five remain positive at 10d. Average post-exit return is +8.0748% at 5d and +11.9698% at 10d, with average post-exit MFE of 10.4089% and 15.5647%, respectively.

### H0-E — Dynamic Hold Giveback: SUPPORTED

The four `dynamic-hold` positions average 15.9182% giveback. The clearest case is FSLR entered 2026-05-05: MFE +49.4423%, realized return -3.1252%, giveback 52.5676%. This is diagnostic evidence only and does not prescribe a new exit rule.

### H0-F — A/B vs C Selection: SUPPORTED (descriptive, not causal)

The three variants use the same 70 production PIT screens.

| Variant | Candidate observations | Unique symbols | Entries | Win rate | Avg position return | DD | PF | Avg Q / V / O / T |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| A | 327 | 18 | 11 | 81.82% | 7.2174% | -1.3348% | 5.825 | 83.71 / 91.18 / 49.56 / 58.95 |
| B | 119 | 11 | 8 | 75.00% | 7.3358% | -1.3348% | 26.246 | 80.18 / 87.64 / 74.64 / 57.83 |
| C | 73 | 10 | 7 | 57.14% | 2.5415% | -2.5092% | 2.055 | 79.89 / 88.43 / 73.89 / 63.90 |

The earlier descriptive A/B-over-C observation survives the corrected execution run. It is not proof that any threshold or score weight should change, especially while PIT reconstruction remains incomplete.

## 9. Cost Stress

The default is 5 bps slippage and zero commission. Stress runs alter execution costs only; strategy settings remain fixed.

| Strategy | Baseline | 2x slippage (10 bps) | 3x slippage (15 bps) | 5 bps slippage + 5 bps commission |
|---|---:|---:|---:|---:|
| A/configured | 9.7803% | 9.6747% | 9.5692% | 9.6253% |
| B/configured | 8.5381% | 8.4273% | 8.3166% | 8.4051% |
| C/configured | 2.2923% | 2.2133% | 2.1343% | 2.1923% |
| C/intraday-dynamic | 5.5844% | 4.3275% | 3.1750% | 3.9766% |
| C/fixed-stop-max-hold | 10.6133% | 10.2913% | 9.9704% | 10.2066% |
| C/take-profit and alias | 0.2437% | -0.9416% | -1.4676% | -0.7208% |
| C/partial-profit and alias | 0.1174% | -0.0031% | -0.4431% | -0.4484% |
| C/atr-trailing and alias | -0.7414% | -1.2886% | -1.8128% | -1.3023% |
| C/dynamic-hold and baseline alias | -0.9102% | -1.0093% | -1.1082% | -1.0421% |

`intraday-dynamic` remains positive in all requested stress cases but loses 1.2569 percentage points at 2x and 2.4095 points at 3x slippage. The take-profit and partial-profit labels cross below zero under stress. These are sensitivity observations, not parameter selection.

## 10. Tests and Engineering Changes

Implemented components:

- Bounded Daily and native-intraday qualification with XNYS calendar and early closes.
- Read-only SEC raw-context comparison using the corrected XBRL identity.
- Qualification header and machine-readable comparison metadata.
- Non-overwriting custom comparison stems.
- Deterministic qualification JSON and bounded gap-manifest CSV.
- Automatic comparison-, position-, execution-leg-, post-exit-, hypothesis-, and cost-stress diffs.

New/extended behavior tests cover internal Daily gaps despite stale metadata, Daily early closes, complete/missing/partial 15m sessions, one and multiple missing bars, 15m early closes, after-hours exclusion, no synthetic mutation, deterministic summaries, comparison metadata, report non-overwrite, SEC alternate-period detection, missing raw SEC reconstruction, and preservation of the existing no-Daily-fallback behavior.

Final regression is recorded after report completion:

- Pytest: 256 passed; one transitive `websockets.legacy` deprecation warning (180.07 seconds).
- Ruff: passed.
- `git diff --check`: passed; Windows LF-to-CRLF notices only.
- SQLite `PRAGMA quick_check`: `ok`.

## 11. Performance

The comparison completed in approximately 692 seconds. The cost-stress batch completed in approximately 823 seconds while reusing shared PIT screens. Peak process working set observed during these runs was approximately 14.1 GB. Reconstructing the shared A/B/C PIT screens alone took approximately 580 seconds. This is a meaningful performance/memory risk but was not optimized in this correctness task.

## 12. Remaining Risks

- **Survivorship bias:** historical screening starts from the current tradable-company universe.
- **Daily OHLC intrabar ambiguity:** native Daily bars cannot order multiple same-bar price events.
- **Intraday halt/no-trade semantics:** 154 absent native timestamps across 100 candidate symbol-sessions remain cause-unknown.
- **PIT raw coverage:** 5,287/6,085 company identities cannot be re-parsed from local raw Company Facts.
- **Accounting-period coherence:** the fixed parser preserves future QTD/YTD contexts, but the old database has no multi-start groups and cannot prove what was historically discarded.
- **Database initialization side effect:** CLI initialization updates the DB file `mtime` even when business-data counts remain stable.
- **Resource usage:** compare and PIT-screen preparation are memory-heavy and slow on the full universe.

## 13. Readiness Decision

**NOT READY FOR D1-D5 STRATEGY DEVELOPMENT**

Required blockers before D1-D5 work:

1. Resolve the 100 partial 15m candidate symbol-sessions using an authoritative provider/trade-status source, repairing only timestamps proven to be provider/storage loss; then rerun qualification and comparison.
2. Perform an universe-neutral SEC Company Facts refresh/reconstruction through the existing idempotent sync path, or establish an equivalent complete raw snapshot; measure newly restored `period_start`/discrete-quarter contexts and rerun PIT candidate screens.
3. Repeat the old/new attribution after those data operations. Only then can datarepair effects be separated from strategy effects for the same baseline period.

## 14. Artifacts

Primary reports:

- `reports/all_comparison_2026-05-01_2026-08-12_post_audit.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_positions.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_execution_legs.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_post_exit_analysis.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_data_qualification.json`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_gap_manifest.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_sec_qualification.json`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_candidate_audit_variants.json`

Diff and diagnostics:

- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_old_vs_new.json`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_old_vs_new.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_position_diff.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_execution_leg_diff.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_post_exit_diff.csv`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_hypothesis_revalidation.json`
- `reports/all_comparison_2026-05-01_2026-08-12_post_audit_cost_stress.csv`

Cost-run source reports use suffixes `_cost_2x`, `_cost_3x`, and `_commission_5bps` with their JSON, summary, positions, execution-leg, and post-exit exports.
