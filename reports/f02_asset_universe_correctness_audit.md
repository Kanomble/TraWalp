# F02 — Atomic Asset Universe Snapshot Qualification

## Ergebnis und Baseline

F02 ist durch eine Snapshot-Qualifikation **vor** der autoritativen Asset-Reconciliation
behoben. Schon eine technisch unparsebare Zeile verhindert die gesamte Promotion.
Kein partielles Upsert, kein indirekter Universe-Austritt und kein success-State.
Legitime Austritte aus einem vollständig parsebaren Snapshot bleiben möglich.

Baseline: lokales main, HEAD `3f53aa66aaaa59fb406fbc83f7e75487b4e559a2`.
Arbeitsbaum beim Start sauber. Referenz: `reports/full_codebase_strategy_audit.md`, F02.
Alle Proben offline mit synthetischen SDK-Antworten und temporären SQLite-Dateien.
Keine echte Alpaca-Abfrage, reale Asset-Synchronisierung, Marktdatenbankänderung,
Datenbereinigung, Backtests, Journal-Init/Run, F-Parameteränderung oder Commit/Push.

## Root Cause und rekonstruierter Vertrag

`AlpacaDataClient.list_tradable_us_equities` holt eine Liste für ACTIVE/US_EQUITY,
validiert Symbol, Status, Klasse, Exchange und boolesche Asset-Flags und isoliert
bisher ungültige Zeilen. Bei ausreichender Parse-Retention wurde der verbliebene
Teil als normale Ergebnisliste zurückgegeben.

`DataSynchronizer._sync_assets` behandelte diese Liste als vollständig, rief
`Database.reconcile_assets` auf und setzte errors=0. Die Reconciliation darf bei
einem autoritativen Snapshot vorher aktive, jetzt fehlende Symbole deaktivieren.
Diese korrekte Absenz-Semantik wurde fälschlich auch auf Parse-Verluste angewendet.

Die beiden bisherigen Retention-Guards verhindern katastrophalen Datenverlust,
beweisen aber keine Vollständigkeit:
- Adapter: mindestens minimum_retained_ratio (Default 0,5) der rohen Zeilen parsebar.
- DB: Snapshot-Größe mindestens ceil(vorher tradable × minimum_retained_ratio),
  sofern vorher Assets vorhanden waren; leerer Snapshot grundsätzlich abgewiesen.

Im F02-Beispiel passieren 2/3 gültige Zeilen beide Guards. CCC wird als abwesend
behandelt, obwohl es tatsächlich unlesbar vorhanden ist. Die Stage meldet Erfolg.
Der generische Stage-Fehlerpfad erbte außerdem bisher alte Ergebnis-/Fehlerzähler;
auch ein abgewiesener Asset-Versuch konnte deshalb alte errors=0 tragen.

## Red-Nachweis vor Produktionsänderung

`test_f02_known_malformed_asset_never_deactivates_or_reports_success` wurde zuerst
gegen die Baseline ausgeführt. Bestand AAA/BBB/CCC; Antwort AAA gültig mit geändertem
Namen, BBB gültig, nur `{'symbol':'CCC'}` für CCC.

**1 Test fehlgeschlagen**, Exit-Code 1, 2,25 s. Die Assertion protokolliert exakt:
`('F02: unqualified snapshot promoted', ['AAA', 'BBB'], 'success', 0)`.
Damit sind sowohl CCC-Deaktivierung als auch falscher success/errors=0-State bestätigt.
Rohprotokoll: `reports/f02_asset_snapshot_red.txt`.

## Implementierte minimale Korrektur

### Adapter-Qualifikation

- Diagnostik wird zu Beginn **jedes** Asset-Aufrufs auf unqualifiziert zurückgesetzt,
  auch wenn anschließend Transport oder Response-Form scheitert. Kein alter Parse-
  Erfolg oder Fehlerzähler wird einem neuen Providerfehler zugeordnet.
- Bestehende Retention- und Symbol-Collision-Prüfungen bleiben in ihrer Reihenfolge
  erhalten. Wenn sie passiert sind, aber assets_skipped_invalid > 0 gilt, erfolgt
  `ValueError('ALPACA_ASSET_SNAPSHOT_INCOMPLETE')` anstelle einer partiellen Liste.
- Erst nach allen Parser-/Retention-/Collision-Prüfungen wird
  `asset_snapshot_qualified=True` gesetzt und die Liste zurückgegeben.
