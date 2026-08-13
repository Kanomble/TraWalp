# TraWalp Trading System

Ein modularer Research-Unterbau für die Strategie **High Quality + Attractive Valuation +
Price Dislocation + Recovery Signal**. Die erste Zielversion ist ausschließlich für Screening,
Backtests, Dry Runs und Alpaca Paper Trading vorgesehen. Live-Trading ist weder implementiert
noch zulässig.

## Aktueller Stand: Milestone 3

Implementiert sind die Projektstruktur, validierte zentrale Konfiguration, ein ausschließlich
lesender Alpaca-Adapter, SEC-EDGAR-/Company-Facts-Zugriff, robustes US-GAAP-Tag-Mapping,
Universe-Basisfilter und SQLite-Persistenz. Die Datenbank bewahrt Filing-Datum, Periode,
Formular und Accession Number jeder Fundamental-Beobachtung auf. Historische Abfragen filtern
explizit nach `filed <= as_of`; dadurch steht die notwendige Point-in-Time-Grundlage bereit.

Milestone 2 ergänzt Point-in-Time-TTM-Ableitungen, fundamentale Qualitäts- und
Bewertungskennzahlen, sämtliche technischen Indikatoren, hierarchische SIC-Peer-Gruppen sowie
die vier erklärbaren Scores einschließlich Missing-Data-Reweighting und robuster
Ausreißerbehandlung.

Milestone 3 ergänzt den lokalen Point-in-Time-Screener, konfigurierbare Hard Filters, Ranking,
detaillierte Explain-Ausgaben und atomar geschriebene CSV-/JSON-Reports. Ein einzelner defekter
Datensatz beendet den Screen nicht, sondern wird mit einem konkreten Ausschlussgrund markiert.

Noch **nicht** implementiert sind Milestones 4–5: Backtester, Strategie-Vergleich, Risk Management
und Paper Orders. Die entsprechenden CLI-Befehle werden bewusst erst in ihren Milestones ergänzt.

## Strategie

Das spätere Ranking kombiniert vier erklärbare Teilbereiche:

1. Quality: Wachstum, Cashflow, Margen, ROIC und Bilanzqualität.
2. Valuation: relative Branchenbewertung und Free-Cashflow-Rendite.
3. Opportunity: relevanter, aber nicht beliebig tiefer Kursrückgang.
4. Timing: Stabilisierung und Erholung statt eines simplen `RSI < 30`-Kaufs.

Alle Gewichte, Score-Kurven und technischen Recovery-Grenzen liegen zentral in
`config/strategy.yaml` und werden beim Laden validiert. Fehlende Faktoren werden nicht zu null.
Die Gewichte aller berechenbaren Faktoren werden immer proportional als
`normalized_available_weight` ausgewiesen. Erst wenn die konfigurierte Mindestanzahl erreicht
ist, werden sie als `effective_weight` für den Gesamtscore wirksam. Andernfalls nennt die
Explain-Ausgabe den konkreten `reason_score_unavailable`.

## Architektur

```text
config/strategy.yaml                 zentrale Strategieparameter
src/trading_system/config.py         Environment + validierte Konfiguration
src/trading_system/models/           unveränderliche Domainmodelle
src/trading_system/data/
  alpaca_client.py                    Assets und adjustierte Tagesbars (read-only)
  market_sessions.py                  abgeschlossene XNYS-Sessions und Request-Fenster
  sec_client.py                       Fair-Access-konformer SEC-Client mit Retries
  xbrl_parser.py                      alternative US-GAAP-Tags und Normalisierung
  universe.py                         liquide US-Aktien, Market-Cap-Fallback, SIC-Filter
  database.py                         SQLite, Upserts, Cache, Point-in-Time-Abfragen
  sync.py                             inkrementelle Synchronisation
src/trading_system/fundamentals/
  debug.py                            XBRL-Provenienz und Ableitungsformeln
  metrics.py                          PIT-TTM, Quality- und Bewertungskennzahlen
  quality.py                          fundamentaler Point-in-Time-Snapshot
  peers.py                            SIC-Fallback-Gruppen und Branchenmediane
src/trading_system/technical/
  indicators.py                       SMA, EMA, RSI, ATR, Momentum, Volumen, Drawdown
  momentum.py                         Stabilisierung und Recovery-Zustand
src/trading_system/strategy/scoring.py vier Teil-Scores und Total Score
src/trading_system/strategy/screener.py PIT-Screen, Universe und Hard Filters
src/trading_system/strategy/reporting.py CSV/JSON, Rangliste und Explain-Text
src/trading_system/ai/                Schema und JSON-Export für manuelle AI-Analyse
src/trading_system/cli.py             Sync-, Screen-, Export-, Explain- und Diagnose-CLI
tests/                                isolierte Unit-/Integrationstests ohne echte APIs
```

