# TraWalp strategy-research lifecycle

This registry separates reproducibility from active research runtime. Archived does not mean
deleted: archived presets and their deterministic regression coverage remain available through
their explicit historical comparison families, but they are excluded from active expensive
comparisons.

## Current champion

- F0/C-intraday-dynamic — `CHAMPION_CONTROL`; frozen control with unchanged C selection and
  intraday execution/management semantics.

## Pending analysis

- F1/C-intraday-loss-cooldown — `PENDING_EVALUATION`.
- F2/C-intraday-opening-survivor-gate — `PENDING_EVALUATION`.
- Their existing `research-intraday-isolation` family remains unchanged.

## Active next research

- F3/C-intraday-thesis-recovery — `ACTIVE_RESEARCH`; after a negative gross market result,
  same-symbol re-entry requires a strictly higher point-in-time C score than at the failed entry.
- F4/C-intraday-first-hour-pullback — `ACTIVE_RESEARCH`; EMA20-qualified first-hour observation,
  confirmed pullback entry, fixed 0.75% stop, and next confirmed swing-high/session-close exit.
- The opt-in `research-intraday-next` family contains exactly F0, F3 and F4.

## Archived and compatibility strategies

- D1–D5 are `ARCHIVED_RESEARCH` and remain callable through `research-d1-d5`.
- Dynamic-hold, take-profit, ATR-trailing, partial-profit and fixed-stop experiments are
  `ARCHIVED_RESEARCH`.
- The original legacy preset and configured score controls remain `LEGACY_COMPATIBILITY`.

The period 2025-05-01 through 2026-08-12 has already informed hypothesis construction. F3/F4
results over that period are development research evidence, not out-of-sample evidence. No
result from the active family automatically promotes a strategy.
