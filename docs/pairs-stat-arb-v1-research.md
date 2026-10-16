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

Only current local tradable non-OTC `US_EQUITY` assets with local company identity,
valid/nonzero SEC CIK and SIC, and Massive **COMMON_STOCK** classification without
local/Massive or cached SEC/local CIK conflict qualify.
Company/SIC identity alone does not establish that security type. SPY is reserved
exclusively for diagnostics, even if local company identity exists. No symbol
blacklist or name heuristic supplies the company boundary. SIC is
normalized to four digits (three-digit codes receive a leading zero); SIC2 is its
first two digits. `CURRENT_UNIVERSE_ONLY`, `NOT_SURVIVORSHIP_CLEAN`,
`CURRENT_REFERENCE_SECURITY_TYPE_NOT_PIT`, and
`CURRENT_LOCAL_SIC_NOT_PIT` apply throughout. SIC classification is current/local
and not guaranteed PIT-safe. Historical constituents are not reconstructed.

### Identity-qualified research universe

Pairs uses the local tradable universe intersected with eligible security type
and **identity-qualified securities**. Qualification is a run-level data-quality
gate before SIC2 grouping, ADV20, Top-5, pair generation and calibration.
`IDENTITY_QUARANTINED` excludes a security for the entire run: historical bars
cannot safely be assigned to the intended security. It makes no claim that the
company or ticker is erroneous and uses no returns, correlation, trade outcomes,
price/volume continuity or PnL.

The symbol-independent triggers are unresolved persisted epoch evidence,
persisted lifecycle/name-change evidence without a verified current-security
interval, or a disconnected local history of at least **365 calendar days**
combined with a cached current-CIK SEC `8-A12B` registration inside that gap.
The last combination requires identity verification; neither dates nor bars
establish same/different security or effective interval boundaries. A gap alone,
registration alone, missing FIGI, or absent epoch row is not a quarantine trigger.
Normal current securities without a warning retain
`CURRENT_SECURITY_NO_IDENTITY_WARNING`; this explicitly does **not** mean
historically verified. Their prefixes remain nonblocking warmup diagnostics;
internal missing inputs still block the affected SIC2/session.

A valid verified interval takes precedence over unverified suspicion and remains
strictly enforced for selection, calibration and outcomes. Outside it the security
is a nonmember. Competing verified assertions that the existing interval resolver
cannot reconcile produce `IDENTITY_EVIDENCE_CONFLICT` and run-global quarantine;
no provider is chosen by plausibility. Invalid evidence or a failed qualification
read/execution remains blocking (`IDENTITY_QUALIFICATION_FAILED`, ready=false).

Manifest V7 binds the policy, warning threshold, per-symbol status/reason and
evidence fingerprints, including excluded symbols. Preflight exports eligible,
quarantined, verified-outside-run and unresolved-blocking counts and per-symbol
`identity_status`, `reason`, `evidence_source`, `run_excluded`. Only persistent
local identity, lifecycle, SEC cache and timestamp data are used; research report
files and provider raw bodies are not runtime inputs/report contents. No provider
requests, epoch creation, bar deletion or issuer migration occur.

This policy addresses unsafe unresolved ticker/security histories. It does not
make Pairs fully PIT-clean or survivorship-bias-free: `CURRENT_UNIVERSE_ONLY`,
current security type and current SIC limitations remain. All strategy economics
below are unchanged.

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

### Pre-outcome universe/identity definition refinement

Pairs V1 has not inspected historical validation outcomes. The original definition
did not contain an OTC exclusion. This final eligibility refinement was made before
first Pairs outcome validation, not as performance tuning or a response to returns.
User-provided local reference diagnostics exposed a deterministic market-scope
mismatch: all 22 remaining company reference gaps had persisted Alpaca exchange
`OTC`. Pairs is a highly liquid long/short US-equity research family; OTC execution,
borrow and reference semantics differ from the primary-listed universe.

The new frozen base gate requires a local asset, tradable=true, US_EQUITY, not SPY,
normalized persisted exchange != OTC, local SEC CompanyIdentity, valid/nonzero CIK
and valid/nonzero SIC. Exchange comparison strips whitespace and uppercases;
missing/other exchange values are not inferred from names, suffixes or ADR wording.
OTC symbols have no Massive requirement, blocker, ranking membership or pair.

Common-stock identity disagreement creates an explicit, nonblocking symbol
quarantine, `EXCLUDED_IDENTITY_CONFLICT`, before historical liquidity ranking.
Ten-digit local company, cached current SEC (`sec_reference / ticker_to_cik` in
sync_state) and Massive CIKs classify its provenance:

- local == current SEC and Massive differs: `MASSIVE_REFERENCE_CIK_CONFLICT`;
- current SEC == Massive and local differs: `LOCAL_IDENTITY_STALE_OR_TICKER_REUSE`;
- any other local/Massive or local/current SEC conflict: `UNRESOLVED_CROSS_SOURCE_IDENTITY`.

Absent Massive CIK is accepted only without cached SEC/local conflict. No source
is automatically trusted to migrate an issuer. Companies, historical bars and
fundamentals remain untouched; the general SEC conflicting-update guard remains
unchanged. A future symbol/issuer lifecycle model is separate from this safe
eligibility boundary. Quarantine is an explicit static exclusion, not silent
rank-six replacement for unresolved Daily inputs. All trading economics below
and the ADV20 integrity rules remain unchanged.

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

### Authoritative common-stock reference boundary

