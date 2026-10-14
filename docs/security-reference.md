# Security reference data

TraWalp's Massive reference integration is designed for **Stocks Basic Free**.
Alpaca remains the market-price/data source; SEC remains canonical for issuer
identity and fundamentals. Massive supplies current per-security classification
only. It supplies no prices, bars, trades, quotes, snapshots, indicators,
fundamentals, news, corporate actions, flat files, WebSockets or partner data.

## Provider contract

```text
provider = MASSIVE
required_access_tier = STOCKS_BASIC_FREE_COMPATIBLE
paid_features_allowed = false
request_limit_per_minute = 5
```

The exact endpoint allowlist is:

```text
GET /v3/reference/tickers
GET /v3/reference/tickers/types
```

Both endpoints are documented as included in all Stocks plans, including Basic
Free: [All Tickers](https://massive.com/docs/rest/stocks/tickers/all-tickers) and
[Ticker Types](https://massive.com/docs/rest/stocks/tickers/ticker-types).
The ticker page limit is the documented maximum, **1000**. Bulk ticker requests
use `market=stocks`, `active=true`, `sort=ticker`, `order=asc`; no date, individual
ticker, or other market is requested. The current ticker query schema does not
document a `locale` filter. Every returned row must have `market=stocks`,
`locale=us`, `active=true`, or the snapshot fails closed. Types use documented
`asset_class=stocks&locale=us`. Documentation was reviewed on 2026-10-14 without
making any API request.

TraWalp cannot verify an account's billing state. Compatibility means it calls
only these documented Free-compatible endpoints. Provider pricing/access may
change externally. Any access error fails closed; it never selects a paid feature.
There is **no billing/subscription integration**, checkout, Stripe, trial
activation, plan mutation, credit-card workflow, account upgrade or purchase code.

## Rolling request limit

The hard maximum is **5 TraWalp-controlled requests in any rolling 60 seconds**.
One persistent budget protects all local reference-sync processes, clients,
types, pages and retries, including rapid command restarts. The module-level
limiter is now a front end to shared timing state and an OS-level exclusive lock.
No process-local fallback exists.

The fixed state location is independent of the API key, working directory and
checkout, with no CLI/env/debug path override:

- Windows: `~/AppData/Local/TraWalp/state/massive-reference-rate-limit.json`
  (`~` is the Windows user's home directory).
- POSIX: `~/.local/state/TraWalp/state/massive-reference-rate-limit.json`.
- Both use a separate `massive-reference-rate-limit.lock` in the same directory.

All invocations in the local TraWalp user environment share this location. State
contains only at most five paired wall/monotonic timing observations, the last
clock observation and an in-flight timing flag. The lock file contains one
initialization byte. Neither filename nor contents contain an API key, API
response, URL, ticker or account identifier. Files are created only when a
request is explicitly attempted; import, audit and Pairs commands do not create them.

Windows uses `msvcrt.locking` on byte zero; POSIX uses exclusive `fcntl.flock`.
A thread lock also coordinates callers in a process. The OS lock remains held
through budget waiting, reservation, HTTP transport and completion. Handle
closure releases it after normal completion, exceptions or KeyboardInterrupt;
the OS releases locks when a process terminates. The lock file is never replaced.
These are standard-library facilities, with no new locking dependency.

Before every attempt:

1. Acquire the OS lock and read the persistent timing history.
2. Validate both clocks before expiring anything. If the previous owner left an
   in-flight reservation, conservatively advance its timestamp to now and persist
   it: the acquired lock proves that owner no longer controls transport.
3. Expire entries only once **both wall and monotonic ages reach 60.5 seconds**.
4. If five entries remain, wait until the oldest safely expires. Recheck both
   clocks after sleeping. Also honor any monotonic retry deadline.
5. Atomically persist a reservation and in-flight flag **before transport**.
6. In `finally`, advance its timestamp to completion and clear the flag. Both
   writes flush/fsync a temporary file before atomic replacement under the
   separate OS lock. Release the lock only after completion handling.

Failed attempts consume the same durable slots. If reservation persistence
fails, transport is never entered. If completion is interrupted or cannot be
written, the previous durable in-flight reservation survives for the next owner.
Missing initialized state, corrupt state and storage errors fail closed with
`MASSIVE_RATE_LIMIT_STATE_UNAVAILABLE` / `MASSIVE_RATE_LIMIT_STATE_INVALID`;
they never silently reset the budget. Interruption before replacement preserves
the previous complete JSON document.

For any six actual transmissions, the sixth cannot start until at least 60.5
seconds after the first's completion, which is no earlier than its transmission.
The OS lock prevents two processes from checking/reserving the same capacity;
each new owner loads the prior owner's committed history. The sixth therefore
cannot fit inside a rolling 60-second interval even when earlier owners exit,
or when processes start simultaneously. Pagination remains sequential, without
fan-out or worker pools.

Persisted wall time is conservative epoch time; monotonic readings are additionally
used to guard elapsed time. Python >=3.11 uses the same monotonic clock across
processes on a boot, including Windows. See the standard-library
[time](https://docs.python.org/3.11/library/time.html#time.monotonic),
[Windows locking](https://docs.python.org/3.11/library/msvcrt.html#msvcrt.locking)
and [POSIX locking](https://docs.python.org/3.11/library/fcntl.html#fcntl.flock) docs.
Rollback, future clock observations, monotonic resets or a wall/monotonic elapsed
discrepancy over 0.5 seconds raise **MASSIVE_RATE_LIMIT_CLOCK_ANOMALY** without
discarding history or starting transport. Small adjustments cannot shorten the
window because both ages must reach 60.5 seconds. Suspend/reboot/clock corrections
may conservatively trigger this error; there is no automatic state reset. Do not
delete/reset state while any local sync process may be active.

Rates of 1 through 5 may be configured programmatically; all other values,
including 6, 100 and `unlimited`, raise `MASSIVE_RATE_LIMIT_EXCEEDS_FREE_TIER`.
There is no CLI/env/debug override. This controls TraWalp's own local sync
processes; it cannot control another application, Massive dashboard tooling,
manual curl requests or requests from another computer using the same key.
It is not an account-wide guarantee against external API consumption.

Requests' automatic retries and redirects are disabled. Every manual retry
consumes a slot. At most three retries follow the original attempt:

| Failure | Behavior |
| --- | --- |
| 401 | `MASSIVE_AUTH_FAILED`; immediate stop, no retry/fallback |
| 402 | `MASSIVE_PAYMENT_OR_PLAN_REQUIRED`; immediate stop, no retry/fallback |
| 403 | `MASSIVE_REFERENCE_ACCESS_DENIED`; immediate stop, no retry/fallback |
| 429 | Honor numeric or HTTP-date `Retry-After`; exponential backoff and shared limiter; bounded retries |
| 5xx/network exception | Bounded exponential backoff (2, 4, 8 seconds), shared limiter |
| Other HTTP/invalid response/unsafe pagination | Stop; no alternate endpoint |

Retry exhaustion has explicit rate/server/network statuses. Request counters
include failed transmissions and retries. The private URL gate rejects any
unapproved path, different origin, credential-bearing authority, redirect or
unexpected query scope. Pagination must remain on the exact ticker-list path;
even `/v3/reference/tickers/AAPL` is forbidden.

## Credentials and explicit commands

The project's existing `load_settings` dotenv/environment mechanism loads `.env`.
Only the explicit reference-sync command reads `MASSIVE_API_KEY` and constructs
`MassiveReferenceClient`. Missing credentials produce `MASSIVE_API_KEY_MISSING`.
Unrelated operations require no Massive key. The key is excluded from Settings,
strategy YAML, SQLite and reports. Supported
[Bearer authentication](https://www.massive.com/docs/rest/quickstart) keeps it
out of request URLs. Returned pagination `apiKey` parameters are removed before
transport. Exception/retry diagnostics contain fixed statuses, never provider
bodies, exception URLs or headers. The integration emits no HTTP trace or secret
logging and introduces no SDK dependency; it uses existing `requests`.

Only when a reference refresh is separately needed, the user manually runs:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli sync-security-reference
```

The compact JSON summary includes snapshot ID/reference fingerprint, catalog
fingerprint/mapping version, all canonical type counts, unknown raw code counts,
provider/local coverage, missing local references, SEC CIK states, eligible
Pairs common stocks, request/retry counts, configured cap, elapsed time,
`paid_features_used=false` and `allowed_endpoints_only=true`. It never dumps the
full ticker list. Successful metadata and the normalized type catalog are stored
with the snapshot. The type catalog is fetched once per run, with bounded retries
if needed, never per page/symbol.

An optional local audit performs **0 Massive requests** and no database writes:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli audit-security-reference
```

It reports snapshot age, distribution, unknown codes, coverage, missing references
and CIK conflicts. `screen`, `backtest`, normal sync, refresh-market, update-bars,
Pairs preflight and Pairs validation never invoke this reference sync.

## Storage and identity

Idempotent `Database.initialize` adds `security_reference`,
`security_reference_snapshots` and `security_reference_active`. Existing assets,
companies, bars and fundamental facts retain their meanings and data. Legacy
read-only databases without these tables report missing references without
migration. Existing F screening/backtesting needs no Massive reference data.

The sync fetches all pages into memory, validates/maps them, computes SHA-256
fingerprints, then commits reference rows, successful snapshot metadata and the
active pointer in one SQLite transaction. No page writes active data. Any fetch,
mapping, database error or interruption preserves the last successful snapshot.
There is no checkpoint/resume mode or background worker. Restarting the explicit
command starts a fresh complete run.

Rows retain symbol, canonical/raw provider type, source/version, market, locale,
active status, primary exchange, provider CIK, composite/share-class FIGI,
provider update timestamp, observed timestamp, raw fingerprint and snapshot ID.
Only reference fields are stored; provider response bodies and pagination URLs
are not persisted. Snapshot ID includes observation time; the reference digest
excludes observation time/ID so identical classification does not invalidate
membership merely because it was observed again.

The reviewed `MASSIVE_STOCK_TYPES_V2` mapping validates codes **and exact
descriptions against the fetched US Stocks catalog**. `CS` / `Common Stock`
maps to `COMMON_STOCK`; ETF, PFD, ADRC/ADRP/ADRR/ADRW, WARRANT, UNIT, RIGHT,
ETP, ETN, FUND, BOND and OTHER map only when their expected descriptions are
present. The canonical model contains `COMMON_STOCK`, `PREFERRED_STOCK`, `ETF`,
`ETP`, `ETN`, `FUND`, `ADR`, `WARRANT`, `UNIT`, `RIGHT`, `STRUCTURED_PRODUCT`,
`BOND`, `OTHER`, `UNKNOWN`. The V2 corrections use the exact catalog entries
supplied from the user's authoritative stored Massive Stocks US catalog:

| Provider code | Required exact description | Canonical type |
| --- | --- | --- |
| ETS | Single-security ETF | ETF |
| ETV | Exchange Traded Vehicle | ETP |
| RIGHT | Rights | RIGHT |
| SP | Structured Product | STRUCTURED_PRODUCT |

The old singular `RIGHT / Right` description is not accepted by V2. Description
case, spacing and punctuation must match exactly; no symbol/name heuristic is
introduced. Structured products are known non-common securities, excluded from
Pairs without blocking it. Existing snapshots are not silently reclassified.
A fresh explicit manual reference sync is required to upgrade a V1 snapshot to V2;
this eligibility refinement does not require resyncing an already-current V2 snapshot.
Missing/new codes or changed/unsupported descriptions become **UNKNOWN**;
unknown never becomes common stock. No symbol/name heuristic or blacklist is used.
The raw provider code remains available for later mapping audits. Extending the
mapping requires review and a new mapping version/reference sync.

SEC CIK is never overwritten. Valid nonzero digit CIKs normalize to ten digits:
`CIK_MATCH`, `CIK_MISMATCH`, `CIK_MISSING_MASSIVE`, `CIK_MISSING_LOCAL` are reported.
Malformed nonempty provider CIK fails the sync. **Missing Massive CIK is accepted
for Pairs when local SEC identity/SIC are valid and no cached SEC/local conflict
exists**; it is reported as unavailable, not a verified match. For COMMON_STOCK
eligibility, conflicts cause explicit symbol quarantine, never issuer migration.
Known non-common-stock classifications are already outside Pairs; their CIK
cross-check is retained diagnostically but cannot block that exclusion.

## Pre-outcome Pairs V1 refinement and Manifest V5

Pairs V1 has not inspected historical validation outcomes. The original definition
did not contain the OTC rule. This is a pre-outcome research-definition refinement,
not performance tuning. User-provided local diagnostics identified all 22 remaining
base-company reference gaps as Alpaca `exchange=OTC`, exposing a deterministic
market-scope mismatch. Pairs is a highly liquid long/short US-equity research family;
OTC execution, borrow and reference semantics differ from the primary-listed
universe. The exclusion was introduced before first Pairs outcome validation,
without inspecting returns. Verification here uses synthetic equivalents only.

The frozen **base-company gate is evaluated before Massive qualification**:
local TradableAsset exists, tradable=true, asset_class=US_EQUITY, symbol is not
SPY, normalized local Alpaca exchange is not `OTC`, local SEC CompanyIdentity exists,
local SEC CIK is valid/nonzero and SIC is
valid/nonzero (three/four digits normalized to four). Missing or invalid local
company/CIK/SIC metadata deterministically places an asset outside the frozen
Pairs company universe. It creates no security-reference requirement or blocking
diagnostic. General CompanyIdentity validation and F database helpers are unchanged.
Exchange comparison strips whitespace and uppercases the persisted asset field;
missing/other exchange values are not guessed to be OTC. No ticker suffix, security
description, company name, ADR wording or symbol blacklist is consulted. OTC
symbols are excluded before Massive lookup and before historical liquidity ranking.

Within this base scope, `COMMON_STOCK_WITH_SEC_ISSUER_IDENTITY` requires Massive
canonical `COMMON_STOCK` and no local/Massive or cached SEC/local CIK conflict.
SPY retains its frozen
diagnostic-only role. The unsupported `operating_company=true` requirement is
removed; no Pairs economic parameter changes.

Within the base scope, qualification blocks missing reference/unresolved type,
then excludes known
non-common instruments as `EXCLUDED_SECURITY_TYPE`, nonblocking regardless of CIK
agreement. COMMON_STOCK then applies three-source identity qualification. Missing
reference is `SECURITY_TYPE_REFERENCE_MISSING`; unknown type is `SECURITY_TYPE_UNKNOWN`.
Both remain blocking inside the non-OTC base scope. A common-stock disagreement is
`EXCLUDED_IDENTITY_CONFLICT`, `validation_blocking=false`, and is excluded from
ranking and pair formation through explicit identity quarantine. This static
eligibility exclusion is distinct from silently replacing a member with missing
Daily inputs. Only unresolved reference/type evidence inside this scope blocks. It is
resolved **before historical ADV20 ranking**: preflight freezes no replacement
names/pairs and remains unready. It reports metadata requirements separately
from Daily-bar remediation. The overall unavailable status remains a fail-closed
summary of unresolved evidence.

Provenance uses only normalized ten-digit CIKs from the local company row, cached
`sync_state / sec_reference / ticker_to_cik`, and the active Massive reference:

| Local relationship | Quarantine class |
| --- | --- |
| local == cached current SEC; Massive exists and differs | `MASSIVE_REFERENCE_CIK_CONFLICT` |
| cached current SEC == Massive; local differs | `LOCAL_IDENTITY_STALE_OR_TICKER_REUSE` |
| Any other local/Massive or local/cached SEC disagreement | `UNRESOLVED_CROSS_SOURCE_IDENTITY` |

Absent cached SEC evidence is not refreshed automatically. A missing Massive CIK
remains accepted only without a cached SEC/local conflict. All three classes are
quarantined; classification does not establish which history to rewrite. No
company CIK, facts, bars or issuer histories are changed or merged. The existing
general SEC sync conflict guard still retains persisted identity and skips
conflicting updates. A symbol/issuer lifecycle model is a separate future task.

Pairs preflight/validation are local and read-only: **0 Massive, Alpaca or SEC
requests**. Manifest V5 binds source, current-reference semantics, company rule,
reference fingerprint, catalog fingerprint, mapping version, CIK conflict policy,
`company_universe_scope=LOCAL_TRADABLE_NON_OTC_US_EQUITY_WITH_VALID_SEC_COMPANY_CIK_SIC`,
`identity_conflict_policy=SYMBOL_QUARANTINE_NONBLOCKING`,
`identity_conflict_classification_version=LOCAL_CACHED_SEC_MASSIVE_CIK_V1`,
`identity_qualification_fingerprint`, selection-gap semantics and fail-closed
membership. V1/V2/V3/V4 are rejected, including rehashed manifests. Mapping stays
`MASSIVE_STOCK_TYPES_V2`. The CIK policy binds acceptance of missing Massive CIK
only without cached SEC conflict, and symbol quarantine for identity disagreement.
Economic parameters remain unchanged.
The membership reference digest covers **every symbol passing the frozen local
non-OTC base-company gate**, including common stocks, identity-quarantined symbols,
authoritative non-common exclusions and missing
markers within that scope: symbol,
canonical/raw type, provider CIK, market/locale/active, exchange, both FIGIs,
source/version and provider timestamp. Assets outside that base scope do not
participate: changing/removing their Massive rows cannot invalidate Pairs
membership. Adding valid local company/CIK/SIC metadata can bring a symbol into
scope and changes the fingerprint. Changing an OTC Massive row or its cached SEC
entry does not invalidate the manifest; changing its local exchange to non-OTC
brings a valid company into scope and invalidates the old manifest. The separate
identity qualification digest binds each base member's symbol, normalized local,
cached current SEC and Massive CIKs, conflict class, quarantine status, normalized
local exchange, SIC, qualification status and blocking flag. A new or resolved
conflict therefore invalidates frozen membership before any price discovery.
Local SEC identities/SIC and selection
Daily inputs remain separately bound. Relevant corrections invalidate the old
manifest through local checks; no network repair or rediscovery occurs.

## Local audit coverage

The read-only audit retains provider/global `missing_local_references`,
`coverage_fraction`, `security_type_counts`, `unknown_provider_type_counts` and
SEC CIK diagnostics. It separately reports:

```text
pairs_base_company_universe_count
pairs_base_company_missing_reference_count
pairs_base_company_missing_reference_symbols
pairs_base_company_unknown_type_count
pairs_base_company_unknown_type_symbols
common_stock_cik_mismatch_count
common_stock_cik_mismatch_symbols
pairs_otc_excluded_count
pairs_otc_excluded_symbols
pairs_identity_quarantine_count
pairs_identity_quarantine_symbols
pairs_identity_quarantine_by_reason
pairs_non_otc_company_universe_count
pairs_non_otc_missing_reference_count
pairs_non_otc_missing_reference_symbols
pairs_non_otc_unknown_type_count
pairs_non_otc_unknown_type_symbols
pairs_reference_identity_blocking_count
company_security_types_complete
```

The older `pairs_base_company_*` fields now describe the non-OTC base scope.
OTC exclusion counts cover valid local base companies excluded by their exchange.
Quarantine counts also include cached SEC/local conflicts when Massive CIK is absent.
`company_security_types_complete` reflects missing/unknown reference blockers only;
it does not establish Daily readiness or authorize validation. The mismatch list
applies only to common stocks inside the non-OTC base-company scope;
ETF/fund/etc. CIK disagreements remain visible in broader CIK totals. Symbol lists
are sorted and capped at 100 each; counts remain exact and corresponding
`*_symbols_truncated` flags identify omitted entries. Outside-scope assets are
counted separately as `outside_pairs_company_universe_count`; they are not Massive
blockers. `expected_provider_type_mapping_version` and
`security_reference_mapping_current` make old V1 snapshots visible without any
network repair. Auditing an old snapshot remains permitted and read-only.

These limitations apply together: **CURRENT_UNIVERSE_ONLY**,
**NOT_SURVIVORSHIP_CLEAN**, **CURRENT_REFERENCE_SECURITY_TYPE_NOT_PIT**.
`CURRENT_LOCAL_SIC_NOT_PIT` also remains. Current classifications are not historical
point-in-time evidence. The price gate first, nonblocking prefix history,
blocking internal ADV20/previous-close gaps, unresolved SIC2/session and no
rank-six replacement rules remain intact.

## Manual verification after implementation

`MANUAL_VERIFICATION_REQUIRED`: review this commit, then manually run only the
read-only audit. No Massive resync is needed if the active snapshot is V2/current:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli audit-security-reference
```

Provide the audit JSON console output for review. Expected observed-shape diagnostics
are 22 OTC exclusions, four explicit identity quarantines (two per observed class),
zero non-OTC missing/unknown references, and zero blocking reference/identity
conditions; real counts must be reviewed, not presumed. These are metadata checks,
not Pairs outcomes or trading authorization. Never provide credentials;
never provide `.env` or the key. Historical Pairs preflight/validation remain
deferred until the user-provided audit has been reviewed. No targeted ticker/CIK
fallback requests or inactive-universe crawl have been added: the sync still
fetches one type catalog and the same sequential active-stock bulk pages.

Implementation verification uses narrow mock/synthetic tests and static checks.
No real provider access, rate-limit sleeps, historical outcomes, full repository
tests, detached process or background job is part of verification.
