# ORB V1 signal-level research

Family `research-orb-v1`, ID `ORB-V1-15M-LONG`, status **ACTIVE** pending a later human
research decision. The single definition is `research_definitions.py::ORB_V1`.
The independent family registry does not add a Daily StrategyVariant or a management
preset. F/configured/C1 remains the frozen champion. Results never promote ORB.

**Operational hold: `ORB_STOCK_CLASSIFICATION_SOURCE_UNAVAILABLE`.** The offline
security-scope audit below found no reliable individual-stock discriminator in the
existing sources. Runtime implementation stopped at that prerequisite, as required;
the current code does **not** enforce the intended stocks-only universe. The family
registry status above is unchanged and does not authorize a historical run. Do not
run ORB preflight, remediation or outcome validation until classification is resolved.

## Security-scope correction and source audit

The first historical preflight for **2024-01-02 through 2026-08-12** was discarded
before remediation because ETFs/funds were found in the candidate universe,
including SPY, QQQ, DIA, GLD, SLV, IBIT and ETHA. This is a methodology correction
before ORB outcome validation, not post-result strategy tuning.
**No ORB trade outcomes had been evaluated when the universe correction was made.**
The intended scope is corrected here; its runtime enforcement remains blocked.

The 2026-09-14 audit inspected repository code, the installed Alpaca SDK model,
the actual local SQLite schema and cached SEC submissions for the examples below.
It made no network/provider requests and read no historical bars or trade outcomes.

