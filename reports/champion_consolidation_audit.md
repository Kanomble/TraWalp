# Champion consolidation and repository audit

Date: 2026-09-14. Scope: source packages, CLI/registries, configuration, tests,
packaging, documentation and report/migration contracts. Static inspection and small
offline fixtures only. No provider requests, production database runs, historical
validation, profiling, parameter optimization or trading implementation.
Existing uncommitted consolidation work was retained and completed.
Short source paths below are relative to `src/trading_system/`.

## 1. Champion definition

**F / configured / C1**. Authority:
`src/trading_system/backtest/research_registry.py::FROZEN_CHAMPION_F`.
`StrategyVariant.QUALITY_VALUE_MOMENTUM` is the existing enum member whose value is
`F`; there is no new enum/strategy ID. Historical `F/configured` report labels stay intact.

The checked-in YAML retains 1 position, 100% notional ceiling, 1% equity risk,
ATR(14) stop distance `min(2 * ATR, entry_fill * 0.10)`, 12% target above entry fill,
hard hold 10 sessions, 5 bps slippage per fill and 0 bps commission. Re-entry remains
enabled with zero cooldown. F scoring, gates, thresholds, rank ordering and sizing
calculations were not changed. No L5 extension, I1 veto, R1 overlay, regime switching,
profit deferral or intraday entry delay is selected by the champion workflow.

`validate_champion_config` rejects execution-setting drift without mutating config.
It is an execution contract, not a cryptographic freeze of every data/scoring input;
reproducibility still requires the original YAML and local data provenance.

## 2. Production-path audit

| Path | Resolution and boundary |
|---|---|
| `screen --champion` | `champion.py::screen_champion` uses completed local Daily data without snapshots; `champion_ranking` calls the engine's F entry evaluator. |
| `export-ai --champion` | Same screen, gates and descending F-score/symbol order; existing AI schema identifies `F/configured/C1` in `strategy.name`. |
| `backtest --champion` | `champion.py::run_champion_backtest` validates config and supplies canonical F/configured to a fresh engine without research hooks. Conflicting variant/management flags are refused. |
| Normal champion reporting | Screen JSON has additive optional `strategy_label`; older reports still load. Champion screens use `reports/champion/`, avoiding legacy screen filenames. AI export preserves quant ranks. Lists are candidates before portfolio allocation, not executable orders. |
| Generic `screen`, `export-ai`, `explain` | Historical legacy screen retained. These commands without `--champion` are not the frozen champion workflow. |
| Generic `backtest` / direct `BacktestEngine.run()` | Existing C/configured default retained. `PortfolioConfig()` still has the generic capacity default 5; the shipped YAML has 1. Explicit champion validation rejects capacity drift. |
| Champion validation / exact LOSO | Existing canonical identity reused; execution config guard applied before preparation. Internal cost-stress cases remain explicit research. |
| Research/comparison CLI | Existing choices and runners retained, including explicit rejected presets. Broader comparisons can prefetch unless their existing local-only option is supplied; they are not production champion commands. |
| Future paper/shadow | No executable entry point exists. `champion.py` is a selection/reference boundary, not a trading service. |

Research metadata was moved to lightweight `backtest/research_definitions.py` with
compatibility re-exports from the original modules. CLI research calls and engine
overlay imports are deferred until explicitly used. Importing the CLI or executing
the champion does not load lifecycle, entry-quality, risk, regime, first-hour,
peer-context or forward-validation modules. Shared PIT features, accounting peers,
Daily diagnostics and reporting remain shared; no logic was cloned.

## 3. Research-family status

Current decisions use the immutable `RESEARCH_FAMILY_STATUS` map and `ResearchStatus`
(`ACTIVE`, `CHAMPION`, `REJECTED`, `ARCHIVED`). Historical serialized
`ResearchLifecycle` roles and strategy IDs retain their old values.

| Exact family/composition ID | Status | Decision |
|---|---|---|
| `F/configured/C1` | CHAMPION | Frozen Daily control |
| `research-f-capacity` | REJECTED | Capacity > 1 rejected; C1 control retained |
| `research-f-regime-capacity` | REJECTED | Regime capacity rejected |
| `research-f-lifecycle-v2` | REJECTED | Completed lifecycle family; all historical L0-L6 identities retained |
| `research-f-intraday-entry-quality` | REJECTED | I1 rejected; preserve this existing ID, not the illustrative `research-f-entry-quality-v1` |
| `research-f-intraday-risk-v1` | REJECTED | R1 rejected |
| `research-f-lifecycle-l5-forward-v1` | REJECTED | L5 rejected after forward holdout |
| `intraday-control`, `intraday-isolation`, `intraday-next`, `intraday-hybrid`, `f-entry` | ARCHIVED | Historical controls/hypotheses; no inferred new economic verdict |
| `d1-d5-archive`, `historical-position-management`, `configured-controls`, `screen-strategy-research` | ARCHIVED | Historical compositions remain callable |

