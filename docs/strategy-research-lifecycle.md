# TraWalp strategy-research lifecycle

This registry separates reproducibility from active research runtime. Archived does not mean
deleted: archived presets and their deterministic regression coverage remain available through
their explicit historical comparison families, but they are excluded from active expensive
comparisons.

## Current champion

- F/configured is the frozen champion for the current Daily robustness/capacity research.
  See [F lifecycle V2](f-lifecycle-v2-research.md) for the separate new research families.

## Historical intraday control

- F0/C-intraday-dynamic — `CHAMPION_CONTROL`; frozen control with unchanged C selection and
  intraday execution/management semantics.

## Archived research

- F1/C-intraday-loss-cooldown — `ARCHIVED_RESEARCH`.
- F2/C-intraday-opening-survivor-gate — `ARCHIVED_RESEARCH`.
- F4/C-intraday-first-hour-pullback — `ARCHIVED_RESEARCH`; its entry mechanism remains available
  as a reproducible reference, but its fixed 0.75% stop and confirmed-swing-high/session-close
  management were rejected as a complete strategy.
- Their historical `research-intraday-isolation` and `research-intraday-next` family compositions
  remain unchanged.

## Active next research

- F3/C-intraday-thesis-recovery — `ACTIVE_RESEARCH`; after a negative gross market result,
  same-symbol re-entry requires a strictly higher point-in-time C score than at the failed entry.
- F5/C-intraday-first-hour-pullback-f0-management — `ACTIVE_RESEARCH`; it reuses F4's causal
  EMA20/first-hour/confirmed-pullback entry planner and then delegates all management to F0.
- F-intraday/F-intraday-dynamic — `ACTIVE_RESEARCH`; exact PIT screen F candidates with the
  unchanged F0 intraday-dynamic management preset. F0 remains C plus intraday-dynamic.
- The opt-in `research-intraday-hybrid` family contains exactly F0, F3, F5 and F-intraday.

## Archived and compatibility strategies

- D1–D5 are `ARCHIVED_RESEARCH` and remain callable through `research-d1-d5`.
- Dynamic-hold, take-profit, ATR-trailing, partial-profit and fixed-stop experiments are
  `ARCHIVED_RESEARCH`.
- The original legacy preset and configured score controls remain `LEGACY_COMPATIBILITY`.

For new hypotheses, all already analyzed 2022–2026 history is DEVELOPMENT / RESEARCH, not clean
OOS. The following classification describes the historical F3/F5 round only.

The period 2025-05-01 through 2026-08-12 has already informed hypothesis construction. F3/F5
results over that period are development research evidence, not out-of-sample evidence. Earlier
history is labeled `historical_extension` unless the user has explicitly certified an untouched
holdout. No result from the active family automatically promotes a strategy.
