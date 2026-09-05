# Codex-Auftrag: Quality + Value + Opportunity + Momentum Trading System mit Alpaca

Baue ein vollständiges, modular aufgebautes Python-Projekt für ein algorithmisches Aktien-Screening- und Paper-Trading-System.

Das System soll US-Aktien anhand von Fundamentaldaten und Marktdaten analysieren und Unternehmen identifizieren, die:

1. fundamental qualitativ hochwertig sind,
2. relativ zu ihrer Branche attraktiv bewertet sind,
3. aktuell einen relevanten Kursrückgang hinter sich haben,
4. erste Anzeichen einer technischen Erholung zeigen.

Die Strategie lautet konzeptionell:

**High Quality + Attractive Valuation + Price Dislocation + Recovery Signal**

Das Projekt muss zunächst ausschließlich für:

* Screening,
* Backtesting,
* Dry Runs und
* Alpaca Paper Trading

ausgelegt sein.

**Kein echtes Live-Trading implementieren oder aktivieren.**

---

# 1. Technologie

Verwende:

* Python
* `alpaca-py`
* pandas
* numpy
* scipy, falls sinnvoll
* requests oder httpx
* pydantic für Datenmodelle/Config, falls sinnvoll
* pytest
* SQLite als lokale Persistenz für die erste Version
* SQLAlchemy optional
* matplotlib für Backtest-Reports, falls sinnvoll

Secrets dürfen niemals im Sourcecode stehen.

Verwende `../.env` bzw. Environment Variables:

```text
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
SEC_USER_AGENT=
TRADING_MODE=paper
ENABLE_ORDER_SUBMISSION=false
```

Erstelle eine `../.env.example`.

---

# 2. Projektarchitektur

Erstelle eine klare modulare Struktur, ungefähr:

```text
trading_system/
│
├── README.md
├── pyproject.toml
├── .env.example
├── config/
│   └── strategy.yaml
│
├── src/
│   └── trading_system/
│       │
│       ├── config.py
│       │
│       ├── cli.py
│       │
│       ├── models/
│       │
│       │   ├── fundamentals.py
│       │   ├── market_data.py
│       │   ├── scores.py
│       │   └── signals.py
│       │
│       ├── data/
│       │   ├── alpaca_client.py
│       │   ├── sec_client.py
│       │   ├── xbrl_parser.py
│       │   ├── universe.py
│       │   └── database.py
│       │
│       ├── fundamentals/
│       │   ├── metrics.py
│       │   ├── peers.py
│       │   └── quality.py
│       │
│       ├── technical/
│       │   ├── indicators.py
│       │   └── momentum.py
│       │
│       ├── strategy/
│       │   ├── scoring.py
│       │   ├── screener.py
│       │   ├── signals.py
│       │   ├── risk.py
│       │   └── portfolio.py
│       │
│       ├── backtest/
│       │   ├── engine.py
│       │   ├── metrics.py
│       │   └── report.py
│       │
│       └── trading/
│           ├── paper_broker.py
│           └── daily_runner.py
│
└── tests/
```

Wenn bereits ein Repository existiert, inspiziere zuerst dessen Struktur und integriere die Implementierung sinnvoll, anstatt unnötig alles neu aufzubauen.

---

# 3. Aktienuniversum

Verwende Alpaca, um das handelbare Aktienuniversum zu bestimmen.

Version 1 soll sich auf liquide US-Aktien konzentrieren.

Konfigurierbare Default-Filter:

```yaml
universe:
  min_price: 5
  min_market_cap: 1000000000
  min_avg_dollar_volume_20d: 10000000
  exclude_financials: true
  exclude_reits: true
```

Berechnung:

```text
Average Dollar Volume =
Average Daily Volume * Average Price
```

Wenn Market Cap nicht zuverlässig verfügbar ist:

```text
Market Cap ≈ Latest Price × Shares Outstanding
```

Shares Outstanding aus Fundamentaldaten beziehen.

Banken, Versicherungen und REITs in Version 1 nach Möglichkeit ausschließen, weil Kennzahlen wie Debt/EBITDA, EV/EBITDA und klassische FCF-Betrachtungen dort nicht gleich interpretiert werden können.

---

# 4. Fundamentaldaten über SEC EDGAR

