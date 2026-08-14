# Ziel

Erweitere TraWalp um eine vollständige, persistente und inkrementelle **Intraday-Market-Data-Pipeline** für:

```text
5m
15m
1h
```

Die bestehende Daily-Bar-Pipeline muss vollständig erhalten bleiben.

Die neuen Intraday-Bars sollen für:

* Backtests
* Position Management
* Strategy F / `intraday-dynamic`
* spätere Live-/Paper-Trading-Überwachung

verwendbar sein.

Aktuell kann das Position Management zwar Timeframes wie:

```text
5m
15m
1h
1d
```

konfigurieren, der Data Layer speichert aber bisher nur echte Daily-Bars. Deshalb werden Intraday-Strategien korrekt abgewiesen.

Diese Task soll diese Lücke vollständig schließen.

---

# 1. Bestehende Datenarchitektur zuerst analysieren

Vor Änderungen:

* bestehende Alpaca-Integration analysieren,
* aktuellen `sync`-Workflow untersuchen,
* Bar-Datenmodell prüfen,
* SQLite-Schema prüfen,
* bestehende Daily-Bar-Persistenz verstehen,
* bestehende Query-/Repository-Funktionen prüfen,
* Backtest-Bar-Loader prüfen,
* Timeframe-Konfiguration des Position Managers prüfen,
* vorhandene Market-Data-Feed- und Adjustment-Konfiguration berücksichtigen.

Keine zweite parallele Market-Data-Architektur bauen.

Bestehende Komponenten wiederverwenden und erweitern.

---

# 2. Unterstützte Timeframes zentral definieren

Mindestens unterstützen:

```text
1d
1h
15m
5m
```

Wenn bereits eine Timeframe-Abstraktion existiert, diese erweitern.

Keine verstreuten String-Vergleiche wie:

```python
if timeframe == "15m":
```

über viele Dateien verteilen.

Eine zentrale Normalisierung verwenden.

Beispielsweise:

```text
5m   → Alpaca 5Min
15m  → Alpaca 15Min
1h   → Alpaca 1Hour
1d   → Alpaca 1Day
```

Die konkrete Alpaca-SDK-Darstellung an die bereits verwendete Library anpassen.

---

# 3. Bar-Schema timeframe-fähig machen

Daily- und Intraday-Bars dürfen sich nicht überschreiben.

Falls das aktuelle Bar-Modell beispielsweise nur aus:

```text
symbol
timestamp
open
high
low
close
volume
```

besteht, muss `timeframe` Bestandteil der Identität werden.

Ziel beispielsweise:

```text
symbol
timeframe
timestamp
open
high
low
close
volume
trade_count
vwap
```

soweit die bestehenden Alpaca-Daten diese Felder bereits unterstützen.

Der eindeutige Key soll sinngemäß sein:

```text
(symbol, timeframe, timestamp)
```

Nicht:

```text
(symbol, timestamp)
```

---

# 4. Datenbankmigration sauber implementieren

Falls das SQLite-Schema geändert werden muss:

* bestehende Daily-Daten erhalten,
* keine Datenbank manuell löschen müssen,
* Migration deterministisch durchführen,
* bestehende `1d`-Bars korrekt auf `timeframe = 1d` migrieren,
* Indizes für effiziente Intraday-Abfragen anlegen.

Sinnvolle Indizes beispielsweise auf:

```text
symbol
timeframe
timestamp
```

und Kombinationen davon.

Vorhandenes Migrationssystem verwenden, falls vorhanden.

Keine ad-hoc-SQL-Migration bei jedem Start, wenn das Projekt bereits eine sauberere Lösung hat.

---

# 5. Intraday-Download über bestehenden Alpaca-Provider

Den bestehenden Alpaca-Market-Data-Code erweitern.

Unterstützen:

```text
5m
15m
1h
```

Keine neue HTTP-Implementierung bauen, wenn bereits Alpaca SDK/API-Abstraktionen vorhanden sind.

Bestehende:

```text
feed
adjustment
retry
rate limiting
logging
```

Mechanismen wiederverwenden.

---

# 6. Inkrementellen Intraday-Sync implementieren

Sehr wichtig:

Der Sync darf nicht bei jedem Lauf die komplette Intraday-Historie erneut laden.

Für:

```text
symbol + timeframe
```

