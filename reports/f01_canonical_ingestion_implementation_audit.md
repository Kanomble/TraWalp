# Task 8F.1 – Canonical Fact Storage and Ingestion

Datum: 15.11.2026. Basis-HEAD: `ffca273aa4f024ee848900895ea66ea45704b7e6`.

**Abschlussstatus: BLOCKED für Copy-Rehearsal.** A-V1 ist als expliziter
Offline-Speicher-/Writer-Pfad implementiert und synthetisch geprüft. G0–G2 sind
für einen realen Copy-Rehearsal nicht erfüllt. Dieser Bericht erteilt weder
Rehearsal-, Writer-Aktivierungs- noch Cutover-Freigabe.

## Grundlage und Scope

Geprüft wurden die vier Auftragsreferenzen, insbesondere die Entscheidung und
G0–G6 in `f01_alias_ingestion_resolution_design.md`. Die bekannte
Single-Row-Teilprojektion aus 8C ist nicht das hier implementierte Modell A.
Keine neue konkrete Census-Inkonsistenz festgestellt; keine Census-/Alias-Audits
wiederholt. `.gitignore` wurde gelesen; die neuen temporären Testverzeichnisse
fallen unter `.tmp_pytest_*/`, eine Änderung ist nicht nötig.

Keine produktive Datenbank geöffnet, gehasht, kopiert oder verändert. Keine
Providerzugriffe, kein Sync, keine Shadow-Inception und kein Shadow-Run. Keine
Änderung an Champion, Parser, Cash-/Debt-/Shares-/TTM-Selektoren oder
Strategieparametern. Kein Commit/Push. Alle geöffneten SQLite-Dateien waren
selbst erzeugte synthetische pytest-Fixtures. Netzwerkzugriffe sind in diesen
Tests durch das vorhandene Offline-Fixture verboten.

## Implementierter A-V1-Vertrag

`src/trading_system/data/canonical_facts.py` implementiert
`F01_CANON_OCC_V1`, ohne Aufruf aus `Database.initialize`, CLI oder SEC-Sync.

| Bestandteil | Vertrag |
| --- | --- |
| Canonical | Unveränderter neunspaltiger fachlicher Key, ohne Symbol/Taxonomy; getrennte partielle Instant-/Duration-UNIQUE-Indizes, kein Datumssentinel; NOT-NULL-Regeln des alten Schemas. |
| Snapshot | Stream in Original-ID-Reihenfolge; jede Originalzeile wird Occurrence, auch identische Wiederholungen und unbekannte Metrics. Unterschiedliche korrigierbare Inhalte sammeln ein Konfliktmanifest in der Exception und rollen den ganzen Import zurück. |
| IDs | Ursprüngliche ID = Occurrence-ID und `original_id`; Canonical-ID separat. Neue Occurrences erhalten globale fortlaufende SQLite-IDs. Keine Neuordnung nach Fact-ID oder Metric. |
| Source | Symbol und Taxonomy bleiben an jeder Occurrence erhalten. Version, Evidence und getrennte Legacy-/Forward-Rollen sind explizit gespeichert. Keine Security-Epoch-/Share-Class-Gleichsetzung. |
| Revisionen | `fact_revision` erhält Wert, Fiscal Year/Period und Form je Dataset-Version. Korrektur aktualisiert nur diese vier Canonical-Felder; alte versionierte Fact-Streams bleiben lesbar. |
| Projektionen | `legacy_occ_v1`, `forward_occ_v1` für die aktuelle Version; `legacy_occ_versions_v1`, `forward_occ_versions_v1` mit expliziter `dataset_version` für alte Streams. `fundamental_facts` ist die Legacy-Kompatibilitätsview mit alten Spalten/IDs. |
| PIT | Filed-PIT bleibt unverändert. Dataset-Version wählt den Input-Snapshot, keine zusätzliche Retrieval-PIT-Regel. |
| Receipts | Request-ID eindeutig, Bindung an vollständigen normalisierten Input einschließlich Company, Reihenfolge, Identity, Admissions, Payloadhash und Retrievalzeit. Gleicher Request ist idempotent; veränderter Input bei gleicher Request-ID ist Fehler. Neue Requests behalten Provenienz ohne Replay-Anker. Historischer Snapshot-Receipt hat unbekannte Retrievalzeit NULL. |
| Guards | Schema-/Writer-Version, gespeicherte DDL-Signatur und FK-Prüfung vor neuen Writes; alter Writer scheitert auf A-V1 vor Company-/Fact-Write. Neuer Writer scheitert auf altem Schema. Separate explizite V1-Tabellen-/Operations-Allowlist, keine Erweiterung des alten SEC-Guards. |

`build_snapshot` ist ein Connection-basiertes Offline-Primitiv, kein sicherer
Datei-Migrationseinstieg. Es bleibt atomar und behält zusätzlich die alte Tabelle
als `canonical_import_archive`; daraus folgt ausdrücklich keine Platzersparnis.
Das Primitiv ist kein für 18 Millionen Zeilen freigegebener G3-Batch-/Resume-Lauf.

