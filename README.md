# TraWalp Trading System

Ein modularer Research-Unterbau für die Strategie **High Quality + Attractive Valuation +
Price Dislocation + Recovery Signal**. Die erste Zielversion ist ausschließlich für Screening,
Backtests, Dry Runs und Alpaca Paper Trading vorgesehen. Live-Trading ist weder implementiert
noch zulässig.

## Aktueller Stand: Research-Baseline nach Milestone 4

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

Milestone 4 ergänzt einen lokalen Point-in-Time-Backtester, simuliertes Portfolio- und Risk
Management, nachvollziehbare Trade-/Equity-Reports, einen lokalen SPY-Benchmark und den fairen
Vergleich der Strategievarianten A/B/C. Noch **nicht** implementiert ist Milestone 5: Paper Orders.
Es existiert weiterhin kein Codepfad, der eine Alpaca-Order absendet.

Die aktuelle Research-Baseline umfasst darüber hinaus historischen Daily-Backfill, Candidate-
Funnel-Audits, Position- und Execution-Leg-Diagnostik, MFE/MAE, Profit Capture, Re-Entry- und
Post-Exit-Analysen sowie native `5m`-/`15m`-/`1h`-Bars, Strategy F und Multi-Timeframe-
Strategy-Compare. Diese Erweiterungen ändern nicht die Grenze zu Milestone 5: TraWalp sendet
weiterhin keine Orders.

## Strategie

Das spätere Ranking kombiniert vier erklärbare Teilbereiche:

1. Quality: Wachstum, Cashflow, Margen, ROIC und Bilanzqualität.
2. Valuation: relative Branchenbewertung und Free-Cashflow-Rendite.
3. Opportunity: relevanter, aber nicht beliebig tiefer Kursrückgang.
4. Timing: Stabilisierung und Erholung statt eines simplen `RSI < 30`-Kaufs.

Alle Gewichte, Score-Kurven und technischen Recovery-Grenzen liegen zentral in
`config/strategy.yaml` und werden beim Laden validiert. Fehlende Faktoren werden nicht zu null.
Der Default-Pfad sowie relative Datenbank-/Reportpfade werden gegen die Repository-/Config-Basis
aufgelöst; CLI-Aufrufe sind deshalb nicht vom aktuellen Working Directory abhängig. Eine explizite
Datei kann weiterhin mit `--config <pfad>` gewählt werden.
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
src/trading_system/backtest/            PIT-Engine, Performance-Metriken und Reports
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

Die SEC-Ticker-/CIK-Referenz wird bei jedem Lauf mit einem einzelnen Request aktualisiert. Dadurch
werden neue Listings sichtbar, ohne tausende Unternehmensendpunkte abzufragen. Sichere
Class-Share-Aliase wie `BRK.B` → `BRK-B` werden normalisiert; unsichere Namens- oder
Ähnlichkeits-Matches finden nicht statt.

Persistierte Unternehmensidentitäten werden nicht stillschweigend durch eine neue SEC-Ticker-
Zuordnung überschrieben. Widersprechen sich vorhandene und aktuelle Symbol-/CIK-Beziehungen – etwa
bei Ticker-Reuse, Fusion oder Umbenennung –, wird der Vorschlag vor jedem Company-Request als
`identity_conflict` quarantänisiert. Der bestehende Company-, Fact- und Bar-Bestand bleibt erhalten,
`errors` und `database_failures` bleiben unverändert, und der SEC-Stage-Status wird zur manuellen
Prüfung `partial`. Ein Dot-/Hyphen-Alias darf ebenfalls keinen bereits gespeicherten CIK umbenennen.
Der Sync persistiert aktive Konflikte kompakt in `sync_state` unter `sec_identity_conflicts`. Diese
lokale Quarantäne ist anschließend die gemeinsame Quelle für Screening, Snapshot-Refresh und
Daily-Bar-Updates; die drei Pfade erzeugen dafür keine zusätzlichen SEC-Requests.
Für Datenbanken aus der Version vor dieser Persistenz ergänzen dieselben Guards den Zustand
vorübergehend durch den identischen Resolver auf der bereits lokal gecachten SEC-Ticker-Referenz.
Damit besteht auch vor dem ersten neuen Sync kein ungeschütztes Screening-Fenster.