| Existing source | Finding |
| --- | --- |
| `alpaca.trading.models.Asset` / `AssetClass` | The installed model exposes `asset_class`, trading flags and optional `attributes`, but no explicit stock/ETF/fund/ETN product type. `US_EQUITY` includes the contaminated instruments. The documented attributes concern PTP tax treatment, not an affirmative common-stock classification. |
| `data/alpaca_client.py::list_tradable_us_equities()` and `data/sync.py::_sync_assets()` | Select active, tradable `US_EQUITY` assets and reconcile that snapshot without a security-type filter. |
| `models/market_data.py::TradableAsset` and SQLite `assets` | Store symbol, name, exchange, tradable/fractionable/shortable flags (plus the table's update timestamp). No authoritative security-type field is being dropped by this adapter. |
| `models/fundamentals.py::CompanyIdentity` and SQLite `companies` | CIK, symbol, name and issuer SIC metadata establish company identity/industry, not the listed instrument's product type. SEC identity does not imply individual stock. |
| Cached SEC `submissions` | Retain `entityType`, SIC and filer `category`, but `entityType="operating"` also occurs for commodity and crypto trusts. See the local counterexamples below. |
| Existing classification helpers | `classify_unmapped_asset()` uses names/symbols for sync diagnostics and is inadmissible for this filter. `is_financial_or_reit()` / `is_reit()` classify industry groups; no existing deterministic SEC pooled-vehicle security classifier was found. |

Actual retained SEC evidence (not runtime rules or a symbol blacklist):

| Symbols | Cached `entityType` | Cached SIC |
| --- | --- | --- |
| AAPL | `operating` | `3571` |
| MSFT | `operating` | `7372` |
| GLD, SLV, IBIT, ETHA | `operating` | `6221` (Commodity Contracts Brokers & Dealers) |
| QQQ | `investment` | Empty |
| SPY, DIA | `other` | Empty |

Thus `entityType="operating"` is demonstrably insufficient. Industry exclusions or
the absence of an investment flag cannot affirm that an instrument is common stock;
issuer metadata also cannot distinguish an issuer's equity from its other products.
No authoritative classification source was selected. Missing security evidence is
`UNKNOWN`, never implicit stock eligibility. No speculative metadata column, schema
migration, stock-universe helper or heuristic was added. Generic
`Database.list_tradable_companies()` retains its existing semantics.

These discarded artifacts **must NOT be reused or migrated**:

- `orb_v1_preflight_2024-01-02_2026-08-12_v1_orb_candidates.json`
- `orb_v1_preflight_2024-01-02_2026-08-12_v1_intraday_requirements.json`

Their files are retained unchanged as research records. This audit-only change does
not bump the runtime manifest version or mechanically invalidate their checksums.
Before any new run, the implementation must enforce the rule below, increment the
ORB compatibility version, and fingerprint authoritative security classification,
current tradable membership, company/identity basis, Daily selection inputs and the
stocks-only definition. Classification drift must produce
`ORB_CANDIDATE_MANIFEST_MISMATCH`, with no rediscovery fallback. Classification must
be loaded once per run before batched Daily selection, with stock/non-stock/unknown
and identity-conflict diagnostics. These implementation and regression checks remain
pending an authoritative source; the existing coverage/cache/spool paths are unchanged.

## Frozen hypothesis and execution

The intended universe is **`ORB_LIQUID_TOP100_STOCKS`**: current locally tradable
**individual equities only**, with SEC/company identity and no unresolved identity
conflict. Only explicit `COMMON_STOCK` / `OPERATING_EQUITY` classification is eligible.
ETF, ETN, closed/open-end fund, commodity trust, crypto ETF/trust, other pooled
vehicles and `UNKNOWN` are excluded. This rule is ORB-specific; it does not alter F
screening, ranking, configured management, historical backtests or other families.
The current runtime still uses `ORB_LIQUID_TOP100` and the generic company join;
it must not be presented as implementing this corrected definition.

The eventual summary and manifest must explicitly report:

```text
security_scope = INDIVIDUAL_STOCKS_ONLY
etfs_allowed = false
funds_allowed = false
unknown_security_type_allowed = false
```

For each eligible stock and requested XNYS session, require 20
consecutive valid completed Daily sessions ending at T-1, previous close at least
`universe.min_price` (existing configured value: $5), and mean(close × volume) at least
`universe.min_avg_dollar_volume_20d` (existing configured value: $10M). Sort that
dollar-volume mean descending, symbol ascending; select the first 100 stocks, or all
survivors if fewer qualify. Security filtering occurs **before** ADV ranking, so
excluding a raw rank-1 ETF can admit a stock formerly ranked 101. After the daily
stocks-only Top-100 is frozen, provider absence never admits stock rank 101.
No current snapshots, market cap, sector/SIC/REIT, F scores or technical gates enter
this selection. Missing Daily windows are counted explicitly; no stale close or
forward fill supplies a window. Preflight is not ready if a requested session has
no complete Daily windows. Zero survivors after valid liquidity checks is a valid
empty universe. Daily qualification details are in the summary.

Only provider-native 15m regular-session bars are admitted. The single 09:30–09:45
America/New_York bar defines the opening-range high and low. The first subsequent
completed bar with **close strictly above** that high consumes the sole daily signal
attempt. Intrabar highs and equality do not trigger. Decision time is bar completion;
entry is the next **actual native bar's open**, never the confirming close. No next
bar means `NO_ENTRY_NO_NEXT_BAR`. An entry open at or below the range low means
`INVALID_RISK_GEOMETRY`; there is no retry.

The stop stays at the opening-range low; initial R is entry reference minus that low.
From the entry bar onward, open <= stop exits at the actual open; otherwise low <=
stop exits at the stop. Stop precedence applies on the final bar too. With no stop,
exit at the final regular native bar's close. XNYS holidays, DST and official early
closes are respected. No overnight hold, target, trailing, delay filter, or second
attempt exists. Entry costs multiply reference by 1.0005; exit costs multiply it by
0.9995. Commission is zero. These constants have no CLI switches.

## Local qualification and remediation

Preflight discovers the Daily universe and exact full regular-session native 15m
requirements, without evaluating ORB outcomes or creating a native simulation spool.
It exports a summary, coverage CSV, sync-compatible `_intraday_requirements.json`,
and reusable `_orb_candidates.json`. Coverage uses existing durable
provider observations, matched to symbol, date, timeframe, configured feed,
adjustment, regular hours and the exact missing timestamps.

| Status | Local validation |
| --- | --- |
| `REQUIRED_PRESENT` | Simulate the complete native session |
| `PROVIDER_CONFIRMED_ABSENT` | Retain an unobservable event; never substitute rank 101 |
| `LOCAL_MISSING_FETCHABLE` | Refuse until explicit remediation qualifies it |
| `PROVIDER_CHECK_FAILED` | Refuse; inspect the recorded error |

Duplicate, off-grid or invalid OHLC rows also block qualification. No resampling,
synthetic bars, interpolation, Daily substitution or provider fallback occurs.
A partially present provider-absent session is wholly unobservable for this study;
using its partial outcomes would change the observation set. Coverage ratio is
fully present selected symbol-sessions divided by all selected symbol-sessions.
Coverage by symbol/month and provider absence counts remain visible in the summary.

The following is the exact fresh manual preflight command reserved for **after an
authoritative classifier and the stocks-only implementation are completed**. It is
not ready to run with the current code. Existing report files are refused before
discovery; use this new stem rather than either discarded artifact.

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-orb-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --output-stem orb_v1_stocks_preflight_2024-01-02_2026-08-12_v2
```

Do not run `sync-intraday` until that fresh stocks-only preflight has been reviewed.

ORB manifests explicitly require zero intraday warmup and regular hours; the sync
dispatch checks their feed/adjustment and 15m contract. Other candidate reports keep
their existing warmup behavior. Explicit extended-hours remediation is refused for
ORB. The configured feed is never changed. The remaining workflow documentation
describes existing mechanics, subject to the operational hold above. After review
and qualification, validation must consume the newly generated stocks-only manifest:

```powershell
python -m trading_system.cli validate-orb-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --candidate-manifest reports/orb_v1_stocks_preflight_2024-01-02_2026-08-12_v2_orb_candidates.json `
  --output-stem orb_v1_stocks_validation_2024-01-02_2026-08-12_v2
```

The candidate manifest is the authoritative bridge from preflight to validation.
It contains schema version, family/ID, exact dates, frozen strategy and universe
definitions, configured price/ADV thresholds, feed/adjustment, regular-hours flag,
candidate count, Daily qualification evidence, source fingerprint and integrity
fingerprint. Candidate rows contain only session, symbol, previous session, rank,
previous close and ADV20; **no intraday bars** are stored.

SHA-256 fingerprints use deterministic canonical JSON. Discovery hashes its input
rows as it processes them. Reuse verifies current tradable-company membership,
issuer mappings, identity conflicts/reference, calendar sessions and all relevant
Daily high/low/close/volume rows through T-1, including histories below the liquidity
thresholds or rank 100. This is a bounded Daily content check, **not** a repeat of
liquidity calculations/ranking. No call to `discover_orb_universe()` occurs.
Candidate content and Daily evidence are also integrity-checked and structurally
validated. These are reproducibility checksums, not signed attestations.

Any incompatible, stale or corrupt manifest fails with
`ORB_CANDIDATE_MANIFEST_MISMATCH`; there is no fallback rediscovery. Intraday bars
and provider coverage observations are deliberately outside the fingerprint so
remediation can add data. Coverage is always freshly qualified. Sync still consumes
only `_intraday_requirements.json`; it does not require the candidate manifest.
Preserve the manifest, configuration and local input snapshot for reproduction.
Manifest reuse does not resolve survivorship bias or make a period clean OOS.

Validation requires exactly one of `--candidate-manifest` or
`--rediscover-candidates`. The explicit research-only fallback is:

```powershell
python -m trading_system.cli validate-orb-v1 `
  --start YYYY-MM-DD `
  --end YYYY-MM-DD `
  --rediscover-candidates `
  --output-stem orb_v1_rediscovered
```

Rediscovery rebuilds the current local Top-100 and is less reproducible across
changing local datasets. It is never the default. Validation never auto-syncs.

## Reports and interpretation

Validation produces `<stem>_summary.json` and eight CSVs: `_orb_events`, `_trades`,
`_monthly`, `_yearly`, `_chronological_subperiods`, `_symbol_concentration`,
`_entry_time_buckets`, and `_coverage`. Each event is one terminal symbol-session
outcome; `signal_status=BREAKOUT_CONFIRMED` preserves confirmation on consumed
attempts. All requested decision, entry, stop, exit, cost and excursion fields are
present, with empty fields for unavailable observations.
Summary provenance includes `candidate_source` (`MANIFEST` or `REDISCOVERED`),
`candidate_manifest_path`, `candidate_manifest_fingerprint`, and `candidate_count`.

Returns are fractions, not percentages. Gross return is exit reference / entry
reference − 1; net return is exit fill / entry fill − 1. R return is net per-share
PnL / initial R. Expectancy is arithmetic mean net return. Aggregated PnL and profit
factor assume one unit of entry notional per independent trade; there is **no**
compounded equity curve, portfolio capacity, CAGR or portfolio Sharpe. Modeled cost
is gross-minus-net return drag (sum and mean), with per-share costs on events.
Profit factor is null without a negative-return denominator.

MFE and MAE use reference-price return excursions, clamped at zero. Native OHLC
cannot tell whether a stop-bar high preceded the stop; unknown stop-bar extrema
are excluded and only its known open/exit contribute. These are conservative
observed excursions. Intrabar stop timestamps use the bar's start as an explicitly
labelled estimate, so holding minutes are coarse and can be zero on an entry-bar
stop. Open fills and final bar completion timestamps are exact bar times.

Diagnostics group trades by calendar month/year, three contiguous thirds of the
requested trading sessions (including zero-trade days), symbol, and ET entry buckets
09:45–10:30, 10:30–12:00, 12:00–14:00, 14:00–15:45. Buckets are left-inclusive and
right-exclusive except the final bucket includes 15:45. They never alter trading
rules. Signed top-symbol PnL shares can exceed 100% when other symbols lose; shares
are null when total net PnL is nonpositive. Counts per day are included in summary
stability data. Empty groups retain valid headers and null unavailable statistics.
The existing eligible-session breakout percentage is unchanged. The additional
`percentage_of_observable_sessions_breaking_out` is 100 × breakout signals / fully
observable symbol-sessions, or null with no observable sessions. Provider-absent
sessions remain in the eligible denominator and are excluded from the observable
denominator. This diagnostic never affects signals or trade selection.

Daily reads use bounded symbol batches and rolling sums. Exact native requirements
use `Database.iter_native_session_batches`: one indexed regular-session native 15m
SELECT per batch of at most 200 pairs, plus the provider-observation lookup for
coverage. There is no unused previous-Daily-close SELECT. The existing entry
coverage loader remains unchanged for other research families. A bounded pure
calendar cache shares immutable expected timestamp tuples between coverage and
native validation, keyed by session/timeframe/extended-hours; it stores no prices
or research state. Duplicate/off-grid/OHLC checks remain intact.

Validation uses the run-owned `NativeEntrySessions` spool with a bounded LRU cache.
The simulation accepts no database/provider object and performs **zero SQLite
queries**. Reports retain all existing performance fields and add
`candidate_manifest_load_seconds` and `candidate_discovery_seconds`. Manifest runs
record zero discovery time; their measured load time includes source fingerprint
verification. Rediscovery records zero manifest-load time. Coverage query counts
exclude that separate source verification. Native bars loaded, cache peak, and
verification/preparation/simulation/diagnostics times remain reported; preflight
records zero for stages it does not execute. No historical speedup is claimed.
Shared backtest package exports are deferred so importing ORB/shared utilities
does not initialize the F engine; the existing exported objects are unchanged.

## Limitations

- Historical universe uses current locally stored tradable company membership.
  Results are not survivorship-clean (`CURRENT_UNIVERSE_ONLY`, `NOT_SURVIVORSHIP_CLEAN`).
- ORB V1 is a signal-level research study. No realistic multi-position
  capital-allocation policy has yet been defined. It is not production-ready.
- With configured IEX: `market_data_feed=IEX`, `consolidated_market_data=false`.
  IEX is a single-venue feed; intraday price/volume observations may differ from
  consolidated SIP. No feed comparison is part of V1.
- Local native rows are not versioned by feed provenance. The configured feed is
  reported, while old rows' origins cannot be independently certified by this schema.
  Daily adjustments likewise are provider-adjusted history, not corporate-action
  vintages. Missing Daily windows and provider absence can bias observable results.
- Research periods are not clean OOS unless explicitly established later. Neither
  this implementation nor its synthetic tests establish positive ORB expectancy.
- CC-01 remains OPEN / HIGH / PRE_PAPER_BLOCKER for the existing historical champion;
  its missing-Daily-bar policy was not changed. Paper/shadow execution remains a
  separate future engineering milestone.

Focused synthetic verification covers frozen ORB economics, manifest compatibility
and refusal paths, skipped discovery, native query plans/counts, bounded spool,
grid reuse and remediation compatibility. No real historical ORB preflight, sync,
validation, provider requests or network operations were run for this patch.

For the 2026-09-14 audit-only correction, 16 existing offline regression cases passed:
the frozen definition, T-1 Top-100/identity/no-absence-replacement behavior, incompatible
manifest rejection, same-session Daily exclusion from fingerprints, and indexed
native-only batched reads. These verify existing behavior, not stocks-only enforcement.
New classification, classification-drift and stock-filter-before-ranking regressions
remain blocked with the runtime implementation. No Python files changed, so Ruff and
the Python formatter were not applicable. The documentation diff passed whitespace
checks. The shared schema/models, F champion, ORB economics and all runtime paths
remain unchanged.
