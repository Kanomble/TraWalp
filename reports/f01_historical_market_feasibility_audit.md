# F01 · Kostenlose historische Marktdaten und Security-Evidenz

**HISTORICAL_MARKET_FEASIBILITY_ASSESSED · Entscheidung C.** Die erfassten kostenlosen Tarife reichen als alleinige Datenbasis für eine historische Trading-Qualifikation 2016–2026 nicht aus. Alpaca ist ein plausibler Kandidat für Daily-Bars ab 2016. Massive kann neuere Bars und datierte Identitätspunkte ergänzen. Unbelegt bleiben der frühe Warm-up, zusammenhängende Security-Epochen und belastbare Exit-Daten für ausgeschiedene Titel. Die neuen kontobezogenen Provider-Proben stehen aus. Der nächste Schritt ist ein begrenzter Marktdaten- und Identity-Test vor der Beschaffung weiterer SEC-Filings. [Maschinenlesbarer Bericht](f01_historical_market_feasibility.json) · [50er-Preflight](f01_historical_security_50_issuer_preflight_audit.md)

## Dokumentierte Verträge, Stand 23.11.2026

| Quelle | Kostenlos dokumentiert | Grenze für diesen Pilot |
| --- | --- | --- |
| Alpaca Basic, Aktien | Historische Aktien- und ETF-Daten seit 2016, 200 historische API-Aufrufe pro Minute, IEX in Echtzeit. Historischer SIP ohne SIP-Abonnement, wenn das Anfrageende mindestens 15 Minuten zurückliegt. | Die globale Historiengrenze garantiert weder alle Symbole noch ein lückenloses IEX-/SIP-Fenster. IEX ist eine einzelne Börse, SIP das konsolidierte US-Tape. Fehlendes Volumen oder fehlende Sessions im einen Feed werden nicht durch den anderen aufgefüllt. |
| Alpaca Bars und Symbole | `raw`, `split`, `dividend`, `spin-off` und `all`. Split-Adjustment wirkt auf Preis und Volumen. `asof` steuert die Zuordnung über Namenswechsel, `asof=-` fragt das exakte Symbol ohne Namensauflösung ab. Der Paper-Asset-Lookup per Symbol oder Asset-ID kann `active` oder `inactive` liefern. | Bars sind unter dem abgefragten Label dokumentiert. Der Asset-Lookup zeigt einen Providerstatus zum Abruf. Beides zusammen liefert noch keine datierte Security-ID-/Klassen-Epoche und keine Garantie für delistete Symbole oder deren früheren Handel. |
| Alpaca Corporate Actions | Gezielte Abfragen für Splits, Namensänderungen und Merger. `data_quality=all` kann unvollständige Zeilen enthalten. Der Zeitfilter verwendet das Process Date. | Alpaca garantiert den Veröffentlichungszeitpunkt der Aktion nicht. Process Date, Effective Date, Ex-Date und tatsächlich verfügbarer Veröffentlichungsbeleg separat behandeln. Fehlende Einträge sind ergebnisoffen. |
| Massive Stocks Basic | Fünf Aufrufe pro Minute, rollierende zwei Jahre für REST-Bars und datierte Ticker-Referenz, keine Stock-Flat-Files. Split-Endpoint auf Basic ebenfalls zwei Jahre. Aggregates sind standardmäßig splitbereinigt, `adjusted=false` liefert Rohwerte. | Am 23.11.2026 liegt die ungefähre freie Untergrenze beim 23.11.2024 und wandert täglich. Archivdaten bis 2003 betreffen andere Zugriffsfenster. TWTRs 2022-Bars und AAPLs 2020-Split liegen außerhalb des dokumentierten Gratisfensters. Massive `adjusted=true` hat eine andere Semantik als Alpaca `all`. |
| Massive Ticker-Referenz | Datiertes Ticker Overview mit Typ, Exchange, CIK, Composite FIGI und Share-Class FIGI, soweit vorhanden. All Tickers mit `active=false` für inaktive Titel und ggf. `delisted_utc`. Ticker Events ist experimentell. | Historische Identifier können fehlen. FIGI und CIK können sich bei Übergängen unterschiedlich verhalten. Ein Tagestreffer belegt einen Punkt. Ein optionaler `delisted_utc`-Wert braucht eine Zuordnung zu Security und Börse und einen getrennten Exit-Beleg. |

