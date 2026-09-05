# Research code audit — 2026-09-05

Scope: tracked source, tests, configuration, README, research documentation and historical
report schemas, including the existing uncommitted regime-capacity implementation. The audit
preceded Lifecycle V2 implementation. No historical backtest or sync was run for this audit.
The supplied 2022–2023 capacity yearly CSV still uses the canonical calendar-stability schema.

## 1. Active production/research paths

`cli.py` dispatches screening, Daily backtests, comparisons, extended/champion validation,
exact LOSO, capacity and regime-capacity. `research_registry.py` is the identity/family authority;
`screen_strategies.py` defines A–F selection; `presets.py` resolves management configuration.
The current working tree already contains the regime provider and CLI: these are retained.

## 2. Frozen/reproducibility-critical paths

F/configured C1 is the frozen champion. F0/C-intraday-dynamic remains the historical intraday
control. F1/F2/F4 remain explicitly callable archived hypotheses; F3/F5/F-intraday/F-entry
remain registered research. D1–D5, legacy comparison families, configured controls and
historical management presets are reachable by enum/registry/CLI dispatch. None is dead code.

## 3. Shared infrastructure

Retain `engine.py`, `position_manager.py`, `features.py`, `metrics.py`, `diagnostics.py`,
`report.py`, `coverage.py`, `qualification.py`, `preflight.py`, `universe_provenance.py`,
`data/intraday_remediation.py`, `data/market_sessions.py` and SIC helpers in
`fundamentals/peers.py`. Retain PIT accounting, identity conflict exclusions and local Daily
qualification. `validation.py` supplies canonical cost reruns, calendar stability, thirds,
symbol concentration and exact LOSO; its older report labels remain compatibility data.

## 4. Duplicate implementations

Candidate inventory (paths below are relative to `src/trading_system/`):

| file | symbol/function/class | reason | references searched | removal_status | risk |
|---|---|---|---|---|---|
| backtest/engine.py | research_strategy_label | Wrapper delegates to central registry | src, tests, docs, README; imported by validation/complete_validation and exercised by engine tests | KEEP | Removing breaks historical callers |
| backtest/engine.py | FIRST_HOUR_PULLBACK_PRESETS | Alias of shared entry collection | src, tests, docs, config; used in event presets and position metadata | KEEP | Entry metadata compatibility |
| backtest/engine.py | RESEARCH_PRESETS | Registry-derived compatibility collection | src, tests, README/docs; asserted in test_backtest_engine | KEEP | Family composition regression |
| backtest/capacity_validation.py | _profit_factor | Small arithmetic overlaps metrics/diagnostics | src, tests, docs; called by entry_rank_analysis_rows | DEFER_MANUAL_REVIEW | Denominators/zero-loss semantics need unified contract |
| backtest/intraday_isolation.py, intraday_next.py, intraday_hybrid.py | family exporters and coverage annotations | Similar export scaffolding | CLI dispatch, registry families, tests/test_intraday_*, historical report keys, docs | KEEP | Family-specific schemas/coverage differ |
| backtest/complete_validation.py | module/report helpers | Older manual orchestration, no current CLI dispatch | repository-wide module/symbol and historical artifact/documentation references | DEFER_MANUAL_REVIEW | Manual/dynamic external callers cannot be disproved |
| backtest/revalidation.py | generate_revalidation_artifacts | Older artifact orchestration | src, tests, docs/post-audit-baseline-revalidation.md, historical report names | DEFER_MANUAL_REVIEW | Historical artifact reproduction; external callers unknown |
| backtest/features.py, technical/momentum.py | optimized technical features / technical_snapshot | Two feature paths | screen sources, indicator/features/screener tests, config slope lookback | KEEP | Optimized source and reference calculation both tested |
| models/market_data.py | DailyBar | Alias of MarketDataBar | imports across source/tests, docs and default timeframe contracts | KEEP | Widely used public compatibility name |
| models/backtest.py, position_manager.py | historical enums/state/diagnostic fields | Many fields belong to older hypotheses | engine/presets/registry/report serialization, historical tests and JSON/CSV keys | KEEP | Historical report compatibility |

Searches covered import names, symbol strings, enum values, registry compositions, parser choices,
dispatch branches, test imports/assertions, config keys and documentation/report identities.
Static absence of a CLI branch was not treated as proof of absence of manual or dynamic use.

## 5. Confirmed dead code

None established to the requested standard. No candidate passed **all** requirements: no imports,
registry, CLI, tests, docs, report dependence, dynamic use or configuration dependence, plus
central equivalence or proven unreachability. This is a conservative audit, not proof that every
line in the repository is necessary.

## 6. Removed code

None. No `SAFE_REMOVE` classification, hence no source/model/test deletion. Pre-existing report
directories and temporary test artifacts were also retained.

## 7. Deferred removal candidates

The three `DEFER_MANUAL_REVIEW` rows above require external manual-run provenance or a separately
tested reporting contract before consolidation. Do not remove archived families merely because
their historical results were poor.

## 8. Tests retained for historical reproducibility

All existing tests retained, especially engine invariants, position manager, champion F,
screen strategies, features/PIT, intraday isolation/next/F-entry/F-intraday, extended validation,
capacity, regime, candidate audit, qualification, preflight, intraday remediation, SEC identity
and universe provenance. Focused regression results are recorded in the implementation handoff;
the full suite is deliberately not run.

Completed focused verification: 129 distinct retained regressions plus 44 new lifecycle/entry
tests passed (173 distinct tests total). Ruff and format checks passed on the changed Python
files. No historical research jobs were used as a cleanup validation shortcut.

## 9. Documentation inconsistencies

`strategy-research-lifecycle.md` called F0 the current champion; clarify that it is the historical
intraday control and F/configured is the frozen champion. Older validation documents retain
their original OOS labels as historical artifacts; they cannot certify a clean holdout for new
hypotheses. All newly analyzed 2022–2026 history is DEVELOPMENT / RESEARCH. SIC identities and
tradable membership are current snapshots, not a historically versioned industry universe;
new peer reports must disclose that limitation even when all price observations are causal.

## 10. Architecture recommendations

Keep the central screen, sizing, execution, costs and reporting calculations. Add immutable,
opt-in lifecycle identities and a small management adapter; default engine behavior must stay
identical. Keep entry-quality research separate from lifecycle and capacity. Accept the existing
capacity-provider hook without constructing a Cartesian product. Diagnostics belong in an
observer/report layer: forward outcomes must never reach decision providers.

Daily execution audit: overnight stop gaps precede target gaps; within a Daily OHLC range stops
precede targets; targets precede close-based time exits. Existing configured time exits execute
at the Daily close. New target deferral must use the previous completed session's trend/peers,
since the current close is not known at an intrabar target touch. A new deterioration decision
at the close must execute at the next opening price, with stop-gap priority. No inferred
high/low ordering and no changes to the frozen path.