Implementiere einen SEC-Datenclient.

Verwende öffentlich verfügbare SEC-XBRL-/Company-Facts-Daten.

Erforderliche Daten:

* Revenue
* Operating Income
* Net Income
* EPS
* Operating Cash Flow
* Capital Expenditures
* Cash
* Total Debt
* Current Assets
* Current Liabilities
* Total Assets
* Total Equity
* Shares Outstanding
* ggf. Interest Expense
* Tax Expense
* SIC/Industry Classification
* Filing Date
* Fiscal Period

Berücksichtige, dass XBRL-Tags zwischen Unternehmen variieren können.

Implementiere deshalb eine robuste Mapping-Schicht für alternative US-GAAP-Tags.

Beispiele:

```text
Revenue:
- Revenues
- SalesRevenueNet
- RevenueFromContractWithCustomerExcludingAssessedTax
```

Dasselbe Prinzip für die anderen Kennzahlen.

Die XBRL-Parsing-Logik soll separat getestet werden.

---

# 5. Point-in-Time-Daten zwingend beachten

Dies ist für Backtests kritisch.

Bei einem historischen Backtest dürfen ausschließlich Fundamentaldaten verwendet werden, die dem Markt zum jeweiligen Zeitpunkt tatsächlich bekannt waren.

Beispiel:

```text
Quartalsende: 31.03.
10-Q veröffentlicht: 05.05.

Backtest-Datum 20.04.:
→ Daten NICHT verwenden

Backtest-Datum 10.05.:
→ Daten dürfen verwendet werden
```

Verwende deshalb die SEC `filed` / Filing-Date-Information.

Niemals zukünftige Berichte rückwirkend verwenden.

Schreibe hierfür explizite Tests gegen Look-Ahead-Bias.

---

# 6. TTM-Kennzahlen

Berechne nach Möglichkeit Trailing-Twelve-Month-Werte aus den letzten vier veröffentlichten Quartalen.

Benötigt werden insbesondere:

```text
Revenue TTM
Operating Income TTM
Net Income TTM
Operating Cash Flow TTM
CapEx TTM
Free Cash Flow TTM
EPS TTM
EBITDA TTM
```

Berechnung:

```text
FCF = Operating Cash Flow - CapEx
```

EBITDA möglichst aus verfügbaren Fundamentaldaten ableiten.

Dokumentiere Annahmen.

---

# 7. Quality-Faktoren

Berechne folgende Kennzahlen:

## Revenue Growth

```text
Revenue Growth YoY =
Revenue TTM / Revenue TTM One Year Ago - 1
```

## EPS Growth

```text
EPS Growth YoY =
EPS TTM / EPS TTM One Year Ago - 1
```

Negative oder sehr kleine Vergleichswerte robust behandeln.

## Operating Margin

```text
Operating Margin =
Operating Income TTM / Revenue TTM
```

## Operating Cash Flow

OCF muss grundsätzlich positiv sein.

Zusätzlich OCF-Wachstum berechnen.

## ROIC

Implementiere eine vernünftige Näherung:

```text
NOPAT =
Operating Income × (1 - Effective Tax Rate)

Invested Capital =
Debt + Equity - Excess Cash

ROIC =
NOPAT / Average Invested Capital
```

Falls einzelne Daten fehlen, Kennzahl als unavailable markieren und nicht künstlich mit `0` ersetzen.

## Debt / EBITDA

```text
Debt to EBITDA =
Total Debt / EBITDA TTM
```

Negative oder null EBITDA entsprechend behandeln.

---

# 8. Bewertungskennzahlen

Berechne:

## P/E

```text
P/E =
Current Price / EPS TTM
```

Alternativ:

```text
Market Cap / Net Income TTM
```

Negative Earnings:

```text
P/E = unavailable
```

Nicht als extrem günstiges P/E interpretieren.

---

## Enterprise Value

```text
EV =
Market Cap
+ Total Debt
- Cash
```

## EV / EBITDA

```text
EV/EBITDA =
Enterprise Value / EBITDA TTM
```

Negative EBITDA als unavailable behandeln.

---

## Free Cash Flow Yield

```text
FCF Yield =
Free Cash Flow TTM / Market Cap
```

---

# 9. Bewertung relativ zur Branche