Die Schichten trennen externe SDK-Objekte von den internen Modellen. SQLite speichert Dezimalwerte
als Text, damit finanzielle Werte nicht still durch Binär-Float-Rundung verändert werden.

## Setup

Voraussetzung ist Python 3.11 oder neuer.

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

`.env` ist ignoriert und darf nie committed werden. Die Anwendung verlangt Secrets erst beim
Aufbau des jeweiligen externen Clients; Konfiguration und Tests funktionieren ohne API Keys.

## Alpaca Paper API Keys

In `.env` eintragen:

```text
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TRADING_MODE=paper
ENABLE_ORDER_SUBMISSION=false
```

Der Milestone-1-Adapter erzeugt `TradingClient(..., paper=True)` nur zum Lesen des Asset-Universums
und bietet keine Order-Methode an. `ENABLE_ORDER_SUBMISSION` bleibt wirkungslos, bis Paper Trading
in Milestone 5 sicher implementiert und getestet wurde. Ein anderer Wert als `paper` wird von der
Konfiguration abgelehnt.

## SEC User Agent

Die SEC verlangt einen identifizierbaren User-Agent mit Kontaktmöglichkeit:

```text
SEC_USER_AGENT=Vorname Nachname name@example.com
```

Der Client begrenzt die Aufruffrequenz (Default 0,11 Sekunden), verwendet exponentielles Backoff
bei 429/5xx und akzeptiert komprimierte HTTP-Antworten. Das ist bewusst konservativer als zehn
Requests pro Sekunde. Details: <https://www.sec.gov/os/accessing-edgar-data>.

## Daten synchronisieren

Die Datenpipeline besteht aus getrennten Stufen. Der bestehende Befehl `sync` bleibt aus
Kompatibilitätsgründen ein vollständiger Lauf; `--full` macht diese Absicht explizit. Er aktualisiert
Asset-Universum, SEC Submissions und Company Facts sowie historische Tagesbars und eignet sich für
Initialisierung, Reparatur und gelegentliche komplette Aktualisierungen:

```bash
python -m trading_system.cli sync --full
python -m trading_system.cli sync --full --symbols ORCL AAPL MSFT
```

Die SEC-Ticker-/CIK-Referenz wird dabei aktualisiert und anschließend lokal wiederverwendet, statt
bei jedem inkrementellen Lauf neu aufgebaut zu werden.

Der tägliche SEC-Lauf prüft dagegen nur die aktuellen Submissions. Company Facts werden nur dann
geladen und neu geparst, wenn eine neue relevante 10-K-/10-Q-/20-F-/40-F-/Amendment-Accession
vorliegt oder lokal noch keine Facts existieren. 8-K-Filings lösen keinen Download aus, weil der
Parser daraus keine Screening-Facts übernimmt. Der Zustand stammt aus SEC-Accessions, nicht nur aus
einer lokalen Uhrzeit; Upserts machen wiederholte Läufe idempotent:

```bash
python -m trading_system.cli sync --incremental
```

Asset-Universum, historische Bars und aktuelle Snapshots lassen sich unabhängig aktualisieren:

```bash
python -m trading_system.cli sync-assets
python -m trading_system.cli update-bars
python -m trading_system.cli refresh-market
python -m trading_system.cli status
```

`update-bars` verwendet weiterhin adjustierte Daily Bars. Der erste Abruf umfasst ungefähr 480
Kalendertage; Folgeläufe starten sieben Tage vor dem zuletzt gespeicherten Bar, um
Provider-Korrekturen per Upsert zu übernehmen. Die letzten Bar-Zeitpunkte werden für das gesamte
Universum in einer Abfrage gelesen, und API-Aufrufe erfolgen in Batches.

