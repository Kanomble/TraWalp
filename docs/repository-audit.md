# TraWalp Repository Audit

Stand: 2026-08-15  
Audit-Basis: Branch `main`, Commit `d0c7828`

## Executive Summary

Der Audit umfasste den gesamten getrackten Python-Code unter `src/trading_system`, die zentrale
Konfiguration, alle Tests, CLI und Packaging sowie README und technische Dokumentation. Zusätzlich
wurde der vom Benutzer bereitgestellte Vergleichsreport
`reports/all_comparison_2026-05-01_2026-08-12_post_exit_analysis.csv` zusammen mit den zugehörigen
Comparison-, Position- und Execution-Leg-Dateien geprüft. Produktive Markt- und Fundamentaldaten
wurden ausschließlich gelesen; es wurden keine Syncs, Backfills, Orders oder Datenbereinigungen
ausgeführt.

Phase 1 identifizierte 21 Findings:

| Severity | Anzahl |
|---|---:|
| CRITICAL | 1 |
| HIGH | 6 |
| MEDIUM | 9 |
| LOW | 5 |

Die historische Backtest-Screening-Pipeline ist grundsätzlich Point-in-Time-sicher: historische
Screens verwenden keine Market Snapshots, Facts werden mit `filed <= as_of` begrenzt, Daily- und
Intraday-Bars sind durch `(symbol, timeframe, timestamp)` getrennt, und Post-Exit-Daten fließen
nicht in den Strategy State zurück. Die wesentlichen bestätigten Fehler liegen in einem
After-Hours-Snapshot-Pfad des aktuellen Screens, dem XBRL-Kontext-Dedupe, der Lückenverifikation des
Daily-Syncs und mehreren konkurrierenden Exit-/Intraday-Close-Regeln.

Phase 2 behob zehn Findings vollständig und TRW-021 teilweise. Alle Correctness-Fixes wurden zuerst
durch fehlschlagende Behaviour-Tests reproduziert. Es wurden keine Score-, Risk-, Stop-, Target-,
ATR-, Timing-, Recovery-, Positions- oder Timeframe-Parameter optimiert.

### Prioritäten

1. **TRW-001** – After-Hours-Trade kann einen bereits abgeschlossenen historischen Screen ändern.
2. **TRW-002** – SEC QTD- und YTD-Kontexte derselben Einreichung können kollabieren.
3. **TRW-003** – Daily Sync kann interne Lücken übersehen und den Gesamtbereich als verifiziert
   markieren.
4. **TRW-005** – Bei mehreren aktiven Stops gewinnt derzeit nicht der höchste Schutz-Stop.
5. **TRW-006** – Ein höheres Full Target kann ein zuvor gekreuztes Partial Target überspringen.
6. **TRW-007** – Intraday-Close-Regeln verwenden Daily-Preis und Daily-Timestamp.

## Baseline

| Prüfung | Ergebnis vor Änderungen |
|---|---|
| Branch / Commit | `main` / `d0c7828` |
| Arbeitsbaum | Nur bestehende ungetrackte Benutzerartefakte: `docs/codex_tasks.md`, `reports/`, `results/` |
| Tests gesammelt | 234 |
| `pytest` (normaler Temp-Pfad) | 116 passed, 118 Setup-Fehler durch verweigerten Zugriff auf den globalen Windows-Pytest-Temp-Pfad |
| `pytest -p no:cacheprovider --basetemp=.tmp_pytest_audit_baseline` | 234 passed, 1 warning, 167.92 s |
| Warning | Transitives `websockets.legacy`-DeprecationWarning aus `alpaca-py` |
| `ruff check .` | bestanden |
| `git diff --check` | bestanden |
| Weitere Projektchecks | In `pyproject.toml` sind keine weiteren Lint-/Typecheck-Kommandos definiert |

Der erste Pytest-Fehler war kein Repository-Fehler. Der identische Testbestand bestand vollständig,
sobald Pytest ein beschreibbares, repository-lokales Temp-Verzeichnis erhielt. Bestehende
Benutzerartefakte wurden nicht verändert oder zurückgesetzt.

Leichte Messungen auf den vorhandenen deterministischen Fixtures:

- Feature-, Candidate-Audit-, Compare- und Intraday-Gruppe: 63 Tests in 45.84 s. Der optimierte
  historische Screen benötigt im Behaviour-Test höchstens acht SQLite-Queries und entspricht dem
  Referenz-Screen bitgenau bzw. innerhalb der definierten Float-Toleranz.
- Sync-Gruppe: 39 Tests in 57.22 s. Bulk-Upserts, Batch-Fortschritt, Resume und partielle Fehler
  werden getestet.
- Die 5.49-GB-Produktionsdatenbank enthält 6,085 Companies, 11,899,061 normalisierte Facts, 4,619
  Snapshots und 12,356 Sync-State-Zeilen. Bar-Range-Queries verwenden den Primary-Key-Index.
- Ein vollständiges SQLite `quick_check` wurde nach rund zwei Minuten ohne Ergebnis kontrolliert
  abgebrochen. Diese Integritätsprüfung ist daher nicht als bestanden zu werten; sie hat aber auch
  keinen Fehler gemeldet, bevor sie beendet wurde.

## Critical Findings

### FINDING TRW-001

**Severity:** CRITICAL  
**Confidence:** high  
**File:** `src/trading_system/strategy/screener.py`  
**Function:** `Screener._prepare`

**Problem:** Ein Market Snapshot wird allein dann als Point-in-Time-sicher akzeptiert, wenn das
Kalenderdatum des letzten Trades dem Datum der abgeschlossenen Analyse-Session entspricht. Ein
After-Hours-Trade desselben Tages erfüllt diese Bedingung und ersetzt den offiziellen Daily Close.

**Evidence:** Die Auswahl prüft
`market_snapshot.latest_trade_timestamp.date() == analysis_date`, aber weder den offiziellen
XNYS-Close noch Regular-Hours-Grenzen. README behauptet dagegen, Intraday- und Zukunftspreise
könnten nicht in historische Screens gelangen.

**Why it matters:** Der Snapshot-Preis beeinflusst historische Market Cap, 52-Wochen-Drawdown,
Universe-Filter und Scores nachträglich.

**Observed impact:** Im bereitgestellten Comparison-Report nicht beobachtet, weil historische
Backtests Snapshots explizit deaktivieren. Der aktuelle `screen`-Pfad ist jedoch direkt
reproduzierbar betroffen.

**Potential impact:** Andere Kandidaten, Rankings und Entry-Entscheidungen nach dem offiziellen
Session-Close.

**Recommended fix:** Snapshot nur akzeptieren, wenn der Timestamp timezone-aware ist und innerhalb
der offiziellen XNYS-Regular-Session einschließlich des offiziellen Close liegt.

**Required tests:** Trade exakt am Close bleibt gültig; After-Hours-Trade am selben Datum wird
ignoriert; nächster Kalendertag bleibt ignoriert.

**Safe to fix automatically:** yes

## High Findings

### FINDING TRW-002

**Severity:** HIGH  
**Confidence:** high  
**File:** `src/trading_system/data/xbrl_parser.py`  
**Function:** `parse_company_facts`

**Problem:** Der Parser-Dedupe-Key enthält `metric`, `filed`, `end`, `accn` und `unit`, aber nicht
`start`. Diskrete Quartals- und Year-to-Date-Kontexte derselben 10-Q können deshalb kollabieren.

**Evidence:** Die Datenbank-Unique-Constraint unterscheidet `period_start`; der vorgeschaltete
Parser verwirft diese Unterscheidung bereits. Die Docstring verspricht, unterschiedliche
Filing-Beobachtungen nicht zusammenzuführen.

