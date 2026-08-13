# SEC storage decision

Date: 2026-08-13

## Context and audit findings

The operational SQLite database is 15,189,172,224 bytes (14.15 GiB). It has 4 KiB
pages, 3,708,294 allocated pages, and no freelist. The supplied `dbstat` measurements
attribute about 10.5 GB to `raw_sec_cache`, of which about 9.9 GB is Company Facts
JSON and 0.58 GB is Submissions JSON.

All raw-cache access is in the persistence and synchronization layers:

- `cache_sec_payload` is the compatibility writer used by tests and older workflows.
- the pre-existing complete sync cached Submissions and Company Facts before parsing;
  the incremental/full refactor wrote both payloads atomically with normalized facts.
- incremental-state migration reads cached Submissions to recover recent accessions and
  checked for a cached Company Facts row as a proxy for a previous import.
- screening, point-in-time queries, metric calculation, reporting, and
  `debug-fundamentals` read only `fundamental_facts`.

The normalized table intentionally retains each supported observation's `filed`,
`period_end`, `period_start`, accession, frame, form, taxonomy, tag, unit, and value.
`facts_available_as_of` filters on `filed <= as_of`, so raw JSON is not part of
point-in-time correctness. The parser retains only concepts mapped to TraWalp metrics;
raw Company Facts therefore contains information that is not represented structurally.
That extra information is useful only for future parser development or source-level
debugging, neither of which currently has an offline command or runtime dependency.

Raw Company Facts can be reconstructed with another SEC request. Losing the cached copy
makes offline reparsing less convenient and makes recovery depend on SEC availability.
However, the current cache keeps only one overwritten response per CIK rather than a
versioned snapshot, so it cannot reproduce the source exactly as it existed for older
sync runs or by itself reproduce historical parser behavior. Parser behavior is better
reproduced from versioned code and fixtures; historical screening is reproduced from
the persistent normalized observations.

## Options considered

### A. Keep full Submissions, Company Facts, and normalized facts

This minimizes repeat SEC requests during short cache windows and permits immediate
offline reparsing of the latest cached response. It is simple, but duplicates about
10.5 GB in the operational database, does not retain source versions, and is no longer
needed for incremental detection now that accession state exists.

### B. Keep Submissions and make Company Facts transient

This removes the dominant 9.9 GB payload while retaining a convenient migration and
inspection source. It is sound for screening and point-in-time behavior. Full reparsing
requires an SEC request, and persisted Submissions still duplicate compact accession
state.

### C. Persist compact synchronization state

Store the observed relevant accession numbers in `sync_state`, update that state only in
the same transaction that successfully stores parsed facts, and fetch Company Facts only
when the accession set changes or a full repair is explicitly requested. This has the
smallest operational footprint and clear partial-failure semantics. Every incremental
check still needs the current Submissions response; a local cache cannot prove that a
remote filing has not changed. Existing cached Submissions remain a useful one-time
migration baseline.

### D. Store compressed raw payloads outside SQLite

A five-row production sample compressed from 20,213,845 bytes to 1,375,421 bytes with
gzip level 6 (6.8% of raw) in 0.19 seconds. A compressed optional archive could therefore
retain most reparsing convenience for well under 1 GB at the measured ratio. It would
also require cache naming, atomic writes, retention, invalidation, backup, and privacy/
operational policy that TraWalp does not otherwise need. No zstd dependency is present,
so one was not added merely to benchmark it.

## Decision

Use Option C for new synchronization, with Option B-compatible legacy handling:

- Company Facts is transient: fetch, parse, atomically upsert normalized facts and sync
  state, then discard the payload.
- Persist compact relevant-accession state. Do not advance it when parsing or database
  persistence fails.
- Do not write new raw Submissions payloads. Continue reading legacy Submissions once to
  bootstrap accession state, and retain legacy Submissions during default cleanup.
- Keep all structured historical facts and all market bars.
- Keep the legacy `raw_sec_cache` table and public cache methods for database and API
  compatibility. Cleanup is an explicit command, never startup behavior.
- Default cleanup removes only Company Facts rows whose CIK already has at least one
  normalized fact. Rows without structured facts remain in place and are reported as
  blocked, because they may be the only recoverable source after an old parse failure.
- VACUUM is separate/opt-in, reports free space, and refuses to start without a
  conservative amount of temporary disk space.

The inability to reparse offline is the principal cost. Users who require that workflow
should copy or compress the raw cache before cleanup; an opt-in external gzip archive can
be added later if a real workflow justifies its lifecycle complexity.

## Non-destructive validation

The production database was opened with SQLite `mode=ro`; no cleanup or VACUUM was run.
The guarded plan found:

- 5,860 total Company Facts cache rows / 10,395,596,569 payload bytes;
- 5,062 rows / 10,150,667,222 bytes (9.45 GiB) with normalized facts and eligible
  for cleanup;
- 798 rows / 244,929,347 bytes (233.6 MiB) without normalized facts, therefore blocked;
- 11,618,746 normalized fact rows and 1,816,917 daily bars, matching the supplied audit.

A disposable synthetic database validated physical SQLite behavior. It was 10,190,848
bytes before cleanup and remained that size after `DELETE`, with 2,440 freelist pages. An
explicit `VACUUM` reduced it to 176,128 bytes. All Company Facts rows were gone, all
Submissions rows remained, and the point-in-time fact result was identical. An automated
full screener test likewise compares reports before and after cleanup, excluding only the
generation timestamp.

Applying the production cleanup and VACUUM should therefore reclaim roughly 9.5 GiB of
payload plus associated SQLite page overhead and leave a database on the order of 4–5 GiB.
That is an estimate until the user explicitly performs both operations.

## Index decision

The wide fact uniqueness index is the next largest single object (about 1.47 GB). A
compact deterministic digest could reduce it, but collision-safe enforcement would need
a schema migration, digest versioning, duplicate verification, and performance testing
over more than 11 million rows. The immediate problem is the roughly 10 GB raw duplicate,
not structured history. No fact schema or index is changed in this work.
