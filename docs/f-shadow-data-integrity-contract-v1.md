# F Shadow Daily data integrity contract v1 — CC-01

This contract governs Daily data observability for critical position state. It
does not change F/configured/C1 economics or implement a Shadow journal, broker,
orders, reconciliation or a historical data repair.

## Official sessions and completion

The canonical sequence is `data.market_sessions.trading_sessions_between()`, using
the existing XNYS calendar, independently of stored bars. A missing bar is never
a missing session. A session can be processed only at or after its official close
from `regular_session_bounds()`, using an explicitly supplied timezone-aware
`now`. This includes holidays, DST and early closes from the existing calendar.

`previous_processed_session` must be the immediately preceding official session.
Skipping, repeating or reversing sessions raises `OFFICIAL_SESSION_SEQUENCE_GAP`.
`None` means genuine inception, never a way to restart the cursor after a gap.
Calendar errors use role `SESSION`, with `symbol=null`:

- `SESSION_NOT_OFFICIAL`
- `SESSION_NOT_COMPLETE`
- `SESSION_CLOCK_INVALID`
- `OFFICIAL_SESSION_SEQUENCE_GAP`

Calendar completion is not a proof that every upstream provider record was
finalized correctly. This contract validates the local dated observations and
their existing model constraints; it does not certify provider coverage.

## Critical Daily observations

Before **any** economic transition for a session, validate the entire set of
held positions and outstanding next-session entries, in one bounded batch read.
Each must have its own Daily bar for the exact official session and symbol.
Checks revalidate the existing `DailyBar` model and `validate_market_bar()` OHLC
constraints, including positive finite prices, aware timestamps and timeframe
`1d`. Strict critical-state validation also requires positive volume: a
zero-volume record cannot establish executable stop/target observations, whether
it came from a placeholder, a halt or another upstream condition. This is an
integrity rejection requiring review, not a strategy threshold or a fabricated fill.

No previous close, last-known bar, forward fill, synthetic flat OHLC or zero-volume
substitute is constructed or used, including for valuation. Bars for noncritical,
unselected symbols do not block this preflight. Candidate/ranking completeness
remains with existing screening rules and is distinct from this contract.

## Held positions

The canonical missing-data code is **`HELD_POSITION_BAR_MISSING`**, with
`role=HELD_POSITION`. A wrong-date observation raises
`HELD_POSITION_BAR_STALE`; invalid OHLC, symbol, timeframe or volume raises
`HELD_POSITION_BAR_INVALID`. Impossible entry-session state raises
`HELD_POSITION_STATE_INVALID`.

All failures stop processing the session. Without the session's OHLC it is
impossible to establish opening gap stops, opening targets, intrabar stops or
intrabar targets. Missing observations must not be treated as unhit levels.

Entry session remains holding session 1; hard max hold remains 10 official
sessions. The validator does not mutate counters. The engine calls it before
`holding_days += 1`, management callbacks, fills, marks and equity records. A
missing day-10 observation stops with the last valid day-9 state, rather than
moving the time exit to day 11. The exception's `holding_session_number` is a
diagnostic count of official sessions inclusive of entry, not a committed update.

## Pending entries

The canonical missing-data code is **`PENDING_ENTRY_BAR_MISSING`**, with
`role=PENDING_ENTRY`. Stale and invalid records use corresponding
`PENDING_ENTRY_BAR_STALE` / `PENDING_ENTRY_BAR_INVALID` codes.

A signal at official session T must be processed at exactly the next official
session T+1. The API receives each pending entry's **signal session**, not its
planned execution date. A mismatched execution session raises
`PENDING_ENTRY_SESSION_MISMATCH`. A missing T+1 bar stops the entire session before
any held-position management or other entry. It creates no simulated order/fill,
does not consume or discard the committed pending state, and does not defer it to
T+2. Any later recovery/replay must be an explicit separate operational decision.

## Structured failure and deterministic order

`backtest.data_integrity.DataIntegrityError` is a `RuntimeError` with directly
accessible `code`, `symbol`, `session`, `role`, `position_entry_session` and
`holding_session_number`. `as_dict()` produces JSON-compatible fields with
`status=DATA_INTEGRITY_FAILURE`. No consumer should parse exception message text.