**Why it matters:** `discrete_quarters` und TTM-Rekonstruktion benötigen beide Kontexte. Der Verlust
kann Wachstums-, FCF-, EBITDA- und Quality-Scores verfälschen.

**Observed impact:** Der Fehler ist mit zwei Revenue-Kontexten derselben Accession und demselben
Periodenende reproduzierbar. Eine rückwirkende Quantifizierung in der bestehenden Datenbank ist
ohne erneuten SEC-Download nicht zuverlässig möglich und wurde daher nicht versucht.

**Potential impact:** Fehlende oder falsche PIT-Fundamentals und Screening-Entscheidungen.

**Recommended fix:** `period_start` in den Dedupe-Key aufnehmen; Alias-Dedupe für tatsächlich
identische Kontexte beibehalten.

**Required tests:** QTD und YTD derselben Filing/End/Accession bleiben beide erhalten; identische
Alias-Beobachtungen bleiben dedupliziert.

**Safe to fix automatically:** yes

### FINDING TRW-003

**Severity:** HIGH  
**Confidence:** high  
**File:** `src/trading_system/data/sync.py`  
**Functions:** `_bar_edge_ranges`, `_daily_coverage_value`,
`DataSynchronizer._sync_daily_history`

**Problem:** Ein inkrementeller erster Daily-History-Check plant für normale Symbole nur vordere
und hintere Kanten. Eine Lücke zwischen vorhandenem frühestem und spätestem Bar bleibt unberührt.
Trotzdem wird nach erfolgreichen Edge-Requests der gesamte angeforderte Zeitraum als verifiziert
gespeichert. SPY besitzt bereits einen Sonderfall für den vollständigen ersten Verify-Request.

**Evidence:** Range-Planung basiert auf `MIN/MAX(timestamp)`. `successful_ranges` kennt nur den
erfolgreichen Provider-Call, nicht dessen innere Vollständigkeit. `_daily_coverage_value` vereinigt
zusätzlich disjunkte alte und neue Intervalle über `min(start)/max(end)` zu einem scheinbar
durchgehenden Bereich.

**Why it matters:** Ein lokales Loch wie Januar vorhanden / Februar fehlend / März vorhanden kann
unentdeckt bleiben und anschließend dauerhaft als geprüft gelten.

**Observed impact:** Der bestehende Test repariert diesen Fall nur für SPY. Für ein normales Symbol
ist der Fehler reproduzierbar.

**Potential impact:** Backtests und Indikatoren verwenden unvollständige Daily-Historien.

**Recommended fix:** Für jedes noch nicht für Feed/Adjustment und Zielintervall verifizierte Symbol
einmal das vollständige explizite Intervall provider-seitig abrufen. Nur bereits verifizierte
Bereiche dürfen den Correction-Overlap-Pfad verwenden. Disjunkte Coverage-Intervalle nicht als ein
kontinuierliches Intervall speichern.

**Required tests:** Nicht-SPY-interne Lücke wird gefüllt; disjunkte Coverage wird nicht falsch
vereinigt; wiederholter verifizierter Lauf bleibt inkrementell/idempotent.

**Safe to fix automatically:** yes

### FINDING TRW-004

**Severity:** HIGH  
**Confidence:** high  
**Files:** `src/trading_system/data/sync.py`, `src/trading_system/backtest/engine.py`  
**Functions:** `_incremental_bar_ranges`, `assess_comparison_intraday_coverage`

**Problem:** Intraday-Sync verwendet ebenfalls nur First/Last-Edges. Strategy Compare erkennt zwar
komplett fehlende Sessions, der anschließende inkrementelle Sync kann eine interne Session-Lücke
aber nicht reparieren. Innerhalb einer Session gilt bereits ein einzelner Bar als vollständige
Coverage; fehlende 5m-/15m-/1h-Bars werden nicht erkannt.

**Evidence:** Coverage bildet nur `present_sessions` und vergleicht diese mit Required Sessions.
Eine erwartete native Timestamp-Folge oder Baranzahl wird nicht geprüft.

**Why it matters:** Eine teilweise gespeicherte Session kann Stop, Partial, Trail und Exit verpassen.

**Observed impact:** Vollständig fehlende Sessions führen nach der Nachprüfung klar zu einer
nicht ausführbaren Strategy statt zu einem Daily-Fallback. Teilweise Sessions können dagegen
unbemerkt verwendet werden.

**Potential impact:** Materiell falscher Intraday-P&L und unfairer Strategy Compare.

**Recommended fix:** Session-aware Gap-Manifest auf Basis des Provider-Timeframes und des
Exchange-Kalenders entwerfen; Halts und echte No-Trade-Bars explizit berücksichtigen. Keine naive
synthetische Bar-Erzeugung.

**Required tests:** Interne ganze Session, einzelner fehlender Bar, Early Close, Halt/No-Trade und
alle drei Timeframes.

**Safe to fix automatically:** no – Provider-/Halt-Semantik ist nicht eindeutig genug.

### FINDING TRW-005

**Severity:** HIGH  
**Confidence:** high  
**File:** `src/trading_system/backtest/position_manager.py`  
**Functions:** `PositionManager._stops`, `evaluate_open`, `evaluate_intrabar`

**Problem:** Konkurrierende Long-Stops werden in Konfigurationsreihenfolge geprüft: Fixed, Prozent-
Trail, ATR-Trail. Wenn der Fixed Stop bei 97 und ein bereits aktiver Trail bei 105 liegt, verkauft
eine Bar durch beide Levels fälschlich bei 97 statt am zuerst gekreuzten, höheren Schutz-Level 105.

**Evidence:** `_stops` gibt unsortierte Kandidaten zurück; beide Evaluators beenden beim ersten
Treffer.

**Why it matters:** Exit-Preis und Exit-Grund sind materiell falsch. Das kann Gewinne in Verluste
verwandeln und Giveback/MFE-Diagnostik verfälschen.

**Observed impact:** Reproduzierbar mit einem Bar, dessen Low beide Stops unterschreitet. Der
bereitgestellte Report beweist nicht, ob konkrete Positionen gleichzeitig mehrere aktive Stops
hatten.

**Potential impact:** Systematisch zu später/zu niedrig ausgeführte Trailing Exits.

**Recommended fix:** Aktive Long-Stops absteigend nach Preis auswerten; bei identischen Levels eine
stabile dokumentierte Reason-Priorität behalten.

**Required tests:** Intrabar und Gap mit mehreren Stops; Stop-vor-Profit-Konservatismus bleibt
erhalten.

**Safe to fix automatically:** yes

### FINDING TRW-006

**Severity:** HIGH  
**Confidence:** high  
**File:** `src/trading_system/backtest/position_manager.py`  
**Functions:** `evaluate_open`, `evaluate_intrabar`, `_partial_decision`

**Problem:** Full Take Profit wird immer vor Partial Take Profit geprüft. Sind beide aktiviert und
liegt ein Partial-Level unter dem Full Target, überspringt eine Bar durch beide das zuerst
erreichte Partial und liquidiert die komplette Position am höheren Target.

**Evidence:** Target-Check steht vor `_partial_decision`. Die Engine kann mehrere Fills in einem Bar
bereits korrekt abarbeiten, bekommt aber wegen dieser Reihenfolge keinen ersten Partial-Entscheid.

**Why it matters:** Menge, Kosten, Execution Legs und P&L entsprechen nicht der konfigurierten
Orderfolge.

**Observed impact:** Die aktuellen Partial-Presets deaktivieren Full Take Profit; deshalb ist der
bereitgestellte Report nicht betroffen. Die frei konfigurierbare Kombination ist jedoch aktiv und
reproduzierbar fehlerhaft.

