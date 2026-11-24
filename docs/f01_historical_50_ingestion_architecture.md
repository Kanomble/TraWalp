# F01 · Task 9 Historical Filing Ingestion Architecture

Task 9 takes up the work previously planned as 8F.3D.12. It adds a manually gated filing input adapter for the existing Canonical-A‑V1 schema. The branch was `SEC-integration` at `ec6a8158564bdf864295dea1479f132104d12543`, with a clean starting worktree containing the committed D.11 changes. [Runbook](f01_historical_50_manual_ingestion_runbook.md)

## Reused components and scope

| Layer | Reuse and boundary |
| --- | --- |
| Selection | D.11 exact manifest, artifact member SHA-256, D.6 cohort and per-issuer archived Submissions member hashes. The initial CLI accepts 1–4 from the approved planning parent. The first proposal covers one NKE 10-Q. Newly reviewed planning parents are needed to move beyond the current D.11 set. |
| SEC | Existing `SecHttp`, per-request throttle, retry handling, and atomic per-document local cache publication. Task 9 adds persisted pacing and explicit hash-bound manual authorization. Source verification checks all selected document bytes and confines source directories to the two known cache locations. |
| Arelle | Existing `ArelleProcessor`, offline worker, SEC plugin and DTS/Package readers. Recursive discovery precedes Arelle. The selected model must load and hash all actual dependencies against the original-file or taxonomy inventory. Arelle findings and alternate documents are preserved. |
| Accounting | Original QName, issuer context, USD unit and date checked through the existing filing methods. Initial permitted mappings are consolidated as-filed `total_assets` and `cash`. Scope prior to wider E.11/E.12 onboarding deliberately stays small. |
| Canonical | Existing bootstrap and `write_company`, with the same Key, constraints, revisions, writer guard, receipt, atomicity and dataset ledger. A filing archive has different input semantics from a submissions/companyfacts polling envelope. Task 9 uses the shared Writer with a deterministic filing request, and retains the exact selected original and review inputs as its archive. |
| E.13 | Existing `CanonicalResearchReader` with new version-bound original witnesses. The historical CLI's approval chain remains specific to E.11. The Task 9 CLI checks only the newly admitted filing's pre-Acceptance and exact-Acceptance boundary, with no E.12 fallback. |
| Security and Market | D.6 50 issuers, seven historical pilot controls and 43 additional issuers. SPY separate. SEC CIK scopes accounting facts. D.8 market coverage, Security epochs, split/exit and missing benchmark warm-up remain external trading gates. |

The adoption path is `select → download or reuse → verify → taxonomy → extract → source-template → accounting-template → bootstrap-manifest → bootstrap CLI → import → assurance CLI → reader`. Only one phase runs per invocation. Reported status denotes a result of the named phase. Drafts and approvals are independent files. No part of the existing pilot, E.11, E.12 or E.13 approval is transferred to the expansion. [Source review](f01_historical_50_source_admission.md) · [Accounting review](f01_historical_50_accounting_admission.md)

## Versioning, crashes and evidence

The stable request ID is derived from Dataset-ID, Reporting CIK, Accession and the versioned Task-9 contract. Exact selection, source and extraction manifests and both review hashes are bound to its payload. Per-Filing atomicity includes the receipt, occurrence, revision, current company/facts, dataset version and state. Replaying an older receipt reconstructs its as-of accession set from prior revisions. A changed payload under the same ID is a conflict, including an approval with changed bytes. Distinct filings retain separate Accession/Filed keys. Original observations with equivalent canonical keys remain independently addressable in the extraction shard. A second identical admission does not add a canonical occurrence for the same membership. [Canonical contract](f01_canonical_deduplication_contract.md)

The first batch is immutable in the bootstrap scope. Later disjoint slices of this planning parent may append on the same code, universe and source generation after all initial receipts exist and after new source/accounting and operation reviews. Amendments are rejected by V1 selection. Supporting them requires review of both originals, supersession and acceptance, plus new PIT coverage. BBBY maintenance of exits, HEI class intervals and BG reporting-entity transitions remain separate evidence gaps.

Immutable metadata and Shards are checked again at phase boundaries. The import transaction rolls back on raised errors. If a hard exit left a native SQLite rollback journal, read-only checks stop and an authorized retry may recover the journal on the exact manifest-bound target. WAL and SHM block this path. Import conflict and unresolved taxonomy reports remain outside the database for review. The real target has no implied release or backup.

## Storage

Preflight budgets bound one request to at most 35 selected source documents at 64 MiB each, plus temporary staging and bounded evidence shards. Taxonomy storage, SQLite/sidecars, request provenance, backup and working reserve are budgeted separately. The pending job can reuse matching staged documents and extraction outputs. F: is a candidate for a later independently approved backup after physical disk separation and restore verification. [Current capacity and bound evidence](</G:/TraWalpResearch/import-artifacts/f01-task9-ingestion-preparation-v1/capacity_plan.json>)
