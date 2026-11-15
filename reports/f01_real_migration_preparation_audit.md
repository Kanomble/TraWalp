# Task 8F.2A – Reale Rehearsal-Vorbereitung, Phase A / G0-Vorlage

Stand: 15.11.2026, erste Messung 08:35:49 UTC.

**Status: BLOCKED – ausdrückliche Pfad-/Kopierfreigabe ausstehend.**
Phase A ist abgeschlossen. Gemäß Phase A, Punkt 5 des Auftrags wird hier vor
Backup, Restore, Work-Erzeugung und Snapshot-Census gestoppt. G0–G2 sind noch
nicht erfüllt. Keine Migration und kein Aufruf von `migrate_work`.

## 1. Ausführung und Vertrag

| Merkmal | Beobachtung |
| --- | --- |
| Git-HEAD | `3155ec6eb32f9225d8588c5bf54e7d42001c0aa4` |
| Arbeitsbaum vor Untersuchung | sauber |
| Interpreter | `G:\GitHub\TraWalp\.venv\Scripts\python.exe` |
| Python | 3.12.7, Anaconda-Build, 64 Bit, MSC 1929 |
| SQLite | 3.45.3; `TEMP_STORE=1`, `THREADSAFE=1` |
| Betriebssystem | Windows 11, Kernel 10.0.26300, AMD64 |
| A-V1 | `F01_CANON_OCC_V1` |
| Executor | `F01_BATCH_EXECUTOR_V1` |
| Aktivierung | `EXECUTION_ENABLED = False`, unverändert |

Die drei Auftragsberichte sowie `canonical_migration.py` und
`canonical_paths.py` wurden geprüft. Die lokalen Skills code-search und
python-tools wurden verwendet. Keine Providerkonstruktion, keine Synchronisierung,
keine Shadow-Aktivität, keine Strategieänderung, keine Testläufe, kein Commit/Push.

## 2. Tatsächliche Konfiguration und lokale Overrides

Standardkonfiguration:
`G:\GitHub\TraWalp\config\strategy.yaml`, `storage.database_path` =
`data/trading_system.sqlite3`. `load_strategy_config()` löst relative Speicherpfade
gegen die Konfigurationsbasis auf, unabhängig vom aktuellen Arbeitsverzeichnis.

**Konfigurierte Produktivquelle / Production Origin:**
`G:\GitHub\TraWalp\data\trading_system.sqlite3`.

`load_settings()` lädt `.env`, überschreibt damit aber nicht den Storage-Pfad.
Die lokale `.env` enthält ausschließlich Provider-/Trading-Schlüsselnamen und
keinen DB-/Config-Pfad. Geheimniswerte wurden weder ausgegeben noch in Artefakte
übernommen. In der Prozessumgebung wurde kein einschlägiger DB-Pfad-Override
gefunden. Der CLI unterstützt ausdrücklich `--config`; bei diesem Auftrag wurde
kein abweichender Konfigurationspfad übergeben.

Die lokalen `.idea`-XML-Dateien und vorhandenen Run-Configuration-Dateien wurden
auf konkrete Config-/Working-Directory-Overrides geprüft. Gefunden wurde nur
`$PROJECT_DIR$/scripts` als Arbeitsverzeichnis, kein abweichendes `--config`.
Andere Benutzerprozesse oder externe, hier nicht sichtbare Aufrufskripte können
damit nicht allgemein ausgeschlossen werden; sie sind vor dem Sicherungsfenster
zu koordinieren. Es wurde kein CLI-Einstieg ausgeführt.

## 3. Ausschließlich lesende Bestandsaufnahme

Es wurde **keine SQLite-Verbindung zur Produktivquelle** geöffnet. Gelesen wurden
Dateimetadaten und die ersten 100 Bytes des SQLite-Headers. Nur eine separate
`:memory:`-Verbindung diente der Ermittlung der SQLite-Compile-Optionen.
Insbesondere kein `initialize`, Checkpoint, Recovery, VACUUM oder Journalwechsel
an der Quelle. Auch eine normale WAL-Read-only-Verbindung könnte SHM-/WAL-Zustand
berühren; diese wurde bewusst nicht verwendet.

| Datei | Größe | Letzte Änderung UTC |
| --- | ---: | --- |
| `data\trading_system.sqlite3` | 10.501.169.152 Bytes (ca. 9,78 GiB) | 09.11.2026 18:23:12,928785 |
| `data\trading_system.sqlite3-wal` | 0 Bytes | 09.11.2026 18:29:13,590173 |
| `data\trading_system.sqlite3-shm` | 32.768 Bytes | 12.11.2026 19:14:59,034245 |
| `data\trading_system.sqlite3-journal` | nicht vorhanden | – |