**Potential impact:** Überhöhter Backtest-Return und fehlende Partial-Legs.

**Recommended fix:** Nach den konservativen Stops das niedrigste noch offene Profit-Level wählen;
bei Open-Gaps gelten weiterhin Open-Fills.

**Required tests:** Combined Partial/Target intrabar und Gap; mehrere Partial-Level bleiben
aufsteigend und einmalig.

**Safe to fix automatically:** yes

### FINDING TRW-007

**Severity:** HIGH  
**Confidence:** high  
**File:** `src/trading_system/backtest/engine.py`  
**Function:** `Backtester.run`, Close-based rule block

**Problem:** Bei Intraday-Position-Management werden Signal Decay, Rotation und Max Hold am
Daily-Bar-Close bewertet und mit dem Daily-Bar-Timestamp ausgeführt. Native Intraday-Bars werden
für Stop/Partial/Trail verwendet, aber nicht für diese Close-Regeln.

**Evidence:** Der Close-Block verwendet immer `bars.get(symbol)`; erst die End-of-Backtest-
Liquidation wählt korrekt `last_intraday_bars`. Ein Daily-Bar-Timestamp kann vor dem Timestamp der
ersten Intraday-Entry-Bar desselben Tages liegen.

**Why it matters:** Exit-Referenz, P&L und zeitliche Reihenfolge können falsch sein; außerdem wird
die konfigurierte Positions-Timeframe-Semantik gebrochen.

**Observed impact:** In den 224 bereitgestellten Comparison-Positionen existiert kein
`exit_timestamp < entry_timestamp`; alle 46 `intraday-dynamic`-Positionen endeten dort über
`atr_trailing_stop`, sodass der fehlerhafte Close-Pfad nicht ausgelöst wurde.

**Potential impact:** Negative/inkonsistente Haltezeiten und Daily- statt Intraday-Close-P&L bei
Signal-Decay/Rotation/Max-Hold.

**Recommended fix:** Für Intraday-Positionen den letzten zulässigen nativen Intraday-Bar als
Close-Execution-Bar und Referenz verwenden; Screening bleibt Daily.

**Required tests:** Same-session Intraday-Close-Exit mit `exit_timestamp >= entry_timestamp`,
nativer Close-Referenz und unverändertem Daily-Screening.

**Safe to fix automatically:** yes

## Medium Findings

### FINDING TRW-008

**Severity:** MEDIUM  
**Confidence:** high  
**File:** `src/trading_system/backtest/engine.py`  
**Functions:** `Backtester.__init__`, `_execute_decision`

**Problem:** `configured` und `legacy` lösen unter der aktuellen YAML in dieselbe wirtschaftliche
Konfiguration auf, verwenden aber unterschiedliche Exit-Reason-Namen. Der Compatibility-Mapping-
Pfad gilt für `configured`, nicht für das explizite Preset `legacy`.

**Evidence:** Im angehängten Report sind Return und sieben Positionen identisch. `C/configured`
meldet `profit_target`/`time_exit`, `C/legacy` dagegen `take_profit`/`max_hold`.

**Why it matters:** Strategy-Compare-Exitdiagnostik zeigt versteckte Unterschiede, obwohl das
Tradingverhalten identisch ist.

**Observed impact:** Zwei Profit- und drei Time-Exit-Legs tragen je nach Label unterschiedliche
Reason-Namen.

**Potential impact:** Falsche Gruppierung in Exit- und Post-Exit-Analysen.

**Recommended fix:** Legacy-Reason-Kompatibilität auch für das explizite `legacy`-Preset aktivieren.

**Required tests:** Configured Legacy-Defaults und Legacy-Preset liefern identische Reasons; andere
Presets behalten moderne Namen.

**Safe to fix automatically:** yes

### FINDING TRW-009

**Severity:** MEDIUM  
**Confidence:** high  
**Files:** `src/trading_system/config.py`, `src/trading_system/cli.py`,
`src/trading_system/ai/export.py`  
**Functions:** `load_settings`, CLI parser/defaults

**Problem:** Default-Config, Datenbank und Reports werden relativ zum aktuellen Working Directory
aufgelöst. Ein CLI-Aufruf aus `repository/reports` sucht
`repository/reports/config/strategy.yaml` und bricht mit einem unaufbereiteten Traceback ab. Der
AI-Export besitzt zusätzlich ein unabhängiges relatives `output/`-Default.

**Evidence:** `python -m trading_system.cli status` aus `reports/` reproduziert
`FileNotFoundError: config\\strategy.yaml`.

**Why it matters:** Read-only-Kommandos sind CWD-abhängig; ein falscher relativer DB-Pfad kann durch
die normale `connect()`-Semantik sogar eine neue leere SQLite-Datei anlegen.

**Observed impact:** CLI aus `reports/` ist aktuell nicht nutzbar.

**Potential impact:** Versehentliche leere Datenbanken oder Reports in unerwarteten Verzeichnissen.

**Recommended fix:** Bundled Default-Config relativ zum Paket/Repository auflösen. Relative
Storage-Pfade deterministisch gegen die Config-Projektbasis auflösen; explizite Pfade nicht
heuristisch durchsuchen; Config-Fehler als klare CLI-Meldung ausgeben. Das historisch dokumentierte
AI-`output/`-Verhalten separat beibehalten oder explizit dokumentieren.

**Required tests:** Aufruf aus Repository-Root und `reports/`; explizite fehlende Config; Windows-
Pfade; keine CWD-abhängige DB-Auswahl.

**Safe to fix automatically:** yes

### FINDING TRW-010

**Severity:** MEDIUM  
**Confidence:** high  
**Files:** `src/trading_system/strategy/screener.py`, `src/trading_system/data/database.py`  
**Functions:** `Screener._prepare`, `facts_available_as_of`, `latest_market_snapshot`

**Problem:** Der aktuelle Screen führt pro Company separate Bar-, Fact- und Snapshot-Queries aus;
die Snapshot-Abfrage öffnet sogar trotz bestehender Screen-Connection eine eigene Connection. Der
Fact-Plan verwendet für All-Metric-Reads einen Temp-B-Tree für die Sortierung.

**Evidence:** `_prepare` wird pro Company aufgerufen. Der historische Backtest-Pfad besitzt dagegen
bereits Batch-Iteratoren und Feature-Caches mit höchstens acht Querys im Fixture-Test.

**Why it matters:** Das Live-Universe umfasst tausende Symbole; der Pfad skaliert als N+1 und baut
viele Pydantic-Objekte in Schleifen.

**Observed impact:** Kein Correctness-Fehler; messbare Fixture-Pfade zeigen, dass der optimierte
historische Pfad wesentlich besser gebündelt ist.

**Potential impact:** Langsame Screens und unnötige SQLite-Connection-/Allocation-Last.

**Recommended fix:** Bestehende Batch-/Feature-Infrastruktur vorsichtig für den aktuellen Screen
wiederverwenden oder Snapshots mindestens über die vorhandene Connection/batchweise lesen. Erst an
realistischen Daten messen.

**Required tests:** Query-Count, Ergebnisparität und Snapshot-PIT-Grenzen.

**Safe to fix automatically:** no – breiter Performance-Refactor ohne produktive Messung.

### FINDING TRW-011

**Severity:** MEDIUM  
**Confidence:** high  
**File:** `src/trading_system/config.py`  
**Classes:** `StrategyConfig` und verschachtelte Config-Modelle

