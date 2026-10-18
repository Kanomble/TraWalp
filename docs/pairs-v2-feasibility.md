# Pairs V2 feasibility: preregistered local relationship study

Contract recorded before measuring the development sample on 2026-10-18.
Base: remote main `ce23f726696eec379e133ab3c20b65f4b82beb30`.

- DEVELOPMENT_SAMPLE evaluations: 2024-01-02 through 2026-08-12, inclusive.
  Earlier local bars are calibration history only. No evaluation or current
  observation after 2026-08-12. Data from 2026-08-13 onward are reserved for a
  separate future validation after a strategy definition is frozen.
- Reuse all frozen V9 V1 pair candidates, including those below V1 correlation
  .70. No rediscovery, requalification, universe modifications or outcome input.
  Canonical direction only: log(A) = alpha + beta log(B) + residual, A < B.
  Direction dependence is retained; no reverse fit or best-of-direction choice.
- Exactly the preceding 252 completed official XNYS sessions, [t-252,t).
  No filling or shortened relationship window. Insufficient history is a
  feasibility availability reason and never a new V1 quarantine.
- OLS with intercept. Engle-Granger: statsmodels `coint`, trend='c',
  method='aeg', maxlag=12, autolag='aic'. AIC selects among lags 0..12;
  no alternative trend, lag method or maximum is tried.
- Ordinary residual ADF is separately labelled: `adfuller`, regression='n',
  maxlag=12, autolag='AIC'. Its ordinary unit-root p-value is not a valid
  replacement for the cointegration p-value (estimated-regressor critical
  values differ). Only Engle-Granger p-values define diagnostic evidence levels.
  The implementation reuses the exact ADF statistic already calculated inside
  statsmodels `coint` under those settings, and applies statsmodels
  `mackinnonp(stat, regression='n', N=1)` for the ordinary diagnostic p-value.
  Cointegration retains the library's estimated-regressor N=2/trend='c'
  mapping. Equivalence to an independently executed library `adfuller` is tested.
- Residual sample std uses ddof=1. Count sign changes around zero and lag-1
  autocorrelation. Exact zero observations are ignored in crossing counts.
- Half-life: OLS delta residual = intercept + lambda * lagged residual.
  phi = 1 + lambda. Use the exact discrete monotone AR(1) half-life
  log(0.5)/log(phi) for 0 < phi < 1, excluding numerically near-zero lambda.
  This avoids presenting -log(2)/lambda as an exact discrete-time formula.
  Invalid, nonfinite, phi<=0 or phi>=1 cases are HALF_LIFE_UNDEFINED with a
  reason. No clipping. No half-life eligibility filter.
- Split the same window into its first/second 126 sessions. Repeat intercept
  OLS and the identical Engle-Granger contract on each half, solely as a
  stability diagnostic. Relative beta change is abs(beta_second-beta_first)
  / max(abs(beta_full),1e-12); the epsilon is numerical protection, not a gate.
  Export half-window alpha changes too. No stability filter.
- Diagnostic p levels .10/.05/.01, beta buckets <=0 / (0,.25) / [.25,.5) /
  [.5,1) / [1,2) / >=2, half-life <=2 / (2,5] / (5,10] / (10,20] /
  (20,60] / >60 / undefined. Residual-z opportunities at abs(z)>=1.5/2/2.5/3
  are counts only. They do not create entries, exits, weights or PnL.
- Current close t is optional and used only to observe residual z relative to
  past parameters; it never changes the historical relationship state at t.
- Persistence runs require consecutive official sessions with tested frozen
  candidate membership; membership gaps, unavailable fits and failures break
  runs. Pair shares use successfully tested full-window states as denominator;
  attempted/unavailable counts are also reported. Overlapping windows create
  strong dependence; daily states and extreme observations are not independent
  opportunities or repeated independent statistical confirmations.
- Full/half stability is reported as all eight combinations of full/first/second
  rejection flags at every p level. Failures are UNAVAILABLE, not nonrejections.
- Raw p-values are descriptive and unadjusted for multiple testing. No selected
  best bucket, pair-PnL ranking, return study or causality claim is permitted.
  Feasibility judgment uses breadth, persistence, stability, speed and residual
  opportunity frequency together. No numeric decision cutoff is optimized.
- Guard missing/nonpositive/nonfinite prices, near-zero log-price variance,
  ill-conditioned fits, near-perfect collinearity and invalid library outputs.
  Near-collinear coint -inf outputs are numerical failures, never p=0 passes.