Ein quarantänisiertes Symbol erhält im Screen einen nicht geeigneten Record mit
`identity_conflict`, ohne Facts, Bars, Snapshots oder Analysefunktionen zu laden. `refresh-market`
und `update-bars` überspringen es vor Alpaca-Requests und melden `identity_conflicts_skipped`.
Bereits gespeicherte Facts, Bars und Snapshots bleiben unverändert. Weil TraWalp noch kein
verifiziertes Datum für einen Ticker-Besitzerwechsel speichert, gilt die Sperre konservativ auch
für historische `--as-of`-Screens. Ein späterer SEC-Sync löscht sie automatisch, sobald die
persistierte und aktuelle Zuordnung wieder übereinstimmen oder kein alter Company-Owner mehr unter
dem Symbol gespeichert ist.

Der tägliche SEC-Lauf lädt zunächst den offiziellen, ungefähr 2–3 MiB großen XBRL-Index des
aktuellen Quartals. Er vergleicht dessen Accessions mit dem lokalen Zustand und ruft Submissions und
Company Facts nur für neue, vom Parser unterstützte 10-K-/10-Q-/20-F-/40-F-/Amendment-Filings ab.
8-K-/6-K-Filings lösen weiterhin keinen Download aus, weil der Parser daraus keine
Screening-Facts übernimmt. Verpasste Läufe werden über archivierte Quartalsindizes aufgeholt. Der
globale Cursor wird nur nach einem fehlerfreien Lauf vorgeschoben; ein unvollständiger oder
fehlerhafter Index bricht sicher ab. Der aktuelle SEC-Index kann bis zum nächsten Geschäftstag
hinter einem gerade eingereichten Filing liegen, verliert das Filing aber nicht:

```bash
python -m trading_system.cli sync --incremental
```

Asset-Universum, historische Bars und aktuelle Snapshots lassen sich unabhängig aktualisieren:

```bash
python -m trading_system.cli sync-assets
python -m trading_system.cli update-bars
python -m trading_system.cli sync-intraday --symbols AAPL,MSFT --start 2026-07-01 --end 2026-08-12 --timeframes 5m,15m,1h
python -m trading_system.cli refresh-market
python -m trading_system.cli data-status
```

Der Intraday-Backfill ist explizit, inkrementell und provider-nativ; der normale Sync lädt nicht
unbeabsichtigt 5-Minuten-Daten für das Gesamtuniversum. Schema, Warmup, Sessions und Strategy-F-
Integration beschreibt [`docs/intraday-market-data.md`](docs/intraday-market-data.md).
Der Full-Universe-Daily-Warmup, beidseitige Coverage-Gaps, SPY-Historie und der anschließende
Candidate-Audit-Workflow sind in
[`docs/historical-daily-backfill.md`](docs/historical-daily-backfill.md) dokumentiert.

`sync-assets` behandelt Alpacas vollständige Liste aktiver, handelbarer US-Aktien als aktuellen
Snapshot. Alle enthaltenen Symbole werden eingefügt oder aktualisiert und erhalten `tradable=1`;
bislang handelbare, im erfolgreichen Snapshot aber fehlende Symbole bleiben aus historischen und
Audit-Gründen als Asset-Zeile erhalten und erhalten atomar `tradable=0`. Name, Börse sowie die
letzten bekannten Fractionable-/Shortable-Angaben werden bei dieser Deaktivierung nicht erfunden
oder gelöscht. Ein später erneut geliefertes Symbol wird durch denselben Upsert wieder aktiviert.

