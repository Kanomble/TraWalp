# F preflight / manifest hardening engineering report

## 1. Root causes found

Confirmed: whole-database fingerprints streamed irrelevant facts and Daily history; the
snapshot guard preceded coverage; manifest validation lost the nested Daily qualification;
hard-rejected names still received full scoring; replay serialized every rejected symbol;
replay rebuilt Pydantic objects; lifecycle diagnostics evaluated and ranked F again; I1 reread
native entry sessions already checked by coverage; technical preparation rebuilt overlapping
arrays. The synthetic benchmark also confirmed an inefficient SQLite join plan despite a
bounded query count: SQLite scanned the timeframe index before scanning requirements.

The frozen Champion, F entry rules/ranks, peer membership/minimums/SIC fallbacks, configured
Daily management, max_positions=1, costs, execution priority, L0–L6 and I0/I1 are unchanged.
The current-universe survivorship limitation remains explicit in the existing research outputs.

## 2. Files changed

| File | Purpose |
|---|---|
| `src/trading_system/backtest/discovery_snapshot.py` | Scoped streaming input fingerprint and SQL/row counters |
| `src/trading_system/backtest/f_candidates.py` | Version 2 artifact, compact serialization, compatibility/content verification, replay timings |
| `src/trading_system/backtest/f_replay.py` | Immutable/slotted F replay views and authoritative candidate metadata |
| `src/trading_system/backtest/features.py` | Safe hard-rejection shortcut, relevant-symbol retention, per-symbol technical arrays |
| `src/trading_system/backtest/engine.py` | Shared canonical blocking reasons, omitted-rejection counts, prepared native sessions |
| `src/trading_system/backtest/lifecycle_validation.py` | Qualification reuse, final snapshot guard, run-owned resources and performance counters |
| `src/trading_system/backtest/lifecycle_diagnostics.py` | Consume canonical F candidate/rank metadata; retain generic fallback |
| `src/trading_system/backtest/native_entries.py` | Temporary native-session spool and eight-session RAM cache |
| `src/trading_system/data/database.py` | Requirements-first indexed coverage joins and narrower projections |
| `src/trading_system/strategy/performance.py` | Count hard rejections avoided before scoring |
| `tests/test_f_candidate_discovery.py` | Exact stream, rejection aggregation and future-share regressions |
| `tests/test_f_candidate_manifest.py` | Compatibility, qualification and composite range-plan checks |
| `tests/test_f_preflight_hardening.py` | Scoped PIT inputs, races, retained scores, I0/I1 equivalence, replay/cache regressions |
| `tests/test_intraday_progress.py` | Require snapshot verification after coverage |
| `tests/reference_f_preflight.py` | Frozen pre-hardening discovery, fingerprint, parser and coverage reference |
| `tests/benchmark_f_preflight_hardening.py` | Bounded deterministic benchmark using an owned temporary database |
| `docs/f-preflight-hardening-benchmark.json` | Raw measured benchmark results |
| `docs/f-preflight-hardening.md` | This report and manual commands |
| `docs/f-candidate-discovery-performance.md` | Link the previous report to this revision |
| `README.md` | Link the current artifact/version documentation |

The Daily preflight benefits through its shared candidate iterator: without a replay request,
candidate discovery now builds no replay payload. Its qualification rules are unchanged.

## 3. Fingerprint architecture

`data_fingerprint(database, config, start=..., end=...)` hashes a conservative discovery scope:

- All current asset symbols/tradability, all company CIK/symbol/name/SIC/description state,
  and SEC identity-conflict/reference state. The canonical identity resolver runs in the same
  SQLite read transaction. Retaining all identity metadata protects alias/ownership resolution.
- Histories only for tradable, non-quarantined companies that are not statically excluded by
  the existing financial/REIT configuration. Price, liquidity and market-cap gates do **not**
  narrow the fingerprint universe. Missing SIC is retained.
- Every fact for those companies with `filed <= end`, including share counts and old periods.
  There is no lower filing bound: old shares and accounting fallback periods remain protected.
- Their Daily OHLCV from the canonical technical warmup through `end`, plus SPY Daily inputs.
  All-symbol Daily session keys within the requested window protect portfolio/calendar availability.

Explicit columns use deterministic SQLite byte encoding and length-framed SHA-256 input,
with batches of 200 symbols and fetches of 4,096 rows. No whole-row JSON or fact models are
constructed. Operational `updated_at` columns and unrelated intraday data/cursors are absent.
Configuration, code and runtime hashes remain separate; the code hash covers the new modules.

**A 15m-only sync does not invalidate a Daily candidate manifest.** Future Daily bars and facts
filed after the window are excluded; corrections within scope invalidate it. Newly tradable or
unquarantined companies change metadata and force their histories into the recomputed scope.

