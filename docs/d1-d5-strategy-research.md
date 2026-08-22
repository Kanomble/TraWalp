# TraWalp D1–D5 Strategy Research

## Executive Summary

Diese Untersuchung ist ein lokaler, eingefrorener Strategy-Research-Backtest für den Zeitraum
2026-05-01 bis 2026-08-12. Sie ist keine Live-Trading-, Paper-Trading- oder
Strategie-Promotion-Empfehlung. Es wurden keine Scores, Schwellenwerte, Universe-Filter,
Portfolio-Limits oder Strategy-Parameter optimiert.

Die isolierte Profit-Lock-Variante D1 verbesserte gegenüber `C/configured` Return,
Drawdown, Profit Factor und Expectancy, reduzierte den durchschnittlichen gesamten Giveback
jedoch nicht. D2 erzeugte zwei Runner und einen fast identischen Gesamtertrag zu D1, bei
niedrigerem Profit Factor und höherem Giveback. D3 beseitigte die 29 finalen Entry-Bar-Exits
des bisherigen Intraday-Pfads vollständig, verschlechterte aber die wirtschaftlichen
Ergebnisse deutlich. D4 reduzierte Exposure, Turnover und Drawdown; seine normale Native-Data-
Auswertung war leicht negativ, während die Strict-Coverage-Sensitivity positiv war. D5/C war
in beiden Coverage-Modi negativ und stützt die kombinierte Hybrid-Hypothese in diesem Sample
nicht. Die B-Mirrors zeigen für D1 einen qualitativ positiven, für D5 aber einen gegensätzlichen
Selection-Effekt.

Wegen des kurzen Samples, nur sieben D1/D2/D5-Positionen, erheblicher Intraday-Coverage-Lücken
und der nicht gestützten Hybrid-Hypothese erfolgt keine automatische Auswahl oder Promotion.

## Environment / Frozen Dataset

- Branch: `main`
- Commit: `ec673ecce22dfc70785b85119445f1b636a75021`
- Config: `strategy.yaml`, unverändert
- Datenbank: `data/trading_system.sqlite3`
- Dateigröße vor dem Research-Lauf: 7,217,475,584 Bytes
- Research-Zeitraum: 2026-05-01 bis 2026-08-12
- Regression vor Implementierung: 256 Tests bestanden; Ruff und `git diff --check` sauber
- Regression nach Implementierung: 273 Tests bestanden; Ruff und `git diff --check` sauber
- Netzwerk/Sync: kein Sync, SEC Request, Alpaca History Request oder Intraday-Prefetch
- Business-Data-Mutation: keine; die Read-only-Counts vor und nach dem Task sind identisch

| Lokaler Datenbestand | Count | Verfügbarer Zeitraum |
|---|---:|---|
| Assets | 13,441 | – |
| Companies | 6,088 | – |
| Fundamental Facts | 16,391,706 | filed 2009-04-15 bis 2026-08-19 |
| Daily Bars | 3,578,927 | 2024-01-02 bis 2026-08-19 |
| 15m Bars | 148,691 | 2025-04-16 13:30 UTC bis 2026-08-12 19:45 UTC |
| 5m Bars | 4,775 | lokal vorhanden, nicht für D1–D5 verwendet |
| 1h Bars | 390 | lokal vorhanden, nicht für D1–D5 verwendet |
| Market Snapshots | 4,625 | – |
| Raw SEC Cache | 6,870 | nur bestehender lokaler Bestand |
| Sync State | 12,357 | nur bestehende Statusmetadaten |

Die bestehenden Dataset States melden Asset Universe, Historical Bars, Daily Backfill,
Intraday und Snapshots als erfolgreich. Der SEC-Fundamentals-State ist `partial`; seine
Metadaten nennen drei historische Identity Conflicts (`EQR`, `LIDR`, `PARA`). Diese Angaben
sind eingefrorene lokale Statusdaten und stammen nicht aus Requests dieses Tasks.

Die Research-Qualification fand 70 gemeinsam gecachte PIT-Screen-Sessions. Für Daily Data
standen 6,055 Symbole und 2,240,350 erwartete Symbol-Sessions 172,638 fehlenden oder
ungeklärten Sessions gegenüber. Für die 15 Intraday-Kandidaten waren von 1,080
Symbol-Sessions 762 `COMPLETE`, 102 `PARTIAL_SESSION`, 102
`UNKNOWN_MARKET_ACTIVITY` und 216 `MISSING_SESSION`; insgesamt fehlten 5,782 erwartete
Intraday-Bars. Diese Coverage ist ein zentraler Unsicherheitsfaktor.