Die Reconciliation läuft in einer SQLite-Transaktion über eine temporäre Symboltabelle, sodass
Upserts und Deaktivierungen gemeinsam committen oder gemeinsam zurückgerollt werden. Ein
fehlgeschlagener Alpaca-Aufruf oder ein leerer Snapshot ändert keine Asset-Zeile. Als zusätzliche
Sicherung wird ein Snapshot abgelehnt, wenn er weniger als 50 % des zuvor handelbaren Universums
enthält; diese Grenze schützt vor offensichtlich unvollständigen Provider-Antworten. `sync-assets`
und `status` melden unter anderem empfangene, upsertete, deaktivierte und anschließend handelbare
Assets. Das kompatible Feld `records_updated` zählt dabei Snapshot-Upserts plus tatsächlich
deaktivierte Zeilen. SEC-Sync, Bar-Update, Market-Refresh und aktuelles Screening verwenden weiterhin
`tradable=1`, sodass deaktivierte Symbole automatisch aus den operativen Universen verschwinden,
während bestehende Facts und Bars erhalten bleiben. Die separate SEC-Identitätsquarantäne wird
anschließend unverändert zusätzlich angewendet.

`update-bars` verwendet weiterhin adjustierte Daily Bars. Der erste Abruf umfasst ungefähr 480
Kalendertage; Folgeläufe starten sieben Tage vor dem zuletzt gespeicherten Bar, um
Provider-Korrekturen per Upsert zu übernehmen. Die letzten Bar-Zeitpunkte werden für das gesamte
Universum in einer Abfrage gelesen, und API-Aufrufe erfolgen in Batches.

`refresh-market` nutzt Alpacas Multi-Symbol-Snapshot-Endpunkt in begrenzten Batches. Aktuelle Trades
werden getrennt von der adjustierten historischen OHLCV-Serie gespeichert; der Befehl lädt keine
Kurshistorie. Finanzwerte und REITs, die bereits durch die bestehende Konfiguration ausgeschlossen
sind, werden vor dem Snapshot-Abruf entfernt. Ein Snapshot-Trade wird im Screening nur verwendet,
wenn sein timezone-aware Timestamp innerhalb der offiziellen regulären XNYS-Session bis
einschließlich des offiziellen Close liegt. Same-Day-After-Hours-Trades und Trades späterer Sessions
gelangen dadurch nicht in abgeschlossene historische Screens.

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

SEC-Ergebnisse unterscheiden erwartete Quellenlücken von echten Fehlern. Ein HTTP 404 für Company
Facts erhöht `companyfacts_unavailable`, nicht `errors`, und erzeugt genau eine kurze INFO-Meldung.
Der kompakte negative Zustand wird standardmäßig sieben Tage gespeichert
(`sec.companyfacts_unavailable_ttl_days`). Eine neue unterstützte Accession, ein abgelaufener
Eintrag oder `sync --full` erzwingt einen erneuten Versuch. Request- und Laufzeitmetriken umfassen
Change Detection, Submissions, Company Facts sowie Parse/Persist; 429/5xx, Timeouts,
Verbindungs-, JSON-, Parser- und Datenbankfehler werden getrennt gezählt.

Nicht gemappte Alpaca-Symbole werden beobachtbar als ETF/Fund, Warrant, Unit, Right, Preferred,
Depositary/Foreign oder `unclassified` zusammengefasst. Diese Diagnose schließt keine gemappten
Aktien aus dem SEC-Sync aus. Insbesondere bleibt `unclassified` bewusst bestehen, wenn lokale
Metadaten keine zuverlässige Aussage erlauben. Diese sieben Gründe sind gegenseitig exklusiv;
`unmapped_otc_exchange` ist dagegen ein zusätzliches, überlappendes Exchange-Tag und darf nicht zu
den Gründen addiert werden.

Normalisierte SEC-Facts, kompakter SEC-Sync-Status, Assets, Unternehmen, Tagesbars und Snapshots
landen standardmäßig in `data/trading_system.sqlite3`. Company-Facts-JSON wird nach erfolgreichem
Parse und atomarem Upsert verworfen. Ein Parser- oder Datenbankfehler schreibt die zugehörige
Accession nicht als erfolgreich; der nächste inkrementelle Lauf kann den Import wiederholen.
Bestehende Legacy-Payloads werden durch den Sync weder gelöscht noch neu erzeugt.

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