den letzten lokal gespeicherten Timestamp bestimmen.

Dann nur fehlende Daten abrufen.

Beispiel:

```text
AAPL 15m

latest local:
2026-08-12 20:00 UTC

sync today:
→ nur Bars danach laden
```

Eine kleine Overlap-Periode ist erlaubt, um Provider-Korrekturen oder Grenzfälle sauber abzudecken.

Beispielsweise:

```text
letzte 1–2 Bars erneut laden
→ UPSERT
```

statt Duplikate zu erzeugen.

---

# 7. Idempotenz sicherstellen

Mehrfaches Ausführen desselben Syncs muss zu demselben Datenbestand führen.

Beispiel:

```bash
sync intraday
sync intraday
sync intraday
```

darf nicht:

* Duplikate erzeugen,
* Volumen mehrfach summieren,
* Bar-Zahlen künstlich erhöhen.

UPSERT bzw. bestehende Persistenzlogik verwenden.

---

# 8. Historischen Intraday-Sync unterstützen

Zusätzlich zum inkrementellen laufenden Sync einen expliziten historischen Backfill ermöglichen.

Beispiel:

```bash
python -m trading_system.cli sync-intraday \
  --start 2026-07-01 \
  --end 2026-08-12 \
  --timeframe 15m
```

Analog:

```bash
--timeframe 5m
--timeframe 1h
```

Wenn bestehende CLI-Struktur anders aufgebaut ist, diese respektieren.

Keine parallele, inkonsistente CLI bauen.

---

# 9. Mehrere Timeframes gleichzeitig synchronisieren

Unterstütze nach Möglichkeit:

```bash
python -m trading_system.cli sync-intraday \
  --start 2026-07-01 \
  --end 2026-08-12 \
  --timeframe 5m \
  --timeframe 15m \
  --timeframe 1h
```

oder eine entsprechende bestehende CLI-Syntax.

Alternativ:

```text
--timeframes 5m,15m,1h
```

Nur eine saubere Variante implementieren.

---

# 10. Bestehenden `sync` sinnvoll integrieren

Prüfen, ob der bestehende:

```bash
python -m trading_system.cli sync
```

optional auch Intraday aktualisieren soll.

Beispielsweise über Config:

```yaml
market_data_sync:
  daily:
    enabled: true

  intraday:
    enabled: false
    timeframes:
      - 15m
```

Wichtig:

Nicht standardmäßig riesige Intraday-Historien des gesamten Universums herunterladen, wenn der bestehende `sync` bisher leichtgewichtig bleiben soll.

Eine explizite Konfiguration bevorzugen.

---

# 11. Symbolumfang steuerbar machen

Intraday-Daten für tausende Aktien können sehr groß werden.

Deshalb mindestens drei sinnvolle Modi unterstützen, soweit mit bestehender Architektur vereinbar.

## Explicit Symbols

```bash
--symbols AAPL,MSFT,NVDA
```

## Candidate/Backtest Symbols

Symbole aus einer vorberechneten historischen Screening-/Backtest-Auswahl.

## Full Universe

Explizit möglich, aber nicht unabsichtlich Default.

Beispiel:

```bash
--universe all
```

Nur implementieren, wenn dies sinnvoll zur bestehenden CLI passt.

---

# 12. Für Backtests benötigte Symbole gezielt vorladen

Für Strategy F soll verhindert werden, dass zwingend Intraday-Historie für sämtliche 6.000+ Unternehmen erforderlich ist.

Architektonisch bevorzugen:

```text
Daily Screening
↓
historische Kandidaten / gehaltene Symbole bestimmen
↓
Intraday-Daten nur für diese Symbole benötigen
↓
Strategy-F Position Management
```

Falls der aktuelle Backtest die Kandidaten bereits deterministisch erzeugen kann, eine Möglichkeit schaffen, die dafür notwendigen Symbole zu exportieren oder direkt an einen Intraday-Backfill zu übergeben.

Noch keine komplexe automatische Zwei-Pass-Pipeline erzwingen, falls dies große Änderungen verlangen würde.

Aber die Datenarchitektur so gestalten, dass dies möglich ist.

---

# 13. Optionaler Backtest-Prerequisite-Check

Für einen Intraday-Backtest:

```text
strategy = intraday-dynamic
timeframe = 15m
```

vor dem Start prüfen:

```text
Sind ausreichend 15m-Bars für die benötigten Symbole vorhanden?
```

Falls nein:

Nicht auf Daily-Daten zurückfallen.

Stattdessen klare Fehlermeldung:

```text
Missing historical 15m bars for:
AAPL
MSFT
...
Run:
python -m trading_system.cli sync-intraday ...
```

---

# 14. Keine Fake-Intraday-Daten

Unter keinen Umständen:

```text
Daily OHLC → künstlich auf 15m verteilen
```

oder:

```text
1h → künstlich auf 15m interpolieren
```

oder:

```text
15m → künstlich auf 5m interpolieren
```

Strategy F darf ausschließlich echte Provider-Intraday-Daten verwenden.

Die bereits existierende Guard-Logik erhalten.

---

# 15. Market Sessions korrekt behandeln

US-Aktien-Intraday-Bars müssen korrekt mit Handelszeiten umgehen.

Bestehendes Exchange-/Calendar-System verwenden, falls vorhanden.

Mindestens unterscheiden:

```text
Regular Market Hours
Extended Hours
```

Config hinzufügen bzw. bestehende nutzen:

```yaml
intraday:
  extended_hours: false
```

Default bevorzugt:

```text
regular session only
```

wenn dies zum bisherigen Backtest passt.

Keine Pre-/After-Market-Bars unbemerkt mit Regular-Hours-Strategien vermischen.

---

# 16. Zeitzonen sauber behandeln

Alle gespeicherten Timestamps eindeutig behandeln.

Bevorzugt intern:

```text
UTC
```

oder bereits bestehende Projektkonvention.

Nicht naive Datetimes und timezone-aware Datetimes vermischen.

Für US-Börsensession entsprechend:

```text
America/New_York
```

nur dort konvertieren, wo Sessionlogik es erfordert.

DST berücksichtigen.

---

# 17. Bar-Grenzen korrekt behandeln

Für:

```text
5m
15m
1h
```

sicherstellen, dass Bars exakt den Provider-Bar-Grenzen entsprechen.

Keine selbst erzeugten Resampling-Grenzen verwenden, wenn echte Timeframe-Bars direkt von Alpaca geliefert werden.

Beispiel 15m:

```text
09:30
09:45
10:00
...
```

soweit der Provider entsprechend timestamped.

---

# 18. Paging / Request Limits berücksichtigen

Historische Intraday-Daten können sehr große Result Sets erzeugen.

Bestehendes Alpaca Paging korrekt verwenden.

Keine Annahme:

```text
ein Request = vollständiger Zeitraum
```

machen.

Tests bzw. abstrahierte Provider-Mocks für mehrere Pages ergänzen.

---

# 19. Rate Limits und Retry

Bestehende Retry-/Throttle-Mechanismen verwenden.

Bei:

```text
429
5xx
Timeout
Connection error
```

sauber reagieren.

Keine aggressive Request-Schleife bauen.

Fortschritt soll nach bereits erfolgreich gespeicherten Batches fortsetzbar sein.

---

# 20. Chunking für große Backfills

Historische Intraday-Downloads sinnvoll in Batches aufteilen.

Beispielsweise:

```text
nach Symbolgruppen
und/oder
nach Zeitfenstern
```

Die genaue Strategie an Alpaca-Limits und bestehende Provider-Architektur anpassen.

Ziel:

* begrenzter RAM-Verbrauch,
* robuste Wiederaufnahme,
* keine Millionen Bars gleichzeitig im Speicher.

---

# 21. Streaming in Persistenz bevorzugen

Wenn große Backfills geladen werden:

Nicht:

```text
alle Bars aller Symbole
→ Python-Liste mit Millionen Objekten
→ dann speichern
```

wenn dies vermieden werden kann.

Bevorzugt:

```text
fetch page/batch
→ normalize
→ bulk upsert
→ memory freigeben
```

Performance beachten.

---

# 22. Pydantic-/Object-Overhead vermeiden

Aus dem bisherigen Performance-Refactor ist bekannt, dass massive Objektkonstruktion teuer sein kann.

Die Intraday-Pipeline soll deshalb für Bulk-Daten keine unnötige Millionenfach-Pydantic-Konstruktion einführen.

Bestehende schnelle Bulk-Pfade verwenden.

Falls nötig:

```text
tuple / dataframe / lightweight records
```

intern verwenden, solange dies zum Projektstil passt.

---

# 23. Bulk-Upsert implementieren

Intraday-Bars effizient in SQLite schreiben.

Nicht:

```text
eine INSERT-Abfrage pro Bar
```

wenn vermeidbar.

Batch-/executemany-/bestehende Bulk-Funktion verwenden.

Transaktionen sinnvoll gruppieren.

---

# 24. Query API timeframe-fähig machen

Bestehende Funktionen wie sinngemäß:

```python
get_bars(symbol, start, end)
```

auf:

```python
get_bars(
    symbol,
    start,
    end,
    timeframe="1d"
)
```

oder passende bestehende Architektur erweitern.

Daily bleibt Default, wenn dies für Rückwärtskompatibilität nötig ist.

---

# 25. Batch Query für mehrere Symbole

Da Strategy F mehrere offene Positionen überwachen kann, möglichst effiziente Abfragen unterstützen:

```text
symbols = [AAPL, MSFT, NVDA]
timeframe = 15m
start
end
```

Nicht pro Symbol dutzende SQLite Queries erzeugen, wenn eine Batch-Abfrage möglich ist.

---

# 26. Timeframe muss in Cache Keys enthalten sein

Alle relevanten Bar-/Feature-Caches prüfen.

Ein Cache Key darf nicht nur sein:

```text
symbol + date
```

wenn dadurch:

```text
AAPL 1d
AAPL 15m
```

kollidieren könnten.

Timeframe einbeziehen.

---

# 27. Backtest Data Loader integrieren

Strategy F soll danach echte Intraday-Bars laden können.

Beispiel:

```yaml
position_management:
  bar_timeframe: "15m"
```

muss dazu führen:

```text
Entry / Daily Screening:
Daily data

Position Management:
15m data
```

Nicht den vollständigen Screener alle 15 Minuten neu berechnen.

---

# 28. Multi-Timeframe-Architektur

Die Backtest-Architektur muss sauber unterscheiden:

```text
screening timeframe
position-management timeframe
```

Beispielsweise:

```text
Screening:
1d

Position Management:
15m
```

Später auch:

```text
5m
1h
```

möglich.

Keine Annahme:

```text
ein Strategy Timeframe für alles
```

erzwingen, wenn dies Strategy F behindert.

---

# 29. Entry-Zeitpunkt korrekt auf Intraday-Timeline abbilden

Die bestehende Entry-Regel lautet sinngemäß:

```text
next available portfolio session open
```

Bei Intraday Position Management muss der Entry korrekt auf die entsprechende erste verfügbare Intraday-Bar abgebildet werden.

Beispiel:

```text
Daily Signal:
2026-08-10 close

Entry:
2026-08-11 market open

15m Position Monitoring:
ab erster zulässiger 15m-Bar nach Entry
```

Kein Monitoring mit Bars, die zeitlich vor dem Entry liegen.

---

# 30. Intraday Exit Order korrekt behandeln

Innerhalb einer 5m-/15m-/1h-Bar besteht weiterhin OHLC-Ambiguität.

Auch hier nicht annehmen, ob:

```text
High vor Low
```

oder:

```text
Low vor High
```

kam.

Bestehende konservative Intrabar-Prioritätsregeln weiterverwenden.

Die Unsicherheit wird kleiner als bei Daily, verschwindet aber nicht vollständig.

---

# 31. ATR auf Intraday-Timeframe korrekt berechnen

Wenn Strategy F nutzt:

```text
atr_period = 14
```

muss ATR auf dem ausgewählten Position-Timeframe berechnet werden.

Also beispielsweise:

```text
14 × 15m bars
```

und nicht versehentlich Daily ATR.

Falls für Stop-Loss bewusst Daily ATR gewünscht ist, dies explizit unterscheiden.

Keine implizite Mischung.

---

# 32. Warmup-Historie berücksichtigen

Intraday-Indikatoren benötigen ausreichend Bars vor dem eigentlichen Backteststart.

Wenn Backtest beginnt:

```text
2026-07-01
```

und ATR 14 benötigt wird, muss vor dem Start genügend Intraday-Historie geladen werden.

Der Sync-/Backtest-Prerequisite-Checker muss Warmup berücksichtigen.