No new ACTIVE family was introduced. Historical results use the **current local
tradable universe** and current company/SIC identities. **Survivorship bias remains
unresolved.** Research periods are not clean OOS unless explicitly established as
such. L5's registered forward period starts 2024-08-13 and is **FORWARD HOLDOUT /
RESEARCH**, not clean OOS. It is distinct from the separate champion-forward
validation cutoff. No holdout or research conclusion was recomputed in this audit.

## 4. Conservative code classification

Paths below are relative to `src/trading_system/` unless stated otherwise. Searches
covered imports, CLI and `__main__` registrations, registry IDs, tests, report keys,
configuration, migrations and historical runner/documentation references. Static
absence of an importer is insufficient evidence against manual/dynamic use.

| Candidate/group | Bucket | Evidence / retention reason |
|---|---|---|
| None | SAFE_REMOVE | No candidate passed every import/CLI/registry/test/schema/migration/history check. |
| `champion.py`, `strategy/`, `fundamentals/`, `technical/`, `config.py` | KEEP | Current local selection, common accounting/scoring and technical calculations. |
| `backtest/engine.py`, `position_manager.py`, `features.py`, `metrics.py`, `diagnostics.py`, `report.py`, coverage/qualification helpers | KEEP | Runtime dependencies and behavioral tests; Daily forward diagnostics run after execution. |
| `engine.research_strategy_label`, `RESEARCH_PRESETS`, `DailyBar` alias, old model/position fields | KEEP | Imported compatibility names and historical JSON/CSV/test contracts. |
| `data/`, SEC/accounting parsers, SQLite migrations, sync/remediation, calendars | KEEP | Explicit acquisition paths and data/report reproducibility. No provider call is needed by the engine loop. |
| `backtest/complete_validation.py` | RESEARCH_ARCHIVE | Has its own `main()` and reference/sensitivity CLI; absence from the main CLI is not dead code. |
| Capacity/regime/lifecycle/entry/risk/L5 runners, `peer_context.py`, native entry and intraday families | RESEARCH_ARCHIVE | Rejected/archived research remains opt-in; registry and reproduction tests require it. |
| `backtest/revalidation.py::generate_revalidation_artifacts` | DEFER | Historical report orchestration documented in `docs/post-audit-baseline-revalidation.md`; external/manual use cannot be disproved. |
| `_profit_factor` helpers in capacity/regime/validation/intraday reporting | DEFER | Similar arithmetic, but denominator/zero-loss/report contracts need independent equivalence evidence. |
| Mutable legacy registry containers and cached settings; large engine state/model surface | DEFER | Shared compatibility surface; no demonstrated safe deletion or narrow semantic refactor. |
| Tests, reference screeners/benchmarks, docs and historical artifacts | KEEP | Reproduction or reference evidence; rejection is not deletion evidence. |

**No safe removals identified.** No DEFER or RESEARCH_ARCHIVE item was deleted.

## 5. Bugs and findings

| ID | Severity | Evidence / disposition |
|---|---|---|
| CC-01 | HIGH | **OPEN / PRE_PAPER_BLOCKER.** `backtest/engine.py::run` skips management when a held symbol lacks a Daily bar; `_backtest_sessions` also omits sessions absent from the entire local bar table. Fixture proves a missing day-10 held bar causes day-11 `time_exit`. Final liquidation can use a stale last available bar. Existing strict held-bar guard is opt-in. **No correction applied:** changing exit/data rejection behavior can alter historical champion results. Requires a separate approved data-policy milestone before shadow execution. |
| CC-02 | MEDIUM | `data/database.py::list_tradable_companies`, `backtest/universe_provenance.py`, peer contexts use current tradable/SIC identities, not a versioned historical universe. Survivorship and research-selection bias remain unresolved; disclosed, not relabeled OOS. |
| CC-03 | MEDIUM | `models/fundamentals.py::FundamentalFact.filed` is a date; `data/database.py::facts_available_as_of` uses `filed <= as_of`. Intraday acceptance/publication timing cannot be proven for same-date filings. No confirmed future-close leak was found; timing granularity remains a research/paper limitation. |
| CC-04 | MEDIUM | `engine.run` calls `bars_on_session`; `_atr_as_of` reads symbol history during holding updates, even with inactive ATR trailing. `diagnostics.add_post_exit_diagnostics` reads per closed position; `Screener.run/_prepare` reads per company. Existing optimized feature preparation batches queries. No new SQL loop introduced; consolidation deferred. |
| CC-05 | LOW | **RESOLVED.** The score-comparison test conflated configured A-F with an outdated three-entry, C-only hybrid family. It now derives configured runs from `SCREEN_STRATEGY_DEFINITIONS` and verifies exactly A/configured through F/configured. A separate test checks hybrid dispatch against `research_family_runs`, retains F-intraday and distinguishes it from configured runs. No registry contents, family membership or historical IDs changed. |
| CC-06 | LOW | Legacy `ACTIVE_RESEARCH`/`CHAMPION_CONTROL` fields and `F_REGIME_CAPACITY_RESEARCH_STATUS = "historical research hypothesis"` remain report-compatibility metadata. Current decisions now live in a separate explicit map; old artifacts are not rewritten. |
| CC-07 | LOW | `config.load_settings` caches mutable Pydantic settings; some legacy research maps/sets are mutable globals. Inspected runners copy experiment configs/use instance state, and champion tests prove no mutation. No demonstrated leakage found; broader immutability work deferred. |
| CC-08 | INFO | Legacy C defaults and generic capacity 5 are intentional compatibility defaults. Explicit champion workflow and guard now make their distinction reviewable. No score/gate/default-economics change. |
| CC-09 | INFO | Eager CLI/registry dependencies previously loaded rejected research code. Deferred calls/imports and shared immutable metadata now isolate normal champion execution; original import aliases preserved. |