[Alpaca Basic](https://docs.alpaca.markets/us/v1.1/docs/about-market-data-api) · [SIP-Zugriff](https://docs.alpaca.markets/us/v1.1/docs/market-data-faq) · [Bars und `asof`](https://docs.alpaca.markets/us/v1.4.2/reference/stockbarsingle-1) · [Assets](https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id) · [Corporate Actions](https://docs.alpaca.markets/us/reference/corporateactions-1)

[Massive Bar-Historie](https://massive.com/knowledge-base/article/how-much-historical-stock-data-does-massive-have) · [Security-Master und datierte Referenz](https://massive.com/knowledge-base/article/does-massive-have-a-downloadable-security-master) · [Inaktive Ticker](https://massive.com/docs/rest/stocks/tickers/all-tickers) · [Split-Historie](https://massive.com/docs/rest/stocks/corporate-actions/splits) · [Adjustment](https://massive.com/knowledge-base/article/is-massives-stock-data-adjusted-for-splits-or-dividends) · [Ticker Events](https://massive.com/docs/rest/stocks/overview)

## Lokale Beobachtungen und Adapter

- **Alpaca:** 1.806.028 gespeicherte Daily-Beobachtungen mit explizitem `ALPACA/SIP/all` vom 01.08.2025 bis 06.11.2026. SPY hat dort 320 nutzbare Sitzungen. Der alte Legacy-Symbolbestand enthält für SPY 1.570 von 1.732 Sitzungen 2020–2026. Die Legacy-Tabelle belegt keinen Feed je Zeile. 2016–2019 und der initiale Warm-up fehlen in beiden lokalen Beständen. Die bisherigen Belege enthalten keine Tarifattestierung für die abgefragten Altjahre. Auch der Forward-SIP-Bestand enthält keine durchgängige Security-Zuordnung. [Forward-Vertragsnachweis](f_forward_sip_shadow_contract_audit.md) · [D.6-Coverage](f01_historical_50_preflight.json)
- **Massive:** Der ältere lokale Identity-Pilot protokolliert 15 HTTP-200-Antworten und einen 404 bei 16 Versuchen. Datierte 2022-Details sind unter anderem für QRVO, BG und den früheren SAIL-CIK erhalten. Der im alten Lauf wirksame Kontotarif ist im Beleg unbestätigt. Die eingefrorenen Punkte bleiben nutzbar, auch wenn heutige Basic-Abfragen für dieselben Daten am rollierenden Fenster scheitern. [Massive-Punktaudit](f_2022_massive_identity_pilot_v1_audit.md)
- **Code:** `AlpacaDataClient` arbeitet standardmäßig mit IEX/all, fragt aktive Assets ab und übergibt bei regulären `StockBarsRequest` keinen expliziten `asof`-Parameter. `AlpacaCorporateActionClient` ist ein isolierter Name-Change-Adapter. `MassiveReferenceClient` erlaubt im produktiven Pfad aktive Ticker-Listen und Typen. `MassiveIdentityResearchClient` unterstützt zusätzlich datierte Einzeltitel und Events. Die neue Diagnose nutzt isolierte Einzel-GETs, damit Feed, Adjustment, `asof`, HTTP-Status und harte Größenlimits im Beleg stehen. Massive nutzt weiterhin denselben dauerhaften lokalen 5/min-Limiter. [Alpaca-Adapter](../src/trading_system/data/alpaca_client.py) · [Actions](../src/trading_system/data/alpaca_corporate_actions.py) · [Massive-Referenz](../src/trading_system/data/massive_reference.py) · [Massive-Research](../src/trading_system/data/massive_identity_research.py)

## Begrenzte manuelle Proben

| Fall | Feste Prüffenster | Entscheidung aus der Probe |
| --- | --- | --- |
| SPY | 04.–08.01.2016, 22.–26.10.2018, 24.–31.12.2019, 02.–06.06.2025 | Alpaca IEX und SIP getrennt. Massive als Kontrollprobe vor und innerhalb seines dokumentierten Fensters. NYSE-Kalender und Feiertage berücksichtigen. Für einen Start im Januar 2016 wären lückenlose 300 vorherige Sitzungen ab 23.10.2014 nötig. Für Januar 2020 beginnt der entsprechende Warm-up am 22.10.2018. Die Stichprobe prüft die Erreichbarkeit, ein späterer Batch muss den ganzen Warm-up belegen. |
| AAPL | 27.08.–02.09.2020 um den Split; Actions-Zeitfenster 20.08.–08.09. | SIP-Rohwerte, Split- und All-Varianten für die überlappenden Sessions getrennt vergleichen. Preis-/Volumenquotienten werden dokumentiert. Effective Date, Faktor und Provider-Aktionsdaten prüfen. Die Basis für Preis, EPS und Shares muss für die jeweilige Entscheidung zeitlich zusammenpassen. Gleichheit der Ratios oder eine erfolgreiche historische Adjustment-Abfrage allein weist diese Basis nicht nach. |
| ACN | Datiertes Massive-Detail 23.10.2018 und 19.11.2026, Daily am 16.–20.11.2026, Paper-Asset | CIK, MIC, FIGI und Klassenbezeichnung mit den bestehenden Cover-Pages abgleichen. Class-A-Preise und die wirtschaftliche Issuer-Shares-Basis bleiben getrennte Nachweise. |
| TWTR | 25.10.–02.11.2022, datierter Referenzpunkt 27.10.2022, inaktiver Lookup, Merger-Action bis 10.11. | Letzte **beobachtete** Bar, Provider-Status und Form-25-Beleg separat. Für den tatsächlichen letzten Börsenhandel und Exit braucht es noch datierte Handels-/Halt- und Merger- bzw. Settlement-Belege einschließlich Auszahlungswert. Weder leere noch verweigerte Antworten beweisen Nichtexistenz. |
| JATT/ZURA | 16.–24.03.2023 als getrennte exakte Labels plus ZURA mit `asof=2023-03-22`, JATT und ZURA im November 2026, Name Change | Alte CIK-/CUSIP-Evidenz, neue Security-Referenz und die Wirkung des Namens-Mappings getrennt prüfen. Die lokale Action enthält ein Process Date und eine CUSIP-Brücke. Die Effective Date der betroffenen Security und Grenzen eines späteren Ticker-Reuse bleiben zu belegen. |

Die festen Pläne umfassen maximal 25 Alpaca- und 14 Massive-GETs. Der Aufruf wählt jeweils einen Provider. Er nutzt Timeouts, keine Redirects und höchstens eine Antwortseite pro Probe. Pagination oder Überschreiten des 128-KiB-Bodybudgets erzeugen einen eigenen Befund. HTTP-Status, Abrufzeit, Hash und bereinigte Beobachtungen werden gespeichert. Feed bzw. Adjustment stammen bei Alpaca aus dem expliziten Request-Vertrag. Die Probe verifiziert kein separates Feed-Feld pro Bar. 401, 402/403, 404, 429 und gültige leere Ergebnisse bleiben unterscheidbar. Bar-Prüfungen berücksichtigen NY-Sessions, Zeitstempel, widerspruchsfreie positive OHLCV, doppelte Sessions und die tatsächlich zurückgegebenen Daten. Private Zugangsdaten bleiben in den Anforderungsheadern. Offizielle Dokumentation wurde gelesen. Aufruf oder Verifikation eines realen Providerkontos mit diesem Skript stehen aus.

## Speicher und Batch-Grenzen

Die Messung verwendet **30 echte gespeicherte SIP/all-Beobachtungen** für SPY, AAPL und ACN. Je fünf Sitzungen stammen aus 04.–08.08.2025 und 02.–06.11.2026. Die sechs Fenster wurden mit `mode=ro&immutable=1` unter nativer Dateisperre gelesen. Das JSON bindet die Zeilen über Hashes an ihre gespeicherte Provenienz und enthält Messgrößen und Coverage. Die ursprünglichen HTTP-Antwortgrößen liegen für diese Beobachtungen nicht vor.

| Position | Stichproben-Hochrechnung für 50 Emittenten + SPY |
| --- | ---: |
| Maximale Daily-Slots im Kalenderfenster einschließlich 300 Sessions Vorlauf | 154.938 |
| Eine Variante als kompaktes JSONL | 14.858.555 Bytes |
| Dieselbe Variante als gzip-JSONL | 5.825.669 Bytes |
| Kompakte Variante plus eine vollständige Kopie | 11.651.338 Bytes |

Der Platzbedarf für weitere Aktienklassen, Preisvarianten, Security- und Corporate-Action-Quellen, Originalantworten, Manifeste, SQLite-Indizes, Sidecars und Reserve liegt außerhalb dieser Schätzung. Die 90,13 GiB aus D.6 sind ein damaliger Messwert. Speicher ist bei Daily-Bars voraussichtlich nachrangig gegenüber Zugriff und Bindung. Die manuelle Probe misst zusätzlich die dekodierten Antwortbytes und weist getrennte Szenarien je Provider, Feed und Adjustment aus, sobald mindestens zehn nutzbare Bars von zwei Symbolen in vollständigen Fenstern der Variante vorliegen. Diese Extrapolation setzt keine belegte historische Vollständigkeit voraus.

1. Zuerst die manuellen Stichproben begutachten und je Abschnitt entscheiden, ob die Entitlements, Security-Epochen und Exit-Belege einen kleinen Marktdaten-Pilot tragen. Zeitfenster ab einem späteren, vollständig belegten Start kann separat beurteilt werden. Ein Feedwechsel, eine beleglose Forward-Fill-Reihe oder ein heutiger Ticker über den alten Exit hinweg schließt die Lücke fachlich nicht.
2. Erst nach dieser Entscheidung mit höchstens fünf Securities je Batch und zwölf Monaten pro Abrufabschnitt beginnen. Seiten streamen und je Symbol/Epoche, Zeitraum, Provider, Feed und Adjustment getrennte Shards erzeugen. Vollständigkeit gegen die jeweilige Sessionliste und Listing-/Exit-Grenzen prüfen. Unvollständige Felder und Exits als Lücken führen.
3. Resume nur über einen überprüften Completion-Checkpoint. Schlüssel sind Provider, Feed, Adjustment, Security-Epoche, Symbol, `asof`, Intervall, Schema-Version und Quellen-/Shard-Hashes. Staging bis zur atomaren Veröffentlichung von validiertem Shard und referenzierendem Manifest behalten. Danach können ausschließlich entbehrliche, ungebundene Transferdateien in einem getrennten Schritt ausgesondert werden. Referenzquellen, gebundene Originale, Manifeste und Siegel bleiben Teil der Wiederherstellungskette.
4. Erst danach SEC-Original-Batches für tatsächlich qualifizierbare Fälle planen. Als kostenlose Ergänzung kommen datierte veröffentlichte Börsenmitteilungen und Issuer-/Merger-Unterlagen in Frage. Die Lücke bei älteren konsistenten Marktdaten, vollständigen delisteten Preisreihen und Exit-Auszahlungen bleibt bis zu einer konkreten Quellenprüfung offen.

## PowerShell und Validierung

Der erste Aufruf ist rein lokal und benötigt nur die gebundenen Reports. `--measure-local-sip` wiederholt bei Bedarf separat die kleine Read-only-Speichermessung. Für die beiden Provider-Aufrufe zuerst den kostenlosen Tarif im jeweiligen Konto prüfen. Eine vorhandene `.env` kann explizit als lokale Credential-Datei übergeben werden. Alternativ stammen die bekannten Variablen `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` und `MASSIVE_API_KEY` aus der Umgebung. Die Zielreports enthalten Marktdaten und sind für lokale private Auswertung vorgesehen.

```powershell
Set-Location 'G:\GitHub\TraWalp'
$Py = '.\.venv\Scripts\python.exe'
$ProbeScript = '.\reports\f01_historical_market_provider_probe.py'
$Run = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$Out = 'G:\TraWalpResearch\import-artifacts'
& $Py -B -X utf8 $ProbeScript --output (Join-Path $Out "f01-market-$Run-offline.json")
if ($LASTEXITCODE -ne 0) { throw 'Offline-Preflight fehlgeschlagen.' }

# Optional, nur lokal und lesend
& $Py -B -X utf8 $ProbeScript --measure-local-sip --output (Join-Path $Out "f01-market-$Run-sip-sizing.json")

# Jeweils eigene manuelle Netzwerkautorisierung
& $Py -B -X utf8 $ProbeScript --probe alpaca --authorize-provider-network --credentials-file .\.env --output (Join-Path $Out "f01-market-$Run-alpaca.json")
& $Py -B -X utf8 $ProbeScript --probe massive --authorize-provider-network --credentials-file .\.env --output (Join-Path $Out "f01-market-$Run-massive.json")
```

Fünf gezielte Offline-Tests mit HTTP-Doubles bzw. kleinen Fixtures und Ruff bestanden. Vorhandene D.6-Quellen, der freigegebene Reader und die Strategie wurden unverändert verwendet. Das Hilfsskript liegt außerhalb des Candidate-Code-Inventars. Die Gitignore-Ausnahmen betreffen ausschließlich die neuen, lokalen Diagnose- und Auditdateien. Entscheidungen zu PIT, Trading und Produktion bleiben gesperrt.
