# SEC incremental synchronization decision

Date: 2026-08-13

## Decision

Use the official EDGAR XBRL filing index as a catch-up-capable change detector. Refresh the SEC
ticker map once, compare supported-form accessions with `sec_accessions`, then request individual
Submissions and transient Company Facts only for changed or uninitialized CIKs. Keep full sync as
the recovery/rebuild path.

Company Facts 404 is a typed, expected source-availability outcome. Persist a compact negative
record in the existing `sync_state` table for seven days. Retry it on a new supported accession,
TTL expiry, or explicit full sync. Do not persist new raw Company Facts and do not delete the 798
protected legacy payloads.

## Evidence

The production result recorded 214 errors but did not retain typed per-error history. A read-only
reconstruction found 209 unique mapped CIKs with no accession state, normalized facts, or legacy
Company Facts; five of those CIKs were requested under two ticker aliases, producing exactly 214
symbol attempts. A rate-limited current recheck against the official Company Facts endpoint
returned 404 for all 209 unique CIKs and no other status. This is strong corroboration, not a claim
that historical log types were persisted.

The prior run made 7,157 per-symbol Submissions checks for only 6,072 unique CIKs. There were 637
multi-symbol CIKs and 1,085 redundant alias checks. Deduplicating by CIK removes those duplicates.

The official current master index was rejected as the daily signal: against current state it
produced 4,799 candidate CIKs, almost all from 8-K/6-K filings the parser does not consume. The
official XBRL index was about 2.45 MiB and downloaded in 0.75 seconds in the audit. Restricting it to
the exact forms accepted by `xbrl_parser.VALID_FORMS` produced three filing-driven CIK candidates;
including unsupported XBRL 8-K/6-K forms would have produced 4,000. This invariant prevents a
change detector from scheduling work that cannot change normalized output.

SEC documents the per-CIK Submissions and Company Facts APIs as real-time and its bulk ZIP archives
as nightly. The EDGAR index documentation describes daily and quarterly indexes, their CIK/form/
filing-date/path fields, and the current full index as a bridge through the previous business day:

- <https://www.sec.gov/search-filings/edgar-application-programming-interfaces>
- <https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data>
- <https://www.sec.gov/about/developer-resources>

## Failure and point-in-time properties

- The saved XBRL cursor is the SEC `Last Data Received` date, not the local wall-clock date.
- A gap crossing quarter boundaries fetches archived quarter indexes plus the current bridge.
- Malformed/regressed/future index data fails before requests or cursor advancement.
- Any real company request, parse, or database failure prevents global cursor advancement.
- Each successful CIK transaction commits normalized facts and accession state atomically; a retry
  therefore repeats only unfinished CIKs.
- Expected 404 records include status, check time, last known supported accession, and HTTP status.
- `fundamental_facts` and its `filed`, `period_end`, `accession_number`, and `frame` fields are
  unchanged. Historical queries still enforce `filed <= as_of`; raw cache is not queried by the
  screener.
- The current XBRL index may defer same-day filings until the next SEC index update. Catch-up state
  prevents loss; `sync --full` remains available when same-day/rebuild behavior is required.

## Universe audit

Of 13,404 Alpaca assets, the original mapping covered 7,157 symbols. The 6,247 unmapped symbols
classified from existing Alpaca name/symbol metadata as:

| Category | Count |
|---|---:|
| ETF or fund | 5,409 |
| Preferred | 336 |
| Depositary or foreign | 270 |
| Warrant | 103 |
| Unit | 35 |
| Rights | 18 |
| Unclassified | 76 |

Safe dot-to-hyphen aliases add 24 mapped symbols and five unique CIKs, leaving 6,223 unmapped and
52 unclassified. Independently, 288 of the remaining unmapped symbols have Alpaca exchange `OTC`;
that count overlaps the categories above and is reported separately. Several apparently valid
common stocks remain unclassified, demonstrating why fuzzy or name-based exclusion would be
unsafe. No mapped common-stock universe was narrowed; only observability and safe alias repair were
added.

The 798 protected legacy Company Facts rows do not overlap the 209 current 404 CIKs. They remain
untouched and are not automatically reparsed.

## Request and runtime expectation

The recorded run performed approximately 7,157 Submissions requests plus at least 1,303 Company
Facts successes and 214 failed symbol attempts (about 8,674 SEC data requests, excluding retries).
After initial negative-state initialization, a no-change same-quarter run requires two SEC
requests: ticker map and current XBRL index. A run with `N` changed CIKs normally requires about
`2 + 2N` requests. The live XBRL change-detection fetch took 0.75 seconds; an end-to-end production
sync was intentionally not run during this audit because it would mutate the production database.

## Alternatives rejected

- Per-CIK Submissions polling is simple and real-time but caused the measured 24-minute bottleneck.
- Nightly `submissions.zip` is authoritative but much larger than the XBRL index and unnecessarily
  transfers all histories for a small change signal.
- The general master/recent filing feed contains too many unsupported 8-K/6-K events.
- Staggered blind polling can miss a newly active filer and was not adopted.
- Fuzzy CIK mapping or wholesale exclusion of unmapped symbols risks dropping valid common stocks.

Remaining opportunities are improving the authoritative ticker mapping (without fuzzy matching),
measuring one post-deployment production run, and revisiting 8-K/6-K detection only if the parser is
extended to persist facts from those forms.