## Implementation and Execution Correctness

Die neue Comparison-Familie ist ausschließlich über `--include research-d1-d5` verfügbar.
`--include all` behält seine bisherige Bedeutung. Die vier Controls und sieben Research-
Varianten laufen in deterministischer Reihenfolge; A bleibt ausschließlich Negative Control.

Die vorhandene Backtest Engine, PIT-Screens, `CachedScreenSource`, `PositionManager`, native
Intraday-Ausführung, Partial-Fill-, Kosten-, MFE/MAE- und XNYS-Session-Semantik werden
wiederverwendet. Neue Stop-Level aus einem abgeschlossenen Bar gelten erst im Folgebar. Der
Profit Lock ist raise-only und verwendet das beim Entry unveränderlich gespeicherte
wirtschaftliche R. Economic Break-even berücksichtigt Slippage und Commission. D3/D4 armen
den ATR-Trail erst nach mindestens einem vollständig abgeschlossenen nativen 15m-Bar. D4/D5
akzeptieren ausschließlich den kanonischen 09:30–09:45-Bar und führen ausschließlich am Open
des vorhandenen 09:45–10:00-Bars aus. D5 rankt bestätigte, ausführbare Kandidaten nur nach dem
ursprünglichen Daily Score.

Zusätzliche Tests decken Profit-Lock-State-Machine, Next-Bar-Aktivierung, Gap-Semantik,
Original-Quantity-Partial, Trail Guard, Confirmation-Bar-Präsenz, exakten Entry-Bar,
per-symbol Cooldown, D5-Multi-Candidate-Ranking, DST/Session-Grenzen, Opt-in-Presets,
Local-only-Ausführung, Report-No-Overwrite und unveränderte Business-Row-Counts ab. Bestehende
Assertions wurden nicht abgeschwächt.

## Baseline Controls

Alle Prozentwerte beziehen sich auf das eingefrorene Sample. Expectancy ist die bestehende
Backtest-Metrik pro Ausführungsleg.

| Strategy | Return | Max DD | Sharpe | Sortino | Position PF | Expectancy | Positions / Legs | Position Win Rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A/configured | -3.267% | -3.819% | -1.841 | -2.486 | 0.514 | -0.2970 | 11 / 11 | 27.3% |
| B/configured | +1.172% | -1.799% | 0.735 | 1.129 | 1.485 | +0.1465 | 8 / 8 | 50.0% |
| C/configured | +1.587% | -3.863% | 0.992 | 1.508 | 1.697 | +0.2267 | 7 / 7 | 57.1% |
| C/intraday-dynamic | +3.795% | -1.131% | 2.943 | 8.897 | 1.887 | +0.0643 | 48 / 59 | 35.4% |

Die Controls reproduzieren damit die vorhandenen Pfade ohne Änderung ihrer Presets. Das
Intraday-Control hatte 29 finale Entry-Bar-Exits, sämtlich Verlustpositionen; 19 Positionen
überlebten den ersten Bar, davon waren 89.5% Gewinner.

## D1

`D1/C-swing-profit-lock` behält C Selection, Daily Entry, configured ATR/Risk Stop, volles
+12%-Target und zehn Trading Days Hard Hold bei. Nach bestätigtem +1R gilt ab dem Folgebar
Economic Break-even, nach +2R mindestens Entry +1R.

- Return +2.532%, Max DD -2.968%, Sharpe 1.603, Sortino 2.567
- Position PF 2.258, Expectancy +0.3618, sieben Positionen, Win Rate 57.1%
- Durchschnittliches MFE +7.598%, MAE -3.832%, Giveback 4.574%
- Drei Positionen erreichten +1R, eine +2R; drei Break-even- und eine +1R-Aktivierung
- Keine Verlustposition nach einem erreichten +1R- oder +2R-Meilenstein
- Durchschnittlicher Giveback nach Lock-Aktivierung: 2.005%

