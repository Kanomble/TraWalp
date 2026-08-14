# SEC company identity decision

Date: 2026-08-13

## Incident and root cause

Production retained `PARA -> CIK 0000813828` for Paramount Global and `PSKY -> CIK 0002041610`
for Paramount Skydance. The current Alpaca asset named `PARA` is Banzai International, while the
cached exact SEC ticker map proposes `PARA -> CIK 0001826011`.

The conflict did not originate in dot/hyphen alias resolution. The previous merge used
`{**persisted_map, **current_sec_map}`, so an exact current SEC entry silently replaced the
persisted symbol owner. CIK deduplication then selected `PARA` as the canonical symbol for
`0001826011`; the atomic company/fact upsert reached SQLite, where `companies.symbol UNIQUE`
correctly rejected the second owner.

## Identity model and precedence

`companies` stores one canonical operational identity per CIK, not a complete historical ticker
ledger. Both its CIK primary key and symbol unique constraint remain appropriate. Facts and bars
also carry symbols, so changing an issuer solely because a ticker string was reused could combine
unrelated histories. A future historical ticker model belongs in a separate explicit alias/history
table rather than a weakened constraint.

Resolution now follows these rules:

1. A current exact SEC mapping that agrees with the persisted mapping is accepted.
2. A persisted symbol/CIK is retained when the current exact SEC mapping contradicts it; the SEC
   proposal is recorded as an identity conflict, not guessed or written.
3. A current exact mapping for an unowned symbol is accepted unless it would silently rename a CIK
   that already has another persisted canonical symbol.
4. A dot-to-hyphen alias is used only for an otherwise unmapped symbol and is subject to the same
   persisted-CIK protection. It is weaker than an existing identity.
5. Multiple non-conflicting symbols may point to one CIK during universe resolution, but the
   persisted canonical symbol wins. A new CIK uses the structurally safest deterministic symbol.

There is no name similarity matching and no Paramount-specific production rule.

## Conflict behavior

Conflicts are resolved before individual Submissions or Company Facts requests. TraWalp emits one
bounded warning, increments `identity_conflicts`, includes up to ten symbols in
`identity_conflict_sample`, skips the unsafe identity, and continues with other CIKs. Conflicts do
not increment `errors` or `database_failures`, but the SEC dataset stage is `partial` because the
mapping needs review.

Each active conflict is also persisted compactly in the existing `sync_state` table under source
`sec_identity_conflicts`, keyed by symbol. The value contains the existing and proposed CIKs,
existing symbol, resolution source, `unresolved` status, first detection time, and latest observed
time. Screening and market-data commands read only this local state and make no SEC requests.
For compatibility with databases whose last SEC run predates conflict persistence, the operational
guard also runs the same shared resolver against the already cached `sec_reference/ticker_to_cik`
map and persisted companies. This is local, deterministic, and introduces no network request. A
subsequent SEC sync persists the result with timestamps.

No company, fact, bar, negative-cache, or accession-state write occurs for the proposed identity.
The XBRL cursor may advance because the proposed CIK remains without successful accession state;
if a later ticker map resolves the ambiguity, `cik not in accession_states` schedules it again.
Per-company commits remain unchanged, so work before and after the conflict is preserved.

Symbol equality between `companies` and `assets` is not sufficient current-issuer evidence. While
a conflict is unresolved:

- screening emits an explicit ineligible `identity_conflict` record before loading facts, bars,
  snapshots, fundamental analysis, technical analysis, peer metrics, or scores;
- current snapshot refresh omits the symbol before Alpaca batching;
- historical-bar refresh omits it before Alpaca batching so a reused ticker cannot append a new
  issuer epoch to the old symbol-keyed series;
- existing facts, bars, snapshots, companies, and accession state are preserved without edits.

Skipped market/bar symbols are expected quarantine conditions, not API errors. Results contain an
`identity_conflicts_skipped` count and a bounded `identity_conflict_sample`.

On a later SEC sync, a conflict is cleared when the same resolution logic finds no active conflict
and either the current SEC mapping agrees with the persisted CIK or the old persisted symbol owner
no longer exists. A missing SEC mapping alone does not clear a still-persisted symbol owner. This
allows reviewed corrections and naturally corrected mappings to re-enter operational pipelines
without manual `sync_state` edits.

TraWalp does not store a verified corporate-action/ticker-transition date. Applying the conflict's
detection timestamp as that date would invent evidence, so unresolved symbols are conservatively
blocked from all screen dates, including historical `--as-of` commands. Historical data remains
queryable through dedicated debug/database paths and is never deleted, but it is not scored until
the identity ambiguity is resolved.

The current production maps evaluated read-only with the new resolver produce exactly one conflict:
`PARA`, proposed CIK `0001826011`, existing CIK `0000813828`, source `exact_sec_ticker`.

## Remaining limitation

TraWalp deliberately quarantines ticker reuse instead of automating historical identity migration.
Resolving a real corporate action may eventually require a reviewed workflow that assigns a new
canonical symbol while preserving old facts and price history under explicit historical aliases.
That broader model and any mass fact migration are outside this focused fix.