Preflight checks the scoped fingerprint before preparation and after coverage. There is no
intermediate post-discovery hash. Manifest validation checks compatibility/content first and
verifies the Daily fingerprint again after coverage. A mismatch raises
`Discovery inputs changed during preflight; rerun on a stable snapshot` without restarting.

## 4. Candidate discovery optimizations

Peer construction happens unchanged before the scoring shortcut. A candidate with canonical
hard base exclusions can skip scoring only if it has never been F eligible during this discovery.
The shared `entry_blocking_reasons()` preserves the exact first failure and exclusion order.
Score-only exclusions continue through canonical scoring/evaluation. Previously relevant symbols
always retain their scores and technical evidence, even when subsequently hard-rejected.

The canonical evaluator still decides eligibility and ranking. Discovery avoids its redundant
second evaluation when writing candidate requirements. Production full-screen output is unchanged.

Technical inputs are converted to NumPy arrays once per symbol, and snapshots use bounded PIT
views. All reductions, warmups and Wilder RSI/ATR re-seeding remain identical. A single full-history
Wilder series would change values after the configured history window moves, so that rewrite
was deliberately not made. Cumulative rolling sums were also avoided to preserve rounding order.

## 5. Replay architecture

On the 30-session, 600-company fixture:

| Measure | Previous version | Version 2 |
|---|---:|---:|
| Replay records | 18,030 | 810 |
| Replay bytes | 6,494,229 | 317,031 |

The artifact retains eligible entries and every later session's available state for a symbol
from its first F eligibility onward. Before that point it cannot be held or have a re-entry
tracker. Such rejections contribute only exact first-reason counts; the engine adds those
counts at the same capacity-available point as the original funnel. No candidate is omitted.

Retained rows contain symbol/SIC, four numeric score components, technical evidence and ordered
exclusions. Session date is stored once; eligible rows also contain the canonical evaluation score.
Factor graphs, fundamentals, unused company/report details and irrelevant rejected rows are absent.
This preserves held-position scores, re-entry triggers, ranking, capacity/selection reasons and
lifecycle observations. Diagnostics reuse the verified candidate order without evaluating it again.

`DISCOVERY_VERSION=2` rejects old artifacts and requests a new preflight. Compatibility still
checks config/code/runtime/data fingerprints, candidate requirements/counts/ranks/scores/session
mapping and replay checksum. Retained rejections and technical PIT dates are validated too.
Sibling-file checks reject traversal, alternate-stream paths and links outside the report directory.
Per-session checksums also detect file changes after initial validation.

Trusted replay uses immutable/slotted views; it creates no Pydantic objects. Full report caching
stays at zero during discovery, and replay holds at most one session. Minimal requirements,
offsets and relevant-symbol metadata scale with artifact size; historical score graphs do not.

## 6. SQLite behavior

Coverage still uses **two SELECTs per batch of at most 200 exact requirements**. `CROSS JOIN`
keeps requirements as the outer loop, allowing the composite symbol/timeframe/timestamp range
lookup. The Daily query now selects only symbol, close and required session. Exact native bars,
missing-session behavior, ordering and previous-completed-Daily-close semantics are unchanged.

Measured counts:

- Synthetic candidate preparation: 0 SQL queries in both versions, because this fixture supplies
  prepared cross-sections. This is not a measurement of real historical feature loading.
- Coverage: 8 queries for 720 candidate requirements in both versions; the corrected access plan
  reduces work within those queries. The separate 402-requirement regression asserts 6 queries.
- Scoped fingerprint / manifest validation: 9 SELECTs on this fixture (6 streaming queries and
  3 canonical universe/identity reads). Real counts depend on symbol batches and identity state.
- Validation performs one additional scoped post-coverage verification. I1 makes **zero extra
  native-entry/previous-close SQL queries** after coverage; the previous path read both again
  for each attempted native entry.

Coverage populates an owned temporary native-entry spool, keyed by symbol, signal, execution
session and timeframe. RAM holds at most eight session payloads plus the current coverage batch.
Internal pickle data is read only from the instance's own temporary file, never from manifests.
These bars are not exported, so manual intraday sync between preflight and validation remains valid.

## 7. Correctness verification

259 distinct focused tests pass. The broad focused run passed 258; its remaining progress test
expected the old snapshot order. After updating that assertion, all 20 progress tests passed.
Ruff and formatting checks cover the changed Python files.

Exact deterministic assertions cover:

- Reference F candidate set == optimized F candidate set.
- Reference F scores, ranks, counts and signal/next-session mapping == optimized values.
- Retained rejection evidence == reference evidence; omitted first-rejection counters == reference.
- Representative complete ScreenRecords/peer/scoring values == the frozen canonical reference.
- Reference I0 results == optimized I0 results; reference I1 results == optimized I1 results.
  The temporal fixture compares complete results, trades, positions, equity, score histories,
  re-entry metadata, skipped entries, entry-quality events, diagnostics tables and summary output.
  It includes ties, capacity limits, hard/score rejection while held, re-entry and I1 vetoes.