Das ist ein zentraler Bestandteil der Strategie.

Nutze in Version 1 SEC SIC Codes als Peer-/Industry-Gruppen.

Berechne für jede Peer-Gruppe:

```text
Median P/E
Median EV/EBITDA
Median Operating Margin
Median ROIC
Median Revenue Growth
```

Vermeide Peer-Gruppen mit zu wenigen Unternehmen.

Konfigurierbar:

```yaml
peers:
  min_peer_count: 8
```

Wenn eine vierstellige SIC-Gruppe zu klein ist, auf eine gröbere SIC-Klassifikation zurückfallen.

Berechne:

```text
Relative P/E =
Company P/E / Industry Median P/E
```

Beispiel:

```text
Company P/E = 18
Industry Median = 24

Relative P/E = 0.75
```

Kleiner als 1 bedeutet relativ günstiger.

Dasselbe für:

```text
Relative EV/EBITDA
```

---

# 10. Marktdaten von Alpaca

Verwende `StockHistoricalDataClient`.

Hole mindestens tägliche OHLCV-Daten:

```text
Open
High
Low
Close
Volume
```

für mindestens 300 Handelstage, damit 52-Wochen-Hoch und langfristige Indikatoren korrekt berechnet werden können.

Kursdaten für Splits sinnvoll adjustieren.

---

# 11. Technische Kennzahlen

Implementiere die Indikatoren selbst oder über eine klar dokumentierte kleine Dependency.

Alle Berechnungen müssen Unit Tests besitzen.

## SMA

```text
SMA20
SMA50
SMA200
```

## EMA

```text
EMA20
EMA50
```

## RSI

Implementiere Standard RSI(14).

RSI alleine darf kein Kaufgrund sein.

Von besonderem Interesse ist eine Erholung aus überverkauftem Bereich.

Beispiel:

```text
RSI war innerhalb der letzten 10 Tage < 30
UND
RSI aktuell > 35
```

→ Recovery Signal.

## Momentum

Berechne:

```text
Momentum 5d
Momentum 20d
Momentum 63d
Momentum 126d
```

Beispiel:

```text
Momentum20 =
Current Close / Close 20 Trading Days Ago - 1
```

## Volatilität

Berechne annualisierte historische Volatilität aus täglichen Returns.

## ATR

Berechne ATR(14) für Risk Management.

## Relative Volume

```text
Relative Volume =
Current Volume / Average Volume Previous 20 Days
```

---

# 12. 52-Wochen-Drawdown

Berechne:

```text
52W High =
Maximum Close der letzten 252 Handelstage
```

und:

```text
Drawdown =
Current Price / 52W High - 1
```

Beispiel:

```text
52W High = 200
Current Price = 150

Drawdown = -25%
```

---

# 13. Vier Haupt-Scores

Jede Aktie erhält:

```text
Quality Score      0–100
Valuation Score    0–100
Opportunity Score  0–100
Timing Score       0–100
```

Gesamtscore:

```text
Total Score =
0.40 × Quality
+ 0.30 × Valuation
+ 0.20 × Opportunity
+ 0.10 × Timing
```

Alle Gewichte müssen über `strategy.yaml` konfigurierbar sein.

---

# 14. Quality Score

Default-Gewichte:

```yaml
quality:
  revenue_growth: 0.20
  eps_growth: 0.20
  operating_cash_flow: 0.10
  operating_margin: 0.15
  roic: 0.25
  debt_to_ebitda: 0.10
```

Hohe Werte sind gut für:

```text
Revenue Growth
EPS Growth
Operating Cash Flow Growth
Operating Margin
ROIC
```

Niedrige Werte sind gut für:

```text
Debt/EBITDA
```

Operating Margin und ROIC sowohl absolut als auch relativ zur Peer-Gruppe berücksichtigen.

---

# 15. Valuation Score

Default:

```yaml
valuation:
  relative_pe: 0.35
  relative_ev_ebitda: 0.30
  fcf_yield: 0.35
```

Günstig:

```text
Relative P/E < 1
Relative EV/EBITDA < 1
High FCF Yield
```

Extremwerte winsorisieren oder clippen, damit einzelne fehlerhafte Werte den Score nicht dominieren.

---

# 16. Opportunity Score

