# Pairs V1 bounded Daily qualification fixpoint

The normal `preflight-pairs-stat-arb-v1` command now orchestrates the existing
targeted Daily auditor when a newly discovered selection/calibration requirement
is unresolved. It constructs the existing Alpaca `DataSynchronizer` lazily, only
if an audit is needed. `--local-only` preserves provider-free inspection and
reports unreviewed requirements as blocking. Validation stays local. There is no
market-wide sync, manual symbol research, new identity engine, or backtest.

## Algorithm

1. Recompute current security-type, identity and persisted Daily qualification.
   Keep identity and Daily quarantines independent and run-global.
2. Fully discover SIC2/ADV20/Top-N membership and unordered pair input needs.
   Calibration is `QUALIFICATION_PENDING`: no correlation, z-score, signals,
   simulated trades, PnL or outcome evaluation is used during iteration.
3. Form exact required-session/role keys bound to provider, feed, adjustment and
   Daily qualification policy. Local usable requirements are already resolved.
   Audit only new unresolved keys through the existing auditor. It reuses valid
   persisted per-session provider evidence, even if a new role needs the same bar.
4. Write the explicit repair plan before sync. Only `TRUE_LOCAL_MISSING` may
   sync, with minimal symbol/date blocks through `DataSynchronizer`. Recheck both
   local usability and feed/adjustment-bound coverage metadata. Successfully
   unavailable history is `DAILY_DATA_QUARANTINED`; technical/system errors are
   `DAILY_QUALIFICATION_FAILED`. No listing or universal no-data claim follows.
5. After qualification changes or repairs, fully rediscover from the changed
   universe. Never retain old ranked selections or pairs across such a change.
6. A complete discovery with no unresolved input keys and no failed qualification
   reaches `SUCCESSFUL_FIXPOINT`. Do not start another qualification round.

After success, source/qualification fingerprints revalidate the final input
discovery. Numerical calibration is calculated for that unchanged membership;
it does not rerank or trigger outcome evaluation. Outcome-only coverage remains
an independent readiness gate and cannot cause universe quarantine.

## Bounds and failure semantics

`MAX_DAILY_QUALIFICATION_ROUNDS = 8` is a fixed operational cap. It permits several
Top-5 replacement rounds while bounding provider and discovery work; it is not
tuned on returns, missing-bar count or candidate profitability.

| Condition | Result |
|---|---|
| Entire discovery resolved, no qualification failures | `SUCCESSFUL_FIXPOINT` |
| Consecutive identical relevant state, or no new keys with unresolved inputs | `DAILY_QUALIFICATION_NO_PROGRESS` |
| Earlier nonconsecutive state repeats | `DAILY_QUALIFICATION_CYCLE_DETECTED` |
| Round eight still unresolved | `DAILY_QUALIFICATION_FIXPOINT_NOT_REACHED` |
| Daily quarantines exceed 10% of the identity-qualified universe | `REVIEW_REQUIRED` |
| Provider/auth/rate-limit/parser/ingestion/database/evidence failure | `DAILY_QUALIFICATION_FAILED` |

Failures, cycles, limits and review stop further audits and keep `ready=false`.
The cycle guard also detects A/B/A recurrence of qualified/provisional membership
and quarantine sets when source/evidence fingerprints differ. Consecutive unchanged
membership is allowed while repairs change the required inputs; a repeated full
unresolved state still triggers the separate no-progress guard.
The 10% breadth guard is checked on the cumulative post-audit qualification and
on each discovered state. A blocked post-audit report can refresh local
qualification without another audit round. A transient error is never an
unavailability decision. Provider outcomes distinguish `SUCCESS_WITH_BARS`,
`SUCCESS_NO_REQUIRED_BARS`, `TRANSIENT_ERROR`, `AUTH_ERROR`, `RATE_LIMIT_ERROR`,
`PARSER_ERROR` and `INVALID_RESPONSE`; the existing per-session audit statuses are
retained for compatibility. Only successful no-required-bars evidence can support
quarantine after the completed audit/repair decision/recheck.

## Evidence and reporting

Existing Daily certificates remain in SQLite `sync_state` under
`pairs_daily_qualification`, with the same run/feed scope. The fixpoint does not
invalidate the 66 existing exclusions merely by introducing orchestration.
Certificate sessions/roles are merged, not replaced when later requirements
arrive. Locally present repaired bars do not receive repeated provider writes.

Manifest **V9** binds the orchestration policy, eight-round limit, source/feed,
all round-state fingerprints and final Daily qualification/quarantines. Older
candidate manifests cannot be reused. The standalone auditor accepts V7/V8/V9
inputs for migration. Trace validation checks internal state hashes, final
current source/qualification inputs, successful termination and the configured
policy/limit. Changing feed, Daily evidence, policy or round-state invalidates
the relevant artifact. No diagnostic report is an implicit runtime database.

Each state fingerprints eligible and provisional symbols, identity/Daily
quarantines, unresolved requirement-role keys, normalized required Daily inputs,
feed identity and relevant evidence/source fingerprints. Calibration/outcome
values are not state inputs. Summary, Requirements and Manifest share the final
trace, including round count, reason, unresolved inputs and qualification failures.
Each round reports eligible/quarantined symbols, new session and role counts,
audited sessions, repairs, new quarantines, failures and pair-session candidates.
`<stem>_round_N_repair_plan.json` precedes mutation; `<stem>_round_N_audit.json`
records sanitized provider results and post-audit qualification.

`ready=true` additionally requires the existing company/identity/membership gates,
zero identity blockers, a successful Daily fixpoint, zero qualification failures,
zero unreviewed requirements and usable remaining local inputs. The fixpoint is
necessary and does not override any other readiness condition.

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-pairs-stat-arb-v1 `
  --start 2024-01-02 --end 2026-08-12 `
  --output-stem pairs_daily_fixpoint_preflight_2024-01-02_2026-08-12
```

## Methodological limits

The source is configuration-specific, currently **ALPACA_IEX**. Exclusion says
TraWalp cannot establish the required Daily coverage under that source, not that
the security did not exist or lacked data everywhere. Identity policy and frozen
SIC2/ADV20/Top-5/calibration/entry/exit/economic rules are unchanged.
`DATA_AVAILABILITY_SELECTION_BIAS`, `CURRENT_UNIVERSE_ONLY`, `NOT_FULLY_PIT` and
`NOT_SURVIVORSHIP_CLEAN` remain. Fixpoint completion improves reproducibility and
qualification completeness; it does not create a PIT historical security master.
Stop after the final preflight and review it before any separately authorized
backtest.