Nicht erst ab exakt `--start` laden und dann die ersten Indikatoren mit unvollständiger Historie berechnen.

---

# 33. Intraday-Historienumfang konfigurieren

Config beispielsweise:

```yaml
intraday:
  enabled: true

  timeframes:
    - 5m
    - 15m
    - 1h

  extended_hours: false

  sync:
    incremental: true
    overlap_bars: 2

  warmup_bars: 50
```

Tatsächliche Struktur an bestehende Config anpassen.

Keine zweite Config-Welt bauen.

---

# 34. Datenqualitätsdiagnostik

Nach Sync pro Timeframe mindestens erfassen:

```text
symbols_requested
symbols_with_data
symbols_without_data

bars_downloaded
bars_inserted
bars_updated

first_timestamp
last_timestamp

duplicate_bars
invalid_bars
```

Optional:

```text
missing_expected_bars
```

wenn zuverlässig berechenbar.

---

# 35. Invalid-Bar-Validation

Offensichtlich ungültige Bars nicht stillschweigend speichern.

Beispielsweise prüfen:

```text
high >= low
high >= open
high >= close
low <= open
low <= close
volume >= 0
```

Umgang mit Provider-Sonderfällen entsprechend bestehenden Regeln.

Keine überaggressive Datenbereinigung.

---

# 36. CLI Status / Inspect erweitern

Falls es bereits Diagnosekommandos gibt, Intraday-Bestand sichtbar machen.

Beispiel:

```bash
python -m trading_system.cli data-status
```

Ausgabe:

```text
Timeframe   Symbols   Bars        First                 Last
----------------------------------------------------------------
1d          6054      1,804,405   2025-04-21            2026-08-12
1h           125        ...
15m          125        ...
5m           125        ...
```

Keine Werte hardcoden.

---

# 37. Optional eigener Intraday-Status

Falls bestehende CLI dafür ungeeignet:

```bash
python -m trading_system.cli intraday-status
```

Sinnvoll, aber nur implementieren, wenn kein vorhandener generischer Datenstatus existiert.

---

# 38. Speicherbedarf sichtbar machen

Da 5m-Bars sehr groß werden können, nach Backfill optional ausgeben:

```text
rows
database size
approx bytes per timeframe
```

Nur Diagnose.

Keine künstliche Datenkompression einführen, sofern nicht bereits vorhanden.

---

# 39. Symbolbasierter gezielter Backfill

Unterstütze:

```bash
python -m trading_system.cli sync-intraday \
  --symbols NVDA,EXE,CF \
  --start 2026-07-01 \
  --end 2026-08-12 \
  --timeframes 5m,15m,1h
```

oder äquivalente Syntax.

Dies ist besonders wichtig für Entwicklung und Tests.

---

# 40. Resumable Backfill

Ein abgebrochener Backfill muss fortgesetzt werden können.

Beispiel:

```text
500 Symbole geplant
nach 230 Symbolen Netzwerkfehler
```

Nächster Lauf soll nicht zwangsläufig die ersten 230 vollständig neu laden.

Durch inkrementelle lokale Timestamps soll der Sync automatisch fortsetzbar sein.

---

# 41. Keine automatische Löschung

Sync darf vorhandene Intraday-Daten nicht pauschal löschen.

Falls später ein `--refresh` benötigt wird, nur explizit.

Diese Task benötigt kein aggressives Rebuild-Verhalten.

---

# 42. Daily Sync darf nicht regressieren

Nach Implementierung muss:

```bash
python -m trading_system.cli sync
```

für den bisherigen Daily-/SEC-Workflow weiterhin funktionieren.

Insbesondere:

* SEC Facts
* Assets
* Daily Bars
* Identity Mapping

dürfen nicht beschädigt werden.

---

# 43. Unit Tests – Timeframe Persistence

Mindestens testen:

```text
AAPL
timestamp X
1d
15m
5m
1h
```

dürfen parallel gespeichert werden.

Keine Kollisionen.

---

# 44. Unit Tests – Idempotenz

Gleiche Bars zweimal speichern.

Erwartung:

```text
row count unchanged
values correctly upserted
```

---

# 45. Unit Tests – Incremental Sync

Lokale Bars bis Zeitpunkt T.

Provider liefert:

```text
T-1
T
T+1
T+2
```

Erwartung:

* Overlap darf aktualisiert werden,
* nur neue Bars hinzugefügt,
* keine Duplikate.

---

# 46. Unit Tests – Pagination

Mock Provider liefert mehrere Pages.

Alle Bars müssen gespeichert werden.

Keine Page verlieren.

---

# 47. Unit Tests – Multiple Timeframes

Sync:

```text
5m
15m
1h
```

und danach Query je Timeframe.

Jede Query muss nur die richtigen Bars zurückgeben.

---

# 48. Unit Tests – Warmup

ATR-Backtest ab Datum T.

Data Loader muss genügend Bars vor T bereitstellen.

Keine Future-Bars verwenden.

---

# 49. Unit Tests – Timezones

Tests über DST-nahe Zeitpunkte, soweit sinnvoll.

Sicherstellen:

```text
UTC ↔ America/New_York
```

wird korrekt verarbeitet.

Keine naive Timestamp-Verschiebung.

---

# 50. Unit Tests – Regular Session Filter

Wenn:

```text
extended_hours = false
```

dürfen Pre-/After-Market-Bars nicht in Position Management gelangen.

Wenn:

```text
extended_hours = true
```

entsprechendes Verhalten testen.

---

# 51. Integration Test – Small Intraday Dataset

Mit Provider-Mock:

```text
2 Symbole
5 Handelstage
5m / 15m / 1h
```

vollständigen:

```text
fetch
persist
query
backtest loader
```

Flow testen.

---

# 52. Integration Test – Strategy F

Kleinen deterministischen Strategy-F-Backtest mit echten Test-Intraday-Bars ausführen.

Nachweisen:

```text
Daily screening
+
15m position management
```

funktioniert.

Ein Exit muss tatsächlich auf einer 15m-Bar ausgelöst werden können.

---

# 53. Strategy F mit 5m und 1h ebenfalls ermöglichen

`intraday-dynamic` darf technisch nicht nur auf 15m hardcoded sein.

Folgende Konfigurationen müssen funktionieren, wenn Daten vorhanden sind:

```yaml
bar_timeframe: "5m"
```

```yaml
bar_timeframe: "15m"
```

```yaml
bar_timeframe: "1h"
```

Die Strategie kann weiterhin standardmäßig 15m verwenden.

---

# 54. Keine automatische Parameteranpassung je Timeframe

Nicht automatisch sagen:

```text
5m → ATR 28
15m → ATR 14
1h → ATR 7
```

oder Ähnliches.

Timeframe und Strategieparameter bleiben getrennt konfigurierbar.

Diese Task implementiert Datenfähigkeit, keine Strategieoptimierung.

---

# 55. Performance messen

Für einen repräsentativen Intraday-Backfill diagnostizieren:

```text
download_seconds
database_write_seconds
bars_received
bars_per_second
SQLite query count
database size delta
```

Keine übermäßige Profiling-Infrastruktur bauen.

Leichte Counter reichen.

---

# 56. Logging

INFO-Level:

```text
INTRADAY SYNC timeframe=15m symbols=25 start=... end=...
INTRADAY SYNC COMPLETE timeframe=15m bars=123456 inserted=... updated=...
```

Per-Symbol-Details bevorzugt DEBUG.

Keine Millionen Bar-Logs.

---

# 57. Fortschrittsanzeige

Bei größeren Backfills sinnvolle kompakte Fortschrittsmeldungen:

```text
[125/500] symbols
15m bars stored: ...
```

Kein Log-Spam je Bar.

---

# 58. Dokumentation

Technische Dokumentation ergänzen:

```text
docs/intraday-market-data.md
```

oder bestehende geeignete Stelle.

Dokumentieren:

* unterstützte Timeframes
* Storage-Schema
* Timestamp-Konvention
* Regular vs Extended Hours
* inkrementeller Sync
* historischer Backfill
* CLI-Beispiele
* Strategy-F-Anforderungen
* Warmup
* bekannte Alpaca-/Provider-Limits
* Datenvolumen-Hinweis
* keine Fake-Interpolation

---

# 59. Beispiel-Workflow dokumentieren

Beispiel:

```text
1. Daily Universe synchronisieren

python -m trading_system.cli sync

2. Intraday-Daten für Backtest laden

python -m trading_system.cli sync-intraday \
  --start 2026-07-01 \
  --end 2026-08-12 \
  --timeframes 5m,15m,1h

3. Datenbestand prüfen

python -m trading_system.cli data-status

4. Strategy F ausführen

python -m trading_system.cli backtest \
  --start 2026-07-01 \
  --end 2026-08-12 \
  --strategy intraday-dynamic
```

Syntax an tatsächlich implementierte CLI anpassen.

---

# 60. Optionaler gezielter Strategy-F-Workflow

Wenn einfach integrierbar:

```bash
python -m trading_system.cli prepare-intraday-backtest \
  --start 2026-07-01 \
  --end 2026-08-12 \
  --timeframe 15m
```

Dieser könnte:

```text
historische Daily Candidates bestimmen
→ benötigte Symbole identifizieren
→ fehlende Intraday-Daten laden
```

Nur implementieren, wenn dies sauber auf bestehender Architektur aufsetzt.

Ansonsten als Follow-up dokumentieren.

Nicht den Scope unnötig aufblasen.

---

# 61. Datenmenge bewusst behandeln

Vor Full-Universe-Downloads abschätzen bzw. warnen.

Insbesondere:

```text
5m × tausende Symbole × >1 Jahr
```

kann eine sehr große SQLite-Datenmenge ergeben.

Ein Full-Universe-Backfill darf möglich sein, soll aber nicht versehentlich durch einen normalen `sync` ausgelöst werden.

---

# 62. Keine unnötige Speicherung redundanter Timeframes durch Resampling

Wenn echte 5m-, 15m- und 1h-Bars von Alpaca angefordert werden, diese separat speichern.

Nicht in dieser Task:

```text
5m herunterladen
→ daraus 15m und 1h erzeugen
```

es sei denn, die bestehende Architektur besitzt bereits einen validierten Resampling-Mechanismus und dies ist eindeutig vorteilhafter.

Standardmäßig echte Provider-Timeframes verwenden.

---

# 63. Datenintegrität prüfen

Nach einem Sync optional prüfen:

```text
COUNT
MIN(timestamp)
MAX(timestamp)
```

je Timeframe.

Bei abnormalen Lücken Warnung ausgeben, sofern sinnvoll bestimmbar.

---

# 64. Backtest-Reproduzierbarkeit

Ein Backtest darf keine neuen Provider-Daten automatisch im Hintergrund laden, wenn dies das Ergebnis unbemerkt verändern könnte.

Bevorzugt:

```text
sync
↓
persistierte Daten
↓
backtest
```

Strategy F soll aus lokal gespeicherten Daten reproduzierbar laufen.

Falls ein Auto-Fetch-Modus implementiert wird, muss er explizit sein.

---

# 65. Point-in-Time-Trennung erhalten

Intraday-Daten dürfen die bestehende Point-in-Time-Logik der Fundamentaldaten nicht verändern.

Screening bleibt:

```text
Point-in-Time Daily/Fundamental Data
```

Position Management bekommt:

```text
Intraday Market Data
```

Keine zukünftigen Intraday-Bars in frühere Entscheidungen einfließen lassen.

---

# 66. End-of-Day / Session Boundaries

Positionen können über Nacht gehalten werden.

Intraday-Loader und Backtester müssen deshalb klar unterscheiden:

```text
last bar current session
next session first bar
```

Trailing State etc. darf über Nacht nicht unbeabsichtigt zurückgesetzt werden.

---

# 67. Stop-State über mehrere Sessions erhalten

Für Strategy F müssen beispielsweise:

```text
highest_price_since_entry
highest_price_since_activation
trailing_stop
partial_take_profit state
```

über mehrere Intraday-Sessions hinweg erhalten bleiben.

Nicht täglich neu initialisieren.

Falls das bereits im Position Manager korrekt funktioniert, durch Integration Test bestätigen.

---

# 68. Backtest-Ende

Wenn der Backtest auf einem Datum endet:

```text
2026-08-12
```

müssen Intraday-Bars nur bis zum konfigurierten Ende der zulässigen Session dieses Tages verwendet werden.

Keine Bar des Folgetags.

---

# 69. Bestehende Warnings erweitern

Bei Intraday-Backtests Report ergänzen:

```text
position management timeframe: 15m
extended hours: false
intrabar ambiguity remains within 15m bars
```

