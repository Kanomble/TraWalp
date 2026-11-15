# Task 8F.1B – Executor, Performance und Transaktionshärtung

Datum: 15.11.2026. Ausgangs-HEAD: `8fa0e4bc34da508ca42a02cf141ad188906edadf`.
Arbeitsbaum zu Beginn sauber. Referenzen: Task-8F.1-Implementierungsbericht,
Task-8E-Alias-/Ingestion-Design und die beiden Canonical-Implementierungsmodule.

**Abschlussstatus: EXECUTOR_READY_FOR_G0_G2, im geprüften Windows-Umfeld.**
Dies ist die technische Bereitschaft zur nachfolgenden G0–G2-Prüfung, keine
Freigabe eines realen Rehearsals. Der Dateieinstieg bleibt mit
`EXECUTION_ENABLED = False` deaktiviert. Keine CLI-/Sync-Registrierung und kein
Environment-Bypass. Tests aktivieren ausschließlich den Einstieg für selbst
erzeugte Wegwerfdateien. Andere Betriebssysteme verweigern die Ausführung, solange
kein gleichwertiger obligatorischer Pfadschutz implementiert ist.

## Umfang

Keine produktive Datenbank geöffnet, gehasht, kopiert oder verändert. Keine reale
Migration, Providerzugriffe, Synchronisierung, Shadow-Inception oder Shadow-Runs.
Keine Änderung an Champion, Parser, PIT-Regeln oder Selektoren. Kein Commit/Push.
Alle SQLite-Dateien, Datei-Kopien, Hash- und Schutzproben liegen in synthetischen
pytest-Verzeichnissen. Kein erneuter Census oder Alias-Audit.

## A – Query-Pläne und Transaktionen

### Reproduzierter Lookup-Fehler

Die ursprüngliche generische Identitätsabfrage mit `period_start IS ?` ergab auf
dem Schema aus 8F.1 tatsächlich:

```text
SCAN canonical_fact
```

Die Regression reproduziert diesen alten Plan ohne den neu hinzugekommenen
Forward-Reader-Index. Dieser zusätzliche Index kann den generischen Lookup zwar
teilweise einschränken, ersetzt aber keinen vollständigen Identity-Index.

`lookup_query` und `_lookup` verwenden jetzt zwei explizite SQL-Formen:

```text
Instant:  period_start IS NULL AND <acht Nicht-NULL-Keyspalten>=?
          SEARCH canonical_fact USING INDEX canonical_instant (...)
Duration: period_start IS NOT NULL AND <neun Keyspalten>=?
          SEARCH canonical_fact USING COVERING INDEX canonical_duration (...)
```

Keine Sentinels, kein Symbol im Canonical-Key. `LIMIT 2` erhält die
Ambiguitätssperre. Alle ursprünglichen NULL-/Optional-Key-Regeln bleiben bestehen.

### Reader-Projektionen

Die aktuellen Views lesen unmittelbar Canonical + Occurrence. Der frühere Umweg
über sämtliche Dataset-Versionen und einen korrelierten Revision-Lookup entfällt.
Historische `*_occ_versions_v1`-Views behalten die explizite Dataset-Version und
den indizierten Revision-Lookup; alte Werte bleiben lesbar.

Neue/gezielt eingesetzte Indizes und beobachtete Pläne:

| Reader | Plan |
| --- | --- |
| Legacy, exaktes Symbol | `SEARCH o USING INDEX occurrence_legacy_reader (symbol=?)`; anschließend Canonical per Integer-PK. |
| Forward, autorisierte CIK und Filed-Cutoff | `SEARCH f USING INDEX canonical_forward_reader (cik=? AND filed<?)`; Occurrences über `occurrence_membership (fact_id=?)`. |
| Historische Revision | Vorhandener `(fact_id,version)`-PK unterstützt die neueste Revision bis zur expliziten Dataset-Version. |

Die selektiven Legacy-/Forward-Abfragen scannen weder Canonical noch Occurrence
vollständig und verwenden keinen aktuellen Dataset-/Revision-Join. Explizites
`ORDER BY filed,period_end,id` bleibt erhalten. SQLite sortiert verbleibende Ties
mit temporärem B-Tree; dieser wird nicht durch eine andere Reihenfolge ersetzt.
Ungefilterte Vollstream-Abnahmen müssen weiterhin alle Occurrences lesen.

### Reproduzierter Commit-Fehler und Korrektur

Eine echte deferred Foreign-Key-Verletzung beim abschließenden Commit erzeugte
ursprünglich zuerst `IntegrityError: FOREIGN KEY constraint failed`, der danach
durch `OperationalError: no such savepoint: canonical_company` verdeckt wurde.