Gegenüber `C/configured` steigen Return um 0.946 Prozentpunkte und PF von 1.697 auf 2.258;
Drawdown sinkt um 0.895 Prozentpunkte. Der gesamte durchschnittliche Giveback steigt jedoch
leicht von 4.467% auf 4.574%. Bei nur drei Lock-Aktivierungen ist die Giveback-Hypothese daher
nicht vollständig bestätigt.

## D2

`D2/C-swing-runner` ersetzt nur den vollen +12%-Exit durch 33% der ursprünglichen Quantity;
der Rest bleibt Runner unter Stop, Profit Lock und Hard Hold.

- Return +2.563%, Max DD -2.968%, Sharpe 1.654, Sortino 2.749
- Position PF 1.885, Expectancy +0.2848, sieben Positionen und neun Legs
- Zwei Partial Targets und zwei Runner; durchschnittlicher Runner Return +14.235%
- Durchschnittliches Runner MFE +18.386%, Runner Giveback 4.151%
- Durchschnittliches Positions-MFE +8.667%, Giveback 5.793%

Der Gesamtertrag liegt nur 0.031 Prozentpunkte über D1, während PF und Expectancy niedriger
und Giveback höher sind. Zwei Runner reichen nicht für eine belastbare Aussage zur
Trend-Capture-Überlegenheit.

## D3

`D3/C-intraday-trail-guard` verändert weder Selection noch Opening Entry, 3%-Catastrophe Stop,
50%@+1.5%-Partial oder ATR-Konfiguration. Nur das Trail-Arming wird um einen abgeschlossenen
nativen 15m-Bar verschoben.

- Return +0.108%, Max DD -4.054%, PF 1.012, Expectancy +0.0019
- 45 Positionen, 56 Legs, Position Win Rate 53.3%, Turnover 29.951
- Finale Entry-Bar-Exits: 0 gegenüber 29 im Intraday-Control
- Trail-Exits im Entry-Bar: 0; alle 45 Positionen überlebten den Entry-Bar
- Elf Partial Targets/Runner; durchschnittlicher Runner Return +1.945%

Der Guard isoliert und beseitigt die sofortigen Trail-Exits wie beabsichtigt. Er verbessert
aber weder Expectancy noch Return gegenüber `C/intraday-dynamic`; der Drawdown steigt.

## D4

`D4/C-intraday-confirmed-entry` nimmt ausschließlich den Top-C-Kandidaten, verlangt einen
grünen kanonischen Opening Bar, führt am 09:45-Bar-Open aus und nutzt den D3 Trail Guard.
Nach einem negativen finalen Position Return blockiert der per-symbol Cooldown genau die
folgende XNYS-Session.

- Return -0.169%, Max DD -1.437%, PF 0.932, Expectancy -0.0073
- 19 Positionen, 23 Legs, Position Win Rate 36.8%, Turnover 12.688
- 44 Confirmation Attempts, 20 Passes: Pass Rate 45.45%
- 23 Rejections, ein fehlender Opening Bar, ein fehlender Execution Bar
- Vier Cooldown-Blocks, vier Partial Targets/Runner, keine Entry-Bar-Exits

Gegenüber D3 fallen Exposure, Turnover und Kosten stark; die Native-Data-Performance wird aber
leicht schlechter. Die Strict-Coverage-Sensitivity dreht D4 auf +0.981% und PF 1.735. Dieser
Vorzeichenwechsel verhindert eine robuste Schlussfolgerung aus dem Native Run.

Die abgelehnten Confirmation-Events enthalten Symbol, Daily Rank/Score, erwartete und
tatsächliche Timestamps, Opening O/H/L/C/Volume/VWAP und Failure Reason. Es werden keine
hypothetischen Trades in Portfolio-State oder PnL erzeugt. Ein weitergehender hypothetischer
Return-Counterfactual wurde nicht als Trading-Resultat emittiert.

## D5

`D5/C-hybrid-confirmed-swing` prüft alle C-Kandidaten, wählt den höchstgerankten bestätigten
und ausführbaren Kandidaten und verwaltet ihn danach mit configured ATR/Risk Stop, D1 Lock,
D2 Partial Runner und zehn Trading Days Hard Hold.

- Return -1.159%, Max DD -2.386%, PF 0.684, Expectancy -0.1448
- Sieben Positionen, acht Legs, Position Win Rate 28.6%
- 25 Confirmation Attempts, neun Passes: Pass Rate 36.0%
- 16 Rejections und ein fehlender Execution Bar
- Zwei +1R-, eine +2R-Position; zwei Break-even- und eine +1R-Lock-Aktivierung
- Ein Partial Runner mit +14.052% finalem Return

