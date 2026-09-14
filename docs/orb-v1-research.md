# ORB V1 signal-level research

Family `research-orb-v1`, ID `ORB-V1-15M-LONG`, status **ACTIVE** pending a later human
research decision. The single definition is `research_definitions.py::ORB_V1`.
The independent family registry does not add a Daily StrategyVariant or a management
preset. F/configured/C1 remains the frozen champion. Results never promote ORB.

## Finalized research universe and methodology

ORB V1 studies long-only 15-minute Opening Range Breakouts over the Top-100
most liquid currently tradable Alpaca US_EQUITY instruments.
The scope is **`CURRENT_ALPACA_TRADABLE_US_EQUITY`**, named
**`ORB_LIQUID_TOP100_US_EQUITY`** in the frozen definition.

Direct crypto assets are excluded. ETFs/ETPs and other instruments classified by
Alpaca as US_EQUITY may be included, including commodity-linked trusts/products
and crypto-linked exchange-traded products. There is no common-stock-only claim,
security master, symbol/name blacklist or SEC/CIK/SIC/REIT/sector eligibility gate.
An instrument needs no SEC company identity, and SEC identity conflicts do not
change ORB membership. Generic SEC workflows and F identity guards are unchanged.

German/EU broker execution eligibility is not evaluated in this research family.
**Alpaca tradable=true is a data/research universe property and does not prove
that the instrument can be purchased by the user through a German/EU broker.**
A future execution-universe layer would address that separately. US ETFs are not
excluded based on possible restrictions at a European broker.

The first historical preflight for **2024-01-02 through 2026-08-12** exposed the
ambiguity of the former SEC/company-based universe when ETFs/funds appeared in it.
It was discarded before remediation. An offline audit found that the installed
Alpaca `Asset` model supplies `asset_class`, but the local `TradableAsset` and
SQLite `assets` table did not persist it. The model does not supply a reliable
common-stock discriminator; cached SEC `entityType="operating"` also describes
GLD, SLV, IBIT and ETHA. The finalized study deliberately uses Alpaca's broader
asset class. The prior common-stock classification hold is removed; both local
ORB CLI commands are available under this definition, with normal data checks.

**No ORB trade outcomes had been evaluated before this universe definition was finalized.**
This is a pre-outcome methodology clarification, not post-result strategy tuning.

These previous artifacts **must NOT be reused, migrated or used for sync**:

- `orb_v1_preflight_2024-01-02_2026-08-12_v1_orb_candidates.json`
- `orb_v1_preflight_2024-01-02_2026-08-12_v1_intraday_requirements.json`

They predate the final research-universe definition. Their files are retained as
research records. Candidate manifest version **2** rejects version 1 with
`ORB_CANDIDATE_MANIFEST_MISMATCH`, even if its integrity hash is recomputed.
A fresh preflight with a new stem is required; the old requirements must not be
used for remediation.

## Asset-class persistence and local scope

`TradableAsset.asset_class` now persists the provider's enum/string as uppercase
text. `US_EQUITY` is eligible; `CRYPTO`, `UNKNOWN` and all other asset classes are
excluded before reading Daily history. Missing/blank metadata becomes `UNKNOWN`;
no symbol, name, company identity or provider request filter fabricates a class.
The existing explicit asset sync requests active `US_EQUITY`, retains tradable
assets and reconciles current membership. It now stores the actual response class.

Normal database initialization adds `assets.asset_class TEXT NOT NULL DEFAULT
'UNKNOWN'` to legacy schemas without changing existing asset flags or company
rows. The migration is idempotent. Old model callers remain valid and default to
`UNKNOWN`. Even a read-only preflight against a pre-migration database reads legacy
rows as `UNKNOWN` without altering the schema. Legacy rows remain excluded from
ORB until the normal explicit asset sync refreshes their metadata. No migration
or provider refresh occurs automatically in preflight or validation. An unknown
class is a missing metadata condition, not a common-stock classification hold.

ORB loads `Database.list_tradable_assets()` once per run and filters on stored
`tradable` and `asset_class == US_EQUITY`. The generic asset list and
`Database.list_tradable_companies()` retain their membership semantics. F screening,
ranking, configured management, SEC guards and other research families are unchanged.

## Frozen hypothesis and execution

For each eligible instrument and requested XNYS session, require 20 consecutive
valid completed Daily sessions ending at T-1, previous close at least
`universe.min_price` (existing configured value: $5), and mean(close x volume) at
least `universe.min_avg_dollar_volume_20d` (existing configured value: $10M).
Sort ADV20 descending, symbol ascending; select the first 100, or all survivors if
fewer qualify. Class filtering precedes ranking: crypto/unknown instruments never
consume a rank. After Top-100 selection, provider absence never admits rank 101.
No current snapshots, T Daily bar, future volume, market cap, F scores or technical
gates enter selection. Missing windows are counted; no stale close or forward fill
supplies a window. Preflight is not ready if no eligible instruments have complete
Daily windows for a requested session. Zero survivors after valid liquidity checks
is a valid empty universe. All trading economics below remain frozen.