`refresh-market` nutzt Alpacas Multi-Symbol-Snapshot-Endpunkt in begrenzten Batches. Aktuelle Trades
werden getrennt von der adjustierten historischen OHLCV-Serie gespeichert; der Befehl lädt keine
Kurshistorie. Finanzwerte und REITs, die bereits durch die bestehende Konfiguration ausgeschlossen
sind, werden vor dem Snapshot-Abruf entfernt. Ein Snapshot-Trade wird im Screening nur verwendet,
wenn sein Datum exakt der vollständig abgeschlossenen Analyse-Session entspricht. Dadurch gelangen
weder Intraday- noch Zukunftspreise in historische Screens.

Der empfohlene Tagesablauf lautet:

```bash
# nachts / morgens
python -m trading_system.cli sync --incremental
python -m trading_system.cli update-bars

# kurz vor dem Screening
python -m trading_system.cli refresh-market
python -m trading_system.cli screen
```

`status` zeigt Erfolg, Modus, Datensatzmengen, Fehler und Laufzeit getrennt für Asset-Universum,
SEC-Fundamentals, historische Bars und Market Snapshot. Fundamentaldaten ändern sich nur mit neuen
Filings und müssen daher nicht unmittelbar vor jedem Screen aktualisiert werden. Snapshots und der
neueste abgeschlossene Daily Bar sind zeitkritischer. `screen` startet niemals automatisch einen
Sync; fehlende oder alte Freshness-Metadaten erzeugen lediglich Warnungen.

Normalisierte SEC-Facts, kompakter SEC-Sync-Status, Assets, Unternehmen, Tagesbars und Snapshots
landen standardmäßig in `data/trading_system.sqlite3`. Company-Facts-JSON wird nach erfolgreichem
Parse und atomarem Upsert verworfen. Ein Parser- oder Datenbankfehler schreibt die zugehörige
Accession nicht als erfolgreich; der nächste inkrementelle Lauf kann den Import wiederholen.

## SQLite-Speicher und alter SEC-Cache

`fundamental_facts` ist die dauerhafte, normalisierte Historie. Sie enthält unter anderem
`filed`, `period_end`, `accession_number` und `frame`; historische Screens lesen weiterhin nur
Facts mit `filed <= as_of`. `raw_sec_cache` ist dagegen ein Quell-Cache aus älteren Versionen und
nicht die historische Fundamentaldatenbank. Das Entfernen eines abgesicherten Raw-Cache-Eintrags
entfernt keine normalisierten Facts und keine Daily Bars.

Die aktuelle Belegung lässt sich ohne Schreibzugriff untersuchen:

```bash
python -m trading_system.cli storage-report
```

Der Report zeigt Dateigröße, SQLite-Pages, Freelist, Zeilenzahlen und Raw-Cache-Größen nach
Endpunkt. Falls das lokale SQLite ohne `dbstat` gebaut wurde, fehlen nur die exakten Größen pro
Tabelle/Index; die übrigen Werte werden weiterhin ausgegeben. Die Analyse kann bei einer großen
Datenbank etwas dauern, weil SQLite die Payload-Längen und Zeilenzahlen lesen muss.

Alte Company-Facts-Payloads werden niemals beim Start, Sync oder Screening automatisch gelöscht.
Zuerst sollte ein Dry Run ausgeführt werden:

```bash
python -m trading_system.cli db-cleanup --dry-run
```

Der Guard gibt nur Company-Facts-Zeilen frei, für deren CIK bereits mindestens ein normalisierter
Fact existiert. Raw-Zeilen ohne strukturierte Facts bleiben erhalten, weil sie aus einem alten,
vor dem Parse geschriebenen Cache stammen und die einzige lokale Quellkopie sein können.
Submissions und alle anderen Endpunkte bleiben beim Standard-Cleanup ebenfalls erhalten. Erst der
explizite Befehl löscht die freigegebenen Zeilen innerhalb einer Transaktion:

```bash
python -m trading_system.cli db-cleanup
```

`DELETE` verkleinert die SQLite-Datei nicht; die frei gewordenen Pages erscheinen zunächst in der
Freelist. Optional kann direkt danach ein ebenfalls explizites VACUUM angefordert werden:

```bash
python -m trading_system.cli db-cleanup --vacuum
```

Vor `VACUUM` werden aktuelle Dateigröße, verfügbarer Speicher und ein konservativer temporärer
Speicherbedarf ausgegeben. TraWalp verweigert den Start, wenn nicht mindestens noch einmal die
aktuelle Datenbankgröße frei ist. `VACUUM` kann lange exklusiv arbeiten und vorübergehend viel
zusätzlichen Speicher belegen. Es wird daher nie automatisch ausgeführt.

Wer die Offline-Reparse- oder Source-Debug-Möglichkeit des alten Caches behalten möchte, sollte die
SQLite-Datei vor dem Cleanup bewusst sichern oder die Raw-Payloads separat archivieren. TraWalp
dupliziert eine rund 15-GB-Datei nicht ungefragt. JSON komprimiert sehr gut; ein kleiner produktiver
gzip-Test erreichte 6,8 % der Rohgröße. Ein optionales externes Archiv ist deshalb eine mögliche
spätere Erweiterung, aber kein Bestandteil des operativen Datenmodells. Die vollständige
Entscheidungsanalyse steht in [docs/sec-storage-decision.md](docs/sec-storage-decision.md).

Der Default-Feed ist `iex`, damit ein üblicher Paper-Account ohne SIP-Abonnement funktioniert.
Wer SIP-Zugriff besitzt, kann `universe.market_data_feed: sip` konfigurieren.

Der historische Market-Data-Import wird auf handelbare Symbole mit einer lokal bestätigten
SEC-Unternehmensidentität begrenzt und in 200er-Batches unmittelbar persistiert. Ein fehlerhafter
Provider-Batch kann dadurch keine bereits erfolgreich geladenen Batches mehr verwerfen. Alpaca
liefert für einzelne illiquide Sessions mitunter flache Bars ohne Trades und mit `vwap=0`. In
diesem exakt abgegrenzten Fall bleibt der Bar erhalten, während VWAP als `unavailable` (`NULL`)
gespeichert wird. Ein nichtpositiver VWAP bei tatsächlichem Handelsvolumen bleibt dagegen ein
ungültiger Datenpunkt und wird mit Symbol und Timestamp protokolliert.

## Screening und Aktie erklären

Der Screener arbeitet ausschließlich auf der lokalen SQLite-Datenbank und benötigt daher keine
API Keys. Ohne `--as-of` wird nicht blind das lokale Kalenderdatum verwendet: XNYS-Kalender und
tatsächlicher regulärer Session-Schluss bestimmen die letzte vollständig abgeschlossene
US-Handelssitzung. Vor dem US-Börsenschluss bleiben damit ein unvollständiger Daily Bar,
Overnight-Preis und Intraday-Snapshot vollständig aus der technischen Analyse ausgeschlossen.

```bash
python -m trading_system.cli screen
python -m trading_system.cli explain ORCL
```

Ein historischer, reproduzierbarer Stichtag kann explizit gesetzt werden:

```bash
python -m trading_system.cli screen --as-of 2025-12-31 --limit 20
python -m trading_system.cli explain ORCL --as-of 2025-12-31
```

Die Terminaltabelle enthält nur Titel, die sämtliche Universe-, Datenqualitäts- und Hard-Filter
bestehen. Die Dateien `reports/screen_YYYY-MM-DD.csv` und `.json` enthalten dagegen alle
analysierten Titel einschließlich Ausschlussgründen und Datenwarnungen. Der flache CSV-Export
enthält Rohmetriken, Branchenmediane, Faktorwerte, Faktor-Scores und effektive Gewichte; JSON
erhält die vollständige verschachtelte Explainability-Struktur.

`explain` berechnet denselben vollständigen Querschnitt wie `screen`, damit Peer-Perzentile und
Branchenmediane identisch sind. Die Ausgabe zeigt Quality, Valuation, Opportunity, Timing, sämtliche
wichtigen Rohwerte, Faktorbeiträge, Total Score und gegebenenfalls Ausschlussgründe.

### Kandidaten für eine manuelle AI-Analyse exportieren