**Problem:** `StrategyConfig` erlaubt unbekannte Top-Level-Felder, während nur einzelne
Untermodelle `extra="forbid"` verwenden. Tippfehler können still akzeptiert werden. Außerdem weichen
mehrere Python-Fallbacks materiell von YAML ab, etwa `max_positions=5` vs. YAML `1` und
`max_position_pct=0.20` vs. YAML `1.0`.

**Evidence:** `model_config = ConfigDict(extra="allow")`; fehlende YAML-Felder fallen unbemerkt auf
abweichende Code-Defaults zurück.

**Why it matters:** Eine falsch geschriebene oder unvollständige Config kann unerwartetes
Tradingverhalten erzeugen.

**Observed impact:** Die aktuelle YAML ist vollständig und alle darin enthaltenen Felder sind
aktiv; kein aktueller Parameter wird ignoriert.

**Potential impact:** Stille Fehlkonfiguration in zukünftigen oder externen Config-Dateien.

**Recommended fix:** Erst eine Versionierungs-/Backward-Compatibility-Regel definieren, dann
unbekannte Felder verbieten oder gezielt als deprecated markieren. Keine Defaultwerte im Audit
ändern.

**Required tests:** Unknown-field-Negativfälle und absichtlich minimale Configs.

**Safe to fix automatically:** no

### FINDING TRW-012

**Severity:** MEDIUM  
**Confidence:** high  
**File:** `src/trading_system/backtest/engine.py`  
**Function:** `_open_position`

**Problem:** Nominale Positionsgröße folgt korrekt `allowed_risk / stop_distance` und den Cash-/
Position-Caps. Der Stop-Distance-Risk enthält aber weder modellierte Exit-Slippage noch Commission.

**Evidence:** Entry-Fill und Entry-Commission begrenzen Cash; die Risk-Distanz ist ausschließlich
Entry-Preis minus Stop-Referenz.

**Why it matters:** Ein Stop kann netto etwas mehr als `risk_per_trade` verlieren, selbst ohne Gap.

**Observed impact:** Bei aktuellen 5 bps Slippage und 0 bps Commission klein, aber vorhanden.

**Potential impact:** Größere Abweichung bei höheren Kostenkonfigurationen.

**Recommended fix:** Semantik zuerst explizit festlegen: Preisrisiko oder vollständig belastetes
Nettorisiko. Keine stillschweigende Sizing-Änderung im Audit.

**Required tests:** Risk-Cap mit Entry-/Exit-Kosten und Gap-Abgrenzung.

**Safe to fix automatically:** no – würde bewusst Tradingverhalten ändern.

### FINDING TRW-013

**Severity:** MEDIUM  
**Confidence:** high  
**File:** `src/trading_system/backtest/candidate_audit.py`  
**Classes:** Candidate-Audit-Collector/Observer

**Problem:** Der vollständige Audit hält Candidate Records und zahlreiche Diagnosefelder über die
gesamte Laufzeit. Die Struktur wächst mit Sessions × tatsächlich funnel-qualifizierten Symbolen;
Score-Historien und Reports erhöhen die Retention zusätzlich.

**Evidence:** Session-, Failure-, Near-Miss- und Candidate-Collections werden bis zum finalen Export
materialisiert. Near Misses sind begrenzt, vollständige Kandidatenhistorien nicht.

**Why it matters:** Lange Multi-Year-Audits mit großem Universe können viel RAM benötigen.

**Observed impact:** Kleine Fixture-Läufe sind unauffällig; kein Memory Leak im engeren Sinn wurde
gefunden.

**Potential impact:** Hoher Peak-RAM und große JSON/CSV-Objekte.

**Recommended fix:** Optionales Streaming/Chunking oder explizite Retention-Limits entwerfen, ohne
Funnel Conservation und historische Exporte zu verlieren.

**Required tests:** Conservation über Chunks, deterministische Exportreihenfolge, begrenzter Peak.

**Safe to fix automatically:** no

### FINDING TRW-014

**Severity:** MEDIUM  
**Confidence:** medium  
**File:** `src/trading_system/fundamentals/metrics.py`  
**Functions:** Balance-Sheet- und Debt-Auswahl

**Problem:** Cash, Shares und Debt-Komponenten werden nach ihren jeweils neuesten zulässigen Facts
ausgewählt. Bei unvollständigen Filings können Kennzahlen Komponenten unterschiedlicher
`period_end`-Stichtage mischen. Debt Current/Noncurrent wird bereits nach gleicher Periode
gekoppelt; die vollständige Balance-Snapshot-Kohärenz ist nicht überall erzwungen.

**Evidence:** Auswahl ist filing-date-sicher, aber überwiegend metrikspezifisch. Es gibt keinen
aktuellen Datenfallback und damit keinen Lookahead.

**Why it matters:** Enterprise Value, ROIC und Debt/EBITDA können accounting-semantisch inkonsistent
sein, obwohl jeder Einzelwert PIT-verfügbar war.

**Observed impact:** Keine konkrete fehlerhafte Position im bereitgestellten Report nachgewiesen.

**Potential impact:** Score-Verzerrung bei lückenhaften oder ungewöhnlichen Filings.

**Recommended fix:** Kohärenzregel und erlaubte Staleness je Metrik fachlich definieren; Debug-
Provenance um gemischte Periodenwarnung ergänzen.

**Required tests:** Missing components, Amendments und gemischte Perioden.

**Safe to fix automatically:** no

### FINDING TRW-015

**Severity:** MEDIUM  
**Confidence:** high  
**File:** `src/trading_system/data/database.py`  
**Functions:** mehrere nominelle Read-Methoden

**Problem:** Mehrere Read-Methoden verwenden `connect()` statt `read_only()`. `connect()` erstellt
Parent-Verzeichnisse, kann eine neue leere DB anlegen und committet beim Verlassen.

**Evidence:** Unter anderem `facts_available_as_of`, `list_tradable_companies` und
`dataset_states` verwenden `connect()`. Range-/Storage-Methoden nutzen bereits den vorhandenen
read-only Context Manager.

**Why it matters:** Ein Pfadfehler kann unbemerkt eine neue DB erzeugen; Lesevorgänge erhalten mehr
Schreibberechtigung als erforderlich.

**Observed impact:** Verstärkt TRW-009; keine produktiven Daten wurden im Audit verändert.

**Potential impact:** Versehentliche leere Datenbasis oder unnötige SQLite-Locks.

**Recommended fix:** Read-Methoden schrittweise auf `read_only()` umstellen; First-Run- und
Migrationspfade separat explizit halten.

**Required tests:** Missing DB schlägt bei Reads klar fehl; Initialize erstellt weiterhin; Screen-
und Sync-Verhalten bleibt gleich.

**Safe to fix automatically:** no – breite API-/First-Run-Auswirkung.

### FINDING TRW-016

**Severity:** MEDIUM  
**Confidence:** high  
**File:** `src/trading_system/backtest/candidate_audit.py`  
**Function:** Failure-Stage-Klassifikation

**Problem:** Unbekannte zukünftige Exclusion Reasons fallen standardmäßig in die Stage
`pit_fundamentals`. Eine neue Data- oder Strategy-Regel kann damit still falsch diagnostiziert
werden.

**Evidence:** Der Fallback ist keine `unknown`-/Invariant-Verletzung, sondern eine fachliche Stage.

**Why it matters:** Der Audit kann dann Data Failure und Strategy Failure falsch zuordnen, obwohl
Funnel Conservation numerisch weiterhin stimmt.

**Observed impact:** Alle aktuell erzeugten Reasons sind abgedeckt; kein Fehler im bestehenden
Report beobachtet.

**Potential impact:** Irreführende Diagnose nach Erweiterungen.

