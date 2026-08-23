# Trade- und Positionsdiagnostik

## Position und Execution Leg

Ein Entry erzeugt genau eine wirtschaftliche Position mit deterministischer `position_id`.
Jeder vollständige oder partielle Verkauf ist ein Execution Leg mit eigener `execution_leg_id`.
Die bestehende `trades`-Liste bleibt kompatibel und enthält weiterhin diese Legs; identische Daten
werden zusätzlich explizit als `execution_legs` exportiert. Die neue `positions`-Liste aggregiert
alle Legs eines Entries über tatsächliche Mengen, Kostenbasis, Commission und Slippage.

Die bisherigen Felder `number_of_trades`, `win_rate`, `loss_rate`, `average_win`, `average_loss`
und `profit_factor` in `metrics` behalten aus Kompatibilitätsgründen ihre bisherige Leg-Semantik.
Neue Reports stellen deshalb zwei eindeutig bezeichnete Bereiche bereit:

- `execution_metrics`: Anzahl und Trefferquote einzelner Exit-Fills.
- `position_metrics`: Anzahl und wirtschaftliches Gesamtergebnis ursprünglicher Entries.

Ein Teilverkauf und der spätere Restverkauf zählen somit als eine Position und zwei Legs.

## Positionsergebnis, MFE und Profit Capture

`position_return` ist der summierte Netto-P&L aller Legs dividiert durch die ursprüngliche
Entry-Kostenbasis einschließlich Entry-Commission. Er wird nicht als ungewichteter Mittelwert der
Leg-Returns berechnet.

MFE und MAE verwenden die bereits während der Simulation konservativ geführten High-/Low-Water-
Marks. Bei einem Stop innerhalb einer Daily-Bar wird kein unbekanntes späteres Tageshoch als vorher
erreichter Gewinn ausgegeben. Die bestehende Regel `pre_bar_stops_first; new trails apply next bar`
bleibt unverändert.

Bei MFE größer als 0,1 Prozent gilt:

```text
profit_capture_ratio = position_return / MFE
profit_giveback      = MFE - position_return
```

Die 0,1-Prozent-Toleranz verhindert Ratios aus bloßem Floating-Point- oder Mikro-Markt-Rauschen.
Bei kleinerer MFE ist Capture `null`. Ein negativer Return nach vorherigem Gewinn ergibt bewusst
eine negative Capture Ratio und einen entsprechend großen Giveback. Alle Werte schließen das
vorhandene Kostenmodell ein.

`profit_capture_by_exit_reason` aggregiert Positionen nach ihrem finalen Exit-Grund. Partial-Profit-
Legs bleiben in der Leg-Datei sichtbar, bestimmen aber nicht den finalen Position-Exit-Grund.

## Stop-Loss-Diagnostik

Final per `stop_loss` geschlossene Positionen erhalten genau eine Kategorie:

- `gap_through_stop`: ausführbares Open lag unter der gültigen Stopmarke.
- `never_profitable`: MFE war höchstens 0,1 Prozent.
- `profitable_then_stopped`: relevante positive MFE, aber negatives Positionsergebnis.
- `normal_stop`: übriger Stop-Fall.

`stop_loss_diagnostics` enthält je Kategorie Anzahl, durchschnittliche MFE/MAE, Haltedauer und
Position-Return. Damit bleiben Entry-Probleme und späterer Profit-Giveback getrennt messbar.
Die kompakte Kennzahl `never_profitable_stop_rate` beantwortet dagegen die wirtschaftliche Frage
direkt und zählt auch Gap-Stops mit MFE bis 0,1 Prozent als nie profitabel; die exklusive
Detailkategorie bleibt dabei weiterhin `gap_through_stop`.

## Post-Exit-Analyse

Nach abgeschlossener Simulation liest ein separater Diagnosepass die Bars nach dem finalen
Positionsexit. Bezugspunkt ist `exit_reference_price`, also die Marktmarke vor Sell-Slippage.
Für 1, 3, 5 und 10 nachfolgende Trading-Bars werden Close-Return, höchstes High (MFE) und tiefstes
Low (MAE) berechnet. Die Exit-Bar selbst gehört nicht zum Forward-Fenster.

Ein Horizont wird nur ausgegeben, wenn die vollständige Zahl nachfolgender Bars innerhalb des
Backtest-Endes vorhanden ist; andernfalls ist er `null`. Forward-Daten verändern weder Entry,
Exit, Sizing, Ranking, Equity Curve noch Strategy State. Sie sind reine Reportdaten.

`post_exit_by_reason` enthält Mittelwerte für alle Horizonte sowie für fünf Tage Median,
positive/negative Forward-Rate, Anteil mit mehr als drei Prozent weiterem Gewinn und MFE/MAE.
Separate `observations_1d` bis `observations_10d` machen bei Exits nahe dem Backtest-Ende den
jeweiligen Stichprobenumfang explizit.