## Neuer Writer und Admission

`write_company` erhält vollständig geparste Facts, eine verifizierte
Request-Zuordnung und explizite Admissions je Source-Symbol/Taxonomy. Company-CIK,
Payload-Fact-CIKs, lokale Symbolzuordnung, persistierte offene Konflikte und
gegebenenfalls gecachte SEC-Zuordnung werden geprüft. Fehler propagieren.

Neue Identitäten erhalten genau einen Forward-Anker in Eingabereihenfolge.
Unveränderte Wiederholung und Korrektur behalten vorhandene Occurrences. Neue
Accessions, Filingdaten, Frames, Tags, Units und Perioden bleiben verschiedene
Identitäten; Amendments werden nicht als Korrektur verschmolzen.

Ein zusätzliches Source-Label derselben Identität setzt keinen Forward-Anker.
Legacy-Aufnahme verlangt explizite Evidence. Eine zunächst nicht zugelassene
Membership kann später mit neuer Occurrence/Version aufgenommen werden; der alte
Snapshot wird nicht rückwirkend erweitert. Admissions sind Aufrufer-Evidenz,
keine aus einem Ticker abgeleitete historische Identitätsbehauptung.

Company, Canonical, Revision, Occurrence, Receipt, Dataset-Version, Accession-State
und Status-Löschung gehören zu einer Transaktion. Eigentümer startet
`BEGIN IMMEDIATE`; bei externer Transaktion verwendet der Writer einen Savepoint
und committet sie nicht. Fehler rollen auch dann die vollständige Company zurück,
wenn der externe Aufrufer die Exception fängt. Busy-/Lockfehler werden nicht in
erfolgreiche No-ops umgedeutet. Stale-Version-Writes scheitern und verlangen eine
bewusste Wiederholung gegen die neue Version.

Es gibt keine Runtime-Verkabelung des neuen Writers. Deshalb ist hier auch keine
allgemeine Freigabe der künftigen DataSynchronizer-Integration enthalten. Deren
Fehlertelemetrie/partial-/failed-/Readiness-Kette bleibt unverändert; spätere
Aktivierung muss sie mit dem neuen Adapter erneut nachweisen. Kein READY-Override.

## Reader und Consumer

Legacy-Reader und Batch-/DENSE_RANK-Queries behalten ihre bestehenden Filter und
explizite Sortierung. `LocalInputs` verwendet bei exakt validiertem A-V1-Schema
die Forward-Projektion; auf alten Datenbanken bleibt der bisherige Tabellenpfad.
Die Companies-CIK-Auflösung und Konfliktsperre bleiben vor dem Fact-Read erhalten,
ohne Symbol-/CIK-Fallback. Source-Symbole werden nicht umgeschrieben.

Gezielte SQL-Verbrauchersuche fand neben Database/LocalInputs: Discovery-Snapshot,
Historical-Universe-/Coverage-/Regime-/Data-Gap-Audits, SEC-Qualification/-Force,
SEC-Backfill-Pilot und Massive-Identity-Pilot. Ihre üblichen Fact-SELECTs haben
weiterhin die Legacy-View. Schema-, Index-, Rowid-, Performance- und externe
Cache-/SQL-Erwartungen sind damit nicht pauschal abgenommen. Sie gehören in das
snapshotgebundene G1-Consumer-Inventar. Die alte SEC-Writer-Allowlist bleibt
bewusst unverändert und kann den neuen Writer nicht autorisieren.

Alte Fact-Versionen sind hier lesbar; vollständige alte Company-/Identity-/Markt-
und Config-Snapshots müssen weiterhin im G1-Input-Archiv erhalten werden. Eine
Fact-Revision allein ist kein vollständiger historischer Research-Snapshot.
Physische Schema-/Source-/Dataset-Fingerprints ändern sich; historische Artefakte
werden nicht umetikettiert.

## Gezielte synthetische Validierung

Neue Suite: `tests/test_f01_canonical_ingestion.py`.

- Sechs AABB-Interleavings, je neun Legacy-/Forward-/PIT-Kombinationen:
  vollständige Stream-/ID-/Multiplicity-Gleichheit, Accounting, Debug, F-Scores,
  Rankingresultate und Batch/DENSE_RANK; mehrere CIKs beim selben Ticker,
  historische Source-Labels, Cash-/Debt-Ties, unbekannte Metric und Amendment.
- Neue Identitäten, gemischte Instant-/Duration-Reihenfolge, Normalisierung
  optionaler Keys, Full-artige Wiederholung und neue Accession/Amendment;
  Korrektur mit erhaltenen Alias-/Original-Occurrences und alten Dataset-Werten.
- Neue Source-/Taxonomy-Membership, zunächst gesperrte und später explizit
  zugelassene Membership, kein zusätzlicher Forward-Anker.