**Recommended fix:** Explizite Unknown-Stage oder fail-fast Registry mit getesteter Abdeckung.

**Required tests:** Neue unbekannte Reason darf nicht als PIT-Fundamental erscheinen.

**Safe to fix automatically:** no – Report-Schema/Backward Compatibility klären.

## Low Findings

### FINDING TRW-017

**Severity:** LOW  
**Confidence:** high  
**File:** `src/trading_system/strategy/scoring.py`  
**Function:** `robust_z_scores`

**Problem:** Interner Helper ohne Import, Call Site, Test, CLI-/Config-/Preset-/String-Dispatch-
Referenz oder `__init__`-Export. Die aktive robuste Peer-Behandlung verwendet Winsorization und
Percentiles.

**Evidence:** Repository-weite Symbolsuche findet ausschließlich die Definition.

**Why it matters:** Kleine falsche öffentliche Erwartung und unnötige Wartungsfläche.

**Observed/Potential impact:** Kein Laufzeitimpact.

**Recommended fix:** Helper entfernen; Scoring-Tests vollständig ausführen.

**Required tests:** Bestehende Scoring-Suite.

**Safe to fix automatically:** yes

### FINDING TRW-018

**Severity:** LOW  
**Confidence:** high  
**File:** `src/trading_system/data/database.py`  
**Object:** `ix_bars_symbol_timeframe_timestamp`

**Problem:** Der explizite Index dupliziert exakt den automatisch erzeugten Primary-Key-Index auf
`(symbol, timeframe, timestamp)`.

**Evidence:** `PRAGMA index_info` zeigt identische Spalten; Query Planner verwendet den
Autoindex.

**Why it matters:** Zusätzlicher Speicher und zusätzliche Write-Amplification.

**Observed impact:** Kein Query-Nutzen in den geprüften Range-Plänen.

**Potential impact:** Langsamere Upserts und größere DB.

**Recommended fix:** In einer separaten, getesteten Migration entfernen; nicht ad hoc an der
5.49-GB-Produktionsdatei.

**Required tests:** Migrations-Idempotenz und Query-Pläne vor/nach Migration.

**Safe to fix automatically:** no

### FINDING TRW-019

**Severity:** LOW  
**Confidence:** high  
**Files:** `README.md`, `docs/position-management.md`,
`docs/intraday-market-data.md`, `docs/historical-daily-backfill.md`

**Problem:** README nennt weiterhin ausschließlich Milestone 4; Position Management behauptet,
der persistente Layer sei Daily-only und Intraday-Presets würden abgewiesen. Einzelne Sync- und
PowerShell-Beispiele beschreiben den heutigen Resume-/Windows-Pfad ungenau.

**Evidence:** Der Code und andere aktuelle Docs unterstützen native 5m/15m/1h-Speicherung,
Strategy F und Compare-Prefetch.

**Why it matters:** Operative Nutzung und Audit-Erwartungen widersprechen dem Code.

**Observed/Potential impact:** Dokumentationsfehler, kein Laufzeitimpact.

**Recommended fix:** Status und Intraday-Grenzen aktualisieren; den historischen
`docs/intraday-task.md` klar als Task-/Designartefakt kennzeichnen statt als Bedienungsanleitung.

**Required tests:** CLI-Help/Docs-Abgleich.

**Safe to fix automatically:** yes

### FINDING TRW-020

**Severity:** LOW  
**Confidence:** high  
**File:** `pyproject.toml` / transitive runtime dependency

**Problem:** Jeder Testlauf meldet ein `websockets.legacy`-DeprecationWarning aus der installierten
Alpaca-Abhängigkeitskette.

**Evidence:** Ein Warning in vollständiger und selektiver Suite; kein eigener Import von
`websockets.legacy`.

**Why it matters:** Künftige Dependency-Versionen könnten den Legacy-Namespace entfernen.

**Observed impact:** Nur Warning.

**Potential impact:** Spätere Provider-Client-Inkompatibilität.

**Recommended fix:** Beim nächsten geplanten Dependency-Update gegen eine kompatible Alpaca-
Version verifizieren. Kein Upgrade nur für diesen Audit.

**Required tests:** Vollständige Provider-Adapter-Suite nach geplantem Upgrade.

**Safe to fix automatically:** no

### FINDING TRW-021

**Severity:** LOW  
**Confidence:** high  
**Files:** `src/trading_system/backtest/report.py`,
`src/trading_system/models/backtest.py`, `docs/position-diagnostics.md`

**Problem:** Legacy-Felder wie `number_of_trades`, `win_rate` und die kompatible `*_trades.csv`
sind Execution-Leg-semantisch; zusätzlich wird eine inhaltlich identische
`*_execution_legs.csv` geschrieben. Das Terminal-Label `Exit summary` ist dabei missverständlich.

**Evidence:** Die aktuelle Dokumentation erklärt die Kompatibilität korrekt. Der bereitgestellte
Report enthält 224 Positionen, aber 263 Execution Legs; Strategy F enthält 46 Positionen und 61
Legs.

**Why it matters:** Externe Nutzer können Leg Win Rate als Position Win Rate lesen.

**Observed impact:** JSON/CSV-Felder sind intern konsistent, aber leicht fehlzuinterpretieren.

**Potential impact:** Falsche manuelle Analyse.

**Recommended fix:** Backward-kompatible Felder behalten, Terminal-/README-Labels explizit als
Execution Legs benennen. Doppelte CSV erst nach einer Versionierungs-/Deprecation-Phase entfernen.

**Required tests:** Exportnamen und Position-vs.-Leg-Counts.

**Safe to fix automatically:** yes für Labels/Docs; no für Feld-/Dateientfernung.

## Dead Code

Bestätigt und sicher entfernbar ist nur `robust_z_scores` (TRW-017). Vor dieser Einstufung wurden
direkte Imports, Call Sites, Tests, CLI-Registrierung, Config-/Preset-Referenzen, String-Dispatch,
`__init__`-Exports und Dokumentation geprüft.

Nicht als Dead Code eingestuft:

- `HistoricalScreenSource` ist ein aktiver Referenzadapter für Paritäts- und PIT-Tests.
- `daily_bars` ist eine dokumentierte Compatibility View und wird von Migrations-/Kompatibilitätstests
  geschützt.
- `backtest-compare`, `sync --full`, Legacy-Reason-Felder und doppelte Trades/Execution-Legs-Exports
  sind explizite Backward-Compatibility-Pfade.
- Sämtliche CLI-Kommandos sind registriert und getestet oder dokumentiert. Es wurde kein sicher
  totes Modul oder Preset gefunden.

## Duplicate Logic

Entry-Filter und Variant-Entscheidungen laufen zentral über `evaluate_variant_entry`; der
Candidate Audit beobachtet dieselbe Entscheidung statt einen zweiten Screener zu implementieren.
`HistoricalFeatureScreenSource` nutzt dieselben Scoring-/Fundamental-Funktionen wie der Referenz-
Screener. Presets erzeugen nur `PositionManagementConfig`; die eigentlichen Exit-Regeln liegen im
Position Manager. Engine dupliziert keine Stop-/Target-Formeln, orchestriert aber Timeframe und
Execution. Die bestätigten Abweichungen sind Reihenfolge-/Bar-Auswahlfehler TRW-005 bis TRW-007,
keine zweite vollständige Trading Engine.

## Trading Correctness

- Signal am abgeschlossenen Daily Close und Entry in der nächsten zulässigen Session sind getrennt.
- Intraday-Entry erfolgt am ersten gespeicherten Regular-Hours-Bar des Symbols; kein Bar vor dem
  Entry gelangt in den Position State.
