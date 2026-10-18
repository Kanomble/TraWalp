# PAIRS-STAT-ARB-V1-DAILY: local symmetry and convergence audit

Diagnostic research over the existing validation for 2024-01-02 through
2026-08-12. The strategy, configuration, database, identity qualification,
Daily qualification and manifest are unchanged. No discovery, preflight,
provider request, validation or new strategy backtest is performed.

The audited checkout matches remote `main` at
`c1960ed8aeee27fd3e98794720f1dae2086cb1f4` (GitHub branch read on
2026-10-18). The unrelated, already staged `.codex/config.toml` is preserved.

## Frozen population and reproducibility

Input stem: `reports/pairs_stat_arb_v1_2024-01-02_2026-08-12_v1`.
The compatible V9 manifest is
`reports/pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_pair_candidates.json`.
Its checksum and validation fingerprint both equal
`b3576dcfea70d2e6a854bca7edf500e574724a8dacd2609183c041a9a55295c4`.

The runner verifies manifest economics against the current frozen definition,
compares every saved candidate membership/calibration and signal observation,
and requires signals and trades to identify the same 1,991 executed trades.
Existing summary, trades, signals, pair candidates, correlation/Z/exit/holding/
leg attribution, yearly, monthly and chronological-thirds artifacts are hashed
before and after the audit. Their hashes are retained in the diagnostic JSON.
This is scoped verification of saved observations, not candidate rediscovery or
a new universe qualification. Historical source-fingerprint inputs outside these
observations are not reloaded to reconstruct the entire qualification run.

Run from the repository root using the configured interpreter:

```powershell
.\.venv\Scripts\python.exe -m trading_system.backtest.pairs_stat_arb_v1_diagnostics
```

Use a new `--output-stem` when repeating; existing diagnostics are preserved.
SQLite connections use `mode=ro` and `PRAGMA query_only=ON`. The executable
runner blocks socket connections. It calls only local loading, frozen pair
mathematics and post-trade aggregation. It never calls `simulate_trade` or
`simulate_prepared_pairs`; synthetic tests may call them as regression oracles.

## Pair order and symmetry

`pairs_stat_arb_v1_data.discover_pairs` first selects Top 5 by ADV, then forms
`combinations(sorted(by_symbol), 2)`. Thus A/B is lexicographic, independently
of liquidity rank, economic role or expected returns.
`pairs_stat_arb_v1_manifest.verify_membership` independently reconstructs that
canonical membership and rejects a reversed additional candidate.
The executor groups by the supplied ordered tuple; canonical uniqueness is
guaranteed at the verified manifest boundary. Its raw helper should not be
misrepresented as normalizing arbitrary, unverified B/A input.

The audit recalculates both representations for every saved pair-session state
using production calibration mathematics and the frozen identity evidence.
It compares log-spread, mean, sample standard deviation, z and the 59-return
Pearson correlation, with absolute tolerance `1e-10` and relative tolerance
`1e-9`. There is no requirement for bit identity.

Trade checks retain the existing entries and prices. They verify directions,
economic long/short symbols, next-open fills, crossing/max-hold timing, exit
prices, gross/net pair returns, cost drag, holding period and MFE/MAE after
swapping leg labels. Pure arithmetic checks do not create a second executed
trade population. The existing MFE/MAE convention excludes the close of a
scheduled next-open exit session, matching V1 exactly. Canonical candidate
uniqueness and actual active-trade intervals are also checked.

Any symmetry violation produces a detailed violation CSV and
`INVESTIGATE_IMPLEMENTATION_FIRST`, before forward-path analysis. No strategy
bug is automatically repaired. A regression test injects a discrepancy and
verifies that forward analysis is never called.

With mirror invariance established, the same economic trade has the opposite
z sign after relabeling. Therefore:

```text
POSITIVE_VS_NEGATIVE_Z_ATTRIBUTION_IS_LABEL_DEPENDENT
DO_NOT_USE_Z_SIGN_AS_STRATEGY_FILTER
```

## Path semantics, timing and censoring

The primary path is V1's actual **rolling** z: every observation uses the
preceding 60 official closes, excludes the current close, and applies sample
standard deviation. Holding observations are not gated again on correlation
or Top-5 membership. The actual mean exit triggers on sign crossing and
executes at the next official open. The fifth holding-session close has max-hold
precedence; a crossing there does not create a mean-reversion exit.

