# XBRL Instant-Fact Correctness Audit — Task 8

## Basis und Umfang

Basis-HEAD: `1e81e1a4c009d03bcec698950b0caabdbbb96661`, sauberer Arbeitsbaum beim Start.
Offline-Untersuchung von F01 aus `reports/full_codebase_strategy_audit.md`.
Keine Providerzugriffe, realen Syncs, Backtests, Shadow-Init/Run, Bestandsbereinigung,
Schema-/Datenmigration oder Commits/Pushes. Tests schreiben nur temporäre SQLite-Fixtures.

## Root Cause und rekonstruierter Vertrag

`fundamental_facts` besitzt einen UNIQUE-Key über
`(cik, metric, tag, unit, period_start, period_end, filed, accession_number, frame)`.
`_fact_rows` speichert fehlenden `period_start` als SQL NULL. SQLite behandelt NULLs
im UNIQUE-Key nicht als gleich; wiederholte Instant-Facts lösen deshalb den bisherigen
`INSERT ... ON CONFLICT DO UPDATE` nicht aus.

Beide produktiven Schreibwege (`Database.upsert_facts` und der atomare
`upsert_sec_company_update`, einschließlich extern übergebener Verbindung) verwenden
`_execute_fact_upsert`. Die Rückgabe zählt eingehende Facts, nicht neu eingefügte Zeilen.
`_fact_rows` normalisiert fehlende Accessions und Frames weiterhin auf den leeren String.

Der Parser behält unterschiedliche Filed-/Accession-/Perioden-/Unit-Beobachtungen.
Innerhalb einer Providerantwort dedupliziert er pro Metric, Filingdatum, Start-/Endperiode,
Accession und Unit; Alias-Priorität bleibt unverändert. Dieser Patch verändert den Parser nicht.

Der Legacy-Reader filtert weiter `symbol` und `filed <= as_of` und sortiert nach
`filed, period_end, id`. `LocalInputs` filtert weiter verifizierte CIK, Filingdatum und
gegebenenfalls Metric und sortiert identisch; Source-Ticker bleiben erhalten.
`balance_sheet_as_of` selektiert das Maximum aus Periodenende und Filingdatum.
Bei einem vollständigen Gleichstand bleibt die erste gelieferte Zeile Sieger.
Darum kann die ältere Duplikatzeile die Korrektur verdecken. Unterschiedliche
Amendments bleiben eigenständige Facts, später eingereichte Amendments sind vorher
nicht sichtbar. Eine Providerkorrektur mit exakt gleicher Identität ersetzt wie beim
bisherigen Duration-Upsert den Wert; sie erzeugt keine neue Availability-Version.

## NULL-sichere Identität und kleinste Korrektur

Der Identitätsumfang des bestehenden UNIQUE-Keys bleibt erhalten. Für Instant-Facts
bedeutet der fehlende Start ausdrücklich `period_start IS NULL`; für Duration-Facts
bleiben konkrete Startdaten getrennt. Es gibt keinen Sentinel, keine Umdeutung von NULL
in ein reales Datum und keine Vermischung von Instant und Duration.

- Unterschiedliche CIKs, Metrics, Tags, Units, Start-/Endperioden, Filingdaten,
  Accessions und Frames bleiben getrennt.
- Wert, Fiscal Year/Period und Form sind wie bisher korrigierbare Metadaten.
- Symbol und Taxonomy werden wie bisher bei Konflikten nicht überschrieben;
  beide werden durch diesen F01-Patch nicht zu neuen Identitätsdimensionen gemacht.
- Ein fehlender Accession/Frame und ein leerer String bleiben absichtlich äquivalent.
- Die Zeilen-ID einer Korrektur bleibt stabil. Gemischte Instant-/Duration-Batches
  behalten ihre Eingabe-/Einfügereihenfolge, damit die Reader-Tie-Breaks erhalten bleiben.

Vor der NULL-sicheren Suche erwirbt ein Standalone-Batch mit `BEGIN IMMEDIATE` den
SQLite-Writer-Lock. Im SEC-Company-Pfad besteht bereits die Schreibtransaktion.
Damit können zwei regelkonforme Writer nicht beide erst Abwesenheit beobachten und
danach dieselbe Instant-Identität einfügen. Ein bestehender einzelner Treffer wird per
ID mit denselben vier Feldern wie bisher aktualisiert; bei keinem Treffer wird eingefügt.
Duration-Facts behalten ihren bestehenden ON-CONFLICT-Pfad und aufeinanderfolgende
Duration-Zeilen werden weiterhin mit `executemany` verarbeitet. Commit/Rollback bleibt
beim bisherigen Connection-Eigentümer; übergebene Transaktionen werden nicht committed.

