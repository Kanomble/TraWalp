# Task 9.5 · Historische Datenabdeckung und Champion-Readiness

Der Offline-Audit erfasst die 50 gebundenen D.6-Emittenten und SPY separat. Die Untersuchung ab 2010 umfasst
4.238 XNYS-Sessions bis 06.11.2026, der letzten lokal durch einen gültigen SPY-Bar belegten Session. Die
Vollständigkeit aller übrigen Inputs an diesem Datum bleibt separat zu prüfen.
[Ergebnisbericht](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/readiness_report.md>)

## Bestandsnachweis

- Der SHA-256 der veröffentlichten Task-9.4-Staging-Datenbank stimmt. SQLite enthält 1.140.816 Rohbeobachtungen
  und 120.477 retrospektive Kandidaten. Die frühere Zahl von 1.189.487 zählt die beim Bulk-Lauf vor der
  zeitlichen Eingrenzung gelesenen Rohbeobachtungen. Von 17.850 Audit-Zellen haben 11.637 einen retrospektiven
  Kandidaten. Die 850 ergänzten Shares-Zellen dokumentieren die fehlende Staging-Normalisierung.
  Staging-Acceptance bindet das Filing, qualifiziert den retrospektiven Wert aber weiterhin nur provisorisch.
  [Fundamental-Coverage](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/fundamental_coverage.csv>)
- Die historische 50er-Canonical-Lineage enthält vier Facts, vier Occurrences und zwei Receipts in drei
  Dataset-Versionen. Pilot und E.11 bleiben eigene Lineages. Die separat gebundenen E.5-/E.13-Witnesses
  belegen ihre jeweiligen Original- und Reader-Fälle.
  [Inventar](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/inventory_summary.json>)
- Die Task-9.4-Filing-Liste enthält 2.952 CIK-/Accession-Paare. In den geprüften Caches fehlen 2.613 Originale.
  337 lokale Einträge bedürfen weiterer Byteprüfung. Die Bytes zweier NKE-Originalsätze stimmen mit den
  publizierten Dateihashes überein. 2.585 Accessions stehen in 138 gebundenen Planblöcken. Die übrigen
  fehlenden Einträge brauchen Metadata- oder Amendment-Klärung. Originale vor 2010 sind ein eigener Suchauftrag.
  [Beschaffungsplan](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/acquisition_plan.json>)

## Champion und früheste Zeitfenster

Issuer Shares für Market Cap und positiver TTM-OCF sind harte Fundamentalbedingungen. Quality und Valuation
brauchen genügend belegte Einzelkomponenten. Für Total Debt gibt es in 88 von 850 Jahreszellen einen direkt
zugeordneten Kandidaten, für Revenue in 654, OCF in 717 und CapEx in 514. Die Kandidatenzahlen belegen
Einzelwerte. TTM, Share-Freshness, Zeit- und Accounting-Provenienz bleiben separat zu qualifizieren.

Daily-Execution benötigt den nächsten Open und die weitere OHLC-Folge für Fills, Stops, Ziele und Exits.
Der eingefrorene C1-Pfad verlangt 300 Mindest-Vorlaufsessions und 320 angeforderte Ladesessions. Der exakt
gezählte 320er-Vorlauf für Anfang 2010 beginnt am 25.09.2008. Der Frozen-Loader liefert im getesteten
Runtime-Stand den 26.09.2008 mit 319 früheren Sessions. Der Audit verwendet den exakten XNYS-Referenzkalender.
[Anforderungsmatrix](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/champion_data_requirements.json>)