SQLite-Header: gültige Signatur, Read-/Write-Version jeweils 2, daher persistenter
WAL-Modus; Seitengröße 4.096 Bytes und Header-Page-Count 2.563.762. Dies ist eine
Header-Beobachtung und kein aus `immutable=1` irreführend abgeleiteter
`PRAGMA journal_mode`-Wert. Keine SQL-Abfrage und kein Inhalts-Census an der Quelle.

Datei-ID der Hauptdatei: `[3758399467636927046, 5910974510933108]`.
Alle drei vorhandenen Dateien haben Linkzahl 1; keine Symlink-/Junction-Komponente
wurde in den geprüften Pfaden gefunden. Größe, mtime und Datei-Identitäten waren
vor/nach der Untersuchung unverändert. Ein vollständiger Source-SHA-256 ist vor
dem abgestimmten Sicherungsfenster noch nicht erhoben; der Headerhash ist kein
Snapshot-Hash.

### Laufende Zugriffe

Die native Windows-Restart-Manager-Abfrage scheiterte bei `RmStartSession` mit
Rückgabecode 29. Deshalb liegt **keine vollständige PID-Zuordnung der DB-Handles**
vor. Es wurde keine administrative Eskalation oder Prozessbeendigung versucht.

Zusätzliche, sofort wieder geschlossene native Read-Handles ohne Sharing ließen
sich auf Hauptdatei, WAL und SHM erfolgreich öffnen: An diesen Messzeitpunkten
gab es keine damit inkompatiblen offenen Datei-Handles. Dies ist keine Garantie
eines zukünftigen writerfreien Zeitfensters. Der Prozess-Snapshot zeigte zwei
PyCharm-Prozesse (PIDs 20348 und 20964), keine sichtbaren Python-/SQLite-/Trading-
Prozesse aus der gezielten Namenssuche. Scheduled Tasks, neue Zugriffe oder
verbliebene Memory-Mappings sind damit nicht pauschal ausgeschlossen.

**G1 benötigt weiterhin ein ausdrücklich abgestimmtes Zeitfenster ohne Syncs,
Writer und Datenänderungen sowie erneute Handle-/Dateizustandsprüfung.**

## 4. Kapazität und konkrete Abbruchgrenzen

Native `GetDiskFreeSpaceExW`-Messung, verfügbar für den ausführenden Benutzer:

| Volume / Nutzung | Verfügbar | Gesamt |
| --- | ---: | ---: |
| G: / Quelle und vorgeschlagene Kopien | 203.288.526.848 Bytes (189,33 GiB) | 266.197.790.720 Bytes |
| C: / aktuelles Windows-Temp | 165.992.181.760 Bytes (154,59 GiB) | 999.122.817.024 Bytes |

`TEMP`, `TMP` und `GetTempPathW` zeigen derzeit auf
`C:\Users\LUKASB~1\AppData\Local\Temp`. `TMPDIR` und `SQLITE_TMPDIR` sind nicht
gesetzt. Die PowerShell-PSDrive-Abfrage lieferte keine belastbaren Free-Werte;
das Budget verwendet ausschließlich die nativen Messwerte.

Für die spätere Vorbereitung/Sortierung wird ein eigener Scratch-Pfad auf G:
vorgeschlagen. Im ausdrücklich gestarteten Folgeprozess müssen `TMP`/`TEMP`
darauf gesetzt und `GetTempPathW` vor dem SQL-Lauf erneut geprüft werden. Die
globale Benutzer-/Systemkonfiguration bleibt unverändert. Bis dahin bleibt C:
das tatsächlich beobachtete Temp-Volume.

Konservative Budgetbasis S = 10.501.169.152 Bytes. Die Faktoren sind reservierende
Planannahmen, keine gemessene Größe des migrierten realen Bestands.

| Position | Zusätzlich vorgesehen |
| --- | ---: |
| Unveränderte Produktivquelle | bereits vorhanden, weitere 0 S; separat 1 S Bestand |
| Sidecarfreier Source-Snapshot | 1 S |
| Unabhängiges verifiziertes Backup | 1 S |
| Restore-Test, bis zur Abnahme erhalten | 1 S |
| Initiale Work-Datei einschließlich später erhaltenem Import-Archiv und Nicht-Fact-Daten | 1 S |
| Neue Canonical Facts, Occurrences und Revisionen | 3 S |
| Zusätzliche Indizes und Ledger | 2 S |
| SQLite-Rollback-Journal / Recovery-Spielraum | 2 S |
| Sortier-/Index-Scratch | 2 S |
| Zusätzlicher Unsicherheitszuschlag | 2 S |
| Feste Betriebsreserve | 20 GiB |