D5 liegt unter `C/configured`, D2 und D4. Auch Strict Coverage bleibt mit -0.152% und PF 0.943
leicht negativ. Die Kombination bestätigt die Zielarchitektur in diesem Sample nicht.

## B Mirrors

| Strategy | Return | Max DD | PF | Expectancy | Positions / Legs | Lock / Confirmation Diagnostics |
|---|---:|---:|---:|---:|---:|---|
| D1/B-swing-profit-lock | +0.788% | -2.302% | 1.327 | +0.0985 | 8 / 8 | 3× +1R, 1× +2R, 3× BE, 1× +1R Lock |
| D5/B-hybrid-confirmed-swing | +0.908% | -1.980% | 1.320 | +0.1008 | 7 / 9 | Pass 38.71%, 2 Runner, 2× +1R/+2R |

D1 bleibt unter B qualitativ profitabel, ist aber schwächer als D1/C. D5/B ist positiv,
während D5/C negativ ist. Damit ist der Profit-Lock-Mechanismus qualitativ eher robust, die
Hybrid-Wirtschaftlichkeit aber sichtbar selection-abhängig.

## Cost Stress

Die Kostenfälle wurden auf derselben lokalen Datenbasis neu gerechnet. `Costs` sind die vom
Backtest ausgewiesenen gesamten Slippage- plus Commission-Kosten in Portfolio-Währung.

| Strategy | 5/0 bps Return / PF / Costs | 10/0 bps | 15/0 bps | 5/5 bps |
|---|---|---|---|---|
| D1/C | +2.532% / 2.258 / 0.115 | +2.451% / 2.194 / 0.229 | +2.370% / 2.133 / 0.344 | +2.418% / 2.172 / 0.229 |
| D2/C | +2.563% / 1.885 / 0.117 | +2.472% / 1.842 / 0.233 | +2.381% / 1.801 / 0.349 | +2.447% / 1.831 / 0.233 |
| D3/C | +0.108% / 1.012 / 1.512 | -2.190% / 0.783 / 2.981 | -3.601% / 0.664 / 4.439 | -1.383% / 0.853 / 3.001 |
| D4/C | -0.169% / 0.932 / 0.632 | +0.469% / 1.189 / 1.263 | +0.217% / 1.086 / 1.793 | -0.799% / 0.720 / 1.260 |
| D5/C | -1.159% / 0.684 / 0.113 | -1.240% / 0.668 / 0.226 | -1.321% / 0.651 / 0.339 | -1.271% / 0.662 / 0.226 |

D1/D2 bleiben in allen vordefinierten Fällen positiv. D3 ist wegen hohen Turnovers stark
kostenempfindlich. D4 ist nicht monoton: Kosten verändern den Netto-Return einzelner
Positionen und damit den kostenbasierten Cooldown-State und den nachfolgenden Portfoliopfad.
Die Cost Cases sind deshalb vollständige Pfad-Neuberechnungen, keine statischen Abzüge.

## Strict Coverage Sensitivity

Diese Runs sind Data-Quality-Sensitivitäten, keine tradable Strategies und werden nicht als
Winner gerankt. Nur Intraday-Symbol-Sessions mit Qualification `COMPLETE` sind zulässig.

| Strategy | Positions | Return | Max DD | PF | Expectancy | Coverage Exclusions |
|---|---:|---:|---:|---:|---:|---:|
| D3/C | 35 | -1.224% | -3.038% | 0.827 | -0.0285 | 10 |
| D4/C | 17 | +0.981% | -0.597% | 1.735 | +0.0467 | 18 |
| D5/C | 7 | -0.152% | -1.980% | 0.943 | -0.0190 | 15 |

Der starke Unterschied zu Native Data, besonders bei D4, zeigt, dass residuale
Partial-Session-Gaps die Interpretation materiell beeinflussen.

## Monthly Stability

Die Klammer enthält die Zahl der in diesem Monatsblock geschlossenen Positionen. Die
vollständige Datei enthält zusätzlich Win Rate, PF und Average Position Return je Strategy
und Monat.