Das Backtesting unterstützt zusätzlich ein konfigurierbares dynamisches Position Management mit
Stop Loss, Take/Partial Profit, profit- und ATR-basierten Trailing Stops, Signal Decay, optionalem
Max-Hold-Review, Re-Entry und Portfolio Rotation. Presets werden mit `backtest --strategy ...`
gewählt; `compare-strategies` vergleicht standardmäßig A/B/C und alle Position-Presets auf
identischen Point-in-Time-Screens. Details, Exit-Prioritäten und Lookahead-Regeln stehen in
[`docs/position-management.md`](docs/position-management.md).
Trade-Ideen, Partial-Exit-Legs, Profit Capture, Post-Exit-, Re-Entry-, Stop- und Score-Diagnostik
sind in [`docs/position-diagnostics.md`](docs/position-diagnostics.md) dokumentiert.

Der `BacktestEngine` läuft ausschließlich auf lokal gespeicherten Bars und Fundamentaldaten und
erzeugt selbst keine Netzwerkaufrufe. `compare-strategies` besitzt davor eine getrennte
Vorbereitungsphase, die bei Bedarf fehlende Intraday-Daten beschafft:

```bash
python -m trading_system.cli backtest --start 2025-05-01 --end 2025-06-30
python -m trading_system.cli backtest --start 2025-05-01 --end 2025-06-30 --variant A
python -m trading_system.cli compare-strategies --start 2025-05-01 --end 2025-06-30
```

### Automatischer Intraday-Prefetch für Vergleiche

`compare-strategies` löst zunächst alle angeforderten Position-Management-Presets auf und erkennt
daraus generisch die benötigten Timeframes (`5m`, `15m` oder `1h`). Für Intraday-Runs berechnet es
die historischen Point-in-Time-Screens einmal, verwendet mit `evaluate_variant_entry(...)` denselben
kanonischen Entry-Funnel wie der Backtester und sammelt nur daraus grundsätzlich entry-fähige
Symbole. Der letzte Vergleichstag wird nicht als Signal-Tag verwendet, weil daraus innerhalb des
Backtest-Horizonts kein Next-Session-Entry mehr entstehen kann.

Nur diese deduplizierten Kandidaten werden auf lokale Session-Abdeckung und den konfigurierten
`intraday.warmup_bars`-Vorlauf geprüft. Fehlende Bereiche werden über den bestehenden inkrementellen
Alpaca-Sync geladen; vorhandene Daten, `intraday.sync.overlap_bars`, Batching, Request-Fenster und
`intraday.extended_hours` werden unverändert wiederverwendet. Es findet kein Intraday-Download für
das gesamte Aktienuniversum statt. Nach der Vorbereitung arbeitet der Engine wieder ausschließlich
mit dem eingefrorenen lokalen Datenbestand. Providerfehler oder weiterhin unvollständige Coverage
skippen nur die betroffenen Intraday-Runs mit einer konkreten Ursache; Daily-Runs laufen weiter.

```powershell
python -m trading_system.cli compare-strategies `
  --start 2026-05-01 `
  --end 2026-08-12
```

Für einen vollständig offline ausgeführten Vergleich deaktiviert
`--no-intraday-prefetch` die automatische Beschaffung. Dann gilt das bisherige Verhalten:
Vorhandene lokale Intraday-Bars werden verwendet und Runs mit fehlenden Daten werden als skipped
ausgewiesen. Der manuelle Befehl `sync-intraday` bleibt unabhängig davon verfügbar.

### Aktive Intraday-Forschung und lokaler Preflight

Die zentrale Research-Registry trennt reproduzierbare Historie von der aktiven, teuren Pipeline.
F0/C-intraday-dynamic bleibt unveränderter `CHAMPION_CONTROL`; F3/C-intraday-thesis-recovery und
F5/C-intraday-first-hour-pullback-f0-management sind `ACTIVE_RESEARCH`. F1, F2, F4, D1–D5 und die
historischen Position-Management-Experimente bleiben reproduzierbar, sind jedoch archiviert und
werden nicht automatisch in aktive Vergleiche aufgenommen. Archived bedeutet ausdrücklich nicht
gelöscht. Die genaue Zuordnung steht in
[`docs/strategy-research-lifecycle.md`](docs/strategy-research-lifecycle.md).

