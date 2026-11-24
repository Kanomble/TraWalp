# F01 · Task 9 Source Admission

Source review verifies that the exact bytes belong to the specified Reporting Entity and Filing. The review binds the selection, verified byte manifest and extraction manifest. The generated source draft contains `UNREVIEWED`. A reviewer provides a separate document and communicates its SHA-256 independently. [Runbook](f01_historical_50_manual_ingestion_runbook.md) · [JSON schema](</G:/TraWalpResearch/import-artifacts/f01-task9-ingestion-preparation-v1/source_admission_schema.json>)

1. Compare the Submissions accession and exact date, form, Acceptance and primary filename with the SEC directory, document set and extracted cover/context evidence. Review the source SHA-256, retrieval provenance, extraction status and unresolved DTS findings.
2. Verify the Reporting CIK and as-filed issuer. Establish document completeness and justify the exact `us-gaap` membership for this filing. Record a durable original reference in `reference`.
3. For an approved V1 original use `issuer_identity=VERIFIED_REPORTING_ENTITY`, `completeness=VERIFIED`, `amendment=ORIGINAL_FILING`, `source_memberships=["us-gaap"]` and the filing's `decision=APPROVED`. Set the top-level decision, reviewer and zoned `reviewed_at` only after reaching a defensible conclusion. `REJECTED`, `BLOCKED` and `UNREVIEWED` gate publication.
4. Keep discrepancies and reviewer references with the job. Ambiguous issuer identity, incomplete evidence and amendments stay blocked by the initial V1 implementation. Trading status also needs security class, epoch and care with current symbol reuse.

HTTP success, hashes, an offline Arelle extraction and Source Admission are distinct milestones. Accounting Admission has its own evidence and decision. The source record never confers permission to trade or initiate a later download or import.