- Gap-Fills verwenden den Open-Preis, Stop-vor-Profit bleibt bei Daily-OHLC konservativ, Trails
  werden nach einem abgeschlossenen Bar nur raise-only für den Folgebar aktualisiert.
- Eine offene Position pro Symbol wird durch den Positions-Dictionary-Key erzwungen. Re-Entry,
  Cooldown und Fresh-Trigger-Metadaten besitzen Behaviour-Tests.
- Partial-Fills teilen Entry-Kosten proportional auf und `finalize_position` aggregiert Mengen und
  P&L wirtschaftlich. Der ergänzte Audit-Test für 100 @ 100, 50 @ 102 und 50 @ 97 bestätigt
  exakt -50 Brutto-/Netto-P&L bzw. -0.5% ohne Kosten.
- MFE/MAE auf dem Exit-Bar verwenden bewusst nur sicher bekannte Open-/Execution-Level, um aus
  Daily-OHLC keine unbekannte Intrabar-Reihenfolge zu erfinden.

## PIT / Lookahead

- Backtest-Facts sind auf `filed <= screen_date` begrenzt. Period End allein macht keinen Wert
  verfügbar. Amendments bleiben in der Datenbank getrennt und werden nach Verfügbarkeit gewählt.
- Vorbereitete Historical Features begrenzen Bars und Facts pro Session. Vorwärts hinzugefügte Bars
  und Filings verändern frühere Fixture-Screens nicht.
- Post-Exit-Bars werden erst nach der Simulation an Positionen angehängt und ändern keine Trades.
- Peer Cross Sections werden aus dem PIT-Zustand der jeweiligen Session erzeugt.
- Kein aktueller Fundamental-/Market-Cap-Fallback wird rückwirkend benutzt.
- Die Snapshot-Grenze TRW-001 und der Parser-Kontextverlust TRW-002 wurden in Phase 2 behoben.
  Mögliche Balance-Periodenmischung TRW-014 sowie eine Datenreparatur historisch verworfener
  Rohkontexte bleiben offen.

## Multi-Timeframe

| Aspekt | Audit-Ergebnis |
|---|---|
| Persistenter Key | `(symbol, timeframe, timestamp)` als Primary Key |
| DB-Reads | Alle Bar-Reads für Backtest/Sync filtern den angeforderten Timeframe |
| Caches | Daily Screens sind absichtlich timeframe-unabhängig; Intraday-Historien sind pro Run auf genau einen Position-Timeframe begrenzt |
| ATR | Entry/Trail-ATR nutzt den nativen Position-Timeframe; kein Daily-Fallback |
| Screening | Daily und von Intraday-Positionsmanagement getrennt |
| 5m / 15m / 1h | Alle drei durch Modelle, Provider, DB, Sync, Compare und parametrische Tests abgedeckt |
| Lücken | Interne/partielle Intraday-Coverage ist nicht ausreichend verifiziert (TRW-004) |

## Market Sessions / Timezones

Persistente Bar-Modelle normalisieren timezone-aware Timestamps nach UTC. XNYS-Sessions,
Feiertage, DST und Early Closes kommen aus `exchange_calendars`; Extended Hours werden in
`America/New_York` lokalisiert und anschließend nach UTC konvertiert. Regular-Hours-Filter verwenden
offizielle Open/Close-Grenzen. Naive Bar-Timestamps werden abgewiesen. Der Snapshot-Close-Guard
TRW-001 und der Daily-Timestamp im Intraday-Close-Pfad TRW-007 wurden in Phase 2 behoben und durch
Behaviour-Tests abgesichert.

## Data / Sync

- Bar-Upserts validieren positive OHLC-Werte, `high >= open/close/low`, `low <= open/close/high`,
  Volume/Trade Count und Timeframe. Provider-Korrekturen überschreiben deterministisch denselben
  Key; Batch-Statistiken trennen inserted/updated/unchanged/duplicate/invalid.
- Daily- und Intraday-Sync sind gebatcht, gepaged und nach Fehlern resumierbar. SEC-Requests besitzen
  Retry/Rate-Limit/404-Negativcache und schreiben Accession-State erst nach erfolgreichem Parse/
  Persist.
- SPY wird als Benchmark separat einbezogen. Benchmark-Reads sind auf den Backtestzeitraum begrenzt.
- Daily-interne Lücken TRW-003 wurden für neue Verifikationsläufe behoben; Intraday-interne bzw.
  partielle Lücken bleiben TRW-004.

## Database

- Schema/Keys und UPSERT-Semantik sind konsistent. Die `daily_bars` View ist weiterhin ein
  notwendiger Compatibility Layer.
- Bar-Range-Plan: Primary-Key-Range-Search auf Symbol, Timeframe und Timestamp.
- Fact-PIT-Plan: `ix_facts_pit` wird für Symbol verwendet, benötigt bei All-Metric-Reads aber eine
  temporäre Sortierung; dies ist Teil des Performance-Findings TRW-010.
- Keine produktiven Tabellen, Rows, Facts, Bars, Sync-States oder Cache-Payloads wurden gelöscht.
- Redundanter Bar-Index: TRW-018. Schreibfähige Read-Methoden: TRW-015. Beides bleibt bewusst ohne
  produktive Schema-/Lifecycle-Änderung deferred.

## Config

Status der Konfigurationsgruppen:

| Status | Felder |
|---|---|
| ACTIVE | Alle Felder in `config/strategy.yaml`: Universe, Peers, Score-Gewichte/-Kurven, Filter, Data Quality, Technical, Portfolio, Risk, Backtest, Intraday, Position Management, Storage und SEC |
| DEPRECATED / COMPATIBILITY | `backtest.profit_target_pct` und `backtest.max_holding_days` als Legacy-Fallbacks; `sync --full`; Legacy-Preset/-Reason-Felder |
| UNUSED | Environment-Flag `ENABLE_ORDER_SUBMISSION` wird validiert, hat mangels Orderpfad aber keine aktive Wirkung; es bleibt als Safety-Placeholder bestehen |
| AMBIGUOUS | Unbekannte Top-Level-Felder durch `extra="allow"`; abweichende Code-/YAML-Defaults (TRW-011) |

Es wurde kein Strategy-, Score-, Risk- oder Timeframe-Parameter verändert oder optimiert.

## Preset / Strategy Fairness

Alle Presets verwenden unter der aktuellen YAML `risk_per_trade=1%`, `max_position_pct=100%`,
`max_positions=1`, 5 bps Slippage und 0 bps Commission. Screening bleibt der gewählten
Score-Variante zugeordnet; im Position-Management-Vergleich ist dies Variante C.

| Preset | Stop | Take Profit | ATR Trail | Partial | Signal Decay | Max Hold | Timeframe |
|---|---|---|---|---|---|---|---|
| configured | ATR/Risk Entry Stop | Legacy 12% | nein | nein | nein | hard 10 | 1d |
| legacy | ATR/Risk Entry Stop | Legacy 12% | nein | nein | nein | hard 10 | 1d |
| dynamic-hold | fixed 3% | nein | nein | nein | 75% | aus | 1d |
| take-profit | fixed 3% | 2% | nein | nein | 75% | aus | 1d |
| atr-trailing | fixed 3% | nein | 1×ATR | nein | 75% | aus | 1d |
| partial-profit | fixed 3% | nein | 1×ATR | 50% bei +1.5% | 75% | aus | 1d |
| intraday-dynamic | fixed 3% | nein | 1×ATR | 50% bei +1.5% | 75% | aus | 15m Default |
| baseline-fixed-stop | fixed 3% | nein | nein | nein | nein | aus | 1d |
| fixed-stop-max-hold | fixed 3% | nein | nein | nein | nein | hard 10 | 1d |
| fixed-stop-take-profit | fixed 3% | 2% | nein | nein | nein | aus | 1d |
| fixed-stop-atr-trailing | fixed 3% | nein | 1×ATR | nein | nein | aus | 1d |
| fixed-stop-partial-atr | fixed 3% | nein | 1×ATR | 50% bei +1.5% | nein | aus | 1d |