Die explizite Familie `research-intraday-hybrid` enthält ausschließlich F0, F3 und F5. F5 ist die
kausale F4-Entry-Auswahl (EMA20, vollständige erste Stunde, erster bestätigter Pullback, Entry erst
am folgenden kanonischen 15-Minuten-Open) mit anschließend unverändertem F0-Management: 3-Prozent-
Katastrophenstop, sofortige ATR14-x1-Trail-Semantik, einmalig 50 Prozent bei +1,5 Prozent, Runner
und Daily-Score-Decay. Sie verwendet weder den F4-Stop noch dessen Swing-High- oder Session-Close-
Exit. Provider-native Lücken werden weder synthetisiert noch aufgefüllt oder überbrückt.

`compare-preflight` prüft eine lange Vergleichsperiode ausschließlich gegen den lokalen Bestand,
ohne einen Backtest zu starten. Es verwendet dieselbe Vergleichsvorbereitung und PIT-
Kandidatenermittlung wie `compare-strategies`, schreibt einen maschinenlesbaren JSON-Bericht sowie
einen mit `sync-intraday --candidates-report` kompatiblen Kandidatenbericht und nennt erforderliche
manuelle Daily-/Intraday-Sync-Befehle, führt sie aber nicht aus:

```powershell
python -m trading_system.cli compare-preflight `
  --start 2024-01-02 `
  --end 2026-08-12 `
  --include research-intraday-hybrid `
  --output-stem intraday_hybrid_preflight_2024-01-02_2026-08-12
```

Research-Vergleiche rechnen standardmäßig nur das konfigurierte Baseline-Kostenmodell. Erst das
explizite Flag `--cost-stress` aktiviert 2X-/3X-Slippage, Commission- und vorhandene
path-preserving Diagnosen; Reports speichern `cost_stress_requested` und
`cost_stress_executed`. Native Post-Exit- und Counterfactual-Hold-Werte sind reine, nachgelagerte
Diagnostik und beeinflussen den simulierten Pfad nie. Details stehen in
[`docs/position-diagnostics.md`](docs/position-diagnostics.md).

Der Zeitraum 2025-05-01 bis 2026-08-12 ist Entwicklungs-/In-Sample-Research. Frühere Daten werden
neutral als `historical_extension` bezeichnet und nicht automatisch als OOS gewertet. Kein Report
promotet eine Strategie automatisch.

### Point-in-Time- und Ausführungsmodell

Jede Simulation verwendet offizielle XNYS-Sessions, für die mindestens ein lokaler Bar existiert.
Pro Session ist die Reihenfolge fest:

1. Bereits offene Positionen werden am Open auf Stop-/Target-Gaps geprüft.
2. Signale vom vorherigen abgeschlossenen Handelstag werden zum aktuellen Open ausgeführt.
3. Intraday-Stop und Profit Target werden anhand von High/Low geprüft.
4. Der konfigurierte Time Exit wird am Close ausgeführt.
5. Erst nach dem Close läuft derselbe Screener wie im operativen Betrieb und erzeugt Orders für die
   nächste Session. Am letzten Backtest-Tag werden Positionen am letzten verfügbaren Close
   geschlossen und keine neuen Orders erzeugt.

Damit kann ein Signal vom Montagsschluss frühestens am Dienstag-Open handeln; Wochenenden und
NYSE-Feiertage werden übersprungen. Käufe erhalten einen Preisaufschlag, Verkäufe einen Abschlag
von `backtest.slippage_bps`; auf beide Seiten wird `commission_bps` angewendet. Berühren Stop und
Target denselben Daily Bar, ist die unbekannte Intraday-Reihenfolge konservativ: der Stop gilt als
zuerst getroffen. Gap-Ausführungen verwenden das schlechtere tatsächliche Open. Eine zusätzliche
Signal-Exit-Regel wird nicht erfunden, weil die aktuelle zentrale Konfiguration keine enthält.

