# F intraday candidate discovery performance refactor

This changes computation, storage, and observability, preserving the canonical research rules.
The production screener still returns complete ranked `ScreenReport` objects. Comparison
backtests retain their existing shared cache.

## Confirmed bottlenecks and measurement

The old preflight prepared and scored the entire PIT cross-section for every signal session,
constructed every `ScreenRecord`, and retained every report in `CachedScreenSource`.
Surviving candidates each scanned the peer DataFrame twice, rebuilt metric vectors, and
repeated pandas winsorization/percentile work. Peer debug diagnostics ran even with DEBUG off.
Coverage separately performed two database reads per candidate.

Lightweight timers were added before the optimization. On 3 synthetic sessions × 200 companies,
the original canonical reporting path took **5.636 seconds**: scoring 4.621 seconds, peer
lookups/vector preparation 0.908 seconds, peer tables 0.063 seconds, report construction
0.019 seconds. The same workload after indexing/statistics reuse took **0.275 seconds**.
These measurements isolate this hotspot; they do not measure the user's entire database.

Discovery now aggregates `screen_session_seconds`, `candidate_prepare_seconds`,
`peer_table_seconds`, `peer_group_lookup_seconds`, `scoring_seconds`,
`report_construction_seconds`, and `variant_f_evaluation_seconds`. Counters include sessions,
companies, prepared candidates, peer rows, canonical screen eligibility, and F eligibility.
The session timer includes its constituent stages; do not add it to those stages.
Coverage load/evaluation stream together; their exported work timers are measured separately.

## Architecture and exact peer calculations

Before:

```text
session -> full preparation -> peer DataFrame -> repeated scans/statistics per company
        -> every ScreenRecord -> full cached ScreenReport -> F evaluator -> candidates
candidate -> 15m query + Daily query
validation -> repeat discovery -> I0/I1
```

After:

```text
session -> unchanged PIT preparation -> canonical SIC assignment/medians
        -> symbol dictionary + group dictionary + reusable percentile distributions
        -> canonical scoring -> small evaluation view -> evaluate_variant_entry(F)
        -> materialize only eligible F records
        -> minimal candidate requirements + disk-streamed session replay
candidate requirements -> bounded VALUES joins -> native/previous-close dictionaries
validation -> verify manifest -> batch local coverage -> replay I0 -> replay I1
```

`PeerIndex` provides O(1) symbol and selected-group lookup. Each group metric is converted and
checked against the original available-value minimum once. `PeerPercentiles` runs the original
pandas winsorization once, retaining sorted clipped values. Binary search obtains exactly the
original strict-less and equal counts; the percentile arithmetic and tie weighting are unchanged.
Valuation medians still come from the original `assign_peer_groups()` transforms. The original
4/3/2 SIC selection, selected-group membership, valid-value rules, positive valuation multiples,
and minimum peer count are unchanged. Debug-only calculations now run only at DEBUG level.

F discovery uses `HistoricalFeatureScreenSource.f_candidates()`. It passes a small scored view
to **the existing `evaluate_variant_entry()`**, without a second implementation of F thresholds.
Canonical full-screen ranks are preserved on materialized records; F ranks retain the original
`(-evaluation_score, symbol)` ordering, before capacity allocation or intraday coverage.

## Memory and persisted manifest

Preflight explicitly bypasses the comparison cache. It never retains a full historical report.
Only one session's preparation, peer index, and nested scoring objects are needed at once.
Minimal candidate requirements accumulate for the exported candidate manifest. Existing
run-local bar/fact/technical feature caches remain; this refactor does not claim constant memory
for those existing input caches or for the candidate requirements themselves.

The additional `*_candidate_replay.jsonl` file streams to temporary disk during discovery and is
published alongside `*_intraday_candidates.json`. Eligible candidates retain canonical records.
Rejected companies retain compact score values, technical inputs, and exclusion reasons. This
preserves skipped-entry counts, re-entry trigger observations, and score histories of positions
that subsequently stop qualifying for F. Validation loads at most **one session** into its replay
cache; it reconstructs full `ScreenRecord` models only for eligible candidates. The sidecar trades
disk space for bounded report/scoring memory and must be kept beside the manifest.

The manifest contains family, requested interval, variant F, discovery version, config hash,
discovery source-code hash, Python/numerical-library versions hash, actual data hash, count,
ordered candidate requirements with scores/ranks/signal/execution sessions, candidate checksum,
and replay checksum/file name. Storage/output paths are excluded from the config hash.

The data hash streams the actual local Daily bars, facts, universe, and identity-quarantine
inputs in a read transaction. It detects corrections as well as additions/deletions. Intraday-only
sync leaves this hash unchanged. Changes to Daily/fundamental/universe data conservatively require
a new preflight, including changes outside the requested interval. Preflight checks the data hash
before preparation and after discovery to reject concurrent input changes. Validation verifies
data/code/config/runtime compatibility and the complete ordered replay before any backtest.
This requires a linear local input/replay read; it performs no fundamental/technical preparation,
peer reconstruction, or historical candidate screening. It is not a constant-time metadata check.

Old manifests without this provenance fail safely. Validation requires `--candidate-manifest`
or an explicit `--rediscover-candidates`; it never silently falls back to discovery. Explicit
rediscovery also reuses the resulting temporary replay for I0/I1. The export keeps the existing
non-overwrite checks and rollback on interrupted publication.

## Coverage batching and PIT

