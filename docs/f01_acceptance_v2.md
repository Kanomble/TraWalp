# F01 Scoped Acceptance V2

Task 8F.2E genehmigt die getrennte Bewertung nach Abschnitt 6 des
[Vertragsaudits](../reports/f01_acceptance_contract_resolution_audit.md). Der
[Genehmigungsdatensatz](f01_acceptance_v2_approval.json) dokumentiert Datum,
Scope, Vertragsversion, Abdeckung, historische Fingerprints sowie die
SHA-256-Werte der fünf Seals und drei ursprünglichen Reviews. Der
[Implementierungsaudit](../reports/f01_acceptance_bridge_audit.md) dokumentiert
die Offline-Validierung.

## Ergebnis des genehmigten Vertrags

| Dimension | Ergebnis bei verifizierter Evidenz |
| --- | --- |
| A · `STORAGE_AND_SEMANTIC_PARITY` | `EVIDENCE_SATISFIED_FOR_BOUND_SNAPSHOT`. Vollständige reale Bestandsgleichheit und bestandene synthetische Identity-Regression. |
| B · `QUALIFIED_FORWARD_RANKING_PARITY` | `INSUFFICIENT_EVIDENCE`. Vergleichbare Qualification und Sperrung, 334 ungelöste Kandidaten pro Seite, kein gültiger Forward-Rank-1. |
| C · `HISTORICAL_IDENTITY_EVIDENCE` | `NOT_ESTABLISHED`. Keine autoritativ belegten Epochen oder Reuse-Witnesses. Die synthetische Regression zählt ausschließlich für A. |
| D · `OPERATIONAL_CHAMPION_READINESS` | `NOT_RELEASED`. Diese Brücke kann D auch mit einem G6-PASS und aufgenommener G5-Evidenz nicht freigeben. |

Die historischen G4-, Ranking- und Identity-Ergebnisse erscheinen mit ihren
gespeicherten Statuswerten, Gaps, Clearances und Fingerprints. Aktive Gates und
der V1-Finalizer bleiben maßgeblich für ihre Verträge. Freigabefelder für
Rehearsal, operativen Betrieb und Cutover sind immer `false`.

## Verifikation

Die Brücke ist auf den genehmigten Rehearsal-Snapshot unter
`reports/f01_copy_rehearsal/20261115_8f2a` gebunden. Der Standard-Preflight
prüft unter nativen Windows-Pins die fünf Original-Seals samt Mitgliedern, die
historischen Bindings und Fingerprints sowie die unveränderte Code-, Config-,
Runtime- und externe Input-Bindung des G3-Gates. Die Approval-JSON ist über
ihren kanonischen Fingerprint im Einstieg gebunden.

Die drei Originalreviews und der lokale
[`f01_acceptance_v2_additions_review.json`](../reports/f01_acceptance_v2_additions_review.json)
bestimmen exakt die erlaubten Additions. Gleichnamige Einträge brauchen
identische Hashes. Nur die private Verifikationsinstanz des G3-Gates erhält
die erweiterte Vereinigung. Beim Lesen von V1-G4 und den Supplements gelten
die ursprünglichen, exakt rekonstruierten Bindings. Der Additions-Review
bindet auch den unveränderten 8F.2D-Grundlagenbericht. Der neue lokale Review,
der V2-Audit und Laufartefakte unter `reports/` sind von Git ignoriert.

Der gebundene Git-Basisstand verlangt ausschließlich neu hinzugefügte,
geprüfte Dateien. Eine Änderung an einer bestehenden Datei wie
`.gitignore` führt zu `NON_ADDITIVE_GIT_DRIFT`. Das gilt auch für die
vorliegende Ausnahmezeile, die den 8F.2D-Bericht in Git sichtbar macht.

Der Standard-Preflight veröffentlicht JSON auf stdout, erstellt keine
Ergebnisdatei und überprüft die versiegelte Snapshot-Evidenz ohne
Datenbankzugriff. `reference_freshness: NOT_CHECKED` gibt diese Grenze an.
`--verify-references` ergänzt den physischen Read-only-Check von Dateipfaden,
Identitäten, Sidecars, Hashes, Schema und COMPLETE-Marker. Diese Option liest
die großen Referenzdateien und öffnet Source und Work read-only.

## Manueller Preflight und G6

PowerShell im genehmigten Workspace mit dem gebundenen Python-Environment.
Der Preflight meldet bestehenden Git-Drift vor einem G6-Start.