- Batch-read local bars once per symbol batch into run-local log arrays.
  Group CPU work by canonical pair, with bounded worker processes and one
  BLAS thread per worker. No per-pair SQL. Record SQL count, library versions,
  contract hash, source hashes, failure reasons and elapsed time.
  Identical 126-session subwindows of the same immutable pair arrays are cached:
  a second half at t is the identical first half at t+126. Cached and uncached
  states are regression-tested for equality. No different window is substituted.
- All V1 artifacts and manifests are preserved and hashed; V1 outcomes are
  never loaded as relationship inputs. SQLite uses mode=ro and query_only.
  The explicit command blocks network connections and constructs no provider
  clients. No new V1/V2 validation, backtest or trading strategy is executed.

`statsmodels>=0.14.6,<0.15` is an optional `research` dependency. Its established
Engle-Granger/ADF implementations are used; no approximate replacement tests
are implemented. Statistical outcomes remain DEVELOPMENT_SAMPLE evidence.

## Explicit local command

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli research-pairs-v2-feasibility `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --output-stem pairs_v2_feasibility_2024-01-02_2026-08-12_v1
```

The default is four worker processes. `--workers 1..8` controls CPU resources
only; it does not change the model or sample. The measured run uses eight.
Worker processes are local numeric computation, with no network or DB handles.
The CLI imports the research module only for this explicit command and returns
before the normal database/provider construction path. It rejects forward
dates and existing output stems. An optional `--candidate-manifest` must match
the exact frozen V1 manifest fingerprint; it cannot supply a new universe.

Outputs use the requested stem plus `_summary.json`, `_pair_summary.csv`,
`_session_statistics.csv` (one row per evaluation session),
`_beta_stability.csv`, `_half_life.csv`, `_cointegration_persistence.csv`,
`_sic2_attribution.csv`, `_yearly.csv`, `_chronological_thirds.csv`,
`_relationship_stability.csv`, and `_residual_z_opportunities.csv`.
All per-pair/session parameters and reason codes are preserved in compressed
`_pair_session_details.csv.gz`; raw bars are not dumped.

The main tested denominator requires a valid full-window OLS, Engle-Granger
and ordinary residual ADF result under the single contract. Half-window
failures remain separate stability availability counts. Current-close
availability is independent of historical fit availability, and opportunity
shares use only observable current residual-z states. Half-life/beta summaries
separately show all tested states, each nested diagnostic p level, and states
rejecting at .05 in all three windows; these are descriptive cohorts, not
eligibility rules. The exact-beta buckets never remove any fitted pair.

The 252 calibration sessions preceding the first development evaluation
start before 2024. They are historical fit inputs, never extra evaluation dates.
No Daily close from 2026-08-13 onward is read in this study.

## Focused verification