- Diagnose enthält aktuelle Raw-/Parsed-/Invalid-/Final-Zähler und maximal drei
  Parse-Fehlerbeispiele mit Symbol (max. 64 Zeichen) und Grund (max. 200 Zeichen).
  Keine komplette Providerantwort wird im Dataset-State gespeichert.

### Promotion-Grenze im Synchronizer

Vor `reconcile_assets` wird vorhandene Diagnostik zusätzlich ausgewertet:
Invalid-Zeilen, explizit nicht qualifizierte Snapshots oder ein gemeldeter Unterschied
zwischen Raw- und Parsed-Anzahl werden abgewiesen. Damit kann auch ein älterer oder
alternativer Adapter eine ausdrücklich unvollständige Liste nicht promoten.
Bereits typisierte Legacy-Test-/Adapterlisten ohne Diagnosefelder bleiben kompatibel;
für den produktiven Alpaca-Adapter ist Qualifikation immer explizit.

### Asset-Dataset-State

Nur für asset_universe werden zwischen Versuchen die historischen
last_success_at-/last_completed_at-Zeitstempel übernommen. Alte Erfolgsmetriken,
Fehler und Diagnosezähler erscheinen nicht als Resultate des neuen Versuchs.

Bei einer Exception schreibt die Stage:
- status=failed, errors=1 und den konkreten Exception-Typ/Fehlertext;
- aktuellen last_started_at und elapsed_seconds;
- aktuelle verfügbare Asset-Diagnostik;
- unveränderten historischen last_success_at/last_completed_at, soweit vorhanden.

errors=1 zählt **den fehlgeschlagenen Refresh**, nicht kumulativ sämtliche Versuche
oder ungültigen Zeilen. Die Zeilenanzahl bleibt separat assets_skipped_invalid.
Bei einem gültigen Retry werden neue Erfolgszeitstempel, errors=0 und ausschließlich
aktuelle Erfolgsmetriken/Diagnosen geschrieben; alte Fehlermeldungen verschwinden.
Der State-Vertrag anderer Datasets, insbesondere SEC, wurde nicht verändert.

`asset_snapshot_qualified` bedeutet **qualifizierter Adapter-Input**, nicht erfolgte
Datenbank-Promotion. Bei gültiger Antwort und anschließendem DB-Rollback kann das
Flag true sein, während status=failed und errors=1 die Promotion korrekt ablehnen.

## Unterscheidung der sechs Fälle

| Fall | Verhalten / Evidenz |
| --- | --- |
| Legitime Provider-Absenz | Vollständig parsebare AAA/BBB-Antwort deaktiviert zuvor vorhandenes CCC weiterhin, sofern Retention-Guards erfüllt sind. CCC-Zeile bleibt gespeichert, tradable=0. |
| Legitim nicht mehr tradable | CCC mit echtem booleschem tradable=False beziehungsweise gültigem inactive-Status ist parsebar, wird fachlich ausgefiltert und regulär deaktiviert. |
| Technisch unparsebare Antwort | Fehlendes Feld, ungültiger Status/Exchange oder nichtboolesches Flag: gesamte Antwort abgewiesen, keine Assets geändert, failed/errors=1. Auch unbekanntes fehlerhaftes Asset NEW blockiert die Promotion. |
| Provider-Fehler | Exception wird weitergereicht, aktuelle failed/errors=1-Diagnose, keine falschen aktuellen Parse-Metriken oder Asset-Mutation. |
| Unvollständiger Snapshot | Jede bekannte Parse-Lücke, unpassende Response-Form oder explizite Qualifikations-/Zählerinkonsistenz scheitert vor Reconciliation. Leere/suspekt kleine Snapshots bleiben zusätzlich bewacht. |
| Symbol-Mapping-Kollision | Bestehende explizite One-to-One-/Canonical-/Response-Collision-Guards bleiben erhalten; keine Zusammenführung kollidierender Symbole. Fehler zählt als fehlgeschlagener Refresh. |

Unbekannte Exchanges wie TXSE oder XYZNEW bleiben bei technisch gültiger Antwort
akzeptiert und diagnostiziert. Keine Änderung an Exchange-/Statusregeln, Class-
Behandlung, explizitem Provider-Symbolmapping oder F-Scoring/-Gates.
Es wurde insbesondere keine neue automatische Großschreibung/Interpunktion-Heuristik
für rohe Provider-Symbole eingeführt.

