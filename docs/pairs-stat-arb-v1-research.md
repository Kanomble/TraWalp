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

Only current local tradable `US_EQUITY` assets with local company identity, valid
SIC, and authoritative **operating-company common-stock** identity qualify.
Company/SIC identity alone does not establish that security type. SPY is reserved
exclusively for diagnostics, even if local company identity exists. No symbol
blacklist or name heuristic supplies the company boundary. SIC is
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

### Company security type: currently unavailable

**AUTHORITATIVE_COMPANY_SECURITY_TYPE_UNAVAILABLE.** The current local/provider
architecture does not supply an authoritative per-security distinction between
operating-company common stock and ETF/ETP/fund/warrant/unit/other instruments.
The source audit used repository code and the installed SDK; no network or
historical pair outcomes were consulted:

- The installed Alpaca `Asset` model exposes broad `asset_class`, exchange,
  tradability, margin/borrow/fractional flags and attributes. None establishes
  operating-company common-stock identity. `AlpacaDataClient.list_tradable_us_equities`
  persists the broad asset class and trading flags in `TradableAsset`/`assets`.
- The SEC identity/submissions path persists CIK, symbol, name, SIC and SIC
  description in `CompanyIdentity`/`companies`. Cached raw issuer submissions
  are not an authoritative mapping from each traded security to common stock;
  issuer identity or an issuer-level entity type cannot supply that mapping.
- No other persisted authoritative instrument classification or project-wide
  security-type classifier was found. Prices, fundamentals, names, symbols and
  SIC codes are not used to invent one.

The local adapter therefore returns `UNKNOWN` with no source. Preflight exports
per-symbol identity requirements, freezes no pairs for these unqualified symbols,
reports `company_only_universe_satisfied=false`, and remains unready. Validation
and the prepared-data simulation entry point refuse unresolved company identity.
This is a metadata blocker, not a Daily-bar fetch request.

A safe extension requires an authoritative per-security source, a reviewed
mapping to `COMMON_STOCK`/`ETF`/`ETP`/`FUND`/`WARRANT`/`UNIT`/`OTHER`/`UNKNOWN`,
and positive operating-company evidence for common stock. Persist stable
security/symbol/issuer linkage, source, source version/timestamp and classification
provenance in a migrated schema; integrate its local reader before manually
synchronizing that source. Only sourced `COMMON_STOCK` with
`operating_company=true` may enter. Unknown/missing/unsupported evidence fails
closed; positively identified non-company securities are excluded.
An existing Alpaca/SEC metadata sync alone cannot resolve this blocker. No
unsupported upstream field, schema migration with invented values, config
override, or manual category file has been introduced. Synthetic tests inject
explicitly marked evidence at the adapter boundary only.

### Coverage qualification (manifest schema 3)

Apply the preceding-close price gate first: a known close below the configured
minimum deterministically excludes the symbol without requesting ADV remediation.
Otherwise every previous-close/ADV20 input must exist. The earliest locally
observed Daily session **on or before T** distinguishes missing prefix from
internal gaps. A timestamp-only batch also checks observations before the
calibration window; an observation after T cannot establish earlier history.
Invalid/duplicate records remain unusable and are never filled.

Missing sessions before that first observation, or no observations yet, produce
nonblocking `INSUFFICIENT_SELECTION_HISTORY`; the symbol is not liquidity
eligible. This is not a claim about its actual listing date. Current names with
no bars do not generate fake fetch requests. Prefix diagnostics remain bounded
per-symbol summaries with first/last affected signal dates and counts (not an
assertion that the whole span is missing).

Missing required selection inputs on/after an established observation produce
`LOCAL_MISSING_FETCHABLE`, role `LIQUIDITY_SELECTION_INTERNAL_GAP`, scope
`VALIDATION_BLOCKING_REQUIREMENT`, and `validation_blocking=true`. This includes
internal holes during initial ADV warmup. The **entire SIC2/session** becomes
`SELECTION_MEMBERSHIP_UNRESOLVED`; no final names or pairs are frozen there.
Rank six cannot replace an unresolved member. Unrelated SIC2 groups continue.
`unavailable_liquidity_symbol_sessions` counts both prefix and internal cases.

`VALIDATION_BLOCKING_REQUIREMENT` covers frozen selected names' ADV/previous-close
windows, frozen pairs' calibration/observation windows, and eligible pairs'
possible five-session outcomes. A missing bar within an established required
history or a possible outcome window remains `LOCAL_MISSING_FETCHABLE` and blocks
readiness. A calibration prefix preceding any locally observed history is instead
`INSUFFICIENT_CALIBRATION_HISTORY`: the pair remains `INCOMPLETE_CALIBRATION`,
cannot signal, and nonexistent prefix history is not requested. No fill, replacement,
or future listing information is used. Readiness requires candidates, complete
authoritative security-type qualification, resolved selection membership and
zero unresolved blocking Daily inputs. Prefix diagnostics alone do not prevent
readiness. Correcting selection inputs requires a new preflight; an unresolved
manifest cannot authorize validation, even after diagnostics are edited and its
checksum recomputed.

All current-universe inputs still participate in the membership digest, including
missing markers. They are described separately as `SOURCE_FINGERPRINT_INPUT`,
which is not an automatic remediation requirement. The coverage CSV compresses
present requirements and insufficient calibration prefixes into contiguous official
session ranges, while preserving each missing required bar as an actionable row.
The requirements JSON contains compact `required_ranges`, unresolved-only
`required_sessions`, non-blocking diagnostics, unresolved SIC2/session membership,
company security-type requirements, and a compact fingerprint-scope
descriptor. It never enumerates the entire current-universe/history cross product.
An internal missing bar appears once by symbol/session with SIC2, its first
observation anchor and **all affected signal sessions**. Its requirement may
also carry other needed-input roles. Source fingerprints additionally bind
history anchors and qualified security-type evidence.

Runtime and manifest construction read the same frozen `PAIRS_STAT_ARB_V1`
definition, including numeric `adv_lookback_sessions=20`, `calibration_sessions=60`,
`min_correlation=0.70`, `entry_z=2.0`, `long_weight=short_weight=0.50`,
`slippage_bps=5.0`, `max_hold_sessions=5`, `top_n=5`, and `outcome_tail_sessions=5`.
No new parameter choices are exposed. Simulation also rejects a prepared manifest
whose definition differs from the current execution definition. Schema 3 binds
`selection_gap_semantics`, `selection_membership_fail_closed=true`,
`instrument_type_semantics`, and `company_only_requirement`. Schema 1/2 manifests
are rejected. The earlier historical preflight fingerprint
`f6985aad8b760cc654bce1c8f6220b6af16bfb78c01d93100f91738f1915a27d`
is obsolete for validation. This patch follows a historical preflight and precedes
any historical Pairs outcome evaluation; frozen signal/execution economics remain
unchanged. Do not reuse the old `_v1` report stem or manifest. The next manual
preflight uses `_v2` (the report suffix is distinct from manifest schema **3**):

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-pairs-stat-arb-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --output-stem pairs_stat_arb_v1_preflight_2024-01-02_2026-08-12_v2
```

Until the authoritative source extension is available, this command reports the
company-security-type blocker. Manually review the new preflight before considering
any validation; no historical run or synchronization was performed for this patch.

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
