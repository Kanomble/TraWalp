# Pairs V1 Daily qualification run audit

2026-10-16; remote/local main verified at `ed500dcb98c5f8d6eb670eaff44987d7cadd2cb9`.
Requested period: 2024-01-02 through 2026-08-12; configured source: **ALPACA_IEX**, adjustment `all`.

## Policy and operational distinction

After identity qualification, actual frozen selection/calibration requirements undergo a bounded provider audit, explicit repair decision and local recheck. Only successfully established source unavailability causes independent run-global `DAILY_DATA_QUARANTINED` exclusion before SIC2/ADV20/Top-N/pairs. Identity, listings and trading activity are not inferred. Missing bars alone never cause exclusion. Technical, parser, ingestion or evidence errors remain blocking. Outcome-only requirements cannot determine qualification.

The SQLite `pairs_daily_qualification` evidence namespace binds run, source/feed/adjustment, economics/settings, exact required sessions, identity and original manifest. Normal preflight is offline and reads no audit report. Manifest V8 binds policy, guard and qualification fingerprint; older candidate manifests cannot be reused. Restored required bars clear exclusions.

The fixed diagnostic breadth guard is 10% of identity-qualified symbols, independently of missing-bar count and performance. Above it: `REVIEW_REQUIRED`, `ready=false`. Current result: `ACCEPTED`.

## Before and after

| Metric | Before | After |
|---|---:|---:|
| ready | False | False |
| identity_qualified_symbols | 4557 | 4557 |
| provisional_run_universe | 4557 | 4491 |
| daily_quarantined | 0 | 66 |
| daily_unresolved_symbols | 66 | 1 |
| daily_missing | 1697 | 41 |
| selection_gaps | 1479 | 0 |
| sic2_unresolved_sessions | 2097 | 0 |
| signal_sessions | 655 | 655 |
| selected_symbol_sessions | 108012 | 117409 |
| pair_session_candidates | 173008 | 190849 |
| universe_sic2_groups | 69 | 69 |
| selected_sic2_groups | 58 | 59 |
| pair_sic2_groups | 50 | 51 |
| daily_eligible_symbols | not previously classified | 4490 |

The retained provisional universe includes one unresolved symbol; 4,490 symbols are Daily eligible. A false preflight does not authorize executing its provisional candidates.

Exclusions: **1.4483%** of identity-qualified symbols. Before application, the measured frozen-manifest minimum impact was 259 selected symbol-sessions (0.2398%) and 1016 pair-sessions (0.5873%); no replacement ranking was used for that measurement. 24 universe SIC2 groups are affected; 0 completely disappear. Fresh discovery then adds 17,841 net pair-sessions by resolving previously blocked SIC2 sessions. This measures input/candidate coverage, not profitability.

Signal sessions and selected symbol-sessions are separate denominators. Both before and after have 655 signal sessions. The newly discovered candidate set is not an old manifest filtered after pair formation.

## Real samples

All samples pass the same symbol-independent policy. Reason for every quarantine: `CONFIGURED_PROVIDER_REQUIRED_HISTORY_UNAVAILABLE`.

| Symbol | Status | Audited required / still missing |
|---|---|---:|
| BRK.A | DAILY_DATA_QUARANTINED | 674 / 674 |
| FIEE | DAILY_DATA_QUARANTINED | 373 / 373 |
| NBIS | DAILY_DATA_QUARANTINED | 221 / 221 |
| LION | DAILY_DATA_QUARANTINED | 111 / 111 |
| CRCL | DAILY_DATA_QUARANTINED | 41 / 41 |
| TLN | DAILY_DATA_QUARANTINED | 41 / 41 |

## Actual audit and repair activity

The bounded audit completed 1697 exact required-session observations. Provider statuses: `{'EMPTY_RESPONSE_UNRESOLVED': 1697}`. No actual sync was warranted or performed: **0 bars received for ingestion, 0 inserted, 0 updated, 0 unchanged, 0 sync errors**. `repair_attempted=false` truthfully records no sync; `repair_decision=NO_SYNC_NO_USABLE_PROVIDER_BAR` and completed audit/plan/recheck establish source unavailability. No market-wide or intraday requests were made.

The prior 3,083-bar repairs were not repeated. BKYI, CAMP, GURE, PAPL, PTN, VIVK and WBUY remain `DAILY_HISTORY_USABLE` in the retained universe; none is quarantined by this policy.

## Remaining blocker and next step

`ready=false`: 41 newly discovered calibration requirements (GEV, 2024-02-01 through 2024-04-01) have no completed targeted provider audit. They remain `UNKNOWN_BLOCKING`; no listing boundary or provider unavailability is guessed. Selection gaps and SIC2 unresolved sessions are zero. No second provider/repair round or symbol history investigation was performed.

Recommendation: **STOP_MANUAL_SYMBOL_RESEARCH**. The measured removal is small. The next step is to examine the systemic handoff of newly discovered calibration requirements to the existing generic targeted audit, with the same repair/failure/quarantine rules. Do not optimize more symbols automatically. No backtest was started.

Identity unresolved remains 0; the same 53 identity-quarantined symbols remain excluded. Artifact checks confirm neither identity nor Daily quarantines appears in universe, selected members, pair candidates, Daily requirements or selection gaps. Economics match the previous manifest exactly.

## Methodological limits

