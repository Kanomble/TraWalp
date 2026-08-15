# Dynamisches Position Management

## Architektur

Der bestehende Point-in-Time-Backtester bleibt die einzige Trading- und Execution-Architektur.
Der Screener bewertet den Markt einmal nach dem abgeschlossenen Tages-Close. Der
`PositionManager` bewertet ausschließlich bereits offene Positionen und liefert eine strukturierte
Entscheidung `hold`, `sell` oder `partial_sell` samt Exit-Grund. Entries werden weiterhin erst am
nächsten verfügbaren Open und in normaler Candidate-Rangfolge ausgeführt.

Positionszustand ist an den einzelnen Trade gebunden. Er umfasst Entry-Preis/-Datum/-Score,
Stückzahl und verbleibende Kostenbasis, High/Low-Water-Marks, aktive Trail-Marken, aktuellen ATR,
realisierten Teilgewinn und bereits ausgelöste Partial-Level. Ein früher verkaufter Ticker erhält
keinen Score-Bonus und es gibt kein Averaging Down.

## Konfiguration

Alle Regeln liegen im bestehenden `StrategyConfig` unter `position_management`:

```yaml
position_management:
  bar_timeframe: "1d"
  stop_loss: {enabled: true, percent: null}
  take_profit: {enabled: true, percent: null}
  trailing_stop:
    enabled: false
    activation_profit: 0.01
    trailing_distance: 0.006
  atr_trailing_stop:
    enabled: false
    atr_period: 14
    atr_multiplier: 1.0
    activation_profit: 0.0
  signal_decay:
    enabled: false
    minimum_score_ratio: 0.75
  partial_take_profit:
    enabled: false
    levels:
      - {profit: 0.015, sell_fraction: 0.5}
  max_hold:
    enabled: true
    days: null
    mode: "hard"
    review_minimum_score_ratio: 0.75
  portfolio_rotation:
    enabled: false
    minimum_score_improvement: 0.15
    minimum_holding_days: 1
  reentry: {enabled: true, cooldown_days: 0}
```

`stop_loss.percent: null`, `take_profit.percent: null` und `max_hold.days: null` sind
rückwärtskompatible Brücken: Sie verwenden den bisherigen ATR/Risk-Stop,
`backtest.profit_target_pct` beziehungsweise `backtest.max_holding_days`. Explizite Prozentwerte
ersetzen nur die jeweilige Exit-Marke, nicht das vorhandene Slippage-/Commission-Modell.

`max_hold.mode` hat folgende Bedeutung:

- `hard`: Verkauf am Close, sobald `holding_days >= days`.
- `review`: Verkauf an der Grenze nur, wenn der aktuelle/Entry-Score unter
  `review_minimum_score_ratio` liegt.
- `disabled`: keine haltedauerbedingte Schließung. `enabled: false` wirkt ebenfalls so.

TraWalp-Scores liegen einheitlich auf 0..100. Signal Decay verwendet deshalb
`current_score / entry_score`. Fehlende, nicht endliche, negative oder nullwertige Entry-Scores
sind nicht ratio-kompatibel und lösen keinen mathematisch künstlichen Exit aus.

Partial-Level werden aufsteigend ausgewertet. `sell_fraction` bezieht sich auf die beim Trigger
noch offene Stückzahl. Jedes Level kann pro Position nur einmal auslösen; Commission und Slippage
werden auf jeden Fill angewendet und die Entry-Kostenbasis anteilig aufgeteilt.

Portfolio Rotation interpretiert `minimum_score_improvement` relativ, also beispielsweise 0,15
als 15 Prozent Verbesserung gegenüber dem aktuellen Positionsscore. Die Position muss die
Mindesthaltedauer erfüllen, der Kandidat muss den normalen Entry-Filter bestehen und die relative
Verbesserung muss zusätzlich über den modellierten Round-Trip-Kosten liegen. Der Verkauf reserviert
den Kandidaten nicht: Am Close wird das normale Ranking neu erzeugt, dessen Gewinner erst am
nächsten Open gekauft werden kann.

## Exit-Priorität und Daily-OHLC-Regeln

Die Reihenfolge ist deterministisch:

1. der höchste bereits aktive Long-Stop (Fixed, prozentualer Trail oder ATR-Trail),
2. Profit-Orders aufsteigend nach ihrem Triggerpreis (Partial oder vollständiger Take Profit),
3. Signal Decay,
4. Portfolio Rotation,
5. Max Hold,
6. Liquidation am Backtest-Ende.

Innerhalb der Preisregeln entscheidet nicht die Konfigurationsreihenfolge, sondern das zuerst
gekreuzte Schutz- bzw. Profit-Level. Bei identischen Profit-Levels behält der vollständige Take
Profit Vorrang.

