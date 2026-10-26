# F Shadow Execution V1

## Purpose and scope

This is local forward execution for `FROZEN_CHAMPION_F`, `F/configured/C1`,
`QUALITY_VALUE_MOMENTUM`, `CONFIGURED`. It observes completed official XNYS
sessions using already available local inputs. It never synchronizes data or
constructs a provider, broker, or order-submission client.

A backtest replays a historical interval and liquidates at its end. Shadow
starts with a new, immutable inception and retains its virtual position across
invocations. Paper trading would submit orders to an external simulated broker;
Shadow has only internal intents and modeled fills. There is no broker state.

V1 does not change Quality, Valuation, Momentum, ranking, universe, thresholds,
sizing, stops, target, hold, re-entry, or capacity. It does not perform research,
data repair, historical backfill, QRVO replay, or historical validation runs.

## Inception and forward-only contract

`shadow-f-init` exclusively creates a new separate SQLite file. The default is
`shadow_f.sqlite3` beside the configured market database, normally
`data/shadow_f.sqlite3`. A journal path resolving to the market database,
including a hard-link alias, is rejected. Existing files are never overwritten.
Status, verification, and run do not create an absent journal or market database.

The sole inception event records journal ID, UTC creation time, local Git HEAD
and branch, champion identity/definition, full economic config, fingerprints,
runtime versions, STRICT contract version, schema version, initial capital, and
the latest completed official session at creation. Capital defaults to `100.0`;
`--initial-capital` can override it with a positive finite value.

There is no historical start-date or inception-date CLI option. Initialization
now requires a local dataset readiness check and one read-only current-session
Champion screen, before creating a file. It creates no entry intent and processes
no session. See [pre-inception hardening](f-shadow-pre-inception-hardening-v1.md).
Every session at or before the inception completed-session boundary is refused
with `SHADOW_BEFORE_INCEPTION`. If inception occurs during a session, that session
can first be processed after its official close. If inception occurs after its
close, the following official session is the first possible processing session.
The Python clock injection exists to exercise synthetic historical fixtures.

No production journal was initialized during implementation. Actual forward
inception is a separate action after infrastructure review.

## Session lifecycle and STRICT dependency

The calendar is the existing `data.market_sessions` XNYS calendar, including
holidays, DST, and early closes. Session membership is never inferred from bars.
Explicit `--session` must name a completed official session. The default is the
next official session after the last commit, or after inception if none exists.
If it has not closed, the result is `NO_COMPLETED_SESSION`. Each invocation
processes at most one session. Catch-up processes missed forward sessions one
at a time in order; it cannot cross the inception boundary or skip a session.

The runner obtains `BEGIN IMMEDIATE` on the journal **before** verification and
source reads. It verifies the entire journal, compares inception fingerprints,
checks sequencing, and pins a separate read-only market SQLite transaction.
All inherited market Database access is bound to `mode=ro`, `query_only=ON`.
No market schema initialization, writes, repair, or sync are performed.

The existing CC-01 API
`backtest.data_integrity.validate_session_position_data` is mandatory. It
validates the completed official session, full official chronology, held bars,
and pending T+1 entry bars before fills, management, holding increments, or
marks. V1 neither duplicates nor modifies this integrity implementation.

`HELD_POSITION_BAR_MISSING`, `PENDING_ENTRY_BAR_MISSING`, stale/invalid critical
bars, and other critical errors fail the whole economic session. Only a
`DATA_PREFLIGHT_FAILED` event with the error, observed inputs, and unchanged
state digest is appended. There is no `SESSION_COMMITTED`, fill, mark, holding
increment, equity change, pending consumption, or deferral. Retry must process
the same expected session. An explicit external data correction may make that
retry possible; the runner never performs it.

Successful session events are:

1. `SESSION_STARTED`: portfolio before and binding to inception fingerprints.
2. `DATA_PREFLIGHT_PASSED`: STRICT contract and observed critical OHLC.
3. Economic events: prior position management, pending entry execution or
   rejection, intrabar exits/management.
4. `SCREEN_EVALUATED`: completed-session F ranking, input provenance,
   availability result, compact exclusion summary.