Die Fixed-Stop-Baselines unterscheiden sich nur in den beabsichtigten Exit-Komponenten. Der
Compare-Pfad teilt PIT-Screens über `CachedScreenSource`, lädt Intraday nur für tatsächlich
intraday-abhängige Runs und besitzt keinen Daily-Fallback. Verbleibende Fairness-Risiken sind die
Coverage-Prüfung TRW-004. Die Label-Divergenz TRW-008 wurde behoben.

## Position Sizing / P&L

Die Stückzahl ist das Minimum aus risikobasierter Menge, `max_position_pct`, verfügbarem Cash und
optionalem Fractional-Share-Rounding. Stop-Distanz und Entry-Slippage werden konsistent verwendet;
eine Position mit nichtpositiver Distanz wird verworfen. TRW-012 dokumentiert, dass modellierte
spätere Exit-Kosten nicht in das anfängliche Risk Budget eingerechnet sind. Dieser fachliche
Semantikwechsel wurde nicht automatisch vorgenommen. Partial-Exits verändern weder Originalmenge
noch das Risk Budget nachträglich; Kosten, gewichtete Mengen, realisierte P&L und Endliquidation
werden positionsbezogen aggregiert.

## Metrics / Reports

Position und Execution Leg sind in Modellen und neuen Kennzahlen getrennt. Legacy-Kennzahlen
bleiben absichtlich Leg-basiert. Profit Factor, Average Win/Loss, Holding Period, Costs, Slippage,
Exposure, Drawdown, MFE/MAE, Capture/Giveback und Post-Exit-Horizonte wurden gegen Implementierung
und Tests geprüft. Annualisierte Ratios liefern bei zu wenigen Returns `None`; CLI warnt bei kurzen
Backtests.

Snapshot der bereitgestellten Artefakte vor Cleanup:

| Datei | Rows | SHA-256 |
|---|---:|---|
| Comparison CSV | 14 | `464f030d151327b845c4e9f1b39cac567b6c0df9771f20ca268697555d268907` |
| Positions CSV | 224 | `a8c9fc5b405cc28a19dbd59f7bb1c475cd003ead1033e8667edd72824880d6eb` |
| Execution Legs CSV | 263 | `67c9fa88a5539deb3c06c774b5352068ef6ed34e156e33518054c90797f82e61` |
| Post-Exit CSV | 224 | `9fa7f328efca47592443a72fbe1767c2444011326a51ff22bd7deb95a77f7d37` |

Im angehängten Post-Exit-Report sind Feldnamen/Horizonte konsistent; Reference ist ausdrücklich der
Exit-Referenzpreis vor Sell-Slippage. Es gibt keine Position mit Exit-Timestamp vor Entry-Timestamp.
TRW-021 beschreibt die verbleibende Legacy-Namensambiguität.

## Candidate Audit

Der Candidate Audit instrumentiert die produktive `evaluate_variant_entry`-Pipeline und besitzt
keine parallele Screeningregel. First-Failure, Data-vs.-Strategy-Failure, Near Misses, PIT-Coverage,
Entry-Symbole und Portfolio-/Execution-Blocker werden aus denselben Funnel-Entscheidungen erzeugt.
Conservation-Invarianten brechen bei Unterlauf oder nicht erhaltener Gesamtmenge hart ab. TRW-016
bleibt als eng begrenzte Semantikfrage für unbekannte Gründe offen; TRW-013 betrifft die ungebundene
Retention großer historischer Audit-Resultate.

## CLI / Path Handling

Alle registrierten Commands und Aliase wurden gegen Parser, Handler, Tests und Dokumentation
abgeglichen. Der Default-Configpfad sowie daraus abgeleitete `.env`-, DB- und Reportpfade sind nach
TRW-009 CWD-unabhängig. Explizite Configpfade bleiben deterministisch relativ zu ihrer
Konfigurationsbasis; fehlende oder ungültige Konfiguration endet mit einer klaren Fehlermeldung und
Exitcode 2. Pfade werden mit `pathlib` verarbeitet und bleiben Windows-/Linux-kompatibel.

## Performance / Memory

Die große historische Pipeline verwendet Batch-Reads, kompakte Raw-Bar-Werte, Filing-Cutoff-Caches
und wiederverwendete Screens. Kein offensichtlicher O(N²)-Hotpath wurde im aktiven Backtest-Kern
gefunden. Kandidaten-Prefetch beschränkt Intraday-Symbole auf PIT-qualifizierte Candidates, lädt
aber konservativ alle folgenden Vergleichssessions. Verbleibende priorisierte Themen sind N+1 im
aktuellen Screen (TRW-010) und Audit-/History-Retention (TRW-013). Mikrooptimierungen ohne Messung
wurden nicht vorgenommen.

## Error Handling / Invariants

Es existiert kein produktiver `except Exception: pass`-Pfad. Breite Catches befinden sich an
Batch-/Provider-Grenzen, loggen Kontext und zählen Fehler, oder rollen Transaktionen zurück.
Fehlende Intraday-Daten und Warmup führen zu expliziten Strategy-Failures statt Daily-Fallback.
Bar-OHLC- und Timestamp-Invarianten werden validiert; Quantities und Partial-Fills werden vor der
Ausführung geprüft. Eine allgemeine persistierte Invariante `exit_timestamp >= entry_timestamp`
fehlt weiterhin; der konkret reproduzierbare Intraday-Zeitfehler TRW-007 ist jedoch durch die native
Bar-Auswahl und einen engen Behaviour-Test abgesichert.

## Dependencies / Security

Alle direkten Dependencies aus `pyproject.toml` sind importiert und aktiv. Es wurde keine sicher
ungenutzte Dependency gefunden und kein Upgrade durchgeführt. `.env` und SQLite-Runtime-Dateien
sind ignoriert; `.env.example` enthält nur leere Platzhalter. Es sind keine Secrets, Private Keys
oder `.env`-Dateien getrackt. Credentials werden nicht geloggt, und es existiert kein Order-
Submission-Codepfad. TRW-020 dokumentiert das einzige Dependency-Warning.

## Tests

Die Suite deckt PIT-Filings/Bars, Reference-vs.-Optimized-Screen, Candidate Funnel Conservation,
Timeframe-Isolation, 5m/15m/1h, Warmup, Gap Stops, Trail Timing, Partial-Kosten, Re-Entry, Post-Exit-
Isolation, Sync-Resume und DB-Migrationen gut ab. Phase 2 ergänzte zwölf gesammelte Testfälle für:

- After-Hours Snapshot am selben Session-Datum,
- XBRL QTD/YTD derselben Filing,
- interne Daily-Lücke für normale Symbole,
- konkurrierende Stops,
- kombinierte Partial-/Full-Target-Reihenfolge,
- Intraday-Close-Regel mit nativem Timestamp,
- CWD-unabhängiger Default-Config-/Storage-Pfad,
- identische Legacy-Reason-Semantik,
- exakter 50/50-Partial-P&L-Fall.

Alle neuen Tests sind Behaviour- oder Regressionstests; kein bestehender Test wurde gelöscht oder
abgeschwächt. Interne Intraday-Bar-Lücken und PIT-Balance-Periodenkohärenz bleiben als deferred
Test-/Designarbeit bestehen.