`atomic` trennt nun zwei Fälle:

- Eigentümer: `BEGIN IMMEDIATE`, Commit; bei Body-/Commit-Fehler Rollback der
  gesamten eigenen Transaktion, ohne Zugriff auf einen freigegebenen Savepoint.
- Externe Transaktion: eigener Savepoint, kein Commit; bei Fehler nur eigene
  Änderungen zurückrollen. Vorherige Änderungen des Eigentümers bleiben erhalten.

Der ursprüngliche Fehler wird erneut ausgelöst. Ein zusätzlicher Cleanup-Fehler
wird als Exception-Note angehängt und verdeckt ihn nicht. Tests prüfen den echten
Commit-Fehler, dessen Rollback und externe Transaktionsgrenzen.

### Sparse Revisionen

Unveränderte vier korrigierbare Inhalte erzeugen weder Canonical-UPDATE noch neue
Wertrevision. Verglichen wird die gespeicherte Repräsentation einschließlich
Fiscal Year/Period und Form, ohne neue numerische Normalisierung einzuführen.
Neue Requests behalten Receipt und Dataset-Version. Wiederholung desselben
Requests bleibt idempotent. Echte Korrekturen erzeugen Revisionen; alte
Dataset-Streams und Filed-PIT bleiben unverändert. Probe: zwei neue unveränderte
Requests erzeugen drei Dataset-Versionen/Receipts, aber nur eine Wertrevision;
anschließende Korrektur ergibt die zweite Revision und alte Werte bleiben 10.

## B – Batch-/Resume-Executor

`canonical_facts.py` trennt DDL, begrenzten Zeilenimport und Veröffentlichung.
`canonical_migration.py` ergänzt den bisher fehlenden Dateieinstieg und
Connection-Executor. Das kleine `build_snapshot` bleibt ein atomarer
Fixture-Helfer und wird nicht für große Dateimigrationen verwendet.

1. Manifest-/Dateibindung prüfen; Work ausschließlich mit SQLite `mode=rw` öffnen.
   Source/Backup werden niemals als SQLite geöffnet oder als Ziele benutzt.
2. Input-DDL-/Schema-Metadatenhash und vollständigen geordneten Fact-Stream-Hash
   prüfen. Unabgenommene Views, Trigger oder Fremdschlüssel auf Facts stoppen vor
   dem Umbau. Das alte Fact-Table wird als unveränderliches Work-Archiv erhalten.
3. Vorbereitung atomar committen: Canonical-DDL, Identitäts-UNIQUE-Indizes und
   persistenter `BUILDING`-Status. Reader-Views sind noch nicht veröffentlicht.
4. Keyset-Batches `id > last_id ORDER BY id LIMIT batch_size` importieren.
   Standard 10.000, zulässig 1–50.000 Zeilen. Original-IDs bleiben Occurrence-IDs;
   Canonical-ID ist deterministisch die erste Original-ID der Identität.
5. Pro Batch werden Facts, Revisionen, Occurrences, Cursor und Ledger gemeinsam
   committed. Ledger enthält ID-Grenzen, Zeilenanzahl, Mappinghash und verketteten
   Commit-Hash. Kein Count-/Cursor-Update außerhalb des Batch-Commits.
6. Resume verlangt ausdrücklich `resume=True`, dieselbe Manifest-/Executor-Version,
   dieselben Dateiidentitäten und geschützten Snapshot-Hashes. Sämtliche
   committed Ledger-Bereiche, Mappinghashes, Counts, Cursor, Archivinhalt und
   Staging-Schema werden geprüft; nicht nur der letzte Cursor.
7. Reader-/Membership-Indizes werden erst nach vollständigem Import kontrolliert
   aufgebaut. Identitäts-Indizes bleiben während des Imports aktiv. Views,
   Snapshot-Receipt, Schema-Signatur und `COMPLETE` werden atomar veröffentlicht,
   nachdem Counts, beide vollständigen geordneten Reader-Streams,
   `integrity_check` und `foreign_key_check` bestanden sind.

SQLite verwendet DELETE-Journal, `synchronous=FULL`, dateibasierte temporäre
Sortierung und einen Cache-Zielwert von 8 MiB. Ein Hot-Journal wird nur beim
expliziten Resume auf Work zugelassen und von SQLite recovered. Source/Backup
müssen sidecarfreie, konsistente Snapshots sein. Kein WAL-/SHM-Löschen und kein
Recovery gegen Source/Backup. Inkompatibler Resume stoppt, statt Daten zu skippen.

Finalisierung ist bewusst ein atomarer, neu startbarer Index-/Abnahmeschritt,
kein zeilenweiser Commit während des Indexaufbaus. Ein Prozessabbruch rollt diesen
Schritt zurück; bereits bestätigte Import-Batches bleiben gültig.

