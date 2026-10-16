# Pairs V1 Daily availability audit

For run-global, source-specific exclusion after this audit, see
[Daily data qualification](pairs-stat-arb-v1-daily-qualification.md).

This audit concerns Daily input availability, not identity qualification or
strategy economics. Manifest V8, the identity-qualified research universe,
run-global quarantine and CURRENT_UNIVERSE_ONLY remain in effect.

Every missing `(symbol, session)` has exactly one classification, its original
requirement roles, a reason and a repair action. Counts are unique symbol/session
counts; role counts can overlap. Reports also give first/last required missing
session, the longest consecutive official-session gap, gap blocks, concentration,
local bar bounds/counts, provider-range metadata and current reference status.

The categories are `TRUE_LOCAL_MISSING`, `PRE_LISTING_OR_NOT_YET_ACTIVE`,
`POST_DELISTING_OR_NO_LONGER_ACTIVE`, `PROVIDER_SYMBOL_MAPPING`,
`PROVIDER_CONFIRMED_NO_SESSION`, `LOCAL_COVERAGE_METADATA_ERROR`,
`REQUIREMENT_LOGIC_ERROR` and `UNKNOWN_BLOCKING`.

Only `TRUE_LOCAL_MISSING` receives `SYNC_DAILY`. The dated provider observation
must match symbol, session, feed and adjustment and confirm a usable native bar;
alternatively independent evidence must establish an expected provider bar inside
verified membership. A checked range or a verified membership interval alone does
not guarantee that IEX emitted a bar. Empty responses, 404s and failed requests
remain unknown in this session-level classifier, not confirmed per-session absence.
A completed qualification audit can separately resolve source unavailability as
the run-level exclusion described below. First/last local bars and SEC
registration forms are diagnostic context, never listing/delisting boundaries.
Verified outside-epoch sessions are nonmembers and never sync targets. Explained
provider absence does not authorize interpolation or changing the frozen Top-N.

Normal preflight is still entirely local. It adds per-gap classification/reason
to Coverage/Requirements and a `daily_blocker_audit` summary with concentration.
It neither reads diagnostic report files nor performs provider calls or syncs.
The separate read-only artifact auditor can consume explicitly supplied bounded
provider observations and produce a reviewable plan before any repair:

```powershell
.\.venv\Scripts\python.exe -m trading_system.backtest.pairs_stat_arb_v1_daily_audit `
  --candidate-manifest reports/<stem>_pair_candidates.json `
  --daily-requirements reports/<stem>_daily_requirements.json `
  --output-stem <new-audit-stem>
```

An optional `--provider-observations` points to the explicit audit evidence object
containing `observations`. These are not automatic production inputs. The auditor
checks manifest integrity/binding and observation scope/provenance. It exports
audit JSON, per-requirement CSV and a plan partitioned into minimal consecutive
official-session ranges. Audit outputs are immutable by stem.

## Local investigation on 2026-10-16

Baseline: `pairs_identity_quarantine_preflight_2024-01-02_2026-08-12`, main
`ce7c832`. Its 4,726 missing requirements were all actually absent locally:
4,562 selection inputs and 164 calibration inputs. All 4,726 ranges were marked
checked; that metadata correctly describes successful queries, not completeness.
There was no verified membership interval for these symbols. No pre/post-listing
or metadata error was asserted from first/last bars or registration filings.

The existing Alpaca adapter checked exactly the missing sessions, grouped into
71 minimal range queries (74 symbol/range blocks). It returned valid IEX/all bars
for **3,083 sessions**, and no bar for **1,643**. There were no probe request
failures. The seven positive blocks belong to BKYI, CAMP, GURE, PAPL, PTN, VIVK and
WBUY; only these received repair actions. The plan and observations were saved
before invoking the existing DataSynchronizer, without benchmark/SEC sync or
market-wide acquisition. Each repair checks local usable bars and range metadata.

BRK.A is stored/addressed as `BRK.A` in the local asset, reference, database and
adapter. A read-only Alpaca asset query confirms that exact active NYSE symbol.
No bar was returned for its 674 missing IEX sessions. This is unknown provider/feed
availability, not an established punctuation mapping defect or listing boundary.
For the same missing session, 2024-02-06, direct SDK response-key inspection found
no data under `BRK.A`; `BRK-A` and `BRK/A` returned API errors. No alternate
response key was being dropped by the adapter and no working alternate address
was found.
The existing SEC dot-to-dash CIK alias is unrelated to market-data addressing.
AlpacaDataClient now supports explicit, immutable, one-to-one provider aliases
with canonical response/database keys; no punctuation is guessed and no alias is
configured for BRK.A. Colliding requests fail before provider access.

Alpaca documents that IEX is one exchange and daily bars require eligible trades;
no eligible price-updating trades can mean no emitted bar. This explains why an
empty result cannot prove a local ingestion error or a listing boundary; it does
not establish the particular cause for any one missing session.
See the [Alpaca Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).

Audit artifacts use `reports/pairs_daily_gap_classification_*`; the fresh local
preflight uses `pairs_daily_blocker_audit_2024-01-02_2026-08-12`. Candidate discovery
is rerun after repair; the old manifest is not reused. Remaining unknowns block.
No backtest, feed switch, performance exclusion or strategy parameter change is
part of this workflow. PIT and survivorship limitations remain unchanged.

## Repair verification and fresh preflight

All 3,083 received bars were inserted through DataSynchronizer. Updated,
unchanged, invalid and duplicate bars, sync errors and unresolved repaired
requirements were all zero. All seven coverage ranges were verified again.

| Metric | Original preflight | Fresh preflight |
| --- | ---: | ---: |
| Ready | false | false |
| Daily local missing | 4,726 | 1,697 |
| Selection-input gaps | 4,562 | 1,479 |
| SIC2 unresolved sessions | 4,084 | 2,097 |
| Pair/session candidates | 155,901 | 173,008 |
| Identity unresolved/blocking | 0 | 0 |
| Identity quarantined | 53 | 53 |

The refreshed discovery adds 54 calibration requirements (CRCL 13, TLN 41).
Thus 4,726 - 3,083 + 54 = 1,697. Every remaining requirement is
`UNKNOWN_BLOCKING` / `NO_SYNC`: 1,643 have empty-response evidence from the
bounded probe; the 54 newly required sessions have no per-session availability
verification. They are not retrospectively called pre-listing. The 1,697 consist
of 1,479 selection inputs and 218 calibration inputs.

The largest remaining blocks are BRK.A 674, FIEE 373, NBIS 221 and LION 111;
together they account for 81.3% of remaining gaps. Next investigate that unknown
provider/feed-availability class and, separately, authoritative membership bounds
for calibration prefixes. No further repair or backtest was started. Manifest V7
was freshly discovered, its source fingerprint changed, and the exact same 53
quarantined symbols are absent from universe, Top-N, pairs and all requirements.
`pairs_daily_gap_classification_after_audit.json` and its CSV contain the full
remaining classification and symbol diagnostics; the repair log records every
executed range and ingestion count.