5. Close management, followed by `ENTRY_INTENT_CREATED` or `DECISION_NO_TRADE`.
6. `SESSION_COMMITTED`: portfolio after, portfolio digest, session event/decision
   digest, and the hash of the event immediately preceding the commit.

Each event also carries its session in the envelope. The commit's own event hash
is the terminal chain hash returned by the runner. Its payload cannot contain
its own hash recursively, so `terminal_event_hash` in that payload explicitly
references its predecessor. No economic session is complete before its commit.

There is no artificial liquidation when `shadow-f-run` returns. A ten-session
hold is a normal PositionManager close exit, subject to the current-bar STRICT
barrier. A stale final/current bar can never be used for liquidation. The existing
STRICT backtest final-liquidation regression is also included in validation.

## Candidate ranking and data completeness

Production screening uses the exact `screen_champion` operations: the existing local
`Screener` with `use_market_snapshots=False` and `champion_ranking`. It retains the
raw report for causal completeness and reuses the ranked report. Injected
fixture screens pass through the same `champion_ranking`. Selection uses the
existing `BacktestEngine._entry_orders`. No Shadow ranking formula exists.
F combines Quality and Valuation with its existing normalized F weights;
equal scores sort by symbol alphabetically. The full ordered eligible list
records symbol, F score, Quality score, Valuation score, and rank.

Observed threshold failures are legitimate ineligibility. A complete evaluated
cross-section without eligible names records `NO_ELIGIBLE_CANDIDATES`. An
occupied C1 records `CAPACITY_FULL`; there is no rotation. Availability failures
record `NO_TRADE_DATA_INCOMPLETE` when an input remains rank-relevant: an
empty/wrong-session cross-section, inconsistent membership, unresolved current
observation, scores, PIT prerequisites, F gate features or selected rank-one ATR.
A fully observed mandatory rejection makes later gaps irrelevant. Upstream
dataset readiness failures also prevent new intents. Position risk management
and prior intent execution still require the unchanged STRICT critical bars.

`NO_TRADE_DATA_INCOMPLETE` prevents new intent selection across the whole
cross-section. It does not defer a previously committed intent or stop management
of a fully observed existing position. Critical state-bar errors instead fail
the session without an economic commit. A no-trade event describes the new
close-of-session entry decision; a prior intent can have executed in that session.

Completeness is relative to the existing local production universe and available
PIT feature semantics. V1 cannot prove that an upstream provider has delivered
every fact or that the local universe contains every historical security.

## Entry, sizing, and position management

A signal on completed T creates an intent for the next official session T+1.
It does not book a fill or reserve a separate cash balance. T+1 requires the
observed Daily open. The unchanged `_open_position` helper applies the buy fill
`open * (1 + 5 / 10000)` and fractional quantity:

```text
stop_distance = min(signal_ATR14 * 2, fill * 0.10)
risk_quantity = open_equity * 0.01 / stop_distance
cap_quantity = open_equity * 1.0 / fill
cash_quantity = cash / (fill * (1 + commission_rate))
quantity = min(risk_quantity, cap_quantity, cash_quantity)
stop = fill - stop_distance
target = fill * 1.12
```

Commission is zero. No new rounding is applied. `_execute_decision` supplies the
existing sell slippage, proceeds, fees, and realized profit calculations.
The actual `PositionManager` determines opening gap stop, opening target,
intrabar stop before target for unresolved Daily OHLC ambiguity, and hard
max-hold exit at the tenth session close. The entry session counts as one.
Re-entry is enabled with zero cooldown and requires winning the ordinary F
ranking again. C1 allows only one position or effective pending entry.
Trailing stops, ATR trailing stops, partial take profit, signal decay, rotation,
and profit lock stay disabled. Exit reason aliases match the configured engine.

## Immutable journal and reconstruction

The separate DB has one append-only `events` table. It has no mutable portfolio
or history table. SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers reject
changes, including inception changes. Insert guards enforce sequence append,
previous-hash linkage, genesis type, and journal identity. Primary/unique
constraints reject duplicate sequences, event hashes, inception events, and
session commits, including attempted replacement inserts.

