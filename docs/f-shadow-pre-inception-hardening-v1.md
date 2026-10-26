# F Shadow pre-inception input hardening

This contract applies to local forward `F/configured/C1`. It does not change the
F definition, weights, thresholds, universe rules, ranking, sizing, fills, position
management or `LEGACY_RESEARCH`. It does not start a forward test.

## Configuration and assets

`load_strategy_config` is the pure local part shared by normal settings and Shadow.
Default configuration is rooted at the installed repository, independent of CWD.
Explicit configuration paths resolve against the invoking CWD; relative storage
paths resolve against the configuration directory (or its parent for `config/`).
Default Shadow storage remains `shadow_f.sqlite3` beside the absolute market DB.
Configuration resolution constructs no providers and reads no credentials.

The Alpaca asset client requests SDK `raw_data=True`. Each asset is normalized
individually into `TradableAsset`, preserving the exchange verbatim, including
`TXSE` and future strings. Raw JSON uses `class`; SDK objects use `asset_class`.
Required symbol/status/class/flags/exchange fields are validated. Malformed records
are skipped with warnings and counts. At least `minimum_retained_ratio` of raw
records must parse, and the existing database snapshot retention guard remains.
Diagnostics distinguish raw/parsed/invalid/tradable counts and unknown exchanges.

## Daily coverage and current observations

`daily-observations/v2` separates `REQUEST_SUCCESS` from observation evidence.
Only a complete bounded local set of valid positive-volume bars for all official
XNYS sessions, with all requested observations actually returned, can establish
`VERIFIED_COMPLETE`. Cached bars cannot certify an omitted response under a new
feed or adjustment. A previously proven range with no request planned is explicitly
`NOT_REQUESTED_LOCAL_VERIFICATION`. Otherwise the attempt records
`OBSERVATION_ABSENT` or `OBSERVATION_INVALID`, expected/present/missing counts and a
missing-session sample. Absence is not automatically a provider error: listings,
delistings, halts, feeds and no-trade days can all cause it. The adapter does not
guess a cause. Exceptions do not advance coverage. Old request-only metadata is
not a proof under v2. An incomplete range remains eligible for incremental retry.

Zero-volume placeholders remain stored without price rewriting. The pure
`current_session_observation` helper qualifies official-session membership,
matching Daily timestamp, finite positive consistent OHLC and positive volume.
The screener records this evidence without changing legacy feature calculations.
Only Forward uses it as an input barrier. CC-01 remains the original mandatory
API for held/pending/final execution; no stale fill or deferral is introduced.

## Causal F completeness

The single production screen produces a raw `ScreenReport` and the canonical
`champion_ranking`. Qualification is O(N) over these existing records and uses
`evaluate_variant_entry` / `evaluate_strategy_gate`; it introduces no rank formula.
Canonical independently observed hard failures prove rejection. This includes
identity/financial exclusions, observed hard-universe failures, known low Quality
or Valuation, negative OCF and observed momentum failures. A missing downstream
value is irrelevant once a mandatory rejection is certain. A stale historical
Quality score cannot prove rejection for the current session. Unobserved values are
never treated as negative observations. Current price-derived rejections require
a qualified current observation. Unknown potentially eligible rows remain
`F_ELIGIBILITY_UNRESOLVED` in the qualification counts/reasons.

Only canonical rank one needs ATR for a new intent. Missing rank-one ATR blocks
entry; rank two is never substituted. Complete input produces the same eligible
symbols, ordering and signal as canonical F. Legitimate no candidates remains a
valid no-trade result. Missing relevant inputs produces `NO_TRADE_DATA_INCOMPLETE`.
An incomplete new-entry cross-section does not suppress stop/target/hold management
of an existing position. STRICT still executes before any economic transition.

## Upstream readiness and inception

`evaluate_shadow_input_readiness` reads a pinned `mode=ro`, `query_only=ON` snapshot
and existing `sync_state` dataset keys: `asset_universe`, `daily_history` (or
`historical_bars`, choosing the most recent workflow attempt), `sec`, plus the
actual current SPY bar. There are no clients,
sync, repairs or additional per-symbol readiness SQL queries.