```powershell
Set-Location -LiteralPath 'G:\GitHub\TraWalp'
$F01Python = 'G:\GitHub\TraWalp\.venv\Scripts\python.exe'

# Standard: Metadaten und Seals, ohne Zugriff auf Datenbankdateien.
& $F01Python -B scripts/validate_f01_acceptance_v2.py

# Optional: physische Referenzfrische zusätzlich lesend prüfen.
& $F01Python -B scripts/validate_f01_acceptance_v2.py --verify-references

# Vollständiges isoliertes G6 mit genau einem Crash-/Resume-Neubau.
& $F01Python -B scripts/validate_f01_acceptance_v2.py --g6 --authorize-recovery-copies --rebuild-recovery --batch-size 5000
```

G6 prüft die physische Referenzbindung selbst und braucht keinen vorherigen
Lauf mit `--verify-references`. Mit `--g6 --authorize-recovery-copies` ohne
`--rebuild-recovery` werden die Restore- und Ledger-Prüfungen ausgeführt.
Der G6-Status bleibt dann `INSUFFICIENT_EVIDENCE`. Mit dem zusätzlichen Flag
nutzt dieselbe isolierte Source-Kopie die vorhandenen Worker für Crash vor und
nach Batch-Commit, Crash im SQLite-Indexaufbau und erfolgreiches Resume.

Der gemeinsame `f01_acceptance.lock` serialisiert mit V1 und dem Supplement.
Geschützte Referenzen und Metadaten bleiben während der Probe gepinnt. G6 prüft
die Referenzen vor der Probe und erneut vor erfolgreicher Veröffentlichung.
Das übernommene Budget berücksichtigt neue Kopien, Journals und Sortierplatz
mit 20 GiB Reserve auf dem Job-Volume und 20 GiB frei im Windows-TEMP.
Der isolierte Worker verwendet den Scratch-Pfad des Versuchs. Die erfolgreiche
G3-Work ist stets eine schreibgeschützte Referenz.

Ein veröffentlichter G6-Versuch liegt unter
`acceptance_v2_runs/<UTC>_g6_<UUID>/result/` und enthält
`f01_acceptance_v2_g6.json`, `fingerprint.json`, `progress.jsonl` und
`seal.json`. Kopien und Worker-Protokolle bleiben im Versuchsordner.
Die Manifest-, G3-, G4-, Supplement-, Genehmigungs- und Review-Bindungen
stehen im neuen Ergebnis. Ein abgebrochener Versuch erzeugt keinen
veröffentlichten PASS. Der Exitcode ist `0` für verifizierten Preflight oder
G6-PASS, `2` für unzureichendes G6 oder Blockierung und `130` bei Abbruch.

## Spätere Aufnahme von G5

Die Brücke startet kein G5 und erwartet getrennt abgenommene Evidenz für ein
eigenes Integrations-Changeset. Nach einer solchen Abnahme können
`--g5-result` mit absolutem Ergebnisverzeichnis und `--g5-review-sha256`
mit dem separat vom Review erhaltenen SHA-256 zusammen übergeben werden.
Ein Ergebnis der alten isolierten Adapterprobe erfüllt dieses Format nicht.

Das Verzeichnis enthält die folgenden Dateien. Die drei Evidenzdateien
werden durch `seal.json` gebunden.

- `f01_g5_integration_report.json` mit Vertragswert
  `F01_G5_INTEGRATION_EVIDENCE_V1`, Scope
  `ISOLATED_DATA_SYNCHRONIZER_INTEGRATION`, `adapter_only: false`,
  `status: PASS`, leerem `gaps`-Array und
  `production_cutover_authorized: false`. `probes` enthält jeweils `PASS`
  für `regular_full_and_incremental_sync`, `canonical_writer_atomicity`,
  `commit_failure_and_receipt_replay`, `cursor_and_stage_recovery`,
  `readiness_failure_propagation` und `schema_and_identity_guards`.
- `review.json` mit `decision: REVIEWED_ISOLATED_G5_INTEGRATION`,
  `reviewer`, `reviewed_at_utc`, `report_fingerprint`, `report_sha256`
  und `operational_release_authorized: false`. Der externe Review beurteilt
  auch die tatsächlichen Testbelege des Candidate-Changesets.
- `fingerprint.json` mit `contract` und kanonischem `report_fingerprint`.
- `seal.json` als SHA-256-Map der drei vorstehenden Mitglieder.

Bericht und Review enthalten dasselbe `binding` mit
`approval_fingerprint`, `manifest_sha256`, `g3_result_fingerprint`,
`work_sha256` und eigenem `candidate_code_fingerprint`. Kanonische
Fingerprints folgen der vorhandenen G3-Serialisierung. Die eingereichte
Candidate-Evidenz bleibt an den neuen Code-Fingerprint gebunden. Die Brücke
prüft weiter im unveränderten historischen Workspace und übernimmt die
separat geprüfte G5-Evidenz als Referenz. Sie prüft den Candidate-Code nicht
erneut und ändert keine Freigabefelder.