The source of truth is the event stream. Economic events contain the previous
state digest and resulting state. The reducer reconstructs cash, complete
PositionState, pending intent (including the frozen selected ScreenRecord),
realized PnL, position sequence, and previous closed positions needed for
re-entry. Unrealized PnL and equity derive from the journaled mark and cash.
The verifier checks transition type, C1, pending timing, entry/exit conservation,
ledger conservation, before/after consistency, and commit digests without
rerunning old decisions or rereading old market/fundamental inputs.

## SHA-256 chain and canonical encoding

Genesis is `SHADOW_INITIALIZED` with `previous_event_hash` equal to 64 ASCII zero
characters. Every event hashes its entire envelope: sequence, journal ID, schema
version, UTC event timestamp, session, event type, parsed payload, and previous
hash. The `event_hash` field itself is excluded. SHA-256 uses UTF-8 canonical
JSON with sorted string keys, no whitespace, Unicode preserved, and no NaN/Inf.
Dates are ISO dates, aware datetimes normalize to UTC with microseconds, sets
sort by canonical encoding, decimal source prices are exact normalized decimal
strings, and engine floats retain their finite binary64 round-trip JSON values.
No `repr`, ad hoc rounding, or integer-share conversion is used.

`shadow-f-verify` checks exact schema/index/trigger definitions, sequence
continuity, chain links and hashes, schema/contract versions, inception identity
and snapshot fingerprints, duplicate commits, chronological sessions, event
lifecycle, candidate ordering/digests, ledger reconstruction, and commit state.
The same verification runs under the writer lock before **every** run and again
inside the session transaction before commit. Corruption raises
`SHADOW_JOURNAL_INTEGRITY_FAILURE` and stops further processing.

These guards protect normal application paths. SQLite is not WORM storage: an
administrator with file access can remove triggers, recompute an entire
self-consistent chain, replace a file, or truncate to an earlier valid commit.
There is no external signature, trusted terminal-hash anchor, or backup service
in V1. Preserve independent copies/terminal hashes for stronger offline audit.

## Fingerprint scope and drift

`strategy_fingerprint` hashes the complete `FROZEN_CHAMPION_F` definition.
`economic_config_fingerprint` hashes the validated strategy model excluding
storage paths, SEC transport settings, and backtest initial capital (journal
capital is an immutable inception field). All other config sections, including
screen thresholds, score weights/curves, universe rules, and disabled management
settings, are included. Their actual snapshots are stored at inception.

`engine_fingerprint` hashes a sorted path-to-SHA-256 manifest of the following
whole UTF-8 source files, normalizing CRLF to LF, plus runtime versions:

```text
champion.py, config.py, cli.py
backtest/engine.py, backtest/position_manager.py, backtest/presets.py
backtest/diagnostics.py, backtest/screen_strategies.py
backtest/research_registry.py, backtest/research_definitions.py
backtest/data_integrity.py
data/database.py, data/market_sessions.py, data/universe.py, data/sec_identity.py
fundamentals/*.py, technical/*.py, models/*.py
strategy/screener.py, strategy/scoring.py
shadow/*.py
```

Runtime versions: Python, exchange-calendars, pandas, numpy, pydantic, PyYAML.
The manifest and versions are persisted. This includes scoring/eligibility,
peers, universe/identity, feature models, position sizing, fills, management,
calendar, CC-01, journal schema/reducer, runner, and CLI. Whole-file hashing is
deliberately conservative: even a comment or unrelated edit in these files can
stop a journal. Git HEAD/branch are inception provenance; a commit alone is not
the engine identity. Dirty relevant code is covered by content hashes.

The existing STRICT API is additionally bound as
`CC01/validate_session_position_data/v1` and by the SHA-256 of its entire file.
Changes to identity, config, engine files/runtime, schema, or STRICT contract
raise `SHADOW_CONFIGURATION_DRIFT`. There is no automatic migration or fingerprint
update for an initialized journal. A reviewed new version/journal is a separate
decision. Status reports drift without changing the journal; verification can
still inspect a structurally compatible old journal without source reevaluation.