Bei mindestens zwei bestehenden Treffern bricht der Writer mit
`sqlite3.IntegrityError: AMBIGUOUS_FACT_IDENTITY: explicit migration required` ab.
Er wählt keine Altzeile aus und überschreibt oder entfernt keine Duplikate. Auch identische
Altdubletten gelten als mehrdeutig. Die öffentlichen Transaktionspfade rollen den
Batch beziehungsweise Company-/Facts-/Accession-Update zurück. Externe Connection-
Eigentümer müssen wie bisher Fehler propagieren beziehungsweise ihre Transaktion zurückrollen.

## Red-/Green-Nachweise

Vor Produktionsänderung: **17 neue Fälle fehlgeschlagen**, Exit-Code 1,
14,69 s (`reports/xbrl_instant_fact_red.txt`). Konkrete synthetische Nachweise:
identischer Cash-Instant zweimal ergibt zwei Zeilen statt einer; Korrektur 10 → 20
liefert im Balance-Sheet weiter 10. Diese Zahlen sind ausschließlich Test-Fixture-Werte.

Erster Green-Lauf: **119 bestanden**, 210,72 s. Nach Ergänzung des expliziten
Mixed-Batch-Reihenfolgetests und der Company-/Accession-Rollback-Assertions folgt ein
abschließender Lauf derselben unmittelbar betroffenen Dateien: **120 bestanden**, Exit-Code 0, 233,02 s. Die einzige Warnung betrifft das bereits bestehende `websockets.legacy`-DeprecationWarning. `ruff check` für beide geänderten Python-Dateien besteht; `git diff --check` meldet keine Whitespace-Fehler.

Geprüfte Dateien: `test_xbrl_fact_upsert.py`, `test_database.py`, `test_xbrl_parser.py`,
`test_fundamental_metrics.py`, `test_forward_fact_identity.py`, `test_sync.py`,
`test_sec_daily_change_detection.py`. Sync-Tests verwenden synthetische Provider-Doubles;
es wurde kein echter Sync gestartet. Keine globale Suite oder Backtest-Suite.

Die neuen Tests decken doppelte Instant-Ingestion, komplette Batch-Wiederholung,
Korrekturen samt stabiler ID und aktualisierten Metadaten, alle Identitätsdimensionen,
Duration-Korrektur, Neustart, optionale Identitätsnormalisierung, parallele Writer,
atomare SEC-Company-Updates, gemischte Reihenfolge, Legacy- und Forward-PIT-Reader,
Balance-Sheet-Auswahl vor/nach Amendment-Filingdatum sowie identische und widersprüchliche
Altdubletten mit Batch-/Company-/Accession-Rollback ab.

## Read-only Bestandsbefund

Die vollständige Bestandsprüfung ist abgeschlossen. Sie verwendet ausschließlich SQLite
`mode=ro`, `PRAGMA query_only=ON` und eine Lesetransaktion, niemals `Database.initialize`.
Alle echten Finanzwerte bleiben aus dem Bericht heraus. Identitätsgruppen werden nur
als Zähler beziehungsweise SHA-256-Kennungen ausgegeben. Widersprüche werden mit
`Decimal` numerisch verglichen, sodass unterschiedliche Schreibweisen wie 10/10.0
nicht als widersprüchliche Werte zählen. Gegenstand sind lokale `data/**/*.sqlite3`,
nicht historische Test-Fixtures oder Research-Artefakte.

## Separater Migrationsplan — NICHT ausgeführt

Für korrekte neue Ingestion in eindeutige Identitäten ist keine Migration erforderlich.
Vorhandene Mehrfachidentitäten werden durch diesen Patch bewusst nicht repariert.
Eine eventuelle Bereinigung beziehungsweise DB-seitige Absicherung ist ein eigener,
explizit zu genehmigender Vorgang:

1. Writer stoppen; konsistenten SQLite-Backup-Snapshot einschließlich aller committed
   WAL-Daten erzeugen. Quellbestand unverändert aufbewahren, Identitäts-/Zeilenzähler,
   Schema und betroffene historische Outputs dokumentieren.
2. In einer separaten Arbeitskopie alle NULL-sicheren Gruppen inventarisieren.
   Nicht nur Werte, sondern auch Fiscal-Metadaten, Form, Source-Symbol und Taxonomy
   vergleichen. Die vollständigen Originalzeilen bleiben im Originalbackup erhalten.
3. Widersprüchliche Gruppen anhand archivierter Filing-/Provider-Evidenz manuell
   entscheiden. Größte ID, spätester Import oder höchster Betrag beweisen keine
   fachliche Autorität. Ohne belastbare Entscheidung keine Promotion.
4. Eine neue kanonische Datenbankkopie mit entschiedenen Identitäten erstellen.
   Für gleichwertige Duplikate stabile IDs/Tie-Breaks gezielt erhalten; weder
   unterschiedliche Accessions/Filed-Daten/Units noch Duration-Perioden zusammenführen.