- Alle Key-Dimensionen getrennt; falsche CIK nach vorherigem gültigen Fact,
  fehlende Source-Admission und offene Identity-Konflikte rollen vollständig zurück.
- Fehler-Injektion an Company, Canonical, Revision, Occurrence, Receipt,
  Accession-State, Dataset und Version sowie beim abschließenden Status-Löschen;
  vollständiger Zustand vorher/nachher gleich. Externe Rollbacks werden respektiert.
- Zwei konkurrierende Connections, gleicher idempotenter Request, Restart-Retry,
  konkurrierender Lockfehler und Stale-Version-Sperre.
- Alte/neue Schema-/Writer-Kreuzkombinationen, DDL-Drift, direkte NULL-safe
  Constraint-Verletzung und verweigerte unzulässige Guard-Operationen.
- Manifest-Hashdrift, Source-/Backup-Aliasing, echte Hardlinks und simulierte
  verlinkte Elternpfade. Kein realer Symlink-/Junction-Erzeugungsversuch nötig.

Regression: 90 Tests aus der neuen Suite sowie
`test_f01_alias_ingestion_design.py`, `test_f01_survivor_resolution.py` und
`test_xbrl_fact_upsert.py` bestanden. Nach der zusätzlichen Admission-/Statusprobe
bestanden final **44 Tests** aus der neuen Suite und `test_database.py`
(49,08 Sekunden). Ruff für alle geänderten Python-Dateien und
`git diff --check` bestanden auch final. Nur vorhandene websockets-Deprecation.

Die Matrix prüft Writer-Operationen und bestehende Offline-Reader, keinen echten
Full-/Incremental-Sync, kein Providerverhalten und keine historische Vollgleichheit
auf dem realen Bestand. Fehler-/Recovery während eines realen Batch-Neubaus und
Indexaufbaus gehören weiterhin zu G3/G6.

## Deaktiviertes Migrationstool

`canonical_migration.py` enthält den manifestgebundenen Validator und
`migrate_work`. Der Einstieg scheitert vor Dateizugriff mit
`COPY_REHEARSAL_DISABLED_G0_G2_REQUIRED`; kein CLI-/Environment-Bypass.
Auch bloßes Umlegen der Konstanten führt noch nicht zur Ausführung: Ein echter
G3-Batch-/Resume-Executor verlangt eine separate Implementierung/Review.

Der Validator verlangt Vertrags-/Planbindung, drei verschiedene existierende
Dateien, Hashes, Counts, Input-/Consumer-Inventar, Kapazitätsbudget und Restore-
Evidenz. Er prüft samefile, aufgelöste Pfade, Symlinks/Junctions einschließlich
Eltern, Linkzahl und Work-Sidecars; nur Work wird zum Hashen geöffnet.
Quelle und Backup werden nicht als SQLite geöffnet oder kopiert. Der Validator
ersetzt keine unabhängige Prüfung der eingetragenen Backup-/Restore-Evidenz und
keine gegen Pfadaustausch abgesicherte spätere Executor-Verbindung.

## G0–G2 und Entscheidung

| Gate | Ergebnis |
| --- | --- |
| G0 Scope | Dieser Auftrag erlaubt Implementierung/Offline-Fixtures. Absolute Source-/Backup-/Work-/Result-Pfade und gesonderte Autorisierung für reale Kopien fehlen. Migration und Runtime-Aktivierung bleiben gesperrt. |
| G1 Sicherung/Inputs | Nicht erfüllt: kein konsistentes Backup, Restore, WAL-/Readerzustand, vollständiges Input-/Consumer-Inventar und reserviertes Backup-/Work-/Temp-Budget nachgewiesen. Diese Aktionen wurden auftragsgemäß nicht ausgeführt. |
| G2 Vertrag/Counts | A-V1-Code und synthetischer Gleichheitsnachweis liegen vor. Kein konkretes Snapshot-/Schema-/Reader-/Config-Manifest und keine daraus abgeleitete Membership-Zahl gebunden; daher nicht vollständig erfüllt. |

Nur aus 8E übernommene, hier nicht neu gemessene Erwartungen:
18.698.090 Occurrences, davon 13.764.701 Instant und 4.933.389 Duration;
8.329.908 Canonical, davon 3.396.519 Instant; 180.283 nachvollziehbare Aliasgruppen,
0 bekannte numerische Konfliktgruppen. Keine erwartete Byte-Einsparung und keine
erfundene Membership-Zahl. Diese Werte dürfen nur bei passendem Source-Manifest
zu Sollzahlen einer späteren Ausführung werden.

**BLOCKED**, weil konkrete Ausführungsvoraussetzungen und der geprüfte reale
Batch-/Resume-Executor fehlen. Die synthetischen Ergebnisse schließen diese
Lücken nicht. Ein nächster Auftrag kann G0–G2 anhand ausdrücklich benannter
isolierter Pfade herstellen und den G3-Executor vorbereiten; dieses Arbeitsergebnis
startet weder eine Kopie noch ein Rehearsal.