Validation order is calendar first, held symbols alphabetically, then pending
symbols alphabetically. Validation reads its inputs and never changes them. A
critical failure is not `NO_TRADE_DATA_INCOMPLETE` and cannot be downgraded to an
ordinary no-trade screen or silently skipped.

## Final liquidation

Strict evaluation uses the final **official** session of the requested window,
even when that day is absent from the local bar table. It requires a valid current
bar for every remaining position before management and again at the final exit
barrier. The legacy `bars_available_as_of(..., limit=1)` liquidation fallback is
unreachable in Strict mode. Final-session failures use the same held-position
codes; they cannot produce a stale fill or a successful partial portfolio result.

## Legacy versus Strict

`BacktestEngine(..., data_integrity_mode=DataIntegrityMode.STRICT)` selects the full
contract. `run_champion_backtest(..., data_integrity_mode=DataIntegrityMode.STRICT)`
exposes the same policy without changing F economics. Strict is currently a Daily
path; native intraday management/entry paths reject it with
`STRICT_DAILY_PATH_REQUIRED`, rather than falling back to a weaker policy.

Both historical entry points retain `LEGACY_RESEARCH` as their default to preserve
published research compatibility. Legacy `_backtest_sessions()` intersects XNYS
with locally represented bar dates and can omit a globally absent session. Legacy
held-bar handling can skip management, increment the holding counter, mark the
previous price, and liquidate using an old bar at the window boundary. These
behaviors are reproduced in regression tests, not endorsed for execution.

The older research-only `require_complete_daily_position_bars=True` flag and its
`DAILY_POSITION_DATA_UNAVAILABLE` error remain compatible. That held-only check
does **not** implement the complete Shadow contract: it lacks the independent
official sequence and pending-entry validation. When the new Strict mode is set,
its structured preflight runs first regardless of that older flag.

No historical report is overwritten, no old return is recomputed, and the
historical CC-01 finding remains preserved as evidence. New Strict result metadata
explicitly identifies the mode and official-session entry/final-exit policy.

## Shadow integration API and atomicity

The next Shadow implementation must use this preflight unconditionally; selecting
the historical default or catching and continuing after an integrity failure is
not permitted. The API performs no I/O or persistence:

```python
validate_session_position_data(
    session,
    bars=critical_daily_bars,
    held_positions={symbol: position.entry_date for symbol, position in held.items()},
    pending_entries={symbol: pending.signal_date for symbol, pending in entries.items()},
    previous_processed_session=last_committed_session,  # None only at inception
    now=aware_clock_now,
)
```

The caller obtains `session` from the official XNYS sequence, waits for its close,
loads the union of held and pending Daily observations, and validates the entire
set before starting economic transitions. Only after successful validation may it
stage and later commit the session as one unit. For any critical data integrity
failure, **no new economic Shadow state may be committed for that session**:
cash, positions, holding counters, pending orders and the economic decision
journal remain at the last committed session. The processed-session cursor must
also remain unchanged. Separate operational error reporting must never masquerade
as a successful economic decision. Future persistence must preserve atomicity if
any later transition fails as well; this task builds no journal or transaction layer.

## ABBV evidence and acceptance boundary

Local evidence: 2022-03-08 is an official XNYS session; ABBV has Daily bars on
2022-03-07 and 2022-03-09 but none on 2022-03-08. The preserved F/C1 regime report
records `F-000005-ABBV-2022-02-24`, held from 2022-02-24 through 2022-03-09. A
read-only critical-state replay for 2022-03-08 now raises
`HELD_POSITION_BAR_MISSING` (official holding session 9). Synthetic engine
regressions independently reproduce and reject the same missing-session pattern.

Complete-data regression fixtures preserve next-open entry, opening gap stop,
opening target, conservative intrabar stop-before-target, hard max hold, prices,
quantities, costs and equity. `FROZEN_CHAMPION_F`, `strategy.yaml` and position
management formulas are unchanged. No provider is contacted and no production
database mutation or missing-bar repair is performed.

Passing this contract closes CC-01 **for Shadow implementation** and permits the
next journal implementation task. It does not establish readiness for paper
orders or live trading. STOP after the CC-01 fix and its review.