| Strategy | May | June | July | August partial |
|---|---:|---:|---:|---:|
| A/configured | -0.615% (3) | +0.346% (2) | -0.416% (3) | -2.582% (3) |
| B/configured | -1.052% (2) | +1.313% (1) | +0.512% (3) | +0.398% (2) |
| C/configured | +1.232% (2) | +1.343% (1) | -1.724% (3) | +0.735% (1) |
| C/intraday-dynamic | +1.651% (16) | +1.646% (7) | -0.851% (19) | +1.348% (6) |
| D1/C | +1.232% (2) | +1.343% (1) | -0.785% (3) | +0.742% (1) |
| D2/C | +1.262% (1) | +1.344% (2) | -0.785% (3) | +0.742% (1) |
| D3/C | +0.891% (14) | +1.770% (7) | -3.036% (18) | +0.483% (6) |
| D4/C | -1.105% (7) | +0.489% (2) | +0.459% (7) | -0.012% (3) |
| D5/C | -1.114% (3) | +1.601% (1) | -1.379% (2) | -0.267% (1) |
| D1/B | -1.052% (2) | +1.313% (1) | +0.130% (3) | +0.397% (2) |
| D5/B | -1.008% (1) | +3.740% (2) | -1.410% (2) | -0.414% (2) |

Kein D-Resultat ist monatlich stabil positiv. D3s Gesamtwert verdeckt insbesondere den
starken Juli-Verlust; D5/B wird wesentlich von zwei Juni-Positionen getragen.

## Symbol Concentration

| Strategy | Best / Worst Contributor | Top-1 / Top-3 Anteil am Netto-PnL |
|---|---|---:|
| A/configured | EAT / EXE | -63.2% / -41.2% |
| B/configured | EAT / EXE | 112.0% / 246.5% |
| C/configured | FSLR / PTC | 91.1% / 222.1% |
| C/intraday-dynamic | EAT / EXE | 67.5% / 137.9% |
| D1/C | FSLR / EXE | 57.1% / 150.2% |
| D2/C | DXCM / PTC | 95.2% / 184.1% |
| D3/C | EAT / EXE | 2,366.4% / 5,055.4% |
| D4/C | EAT / PTC | -322.6% / -662.3% |
| D5/C | EAT / EQT | -138.2% / -185.4% |
| D1/B | EAT / EXE | 166.6% / 385.4% |
| D5/B | DXCM / EQT | 231.7% / 396.4% |

Anteile über 100% entstehen, wenn Gewinner durch andere Symbole teilweise kompensiert werden;
bei negativem oder nahezu null liegendem Netto-PnL sind Vorzeichen und sehr große Quotienten
mathematisch erwartbar und nicht als positive Diversifikation zu lesen. Die vollständige
Symbol-Datei enthält Positionszahl, Netto-PnL, Return-Summe, Wins/Losses, Same-Bar-Stopouts und
Partials. Es wird daraus keine Symbol-Blacklist abgeleitet.

## Post-Exit

Für jede D-Position wurden 1d/3d/5d/10d Return, MFE und MAE post hoc berechnet. Ausgewählte
durchschnittliche Returns:

| Strategy | 1d | 3d | 5d | 10d |
|---|---:|---:|---:|---:|
| D1/C | +0.134% | +1.575% | +3.372% | +7.819% |
| D2/C | -2.002% | -0.270% | +2.421% | +6.792% |
| D3/C | +0.033% | +0.993% | +2.199% | +2.273% |
| D4/C | +0.479% | +0.321% | +2.096% | +0.330% |
| D5/C | +0.167% | +3.259% | +4.488% | +6.839% |
| D1/B | +0.481% | +2.281% | +4.855% | +3.711% |
| D5/B | +0.332% | +2.653% | +4.926% | +5.742% |

Der einzelne D1/C-Profit-Lock-Exit hatte danach -1.053%/−5.762%/−5.800%/−8.312% über
1d/3d/5d/10d. Die vier D2-Max-Hold-Runner zeigten im Durchschnitt -0.624%, +3.167%, +9.965%
und +17.398%. Diese sehr kleinen Teilmengen sind diagnostisch, nicht inferenziell. Die
Positionsdatei enthält die vollständigen MFE/MAE-Werte je Horizont.

## Hypothesis Evaluation

### H1 — Profit Lock: PARTIALLY SUPPORTED