**Clock:** entry occurs at the next session's open. H1 is that session's close,
H2 the following close, and H5 V1's fifth holding-session close. Consequently,
CSV fields `z_t_plus_H` mean H completed holding sessions from entry open,
not H calendar days or H sessions after the entry-day close. Each horizon's
actual date is exported. `entry_z` is the persisted signal-close z; there is
no new observation or entry decision at entry open.

The separate `fixed_*` columns use **FIXED_ENTRY_CALIBRATION_Z**, freezing the
persisted signal/entry calibration mean and standard deviation. They measure
the original divergence against its original reference. Rolling contraction
alone may reflect a moving calibration mean or expanding standard deviation.
The two paths are never pooled.

First passages are first observed closes in H1-H15; no intraday crossing is
inferred. Horizon summaries require a complete observable prefix H1-H, with
separate denominators for the rolling and fixed paths. Missing local bars,
unusable frozen identity intervals or incomplete calibration produce null
values and `CENSORED_FORWARD_PATH`. There is no forward fill or external fetch.
Threshold hits among only complete prefixes are descriptive complete-case
rates, not survival estimates; censoring need not be independent of outcomes.

The 61 original mean exits use crossing triggers through H4; forward
`zero_cross_by_5` also observes H5 and may therefore exceed 61. Paths continue
after original exits only as diagnostics, even if another frozen trade in the
same pair subsequently overlaps. No new exits or portfolio PnL are simulated.

Diagnostic classes use a transparent priority order: actual sign crossing;
otherwise minimum absolute z <=1; otherwise >=20% contraction from entry;
otherwise minimum absolute z remains >= entry and terminal absolute z expands
by >=20%; otherwise no convergence. An additional flag identifies >=20%
contraction followed by terminal redivergence to at least entry absolute z.
These are descriptive labels, never strategy rules.

Z buckets are `[2,2.5)`, `[2.5,3)` and `[3,infinity)`; correlation buckets are
`[.7,.8)`, `[.8,.9)` and `[.9,1]`. Original chronological-third boundaries are
reused; year and month refer to signal date. Threshold-passage medians are
conditional on a hit within the indicated observable horizon.

## Economic and market diagnostics

Forward pair returns use unchanged economic legs and 50/50 weights, original
entry reference opens and horizon closes. They are gross
`FORWARD_DIAGNOSTIC`, without a new cost or execution model. Pearson and Spearman
relate `entry_abs_z - horizon_abs_z` to those returns, separately for rolling
and fixed paths. Larger positive contraction is expected to accompany a
positive gross pair return; this is association, not a causal estimate.

Leg tables use the original executed trade returns. Each leg is reported
unweighted; pair returns equal half the long return plus half the short return.
Tables compare Z bucket, correlation bucket, year, current SIC2, original
same-window SPY return, strong market days and pre-signal beta differences.
Strong market days mean SPY close-to-close moves >=+1% or <=-1% during the
original holding sessions, allowing a BOTH category. These include market
moves outside the exact next-open-to-open exit window and are contextual.
SPY window buckets use +/-1% over the original execution window.

Betas use only the 59 returns from the same preceding 60 signal calibration
closes and local SPY, without external data. Pair beta difference is long beta
minus short beta; its absolute 0.5 split is diagnostic only. Dollar neutrality
does not imply beta neutrality. Industry concentration and market associations
cannot establish a permanent structural short-side defect from this sample.

The JSON artifact index links all compact output tables. Raw forward bars are
not dumped. The full per-trade table retains the existing trade fields and
adds path, first-passage, convergence and local-market diagnostics.

## Leakage and verification

Forward readers take existing trade rows. They have no interface returning
candidate membership or execution decisions. Tests replace discovery,
preparation, signal selection and execution entry points with failures, then
run forward diagnostics and mutate future bars. Frozen trades and prior
signal observations remain identical. Another test verifies SQLite write
rejection and blocked network connections.

Results remain descriptive over a previously observed validation sample.
Overlapping trades are dependent. Current-universe, current-SIC, qualification
selection bias, non-PIT reference and per-bar provenance limitations from the
original validation continue to apply. No best bucket is selected as optimal.