`Database.iter_entry_coverage_batches()` issues **two SELECTs per at-most-200 requirements**
using bounded `WITH requirements AS (VALUES ...)` joins against the existing composite bar index.
The in-memory dictionaries are `(symbol, execution_session) -> native 15m bars` and
`(symbol, signal_session) -> previous Daily close`. A missing signal-session close stays missing;
an older close is not substituted. Queries do not fetch unrestricted multi-year intraday history.

For 402 exact requirements the test observes **6 SELECTs**, versus the old **804**. Empty input
issues no coverage SELECT. Every batch is evaluated and released before the next is loaded.

F remains a completed-session T decision with execution at T+1. I1 still uses the previous
completed Daily close and first two completed native 15m bars; native volume-weighted VWAP,
opening weakness, complete-session qualification, and next executable bar rules are unchanged.
Future bars/filings are excluded from signal inputs. Coverage affects availability, never F
candidate eligibility/rank. I0, I1, costs, stops, targets, max hold, position limits, L0–L6, and
the frozen champion have no strategy-rule changes.

## Representative benchmark

The offline benchmark uses **50 sessions × 400 companies**, 40-company SIC groups, varied
fundamental values, 75% technical-gate rejection, plus one quarantined identity per session.
It compares the frozen reference report/scoring implementation and original full-report cache
against F-only discovery including disk-spool serialization. It checks exact candidate ordering,
signal/execution sessions, ranks, and complete score breakdowns for all 5,000 F candidates.

| Measurement | Reference | Optimized |
|---|---:|---:|
| Discovery wall time | 230.507 s | 9.374 s |
| Candidate count | 5,000 | 5,000 |
| Retained full historical reports | 50 | 0 |
| Speedup | — | **24.59×** |

Raw measurements: [f-discovery-benchmark.json](f-discovery-benchmark.json).
The replay spool was 32,415,550 bytes. Reference per-stage zeros in that file mean uninstrumented
frozen reference methods; the initial instrumented measurements above provide the stage breakdown.
The synthetic benchmark does not include the initial historical feature-loading stage or data
fingerprinting and is not a promise of the same full-job speedup on the user's database.

## Verification and changed files

Exact regression coverage compares all full report fields except generation time, all score
breakdowns, selected peer groups/medians, F eligible and rejected evaluations (including scores,
blocking reasons and ordering), historical future-filing/bar exclusion, manifest compatibility,
native batching/query counts, one-session replay bounds, and heartbeat/ETA behavior. Complete
I0/I1 replay results are compared, including score history after a held stock stops qualifying.

The broader focused run passed 217 tests and found one pre-existing assertion failure in
`test_screen_strategies.py::test_score_variant_comparison_is_exact_a_through_f_on_configured_management`:
it expects the intraday-hybrid family to contain only C, although the unchanged registry contains
C and F. That unrelated assertion and the registry are left unchanged.
After the final manifest/calendar changes, all 41 focused discovery/manifest tests passed,
including an added runtime-compatibility case and SQLite query-plan checks proving indexed
bar lookups. Ruff check, formatter check (14 changed Python files), and `git diff --check` passed.

The focused verification command was:

```powershell
.\.venv\Scripts\python.exe -m pytest -o addopts='' -q `
  tests/test_backtest_features.py tests/test_peers.py tests/test_screener.py `
  tests/test_scoring.py tests/test_screen_strategies.py `
  tests/test_f_candidate_discovery.py tests/test_f_candidate_manifest.py `
  tests/test_intraday_progress.py tests/test_f_lifecycle_v2.py
```

Implementation: `strategy/performance.py`, `strategy/scoring.py`, `strategy/screener.py`,
`backtest/features.py`, `backtest/f_candidates.py`, `backtest/lifecycle_validation.py`,
`data/database.py`, `cli.py`.
Tests/benchmark: `reference_intraday_screener.py`, `benchmark_f_discovery.py`,
`test_f_candidate_discovery.py`, `test_f_candidate_manifest.py`, plus updated progress and
lifecycle CLI fixtures. Documentation: this report, raw benchmark JSON, README, and the lifecycle
research command documentation. Existing user edits in README/requirements were preserved.

Existing heartbeat fields remain: sessions processed/total, candidate sessions discovered,
session, percentage, and elapsed time. Mean and last-10-session throughput are measured. ETA is
emitted only after three completed sessions with positive measured elapsed time, and is diagnostic.

No full historical preflight/validation, full test suite, network sync, Alpaca/SEC calls, or
production/paper trading was executed. Only deterministic local fixtures and the benchmark ran.

## Manual commands

Run from the repository root. Use fresh stems because exports refuse overwrites. The default
configuration writes to `reports`; substitute its configured reports directory if customized.

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-f-intraday-entry `
  --start 2022-03-09 `
  --end 2024-08-12 `
  --output-stem f_intraday_entry_preflight_2022-03-09_2024-08-12_v2

# After any necessary manual native-intraday data qualification/sync:
.\.venv\Scripts\python.exe -m trading_system.cli validate-f-intraday-entry `
  --start 2022-03-09 `
  --end 2024-08-12 `
  --candidate-manifest .\reports\f_intraday_entry_preflight_2022-03-09_2024-08-12_v2_intraday_candidates.json `
  --output-stem f_intraday_entry_validation_2022-03-09_2024-08-12_v2

# Optional small offline performance reproduction:
.\.venv\Scripts\python.exe tests/benchmark_f_discovery.py --compare --sessions 50 --companies 400
```