Operational successful snapshots must be no older than 96 hours, covering a
weekend and one exchange holiday; future/invalid success timestamps are rejected.
SEC success must follow the analysis-session close. This is a successful refresh
requirement, not a demand for new filings from every issuer each day. Missing,
running, failed, error-bearing and unexplained partial datasets block readiness.
An SEC partial state caused solely by safely excluded identity conflicts can be
qualified with a warning if no rank-relevant unresolved input remains. Its
technical completion uses `last_completed_at`; general `last_success_at` retains
its original success-only meaning. Completed syncs clear a previous failure's
stale `error` field. Daily
feed/adjustment must match the configuration; SPY and potential F candidates need
usable observations for the official analysis session. Counts and reasons retain
the distinction between irrelevant gaps and unresolved eligibility.

Before `SHADOW_INITIALIZED`, `ShadowRunner.initialize` validates champion config,
fingerprints, market existence, readiness and the completed-session screen. It
performs one screen only and refuses with `SHADOW_INCEPTION_DATA_NOT_READY` before
opening the journal file. If an official close occurs during that screen, it
refuses with `SHADOW_INCEPTION_SESSION_CHANGED_RETRY` without a second screen or
file. Creation time is sampled after qualification; a backward clock also refuses.
Successful synthetic inception tests retain the readiness
snapshot/digest, universe/qualified/unresolved counts and analysis session.
Initialization does not create an intent or simulate past sessions.

Before each new session the runner verifies the journal and drift, executes CC-01,
then obtains the same reusable readiness result. Upstream failures prevent new
intents and are recorded in `SCREEN_EVALUATED`. Existing risk management and prior
intent execution remain governed by their mandatory STRICT critical observations.

## Journal and drift

Event envelope, SQLite append-only triggers, SHA-256 chain and schema version 1
remain unchanged. Inception adds `input_readiness` and separate data-pipeline
identity fields. Verification accepts prior V1 envelopes, verifies any additional
readiness/pipeline digests, and current-run drift still refuses an older fingerprint
scope. There is no automatic continuation or migration.

NO REAL V1 JOURNAL EXISTS. MIGRATION NOT REQUIRED.

`engine_fingerprint` retains its existing decision/execution file-manifest
semantics. `data_pipeline_fingerprint` separately hashes LF-normalized whole files:

```
config.py
data/alpaca_client.py  data/sync.py  data/xbrl_parser.py
data/sec_client.py  data/sec_identity.py  data/database.py
data/market_sessions.py  data/observations.py  data/daily_history.py
data/universe.py  models/market_data.py  models/fundamentals.py
```

Both use the deterministic installed runtime manifest: Python, alpaca-py,
requests, exchange-calendars, pandas, numpy, pydantic and PyYAML. Ingestion-only
changes such as `alpaca_client.py` can therefore be identified independently of
execution changes. Shared input/domain files can affect both fingerprints.
After actual inception, relevant code/config/runtime changes cause
`SHADOW_CONFIGURATION_DRIFT`. The chosen forward version must be consciously
frozen before inception. A changed version requires an explicit new journal.

## Adjustment and operational limits

Bar requests explicitly carry the configured feed and adjustment. Adapter
diagnostics and Daily sync metadata record both; historical-bars sync records
them too. This establishes the requested interpretation, not independent proof
of Alpaca's actual corporate-action response.
`PROVIDER_ADJUSTMENT_BEHAVIOR_NOT_INDEPENDENTLY_VERIFIED` remains applicable.
There is no custom split adjuster or historical price rewrite.

Shadow fills are modeled, fixed slippage is 5 bps, and there are no broker fills,
partial fills, rejects, borrow checks or reconciliation. Historical survivorship,
QRVO and current-SIC/company PIT caveats remain. Later provider revisions do not
rewrite journal history. There is no external hash anchor. Separate research
status/documentation debt (Pairs, capacity and regimes) is outside this task.

After review of this hardening, the next step is a separate real-data inception
preflight. Do not initialize or run a real journal as part of hardening.