SEC-Facts bleiben nur bei `filed <= as_of` sichtbar. Bars werden auf die jeweilige Session begrenzt;
SMA, RSI, ATR, Momentum, Relative Volume und 52-Wochen-Hoch sehen keine spätere Zeile. Historische
Screens ignorieren `market_snapshots` ausdrücklich, selbst wenn ein Snapshot zufällig dasselbe
Datum trägt. Peer-Gruppen und Branchenmediane werden pro Session aus genau diesem PIT-Screen
berechnet. Die aktuelle, konservative SEC-Identitätsquarantäne gilt für alle Backtest-Daten, weil
noch keine verifizierten Ticker-Besitzzeiträume existieren.

### Entry, Risiko und Portfolio

Die Defaults in `config/strategy.yaml` verlangen Quality ≥ 70, Valuation ≥ 60 und einen
variantenspezifischen Gesamtscore ≥ 75. B/C verlangen zusätzlich Opportunity ≥ 60, C zusätzlich
Timing ≥ 55. Alle Varianten verwenden denselben Recovery-Gate: Kurs über SMA20 und mindestens
eines aus RSI Recovery, Momentum5 > 0 oder Relative Volume > 1,2.

Das initiale Stop-Risiko ist das Minimum aus `ATR14 × atr_stop_multiple` und dem maximal erlaubten
prozentualen Stop-Abstand. Ohne positiven ATR wird kein Trade eröffnet. Die Stückzahl ergibt sich
aus Portfolio-Equity × `risk_per_trade` geteilt durch Risiko je Aktie und wird anschließend durch
Cash und `max_position_pct` begrenzt. Fractional Shares sind intern erlaubt; Leverage nicht. Das
Portfolio respektiert `max_positions` und `max_sector_positions` (SIC-Zweisteller, fehlendes SIC als
`unknown`). Defaults sind fünf Positionen, 20 % je Position und zwei Positionen je Sektor. Exits
sind Stop Loss, +12-%-Profit-Target, zehn Handelstage oder `end_of_backtest`.

### Strategievarianten und faire Berechnung

- A — Quality + Value: normalisierte bestehende Gewichte 40:30.
- B — Quality + Value + Opportunity: normalisierte Gewichte 40:30:20.
- C — Quality + Value + Opportunity + Timing: bestehende Gewichte 40:30:20:10.

Alle Varianten teilen Datenhorizont, Recovery-Gate, Portfolio, Risiko, Ausführung und Kosten. Nur
Komponentenmix und die zugehörigen Mindestwerte unterscheiden sich. `compare-strategies` berechnet
jeden Session-Screen einmal und cached ihn ausschließlich unter dem exakten Session-Datum; dadurch
können keine späteren Daten rückwärts gelangen. In diesem Milestone werden keine Parameter anhand
historischer Ergebnisse optimiert.

### Reports und Kennzahlen

`backtest` schreibt atomar JSON, Trade-CSV und Equity-CSV unter
`reports/backtest_<start>_<end>_<variant>.*`. Das JSON enthält Konfigurations-Snapshot,
angeforderten/tatsächlichen Zeitraum, Annahmen, Warnungen, Trades, Equity Curve und Benchmark.
Die Equity Curve weist Cash, Marktwert, Equity, Positionen sowie realisierten und unrealisierten
P&L je Session aus. `session_exposure`/die generische `exposure` misst den maximal tatsächlich im
Session-Verlauf gebundenen Kapitalanteil. Damit zählt auch ein am selben Tag eröffneter und
geschlossener Trade. `end_of_day_exposure` misst separat nur den Bestand am Session-Ende; aus Daily
OHLC wird keine scheinpräzise intraday zeitgewichtete Exposure abgeleitet.
`compare-strategies` schreibt JSON und eine kompakte CSV-Tabelle. Kennzahlen sind Total Return,
kalendertägig annualisierte CAGR, Maximum Drawdown, Sharpe und Sortino mit 252 Sessions/Jahr,
Win Rate, durchschnittlicher Gewinn/Verlust als Trade Return, Profit Factor, monetäre Expectancy,
Trade-Anzahl, Haltedauer, zweiseitiger Turnover und durchschnittliche Kapital-Exposure. Nicht
definierte Werte bleiben `null`/`N/A`, statt irreführend null zu werden.