## Atomarität und erreichter Codepfad

`Database.reconcile_assets` und seine Transaktionsgrenzen sind unverändert.
Technische Unvollständigkeit wird vollständig **davor** erkannt. Tests vergleichen
sämtliche Asset-Spalten einschließlich Name und updated_at und verhindern den
Reconcile-Aufruf zusätzlich per Fail-Spy. Eine gemischte Antwort mit geändertem AAA,
neuem DDD und fehlerhaftem Asset führt somit zu keinerlei partiellem Universe-Update.

Ein gültiger Snapshot erreicht dagegen nachweislich die Reconciliation. Eine
synthetische SQLite-Trigger-Exception bei CCC-Deaktivierung tritt nach den Upserts
auf; die bestehende DB-Transaktion rollt Namen, updated_at und neu eingefügtes DDD
vollständig zurück. Der Stage-State bleibt failed/errors=1, nicht success.

Die Atomarität bezieht sich auf die bestehende Asset-Reconciliation; die Stage-
Statusschritte haben weiterhin eigene Transaktionen. Der Patch verbindet diese
Grenzen nicht zu einem neuen globalen Asset-/Dataset-State-Commit.

## Auswirkungen auf Shadow-Readiness

Readiness-Code und Guards sind unverändert. Erforderlich bleibt ein erfolgreicher,
frischer Asset-Dataset-State ohne Fehler. Nach einem abgewiesenen Snapshot erscheinen
`ASSET_UNIVERSE_STATE_FAILED` und `ASSET_UNIVERSE_ERRORS_PRESENT`.
Ein alter erfolgreicher Zeitstempel macht failed nicht frisch/zulässig.
Auch Observation-Readiness bleibt dann gesperrt.

Die neue Offline-Probe etabliert zuerst einen bereiten Zustand mit gültiger
SPY-SIP-Beobachtung und deterministisch auswertbarer Cross-Section. Nach dem F02-
Fehler sind die beiden genannten Asset-Gründe **exakt die einzigen Blocker**;
Economic- und Observation-Readiness sind false. Ein gültiger Retry macht beide
wieder true. Kein ShadowRunner-/Journal-Init/Run wird dafür verwendet.

Die geänderten Ingestionsdateien sind Bestandteil des vorhandenen Pipeline-
Fingerprints. Dessen reguläre Drift-Prüfung wird nicht umgangen; bestehende Journale
wurden weder ausgeführt noch angepasst.

## Testumfang und Ergebnisse

Die Abschlussprüfung enthält nur neue und unmittelbar betroffene Offline-Tests:

| Testdatei / Auswahl | Fälle |
| --- | ---: |
| test_f02_asset_snapshot.py | 23 neue Regressionen |
| test_alpaca_client.py | 6 |
| test_sync.py | 48 |
| test_database.py: drei Asset-Reconciliation-Tests | 3 |
| test_shadow_hardening.py: Asset-Parsing/Exchange/Retention und Stage-State | 7 |
| test_pairs_daily_blocker_audit.py: Asset-Alias-Collision | 1 |
| Gesamt | 88 |

Der vollständige Sync-Testumfang ist unmittelbar relevant, weil der gemeinsame
Stage-Handler geändert wurde; die neue State-Sonderbehandlung ist auf asset_universe
beschränkt. Keine Backtest- oder Journal-Testdatei wurde ausgeführt.

Ein bestehender Test in test_shadow_hardening erwartete bisher die problematische
partielle Ingestion. Er prüft jetzt Ablehnung ohne Asset-Zeilen und danach gültigen
Retry; Retention- und Unknown-Exchange-Prüfungen bleiben erhalten.

Erster Green-Zwischenlauf: 21 neue Tests bestanden, 1 Fixturefehler. Die Fixture
hatte nicht existierende automatische Großschreibung roher Provider-Symbole
angenommen; sie wurde auf den bestehenden expliziten Mapping-Vertrag korrigiert.
Keine sachfremde Produktionskorrektur. Der folgende unmittelbar betroffene Lauf
bestand mit 87 Tests; danach wurden begrenzte Parserfehler-Beispiele samt zusätzlichem
Regressionstest ergänzt. Endgültiger Lauf: **88 bestanden**, Exit-Code 0, 95,23 s; eine bereits bestehende websockets.legacy-DeprecationWarning.

