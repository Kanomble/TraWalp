# Intraday-Market-Data

TraWalp speichert echte Provider-Bars für `1d`, `1h`, `15m` und `5m`. Daily Bars bleiben die
Grundlage für Point-in-Time-Screening und Ranking. Intraday Bars werden ausschließlich zum Management
bereits ausgewählter Positionen verwendet; der Backtest startet keine Provider-Abfragen und führt den
Gesamtmarkt-Screener nicht pro Intraday-Bar erneut aus.

## Speicherung und Migration

Alle Bars liegen in der SQLite-Tabelle `bars`. Ihre Identität ist
`(symbol, timeframe, timestamp)`. Der bisherige Daily-Key wird beim ersten `Database.initialize()`
transaktional nach `timeframe='1d'` migriert, gezählt und erst nach erfolgreicher Validierung ersetzt.
Eine read-only View `daily_bars` hält bestehende SQL-Auswertungen kompatibel. Relevante Indizes decken
`(timeframe, timestamp)` und `(symbol, timeframe, timestamp)` ab.

Timestamps werden timezone-aware angenommen und als UTC gespeichert. OHLC-Relationen und nicht
negatives Volumen werden vor dem Bulk-Upsert validiert. Daily- und Intraday-Bars desselben Symbols und
Zeitpunkts kollidieren nicht. Wiederholte Downloads aktualisieren per UPSERT und erzeugen weder
Duplikate noch aufsummiertes Volumen.

## Historischer und inkrementeller Sync

Ein gezielter Backfill benötigt immer eine explizite Symbolquelle:

```powershell
python -m trading_system.cli sync-intraday `
  --symbols NVDA,EXE,CF `
  --start 2026-07-01 `
  --end 2026-08-12 `
  --timeframes 5m,15m,1h
```

Alternativ akzeptiert `--candidates-report <report.json>` Symbole aus einem bestehenden Screen- oder
Backtest-Report. Das gesamte aktuelle Universum wird nur mit dem bewussten
`--universe all` geladen. Ein normaler `sync` lädt standardmäßig keine Intraday-Historie. Dies ändert
sich nur mit `intraday.enabled: true` in der zentralen Strategie-Konfiguration.

Der CLI-Backfill erweitert `--start` automatisch um `intraday.warmup_bars` auf XNYS-Sessions. Für
jede Kombination aus Symbol und Timeframe wird die lokale Abdeckung geprüft. Ein inkrementeller Lauf
lädt einen konfigurierbaren Overlap erneut und schreibt nur neue oder korrigierte Werte. Ein früher
abgebrochener Lauf setzt dadurch am letzten dauerhaft gespeicherten Fenster fort. `--full-window`
fordert den vollständigen Zeitraum erneut an, löscht aber ebenfalls keine Daten.

Große Zeiträume werden nach Zeitfenstern und Symbolgruppen begrenzt. Jede Provider-Antwort wird direkt
validiert und per Bulk-Transaktion persistiert. Alpaca-py übernimmt seine Page-Token- und Retry-Logik;
TraWalp begrenzt zusätzlich die Request-Fenster, damit ein Backfill nicht sämtliche Bars im RAM hält.
Nach einem fehlerhaften Fenster beendet TraWalp die weiteren Fenster derselben Gruppe für diesen
Lauf. Dadurch rückt der lokale High-Water-Mark nicht über den Fehler hinweg; der nächste Lauf setzt
vor dem letzten dauerhaft gespeicherten Fenster fort.

Die wichtigsten Konfigurationswerte sind:

```yaml
intraday:
  enabled: false
  timeframes: [15m]
  extended_hours: false
  warmup_bars: 50
  sync:
    incremental: true
    overlap_bars: 2
    symbol_batch_size: 25
    request_window_days: 7
```

`extended_hours: false` speichert und lädt nur die reguläre XNYS-Session. Bei `true` verwendet TraWalp
04:00 bis 20:00 `America/New_York`; die Umrechnung nach UTC ist DST-sicher. Es werden native
Provider-Bargrenzen verwendet. TraWalp resampelt, interpoliert oder verteilt keine Daily-/1h-/15m-
Daten künstlich auf kleinere Timeframes.

## Bestand und Qualitätsdiagnostik

```powershell
python -m trading_system.cli data-status
python -m trading_system.cli storage-report
```

`data-status` zeigt je Timeframe Symbolzahl, Barzahl sowie ersten und letzten Timestamp.
`sync-intraday` meldet unter anderem empfangene, eingefügte, geänderte, unveränderte, doppelte und
ungültige Bars, Symbole ohne Daten, Download-/Schreibzeit, Durchsatz und die SQLite-Größenänderung.

Ein Full-Universe-Backfill mit 5-Minuten-Bars kann sehr groß werden. Für Entwicklung und Strategy-F-
Backtests sind explizite Kandidaten- oder Positionssymbole der bevorzugte Weg. SQLite speichert die
drei Provider-Timeframes separat; diese Task führt bewusst keine zusätzliche Kompression oder
Resampling-Schicht ein.

## Strategy F und Reproduzierbarkeit

`intraday-dynamic` verwendet standardmäßig `15m`. Ist in `position_management.bar_timeframe` bereits
`5m`, `15m` oder `1h` konfiguriert, respektiert das Preset diesen Wert. Die Verarbeitung lautet:

1. Daily Signal nach abgeschlossenem Session-Close.
2. Entry an der ersten verfügbaren Regular-Session-Bar des Folgetags.
3. Stop-/Target-/Partial-/Trailing-Auswertung auf chronologisch gespeicherten Intraday-Bars.
4. Daily Score Decay, Rotation und Max Hold erst nach dem abgeschlossenen Daily Screen; ein daraus
   entstehender Exit verwendet Preis und Timestamp des letzten zulässigen nativen Intraday-Bars.

ATR 14 bedeutet dabei 14 Bars des Position-Timeframes. Vor dem Entry werden ausschließlich frühere
Bars bis zur konfigurierten Warmup-Grenze verwendet. Trail-Marken aus einer abgeschlossenen Bar gelten
erst in der nächsten Bar. Position State und High-Water-Marks bleiben über Sessiongrenzen erhalten.

Fehlen für eine benötigte Position echte Bars oder Warmup-Daten, bricht der Lauf mit Symbolen,
Timeframe und einem passenden `sync-intraday`-Befehl ab. Es gibt keinen Daily-Fallback und kein
Auto-Fetch im Backtest. Innerhalb einer 5m-/15m-/1h-OHLC-Bar bleibt die Reihenfolge von High und Low
unbekannt; deshalb gilt weiterhin die konservative Stop-first-Regel.

Beispielworkflow:

```powershell
python -m trading_system.cli sync

python -m trading_system.cli sync-intraday `
  --symbols AAPL,MSFT,NVDA `
  --start 2026-07-01 `
  --end 2026-08-12 `
  --timeframes 5m,15m,1h

python -m trading_system.cli data-status

python -m trading_system.cli backtest `
  --start 2026-07-01 `
  --end 2026-08-12 `
  --strategy intraday-dynamic
```

Die Fundamental- und Universe-Selektion bleibt unverändert point-in-time. Forward-Daten werden nicht
geladen oder in Strategieentscheidungen eingespeist. Ergebnisse hängen damit ausschließlich vom vor
dem Lauf persistierten Datenbestand ab.