`export-ai` verwendet denselben lokalen Point-in-Time-Screener und exportiert standardmäßig die
20 bestplatzierten berechtigten Kandidaten. Es werden keine OpenAI- oder sonstigen externen
AI-Anfragen ausgeführt. Fehlende Kennzahlen bleiben im JSON als `null` erhalten.

```bash
python -m trading_system.cli export-ai
python -m trading_system.cli export-ai --limit 20
python -m trading_system.cli export-ai --output output/custom_name.json
python -m trading_system.cli export-ai --as-of 2025-12-31 --limit 10
```

Ohne `--output` wird eine Datei nach dem Muster
`output/ai_candidates_YYYY-MM-DD_HHMMSS.json` erzeugt. Sie enthält Identität, Screener-Rang und
-Scores, technische Momentum- und Trenddaten, Fundamentals, Risiko- und Volatilitätsdaten sowie
eine kurze Analyseanweisung für den manuellen Upload zu ChatGPT. Wenn kein Titel die konfigurierten
Filter besteht, endet der Befehl mit einer verständlichen Fehlermeldung und erzeugt keine Datei.

Für eine gezielte Datenprüfung stehen drei Diagnosebefehle zur Verfügung:

```bash
python -m trading_system.cli debug-peers MSFT
python -m trading_system.cli debug-fundamentals MSFT
python -m trading_system.cli debug-market MSFT
```

`debug-peers` zeigt die 4-/3-/2-stellige SIC-Fallback-Hierarchie, Gruppen- und gültige
Multiple-Anzahlen sowie Mediane. `debug-fundamentals` zeigt XBRL-Concept, Filing, Fiscal Period,
Filed Date, Unit und Formel aller wesentlichen Roh- und abgeleiteten Kennzahlen. `debug-market`
zeigt den angeforderten Zeitraum, Feed, Adjustment, effektive Session, die letzten zehn Bars und
alle technischen Inputs. Sämtliche technischen Kennzahlen eines Laufs stammen aus genau derselben
Serie abgeschlossener, identisch adjustierter Daily Bars.

Mit dem Default `peers.min_peer_count: 8` sollte nicht nur eine kleine Symbolauswahl synchronisiert
werden. Zu kleine lokale Universen liefern absichtlich keine Branchenmultiples und können deshalb
am Valuation-Datenqualitätsfilter scheitern.

## Backtest und Strategie-Vergleich

Point-in-Time-Backtests und Next-Day-Ausführung folgen in Milestone 4:

```bash
python -m trading_system.cli backtest --start 2020-01-01 --end 2025-12-31
python -m trading_system.cli compare-strategies
```

Die bereits implementierte Datenbasis verhindert, dass ein am 5. Mai eingereichter Bericht am
20. April sichtbar ist. Die spätere Engine muss zusätzlich Signale von Tag T frühestens am
nächsten verfügbaren Kurs ausführen. Tests in `tests/test_database.py` sichern Filing- und
Amendment-Grenzen explizit ab.

## Paper Trading und Sicherheitsmechanismen

`run-daily` folgt erst in Milestone 5. Vorgesehen ist standardmäßig ein reiner Dry Run. Paper
Orders dürfen später nur bei der Kombination `TRADING_MODE=paper` und
`ENABLE_ORDER_SUBMISSION=true` gesendet werden. Live-Endpunkte und Live-Orderpfade werden in
Version 1 nicht implementiert.

## Tests und Codequalität

```bash
python -m pytest --cov=trading_system --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
```

Die Tests decken zusätzlich TTM-Ableitungen aus Q1/H1/9M/FY, Filing-Date-Look-Ahead, FCF, Wachstum,
ROIC, Debt/EBITDA, P/E, EV, EV/EBITDA, FCF Yield, negative Earnings/EBITDA, SMA, EMA, Wilder-RSI,
ATR, Momentum, Drawdown, Relative Volume, Recovery-Logik, SIC-Fallbacks, Peer-Mediane,
Winsorization, nichtlineare Opportunity-Scores, Missing-Data-Reweighting und Total Score ab.
Milestone-3-Tests führen zusätzlich vollständige SQLite-Screens mit historischen Stichtagen,
Rankings, Hard Filters, CSV/JSON-Roundtrips und Explain-Ausgaben aus. Sie führen keine echten
API-Aufrufe und keine Orders aus.

