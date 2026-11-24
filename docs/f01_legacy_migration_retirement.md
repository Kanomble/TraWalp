# F01 · Abschluss der historischen Copy-Rehearsal-Migration

Die alte G0–G6-Kette sollte wiederholte SEC-Fundamentalbeobachtungen aus der Legacy-Tabelle in eindeutige Canonical Facts überführen. Wiederholungen erhöhten das Datenvolumen und konnten über Auswahlreihenfolge und Tie-Breaks die Fakt-Auswahl und damit potenziell Screening und Scores beeinflussen. Der historische Audit untersuchte den Effekt im damals gebundenen Umfang. [Duplicate-Champion-Impact](../reports/f01_duplicate_champion_impact_audit.md) · [Migrationsplan](../reports/f01_fact_deduplication_migration_plan.md)

Canonical A-V1 hält die Fact-Identität separat von ihren Source Occurrences. Historische Zeilen behalten ihre Occurrence-ID und Herkunft. Neue Beobachtungen brauchen explizite Source-Membership-Admission. Dataset-Versionen und Fact Revisions erhalten frühere Inhalte. Ingestion Receipts binden den Request und seine Eingaben atomar an die veröffentlichte Version. Die zeitgebundene Auswahl braucht den konkreten Dataset-Stand und für E.13 die Originalevidenz samt Filing-/Acceptance-Zeit. [Canonical-Vertrag](f01_canonical_deduplication_contract.md)

| Historischer Nachweis | Ergebnis |
| --- | --- |
| G3 aus dem gebundenen Rehearsal | `G3_COMPLETE`, 18.698.090 Source Occurrences, 8.329.908 eindeutige Canonical Facts |
| G6 | `PASS` für die damals getesteten Restore- und Crash-/Resume-Pfade |
| Acceptance V2 A | `EVIDENCE_SATISFIED_FOR_BOUND_SNAPSHOT` |
| Historisches V1-G4 und Ergänzungen | `INSUFFICIENT_EVIDENCE` |
| G5 | Eigenständige reale Acceptance weiterhin unbelegt. Offline-Integration ist separat belegt. |
| Acceptance V2 B / C / D | B `INSUFFICIENT_EVIDENCE`, C `NOT_ESTABLISHED`, D `NOT_RELEASED` |

[Acceptance V2 und historische Bindungen](f01_acceptance_v2.md) · [Historische Vorbereitung](../reports/f01_real_migration_preparation_audit.md) · [G5-Input-Evidenz](../reports/f01_g5_input_evidence_preparation_audit.md)

Die künftige 50-Issuer-Forschung startet auf einer eigenen isolierten `RESEARCH_ONLY`-Lineage. Die Produktionsdatei und ihre Sidecars bleiben geschützt. Der alte produktive SQLite-Bestand ist kein Expansionsziel. Aktive Legacy-Lese-/Schreib-Guards bleiben Teil der Laufzeit.

Der Benutzer hat den Inhalt von `reports/f01_copy_rehearsal/20261115_8f2a` einschließlich der Original-Rehearsal-Datenbanken bereits entfernt. Beim Taskstart war der dokumentierte Jobpfad abwesend. Prüfungen, die dessen versiegelte Source-, Backup-, Work-, Restore-, Recovery- oder weitere Byte-Evidenz benötigen, sind mit dem lokalen Bestand nicht mehr reproduzierbar. Git bewahrt den Quellstand. Ein Seal, Manifest oder Fingerprint ersetzt weder die gelöschten Bytes noch eine heutige operative Freigabe. Historische Ergebnisse behalten ihren ursprünglichen Scope. Die dokumentierten historischen Runbooks beziehen sich auf ihren genau gebundenen Checkout, die damalige Runtime und den damaligen Dateibestand.

Eine kanonische Fact-Identität darf nur einmal existieren. Mehrere reale Beobachtungen desselben Facts müssen nachvollziehbar bleiben. Unterschiedliche historische Versionen müssen erhalten werden.

## Ausführung am 24.11.2026

Im Arbeitsbaum von `SEC-integration` am Basis-HEAD `1a869020d0f4ef54c1437b5fbfa2becad6c0a5ba` wurden 393 Legacy-Dateien tatsächlich entfernt, davon zwölf Git-Dateien der exklusiven Rehearsal-Kette, 374 synthetische SQLite-/Lock-Dateien und sieben zugehörige Python-Bytecode-Dateien. Das entspricht 39.909.268 logischen Dateibytes. Der physisch freigegebene Platz wurde nicht separat gemessen. Die historische Vorab-Löschung durch den Benutzer ist in dieser Summe nicht enthalten.

Bootstrap, Bulk-Pilotimport und Bulk-CLI beziehen ihre gemeinsamen Helfer jetzt aus [f01_research_evidence.py](../scripts/f01_research_evidence.py). Die vollständige Ausgabe von `repository_evidence` stimmte für alten und neuen Helfer auf demselben Arbeitsbaum überein. SHA-256-Inventar, kanonische JSON-Bildung, Runtime-/Paketbindung und Ergebnisfelder bleiben gleich. Die geänderten Codepfade führen erwartungsgemäß zu einem neuen Kandidaten-Fingerprint. Frühere Freigaben behalten ihre damaligen Hashes. Die unabhängigen G5-Evidenzwerkzeuge, die Quellenbeobachtungen aus dem manuellen G5-Versuch, die Canonical-Runtime und ihre Alt-Schema-Kompatibilitätsprüfungen bleiben verfügbar. Alte historische Berichte, Manifeste, Seals, Reviews, Fingerprints und unklar gebundene oder ACL-gesperrte Restartefakte bleiben erhalten.

[Inventar](</G:/TraWalpResearch/import-artifacts/f01-legacy-retirement-v1/legacy_remaining_inventory.csv>) · [Löschmanifest](</G:/TraWalpResearch/import-artifacts/f01-legacy-retirement-v1/legacy_deletion_manifest.json>) · [Ausführung](</G:/TraWalpResearch/import-artifacts/f01-legacy-retirement-v1/legacy_retirement_execution.json>)