**Mindestfreiplatz vor Freigabe eines späteren Laufs auf G:**
15 S + 20 GiB = **178.992.373.760 Bytes (166,70 GiB)** zusätzlich zum vorhandenen
Quellbestand. Gemessener Abstand dazu: 24.296.153.088 Bytes (22,63 GiB).
Speicher ist nicht tatsächlich reserviert. Für einen zusätzlichen getrennten
Ergebnis-DB-Export wäre ein weiteres Budget nötig; hier ist Ergebnis = Work
nach einer erst später separat autorisierten In-place-Migration.

Falls Temp nicht nach G gebunden werden kann, ist auf C zusätzlich mindestens
2 S + 20 GiB = **42.477.174.784 Bytes** frei zu halten; das konservative G-Budget
wird dabei nicht automatisch abgesenkt. Jede relevante Änderung von S, WAL-Volumen,
Snapshot-Inhalt oder Ergebnisstrategie verlangt eine neue Budgetprüfung.
Keine erwartete Speicherersparnis aus weniger Canonical-Facts abgeleitet.

Vorgeschlagene Überwachung für den späteren Lauf:

- Vor Beginn und jeder Phase verfügbaren Platz, Work-/Journal-/Scratch-Größen
  und verbleibendes Budget prüfen. Vor finalem Indexaufbau zusätzlich mindestens
  2 S Journal + 2 S Scratch + 20 GiB auf dem betroffenen Volume verfügbar halten.
- Alle fünf Sekunden nativen Freiplatz und Dateiwachstum protokollieren; ab
  30 GiB Restplatz warnen. Kein neuer Batch/Indexschritt, sobald verbleibender
  Bedarf + 20 GiB Reserve nicht mehr gedeckt ist.
- Bei weniger als **20 GiB** auf einem betroffenen Volume, Budgetüberschreitung,
  Source-/Backup-Drift oder unerwarteter Writer-Aktivität kontrolliert abbrechen.
  Aktuelle Transaktion durch SQLite zurückrollen lassen; committed Ledger erhalten.
  Bei hängendem/fehlerhaftem Abbruch keine Referenzdateien oder Sidecars löschen.

Diese Überwachung ist eine G0–G2-Betriebsvorgabe, noch kein gestarteter Watchdog.
Der Executor enthält die atomaren Recovery-Grenzen, aber keine automatische
Kapazitätsreservierung. Die fünfsekündige Beobachtung ersetzt nicht die Reserven.

## 5. Zur ausdrücklichen Freigabe vorgeschlagene Pfade

Basisverzeichnis:
`G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a`.
Es wurde noch nicht angelegt; keine der folgenden DB-Dateien wurde erzeugt.

| Rolle | Absoluter Pfad |
| --- | --- |
| Unveränderter Production Origin | `G:\GitHub\TraWalp\data\trading_system.sqlite3` |
| Manifest-Source, separater konsistenter Export | `G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a\reference\source.sqlite3` |
| Backup | `G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a\reference\backup.sqlite3` |
| Restore-Probe | `G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a\restore\restore.sqlite3` |
| Work | `G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a\work\canonical_rehearsal.sqlite3` |
| Späteres Ergebnis, keine zusätzliche Datei | derselbe absolute Work-Pfad |
| Scratch | `G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a\scratch` |

### Warum ein zusätzlicher Source-Snapshot nötig ist

Der bestehende Executor verweigert Source-/Backup-Sidecars ausdrücklich, auch
ein leeres WAL. Die unveränderte konfigurierte Datenbank hat solche Sidecars.
Sie kann deshalb im beobachteten Zustand nicht direkt als Manifest-Source
zugelassen werden. Keine Quelldatei oder Sidecar wird bereinigt, um den Guard
zu umgehen; keine Guard-Aufweichung und kein Checkpoint.

Vorgeschlagene Lösung: Im genehmigten writer-/readerfreien Sicherungsfenster aus
dem unveränderten Production Origin einen **konsistenten SQLite-Backup-Export**
in die gesonderte Source-Datei erstellen. Diese wird ausschließlich am neuen
Ziel auf sidecarfreien Referenzzustand gebracht, geprüft und anschließend nicht
mehr verändert. Von dieser Referenz folgen Backup, Restore und Work. Im Manifest
bleibt `production_origin` zusätzlich explizit erhalten; `source` bezeichnet
den tatsächlich vom Executor gepinnten unveränderlichen Snapshot. Das ist eine
bewusst zur Freigabe vorgelegte Source-Routing-Entscheidung, keine stillschweigende
Umdeklaration der produktiven Konfiguration.