| Scope | Frühester Teilnachweis | Grenze |
| --- | --- | --- |
| Frozen U0 | 03.01.2022 bis 12.08.2026 | Publizierte Rekonstruktion. 922 screenbare Symbole am Start. Historische Mitgliedschaft und Feed je Bar unbelegt. |
| D.6 Legacy-Daily | 04.10.2021 | Zehn Symbol-Labels am ersten Datum, maximal 38 gleichzeitig. Marktproxy aus der lokalen Legacy-Serie und SPY. |
| D.6 Forward SIP/all | 12.10.2026 | 33 Symbol-Labels und SPY mit lokal ausreichender Daily-Historie. Security, Accounting, Adjustment und Execution bleiben offen. |
| Vollständiger D.6-Champion-Test | UNBELEGT | Kein vollständig technisches oder historisch PIT-qualifiziertes Fenster. |

Die zehn ersten Legacy-Labels sind AAPL, AMD, COST, DOMO, EXC, FOXA, MSFT, NVDA, QRVO und TXN.
Die Security-Matrix enthält 92 datierte Zeugen, 50 offene Emittenten-Epochen und eine offene SPY-Epoche.
Alternative Ticker bleiben getrennte Census-Daten. Den Monats- und Jahresmatrizen liegt je Marktproxy eine
lückenlose Historie von 300 vorherigen Sessions für Symbol und SPY aus derselben Quellvariante zugrunde.
[Readiness](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/champion_readiness.csv>) ·
[Security Coverage](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/security_coverage.csv>)

## Übergabe an Task 9.6

1. Lokale Belege nach CIK, Accession und Datei-SHA wiederverwenden.
2. SPY und Warm-up priorisieren. Das SIP/all-Raster weist 4.238 fehlende SPY-Sessions aus.
   Alle 300 Mindest- und 320 exakten Ladesessions im Vorlauf fehlen.
3. Historische Epochen, Splits, Klassenwechsel und Exits nachweisen. Die 217.076 offenen SIP/all-Symbolsessions
   der Emittenten sind vor realer Beschaffung an belegte Epochen zu schneiden. Vor 2016 fehlt ein geeigneter
   nachgewiesener Anbieter für den geplanten Scope. Massive ist punktuell für Identity und Actions belegt.
4. SEC-Planblöcke separat autorisieren und mit dem bestehenden SecHttp-, Cache- und Resume-Vertrag verarbeiten.
   Das Szenario budgetiert 5.170 bis 90.475 SEC-Requests und 1,4 bis 25,1 Stunden bei serieller Sekundentaktung
   ohne Verarbeitung und Wiederholungen. Für Alpaca sind 573 Einzelsymbolfenster ab 2016 vorgesehen, vor der
   Prüfung von Security-Epochen und Pagination. Tarife, API-Grenzen und Archivierungsrechte sind offen.
5. Kapazität erneut prüfen. Rund 104 GiB Speicherbedarf auf dem Arbeitslaufwerk inklusive Reserve und 80 GiB für das
   Backup-Szenario stehen zum Auditzeitpunkt mindestens 177 GiB frei auf G: und 118 GiB frei auf F: gegenüber.
   Physische Trennung und Restore von F: gesondert prüfen.
   [Provider](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/provider_feasibility.json>) ·
   [Speicher](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/storage_estimate.json>)

Der v1-Ordner bleibt als Vorgängerbeleg mit 319 Vorlaufsessions erhalten. Das für Task 9.6 maßgebliche
v2-Manifest bindet den Vorgänger, den exakten Referenzkalender und die eigenen Ausgaben. Die CLI pinnt
SQLite-Eingaben unter Windows schreibgeschützt und nutzt mode=ro&immutable=1 sowie query_only.
Ausstehende WAL-Seiten oder eine Änderung des Dateizustands brechen den Audit ab.

~~~powershell
Set-Location 'G:\GitHub\TraWalp'
.\.venv\Scripts\python.exe -B scripts/run_f01_historical_data_readiness.py audit --output 'G:\TraWalpResearch\import-artifacts\f01-task95-historical-data-readiness-repeat'
~~~

Der Planstatus lautet PLAN_ONLY. Netzwerk, Import und Trading erfordern separate Freigaben.
[Auditmanifest](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/manifest.json>)