5. Nur in der freigegebenen Kopie einen partiellen UNIQUE-Index auf
   `(cik,metric,tag,unit,period_end,filed,accession_number,frame)` mit
   `WHERE period_start IS NULL` anlegen. Vorherige Durations-Unique-Regel erhalten.
   Der Patch erstellt diesen Index weder in initialize noch beim Upsert.
6. Integrity- und Identitätsprüfungen sowie die hier genannten Offline-Regressionen
   durchführen. PIT-/Balance-Ausgaben für nicht betroffene Gruppen auf Gleichheit
   prüfen, Änderungen betroffener Gruppen separat auditieren. Historische
   Research-Artefakte nicht neu schreiben. Originaldatei behalten.
7. Erst nach expliziter Freigabe kontrolliert auf die neue Kopie umschalten.
   Rollback: Writer stoppen, vollständig konsistenten Originalsnapshot wieder
   aktivieren; zwischenzeitliche neue Ingestion separat sichern und entscheiden,
   statt sie stillschweigend zu verlieren. Ein bloßes Code-Downgrade reaktiviert F01.

Risiken: Verlust echter Amendments durch falsche Gruppierung, unbegründete Auswahl
widersprüchlicher Werte, geänderte ID-Tie-Breaks, benötigter Platz für Backup/Kopie,
Writer-Downtime, historisch anders ausfallende PIT-Ausgaben und geänderte Provenienz-
Digests. Bestehende Journale oder Research-Artefakte dürfen nicht rückwirkend angepasst werden.

## Geänderte Dateien und offene Grenzen

- `src/trading_system/data/database.py`: ausschließlich gemeinsamer Fact-Writer.
- `tests/test_xbrl_fact_upsert.py`: synthetische F01-Regressionen.
- `.gitignore`: Ausnahme für den angeforderten Audit-Bericht.
- `reports/xbrl_instant_fact_correctness_audit.md`: dieser Bericht.

Keine Änderungen an F-Scores/Schwellen, Parser-Aliaswahl, Reader-Sortierung,
Amendment-PIT-Regeln, Schema, Shadow-Journal-Verträgen oder historischen Artefakten.
Die bestehende Code-Fingerprint-Prüfung kann durch diese Produktionscodeänderung
regulär Drift erkennen; dieser Schutz wird nicht umgangen und es wurde kein Journal ausgeführt.

Grenzen: Direkte Raw-SQL-/ältere externe Writer können wegen der unveränderten DDL
weiter NULL-Duplikate erzeugen. Die Garantie gilt für die gemeinsamen produktiven
Fact-Upsert-Pfade. Bestehende Dubletten können bis zur separat freigegebenen Migration
weiter die Reader beeinflussen. Unterschiedliche Accessions mit identischem Filingdatum
behalten ihre bisherige Tie-Semantik; eine neue Amendment-Priorität wäre ein anderer Vertrag.
Für Korrekturen derselben Filing-Identität existiert weiterhin kein separater
Acquisition-Zeitstempel; erhalten bleibt die bisherige Filed-PIT-Semantik.
Zusätzliche indexgestützte Lookups pro Instant-Fact und ein früher Writer-Lock können
Ingestionslaufzeit und Lock-Wartezeiten erhöhen; ein nicht erhaltener Lock muss wie bisher
als Fehler behandelt werden. Kein Produktions-Lasttest wurde ausgeführt.


## Testlauf und Artefakte