No BLOCKER-severity finding was identified. No HIGH trading-correctness fix was performed: CC-01
does not satisfy the requirement to preserve historical economics. There were no
new strategy definitions, confirmed forward-close dependencies in selection/sizing,
or network calls discovered inside the inspected execution loops.

### CC-01: OPEN

Severity: **HIGH**

Milestone: **PRE_PAPER_BLOCKER**

Historical backtest semantics are intentionally preserved. With
`require_complete_daily_position_bars=False`, a missing held-symbol Daily bar on
nominal holding day 10 leaves the position open; it exits at the next represented
executable session's close (day 11 in the fixture). `run_champion_backtest()` retains
that historical/research behavior. With the strict option `True`, the same fixture
raises `DAILY_POSITION_DATA_UNAVAILABLE` on day 10.

Before any shadow/paper order-execution service is implemented, missing Daily bars
for held positions must receive an explicit fail-closed or reconciliation policy.
The historical wrapper must not be reused blindly as a paper execution policy.
**No economics change was performed. Historical compatibility behavior remains frozen.**

## 6. Network/data boundary

`BacktestEngine.run()` remains local-data-only, including screening, portfolio
execution, position management and injected research managers/overlays. Inspected
validation runners consume local preparation/replay data and the same engine.
The only provider invocation in `backtest/` is the explicitly invoked
`engine.prefetch_comparison_intraday_data` helper, outside `run()`. Acquisition
remains in sync/prefetch/remediation paths. No network sync/provider request was run.

Tests block HTTP/socket access and synchronizer construction; a fresh subprocess
blocks research imports while running the actual local champion engine and screen.
Positive-trade fixtures also fail if lifecycle/risk/regime providers initialize.
Filing-prefix, future-bar, snapshot-isolation and post-exit-only diagnostics tests
cover the principal local lookahead boundaries. This is focused evidence, not a
full historical data-integrity certification.

## 7. Configured Daily execution precedence

`position_manager.py::evaluate_open/evaluate_intrabar/evaluate_close` and
`engine.py::run` preserve:

1. Existing positions: gap stops at the observed open, then opening target handling.
   A target already reached at the open wins over a low occurring later that day.
2. Prior-close candidates execute at the next represented XNYS session's open,
   using signal-session ATR and the existing fractional sizing/cost model.
3. Within an unresolved Daily OHLC range, pre-existing stop precedes target.
4. Target precedes close-based hard max-hold; configured max-hold executes at that
   session's close, not the next session. Entry counts as holding session one.
5. Final-session liquidation uses the existing close convention. Missing-data
   caveats are CC-01. Generic research close decisions that explicitly queue exits
   retain their own next-executable-bar/session behavior and cannot enter champion selection.

Legacy configured reports use `profit_target` and `time_exit`; manager enum names
remain `take_profit` and `max_hold`. No trading-precedence discrepancy with existing
documentation was found and no execution precedence was changed.

## 8. Paper/shadow readiness

READY means a reusable local capability exists; it does not certify an operational
paper trader. There is currently no durable trading loop or order submission.