Executed the V2 tests together with the relevant V1 engine, definition,
manifest and diagnostic tests: 220 passed at that stage. After adding exact
ADF/cache-equivalence regressions and strengthening the PnL-isolation check,
all 20 V2 tests passed. The unchanged V1 portion comprises 202 tests.
Only these focused Pairs tests were run; no full pytest, preflight or backtest.
The existing `websockets.legacy` third-party deprecation warning remains.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_pairs_v2_feasibility.py tests/test_pairs_stat_arb_v1.py tests/test_pairs_stat_arb_v1_definition.py tests/test_pairs_stat_arb_v1_manifest.py tests/test_pairs_stat_arb_v1_diagnostics.py
.\.venv\Scripts\python.exe -m pytest tests/test_pairs_v2_feasibility.py
.\.venv\Scripts\python.exe -m ruff check src/trading_system/backtest/pairs_v2_feasibility.py tests/test_pairs_v2_feasibility.py src/trading_system/cli.py
.\.venv\Scripts\python.exe -m ruff format --check src/trading_system/backtest/pairs_v2_feasibility.py tests/test_pairs_v2_feasibility.py src/trading_system/cli.py
git diff --check
```

The statistical library was absent and installed into the configured project
venv: statsmodels 0.14.6, with new transitive dependencies SciPy 1.18.1 and
Patsy 1.0.3. Existing NumPy 2.5.2 and pandas 3.0.5 were retained. Package
installation used PyPI; the study itself has no network access or provider
construction. Dependency versions are recorded in the result JSON.

## Measured feasibility result

**PAIRS_V2_FEASIBILITY_WEAK — STOP PAIRS V2.** The study does not justify
freezing a trading strategy or launching a V2 backtest on the present evidence.
Residual reversion speed is plausible in statistically selected windows, but
relationship persistence, coefficient stability and the joint availability of
consistent relationships and extreme observations are weak. This is a
development-sample feasibility judgment, not a profitability claim or proof
that every individual pair lacks a long-term relationship.

### Coverage and execution

- Frozen candidates: 1,443 unique pairs and 190,849 pair-session states.
- Successfully tested: **1,437 unique pairs; 190,134 states**.
- Insufficient 252-session history: **715 states**; six pairs never have a
  complete window. No requalification, substitute window or provider repair.
- Full-window numerical failures: **0**. Half-window failures: **0**.
- Actual local calibration history starts **2022-12-29**; evaluations start
  2024-01-02. The final local close read is **2026-08-12**.
- **2 SELECT queries**, 3 SQL statements including query-only setup; eight
  local worker processes. No N+1 pair/session price queries.
- Complete-run elapsed time: **4,261.98 seconds (71.03 minutes)**; relationship
  phase 4,124.41 seconds. Long tool-response gaps occurred during execution;
  this is observed wall time, not a clean performance benchmark. An earlier
  artifactless computational attempt was interrupted to eliminate duplicate
  ADF/half-window work, without inspecting empirical test outcomes or changing
  the statistical contract.
- All **32 frozen source artifacts** retained identical SHA256 hashes.
  Database file size and modification time are unchanged. The JSON records
  the manifest, local log-input and statistical-contract hashes.

### Engle-Granger evidence and pair-level persistence

| Diagnostic level | Passing states / 190,134 | State share | Unique pairs ever passing / 1,437 |
|---|---|---|---|
| p<=.10 | 19,706 | 10.36% | 714 (49.69%) |
| p<=.05 | 10,619 | 5.59% | 564 (39.25%) |
| p<=.01 | 2,620 | 1.38% | 254 (17.68%) |

The aggregate rejection rates are only modestly above their nominal levels.
Because tests and pairs are dependent, assumptions may be imperfect and
multiple testing is unadjusted, this comparison alone neither proves that all
passes are false positives nor establishes a population of stable relationships.
The ordinary residual ADF p-values are separate diagnostics, not additional
independent confirmation: the same underlying ADF statistic is reused with
the ordinary rather than Engle-Granger MacKinnon mapping.

At p<=.05, **82 / 33 / 21** pairs pass in at least **25% / 50% / 75%** of
their successfully tested candidate sessions. Their median tested-state
counts are **39 / 9 / 9**, respectively; high fractions often have very short
exposure. No pair is selected on that basis. Median pair persistence across
all tested pairs is **0%**, and median longest p<=.05 run is **0 sessions**.
Among the 564 pairs ever passing, median longest run is **6 sessions**.

### Coefficient and residual-speed diagnostics

Among the 10,619 full-window p<=.05 states:

| Full beta bucket | States | Share |
|---|---|---|
| beta<=0 | 2,298 | 21.64% |
| 0<beta<.25 | 1,530 | 14.41% |
| .25<=beta<.5 | 1,275 | 12.01% |
| .5<=beta<1 | 3,160 | 29.76% |
| 1<=beta<2 | 1,976 | 18.61% |
| beta>=2 | 380 | 3.58% |

Median full beta is **0.5283**; median absolute half-window beta change is
**0.2504**, and median relative beta change is **42.51%**. Relative changes
are <=10% / <=25% / <=50% in **14.76% / 33.88% / 55.10%** of these states;
**44.90% exceed 50%**. Across all tested states, median relative change is
94.25%, with 69.34% exceeding 50%. Negative/small/large coefficients remain
in the diagnostics; none is removed through an eligibility rule.

Raw median absolute half-window alpha change is 1.1711 log-price units for
p<=.05 states (1.3223 in the full-and-both-halves cohort). Alpha changes are
coupled to beta changes and the regressor price level; they are not an
independent normalized instability statistic. Full/first/second alpha and
beta and absolute changes are retained per state.

Half-life is defined in 187,690 of all tested states and undefined in 2,444;
the valid median is **20.99 sessions**, with IQR **13.90–33.54**.
For full-window p<=.05 states, all **10,619** half-lives are defined:
median **7.45 sessions**, IQR **6.39–8.88**.

| p<=.05 half-life bucket | States |
|---|---|
| <=2 | 156 |
| (2,5] | 454 |
| (5,10] | 8,401 |
| (10,20] | 1,569 |
| (20,60] | 39 |
| >60 | 0 |
| undefined | 0 |

Thus speed alone is not the limiting result. These are in-window AR estimates
conditional on statistical selection, not independently validated future speeds.

### Full-window versus half-window evidence

At p<=.05, the full eight-way matrix is:

| Category | States |
|---|---|
| FULL_AND_BOTH_HALVES | **326** |
| FULL_AND_FIRST_ONLY | 1,818 |
| FULL_AND_SECOND_ONLY | 1,858 |
| FULL_ONLY | 6,617 |
| FIRST_AND_SECOND_BUT_NOT_FULL | 603 |
| FIRST_ONLY | 11,908 |
| SECOND_ONLY | 10,319 |
| NONE | 156,685 |

Only **0.171% of tested states**, or **3.07% of full-window p<=.05 states**,
reject in all three windows. This cohort contains **73 pairs**, with a median
of only **3 such states per pair**. Consecutive coherent episodes have median
length **2 sessions**, maximum **11**. Half-window tests have less power, so
failure to reject is not itself proof of relationship breakdown. Nevertheless,
the additional coefficient diagnostics do not rescue stability: even in this
cohort, median relative beta change is **51.89%**, and **50.92% exceed 50%**.
Its half-life median is 6.25 sessions (IQR 4.93–7.59), with no undefined cases.

### Residual-z observations, not trades

All successfully tested states have an observable current residual z. Counts
are nested diagnostic cohorts, not selected strategy settings:

| Cohort | States | abs(z)>=1.5 | >=2 | >=2.5 | >=3 |
|---|---|---|---|---|---|
| p<=.10 | 19,706 | 3,469 (17.60%) | 1,505 (7.64%) | 654 (3.32%) | 284 (1.44%) |
| p<=.05 | 10,619 | 1,927 (18.15%) | 867 (8.16%) | 374 (3.52%) | 156 (1.47%) |
| p<=.01 | 2,620 | 480 (18.32%) | 203 (7.75%) | 97 (3.70%) | 51 (1.95%) |
| Full and both halves p<=.05 | 326 | 65 (19.94%) | **25 (7.67%)** | 13 (3.99%) | 6 (1.84%) |

The 867 full-window p<=.05 / abs(z)>=2 states cover 226 pairs. With consistent
full/half test evidence, only **25 states from 15 pairs** remain across the
entire 655-session development sample. Adjacent extreme states are dependent
and can describe the same episode, so these are not 25 independent trades.
No beta or half-life filter is applied to obtain these counts.

### Industry and regime breadth

All **51 SIC2 groups with pair candidates** have tested states; 47 have some
full-window p<=.05 evidence. The small full-and-both-halves cohort spans 37
groups. Evidence is spread across industries but thin and
episodic, rather than attributable to one industry that could be removed or
retained as a winner. No SIC2 exclusion is introduced.

| Year | Tested states | Full p<=.05 | Share | Full and both halves p<=.05 | Coherent abs(z)>=2 states |
|---|---|---|---|---|---|
| 2024 | 53,770 | 2,786 | 5.18% | 82 | 5 |
| 2025 | 79,033 | 4,242 | 5.37% | 140 | 13 |
| 2026 | 57,331 | 3,591 | 6.26% | 104 | 7 |

Original chronological-thirds full p<=.05 shares are **5.15% / 5.09% / 6.26%**.
Weak broad stability and sparse coherent extremes occur across the sample;
there is no single strong annual regime that resolves the feasibility issue.

### Decision, isolation and retained files

**Would the present evidence justify a V2 backtest tomorrow? NO.** There are
occasional and some repeated reversion findings, and conditional half-lives
are compatible with Daily research. But frequent full/half inconsistency,
substantial hedge-ratio changes and sparse joint relationship/extreme states
do not support a sufficiently stable opportunity population. The decision
does not require positive V1 profits and uses no return or PnL input.

**PAIRS_V2_FEASIBILITY_WEAK — STOP PAIRS V2.** No strategy definition, final
p/z/half-life/beta rule, backtest or forward validation is implemented.
No alpha/beta sizing, entry, exit, holding period, cost or short-feasibility
rule is selected. The p levels and cohorts remain diagnostic only.

All fits use data only through **t-1**. Current t prices affect only the
separate residual observation; future-date mutation tests verify this boundary.
**V1 unchanged; network_used=false; database_writes=false.** No universe,
identity, Daily qualification, manifest, database content or V1 result changed.

Result stem:
`reports/pairs_v2_feasibility_2024-01-02_2026-08-12_v1`.
The summary JSON indexes ten compact CSVs and the compressed per-state file
(about 53 MB). Session statistics are about 104 KB; the pair and persistence
tables about 200 KB each. The descriptive human assessment is appended to the
JSON after inspecting the fixed diagnostics; the runner itself does not
optimize or automatically choose strategy thresholds.

Changed repository files:

- `README.md`
- `pyproject.toml`
- `requirements.txt`
- `src/trading_system/cli.py`
- `src/trading_system/backtest/pairs_v2_feasibility.py`
- `tests/test_pairs_v2_feasibility.py`
- `docs/pairs-v2-feasibility.md`

The pre-existing staged `.codex/config.toml` is preserved. Generated research
artifacts stay local under the existing report ignore rules. Ruff, format and
`git diff --check` pass, including explicit whitespace checks for the three
new files. No full pytest, preflight or new validation/backtest was run.
