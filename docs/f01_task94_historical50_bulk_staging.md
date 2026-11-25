# F01 · Historical 50 Companyfacts Staging

Task 9.4 liefert einen separaten, retrospektiven Fundamentalbestand für genau 50 D.6-Reporting-Entities. Der Zeitraum beginnt am 01.01.2010, gemessen am Periodenende. Die vorliegenden Quellen enden mit dem gebundenen SEC-Bulk-Snapshot. SPY bleibt außerhalb des Fundamentalbestands. Der Staging-Bestand hat die Klassifikation `UNVERIFIED_COMPANYFACTS_RESEARCH_STAGING`.

## Reale Offline-Evidenz

Der Preflight prüfte die D.6-Bindungen, das SEC-Quellenmanifest und die SHA-256-Werte der ausgewählten Companyfacts- und Submissions-Member. Die folgenden Zahlen stammen aus dem vollständigen lokalen Scan.

| Ergebnis | Anzahl |
| --- | ---: |
| Gebundene Emittenten | 50 |
| Gelesene Provider-Beobachtungen | 1.189.487 |
| Beobachtungen im Zeitraum oder mit undatierbarer Evidenz | 1.140.816 |
| Retrospektive normalisierte Kandidaten | 120.477 |
| Kandidaten mit gebundenem Submissions-Acceptance | 120.477 |
| Originalverifizierte historische PIT-Werte | 0 |
| Mehrdeutige Beobachtungen ohne Staging-Kandidat | 4.591 |
| Coverage-Zeilen nach CIK, Kalenderjahr des Periodenendes und Metrik | 17.000 |
| Periodische CIK-/Accession-Paare aus den Beobachtungen | 2.952 |
| Davon in den geprüften Cache-Pfaden ohne lokales Originalverzeichnis | 2.613 |
| Offline geplante Originalbeschaffungsgruppen | 130 |

[Preflight](</G:/TraWalpResearch/import-artifacts/f01-task94-historical50-preflight-v1/preflight.json>) · [Coverage-CSV](</G:/TraWalpResearch/import-artifacts/f01-task94-historical50-preflight-v1/coverage.csv>) · [Filing-Befunde](</G:/TraWalpResearch/import-artifacts/f01-task94-historical50-preflight-v1/filing-review.json>) · [Beschaffungsgruppen](</G:/TraWalpResearch/import-artifacts/f01-task94-historical50-preflight-v1/acquisition-plan.json>)

## Forschungsdatenvertrag

- `issuer` enthält CIK, historisches Label und den Hash des Companyfacts-Members. `observation` hält Taxonomie, Original-Tag, Unit, nullbasierten Index in der Unit-Liste und die vollständigen Beobachtungsfelder als JSON. Nicht-ganzzahlige Provider-Werte bleiben als exakter Dezimaltext erhalten. Der unveränderte ZIP-Member bleibt die Byte-Referenz. Das Archiv enthält auch Konzepte ohne sichere Metrikzuordnung.
- `candidate` bewahrt getrennte CIKs, Periodenarten, Units, Filed-Daten, Accessions und Original-Tags. Nur identische Kandidaten innerhalb desselben Filing-/Perioden-/Metrik-Scopes werden zusammengeführt. Die Rohbeobachtungen behalten ihren Link. Unterschiedliche Werte oder nicht auflösbare Tags/Frames blockieren diesen Scope. Andere Filings, Amendments und getrennte Dauerfenster bleiben separat gespeichert.
- Der Staging-Pfad verwendet die vorhandenen engeren semantischen Zuordnungen. Parent Net Income und konsolidiertes Net Income, Cash mit Restricted Cash und engeres Cash sowie Debt-Komponenten und ausdrücklich ausgewiesenes Total Debt sind fachlich getrennt. Es findet keine Währungsumrechnung, Schuldsynthese, TTM-Bildung oder Accounting-Zulassung statt. Der ausgewählte CapEx-Tag und das Periodenfenster müssen für den späteren Research-Use-Case weiterhin ausgewertet werden.
- Die Coverage weist Rohbeobachtungen, retrospektive Kandidaten, provisorisch gebundene Acceptance, Mehrdeutigkeiten und Ausschlüsse getrennt aus. Der technische Qualitätsscore ist `100 × (retrospektive Kandidaten + Kandidaten mit Acceptance) / (2 × Rohbeobachtungen)`, gerundet. Ohne Beobachtungen ist er null. Er misst Datenbelegung und Zeitstempelbindung und hat keine Aussage über Wertpapierqualität oder Original-PIT.
- `retrospective_fundamentals` ist die isolierte SQL-Sicht für spätere statistische Forschung. Der Schlüsselraum des Consumers umfasst CIK, Metrik, Einheit, Periodenfenster und Accession. `value_decimal` als Dezimalzahl interpretieren. Auswahlregeln und Zeitfenster gehören explizit in den späteren Research-Auftrag. `acceptance_utc` stammt aus der archivierten Submissions-Übereinstimmung. Provider-Rekonstruktionen allein belegen weder historisch sichtbare Originalwerte noch die damalige Handelbarkeit. Der View markiert das als `UNVERIFIED_HISTORICAL_ORIGINAL`.