The summary and candidate manifest report:

```text
security_scope = ALPACA_TRADABLE_US_EQUITY
individual_stocks_only = false
etfs_etps_may_be_included = true
direct_crypto_allowed = false
unknown_asset_class_allowed = false
execution_eligibility_germany = NOT_EVALUATED
membership = CURRENT_UNIVERSE_ONLY
survivorship = NOT_SURVIVORSHIP_CLEAN
```

`daily_qualification` includes `tradable_assets_total`,
`us_equity_assets_considered`, `crypto_assets_excluded`,
`unknown_asset_class_excluded`, and a compact breakdown by asset class. Each
session reports `eligible_after_daily_window`, `liquid_survivors` and `selected`.

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

The V2 preflight is retained as diagnostic evidence: it considered 13,419 current
US_EQUITY assets and selected 65,500 instrument-sessions, while only roughly
4,800-5,900 assets per historical session had complete Daily windows. The previous
Daily store primarily served the narrower company universe. Before intraday
remediation, request Daily history for the complete current asset scope.

The next **manual** step is the existing incremental Daily-history pipeline with
its explicit local US-equity selector. December 1 supplies the 20-session Daily
warmup before the requested January 2, 2024 research start:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli sync-daily-history `
  --start 2023-12-01 `
  --end 2026-08-12 `
  --universe us-equity
```

The default `sync-daily-history` scope remains companies; `--universe companies`
is equivalent, and `--symbols` remains available separately. The new scope reads
local tradable US_EQUITY metadata once without SEC membership or identity guards.
It never runs `sync-assets` automatically. Legacy UNKNOWN rows need an explicit
normal asset sync before they can be selected.

Do not expect all current instruments to have history back to December 2023.
Later IPOs/listings and provider history limits legitimately leave missing windows.
The goal is a provider-history request (or an existing successful verification)
for every eligible symbol over the interval, not `unavailable_daily_windows = 0`.
Review request errors and completion; a failed request does not establish absence.
The first unverified interval is requested in full by the existing incremental
semantics, so `--full-window` is not needed for this workflow.

After Daily sync completes, generate a fresh preflight with the new V3 stem:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli preflight-orb-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --output-stem orb_v1_us_equity_preflight_2024-01-02_2026-08-12_v3
```

Do not run `sync-intraday` until this V3 preflight has been reviewed. The V2 artifacts
remain diagnostic evidence but must not drive intraday remediation after Daily
coverage is expanded. Do not migrate them. V3 is a fresh artifact stem, not a change
to candidate manifest version 2, ORB selection rules or trading economics. These
manual commands were not run as part of this enhancement.

ORB manifests explicitly require zero intraday warmup and regular hours; the sync
dispatch checks their feed/adjustment and 15m contract. Other candidate reports keep
their existing warmup behavior. Explicit extended-hours remediation is refused for
ORB. The configured feed is never changed. After the fresh preflight is reviewed
and its native coverage qualified, validation consumes the new US_EQUITY manifest:

```powershell
python -m trading_system.cli validate-orb-v1 `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --candidate-manifest reports/orb_v1_us_equity_preflight_2024-01-02_2026-08-12_v3_orb_candidates.json `
  --output-stem orb_v1_us_equity_validation_2024-01-02_2026-08-12_v3
```

The candidate manifest is the authoritative bridge from preflight to validation.
It contains schema version, family/ID, exact dates, frozen strategy and universe
definitions, configured price/ADV thresholds, feed/adjustment, regular-hours flag,
candidate count, Daily qualification evidence, source fingerprint and integrity
fingerprint. Candidate rows contain only session, symbol, previous session, rank,
previous close and ADV20; **no intraday bars** are stored.

SHA-256 fingerprints use deterministic canonical JSON. Discovery hashes its input
rows as it processes them. Reuse verifies the finalized universe definition,
current tradable asset membership, stored class/tradable values, calendar sessions
and all relevant
Daily high/low/close/volume rows through T-1, including histories below the liquidity
thresholds or rank 100. Class values of excluded tradable assets also participate,
so a class change fails closed. SEC identity and instrument names are not selection
or fingerprint inputs. This is a bounded Daily content check, **not** a repeat of
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

Asset membership/class is loaded once per run before selection; no per-symbol-
session metadata lookups occur. Daily reads use bounded symbol batches and rolling sums. Exact native requirements
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

- Historical universe uses current locally stored Alpaca tradable US_EQUITY membership.
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

Focused scope regressions additionally cover asset-class persistence and legacy
migration, explicit sync response metadata, generic/F isolation, US_EQUITY inclusion
without SEC identity, name-independent ETF/ETP treatment, crypto/unknown exclusion
before ranking, single asset loads across sessions, class-drift fingerprints and
version-1 refusal. No real historical job or provider request is part of these tests.