Beim beobachteten leeren WAL kann erst nach erneutem Nachweis eines stabilen,
gesperrten Fensters und unveränderlicher Hauptdatei ein nicht schreibender
SQLite-Backup-Lesepfad gewählt werden. Ein Immutable-Lesepfad darf niemals einen
gefüllten WAL ignorieren. Ändert sich der WAL-Zustand, wird vor Durchführung
neu geplant; eine isolierte Hauptdateikopie ist ausgeschlossen. Kein gewöhnlicher
WAL-Open, der unbemerkt Quell-SHM verändert, und keine Quellenänderung sind durch
die vorgeschlagene Freigabe gedeckt.

## 6. Ausstehende Phasen B und C

Erst nach ausdrücklicher Freigabe der obigen Pfade, Kopieraktionen und des
Sicherungsfensters:

1. Konsistenten, sidecarfreien Source-Export erstellen und prüfen. Physische
   Source-/Origin-Identitäten und Hashes sowie unveränderte Origin-Zustände
   dokumentieren. Source/Backup/Work dürfen keine Symlinks oder Hardlinks sein.
2. Backup erstellen, SHA-256, `integrity_check`, `foreign_key_check`, vollständiges
   Schema, Tabellenzähler und Snapshot-Identität prüfen. Unabhängige Restore-Probe
   mit erneuter Verifikation durchführen; Work bytegleich aus dem verifizierten
   Backup erzeugen. Alle Verbindungen schließen, Referenzzustand erneut prüfen.
3. Vollständige Counts und Inhalte am gebundenen Snapshot erheben: Occurrences,
   Instant/Duration, NULL-sichere Canonical-Identitäten, Source-Memberships,
   Aliasgruppen und vollständige Inhaltskonflikte. Definition der Membership
   (Fact/Symbol sowie Fact/Symbol/Taxonomy) explizit halten. Keine automatische
   Toleranz gegenüber den bisherigen Auditwerten.
4. Alle DB-Tabellen/Objekte/PRAGMAs sowie Companies, Assets, Identity-/SEC-/Accession-
   und SIP-/Marktdaten inventarisieren, externe SQL-/Schema-/ID-/Cache-Consumer
   prüfen und Code-/Config-/Input-Fingerprints binden. Keine Netz-Nachfüllung.
5. Exakt die vorhandenen `file_hash`, `sql_schema_hash` und `fact_stream_hash`-
   Verträge verwenden. `file_identities` = `[st_dev, st_ino]`; Work- und
   Backup-Dateihash müssen initial gleich sein. Fertiges Manifest mit
   `validate_work_manifest` prüfen, **niemals `migrate_work` aufrufen**.

Die bisherigen Größen aus 8E (18.698.090 Occurrences, 8.329.908 Canonical,
180.283 Aliasgruppen) sind Vergleichswerte, keine hier neu bestätigten Sollzahlen.
Unbekannte Membership-Zahlen und Hashes werden nicht erfunden. Phase C beginnt
nicht vor der Freigabe/Erstellung eines verifizierten Snapshots.

## 7. Artefakte und Entscheidung

- `reports/f01_preparation_phase_a_observations.json`: lokale Messwerte,
  Dateimetadaten, Handle-/Restart-Manager-Ergebnisse und Code-Fingerprints.
- `reports/f01_copy_rehearsal_manifest.json`: **nicht ausführbarer Entwurf**,
  `DRAFT_PENDING_EXPLICIT_G0_APPROVAL`, `execution_ready=false`.
  SHA-256, Datei-Identitäten der erst zu erstellenden Referenzen, Schema-/Stream-
  Hashes, Sollzahlen, Input-/Consumer-Inventar und Restore-Evidenz bleiben NULL.
  Das Entwurfsmanifest wurde nicht als vollständiges Executor-Manifest validiert.

Alle drei lokalen Vorbereitungsartefakte liegen unter den bestehenden
`.gitignore`-Regeln. Keine Freigabe zur Veröffentlichung sensibler Betriebsdaten,
kein Staging und kein Upload. `.gitignore`, Executor und Produktivkonfiguration
wurden nicht geändert.

**BLOCKED:** Es fehlen die vom Benutzer in Phase A, Punkt 5 verlangte
ausdrückliche Pfad-/Kopierfreigabe sowie das abgestimmte Sicherungsfenster.
Zusätzlich muss die separate sidecarfreie Manifest-Source ausdrücklich akzeptiert
werden. Es wurde keine Backup-/Restore-/Work-Datei erstellt und kein
Migrationsschritt ausgeführt. Diese Freigabe wäre ausschließlich für die weitere
Vorbereitung G0–G2; sie wäre keine Migrations- oder Writer-Aktivierungsfreigabe.