Massive is used only for security classification, with a Stocks Basic Free
contract, a hard maximum of **5 requests in any rolling 60 seconds**, and exactly
`GET /v3/reference/tickers` and `GET /v3/reference/tickers/types`. Paid features,
market-data downloads, billing, subscriptions and upgrades are never used.
Provider pricing/access may change externally; access errors fail closed without
switching endpoints or selecting a paid feature. Alpaca supplies prices; SEC
remains canonical for issuer identity and fundamentals.
The persistent timing budget and OS lock coordinate local TraWalp processes and
rapid restarts; requests made by external software or other computers are outside
TraWalp's control.

The company-only requirement is `COMMON_STOCK_WITH_SEC_ISSUER_IDENTITY`:
first require a local tradable non-OTC US_EQUITY asset other than SPY, local SEC
CompanyIdentity with a valid CIK and valid/nonzero SIC. Assets failing that frozen
base-company gate are outside Pairs; they create no Massive-reference blockers.
Only inside this scope require Massive canonical `COMMON_STOCK` without a
local/Massive or cached SEC/local CIK conflict. Missing
Massive CIK is accepted with valid SEC identity/SIC and no cached SEC conflict;
SEC CIK is never overwritten. This corrects the unsupported operating-company
metadata requirement without changing Pairs economics.

Reviewed mapping `MASSIVE_STOCK_TYPES_V2` accepts exact catalog entries
`ETS / Single-security ETF -> ETF`, `ETV / Exchange Traded Vehicle -> ETP`,
`RIGHT / Rights -> RIGHT`, `SP / Structured Product -> STRUCTURED_PRODUCT`.
Wrong descriptions/new codes remain UNKNOWN. An existing current V2 snapshot
does not need a resync for this eligibility refinement.
Known ETF/ETP/fund/preferred/ADR/warrant/unit/right/structured-product/bond/other
types inside the base scope are excluded nonblocking before the provider CIK
eligibility check, even if their diagnostic CIK cross-check mismatches.
Missing reference rows and unknown/new type codes produce blocking
`SECURITY_TYPE_REFERENCE_MISSING` and `SECURITY_TYPE_UNKNOWN` only for symbols
passing the non-OTC local base-company gate. CIK conflicts instead produce
nonblocking `EXCLUDED_IDENTITY_CONFLICT` with explicit provenance and quarantine.
Static identity is qualified before historical ranking: unresolved
metadata freezes no misleading replacement universe and preflight stays unready.
Neither preflight nor validation repairs metadata or invokes Massive, Alpaca or SEC.

The new reference layer stores canonical and raw provider types, identifiers,
source/version/provider timestamp and atomic snapshot/catalog fingerprints.
Current classifications carry `CURRENT_REFERENCE_SECURITY_TYPE_NOT_PIT` alongside
`CURRENT_UNIVERSE_ONLY` and `NOT_SURVIVORSHIP_CLEAN`; no historical PIT identity is
claimed. See [security-reference.md](security-reference.md) for the limiter proof,
credential protection, mapping, storage, access-error policy and manual workflow.

### Coverage qualification (manifest schema 5)


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
whose definition differs from the current execution definition. Schema 5 binds
security-reference source/semantics/fingerprint, catalog fingerprint, mapping
version, CIK conflict policy,
`company_universe_scope=LOCAL_TRADABLE_NON_OTC_US_EQUITY_WITH_VALID_SEC_COMPANY_CIK_SIC`,
`identity_conflict_policy=SYMBOL_QUARANTINE_NONBLOCKING`,
`identity_conflict_classification_version=LOCAL_CACHED_SEC_MASSIVE_CIK_V1`,
`identity_qualification_fingerprint`,
`selection_gap_semantics`, `selection_membership_fail_closed=true`,
`instrument_type_semantics`, and `company_only_requirement`. Schema 1/2/3/4 manifests
are rejected. The earlier historical preflight fingerprint
`f6985aad8b760cc654bce1c8f6220b6af16bfb78c01d93100f91738f1915a27d`
is obsolete for validation. This patch follows a historical preflight and precedes
any historical Pairs outcome evaluation; frozen signal/execution economics remain
unchanged. The reference membership fingerprint covers every non-OTC base-company
symbol, including common stocks, missing reference markers, known non-common
exclusions and quarantined identities, and omits outside-scope assets. Identity
qualification binds symbol, normalized local/current SEC/Massive CIKs, conflict
class, quarantine status, local exchange, SIC, eligibility and blocking status.
Resolving or introducing a conflict invalidates the frozen manifest. Changing an
OTC reference/cache row is irrelevant; changing its asset exchange to non-OTC can
add it to the scope and invalidates the manifest. All V4 manifests are rejected.
Do not reuse old manifests. Preserve existing report stems; an
already-used `_v2` stem also refuses overwrite, so choose a new unused stem
manually if needed. **Do not run historical preflight now.** First review the patch,
manually run only the read-only security-reference audit. No Massive resync is
needed if the active snapshot is V2/current. The user-provided audit output must
be reviewed before considering the following preflight command.
For that later reviewed workflow, the next manual
preflight uses `_v2` (the report suffix is distinct from manifest schema **5**):

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-pairs-stat-arb-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --output-stem pairs_stat_arb_v1_preflight_2024-01-02_2026-08-12_v2
```

The local audit exposes OTC exclusions, quarantine reasons and any remaining
non-OTC missing/unknown blockers separately from global coverage. No historical
run or synchronization was performed for this patch. Any later preflight and
validation remain separate user decisions after that audit review.

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
