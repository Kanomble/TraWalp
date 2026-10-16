# Pairs V1 Daily data qualification

Pairs uses `current eligible universe ∩ identity-qualified securities ∩
Daily-data-qualified securities`. Identity qualification is unchanged. Independent,
run-global `DAILY_DATA_QUARANTINED` exclusion happens before SIC2 grouping, ADV20,
Top-N, pair formation and calibration. No excluded symbol generates Daily
requirements, pairs or outcomes in that run. No strategy economics change.

A missing bar alone never establishes unusability. An explicit targeted audit
validates the previous frozen preflight and its genuine selection/calibration
requirements, probes only those missing sessions through the normal Alpaca adapter,
writes a repair plan, repairs only positively verified `TRUE_LOCAL_MISSING` through
`DataSynchronizer`, then rechecks local usability. Successful probes without usable
bars plus the completed repair decision/recheck establish
`CONFIGURED_PROVIDER_REQUIRED_HISTORY_UNAVAILABLE`. This is sufficient for
quarantine; no exact listing, predecessor, suspension or delisting reconstruction
is required. There is no threshold on the number of missing sessions.

`repair_attempted` records an actual sync, not a fictional attempt to download
known-empty sessions. A successful empty probe receives
`repair_decision=NO_SYNC_NO_USABLE_PROVIDER_BAR`; its completed plan and local
recheck are mandatory. A bare empty response outside this audit is insufficient.
Returned bars receive targeted repair and remain usable when ingestion succeeds.
Timeouts, authentication, rate limits, HTTP errors (including 404), parser or
database failures, corrupt evidence and unsuccessful ingestion remain
`DAILY_QUALIFICATION_FAILED` / `ready=false`, never quarantine.

The explicit provider-enabled command is:

```powershell
.\.venv\Scripts\python.exe -m trading_system.backtest.pairs_stat_arb_v1_daily_qualification_audit `
  --candidate-manifest reports/<previous-preflight>_pair_candidates.json `
  --daily-requirements reports/<previous-preflight>_daily_requirements.json `
  --output-stem <new-unused-audit-stem>
```

The normal preflight remains local and never reads diagnostic report files.
The command stores checksummed evidence in SQLite `sync_state`, source
`pairs_daily_qualification`, scoped to requested dates, provider/feed/adjustment,
strategy definition and eligibility settings. Evidence contains per-symbol identity
fingerprints, required sessions/roles, provider results, audit/repair/recheck flags
and source-manifest fingerprint; no raw responses, returns, correlation results,
signals or outcomes. Outcome-only requirements cannot cause exclusion. The next
preflight rechecks the certified sessions against local usable bars. Restoring all
these sessions clears the exclusion. New unaudited gaps remain blocking.

Manifest **V8** is required because universe membership changed; V7 and earlier
candidate manifests cannot be reused for validation. The explicit audit accepts
V7/V8 input artifacts to support migration. The new contract binds Daily policy,
guard, provider/feed and qualification fingerprint, including exclusions/reasons,
required-range and evidence fingerprints. Different feeds, identity evidence,
audit evidence or restored local inputs invalidate the relevant qualification.

Summary and Requirements expose `daily_data_qualification` and per-symbol
diagnostics: status, reason, required/missing counts, probe status, repair decision,
actual repair attempted and run exclusion. Successfully excluded symbols are
reported separately from blocking input requirements. The fixed diagnostic guard
is **10% of the identity-qualified universe**: above it, `REVIEW_REQUIRED` and
`ready=false`. It is an operational breadth guard, chosen before outcome research;
it is not a missing-bar or performance threshold.

`daily_data_gate_complete` records the preselection certificate check;
`daily_data_qualification_complete` also accounts for newly discovered, unaudited
selection/calibration gaps. Manifest, Summary and Requirements expose the same
final qualification diagnostics. Outcome-only requirements stay separate from
qualification; repairing outcome-tail coverage cannot change frozen membership.

Qualification is specific to **ALPACA_IEX** under the current configuration. It
does not assert that a security did not exist, did not trade, was misidentified,
or is universally unavailable. It introduces `DATA_AVAILABILITY_SELECTION_BIAS`.
`CURRENT_UNIVERSE_ONLY`, `NOT_FULLY_PIT`, `NOT_SURVIVORSHIP_CLEAN` and current
security-type/SIC limitations remain. No claim of a market-complete historical
security master follows. After a fresh preflight, review its impact before any
separately authorized backtest.
