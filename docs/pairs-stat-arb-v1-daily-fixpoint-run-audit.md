# Pairs V1 Daily fixpoint run audit

2026-10-16; local/remote main verified at `bb4892b6a495aed8831c7bbe02a5b62ab4ec29bb`.
Period: 2024-01-02 through 2026-08-12. Source: **ALPACA_IEX**, adjustment `all`.

## Algorithm and stop conditions

Each round fully recomputes security/identity/Daily qualification and SIC2/ADV20/Top-5 input membership. Only newly unresolved symbol/session/role/feed keys enter the existing targeted auditor. Known certificates and usable local bars are reused. Positive provider availability permits minimal targeted repair followed by local/coverage recheck; successful unavailable history permits run-global Daily quarantine. No failed request or parser/system error can authorize quarantine. Numerical correlation calibration is deferred until the resolved fixpoint; no signals, trade simulation, outcomes or PnL were evaluated.

The operational cap is eight discoveries. Resolved discovery yields `SUCCESSFUL_FIXPOINT`; a consecutive repeated unresolved state yields `DAILY_QUALIFICATION_NO_PROGRESS`; a nonconsecutive repeat yields `DAILY_QUALIFICATION_CYCLE_DETECTED`; unresolved at the cap yields `DAILY_QUALIFICATION_FIXPOINT_NOT_REACHED`. Technical/unknown failure yields `DAILY_QUALIFICATION_FAILED`. More than 10% Daily quarantine yields `REVIEW_REQUIRED`, immediately stopping further audits. All unsuccessful results keep ready false. These limits are not performance/missing-bar thresholds.

## Actual rounds

| Round | Eligible | Daily Q | New | Audited | Repairs | New Q | Failures | Pair sessions |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4490 | 66 | 41 | 41 | 0 | 1 | 0 | 190849 |
| 2 | 4490 | 67 | 0 | 0 | 0 | 0 | 0 | 190849 |

Fixpoint: **True**, reason `SUCCESSFUL_FIXPOINT`. Ready: **True**.

## Generic treatment of the real sample

Audited symbols: `['GEV']`. Provider outcomes: `{'SUCCESS_NO_REQUIRED_BARS': 41}` over 41 exact required sessions. GEV received the same generic policy as every symbol; its 41 required calibration sessions yielded successful no-required-bars observations, so it was run-globally Daily-quarantined. No listing date, corporate-action chain or SEC history was researched. There were no repairs or new bar writes. All 66 prior Daily quarantine evidence fingerprints and the Identity qualification fingerprint remain unchanged. Previously repaired symbols remain usable and were not re-synced.

## Final state and impact

Daily eligible=4490, quarantined=67, unresolved/unreviewed=0, qualification failures=0. Identity quarantined=53, unresolved=0. Daily missing=0, selection gaps=0, SIC2 unresolved=0.

Final Daily quarantine fraction=1.4703%; guard result `ACCEPTED`, review threshold=10%. Selected symbol-sessions=117,409; pair-sessions=190,849; universe SIC2 groups=69, selected SIC2 groups=59, pair-producing SIC2 groups=51.

| Metric | Before fixpoint | After fixpoint |
|---|---:|---:|
| Ready | False | True |
| Daily quarantine | 66 | 67 |
| Daily missing | 41 | 0 |
| Selected symbol-sessions | 117409 | 117409 |
| Pair-session candidates | 190849 | 190849 |

Eligible counts exclude the formerly unresolved GEV before qualification as well as after its resolved quarantine; the unchanged eligible count therefore does not indicate skipped work. The actual current-universe exclusion is 67 of 4,557 identity-qualified symbols.

## Contract and verification

Manifest V9 `b3576dcfea70d2e6a854bca7edf500e574724a8dacd2609183c041a9a55295c4` binds fixpoint policy, round limit, feed, round states and final qualification. Trace fingerprint: `6188ef975060a8bbd57dcfe3e36336e5ed13c9676e444c17266905d459b9f1c3`. Old candidate manifests are rejected. Independent artifact checks passed: all trace/checksums and final input bindings match; Summary/Requirements/Manifest agree; Identity and economics match the previous preflight; identity/Daily quarantines appear in no selected membership, pairs or input requirements. No trade/signal artifacts were generated.