### Pfadaustausch und Schutz der Referenzdateien

`canonical_paths.py` hält native Windows-Handles bis nach dem Schließen der
Work-SQLite-Verbindung. Alle Elternverzeichnisse und Dateien verweigern
Delete-Sharing, Source/Backup zusätzlich Write-Sharing. Dadurch können sie im
kritischen Validierungs-/Öffnungsfenster nicht umbenannt/ersetzt werden, auch
nicht über einen austauschbaren Elternpfad. Erst unter diesen Pins werden
Symlink-/Junction-/Hardlink-/samefile-Prüfungen und Hashprüfungen durchgeführt.

Das Ausführungsmanifest bindet absolute eindeutige Pfade, Volume-/Datei-IDs,
Source-/Backup-/initialen Work-Hash, SQL-Schemahash einschließlich User-Version
und Application-ID, Fact-Stream-Hash, Plan, Sollzahlen und G0–G2-Evidenz.
Initial muss Work bytegleich zum unveränderlichen Backup sein. Beim Resume wird
die erwartbar geänderte Work-Datei durch Datei-ID, Manifestbindung, Archiv und
Commit-Ledger statt durch ihren ursprünglichen Datei-Hash geprüft.

Native Tests verweigern Rename von Source/Backup/Work und eines Elternpfads,
Datei-Schreiben und SQLite-DDL auf den geschützten Referenzen. Ein vor dem Öffnen
bytegleich ausgetauschtes Work wird an der abweichenden Datei-ID erkannt.
Source-/Backup-Hashes sind nach allen Tests unverändert.

Dies schützt die angegebenen Referenzdateien gegen gewöhnliche OS-Dateizugriffe,
nicht gegen einen Administrator, der Kernel-Schutz umgeht. Die Arbeitsumgebung
muss weiterhin isoliert sein; beliebige fremde Rohschreiber auf Work sind kein
unterstützter Betriebsmodus. Auf nicht unterstützten Plattformen kein schwächerer
Stat-/Open-Fallback.

### Runtime und isolierter SEC-Adapter

`Database.connect` und `read_only` verweigern jeden nicht vollständigen
Migrationstatus, bevor ein Consumer die Connection erhält. `require_contract`
schützt zusätzlich den A-V1-Writer. Auf vollständig publiziertem A-V1 validiert
`initialize` den Vertrag und kehrt ohne Legacy-DDL/Indexanlage auf Views zurück.
Alte Datenbanken behalten ihre Initialisierung und Reader.

`canonical_sec_adapter.py` stellt einen ausdrücklich instanziierten,
anbieterfreien `IsolatedCanonicalSecWriter` für spätere G5-Proben bereit. Er
öffnet nur bestehende Dateien, validiert A-V1 und verwendet die gezielte
Tabellen-/Operations-Allowlist. Externe Transaktionen werden nicht committed;
ungültige Write-Operationen scheitern. Keine Registrierung in DataSynchronizer,
SEC-Force, CLI oder produktiver Initialisierung. Der Aufrufer muss die isolierte
G5-Work-Datei explizit wählen; der Adapter ist kein autonomer Pfad-/Provider-Router.

## C – Synthetische Nachweise und Skalierung

Neue Suite: `tests/test_f01_migration_hardening.py`.

- Alte Scan-Abfrage und ursprünglicher verdeckter Commit-Fehler reproduziert;
  neue Instant-/Duration- und Reader-Pläne mit EXPLAIN geprüft.
- Vollständige Streams, Alias-/Source-Multiplizität, Batch/DENSE_RANK,
  Score-/Ranking-Parität und historische sparse Revisionen geprüft.
- Echte Child-Prozesse mit `os._exit(73)` vor/nach Batch-Commit, nach Indexaufbau
  und nach Publish-Commit beendet; Resume ohne Zeilenverlust/Doppelimport,
  auch mit geänderter Batchgröße. Mapping A/B/A bleibt `(1,1),(2,2),(3,1)`.
- Initiale Hashdrift, Resume-Manifest-/Schema-/Ledger-/Archivdrift und inhaltlicher
  Fact-Konflikt stoppen. Bereits committed Teilimport bleibt für Runtime gesperrt.
- Commit-FK-Fehler, externe Savepoint-Rollbacks, Adapter-Guard und
  Initialisierung auf alten, unvollständigen und vollständigen Datenbanken geprüft.
- Native Pfad-/Schreibschutzproben und geschützte Referenzhashes geprüft.

Validierungsergebnis:

- Breite gezielte Regression über Executor, A-V1, 8E-/Survivor-Readerbeweise,
  XBRL-Upsert und Database: 121 Tests bestanden; zwei zusätzliche Fälle meldeten
  ausschließlich eine abweichende Exception-Zeichenfolge bei korrekt verweigerter
  Work-/Backup-Hashbindung. Der Fehlercode wurde auf `WORK_BACKUP_HASH_MISMATCH`
  vereinheitlicht.
- Finale Nachprüfung: **26 bestanden in 28,55 s**, darunter sämtliche aktuellen
  Executor-Funktionstests und der betroffene alte Manifest-Test. Zusätzlich
  User-Version-/Status-Drift, nichtpositive Original-IDs und Erhalt des ursprünglichen
  Fehlers bei ebenfalls fehlschlagendem Cleanup geprüft.
- **2 Skalierungstests bestanden in 10,86 s**; Details unten. Diese werden nicht
  ohne Anlass in der mehrminütigen Reader-Regression wiederholt.
- Ruff für alle geänderten/neuen Python-Dateien und `git diff --check` bestanden.
  Einzige Testwarnung: vorhandene websockets-Legacy-Deprecation.

Die Läufe überschneiden sich; die Zahlen sind keine Summe unabhängiger Tests und
keine Behauptung eines vollständigen Repository-Testlaufs. Es bleiben keine
fehlgeschlagenen Nachprüfungen offen.

Begrenzter Skalierungstest: gemischte Instant-/Duration-Identitäten; Instants
erscheinen dreifach einschließlich historischem Alias, Duration-Facts einmal
(der alte Duration-UNIQUE-Vertrag verbietet dort identische Wiederholungen).
Gemessen wurde der Executor einschließlich Hashprüfung, Commit, Indexaufbau und
Abnahme; Fixture-Erzeugung war ausgeschlossen. Kein historischer Backtest.

| Kennzahl | Kleine Fixture | Größere Fixture |
| --- | ---: | ---: |
| Occurrences / Canonical | 4.000 / 2.000 | 16.000 / 8.000 |
| Batchgröße / Batch-Commits | 250 / 16 | 1.000 / 16 |
| Executor-Laufzeit | 2,508 s | 5,223 s |
| Durchsatz | 1.595 Zeilen/s | 3.063 Zeilen/s |
| Vollständiger Forward-Read | 0,0155 s | 0,0585 s |
| Python-Spitze (tracemalloc) | 2.150.309 Bytes | 2.154.005 Bytes |
| RSS vor / nach Executor | 121.405.440 / 121.413.632 Bytes | 125.677.568 / 126.074.880 Bytes |
| Prozessweite Peak-RSS | 124.534.784 Bytes | 144.936.960 Bytes |
| Input-Datei | 1.060.864 Bytes | 3.837.952 Bytes |
| Work-Datei / belegte Pages | 2.248.704 Bytes | 8.355.840 Bytes |

Python-Spitze enthält keinen nativen SQLite-Speicher; RSS ist prozessweit und
Peak-RSS enthält auch Fixture-/Testkosten. Der Cache-Zielwert ist kein gemessener
harter Gesamtspeicherdeckel. Die Query-Laufzeit umfasst die Materialisierung der
vollständigen Ergebnisliste nach der Executor-Speichermessung. Keine Hochrechnung
auf den realen Bestand; kein behaupteter Storage-Gewinn. Einzelmessungen unter
tracemalloc, keine produktiven Latenz-SLAs. Mess-JSON liegt in den ignorierten
synthetischen Testverzeichnissen, die wesentlichen Werte sind hier festgehalten.

## Grenzen und nächste Gates

G0–G2 bleiben vor jeder realen Ausführung erforderlich: explizit autorisierte
absolute Pfade, konsistentes Backup/Restore, vollständiges Input-/Consumer-
Inventar, Snapshot-/Schema-/Code-/Reader-/Config-Bindung, tatsächliche Sollzahlen,
Kapazitätsbudget und Überwachung für Work, Archiv, Journal und Index-Scratch.
Der Executor erzeugt keine reale Sicherung und prüft keine behauptete
Restore-Evidenz durch Zugriff auf die Produktion. Kein realer Consumer- oder
Performance-Nachweis wird aus den kleinen Fixtures abgeleitet.

Die G5-Integration in eine spätere Fehlertelemetrie-/Readiness-Kette und eine
reale G4/G6-Abnahme sind nicht aktiviert. Alte Research-Artefakte bleiben
unverändert. Der physische Schema-/Dataset-Hash ist neu; keine Gleichheitszusage
für technische Fingerprints.

Die erfolgreiche synthetische Abnahme beseitigt den fehlenden G3-Executor als
Implementierungsblocker aus 8F.1. Sie autorisiert weder Copy-Rehearsal noch
produktiven Writer oder Cutover.