Die Beschaffungsgruppen enthalten maximal 20 nach Filing-Datum und CIK sortierte Originale mit bekannter Acceptance. Amendments und Fälle mit widersprüchlichem oder fehlendem Submissions-Beleg sind ausgeschlossen und verbleiben im Filing-Bericht. Die Gruppen sind Planungsartefakte. Für einen späteren Abruf muss der breitere Scope ausdrücklich an die vorhandenen SEC-HTTP-, Pacing-, Approval- und Cache-Resume-Verträge gebunden werden. Der aktuelle Task-9-Einzelfiling-Runner ist weiterhin auf seinen D.11-Parent beschränkt.

## PowerShell

Der vollständige Offline-Preflight schreibt standardmäßig nur JSON auf stdout. Für einen weiteren veröffentlichten Preflight ein neues, noch nicht existierendes Verzeichnis unter `import-artifacts` mit Präfix `f01-task94-` wählen.

```powershell
Set-Location -LiteralPath 'G:\GitHub\TraWalp'
$Py = '.\.venv\Scripts\python.exe'
$Cli = 'scripts/run_f01_historical_50_bulk_staging.py'
& $Py -B $Cli preflight
if ($LASTEXITCODE -ne 0) { throw "Bulk preflight failed ($LASTEXITCODE)" }
```

Erst nach separat erteilter Staging-Importfreigabe:

```powershell
$PlanSha = '9e62aa3c55722b015d0cef24e00754fdbbf957bd28cd404e51e06ab93e7892eb'
& $Py -B $Cli import --expect-plan-sha256 $PlanSha --authorize-staging-import
if ($LASTEXITCODE -ne 0) { throw "Bulk staging import failed ($LASTEXITCODE)" }
```

Der Plan-SHA bindet Code, Quellenmanifest und D.6-Scope. Bei einem Code- oder Inputwechsel den Preflight neu erstellen. Der Import arbeitet in einem eigenen `.incomplete`-Verzeichnis und schreibt je Issuer transaktional. Nach einer Unterbrechung denselben genehmigten Befehl mit `--resume` und unverändertem SHA verwenden. Bereits geschriebene Issuer werden inhaltlich abgeglichen. Erst nach vollständigen 50 Issuern, Foreign-Key-/Integrity-Checks und gesicherten Berichten wird der Zielordner veröffentlicht. Ein vorhandenes Endziel wird abgewiesen.

Das genaue Endziel ist `G:\TraWalpResearch\research-databases\f01-companyfacts-staging-historical50-v1\companyfacts-staging.sqlite3`. Ein separater späterer Canonical-Import erforderte Original-, DTS-, Source- und Accounting-Evidenz und eine eigene Freigabe. Dieser Task hat den vollständigen Offline-Preflight und eine synthetische 50er-Staging-Veröffentlichung mit Resume durchgeführt. Der reale Staging-Import steht aus.