Dieser Score soll messen, ob ein gutes Unternehmen gerade einen relevanten Kursrückgang erlebt.

Default:

```yaml
opportunity:
  drawdown_52w: 0.50
  medium_term_weakness: 0.30
  volatility: 0.20
```

Der ideale Bereich ist nicht automatisch der größte Drawdown.

Beispielsweise soll:

```text
-15 % bis -35 %
```

interessanter sein als:

```text
0 %
```

aber ein Drawdown von:

```text
-80 %
```

nicht automatisch 100 Punkte bekommen.

Implementiere eine nichtlineare Scoring-Funktion.

Beispiel:

```text
0 bis -10%      → niedriger Score
-10 bis -20%    → steigender Score
-20 bis -35%    → hoher Score
-35 bis -50%    → Score wieder reduzieren
< -50%          → starke Risiko-Penalty
```

Grenzen konfigurierbar machen.

---

# 17. Timing Score

Der Timing Score soll erkennen, ob der Kurs nach einem Rückgang beginnt, sich zu stabilisieren.

Default:

```yaml
timing:
  rsi_recovery: 0.30
  moving_average_recovery: 0.25
  momentum: 0.25
  relative_volume: 0.20
```

Positive Signale:

```text
RSI war kürzlich < 30 und steigt wieder
RSI aktuell etwa 35–60
Price > SMA20
SMA20 beginnt zu steigen
Momentum5 > 0
Momentum20 verbessert sich
Relative Volume > 1
```

Vermeide den Fehler:

```text
RSI < 30 => BUY
```

Ein fallender RSI soll nicht automatisch positiv bewertet werden.

Gesucht wird:

```text
Oversold → Stabilisierung → Recovery
```

---

# 18. Normalisierung

Implementiere Scoring-Funktionen sauber und nachvollziehbar.

Wo sinnvoll:

* Peer Percentiles
* robuste Z-Scores
* Winsorization
* piecewise linear scoring

verwenden.

Keine undurchsichtige Black-Box-ML-Lösung.

Jeder Score muss erklärbar sein.

Für jede Aktie soll später nachvollziehbar sein:

```text
Warum hat ORCL 82 Punkte?

Quality:
Revenue Growth       85
EPS Growth           90
ROIC                 82
Margin               75
Debt                 60

Valuation:
Relative PE          78
Relative EV/EBITDA   73
FCF Yield            35

...
```

---

# 19. Hard Filters

Vor dem Ranking gelten Mindestanforderungen.

Default:

```yaml
filters:
  min_quality_score: 65
  min_valuation_score: 55
  min_total_score: 70
  require_positive_ocf: true
```

Zusätzlich:

* ausreichende Liquidität
* gültige Fundamentaldaten
* keine offensichtlich fehlerhaften Preise
* keine Penny Stocks
* keine extrem kleinen Unternehmen

Filter konfigurierbar machen.

---

# 20. Daily Screener Output

Implementiere:

```bash
python -m trading_system.cli screen
```

Ausgabe:

```text
Rank  Symbol  Total  Quality  Value  Opportunity  Timing
1     ABC      84      88      82       81          71
2     XYZ      81      85      79       76          68
3     ORCL     79      86      72       80          61
```

Zusätzlich exportieren:

```text
reports/screen_YYYY-MM-DD.csv
reports/screen_YYYY-MM-DD.json
```

Für jede Aktie zusätzlich die wichtigsten Rohkennzahlen speichern.

---

# 21. Explain-Funktion

Implementiere:

```bash
python -m trading_system.cli explain ORCL
```

Sie soll detailliert zeigen:

```text
ORCL

QUALITY: 86/100
Revenue Growth: ...
EPS Growth: ...
Operating Margin: ...
ROIC: ...
Debt/EBITDA: ...

VALUATION: 72/100
P/E: ...
Industry Median P/E: ...
Relative P/E: ...
EV/EBITDA: ...
FCF Yield: ...

OPPORTUNITY: 80/100
52W Drawdown: ...
1M Return: ...
3M Return: ...
6M Return: ...

TIMING: 61/100
RSI14: ...
SMA20: ...
SMA50: ...
Momentum20: ...
Relative Volume: ...

TOTAL: 79/100
```

Die Explainability ist ein Kernfeature.

---

# 22. Entry-Regeln