Bei weniger als 63 lokalen Trading-Sessions setzt der Report
`annualized_metrics_reliable: false` und gibt eine Warnung aus. CAGR, Sharpe und Sortino können
rechnerisch weiterhin vorhanden sein, sind über einen so kurzen Horizont aber keine belastbare
Strategieerwartung. Maximum Drawdown basiert auf den aufgezeichneten Session-Equity-Punkten, nicht
auf einem rekonstruierten Intraday-Equity-Pfad.

### PIT-Feature-Pipeline und Performance

Historische Screens verwenden einen ausschließlich laufzeitlokalen, rebuildbaren Feature-Store. Er
lädt Bars über indexfreundliche Timestamp-Ranges in Batches, verwirft Finanzwerte/REITs und
offensichtlich unzureichende Preis-/Liquiditätshistorien vor teurer Rekonstruktion und lädt zunächst
nur Share-Facts für den Market-Cap-Gate. Erst dessen Überlebende erhalten den vollständigen,
filing-bewussten Accounting-Zustand. Technische Snapshot-Werte werden für exakt denselben auf 320
Bars begrenzten Prefix wie im operativen Screener vorberechnet.

Der Accounting-Cache ist nicht nur nach Symbol, sondern nach der tatsächlich sichtbaren
Filing-/Accession-Menge und dem ausgewählten Share-Fact getrennt. Eine spätere Filing- oder
Bar-Zeile kann daher keinen früheren Session-Key verändern. Accounting-Zustände werden zwischen
Filings wiederverwendet; historische Preise werden pro Session erst danach an die unveränderten
Bilanz-/TTM-Werte angehängt. Es gibt keine persistente Feature-Tabelle und keinen zusätzlichen
Source-Datenbestand. Das JSON enthält unter `performance_diagnostics` Ladezeiten, Zeilenzahlen,
ungefähren Speicherbedarf, Query-Anzahl sowie Cache Hits/Misses.

SPY wird nur verwendet, wenn adjustierte lokale Bars den gesamten tatsächlichen Zeitraum abdecken.
Der Benchmark ist ein kostenfreier Close-to-Close-Buy-and-Hold-Vergleich; fehlen Bars, bleibt er mit
einer konkreten Warnung unavailable. Der Backtest lädt SPY niemals aus dem Netzwerk nach.

### Bekannte Backtest-Grenzen

`assets.tradable` beschreibt das aktuelle, nicht das historische Alpaca-Universum. TraWalp besitzt
noch keine Point-in-Time-Mitgliedschaft inklusive Delistings; Backtests verwenden daher die aktuell
handelbaren, SEC-identifizierten Unternehmen und dürfen **nicht** als survivorship-bias-frei
bezeichnet werden. Ebenso fehlt ein verifiziertes Ticker-History-Modell, weshalb aktuelle
Identitätskonflikte auch historische Trades sperren. Vorhandene Facts und Bars bleiben erhalten,
werden aber nicht spekulativ zwischen Emittentenepochen migriert. Alpaca-Bars sind gemäß
`universe.market_data_adjustment: all` bereits adjustiert; die Engine passt Splits nicht ein zweites
Mal an. Historische Shares/Fundamentals können dennoch nicht für jede Corporate Action perfekt
vergleichbar sein.

Die beschleunigte Pipeline ändert die historische Universumsquelle nicht: sie bleibt die heutige
tradable-Mitgliedschaft und damit survivorship-biased. A/B/C teilen weiterhin denselben PIT-Screen.
Der gemeinsame Recovery-Gate gilt ausdrücklich auch für A und B; die Varianten sind deshalb ein
Scoring-/Threshold-Vergleich unter gemeinsamer technischer Entry-Bestätigung und keine reine
Faktor-Ablation. Diese tatsächliche Definition wird im Konfigurations-Snapshot jedes Reports
gespeichert.

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
- Milestone 4 (fertig): Point-in-Time-Backtester, Reports und Strategie-Vergleich.
- Research-Erweiterungen (fertig): Audits/Diagnostik, Presets, Daily-Backfill und native
  Multi-Timeframe-/Strategy-F-Pfade.
- Milestone 5: ausschließlich Alpaca Paper Trading, Risk Management und Daily Runner.