24 new fixpoint test cases passed, covering one-round convergence, quarantine/repair, cascading replacements, technical failures, no-progress, cycles including changed source evidence, round limit, guard, Identity isolation, no calibration/outcomes during iteration, and feed/policy/round/evidence invalidation. Targeted existing Daily-qualification, Manifest, Coverage, Identity, Security, Pairs-definition/engine and audit regressions also passed. Ruff, format check and git diff --check passed. Full pytest, compare-strategies, intraday/market-wide sync and a backtest were not run.

## Methodology and next step

`DATA_AVAILABILITY_SELECTION_BIAS`, `CURRENT_UNIVERSE_ONLY`, `NOT_FULLY_PIT`, `NOT_SURVIVORSHIP_CLEAN`. This is source-specific availability qualification and does not prove universal data absence or create a historical security master. SIC2, ADV20, Top 5, 60 calibration sessions/59 returns, Pearson/min correlation 0.7, z threshold, next-open entry, weights, slippage, exits, max hold and outcome tail are unchanged.

**STOP. Review the final preflight together. No backtest was started.**

## Artifacts

- [Summary](../reports/pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_summary.json)
- [Manifest V9](../reports/pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_pair_candidates.json)
- [Requirements](../reports/pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_daily_requirements.json)
- [Coverage](../reports/pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_coverage.csv)
- [Verification and final metrics](../reports/pairs_daily_fixpoint_verification.json)
- [Round table](../reports/pairs_daily_fixpoint_rounds.csv)
- [Read-only verification script](../reports/pairs_daily_fixpoint_verify.py)
- [Round 1 audit](../reports/pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_round_1_audit.json), [pre-sync repair plan](../reports/pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12_round_1_repair_plan.json)

## Files changed

- [README.md](../README.md)
- [docs/pairs-stat-arb-v1-daily-blocker-audit.md](../docs/pairs-stat-arb-v1-daily-blocker-audit.md)
- [docs/pairs-stat-arb-v1-daily-fixpoint-run-audit.md](../docs/pairs-stat-arb-v1-daily-fixpoint-run-audit.md)
- [docs/pairs-stat-arb-v1-daily-fixpoint.md](../docs/pairs-stat-arb-v1-daily-fixpoint.md)
- [docs/pairs-stat-arb-v1-daily-qualification.md](../docs/pairs-stat-arb-v1-daily-qualification.md)
- [docs/pairs-stat-arb-v1-research.md](../docs/pairs-stat-arb-v1-research.md)
- [src/trading_system/backtest/pairs_stat_arb_v1.py](../src/trading_system/backtest/pairs_stat_arb_v1.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_daily_audit.py](../src/trading_system/backtest/pairs_stat_arb_v1_daily_audit.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_daily_fixpoint.py](../src/trading_system/backtest/pairs_stat_arb_v1_daily_fixpoint.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_daily_qualification_audit.py](../src/trading_system/backtest/pairs_stat_arb_v1_daily_qualification_audit.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_data.py](../src/trading_system/backtest/pairs_stat_arb_v1_data.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_manifest.py](../src/trading_system/backtest/pairs_stat_arb_v1_manifest.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_research.py](../src/trading_system/backtest/pairs_stat_arb_v1_research.py)
- [src/trading_system/cli.py](../src/trading_system/cli.py)
- [tests/test_pairs_daily_fixpoint.py](../tests/test_pairs_daily_fixpoint.py)
- [tests/test_pairs_daily_qualification.py](../tests/test_pairs_daily_qualification.py)
- [tests/test_pairs_stat_arb_v1_identity_quarantine.py](../tests/test_pairs_stat_arb_v1_identity_quarantine.py)
- [tests/test_pairs_stat_arb_v1_manifest.py](../tests/test_pairs_stat_arb_v1_manifest.py)
- [tests/test_pairs_stat_arb_v1_security.py](../tests/test_pairs_stat_arb_v1_security.py)
- [tests/test_pairs_stat_arb_v1_universe_scope.py](../tests/test_pairs_stat_arb_v1_universe_scope.py)

SQLite change: the existing run/feed-scoped Daily certificate was extended; bars/identity/lifecycle records were not deleted. `.codex/config.toml` was already staged and remains untouched.