Keine Daily-OHLC-Warnung ausgeben, wenn tatsächlich 15m benutzt wird; stattdessen passende Timeframe-Warnung.

---

# 70. Vollständige Testsuite

Nach Implementierung:

```bash
pytest
```

und:

```bash
ruff check .
```

bzw. bestehende Projektbefehle ausführen.

Keine bestehenden Tests entfernen oder abschwächen.

---

# 71. Realer kleiner Intraday-Sync-Test

Wenn Alpaca-Zugang im Entwicklungsumfeld verfügbar ist, zusätzlich einen kleinen echten Sync durchführen.

Beispielsweise:

```text
Symbols:
AAPL
MSFT

Zeitraum:
5 Handelstage

Timeframes:
5m
15m
1h
```

Danach validieren:

```text
Bars > 0
keine Duplikate
korrekte Zeiträume
korrekte Timeframe-Trennung
```

Keine riesige Full-Universe-Abfrage nur zur Validierung durchführen.

---

# 72. Danach Strategy F real testen

Falls genügend echte 15m-Daten für den bestehenden kurzen Vergleich verfügbar sind:

```bash
python -m trading_system.cli backtest \
  --start 2026-07-01 \
  --end 2026-08-12 \
  --strategy intraday-dynamic
```

ausführen.

Falls Strategy F standardmäßig 15m nutzt, diesen Default beibehalten.

Zusätzlich Smoke Tests mit:

```text
5m
1h
```

durchführen.

---

# 73. Keine Strategieoptimierung

In dieser Task ausdrücklich NICHT:

```text
ATR multiplier optimieren
Take Profit optimieren
Stop Loss optimieren
besten Timeframe auswählen
5m vs 15m vs 1h nach Return optimieren
```

Nur Datenpipeline und korrekte technische Integration implementieren.

---

# 74. Abschlussbericht

Nach Umsetzung berichten:

```text
Changed files
Database migration
Supported timeframes
New CLI commands/options
Incremental sync behavior
Historical backfill behavior
Storage/query changes
Backtest integration
Strategy-F integration
Tests added
Test results
Real sync smoke-test result
Strategy-F smoke-test result
Known limitations
```

Zusätzlich reale Datenbestände angeben, falls Testsync durchgeführt wurde:

```text
5m bars:
15m bars:
1h bars:

symbols:
first timestamp:
last timestamp:
```

---

# Zentrale Architekturregel

Nach Umsetzung soll TraWalp klar zwischen folgenden Datenebenen unterscheiden:

```text
FULL UNIVERSE
│
├── Daily Bars
├── Fundamentals
└── Screening / Ranking
        │
        ▼
   Entry Candidate
        │
        ▼
 OPEN POSITION
        │
        ├── 5m Bars
        ├── 15m Bars
        └── 1h Bars
              │
              ▼
      Position Manager
              │
       HOLD / PARTIAL / SELL
```

Dabei gilt:

```text
Daily Data
→ Auswahl der Aktien

Intraday Data
→ Management bereits ausgewählter Positionen
```

Der normale Screening-Prozess soll nicht unnötig für jede 5m-/15m-Bar über das gesamte Universum wiederholt werden.

---

# Definition of Done

Die Task ist abgeschlossen, wenn:

1. `1d`, `1h`, `15m` und `5m` parallel persistent gespeichert werden können.
2. Daily-Daten unverändert funktionieren.
3. Historische Intraday-Daten explizit synchronisiert werden können.
4. Wiederholte Syncs idempotent und inkrementell sind.
5. Intraday-Sync nicht standardmäßig unkontrolliert das gesamte Universum lädt.
6. Queries korrekt nach Timeframe filtern.
7. Strategy F echte 15m-Bars verwenden kann.
8. Strategy F technisch auch mit 5m und 1h betrieben werden kann.
9. Fehlende Intraday-Daten weiterhin klar erkannt werden.
10. Kein Daily-Fallback oder künstliches Resampling echte Intraday-Daten vortäuscht.
11. Warmup, Sessions und Timezones korrekt behandelt werden.
12. Unit- und Integrationstests bestehen.
13. Ein kleiner realer Alpaca-Intraday-Sync erfolgreich validiert wurde, sofern Credentials verfügbar sind.
14. Ein echter Strategy-F-Smoke-Test mit lokal gespeicherten Intraday-Daten erfolgreich läuft.