Artefakte: `reports/f02_asset_snapshot_red.txt`, `reports/f02_asset_snapshot_final.txt`,
`reports/f02_asset_snapshot_final.xml`. Ruff für alle vier geänderten Python-Dateien: bestanden. git diff --check: keine Whitespace-Fehler.

## Geänderte Dateien

- src/trading_system/data/alpaca_client.py: Vollständigkeits-/Qualifikationsguard,
  neue versuchsgebundene und begrenzte Fehlerdiagnostik.
- src/trading_system/data/sync.py: Qualifikation vor Promotion; ausschließlich für
  Asset-States aktuelle Fehlerzählung/Diagnostik statt vererbter Erfolgsergebnisse.
- tests/test_f02_asset_snapshot.py: neue gezielte Offline-Regressionsfälle.
- tests/test_shadow_hardening.py: alter Partial-Ingestion-Test an korrigierten Vertrag angepasst.
- .gitignore: Ausnahme ausschließlich für diesen angeforderten Audit-Bericht.
- reports/f02_asset_universe_correctness_audit.md: dieser Bericht.

Keine Änderung an database.py, Schema, Reconciliation-/Readiness-Implementierung,
F-Parametern oder realen Datensätzen. Kein automatischer Commit/Push.

## Verbleibende Risiken und klare Grenzen

- Der Adapter qualifiziert die **lokal empfangene** Antwort. Eine serverseitig
  kommentarlos gekürzte, ansonsten vollständig gültige Liste ist ohne zusätzliche
  Provider-Vollständigkeitsevidenz nicht sicher von legitimer Absenz unterscheidbar.
  Der bestehende Mengen-Retention-Guard bleibt dafür zusätzliche Absicherung,
  aber keine mathematische Vollständigkeitsgarantie.
- Ein komplett leerer oder nur noch nichttradabler Universe-Snapshot wird weiterhin
  abgewiesen; es gibt kein automatisches Deaktivieren des gesamten Universums.
- Bereits durch ältere F02-Läufe deaktivierte reale Assets wurden nicht untersucht,
  reaktiviert oder bereinigt. Ein eventuell später separat autorisierter gültiger
  Refresh kann regulär reaktivieren; er wurde hier nicht gestartet.
- Atomare Asset-DML und separate State-Persistenz bleiben der bestehende Vertrag.
  Keine Behauptung einer neu eingeführten globalen Transaktionsgarantie.
- Explizite Qualifikationsdiagnostik des produktiven Adapters wird geprüft; beliebige
  fremde Adapter ohne Diagnostik können ihre eigene Vollständigkeit nur im Rahmen
  des bestehenden typisierten Listenvertrags zusichern.


## Reproduktion der Abschlussprüfung

Projektinterpreter laut PyCharm: `.venv/Scripts/python.exe`, Python 3.12.7.

```powershell
$f02TestTargets = @(
  'tests/test_f02_asset_snapshot.py',
  'tests/test_alpaca_client.py',
  'tests/test_sync.py',
  'tests/test_database.py::test_asset_snapshot_reconciliation_deactivates_without_deleting_and_reactivates',
  'tests/test_database.py::test_asset_reconciliation_rejects_empty_and_suspicious_snapshots_without_changes',
  'tests/test_database.py::test_asset_reconciliation_rolls_back_upserts_when_deactivation_fails',
  'tests/test_shadow_hardening.py::test_raw_asset_exchange_preserved',
  'tests/test_shadow_hardening.py::test_real_sdk_transport_uses_raw_assets',
  'tests/test_shadow_hardening.py::test_malformed_asset_isolated_and_safety_retention_kept',
  'tests/test_shadow_hardening.py::test_sync_completion_clears_previous_error_and_exposes_technical_completion',
  'tests/test_pairs_daily_blocker_audit.py::test_asset_alias_collision_is_not_silently_merged'
)
& '.venv/Scripts/python.exe' -m pytest @f02TestTargets -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_f02_cache --basetemp=.tmp_pytest_f02_final --junitxml=reports/f02_asset_snapshot_final.xml
```

Die erzeugten pytest-Fixtures und der eigene Cache wurden nach Abschluss entfernt;
Rohprotokolle und JUnit-Ergebnisse bleiben als lokale Analyseartefakte erhalten.