- Future Daily corrections, filings and share facts cannot change earlier decisions. Relevant
  corrections invalidate manifests; intraday-only changes remain compatible. Final-coverage
  races fail conservatively. Existing strict Daily guards and native I1/PIT tests pass.
- Bad compatibility hashes, version, candidates, sessions, ranks/scores, replay checksums and
  filenames fail. Replay avoids Pydantic rebuilding; cache sizes stay bounded.

Focused command used:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_f_candidate_discovery.py tests/test_f_candidate_manifest.py `
  tests/test_f_preflight_hardening.py tests/test_f_lifecycle_daily_preflight.py `
  tests/test_f_lifecycle_v2.py tests/test_backtest_features.py `
  tests/test_screener.py tests/test_scoring.py tests/test_peers.py `
  tests/test_intraday_progress.py tests/test_backtest_engine.py -q --tb=short
```

## 8. Performance result

Python 3.14; deterministic synthetic 30-session/600-company fixture, 720 candidates, 30 unique
F symbols, 256,627 Daily rows and 94,800 facts. It intentionally contains 80% hard-rejected
names and 5% potential F names. One additional synthetic identity-conflict record is included
per session. Full screen cache size is zero in both compared versions.

| Phase | Previous version | Optimized | Ratio |
|---|---:|---:|---:|
| Snapshot fingerprint | 2.749 s | 0.250 s | 11.0× |
| Candidate discovery, including serialization | 4.653 s | 3.805 s | 1.22× |
| JSON encoding/spool write | 0.0728 s | 0.00736 s | 9.90× |
| Coverage loading | 13.604 s | 0.358 s | 38.0× |
| Coverage decision evaluation | not isolated | 0.298 s | — |
| Replay parsing, one pass | 0.229 s | 0.0116 s | 19.8× |
| Manifest compatibility/content validation | not measured | 0.382 s | — |
| Technical prefixes, 30 symbols × 30 sessions | 0.767 s | 0.478 s | 1.61× |

Scoped fingerprinting streamed 53,061 rows for 120 participating issuers plus SPY/session keys.
Discovery avoided scoring 14,490 hard rejections. Replay records fell 95.5%, and bytes fell 95.1%.
The optimized preflight took 5.013 s with an already prepared synthetic screen source.

Discovery speedup is modest; it is **not** a multi-fold discovery improvement over version 1.
The large measured gains are fingerprint scope, replay size/parsing and coverage access planning.
No bounded real-dataset benchmark was performed, and no whole-period speedup is claimed.
Reference coverage load timing excludes decision evaluation. Discovery includes serialization,
and manifest validation includes its fingerprint: inclusive phases must not be added together.
Raw measurements are in [f-preflight-hardening-benchmark.json](f-preflight-hardening-benchmark.json).

Heartbeat/session counters remain intact. Average and recent ten-session throughput are measured;
ETA appears only after at least three completed sessions with positive measured time. Performance
reports distinguish fingerprint/verification, discovery stages, serialization, coverage, replay,
diagnostics and native-cache counters, with explicit timing scope.

## 9. Remaining bottlenecks

Peer-statistic preparation accounts for about 2.77 of the 3.81 discovery seconds on this fixture.
It remains exact and unchanged. Real-run accounting/PIT feature preparation was not represented
by the injected cross-sections and may still dominate. Wilder calculations still run per bounded
prefix; previously eligible symbols continue to need scoring on later sessions. Necessary old
facts for participating issuers are still fingerprinted conservatively. No speculative peer or
accounting changes were made after measuring these limits.

No production/history database was opened for benchmarking or modified. No full historical
preflight/validation, full test suite, provider/network access, synchronization, or paper/live
trading was executed. All writes were source/report files or owned temporary test fixtures.

### Manual rerun

Use fresh stems. Version 1 manifests require regeneration. With the default `reports` directory:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-f-intraday-entry `
  --start 2022-03-09 `
  --end 2024-08-12 `
  --output-stem f_intraday_entry_preflight_2022-03-09_2024-08-12_v3

# After any necessary manual native 15m synchronization:
.\.venv\Scripts\python.exe -m trading_system.cli validate-f-intraday-entry `
  --start 2022-03-09 `
  --end 2024-08-12 `
  --candidate-manifest .\reports\f_intraday_entry_preflight_2022-03-09_2024-08-12_v3_intraday_candidates.json `
  --output-stem f_intraday_entry_validation_2022-03-09_2024-08-12_v3

# Optional bounded offline benchmark:
.\.venv\Scripts\python.exe tests/benchmark_f_preflight_hardening.py --sessions 30 --companies 600
```