| Testdatei | Ergebnis |
| --- | ---: |
| test_xbrl_fact_upsert.py | 18 bestanden |
| test_database.py | 10 bestanden |
| test_xbrl_parser.py | 7 bestanden |
| test_fundamental_metrics.py | 22 bestanden |
| test_forward_fact_identity.py | 4 bestanden |
| test_sync.py | 48 bestanden |
| test_sec_daily_change_detection.py | 11 bestanden |
| Gesamt | 120 bestanden |

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/test_xbrl_fact_upsert.py -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_xbrl_cache --basetemp=.tmp_pytest_xbrl_red
& '.venv/Scripts/python.exe' -m pytest tests/test_xbrl_fact_upsert.py tests/test_database.py tests/test_xbrl_parser.py tests/test_fundamental_metrics.py tests/test_forward_fact_identity.py tests/test_sync.py tests/test_sec_daily_change_detection.py -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_xbrl_cache --basetemp=.tmp_pytest_xbrl_final --junitxml=reports/xbrl_instant_fact_green.xml
```

Rohprotokolle: `reports/xbrl_instant_fact_red.txt`,
`reports/xbrl_instant_fact_green_initial.txt`, `reports/xbrl_instant_fact_green.txt`;
maschinenlesbar: `reports/xbrl_instant_fact_green.xml`.
Reproduzierbarer lesender Inventarscanner: `reports/xbrl_instant_fact_inventory.py`;
Ausgabe: `reports/xbrl_instant_fact_inventory.json`.
Der Scanner macht zunächst den NULL-sicheren Census aus dem abdeckenden UNIQUE-Index;
nur Werte mehrdeutiger Gruppen werden anschließend im sequenziellen Tabellen-Scan
numerisch verglichen. Ein früherer Scan mit zufälligen Index-/Tabellenzugriffen wurde
aus Laufzeitgründen beendet und durch diesen vollständigen Zwei-Pass-Scan ersetzt.
Keine Anwendung der Ingestionskorrektur auf den lokalen Bestand.


## Vollständiger lokaler Bestandsbefund — Ergebnis

Prüftag: 12.11.2026. Unter `data/` wurde genau eine SQLite-Datenbank gefunden:
`data/trading_system.sqlite3` (10.501.169.152 Byte). Vorhandene WAL-/SHM-Sidecars sind
keine separaten Datenbestände. Es wurde eine konsistente read-only Snapshot-Transaktion
verwendet. Größe und Änderungszeit der Hauptdatei sind vor/nach dem Audit identisch;
letzte Hauptdateiänderung weiterhin 09.11.2026. Kein Schreibpfad/Initialize wurde aufgerufen.

| Kennzahl | Anzahl |
| --- | ---: |
| Facts insgesamt | 18.698.090 |
| Zeilen mit NULL-period_start | 13.764.701 |
| Zeilen mit nicht-NULL-period_start | 4.933.389 |
| NULL-sichere Identitätsgruppen mit NULL-period_start | 3.396.519 |
| Davon doppelte Identitätsgruppen | 3.238.932 |
| Zeilen in diesen doppelten Gruppen | 13.607.114 |
| Überzählige Zeilen gegenüber einer Zeile pro Identität | 10.368.182 |
| Gruppen mit mehr als einem numerisch unterschiedlichen Wert | **0** |
| Zeilen in numerisch widersprüchlichen Gruppen | **0** |

Damit ist die lokale Duplikation bestätigt, nicht nur potenziell möglich.
Eine aktuell übergangene numerische Korrektur ist für diesen geprüften Snapshot
nicht nachgewiesen; der entsprechende Mechanismus ist im synthetischen Red-Test bestätigt.
Der größte beobachtete Multiplikator ist 37 Zeilen je Identität.
Es wurden keine Finanzwerte in diesen Bericht oder die Inventar-JSON kopiert.
Die JSON enthält zusätzlich Häufigkeiten je Multiplikator und zehn gehashte
Gruppenkennungen mit Zeilen-/Werteanzahl; keine CIKs/Ticker/Accessions.

| Metric | Doppelte NULL-start-Identitätsgruppen |
| --- | ---: |
| cash | 673.260 |
| current_assets | 302.503 |
| current_liabilities | 301.563 |
| debt_current | 162.230 |
| debt_noncurrent | 178.671 |
| shares_outstanding | 397.640 |
| total_assets | 400.084 |
| total_equity | 758.659 |
| total_debt | 63.963 |
| interest_expense | 52 |
| operating_cash_flow | 5 |
| capital_expenditures | 61 |
| amortization | 23 |
| tax_expense | 58 |
| depreciation_amortization | 15 |
| revenue | 28 |
| eps_diluted | 57 |
| net_income | 45 |
| operating_income | 13 |
| depreciation | 2 |

„NULL-start“ beschreibt die gespeicherte Struktur: Auch einige Flow-Metrics besitzen
solche Einträge. Diese wurden im Census mitgezählt, nicht fachlich umklassifiziert.
Keine automatische Reparatur fehlender Startperioden oder Parseränderung vorgenommen.
Die Konfliktprüfung betrifft numerische Werte, nicht die Gleichheit sämtlicher
Nicht-Key-Metadaten. Aus null Wertkonflikten folgt deshalb keine pauschale Freigabe
für automatische Deduplizierung.

**Betriebsgrenze:** Der vorhandene lokale Bestand wurde nicht migriert. Neue Ingestion
in eine dieser bereits doppelten Identitäten endet absichtlich mit
`AMBIGUOUS_FACT_IDENTITY`; die Company-Transaktion wird dann zurückgerollt.
Unbetroffene eindeutige Identitäten sowie neue Datenbanken sind durch den Patch sicher
idempotent. Für uneingeschränkte Wiederaufnahme betroffener Bestandsidentitäten ist die
separat freizugebende Datenmigration erforderlich. Die Sicherheitsbedingung wird weder
durch eine willkürliche Altzeilenauswahl noch durch automatische Bestandsänderungen umgangen.

Die synthetischen Testdatenbanken und ihr Cache wurden nach den Tests entfernt.
Bestehende lokale Datenbanken, Research-Artefakte und Journale blieben unangetastet.
