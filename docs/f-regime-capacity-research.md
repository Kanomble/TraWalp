# Strategy F: Point-in-Time-Regime-Capacity-Research

`research-f-regime-capacity` ist eine neue, strikt getrennte historische Research-Hypothese für
Strategy `F/configured`. Der Frozen Champion bleibt unverändert `F/configured` mit
`max_positions=1`; der Workflow wählt keinen Gewinner und nimmt keine Production-Promotion vor.

Die Familie enthält exakt vier Varianten:

- `F-regime-control-C1`: statische Capacity 1.
- `F-regime-control-C5`: statische Capacity 5.
- `F-regime-SPY-SMA200-C1-C5`: Capacity 5 nur bei `SPY close > SPY SMA200`, sonst 1.
- `F-regime-SPY-SMA200-MOM126-C1-C5`: Capacity 5 nur bei `SPY close > SPY SMA200`
  und `SPY momentum126 > 0`, sonst 1.

`momentum126` verwendet unverändert die etablierte Definition
`close.pct_change(periods=126, fill_method=None)`. Für einen Screen am abgeschlossenen Handelstag
`T` werden ausschließlich lokale SPY-Daily-Bars bis einschließlich `T` ausgewertet. Die daraus
entstehende Capacity gilt für die Orders am Open von `T+1`. Fehlender SPY-Bar oder unzureichender
SMA-/Momentum-Warmup wird explizit als `UNAVAILABLE` gemeldet und konservativ als Capacity 1
behandelt. Der Backtest lädt keine Daten aus dem Netzwerk.

Ein Capacity-Wechsel steuert nur neue Entries. Bei C5 → C1 bleiben alle bereits offenen Positionen
offen und werden weiter ausschließlich durch das unveränderte configured Daily Management verwaltet.
Solange die Zahl offener Positionen mindestens eins beträgt, entsteht kein neuer Entry. Bei C1 →
C5 werden wieder bis zu vier zusätzliche freie Slots durch das bestehende Ranking und die bestehende
Allocation belegt. Der Regime-Layer erzeugt niemals einen Exit.

In der Session-Diagnostik ist `session` der Signal-/Screen-Tag `T`.
`open_positions_at_signal_selection` und `available_slots` beschreiben die Allocation nach dem Close
von `T`; `execution_session`, `open_positions_before_entries`, `open_positions_after_entries` und
`entries_opened` dokumentieren die Ausführung am Open von `T+1`. Die letzte reine
Liquidations-Session hat `entry_selection_performed=false` und leere Entry-Zustandsfelder.

Die Regime-Returns werden aus den Close-to-Close-Session-Returns der Equity Curve gebildet und nach
dem am jeweiligen Session-Close berechneten Regime chronologisch zusammengesetzt. Es handelt sich
nicht um eine irreführende Zuordnung des gesamten Positions-PnL zum Exit-Tag.

Der Universe-Audit bleibt `CURRENT_UNIVERSE_ONLY` und `NOT_SURVIVORSHIP_CLEAN`. Die Ergebnisse sind
daher kein survivorship-bereinigter historischer PIT-Universumsnachweis.

## Manueller PowerShell-Aufruf

Der vollständige Lauf ist bewusst manuell zu starten:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli validate-f-regime-capacity `
  --start 2022-01-03 `
  --end 2026-08-12 `
  --output-stem f_regime_capacity_2022-01-03_2026-08-12_v1
```

Der Befehl erzeugt Summary/Metadaten, Metriken, Session-Regime-Diagnostik, Regime-Aggregate,
Monats-/Jahres-/Drittelstabilität, regimegruppierte Entry-Ranks, kanonische Full-Portfolio-Kosten-Reruns,
Post-hoc-Symbolkonzentration, Positionen und Execution Legs. Bestehende Artefakte werden nicht
überschrieben.