## Input provenance

One SQLite read snapshot binds each session's source reads. The journal stores
digests of every consumed Daily history query (symbol, cutoff, limit, timeframe),
every consumed available-facts query (symbol, filing cutoff, metric), tradable
company identities, and unresolved identity exclusions. Values come through
the existing Database model/ordering semantics; timestamps from unrelated sync
metadata are not decision inputs.

`SCREEN_EVALUATED` also hashes the complete relevant ScreenRecord feature set
for the cross-section, including ineligible peer rows, sorted by symbol. Its
generation timestamp is excluded. It stores the ordered eligible ranking and
its digest. Critical held/pending Daily OHLC is stored, with a digest, at
preflight; the management input digest also covers those bars. A combined input
set digest binds the per-query and feature digests. Thus later local revisions
can be compared to the recorded inputs without storing millions of raw rows.
Verification checks the historical journal itself, not present-day source
equality. Provider revisions never automatically reevaluate or rewrite history.

## Idempotency, concurrency, and recovery

An already committed explicit session returns `ALREADY_PROCESSED` with no new
events or economic actions. Skipping an official session is `SHADOW_SESSION_GAP`.
The full session and its resulting event-sourced state share one SQLite
transaction (`BEGIN IMMEDIATE`, `synchronous=FULL`). An exception/process abort
before commit rolls back all session events; restart verifies the prior chain
and retries the same session deterministically. A failure event itself also
commits atomically with unchanged economic state. Two concurrent runners
serialize on the SQLite writer lock; the second observes the first commit and
becomes an idempotent no-op for the same explicit session. Lock timeout is an
error, never permission to bypass verification.

## CLI

After review, from the repository root:

```powershell
python -m trading_system.cli shadow-f-init
python -m trading_system.cli shadow-f-init --initial-capital 100.0
python -m trading_system.cli shadow-f-run
python -m trading_system.cli shadow-f-run --session YYYY-MM-DD
python -m trading_system.cli shadow-f-status
python -m trading_system.cli shadow-f-verify
```

All four commands accept `--journal PATH`. The existing global `--config PATH`
comes before the subcommand. Shadow reads strategy YAML directly and does not
load credentials or call the normal synchronization setup. There is no dry-run
option in V1; status and verification are read-only. Valid verification emits
JSON with `status: VALID`; refusals emit structured JSON on stderr with exit 1.
Status summarizes inception, last/next session, cash/equity, open position,
pending entry, last decision, verification, and drift. It does not dump events.

## Known limitations

- Shadow fill is modeled, not broker-observed.
- Slippage remains fixed at 5 bps.
- No partial broker fills.
- No rejected broker orders.
- No borrow issues.
- No broker reconciliation.
- Historical universe biases do not disappear.
- Current SIC/company identity may not be fully PIT.
- Provider revisions may change local source data after a session, but journal
  history does not rewrite.
- Daily OHLC ambiguity retains the existing conservative stop-first convention.
- Verification is linear in journal length; V1 has no mutable state cache or
  externally trusted chain anchor. SQLite provides local single-writer safety.
- Re-entry diagnostic trigger freshness is reconstructed from the previous
  closed position; the frozen zero-cooldown entry rule is unchanged. No trigger
  freshness gate is enabled for configured F.
- No end-of-forward-test liquidation, broker execution, provider repair, data
  scheduling, journal migration, or automated journal restart/inception exists.

## Validation and implementation audit

Focused temporary fixtures cover immutability/tampering, inception, config/code/
STRICT drift, official session sequencing, current/future rejection, C1, T+1
fills/ATR sizing, critical missing bars including hold session 10, no-trade
reasons, source revisions, crash rollback/restart, concurrency, CLI, and economic
equivalence with the unchanged Champion backtest on complete small fixtures.
Relevant existing CC-01 and PositionManager regressions are run locally.
No real inception, forward sessions, 2022 backtest, full historical suite, or
provider tests are part of implementation validation. The implementation audit
and exact test results are recorded in `reports/f_shadow_execution_v1_audit.md`
and `reports/f_shadow_execution_v1_summary.json`.