Ein Trade darf nur entstehen, wenn mindestens:

```text
Total Score >= 75
Quality Score >= 70
Valuation Score >= 60
Opportunity Score >= 60
Timing Score >= 55
```

Default-Werte konfigurierbar.

Zusätzlich soll mindestens ein Recovery-Signal vorliegen.

Beispielsweise:

```text
Price > SMA20
```

und mindestens eines von:

```text
RSI Recovery
Momentum5 > 0
Relative Volume > 1.2
```

Keine Position alleine aufgrund günstiger Bewertung eröffnen.

---

# 23. Kein Look-Ahead beim Entry

Das Screening wird nach Börsenschluss für Tag T berechnet.

Ein Backtest darf frühestens zum nächsten verfügbaren Kurs handeln.

Beispiel:

```text
Signal anhand Close Montag
→ frühester Entry Dienstag
```

Nicht mit demselben Schlusskurs kaufen, auf dessen Basis das Signal entstanden ist.

---

# 24. Portfolio Management

Default:

```yaml
portfolio:
  max_positions: 5
  max_position_pct: 0.20
  max_sector_positions: 2
```

Unterstütze Fractional Position Sizing im internen Portfolio-Modell.

Wenn mehrere Kandidaten vorhanden sind:

```text
höchster Total Score zuerst
```

Alternativ später Score-gewichtete Allokation ermöglichen.

---

# 25. Risk Management

Risk Management nicht ausschließlich über fixe Prozentwerte lösen.

ATR verwenden.

Beispiel:

```text
Initial Stop =
Entry Price - 2 × ATR14
```

Zusätzlicher maximaler Stop:

```text
max_stop_loss_pct: 0.10
```

Positionsgröße anhand des Risikos:

```text
Risk Amount =
Portfolio Equity × Risk Per Trade

Position Size =
Risk Amount / (Entry Price - Stop Price)
```

Default:

```yaml
risk:
  risk_per_trade: 0.01
  atr_stop_multiple: 2.0
  max_stop_loss_pct: 0.10
```

Max-Position-Size trotzdem beachten.

---

# 26. Exit-Regeln

Die Strategie ist für ungefähr 1–2 Wochen Haltedauer gedacht.

Implementiere folgende Exit-Arten:

### Stop Loss

```text
Price <= Stop Price
```

### Profit Target

Default:

```text
+12 %
```

konfigurierbar.

### Time Exit

Default:

```text
10 Trading Days
```

konfigurierbar.

### Signal Exit

Optional:

```text
Price fällt wieder deutlich unter SMA20
```

oder

```text
Timing Score bricht stark ein
```

oder

```text
Total Score fällt unter Exit Threshold
```

Alle Exit-Gründe im Trade Log speichern.

---

# 27. Backtesting Engine

Implementiere einen echten Backtester.

CLI:

```bash
python -m trading_system.cli backtest \
    --start 2020-01-01 \
    --end 2025-12-31
```

Keine Zukunftsdaten verwenden.

Berücksichtigen:

* Point-in-Time Fundamentals
* Next-Day Execution
* Transaction Costs
* Slippage
* Positionsgrößen
* Stops
* maximale Positionen
* Cash
* Fractional Shares
* Time Exits

Default:

```yaml
backtest:
  slippage_bps: 5
  commission_bps: 0
```

---

# 28. Backtest-Metriken

Report mindestens:

```text
Total Return
Annualized Return / CAGR
Maximum Drawdown
Sharpe Ratio
Sortino Ratio
Win Rate
Average Win
Average Loss
Profit Factor
Expectancy per Trade
Number of Trades
Average Holding Period
Portfolio Turnover
Exposure
```

Zusätzlich Benchmark gegen SPY, soweit Daten verfügbar sind.

---

# 29. Trade Log

Jeder simulierte Trade muss nachvollziehbar gespeichert werden:

```text
symbol
signal_date
entry_date
entry_price
exit_date
exit_price
quantity
position_value
stop_price
quality_score
valuation_score
opportunity_score
timing_score
total_score
exit_reason
pnl
return_pct
```

---

# 30. Strategy Experiments

Baue die Architektur so, dass verschiedene Varianten vergleichbar sind.

Mindestens:

```text
A: Quality + Value

B: Quality + Value + Opportunity

C: Quality + Value + Opportunity + Timing
```

Wir wollen testen können, ob das technische Timing tatsächlich Mehrwert liefert.

CLI beispielsweise:

```bash
python -m trading_system.cli compare-strategies
```

Report mit:

```text
Return
Sharpe
Max Drawdown
Win Rate
Trades
Profit Factor
```

---

# 31. Paper Trading

Nach funktionierendem Backtest:

```bash
python -m trading_system.cli run-daily
```

Ablauf:

```text
1. Daten aktualisieren
2. Fundamentaldaten aktualisieren
3. Universe filtern
4. Kennzahlen berechnen
5. Peer Groups berechnen
6. Scores berechnen
7. bestehende Positionen prüfen
8. Exit-Signale berechnen
9. neue Kandidaten auswählen
10. Orders generieren
11. Risk Limits prüfen
12. Paper Orders senden
13. Ergebnis loggen
```

Verwende den Alpaca `TradingClient` ausschließlich im Paper-Modus.

---

# 32. Sicherheitsmechanismus für Orders

Standard:

```text
ENABLE_ORDER_SUBMISSION=false
```

Wenn `false`:

```text
keine Order senden
```

sondern nur:

```text
DRY RUN:
BUY ABC
Notional: $...
Reason: Total Score ...
```

Paper Orders dürfen nur gesendet werden, wenn explizit:

```text
TRADING_MODE=paper
ENABLE_ORDER_SUBMISSION=true
```

gesetzt wurde.

Live Trading soll in Version 1 überhaupt nicht implementiert werden.

---

# 33. Caching und API-Effizienz

Fundamentaldaten ändern sich nicht täglich.

Daher:

```text
SEC Daten lokal cachen
```

und nur aktualisieren, wenn ein neuer Filing-Datensatz vorhanden sein könnte.

Marktdaten inkrementell aktualisieren.

API Calls minimieren.

Retry-Logik mit Exponential Backoff implementieren.

SEC Fair-Access-Anforderungen respektieren und einen konfigurierbaren User-Agent verwenden.

---

# 34. Datenqualität

Das System darf fehlende Daten nicht stillschweigend als `0` interpretieren.

Unterscheide:

```text
0
```

von:

```text
missing / unavailable
```

Bei fehlenden Kennzahlen:

* Score-Gewichte ggf. unter den vorhandenen Faktoren neu normalisieren,
* Aktie markieren,
* bei zu vielen fehlenden Daten ausschließen.

Konfigurierbar:

```yaml
data_quality:
  min_available_quality_metrics: 4
  min_available_valuation_metrics: 2
```

---

# 35. Logging

Verwende strukturiertes Logging.

Beispielsweise:

```text
INFO SEC data updated: ORCL
INFO Alpaca bars updated: ORCL
INFO Candidate ORCL score=79.4
INFO DRY_RUN BUY ORCL notional=...
WARNING Missing EBITDA for XYZ
ERROR Failed SEC request CIK=...
```

Keine API Keys loggen.

---

# 36. Tests

Schreibe umfassende Tests.

Mindestens für:

```text
Revenue Growth
EPS Growth
FCF
ROIC
Debt/EBITDA
P/E
EV
EV/EBITDA
FCF Yield
Peer Median
Relative P/E
RSI
SMA
EMA
ATR
Momentum
Drawdown
Relative Volume
Score Normalization
Total Score
Position Sizing
Stop Loss
Time Exit
```

Besonders wichtige Tests:

```text
No look-ahead bias
Point-in-time SEC filings
Missing data handling
Negative EPS
Negative EBITDA
Stock splits
Outliers
Empty peer groups
```

Tests dürfen keine echten Orders senden.

---

# 37. Codequalität

Anforderungen:

* Type Hints
* docstrings bei wichtigen Funktionen
* kleine testbare Funktionen
* klare Trennung von Datenbeschaffung, Berechnung, Strategie und Execution
* keine God Classes
* keine versteckten globalen Zustände
* keine hartcodierten API Keys
* keine hartcodierten Strategy Thresholds
* Konfiguration zentral
* reproduzierbare Berechnungen

Verwende Ruff/Formatter und pytest.

---

# 38. README

Erstelle ein ausführliches README mit:

```text
1. Projektziel
2. Strategie
3. Architektur
4. Setup
5. Alpaca Paper API Keys
6. SEC User Agent
7. Daten synchronisieren
8. Screening durchführen
9. einzelne Aktie erklären
10. Backtest durchführen
11. Ergebnisse interpretieren
12. Paper Trading starten
13. Sicherheitsmechanismen
14. bekannte Einschränkungen
```

---

# 39. Erste Zielversion

Versuche nicht sofort jedes denkbare Feature zu implementieren.

Arbeite iterativ.

## Milestone 1

```text
Projektstruktur
Config
Alpaca Market Data
SEC Fundamentals
lokale Datenbank
```

## Milestone 2

```text
Fundamental Metrics
Technical Indicators
Peer Groups
Scoring
```

## Milestone 3

```text
Screener
CLI
Explainability
```

## Milestone 4

```text
Point-in-Time Backtester
Reports
Strategy Comparison
```

## Milestone 5

```text
Alpaca Paper Trading
Risk Management
Daily Runner
```

---

# 40. Wichtigste Strategiephilosophie

Der Bot soll NICHT versuchen, einfach Aktien zu kaufen, weil:

```text
P/E niedrig
```

oder:

```text
RSI < 30
```

ist.

Er soll Kombinationen suchen:

```text
starkes Unternehmen
+
solide Bilanz
+
gute Kapitalrendite
+
Wachstum
+
günstige relative Bewertung
+
relevanter Kursrückgang
+
erste technische Erholung
```

Zielbild:

```text
QUALITY
     ↓
Ist das Unternehmen grundsätzlich gut?

VALUATION
     ↓
Ist die Aktie relativ günstig?

OPPORTUNITY
     ↓
Ist der aktuelle Preis ungewöhnlich niedrig?

TIMING
     ↓
Beginnt sich der Kurs zu erholen?

     ↓
TRADE CANDIDATE
```

---

# 41. Keine Überoptimierung

Vermeide von Anfang an Parameter-Overfitting.

Alle wichtigen Grenzwerte zentral in `strategy.yaml` speichern.

Der Backtester muss Parameteränderungen einfach vergleichbar machen.

Keine Parameter nur deshalb optimieren, weil sie auf einem historischen Datensatz die höchste Rendite erzeugen.

Später sollen Walk-Forward-Tests möglich sein.

---

# 42. Umsetzung

Beginne jetzt mit der Implementierung.

Gehe folgendermaßen vor:

1. Repository analysieren.
2. Architektur und kurze Implementierungsplanung erstellen.
3. Abhängigkeiten festlegen.
4. Milestone 1 vollständig implementieren.
5. Tests ausführen.
6. Fehler beheben.
7. Danach Milestone 2 usw. durchführen.
8. Nach jedem Milestone Tests ausführen.
9. Keine ungetesteten Kernberechnungen zurücklassen.
10. README parallel aktuell halten.

Treffe bei kleineren Unklarheiten vernünftige technische Entscheidungen selbstständig.

Wenn eine Datenquelle eine Kennzahl nicht zuverlässig hergibt, nicht improvisieren oder falsche Werte erzeugen. Stattdessen:

* Kennzahl als unavailable markieren,
* sauberen Fallback implementieren,
* Einschränkung dokumentieren.

Am Ende soll das Repository einen reproduzierbaren Workflow ermöglichen:

```bash
# Daten
python -m trading_system.cli sync

# heutiger Screener
python -m trading_system.cli screen

# Aktie analysieren
python -m trading_system.cli explain ORCL

# Backtest
python -m trading_system.cli backtest \
  --start 2020-01-01 \
  --end 2025-12-31

# Strategien vergleichen
python -m trading_system.cli compare-strategies

# täglicher Dry Run
python -m trading_system.cli run-daily

# Alpaca Paper Trading erst nach expliziter Aktivierung
TRADING_MODE=paper \
ENABLE_ORDER_SUBMISSION=true \
python -m trading_system.cli run-daily
```

Prioritäten in dieser Reihenfolge:

```text
1. korrekte Daten
2. kein Look-Ahead-Bias
3. korrekte Berechnungen
4. Testbarkeit
5. Explainability
6. Risk Management
7. Performance
8. zusätzliche Features
```