D1 verbessert Return, Drawdown, PF und Expectancy und verzeichnet keine Verluste nach +1R/+2R.
Der gesamte durchschnittliche Giveback sinkt jedoch nicht, und nur drei Locks wurden
aktiviert. Der Schutzmechanismus wirkt plausibel, die konkrete Extreme-Giveback-Frage ist im
Sample nicht vollständig belegt.

### H2 — Runner: INCONCLUSIVE

Zwei Runner erreichen hohe finale Returns und D2 liegt im Gesamtertrag knapp über D1. PF,
Expectancy und Giveback entwickeln sich aber ungünstiger; zwei Beobachtungen erlauben keine
belastbare Überlegenheitsaussage.

### H3 — Trail Guard: PARTIALLY SUPPORTED

Entry-Bar-Trail-Exits fallen exakt von 29 auf null. Die zweite Hälfte der Hypothese ist nicht
gestützt: Return und Expectancy verschlechtern sich deutlich, der Drawdown steigt.

### H4 — Confirmed Entry: INCONCLUSIVE

Confirmation reduziert Trades, Turnover, Exposure, Costs und Drawdown. Im normalen Native Run
werden Return und Expectancy gegenüber D3 nicht besser; im Strict-Coverage-Run ist D4 dagegen
positiv. Der Coverage-bedingte Vorzeichenwechsel verhindert eine robuste Bewertung.

### H5 — Hybrid: NOT SUPPORTED

D5/C liegt unter `C/configured`, D2 und D4 und bleibt auch bei Strict Coverage leicht negativ.
Die Kombination aus Opening Confirmation und Swing Management liefert im eingefrorenen Sample
keinen belegten wirtschaftlichen Vorteil.

### H6 — Selection Robustness: PARTIALLY SUPPORTED

D1 ist unter C und B qualitativ positiv und aktiviert dieselben Lock-Stufen. D5 wechselt jedoch
von negativ unter C zu positiv unter B. Damit ist D1 qualitativ robuster, die gesamte
Mechanismenfamilie aber nicht selection-neutral belegt.

## Remaining Risks

- Survivorship Bias in Universe und lokal verfügbarer Historie
- Kurzer Sample-Zeitraum von gut drei Monaten
- Begrenzte Zahl von D1/D2/D5-Positionen und Lock-/Runner-Ereignissen
- Daily-OHLC-Ambiguität trotz konservativer Next-Bar-Stop-Semantik
- Unvollständige fundamentale Metric Coverage und `partial` SEC Dataset State
- Residuale Intraday-Partial-Session- und Missing-Session-Gaps
- Selection-Instabilität nach einem späteren Data Refresh
- Multiple-Research-Variant Selection Bias trotz vorab deklarierter Parameter
- Unsicherheit des Transaction-Cost-Modells und kostenabhängige Cooldown-Pfade
- Konzentration auf wenige Symbolbeiträge und instabile Netto-PnL-Anteile

## Next Decision

**NOT READY FOR EXTENDED / OUT-OF-SAMPLE VALIDATION**

Die zentrale H5-Hybrid-Hypothese ist nicht gestützt; zusätzlich bleibt H4 wegen der materiellen
Abhängigkeit von der Intraday-Coverage-Sensitivity unentscheidbar. Es erfolgt keine Strategy-
Promotion und keine Parameteränderung.

## Artifacts

- `reports/d1_d5_research_2026-05-01_2026-08-12.csv`
- `reports/d1_d5_research_2026-05-01_2026-08-12.json`
- `reports/d1_d5_research_2026-05-01_2026-08-12_positions.csv`
- `reports/d1_d5_research_2026-05-01_2026-08-12_execution_legs.csv`
- `reports/d1_d5_research_2026-05-01_2026-08-12_post_exit_analysis.csv`
- `reports/d1_d5_research_2026-05-01_2026-08-12_diagnostics.json`
- `reports/d1_d5_research_2026-05-01_2026-08-12_monthly.csv`
- `reports/d1_d5_research_2026-05-01_2026-08-12_symbol_concentration.csv`
- `reports/d1_d5_research_2026-05-01_2026-08-12_cost_stress.csv`
- `reports/d1_d5_research_2026-05-01_2026-08-12_strict_coverage.csv`
- `reports/d1_d5_research_2026-05-01_2026-08-12_data_qualification.json`
- `reports/d1_d5_research_2026-05-01_2026-08-12_gap_manifest.csv`