| Capability | Status | Exact location / missing abstraction |
|---|---|---|
| Current screen generation | READY | `champion.py::screen_champion`, `strategy/screener.py::Screener.run`; local completed-session data. Freshness policy remains operational work. |
| Candidate ranking | READY | `champion.py::champion_ranking`, `engine.py::evaluate_variant_entry/_entry_orders`; same F gates and order. |
| Signal timestamp | PARTIAL | `ScreenReport.generated_at/effective_market_session`, `_PendingEntry.signal_date`; no immutable decision event with a knowledge cutoff. |
| Next-session entry intent | PARTIAL | `engine.py::_PendingEntry` and `run` queue in memory; no durable intent, and CC-01 affects session continuity. |
| Position sizing | PARTIAL | `engine.py::_open_position` is tested; account/broker precision, fractional eligibility and durable sizing intent are missing. |
| Stop calculation | READY | `engine.py::_open_position`; unchanged signal ATR/max-distance calculation. |
| Profit target | READY | `engine.py::_open_position`, `PositionManager`; unchanged fill-based 12% target. |
| Max-hold state | PARTIAL | `position_manager.py::PositionState.holding_days`, `evaluate_close`; in-memory only, with CC-01. |
| Open-position persistence | MISSING | `data/database.py::Database.initialize` has no trading-position ledger. Backtest report exports are final artifacts. |
| Execution-state persistence | MISSING | No intent/fill/event/checkpoint store in `data/database.py`; execution legs exist only as simulator/report models. |
| Order idempotency | MISSING | `engine.py::_next_position_id` is a simulation sequence, not a durable order idempotency key. |
| Restart recovery | MISSING | No trading-state replay/checkpoint/recovery abstraction. |
| Broker reconciliation | MISSING | `data/alpaca_client.py::AlpacaDataClient` exposes market/assets reads, no account order/position reconciliation workflow. |
| Market calendar | READY | `data/market_sessions.py`; XNYS sessions, completed sessions, DST and early closes. Sparse local data remains CC-01. |
| Corporate actions | PARTIAL | `data/alpaca_client.py` uses provider-adjusted bars; no holdings/cash corporate-action event ledger or historical adjustment-version store. |
| Logging/audit trail | PARTIAL | `cli.configure_logging`, engine position logs, atomic reports; no durable decision-to-intent-to-fill journal. |
| Paper/live safety boundary | READY | `config.py::Settings` restricts mode to paper; read-only adapter contains no order method. Re-audit before any future submission implementation. |

## 9. Files actually removed

None. Definitions were relocated with compatibility re-exports; historical runners,
models, tests, schemas, migrations and report artifacts remain available.

## 10. Verification and remaining technical debt

Follow-up focused result: **94 passed, 0 failed**, covering both affected test files.
**CC-05: RESOLVED.** No registry or runtime code was changed for this patch.

| Focused run | Result |
|---|---|
| `tests/test_screen_strategies.py` and `tests/test_champion_consolidation.py` | 94 passed, 0 failed in 5.18 seconds |

The missing-bar regressions separately verify historical compatibility with the
strict flag disabled, the strict error on day 10, and unchanged champion-wrapper
positions, trades, equity and configuration compared with the non-strict engine.
The nominal complete-data day-10 exit is verified before removing the fixture bar.

Ruff and formatting checks passed on the two modified Python test files;
`git diff --check` passed. The only warning was the existing transitive
`websockets.legacy` deprecation. No full suite, network/provider operation,
historical backtest, lifecycle/intraday validation or profiling was run.
Disposable fixture databases/reports were removed after testing.

Follow-up files: `tests/test_screen_strategies.py`,
`tests/test_champion_consolidation.py`, and this audit report.

Original consolidation changed-file inventory (including completed pre-existing edits):

- `.gitignore`, `README.md`, `reports/champion_consolidation_audit.md`.
- `src/trading_system/champion.py`, `cli.py`, `ai/export.py`, `models/screening.py`.
- `src/trading_system/backtest/research_registry.py`, `research_definitions.py`,
  `engine.py`, `entry_quality.py`, `intraday_risk.py`, `lifecycle.py`, `market_regime.py`,
  `l5_forward_validation.py`, `validation.py`.
- `tests/test_champion_consolidation.py`.

Outstanding: CC-01 data policy; historical universe/filing timestamp provenance;
durable shadow state and reconciliation; existing per-loop reads; mutable legacy
configuration/registry surfaces; historical report-helper duplication and legacy
status labels. CC-05 is resolved; CC-01 remains mandatory before paper/shadow
execution. No expensive historical validation was attempted.

## 11. Recommended next engineering milestone

**New independent strategy research.** Keep the frozen champion unchanged.
CC-01 remains OPEN / HIGH / PRE_PAPER_BLOCKER and must be addressed in a dedicated
data/execution-policy task before any paper/shadow order-execution service is
implemented. That future policy must not silently change historical champion economics.
