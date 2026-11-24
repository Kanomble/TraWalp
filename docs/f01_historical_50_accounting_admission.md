# F01 · Task 9 Accounting Admission

Generate the accounting draft only after an independent Source Admission. The draft inherits the selection, byte and extraction manifests plus a canonical JSON digest of the reviewed Source record. Its raw file SHA is also verified by the CLI. Each candidate key hashes the exact extracted observation, including its original occurrence reference. [Runbook](f01_historical_50_manual_ingestion_runbook.md) · [JSON schema](</G:/TraWalpResearch/import-artifacts/f01-task9-ingestion-preparation-v1/accounting_admission_schema.json>)

Only `us-gaap:Assets` and `us-gaap:CashAndCashEquivalentsAtCarryingValue` are supported in V1. Values require the Reporting CIK, USD, a dimensionless instant context for the Submissions report date and a qualified original source. No synthesized values are produced. Review procedure:

1. Cross-check the exact concept, context, period, dimensions, unit, decimals, value and consolidated scope against the original report. Confirm the row is as filed and not a later restatement or mixed parent/class context.
2. Preserve `original` and the opaque observation key. For a defensible fact set `decision=APPROVED`, `scope=CONSOLIDATED`, `version=AS_FILED`, keep `fallback=NONE`, record its exact decimal text in `expected_value` and cite the source in `reference`.
3. Record `REJECTED`, `BLOCKED` or `UNREVIEWED` for other candidates. A top-level approved review with a named reviewer and zoned time still requires at least one approved fact per selected filing. Submit the reviewed file and independent SHA-256.
4. Check the approved facts at the original Acceptance timestamp with the Task-9 E.13 command. The original shard retains alternative observations, unsupported metrics and findings for later consideration.

Issuer Shares, Total Debt, Finance Leases, OCF, CapEx/PPE-only, EBITDA and ROIC stay outside this initial adapter. E.12 fallbacks and amended-filing supersession require their own versioned scope and regression coverage. Original filing bytes, separate Source decision and Acceptance remain mandatory.