## Documentation

`README.md`, `intraday-market-data.md`, `historical-candidate-audit.md`,
`historical-daily-backfill.md`, `position-diagnostics.md` und `position-management.md` wurden mit
CLI, Config, Presets und Reportfeldern verglichen. Candidate-Audit- und Position-Diagnostics-Doku
sind semantisch aktuell. Die als TRW-019 erfassten veralteten Intraday-/Milestone-/Priority-Angaben
wurden in Phase 2 behoben.
`README.md`, `intraday-market-data.md`, `historical-daily-backfill.md` und
`position-management.md` wurden für die bestätigten Semantikänderungen aktualisiert;
`intraday-task.md` ist nun ausdrücklich als historisches Designdokument markiert.
`docs/codex_tasks.md` war bereits vor dem Audit ein ungetracktes Benutzerartefakt und wurde nicht
verändert.

## Fixed During Audit

| Finding | Änderung | Absicherung |
|---|---|---|
| TRW-001 | Same-day Market Snapshots werden nur innerhalb der offiziellen Regular Session akzeptiert. | After-Hours- und Exact-Close-Tests |
| TRW-002 | Der XBRL-Dedupe-Key enthält `period_start`; QTD/YTD-Kontexte derselben Filing bleiben getrennt. | Same-filing-QTD/YTD-Test |
| TRW-003 | Noch nicht verifizierte Daily-Intervalle werden vollständig geladen; disjunkte Coverage wird nicht als zusammenhängend gespeichert. | Internal-gap- und disjoint-coverage-Tests |
| TRW-005 | Bei mehreren getroffenen Long-Stops gewinnt der höchste aktive Schutz-Stop, einschließlich Gap-Open. | parametrisierte Stop-Priority-Tests |
| TRW-006 | Profit-Ziele werden in aufsteigender Preisreihenfolge ausgeführt; ein Gap kann Partial und danach Full Target abarbeiten. | Intrabar- und Gap-Target-Tests |
| TRW-007 | Intraday-Close-Regeln verwenden den letzten nativen Intraday-Bar samt Timestamp. | 15m-Close-Rule-Test |
| TRW-008 | Explizites `legacy` und äquivalentes `configured` verwenden dieselben kompatiblen Exit-Reason-Labels. | Legacy-Reason-Test |
| TRW-009 | Default-Config, `.env`, DB und Reports werden repository-/configbasiert statt CWD-relativ aufgelöst; Configfehler liefern CLI-Code 2 mit klarer Meldung. | Non-root-CWD- und Missing-config-Tests |
| TRW-017 | Die nach statischer und dynamischer Referenzsuche ungenutzte Funktion `robust_z_scores` wurde entfernt. | volle Suite und Importprüfung |
| TRW-019 | Veraltete Milestone-, Intraday-, Sync- und PowerShell-Dokumentation wurde aktualisiert. | Code-/CLI-Abgleich |
| TRW-021 (teilweise) | Terminalausgabe nennt die bisherige `Exit summary` nun eindeutig `Execution-leg exit summary`; kompatible Dateien/Felder bleiben bestehen. | Report-/Dokumentationsabgleich |

Es wurden keine Dateien entfernt. Der bestätigte Dead-Code-Abbau beträgt eine Funktion bzw. rund
neun Runtime-LOC. Bestehende Compatibility-Views, CLI-Aliase und Reportfelder blieben erhalten.

## Controlled Cleanup Regression

| Prüfung | Ergebnis nach Phase 2 |
|---|---|
| Tests gesammelt / bestanden | 246 / 246 |
| Pytest-Warnings | 1, unveränderte transitive `websockets.legacy`-DeprecationWarning |
| Laufzeit volle Suite | 166.63 s |
| `ruff check .` | bestanden |
| `git diff --check` | bestanden; nur erwartete LF/CRLF-Konvertierungshinweise von Git |
| Strategy-Parameter | unverändert |
| Produktive DB | Größe und `mtime_ns` vor/nach den Smokes identisch |

Download-freie Smokes auf dem lokalen Zeitraum 2026-08-06 bis 2026-08-12:

- Daily: eine Position (`PTC`), Entry 2026-08-07, Exit 2026-08-12,
  `end_of_backtest`, Portfolio Return -0.00042124182789848863.
- Score-Variant-Compare: A zwei Positionen/-0.01052037621001456; B und C je eine
  Position/-0.00042124182789848863; keine übersprungenen Varianten.
- Strategy F: drei Positionen, vier Execution Legs (`PTC`, `RMD`, `FSLR`), alle finalen Exits
  `atr_trailing_stop`, Portfolio Return 0.0016067312996526084.
- Candidate Audit: vier Sessions, fünf Candidate Events, Candidate-Symbole `FSLR`, `PTC`, `RMD`,
  Entry-Symbol `PTC`, Klassifikation `C - Mixed`; Laufzeit 50.419 s.

Die bereitgestellten Comparison-, Position-, Execution-Leg- und Post-Exit-Referenzdateien wurden
nicht überschrieben; ihre SHA-256-Hashes stimmen mit dem vor Phase 2 gesicherten Snapshot überein.
Die beabsichtigten Behaviour-Änderungen sind ausschließlich die oben dokumentierten Correctness-
Fixes. Es gab keine unbeabsichtigte oder unerklärte Trading-Behavior-Änderung.

## Deferred Findings

Phase 2 ändert bewusst nicht automatisch:

- TRW-004 (Intraday-Gap-Manifest benötigt Provider-/Halt-Semantik),
- TRW-010 (breiter Screen-Performance-Refactor benötigt Realmessung),
- TRW-011 (Config-Striktheit/Defaults benötigen Compatibility-Entscheidung),
- TRW-012 (voll belastetes Risk Sizing wäre eine Trading-Semantikänderung),
- TRW-013 (Streaming-Audit wäre ein größerer Report-Refactor),
- TRW-014 (Accounting-Kohärenzregel fachlich nicht eindeutig),
- TRW-015 (breite Read-API-/First-Run-Änderung),
- TRW-016 (neue Report-Stage benötigt Schemaentscheidung),
- TRW-018 (keine Schemaoperation an produktiver 5.49-GB-DB),
- TRW-020 (kein Dependency-Upgrade ohne geplanten Anlass),
- Entfernung kompatibler Reports/Felder aus TRW-021,
- Reparatur möglicherweise bereits vor dem Audit verworfener XBRL-Kontexte oder falsch als
  zusammenhängend gespeicherter Sync-Coverage; dies würde einen ausdrücklich autorisierten
  Daten-Resync bzw. eine Datenmigration erfordern.

## Known Risks

- **Survivorship Bias:** Historische Runs starten vom aktuellen Alpaca/SEC-Universe; delistete oder
  historisch nicht mehr handelbare Titel sind nicht vollständig rekonstruiert.
- **Daily OHLC ambiguity:** Stop-vor-Profit ist konservativ, kann aber die echte Intraday-Reihenfolge
  eines Daily-Bars nicht kennen.
- **Intraday coverage:** Halts/No-Trade versus echte Speichergaps sind nicht vollständig
  unterscheidbar (TRW-004).
- **PIT fundamental coverage:** Parser-Fix verhindert künftigen Kontextverlust, rekonstruiert aber
  ohne neuen SEC-Sync keine möglicherweise früher verworfenen Rohkontexte.
- **Accounting period coherence:** TRW-014 bleibt offen.
- **Database integrity:** Der vollständige `quick_check` der 5.49-GB-Datei wurde aus Zeitgründen
  nicht abgeschlossen; normale read-only Queries und alle Tests waren fehlerfrei.