Focused checks executed (no full pytest or new preflight):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pairs_stat_arb_v1_diagnostics.py tests/test_pairs_stat_arb_v1.py tests/test_pairs_stat_arb_v1_definition.py tests/test_pairs_stat_arb_v1_manifest.py
.\.venv\Scripts\python.exe -m pytest tests/test_pairs_stat_arb_v1_diagnostics.py
.\.venv\Scripts\python.exe -m ruff check src/trading_system/backtest/pairs_stat_arb_v1_diagnostics.py tests/test_pairs_stat_arb_v1_diagnostics.py
.\.venv\Scripts\python.exe -m ruff format --check src/trading_system/backtest/pairs_stat_arb_v1_diagnostics.py tests/test_pairs_stat_arb_v1_diagnostics.py
git diff --check
```

The combined run passed 201 tests, including the then 22 new diagnostics;
after adding the injected-failure guard test, all 23 diagnostics passed.
Thus 179 existing Pairs regressions and 23 diagnostic tests passed.
The combined run emitted one existing third-party `websockets.legacy`
deprecation warning. Whitespace checks also explicitly include the three new,
untracked files using `git diff --no-index --check` against `/dev/null`.

## Measured audit result

**SYMMETRY_PASS.** All 1,443 candidate pairs, 190,849 pair-session states and
1,991 executed trades were checked. Spread, mean, standard deviation, z,
correlation, legs, entries, exits, returns and pair-lock violations: **zero**.
Maximum numeric mirror error: `7.05546732149287e-14` (z). Gross/net return,
cost and entry-price errors: exactly zero in this sample. No production bug
was found and no production path was changed.

The original regression baseline matches exactly: 1,991 trades, gross
expectancy +0.092033%, net expectancy -0.008484%, modeled cost drag 0.100517%,
61 mean exits and 1,930 max-hold exits. Those results are retained as the
baseline, not re-executed.

### Actual V1 rolling path

Hits mean at least one observed close through H, conditional on a complete
prefix. Counts and percentages use the displayed observable denominator.

| H | Observable / censored | Hit abs(z)<=1.5 | Hit abs(z)<=1 | Hit abs(z)<=0.5 | Zero crossing | Median terminal ratio | Median minimum abs(z) |
|---|---|---|---|---|---|---|---|
| 5 | 1,991 / 0 | 794 (39.88%) | 324 (16.27%) | 135 (6.78%) | 79 (3.97%) | 0.7368 | 1.6591 |
| 10 | 1,991 / 0 | 1,325 (66.55%) | 696 (34.96%) | 353 (17.73%) | 203 (10.20%) | 0.6003 | 1.2627 |
| 15 | 1,986 / 5 | 1,644 (82.78%) | 1,021 (51.41%) | 578 (29.10%) | 361 (18.18%) | 0.5148 | 0.9727 |

Terminal absolute z is closer than entry for 81.01%, 88.55% and 92.90% at
H5/H10/H15. This is clear convergence **in V1's moving z reference**; zero
crossing alone substantially understates its magnitude. The 79 H5 crossings
comprise the 61 actual mean-exit triggers plus 18 crossings first occurring on
the original max-hold close.

### Original divergence with fixed entry calibration

| H | Observable / censored | Hit abs(z)<=1.5 | Hit abs(z)<=1 | Hit abs(z)<=0.5 | Zero crossing | Median terminal ratio | Median minimum abs(z) |
|---|---|---|---|---|---|---|---|
| 5 | 1,991 / 0 | 502 (25.21%) | 228 (11.45%) | 105 (5.27%) | 71 (3.57%) | 0.9683 | 2.0007 |
| 10 | 1,991 / 0 | 728 (36.56%) | 429 (21.55%) | 234 (11.75%) | 156 (7.84%) | 0.9786 | 1.8058 |
| 15 | 1,986 / 5 | 893 (44.96%) | 569 (28.65%) | 356 (17.93%) | 252 (12.69%) | 1.0007 | 1.6392 |

Only 53.69%, 51.83% and 49.90% are closer at the terminal H5/H10/H15 close
than at the signal. Some genuine, sometimes temporary contraction exists;
1,269/1,986 paths at H15 cross zero, reach <=1, or contract at least 20%.
But 55.04% never reach <=1.5, 71.35% never reach <=1, and the typical terminal
original divergence is not smaller after 15 sessions. There are 350 fixed
paths with >=20% contraction followed by redivergence to at least entry abs(z).

Therefore **five sessions too short: INCONCLUSIVE** as an economic explanation.
Longer observation increases first-passage opportunities, but it does not
demonstrate durable closure of the original price-ratio divergence. Rolling
mean/std adaptation contributes materially to the much stronger primary-z
contraction. The audit does not separately decompose mean drift versus std
expansion, so it cannot assign a causal share to either mechanism.

### Z and correlation buckets

The following are complete-prefix H15 results; both paths use the same displayed
sample here. These describe different starting distances and are not matched
causal comparisons.

| Entry abs(z) | H15 observable / original count | Rolling hit <=1.5 / <=1 / zero | Fixed hit <=1.5 / <=1 / zero | Rolling / fixed median terminal ratio |
|---|---|---|---|---|
| [2,2.5) | 1,266 / 1,269 | 85.47% / 56.71% / 20.46% | 53.08% / 33.65% / 14.77% | 0.5822 / 1.0068 |
| [2.5,3) | 409 / 410 | 83.62% / 46.70% / 17.36% | 36.92% / 24.69% / 10.51% | 0.4977 / 0.9828 |
| >=3 | 311 / 312 | 70.74% / 36.01% / 9.97% | 22.51% / 13.50% / 7.07% | 0.3910 / 0.9800 |

**Weak entry-z signals less mean-reverting: NO supporting evidence.** They
reach absolute thresholds more often, partly because they start closer.
Larger signals show stronger relative contraction in rolling z, but this
ordering is much weaker in the fixed reference and does not imply faster
closure. This does not support choosing a stricter entry-z cutoff as a winner.

| Entry correlation | H15 observable / original count | Rolling hit <=1.5 / <=1 / zero | Fixed hit <=1.5 / <=1 / zero | Rolling / fixed median terminal ratio |
|---|---|---|---|---|
| [.70,.80) | 1,064 / 1,066 | 82.24% / 50.19% / 14.94% | 42.39% / 25.94% / 10.24% | 0.5142 / 0.9999 |
| [.80,.90) | 723 / 724 | 85.06% / 54.63% / 22.27% | 49.24% / 32.37% / 15.63% | 0.5077 / 0.9805 |
| [.90,1] | 199 / 201 | 77.39% / 46.23% / 20.60% | 43.22% / 29.65% / 15.08% | 0.5501 / 1.0552 |

**Higher correlation improves convergence: NOT MONOTONIC.** The middle bucket
has moderately higher hit rates here, while >=.90 does not improve on it and
has fixed terminal median divergence above entry. No optimal correlation
cutoff is established. The artifact tables also retain H1/H2/H3/H5/H10,
threshold-passage medians and diagnostic classes for each bucket.

### Regime stability and max-hold paths

| Signal year | H15 observable / original count | Rolling hit <=1.5 / <=1 / zero | Fixed hit <=1.5 / <=1 / zero | Fixed median terminal ratio |
|---|---|---|---|---|
| 2024 | 453 / 453 | 82.12% / 50.55% / 19.43% | 43.71% / 26.49% / 12.36% | 1.0377 |
| 2025 | 898 / 898 | 81.74% / 50.67% / 18.82% | 44.77% / 29.84% / 13.47% | 0.9844 |
| 2026 | 635 / 640 | 84.72% / 53.07% / 16.38% | 46.14% / 28.50% / 11.81% | 0.9800 |

The discrepancy between moving-z contraction and original-divergence closure
persists in all three years and the original chronological thirds. Thirds'
rolling H15 <=1 rates are 50.50% / 50.88% / 52.42%; fixed rates are
25.87% / 29.75% / 28.95%. It is not confined to a single annual regime.
Marginal bucket/year tables do not isolate causal parameter effects or remove
composition differences; no such claim is made.

Of the 1,930 original MAX_HOLD_5 trades, rolling H5 classifications are:
18 ZERO_CROSSED, 254 STRONG_CONVERGENCE, 1,110 PARTIAL_CONVERGENCE,
488 NO_CONVERGENCE and 60 FURTHER_DIVERGENCE. Fixed classifications are
15 / 164 / 661 / 785 / 305, respectively. Thus many max-hold trades do
partially approach their original reference, but few get close to equilibrium
inside the actual holding window.

For the 1,925 observable MAX_HOLD_5 paths through H15, fixed <=1.5 / <=1 /
zero rates are 43.38% / 26.81% / 10.03%. Their median fixed terminal ratio is
1.0069; 337 contract >=20% and subsequently rediverge. Extending observation
does not turn most max-hold trades into sustained original-mean closures.

### Pair returns, legs and market context

Rolling-z contraction versus forward gross return has Pearson/Spearman
0.482/0.768 at H5, 0.424/0.717 at H10 and 0.332/0.639 at H15.
The fixed-reference relationships are stronger: 0.697/0.874,
0.694/0.884 and 0.668/0.880. This supports the economic relevance of actual
original-divergence contraction, without establishing causality.

Gross fixed-horizon diagnostic mean pair returns are +0.09285%, +0.04864% and
+0.05517% at H5/H10/H15; medians are +0.14975%, +0.08103% and +0.08539%.
These are reference-open-to-close observations of the original economic legs,
not backtested new exits or a net-return comparison. H5 differs slightly from
the actual-exit baseline because actual mean exits execute at the open.
Longer observation does not show a rising unconditional economic return here.

The short-leg weakness is **broad with concentration and market dependence**:
26/48 traded SIC2 groups have a negative aggregate gross short contribution,
including 18/28 groups with at least 20 trades. All three years and all entry-z
and correlation buckets have negative mean gross short returns.
The largest negative industry aggregates are SIC2 35, 60 and 29; together they
represent about 38.7% of the sum of negative industry-level short aggregates.
The weakness is not attributable to one industry alone; small SIC2 groups are
not individually reliable evidence.

Unweighted mean gross short returns are -0.3373%, -0.4378% and -0.4686% in
2024/2025/2026, respectively. In holding windows with only strong market-up
days, the short mean is -1.9391% and pair net mean -0.1618% (486 trades).
For only strong market-down days, the short mean is +1.3698% and pair net mean
+0.0607% (387 trades). These are the explicitly defined contextual day buckets,
not a market-timing filter or causal attribution.

All 1,991 pre-signal beta differences are locally observable. At abs(long beta
minus short beta)>=0.5, 361 trades have net expectancy -0.0791%, versus
+0.0072% for the remaining 1,630. The continuous signed beta-difference/net
return Pearson relationship is only 0.093; original SPY-window/net-return
Pearson is 0.054. The results suggest descriptive exposure/regime associations,
not proof of a beta-neutralization remedy or a permanent structural short flaw.

### Decision and artifacts

**MOVE_TO_PAIRS_V2_MODEL.** Symmetry passes, but much of the apparent long-horizon
V1-z improvement does not correspond to closure against the original reference.
The median original divergence persists at H15, most paths do not reach <=1.5,
and neither a monotonic correlation effect nor a stronger-entry-z closure
advantage supports a narrow parameter refinement. Genuine transient
convergence exists; the conclusion is that it is insufficiently broad and
persistent to explain V1 as simply a five-session horizon problem.
This is a descriptive model-level research decision, not proof of absent
mean reversion in every pair, and no V2 engine is implemented in this task.

**A/B symmetric: YES. Z sign economically invariant: NO. V1 mean-reverts:
strongly in rolling z, only partially against the original entry reference.
Five sessions too short: INCONCLUSIVE.** No positive-z-only or best-bucket
strategy is recommended. Forward diagnostics did not affect selection or
execution; all original artifacts and the database file remained unchanged.

Local output stem:
`reports/pairs_stat_arb_v1_2024-01-02_2026-08-12_v1_diagnostic`.
The `_diagnostic_summary.json` contains the source hashes, numerical audit,
summary rows, assessment and index of the 13 CSV artifacts. The human assessment
is appended after inspection; the runner itself emits descriptive measurements
and automatically stops only on symmetry failure. Per-trade paths are about
4.1 MB; the symmetry table is about 295 KB. No raw-bar dump is emitted.

Repository files added:

- `src/trading_system/backtest/pairs_stat_arb_v1_diagnostics.py`
- `tests/test_pairs_stat_arb_v1_diagnostics.py`
- `docs/pairs-stat-arb-v1-diagnostic-audit.md`

Generated reports remain local under the repository's existing ignore rules.
The pre-existing staged `.codex/config.toml` was not edited or restaged.
