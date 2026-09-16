# Pairs statistical arbitrage V1

The PAIRS-STAT-ARB-V1-DAILY hypothesis and all primary parameters were frozen before historical pair outcomes were evaluated.

Identity: `research-pairs-stat-arb-v1` / `PAIRS-STAT-ARB-V1-DAILY`, status `ACTIVE`.
This is independent Daily pair-trade research. F/configured/C1 remains reference-only.
There is no portfolio allocator, production selection entry, or automatic promotion.

## Frozen hypothesis

```text
Within highly liquid companies from the same SIC2 industry group,
pairs with strong recent Daily return correlation may temporarily
diverge in relative price.

An extreme positive relative-price deviation is expected to mean-revert:
short the relatively expensive member and long the relatively cheap member.

An extreme negative deviation is treated symmetrically.
```

## Frozen design

Only current local tradable `US_EQUITY` assets with local company identity and valid
SIC qualify. SPY is reserved exclusively for diagnostics, even if local company
identity exists. No ETF blacklist: company/SIC identity supplies the boundary. SIC is
normalized to four digits (three-digit codes receive a leading zero); SIC2 is its
first two digits. `CURRENT_UNIVERSE_ONLY`, `NOT_SURVIVORSHIP_CLEAN`, and
`CURRENT_LOCAL_SIC_NOT_PIT` apply throughout. SIC classification is current/local
and not guaranteed PIT-safe. Historical constituents are not reconstructed.

At signal close T, the preceding official session's close must meet
`config.universe.min_price`; ADV20 is the arithmetic mean of adjusted close times
volume over exactly 20 official sessions ending at T and must meet
`config.universe.min_avg_dollar_volume_20d`. No missing-session substitution.
Within each SIC2, retain five names by ADV20 descending, then symbol ascending.
Form all unordered pairs with symbol A lexicographically before symbol B.

Calibration uses exactly 60 official sessions strictly before T, with both closes
present on all 60 and T. Spread is `ln(close_A / close_B)`; use its arithmetic
mean and sample standard deviation (`ddof=1`). Reject zero variance. Pearson
correlation of the 59 consecutive log-return pairs must be at least 0.70.
T is excluded from every calibration statistic. Signal z is
`(ln(close_A_T / close_B_T) - mean) / sample_std`.

Exactly `z >= 2` shorts A and buys B; `z <= -2` buys A and shorts B.
Entry is both legs at the next official session open. Each leg has fixed 0.50
initial notional; total gross is 1.0, initial net dollars zero. No rebalancing or
beta fitting. Short return is `1 - cover / entry`, not an inverse price return.

Each holding close recalibrates on the preceding 60 sessions. Positive-entry z
crossing to `<= 0`, or negative-entry z crossing to `>= 0`, schedules both exits
at the next official open. Scheduled exits execute before any close processing.
Otherwise exit at holding session five's close. Entry session counts as one.
There is no stop, target, trailing stop, partial exit, or direction reversal.
Ignore active-pair signals; an independent signal at the exit session close may
re-enter next open. An unobservable outcome blocks that pair through its original
five-session horizon, since an earlier close cannot be established.

Every fill incurs adverse 5 bps: long entry/short cover multiply by 1.0005;
short entry/long exit by 0.9995. Commission and borrow fees are zero. Gross and net
pair returns each weight long and short returns by 0.50. MFE/MAE are extrema of
observed Daily-close mark-to-market PnL using fixed quantities implied by entry
fills, including entry slippage but no hypothetical exit slippage; no post-exit
close is sampled. Excursions are not clipped to zero.

Short availability, borrow availability, hard-to-borrow fees, locate fees,
broker execution eligibility, and German/EU retail execution eligibility are
NOT modeled. Positive research requires a separate execution-feasibility study.

## Reproducibility and data boundary

Preflight is outcome-clean: it freezes selected names, all pair-session
candidates, calibration coverage/statistics and the semantic contract. Signal
spread/z remain deferred. Discovery receives no post-signal-period prices. Tail
data is subsequently inspected only for bar coverage; no PnL or exit is evaluated.
The dedicated versioned `pairs_stat_arb_v1_manifest` binds all primary rules,
configured liquidity thresholds, official dates, feed/adjustment, current
identities, and a digest of selection-period local Daily input. Checksums provide
integrity, not cryptographic authorship. Explicit semantic checks reject changed
economics even after a checksum is recomputed. Source corrections affecting
selection require a new reviewed preflight; tail remediation can reuse membership.

Validation with a manifest never rediscovers membership. Rediscovery requires
`--rediscover-candidates`. Both commands use read-only SQLite, batch-load Daily
bars, and make no provider, SEC, synchronization or network calls. Simulation
accepts only prepared in-memory data and performs zero SQLite queries.
Local Daily rows have no per-row feed/adjustment provenance: matching configured
settings are bound and any conflicting local coverage metadata is rejected;
missing provenance is prominently reported rather than claimed verified.
Provider-verified ranges are descriptive only, not proof of a session's absence.

Signals are confined to the requested period. Five official sessions after its
end are outcome-only. Missing required tail observations yield
`OUTCOME_CENSORED`; missing in-period entry/exit data yields explicit
unobservable statuses. There is no delayed substitution, single-leg execution,
or fabricated final close. Incomplete outcomes never enter return metrics.

## Evidence and interpretation

Reports separate evaluations, signals, and completed trades. All returns are
per unit gross notional, not portfolio returns; overlapping pairs preclude claims
about portfolio CAGR, Sharpe, drawdown or equity. Diagnostic SPY returns use the
same entry open and exit open/close as each completed trade, never selection.
OLS regresses net pair return on SPY return; Spearman uses average ranks for ties.
Chronological thirds partition official signal sessions, preserving date order.
Diagnostic buckets are `2 <= |z| < 2.5`, `2.5 <= |z| <= 3`, `|z| > 3` and
`[.70,.80)`, `[.80,.90)`, `[.90,1]`. No bucket changes the strategy.
Pair net-PnL shares use signed total net PnL and can exceed 100% or be negative;
absolute-contribution shares are also supplied. Symbol contributions allocate
the actual weighted leg PnL, avoiding double counting. Profit factor is null
without losses. Missing diagnostics are identified explicitly.

Break-even adverse cost is analytic from fixed gross outcomes, without rerunning
signals: for mean long exit/entry ratio L and short exit/entry ratio S,
`bps = 10000 * (sqrt(L)-sqrt(S))/(sqrt(L)+sqrt(S))`. A negative value means even
zero adverse cost does not break even. Mean-reversion success means a crossing
observed on holding sessions 1–4, before the predetermined max-hold close.

Human review considers gross/net expectancy and PF, time stability, concentration,
mean-reversion success, cost drag and market beta. Overlapping trades are dependent;
these descriptive statistics do not establish independent-sample significance.
No parameter search, industry/name whitelist, alternative hedge weights, or cost
reruns belong to V1. A positive result does not promote Pairs V1 to champion,
paper trading, live trading or a combined F+Pairs portfolio.

Implementation verification uses synthetic data only. Historical preflight and
validation are separate manual research steps.