Intraday-Positionen erhalten zusätzlich native, strikt kanonische 15-Minuten-Diagnosen für den
nächsten Bar, zwei Bars, vier Bars und den Rest der regulären Session. Fehlende Zeitstempel werden
nicht mit einem späteren Provider-Bar überbrückt; der Horizont bleibt dann mit Gap-Grund ungelöst.
Zwei Bezugspunkte werden getrennt gespeichert:

- `post_exit_*` misst die Bewegung relativ zum unveränderten Exit-Referenzpreis.
- `counterfactual_hold_*` misst dieselben späteren Close-/High-/Low-Bewegungen relativ zum
  ursprünglichen Entry-Referenzpreis.

Die Exit-Bar wird konservativ vollständig ausgeschlossen. Damit kann ein unbekanntes High/Low nach
einem Intrabar-Stop nicht in die Zukunftsdiagnose gelangen. Diese Werte werden erst nach Abschluss
des simulierten Positionspfads berechnet und verändern niemals Entry, Exit, Stops, Ranking, Sizing,
Portfoliozustand oder P&L. Sie sind keine hypothetischen Trades.

Die Exit-Grund-Aggregation enthält neben Return, MFE, MAE, Giveback und Haltedauer auch Stichproben,
Medianwerte sowie Recovery- und positive-MFE-Raten der nativen Counterfactual-Hold-Horizonte. Bei
archivierten F4-Swing-High-Exits bleiben zusätzlich Candidate High, dessen Abstand zum Entry,
Bestätigung, intended/actual Exit und Candidate-to-Exit-Giveback auditierbar; F4s Trading-Semantik
wird dadurch nicht verändert.

## Re-Entry- und Trigger-Diagnostik

Eine wieder eröffnete Position verweist auf den vorherigen vollständigen Positionsexit und speichert
Exit-Datum/-Grund, Return, MFE/MAE, Entry-Score und Kalenderabstand. Entry-Trigger werden aus dem
vorhandenen Recovery Gate aufgezeichnet:

- Preis über SMA20,
- RSI Recovery,
- Momentum5 positiv,
- Relative Volume über der bestehenden Schwelle.

`fresh_trigger_since_previous_exit` ist wahr, wenn eine dieser Point-in-Time-Bedingungen nach dem
vorherigen Exit beobachtet von `false` auf `true` wechselte. Ein unmittelbarer Re-Entry bei
durchgehend erfüllten Bedingungen ist falsch. Fehlende Beobachtungen werden nicht als erfundener
Triggerwechsel behandelt. Dies ist nur Diagnostik und führt keine neue Entry- oder Cooldown-Regel
ein.

## Score-Diagnostik

Der Entry-Score stammt vom abgeschlossenen Signal-Tag. Für offene Positionen wird danach höchstens
eine Beobachtung je tatsächlich vorhandenem Daily-Screen gespeichert. Der letzte vor dem Exit
verfügbare Score ist `exit_score`; Intraday-/Open-Exits erhalten niemals rückwirkend den späteren
Tages-Close-Score. Pro Position werden Minimum, Maximum, Ratios und Veränderung gespeichert.

`entry_score_diagnostics` vergleicht Entry-Komponenten für Gewinner, Verlierer und
`never_profitable` Stop-Loss-Positionen. Es werden keine Schwellen daraus automatisch verändert.

## Isolierte Fixed-Stop-Baselines

Zusätzlich zu bestehenden Presets stehen folgende kontrollierte Vergleiche zur Verfügung:

- `baseline-fixed-stop`: 3-Prozent-Stop, keine weitere dynamische Exit-Regel.
- `fixed-stop-max-hold`: Baseline plus zehn Tage Hard Max Hold.
- `fixed-stop-take-profit`: Baseline plus zwei Prozent Take Profit.
- `fixed-stop-atr-trailing`: Baseline plus bestehenden ATR Trail.
- `fixed-stop-partial-atr`: ATR-Baseline plus bestehenden Partial Profit.

Entry-Logik, Score-Schwellen, Universe, Risk per Trade, Position Sizing, Slippage und Commission
bleiben identisch. Dadurch misst der Vergleich den isolierten Effekt der jeweils ergänzten
Exit-Regel. Es findet keine Parameteroptimierung statt.

## Reportdateien

Ein Einzelbacktest erzeugt weiterhin JSON, Trade-CSV und Equity-CSV und zusätzlich:

```text
*_positions.csv
*_execution_legs.csv
*_post_exit_analysis.csv
```

Der JSON-Report enthält Positionen, Position-/Execution-Metriken, Profit Capture nach Exit-Grund,
Stop-Diagnostik, Post-Exit-Aggregate und Entry-Score-Gruppen. Der Compare-CSV enthält beide
Metrikebenen; die CLI zeigt eine kompakte Auswahl der wichtigsten Positionsdiagnosen.