`DATA_AVAILABILITY_SELECTION_BIAS`, `CURRENT_UNIVERSE_ONLY`, `NOT_FULLY_PIT`, `NOT_SURVIVORSHIP_CLEAN`. Source-specific unavailability under ALPACA_IEX is not universal unavailability, a listing assertion, or a historical security master. Current SIC/security type remain non-PIT.

## Executed verification

18 new synthetic policy tests; targeted Daily-audit, Coverage, Epoch, Run-Identity, Manifest, Alpaca-adapter, Pairs-engine, Security and Universe-scope regressions. The last impacted policy/manifest/identity/coverage run passed 155 cases; the final CLI preflight isolation test also passed. Full pytest, compare-strategies and backtests were not run. Ruff check, format check and git diff --check passed. All 1,697 actual requirement-role proofs were independently checked against frozen membership without inspecting calibration results.

## Artifacts

- [Final Summary](../reports/pairs_daily_qualification_preflight_2024-01-02_2026-08-12_v2_summary.json)
- [Final Requirements](../reports/pairs_daily_qualification_preflight_2024-01-02_2026-08-12_v2_daily_requirements.json)
- [Final Manifest V8](../reports/pairs_daily_qualification_preflight_2024-01-02_2026-08-12_v2_pair_candidates.json)
- [Final Coverage](../reports/pairs_daily_qualification_preflight_2024-01-02_2026-08-12_v2_coverage.csv)
- [Quantitative impact and checks](../reports/pairs_daily_qualification_final_impact.json)
- [All 66 quarantined symbols and reasons](../reports/pairs_daily_qualification_quarantined.csv)
- [Provider audit](../reports/pairs_daily_qualification_targeted_audit_audit.json)
- [Pre-sync repair plan](../reports/pairs_daily_qualification_targeted_audit_repair_plan.json)
- [Minimum impact measured before new discovery](../reports/pairs_daily_qualification_measured_impact.json)
- [Initial estimate using previous successful probes](../reports/pairs_daily_qualification_minimum_impact.json)
- [Local artifact verification/report generator](../reports/pairs_daily_qualification_build_report.py)
- Initial pass, preserved: `reports/pairs_daily_qualification_preflight_2024-01-02_2026-08-12_{summary.json,pair_candidates.json,daily_requirements.json,coverage.csv}`.

Final manifest: `2857789d39fbb071a7092283e1c295a9d35bbaff7171ed3f5de58861b0a41ff6`. Original source manifest: `7ddac313ae93f59569dcee2544e56cfa459578cc8db4e2eb250e9019b4ada09d`. Certificate: `712cd8e7c55d786a80ab056fcbdac0a6c6974b5a1766ca03720c2f4a10b191dc`.

The initial requested output stem is preserved. `_v2` is authoritative and includes final qualification consistency across Manifest/Summary/Requirements. No old candidates were forced.

## Files changed

- [README.md](../README.md)
- [docs/pairs-stat-arb-v1-daily-blocker-audit.md](../docs/pairs-stat-arb-v1-daily-blocker-audit.md)
- [docs/pairs-stat-arb-v1-daily-qualification-run-audit.md](../docs/pairs-stat-arb-v1-daily-qualification-run-audit.md)
- [docs/pairs-stat-arb-v1-daily-qualification.md](../docs/pairs-stat-arb-v1-daily-qualification.md)
- [docs/pairs-stat-arb-v1-research.md](../docs/pairs-stat-arb-v1-research.md)
- [src/trading_system/backtest/pairs_stat_arb_v1.py](../src/trading_system/backtest/pairs_stat_arb_v1.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_daily_audit.py](../src/trading_system/backtest/pairs_stat_arb_v1_daily_audit.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_daily_qualification.py](../src/trading_system/backtest/pairs_stat_arb_v1_daily_qualification.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_daily_qualification_audit.py](../src/trading_system/backtest/pairs_stat_arb_v1_daily_qualification_audit.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_data.py](../src/trading_system/backtest/pairs_stat_arb_v1_data.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_manifest.py](../src/trading_system/backtest/pairs_stat_arb_v1_manifest.py)
- [src/trading_system/backtest/pairs_stat_arb_v1_research.py](../src/trading_system/backtest/pairs_stat_arb_v1_research.py)
- [src/trading_system/cli.py](../src/trading_system/cli.py)
- [src/trading_system/data/alpaca_client.py](../src/trading_system/data/alpaca_client.py)
- [tests/test_pairs_daily_qualification.py](../tests/test_pairs_daily_qualification.py)
- [tests/test_pairs_stat_arb_v1.py](../tests/test_pairs_stat_arb_v1.py)
- [tests/test_pairs_stat_arb_v1_identity_quarantine.py](../tests/test_pairs_stat_arb_v1_identity_quarantine.py)
- [tests/test_pairs_stat_arb_v1_manifest.py](../tests/test_pairs_stat_arb_v1_manifest.py)
- [tests/test_pairs_stat_arb_v1_security.py](../tests/test_pairs_stat_arb_v1_security.py)
- [tests/test_pairs_stat_arb_v1_universe_scope.py](../tests/test_pairs_stat_arb_v1_universe_scope.py)

Local SQLite change: one run/feed-scoped qualification certificate in `sync_state`; no migration, deletion or bar writes. Generated local report/audit/impact files are listed above. `.codex/config.toml` was already staged and remains untouched.