Open-Gaps werden zum Open ausgeführt; sonstige Preislevel zum bekannten Stop/Target. Danach greift
das vorhandene Sell-Slippage- und Commission-Modell. Weil Daily OHLC die Reihenfolge von High und
Low nicht verrät, werden in derselben Bar berührte vorab bekannte Stops vor Targets ausgeführt.
Ein in der aktuellen Bar neu beobachtetes Hoch darf einen Trail erst für die nächste Bar erhöhen.
Eine Aktivierung direkt am bekannten Open ist dagegen für den Rest der Bar zulässig. Stops bewegen
sich niemals nach unten.

ATR verwendet die bestehende Wilder-ATR-Implementierung. Am Entry stammt ATR aus dem bereits
abgeschlossenen Signal-Tag. Eine nach dem aktuellen Close berechnete ATR-/Trail-Marke gilt erst ab
der folgenden Bar. Datenbankabfragen sind mit `as_of` begrenzt; spätere Bars können keine frühere
Entscheidung ändern.

## Presets und CLI

Die Score-Varianten A/B/C bleiben unabhängig von folgenden Position-Management-Presets:

- `legacy`: alter ATR/Risk-Stop, 12-Prozent-Target aus Backtest-Config, Hard Max Hold.
- `dynamic-hold`: 3-Prozent-Stop und Signal Decay, kein Max Hold.
- `take-profit`: Dynamic Hold plus 2-Prozent-Target.
- `atr-trailing`: 3-Prozent-Stop, ATR-Trail und Signal Decay, kein Max Hold.
- `partial-profit`: ATR Trailing plus einmal 50 Prozent bei +1,5 Prozent.
- `intraday-dynamic`: Partial Profit mit echten Provider-Intraday-Bars, standardmäßig 15 Minuten.
- `configured`: verwendet ausschließlich die YAML-Werte.

```bash
python -m trading_system.cli backtest --start 2025-01-01 --end 2025-12-31 --strategy legacy
python -m trading_system.cli backtest --start 2025-01-01 --end 2025-12-31 --strategy dynamic-hold
python -m trading_system.cli backtest --start 2025-01-01 --end 2025-12-31 --strategy atr-trailing
python -m trading_system.cli compare-strategies --start 2025-01-01 --end 2025-12-31
python -m trading_system.cli compare-strategies --start 2025-01-01 --end 2025-12-31 `
  --include position-management
```

`compare-strategies` vergleicht standardmäßig sowohl A/B/C als auch alle Position-Management-
Presets auf denselben gecachten Point-in-Time-Screens. Mit `--include score-variants` oder
`--include position-management` lässt sich der Vergleich auf eine Familie begrenzen.
`backtest-compare` bleibt als kompatibler Alias für den reinen Position-Management-Vergleich
erhalten. Nicht ausführbare Strategien – etwa `intraday-dynamic` bei fehlender lokaler Historie – stehen
mit Begründung in `skipped_strategies`, statt den vollständigen Vergleich abzubrechen.

Reports enthalten zusätzlich Loss Rate, Median-Haltedauer,
Trades/Monat, Kosten, Slippage-Kosten, besten/schlechtesten Trade und Exit-Zählungen. Die Trade-CSV
enthält Entry-/Exit-Score, Gross/Net PnL, MFE/MAE, High/Low, Fees, Slippage und Partial-Level.
Ein realisierter Partial-Fill ist dabei ein eigener Trade-Leg in Win Rate, Trade-Anzahl und
Exit-Statistik; die anteilig aufgeteilte Kostenbasis verhindert Doppelzählungen von Entry-Kosten.
Die zusätzlich eingeführte wirtschaftliche Positionsebene und Diagnosefelder sind in
[`position-diagnostics.md`](position-diagnostics.md) beschrieben. Die bisherigen Kennzahlen bleiben
als Execution-Leg-Metriken kompatibel; neue `position_metrics` vermeiden die Partial-Exit-Verzerrung.

## Intraday-Grenze

Die zentrale Config validiert `5m`, `15m`, `1h` und `1d`, und Screening ist architektonisch von der
Positionsüberwachung getrennt. Die persistente `bars`-Tabelle trennt alle Timeframes im
Primärschlüssel. `intraday-dynamic` verwaltet Positionen mit echten provider-nativen Intraday-Bars.
Fehlende Position-Bars oder Warmup-Daten führen zu einer klaren Fehlermeldung bzw. einem begründet
übersprungenen Compare-Run. Es gibt keinen Daily-Fallback, kein Resampling und keine Mock-Bars.
Details stehen in [`intraday-market-data.md`](intraday-market-data.md).