Regressionstests sichern außerdem absolute Growth-Scores ohne Peers, normalisierte und effektive
Missing-Data-Gewichte, unzureichende sowie zurückfallende Peer-Gruppen, D&A-/EBITDA-Ableitungen,
die Auswahl der jüngsten Schuldenrepräsentation, abgeschlossene XNYS-Sessions und die explizite
Corporate-Action-Adjustment-Policy ab. Weitere Tests decken Alpaca-Zero-Trade-Bars,
VWAP-Normalisierung und die Persistenz erfolgreicher Market-Data-Batches trotz eines anderen
fehlgeschlagenen Batches ab.

## Datenannahmen und Einschränkungen

- SEC-Tags sind heterogen. Das Mapping speichert den tatsächlich verwendeten Tag. Gibt es keinen
  verlässlichen Wert, bleibt die Kennzahl unavailable; sie wird nicht als null erfunden.
- `total_debt` verwendet semantisch kombinierte Gesamtverschuldungs-Tags. Current und non-current
  debt werden nur bei identischem Bilanzdatum addiert. Zwischen beiden Repräsentationen gewinnt
  die jüngere Periode; ein veralteter Direkt-Tag kann aktuelle Komponenten nicht verdrängen.
- Market Cap wird als `latest price × shares outstanding` berechnet. Fehlen Shares,
  scheitert der Filter geschlossen statt eine Größe zu schätzen.
- SIC 6000–6799 und SIC 6798 (REIT) werden entsprechend der Konfiguration ausgeschlossen. SIC ist
  nur eine grobe Branchenklassifikation.
- Alpaca-Verfügbarkeit, Feed-Abonnement und Datenabdeckung hängen vom Account ab. API-Fehler werden
  geloggt, ohne Credentials auszugeben.
- TTM-Flows werden aus vier diskreten Quartalen gebildet. Kumulative SEC-Werte werden als
  `Q2 = H1-Q1`, `Q3 = 9M-H1` und `Q4 = FY-9M` aufgelöst. Einheit, Periodenende und spätestes
  beteiligtes Filing-Datum bleiben erhalten. FCF und EBITDA werden nur aus periodengleichen
  TTM-Komponenten gebildet.
- EBITDA ist bevorzugt `Operating Income + Depreciation & Amortization`. Wenn diese Komponenten
  fehlen, ist `Net Income + Interest + Taxes + D&A` nur bei vollständig vorhandenen,
  periodengleichen Komponenten zulässig. Sonst bleibt EBITDA unavailable; EV/EBIT wird als
  dokumentierter Bewertungs-Fallback berechnet. Der effektive Steuersatz wird mangels
  konsistentem Pretax-Income-Tag als
  `Tax Expense / (Net Income + Tax Expense)` angenähert; unplausible Werte bleiben unavailable.
- Für Invested Capital wird in Version 1 sämtliches Cash als Excess Cash behandelt. ROIC nutzt den
  Durchschnitt aus aktueller und Vorjahres-Bilanz.
- Peer-Gruppen fallen von vierstelliger auf drei- und danach zweistellige SIC-Klassifikation
  zurück. Ein Median wird nur bei mindestens `peers.min_peer_count` gültigen Beobachtungen erzeugt.
- Ein Screen verwendet nur Bars bis zur letzten vollständig abgeschlossenen regulären
  XNYS-Session und SEC-Facts mit `filed <=` tatsächlich verwendetem Bar-Datum. Mindestens 300
  lokale Bars sind standardmäßig Pflicht. Ergebnisse sind Research-Ausgaben, noch keine
  handelbare Strategie oder Kaufempfehlung.

## Milestone-Reihenfolge

- Milestone 1 (fertig): Struktur, Config, Alpaca/SEC, lokale Datenbank.
- Milestone 2 (fertig): Fundamentals, technische Indikatoren, Peer-Gruppen und Scoring.
- Milestone 3 (fertig): Screener, CLI-Ausgaben und Explainability.
- Milestone 4: Point-in-Time-Backtester, Reports und Strategie-Vergleich.
- Milestone 5: ausschließlich Alpaca Paper Trading, Risk Management und Daily Runner.
