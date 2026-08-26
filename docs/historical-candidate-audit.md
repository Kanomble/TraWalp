# Historical Candidate Funnel / Point-in-Time Data Audit

## Purpose

`audit-candidates` explains why a historical backtest did or did not create entries. It is
diagnostic only: it does not alter score thresholds, recovery rules, ranking, position sizing,
or execution. The command runs the normal `BacktestEngine`, the optimized
`HistoricalFeatureScreenSource`, and the configured entry/portfolio path. The structured audit
observer cannot feed data back into a trading decision.

## Command

```powershell
python -m trading_system.cli audit-candidates `
  --start 2025-04-21 `
  --end 2026-08-12 `
  --variant C
```

Variant `C` is the unchanged default. `--variant` accepts `A|B|C|D|E|F`; D/E/F are research
screens and do not replace the production default. `--near-miss-limit` controls the bounded number of late-stage
rejections retained per session (default 10). Full session and monthly summaries are always
written even when no candidates exist.

## Sequential funnel

Each symbol/session has one first decisive failure. The next stage receives only survivors from
the preceding stage:

```text
identity
→ static financial/REIT filters
→ market history
→ price
→ liquidity
→ market cap
→ PIT fundamentals
→ positive operating cash flow
→ quality availability / threshold
→ valuation availability / threshold
→ opportunity availability / threshold
→ timing availability / threshold
→ weighted total threshold
→ price above SMA20
→ recovery gate
→ variant-specific D/E/F gate (when applicable)
→ eligible candidate
→ portfolio/ranking
→ entry order
→ next-session execution
```

`incoming = rejected + passed` is checked at every stage. `all_failure_reasons` are deliberately
not used for the sequential funnel; a symbol rejected at quality is never counted again at
timing.

The score/entry gates are evaluated by the same `evaluate_variant_entry` function used by the
backtester. The audit does not contain a second implementation of score thresholds or the
recovery rule.

## Data quality versus strategy rejection

Examples classified as data quality include missing/insufficient bar history, a missing price or
market cap, missing PIT fundamentals, unavailable score components, missing SMA20 inputs, and an
unresolved issuer identity. Examples classified as strategy rejection include low price,
insufficient liquidity, a small market cap, score threshold misses, the positive-OCF rule,
price below SMA20, and a recovery gate whose available triggers are all false.

Missing relative volume remains missing. It is never converted to zero. Reports separately show
`relative_volume_missing`, `relative_volume_below_threshold`, and
`relative_volume_above_threshold`.

The result classification is deterministic and documented in `classification_evidence`:

- A – Strategy Genuine: high coverage and fewer than 5% data-quality first failures.
- B – Data Limited: at least 50% data-quality first failures or PIT coverage below 50%.
- C – Mixed: intermediate data limitations.
- D – Bug / Pipeline Inconsistency: causal provenance or a mutually inconsistent gate is observed.

The label is a dataset diagnostic, not a recommendation to change a strategy.

## Point-in-time and lookahead rules

Historical market features use bars no later than the screening session. Fundamental features
use only SEC facts whose filing date is no later than that session. Each retained PIT sample
contains its screen date, latest filing date, and source period. A future filing or market bar is
reported as a pipeline inconsistency. Later sessions can change later audit rows only; audit data
never changes entries, position sizing, equity, or earlier funnel rows.

The fundamental coverage denominator is the number of symbol/session observations that actually
reach the PIT-fundamental stage. `valid`, `incomplete`, and `without` PIT fundamentals are kept
separate. Monthly metric coverage identifies missing growth, cash-flow, margin, ROIC, leverage,
relative-multiple, and FCF-yield inputs. Technical coverage reports SMA20, slope, RSI, momentum,
relative volume, and ATR independently.

## Recovery diagnostics and near misses

For candidates that reach recovery, reports retain:

- price above SMA20,
- RSI recovery,
- positive 5-session momentum,
- relative volume above the configured threshold,
- passes through each trigger and failures of all triggers.

Near misses are late-stage rejections (opportunity, timing, total, SMA20, recovery, or the
variant-specific D/E/F gate). The JSON and CSV contain score components, threshold distance,
all variant blocking reasons, and bounded technical evidence. D rows retain max drawdown,
63-session recovery, momentum126 and SMA200 distance; E/F rows retain their price, moving-average,
pullback and strength inputs. Only the configured top N per session is retained to keep the long
audit bounded.

## Reports

The report directory receives:

```text
candidate_audit_<variant>_<start>_<end>.json
candidate_audit_<variant>_<start>_<end>_sessions.csv
candidate_audit_<variant>_<start>_<end>_monthly.csv
candidate_audit_<variant>_<start>_<end>_failures.csv
candidate_audit_<variant>_<start>_<end>_near_misses.csv
candidate_audit_<variant>_<start>_<end>_candidates.csv
candidate_audit_<variant>_<start>_<end>_intraday_candidates.json
candidate_audit_<variant>_<start>_<end>_entry_symbols.json
candidate_audit_<variant>_<start>_<end>_near_miss_symbols.json
```

The failure CSV is monthly and includes both the raw count and rejection rate relative to the
incoming population of that rule. Candidate rows distinguish eligibility, portfolio blocking,
order creation, execution failure, and actual execution. The full-universe per-symbol rejection
matrix is intentionally not retained; aggregate first failures are complete, while detailed rows
are limited to economically relevant candidates and bounded near misses.

## Candidate export and intraday backfill

The `*_intraday_candidates.json` file contains every symbol that passed the complete configured
entry funnel at least once. It is directly consumable by the existing intraday sync:

```powershell
python -m trading_system.cli sync-intraday `
  --start 2025-04-21 `
  --end 2026-08-12 `
  --timeframes 15m `
  --candidates-report reports/candidate_audit_C_2025-04-21_2026-08-12_intraday_candidates.json
```

This sync is a separate, explicit step. Running the audit never downloads provider data.

## Known limits

- Historical membership uses the current tradable universe and therefore has survivorship bias.
- Daily OHLC event ordering remains ambiguous; the audit does not resolve it.
- PIT facts can be correctly available but still be insufficient to build a score component.
- Exact rejection details are retained only for candidates and bounded near misses; all first
  failure counts and coverage totals remain complete.
