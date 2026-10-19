# Strategy Portfolio Audit — 19.10.2026

**F/configured/C1 bleibt der stärkste vorhandene Kandidat für den nächsten experimentellen Schritt. Die nächste Arbeit darf Paper-Trading-Vorbereitung und Shadow Execution sein. Ein operativer Paper Trader ist noch nicht freigegeben.** Das Urteil beruht auf positiven vorhandenen F-Portfoliopfaden, negativen abgeschlossenen Alternativvalidierungen und der bereits definierten C1-Allokation. Es beruht ausdrücklich nicht auf dem Registry-Label CHAMPION. Kostenstress, Exact LOSO und echtes Post-Freeze-Forward sind im lokalen Bestand nicht nachgewiesen.

## Prüfstand, Grenzen und Provenienz

Verwendeter Source-Commit: **`da2370e1be7c262bda5c4342926ace394d94c517`**. Remote-`main` wurde am 19.10.2026 über `GET https://api.github.com/repos/Kanomble/TraWalp/branches/main` geprüft; lokales HEAD stimmt überein. Der SSH-Leseversuch war durch die Sandbox blockiert; die GitHub-Abfrage war erfolgreich. Die bereits gestagte `.codex/config.toml` wurde nicht verändert. Keine Source-/Config-/Registry-Änderung, kein Commit/Push und keine Änderung historischer Reports.

Scope: lokales Lesen von Source, Dokumentation und vorhandenen JSON-/CSV-Artefakten. **Keine Backtests, historischen Preflights, Candidate Discovery, Strategieoptimierung, Provider-Abfragen oder Datenbankinitialisierung.** Auch das Feasibility-Kommando wurde nicht ausgeführt. Die Marktdatenbank wurde nicht geöffnet. Einige vorhandene Reports erforderten wegen Windows-Leserechten einen genehmigten Lesezugriff außerhalb des Sandbox-Benutzers. Das änderte den fachlichen Read-only-Scope nicht.

Die Evidenz umfasst auch lokal vorhandene, von Git ignorierte Reports. Source-Commit und Report-Herkunft sind deshalb getrennte Größen: Dieser Commit beweist nicht, dass ein alter Report mit genau diesem Code erzeugt wurde. `strategy_portfolio_input_manifest.json` hält SHA-256, Größe und Pfad der tatsächlich verwendeten lokalen Dateien fest. Es kopiert keine Rohdaten. `strategy_portfolio_evidence.csv` enthält Quellen und konkrete Selektoren; fehlende erwartete Artefakte werden dort einzeln als `MISSING_ARTIFACT` erfasst.

Verwendete Evidenzhierarchie:

1. Echtes zeitliches Forward/Holdout nach nachweislicher Festlegung der unveränderten Strategie.
2. Vorab eingefrorene, unabhängige Validation; formale Zeit-/Hypothesenunabhängigkeit gesondert prüfen.
3. Robustness-, Exact-LOSO-, Regime- und tatsächlich ausgeführte Kostentests.
4. Execution-/Datenqualifikation und kontrollierte Mechanismusvergleiche.
5. Development-Backtests, auch wenn sie später berechnet wurden.
6. Deskriptive Attribution und post-hoc Konzentration.
7. Explorative/isolierte Research-Ergebnisse und reine Code-Fähigkeiten.

Ein implementierter Runner oder eine Anleitung ist kein ausgeführtes Ergebnis. Ein eingefrorener Independent-V1-Contract macht bekannte 2024–2026-Daten nicht nachträglich zu echtem zeitlichem Forward. Fehlende Werte heißen `NOT_AVAILABLE`, fehlende Ergebnisgrundlage `EVIDENCE_MISSING`. Kein numerischer Gesamtscore wird gebildet.

### Quellenkürzel

Alle folgenden Pfade sind relativ zur Repository-Wurzel; der Evidence-Index und das Input-Manifest lösen sie vollständig auf.

| Kürzel | Lokale Quelle |
| --- | --- |
| R1 | `reports/champion_f_recheck_2024-01-02_2026-08-12_v1.json` plus Positions-/Equity-Dateien |
| R2 | `reports/f_champion_edge_decomposition_2024-01-02_2026-08-12_v1_summary.json`, `_symbol_concentration.csv`, `_market_regime_attribution.csv`, `_market_beta.csv` |
| R3 | `reports/f_intraday_entry_validation_2022-03-09_2024-08-12_v1_summary.json`, `_cost_stress.csv`, `_monthly.csv`, `_yearly.csv`, `_chronological_subperiods.csv` |
| R4 | `reports/f_intraday_risk_validation_2022-03-09_2024-08-12_v1_summary.json` |
| R5 | `reports/f_l5_forward_2024-08-13_2026-08-12_v1_summary.json`, `_monthly.csv`, `_yearly.csv`, `_chronological_subperiods.csv` |
| R6 | `reports/orb_v1_us_equity_validation_2024-01-02_2026-08-12_v1_summary.json` |
| R7 | `reports/intraday_reversal_v1_validation_2024-01-02_2026-08-12_v1_summary.json` |
| R8 | `reports/market_intraday_momentum_v1_validation_2024-01-02_2026-08-12_v1_summary.json` |
| R9 | `reports/pairs_stat_arb_v1_2024-01-02_2026-08-12_v1_summary.json` |
| R10 | `reports/pairs_stat_arb_v1_2024-01-02_2026-08-12_v1_diagnostic_diagnostic_summary.json` |
| R11 | `reports/pairs_v2_feasibility_2024-01-02_2026-08-12_v1_summary.json` und `docs/pairs-v2-feasibility.md` |
| R12 | `reports/all_comparison_2026-01-01_2026-08-20.json` |
| D1 | `reports/champion_consolidation_audit.md`, besonders CC-01–03 und Abschnitte 7–8 |
| D2 | `docs/extended-strategy-validation.md` |
| D3 | `docs/f-configured-robustness-validation.md` |
| D4 | `docs/f-configured-capacity-research.md`, `docs/f-regime-capacity-research.md`, `docs/f-lifecycle-v2-research.md` |
| D5 | `docs/strategy-research-lifecycle.md`, `docs/research-code-audit.md`, `README.md` |
| D6 | `docs/orb-v1-research.md`, `docs/intraday-reversal-v1-research.md`, `docs/market-intraday-momentum-v1-research.md` |
| D7 | `docs/pairs-stat-arb-v1-research.md`, `docs/pairs-stat-arb-v1-diagnostic-audit.md`, `docs/pairs-stat-arb-v1-identity-epochs.md`, `docs/security-reference.md` |
| D8 | `docs/post-audit-baseline-revalidation.md`, `docs/repository-audit.md` |
| C1 | `src/trading_system/backtest/research_registry.py`, `research_definitions.py`, `screen_strategies.py`, `models/backtest.py` |
| C2 | `src/trading_system/champion.py`, `backtest/engine.py`, `backtest/position_manager.py`, `backtest/metrics.py`, `config/strategy.yaml` |
| C3 | `src/trading_system/data/database.py`, `backtest/universe_provenance.py`, `backtest/validation.py`, `strategy/screener.py` |
| C4 | `src/trading_system/cli.py`, `tests/test_champion_consolidation.py` (nur Source-Inspektion) |

## A. Vollständiger Bestand und echte Kandidaten

`strategy_portfolio_inventory.csv` inventarisiert jede aktuelle Registry-Familie, alle Legacy-Research-IDs, die sechs Daily-Screens, isolierte F-Familien, vier Independent-Familien und das außerhalb der Family-Statusmap liegende Pairs-V2-Feasibility-Research. CLI, Validation-Module, Dokumentation und lokale Report-Verfügbarkeit wurden zusätzlich gegengeprüft. Datenqualifikations-/Identity-/Performance-Audits sind Infrastruktur-Evidenz und keine zusätzlichen Alpha-Kandidaten. [C1, C4, D5, D7]

| Wirtschaftliche Einheit | Stand / Behandlung |
| --- | --- |
| **F/configured/C1** / `SCREEN-F` | Einziger aktuell fortsetzbarer Primary Candidate; eingefrorene Daily-Auswahl und C1-Verwaltung. |
| ORB V1 | Vollständig historisch validiert; `REJECTED`, kein fortsetzbarer Kandidat. |
| Pairs Stat Arb V1 | Vollständig historisch validiert und diagnostiziert; wirtschaftlich `RESEARCH_REJECTED / FROZEN`, trotz aktuellem Code-`ACTIVE`. |
| Intraday Reversal V1 | Vollständig historisch validiert; `REJECTED`. |
| Market Intraday Momentum V1 | Vollständig historisch validiert; `REJECTED`. |
| D/configured, E/configured | Eigenständige Daily-Auswahlhypothesen existieren im Source. Untergeordnete Lifecycle-Rolle `ACTIVE_RESEARCH`, Family `screen-strategy-research` aber `ARCHIVED`. Passende wirtschaftliche Ergebnisartefakte fehlen: `EVIDENCE_MISSING`; keine Promotion. |
| A/B/C configured | Historische/Legacy-Auswahlkontrollen; aktuelle Family `ARCHIVED`, keine aktuelle Kandidatenfreigabe. R12 enthält nur A/B/C und C-Managementvarianten, keinen D/E/F-Vergleich. |
| F0 = C/intraday-dynamic | Relevanter archivierter historischer Kontrollkandidat; separat unten gewürdigt. Nicht mit dem Daily-F gleichsetzen. |
| F1–F5, F-intraday, F-first-hour-entry, D1–D5, alte Managementpresets | `ARCHIVED / NOT_CURRENT_CANDIDATE`; technische Entry-/Management-/Legacy-Experimente. Keine künstlichen Rangpositionen. |
| F-Capacity, Regime-Capacity, Lifecycle L0–L6, I0/I1, R0/R1, L5-Holdout, Edge Decomposition | Evidenz über F. Keine wirtschaftlich unabhängigen Champions. |
| Pairs V2 Feasibility | Diagnostische Beziehungsprüfung ohne Trading-/PnL-Simulation; abgelehnt/eingefroren, kein zusätzlicher Strategy Candidate. |

### Kompakte Evidence Cards

Die vollständigen maschinenlesbaren Cards einschließlich aller geforderten Felder stehen in `strategy_portfolio_audit_summary.json`. Die folgende Tabelle zeigt die kompatiblen Headline-Größen. Independent-Returns sind gleichgewichtete Einzelbeobachtungen ohne Kapitalallokator; F-Return/CAGR/Drawdown sind Portfoliogrößen. Diese Größen sind **`NOT_DIRECTLY_COMPARABLE`**. PF/Expectancy werden jeweils mit ihrer eigenen Einheit interpretiert. [R1, R6–R9, C2]

| Strategie / Status | Stichprobe / unabhängige Validation / echtes Forward | Trades | Return bzw. Netto-Expectancy | PF | DD | Hit Rate |
| --- | --- | ---: | --- | ---: | --- | ---: |
| F/configured/C1 — CHAMPION, auditierter Favorit | Development 02.01.2024–12.08.2026; unabhängige F-Validation `NOT_AVAILABLE`; echtes Forward `NOT_AVAILABLE` | 91 | Portfolio +40,8175 %; CAGR 14,0183 %; mittlere Positionsrendite +2,4434 % | 1,9620 | −7,0929 % | 57,1429 % |
| ORB V1 — REJECTED | Frozen hypothesis, abgeschlossene historische Validation 02.01.2024–12.08.2026; kein Post-Freeze-Forward | 35.331 | −0,07952 % je Signal-Trade; brutto +0,02045 % | 0,8323 | NOT_AVAILABLE | 41,9688 % |
| Pairs V1 — RESEARCH_REJECTED / FROZEN | Frozen hypothesis, abgeschlossene historische Validation gleicher Zeitraum; kein Forward | 1.991 | −0,008484 % je Paar-Trade auf Gross-Notional; brutto +0,09203 % | 0,9881 | NOT_AVAILABLE | 51,3310 % |
| Intraday Reversal V1 — REJECTED | Frozen hypothesis, abgeschlossene historische Validation gleicher Zeitraum; kein Forward | 5.790 | −0,09480 % je Signal-Trade; brutto +0,005152 % | 0,8836 | NOT_AVAILABLE | 48,1174 % |
| Market Intraday Momentum V1 — REJECTED | Frozen hypothesis, abgeschlossene historische Validation gleicher Zeitraum; kein Forward | 648 | −0,10420 % je Session-Trade; brutto −0,004204 % | 0,2646 | NOT_AVAILABLE | 24,8457 % |
| D/configured; E/configured — ARCHIVED family | Development-Zeitraum und Ergebnis `NOT_AVAILABLE`; keine vollständige Validation gefunden | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE | NOT_AVAILABLE |

**F-Card:** Hypothese: fundamental hochwertige, relativ günstig bewertete Aktien mit anhaltender Kursstärke nahe dem 52-Wochen-Hoch. Quality/Value-Ranking und Momentum-Gates sind fest definiert. Development umfasst auch die früher ausgewerteten 2022–2026-Daten; R1 ist der aktuelle vollständige Recheck für 2024–2026. Stabilität ist positiv, aber uneinheitlich; Konzentration materiell, Exact LOSO unbekannt; Kostensensitivität oberhalb 5 bps unbekannt. Execution ist ein Daily-OHLC-Modell, Data/PIT nur teilweise kontrolliert. Bekannte Biases: heutiges Universe/SIC, historische Datenrevisionen und Filing-Tagesauflösung, mehrfach verwendete Entwicklungsdaten, serielle Re-Entries und C1-Pfadabhängigkeit. Operative Blocker: Daily-Gap-Policy, persistenter eingefrorener Entscheidungszustand und noch undefinierte Broker-/Order-Übersetzung. [R1–R5, C1–C3, D1]

**Independent-Cards:** Für alle vier fehlen portfoliofähige Allokation, echter Forward-Nachweis, Exact LOSO und eigenständige zusätzliche Kostenszenarien (`NOT_AVAILABLE`). ORB und Reversal haben breite Symbolbeobachtungen, aber alle Jahres-/Drittelslices netto negativ; tausende korrelierte Signale sind keine tausenden unabhängigen Experimente. Momentum hat nur SPY, Long und Short beide netto negativ; Borrow und deutsche/EU-Ausführbarkeit sind nicht geprüft. Pairs ist paarweise dollar-, nicht beta-neutral; Borrow/Short-Verfügbarkeit fehlen. Daten sind lokales IEX/adjustment=all mit dokumentierten Qualifikations- und Current-Universe-Grenzen. Der jeweilige negative Edge ist bereits ein wirtschaftlicher Ausschluss; eine neue Portfolioarchitektur würde ihn nicht belegen. [R6–R10, D6–D7, C1]

**Archivierter Gegenbeleg zugunsten F0:** D2 dokumentiert für C/intraday-dynamic ein damals als OOS bezeichnetes 01.05.2025–30.04.2026: 251 Sessions, 81 Positionen, +3,093 %, PF 1,478, DD −1,802 %, Trefferquote 37,04 %. Bei 10 bps noch +0,503 %, bei 15 bps −1,582 %, bei 5+5 bps +0,346 %. EAT lieferte 49,34 % des Netto-PnL; Top-3 114,74 %. Der Post-hoc-LOSO ohne EAT blieb +1,567 %, ohne Top-2 +0,511 %; das waren keine Portfolio-Reruns. R12 liefert zusätzlich für 02.01.–19.08.2026 +0,2393 %, PF 1,1643 bei 18 Trades. Das ist ernstzunehmende historische Evidenz und verhindert ein bloßes Label-Urteil zugunsten F. F0 besitzt gegenüber F den dokumentierten Vorteil einer früher als unabhängig eingestuften Validation. Jedoch fehlen die in D2 verlinkten Rohartefakte unter `reports/old_reports/`, die Probe ist bekannt/entwicklungskontaminiert für heutige Hypothesen und F0 ist ausdrücklich archiviert. Der dokumentierte F0-Edge kippt bei 15 bps; für F ist mangels Stressartefakten keine entsprechende Kostenüberlegenheit nachgewiesen. Aus dem lokalen Bestand ergibt sich damit kein belegter Replacement-Kandidat mit mindestens vergleichbar überprüfbarer aktueller Evidenz. [D2, R12, C1, D5]

## B/C. Primary Candidate und Rangfolge

**1. F/configured/C1.** Positive, mit aktuellem Recheck und Attribution übereinstimmende Entwicklungsevidenz; positive unveränderte Daily-Kontrollen in zwei weiteren historischen Fenstern; konkrete C1-Portfoliodefinition. Abgelehnte Erweiterungen lassen die Baseline bestehen. Diese Gründe reichen für den nächsten kontrollierten Beobachtungsschritt, nicht für einen Profitabilitätsbeweis. [R1–R5, C1–C2]

**Keine Rangplätze 2–N für aktuell fortsetzbare Kandidaten.** ORB/Reversal/Momentum sind aufgrund negativer abgeschlossener Netto-Validierungen ausgeschlossen; Pairs V1/V2 eingefroren. D/E fehlen Ergebnisse. F0/A/B/C sind historische Kontrollen. Ein Ranking dieser ausgeschlossenen Reihen nach inkompatiblen Kennzahlen würde falsche Vergleichbarkeit erzeugen. Insbesondere ist Pairs' nahezu flache Paar-Expectancy kein zweitbestes kapitalisiertes Portfolio. [R6–R12, D2, C1]

## D. Development, Validation und Forward

| F-Evidenz | Zeitraum / Umfang | Zulässige Einordnung |
| --- | --- | --- |
| R1 aktueller Recheck, erzeugt 16.09.2026 | 02.01.2024–12.08.2026; 655 Sessions, 91 Trades | DEVELOPMENT; spätes Berechnungsdatum schafft kein Forward. |
| R3/R4 unveränderte Full-Kontrolle | 09.03.2022–12.08.2024; 610 Sessions laut Monatsdatei, 67 Positionen; +21,7032 %, PF 1,8310 | Historische Development-/Mechanismusvalidation; R3 und R4 sind dieselbe Baseline, keine zwei unabhängigen Erfolge. |
| R5 L0_FORWARD gegen L5 | 13.08.2024–12.08.2026; 501 Sessions laut Monatsdatei; L0 71 Trades, +20,8400 %, PF 1,6738, DD −7,0929 %; L5 56 Trades, +8,6600 % | Artefakt nennt `FORWARD HOLDOUT / RESEARCH`, aber `clean_oos=false`; liegt vollständig vor/auf aktuellem F-Cutoff. Nützlich gegen L5, **kein echtes Forward für heutigen F**. |
| R2 Candidate-forward-/Tail-Diagnostik | Signale bis 12.08.2026, diagnostische Outcome-Tail bis angefordert 10.09.2026 | Unkostierte ex-post Candidate-Horizonte; kein eingefrorenes handelbares Post-Freeze-Portfolio. |
| Champion-F-Forward nach 12.08.2026 | Erwarteter Start 13.08.2026; Enddatum NOT_AVAILABLE | **EVIDENCE_MISSING**. Kein qualifizierender Report gefunden. |

Für echtes F-Forward gilt daher: `forward_start=NOT_AVAILABLE`, `forward_end=NOT_AVAILABLE`, `number_of_sessions=NOT_AVAILABLE`, `number_of_trades=NOT_AVAILABLE`, `performance=NOT_AVAILABLE`, `cost_assumptions=NOT_AVAILABLE`, `frozen_before_period=NOT_AVAILABLE`. Die Zahl gefundener qualifizierender Artefakte ist **0**; daraus folgt weder „0 Marktgelegenheiten“ noch „0 ausgeführte Trades“. Der Code verlangt für `champion-f-forward` einen Start nach dem 12.08.2026, aber seine Existenz beweist keinen Lauf. [R1–R5, D3, C1, C3]

## E. Kosten und Abstand zum Break-even

**Ja: Der vorhandene F-Edge ist nach den modellierten Baseline-Kosten positiv.** R1 enthält 5 bps adverse Slippage je Fill, null Commission, +40,8175 % Portfolio-Return und +40,817498 Kapital-Einheiten Netto-PnL bei Startkapital 100. Expectancy ist **+0,44854394 Kapital-Einheiten pro Trade**, nicht eine Signalrendite; durchschnittliche Positionsrendite separat +2,4434167 %. PF 1,962 liegt 0,962 oberhalb 1. Slippage-Kosten sind 1,946215 Kapital-Einheiten, Commissions 0. Diese Größen beschreiben das vorhandene Modell. [R1 `metrics`, `position_metrics`; C2 `calculate_metrics`]

Ein belastbarer maximal tragbarer Slippage-/Spread-/Commission-Satz, also ein **Kosten-Break-even in bps, ist `NOT_AVAILABLE`**. Keine lineare Hochrechnung aus dem Netto-PnL: Kosten ändern Sizing, Slotbelegung und Folgepfade. Es gibt keinen lokalen 2×-/3×-/Commission-Stress-Report für den aktuellen F-Pfad und kein passendes path-preserving Stressartefakt. D3 definiert 10/15/5+5-bps-Szenarien als Code-/Run-Contract, nicht als bestandene Resultate. R3 `_cost_stress.csv` enthält trotz des Namens **nur drei BASELINE-Zeilen** für I0_FULL/I0_COMMON_SUPPORT/I1_COMMON_SUPPORT. Das ist kein 2×-/3×-Nachweis. [D3, R3, C3]

Bewertung `Cost Robustness=UNKNOWN`; Baseline-Netto-Edge positiv ist davon getrennt. Kostenrobustheit von F0 darf nicht auf Daily-F übertragen werden. Das begrenzt historische Zuversicht, verhindert aber nicht, gerade Feed-/Slippageabweichungen im Shadow-/Paper-Vorhaben zu messen.

## F. Breite, zeitliche Stabilität und Konzentration

**Zeit:** R3 I0_FULL: 2022 partiell −4,5084 %, 2023 +9,7745 %, 2024 bis 12.08. +16,1008 %. Die drei chronologischen Drittel sind −4,7460 / +8,7035 / +17,5371 %. Von 30 Monatszeilen sind 15 positiv, 14 negativ, eine null; bester Monat 05/2023 +5,8282 %, schwächster 06/2022 −2,7531 %. R5 L0: 2024 ab 13.08. +1,4085 %, 2025 +8,2299 %, 2026 bis 12.08. +10,1005 %; Drittel −0,4730 / +11,8525 / +8,5486 %. 16 von 25 Monatszeilen positiv, neun negativ; bester 02/2026 +4,2826 %, schwächster partieller 08/2026 −1,8171 %. Monatszählungen sind ausschließlich Zusammenfassungen vorhandener Tabellen. Die Fenster haben getrennte Portfolio-Neustarts; sie werden weder zusammengesetzt noch als unabhängige Stichproben gezählt. Jahres-/Monats-/Dritteldateien des erwarteten vollständigen R1-Robustness-Bundles fehlen. [R3, R5, D3]

**Symbole:** R2 hat 18 gehandelte Symbole. NVDA liefert 16,161213 von 40,817498 Kapital-Einheiten, also **39,59 % Netto-PnL**, mit 21/91 Trades. Top-5 (NVDA, FTI, TT, STX, MU) liefern **89,15 %** des Netto-PnL. Top-10 liegen bei 121,52 %, weil andere Symbole Verluste beitragen. 73 Re-Entries und 43 Re-Entries ohne frischen Trigger in R1 begrenzen die Unabhängigkeit der 91 Trades. Es gibt keinen lokal vorhandenen Exact-LOSO-Rerun; Attribution ist kein Beweis für Allokationsrobustheit. [R1 `position_metrics`; R2 `evidence`, Symboltabelle]

**Regimes:** 83/91 Trades und 38,695723 Kapital-Einheiten PnL bei SPY oberhalb SMA200; nur acht Trades und 2,121775 PnL darunter/gleich. 81 Trades mit SPY-Drawdown 0–5 % liefern 39,299669 PnL. Volatilitätsquintil Q4 ist leicht negativ (−0,102452; PF 0,9923); übrige Quintile positiv. Trade-Beta gegen SPY ca. 1,0385 ist eine deskriptive Regression über unterschiedliche Haltedauern, kein Portfolio-Alpha-Nachweis. Positive schwächere Regimes beruhen auf sehr kleinen Samples; Current-SIC ist nicht historisch PIT. [R2 Regime-/Beta-Dateien]

Urteil: **mehr als ein einzelner Gewinnmonat, aber materiell symbol- und bullmarktlastig**. `Temporal Stability=MODERATE`, `Cross-sectional Robustness=WEAK`, `Concentration Risk=WEAK`, `Market/Regime Dependence=WEAK`. Schwache Ratings bedeuten begrenzte günstige Evidenz, keine automatische Ablehnung des Basiskandidaten.

### Was die abgelehnten F-Experimente tatsächlich sagen

| Familie | Test / Entscheidung / Information über die Baseline |
| --- | --- |
| Capacity 1/2/3/5 | Höhere Slotzahl bei gleicher F-Auswahl getestet, >1 laut aktueller Entscheidungsmap/D1 abgelehnt. Ergebnisbundle fehlt lokal. Akzeptiert wird nur die dokumentierte Entscheidung, keine quantitative Behauptung zur Capacity-Robustheit. C1 bleibt Control. |
| Regime-Capacity | SMA200 bzw. SMA200+MOM126 schalten C1/C5; keine Regime-Exits. Abgelehnt laut Map/D1, quantitatives Bundle fehlt. Keine Aussage, dass F in jedem Regime robust sei. |
| Lifecycle L0–L6 | Längere/bedingte/peerbestätigte Holds und Profit-Deferral; abgelehnt laut Map/D1. Vollständiger Entwicklungsreport fehlt. Keine nachträgliche Verlängerung der Baseline. |
| L5 Research-Holdout | L0 +20,8400 % versus L5 +8,6600 %, Delta −12,1800 Prozentpunkte bei geringfügig besserem L5-DD. Hauptdelta ist einzigartiger Folgepfad (−12,321803 Kapital-Einheiten), nicht direkter Shared-Entry-Effekt (+0,141798). L5 abgelehnt; Daily-10-Session-Baseline blieb vorzuziehen. Kein echter Post-Freeze-Forward. |
| I1 Opening-Weakness-Veto | Auf Common Support I0 +20,9386 %, PF 1,8789, 63 Positionen versus I1 +15,4902 %, PF 1,6013, 62. Delta −5,4485 Prozentpunkte. Abgelehnt; weder Ablehnung F noch Nachweis eines Intraday-Execution-Systems. |
| R1 Intraday-Risk | Zwei abgeschlossene native 15m-Closes unter Entry−0,5R, Exit am nächsten nativen Open. Common Support R0 +12,0381 %, PF 1,6346 versus R1 +6,5596 %, PF 1,3420 bei jeweils 47 Positionen. 22 ausgeführte Risk-Exits. Abgelehnt; Daily Baseline vorzuziehen. 20 Full-Baseline-Positionen nicht im Support: Coverage-Sample ist ex post, kein handelbarer Filter. |
| Edge Decomposition | ACTIVE, diagnostisch; erklärt Rank/Target/Konzentration. Kein Kandidat, keine unabhängige Validation, keine neue Regel. |

Quellen: C1/D1/D4 für Decisions; R3–R5 für kontrollierte Resultate; R2 für Attribution. Fehlende historische Ergebnisdateien bleiben ausdrücklich `MISSING_ARTIFACT`.

## G. Daten- und Methodikrisiken des Gewinners

| Bereich | Kontrolle | Bedeutung / verbleibendes Risiko |
| --- | --- | --- |
| Auswahl-/Preis-Lookahead | **already controlled** auf geprüftem Code-Contract | Champion-Screen ohne Market Snapshots; historische Features bis abgeschlossener Signal-Session, Entry danach. D1 beschreibt vorhandene Future-Bar-/Filing-Prefix-Tests; hier nicht neu ausgeführt. Kein nachgewiesener neuer Lookahead-Bug. |
| Fundamentals PIT | **partially controlled** | `filed <= as_of`, nicht bloß `period_end`; schützt vor späteren Filings. Tagesauflösung beweist aber keine SEC-Verfügbarkeit vor dem Signal-Close bei same-date Filings. Snapshot-/Accession-Provenienz vorhanden, historisches Raw-Coverage-/Accounting-Kontextproblem aus D8 für heutige F-Daten nicht vollständig neu zertifiziert. Historische Zählungen aus D8 sind keine aktuellen DB-Zählungen. |
| Current universe / Survivorship | **still open** | Aktuelle tradable companies statt historischem vollständigem Universe; `CURRENT_UNIVERSE_ONLY`, `NOT_SURVIVORSHIP_CLEAN`. Keine CRSP-Qualität behaupten. Für prospektives Shadow Logging kein zwingender Vorab-Blocker. |
| Current classification / SIC / Peers | **partially controlled** | PIT-Preis-/Fundamentalwerte, aber heutige SIC-/Company-Zuordnung; historische Sektor-/Peer-Auswahl kann verzerrt sein. |
| Identity lifecycle / Delistings / Symbolwechsel | **partially controlled** | Konflikte werden konservativ ausgeschlossen; R1 meldet 18 ungelöste Konflikte. Später entstandene Pairs-Epoch-/Quarantänefähigkeit ist kein Beweis, dass R1 dieselbe run-globale Identitätsqualifikation nutzte. Keine automatische Übertragung der Pairs-Gates auf F. Delisting-Returns und autoritative historische Tickerketten für F nicht vollständig nachgewiesen. |
| Daily-Datenqualifikation | **partially controlled**, operativ offener Blocker | R1 hat 655 repräsentierte Sessions; das ist keine Held-Path-Vollständigkeitsbescheinigung. Engine lässt ohne Strict-Option fehlende Held-Bars aus, gesamthaft lokal fehlende Sessions verschwinden aus dem Kalender; stale Endliquidation möglich. R3/R5 qualifizierte Subfenster ersetzen keine R1-Komplettqualifikation. [CC-01] |
| Corporate actions | **partially controlled** | Provider-adjusted `all`, keine zweite Splitanpassung. Kein historischer Adjustment-Versionsstore und kein Positions-/Cash-Eventledger; Raw-Brokerpreise müssen mit Signalpreisen konsistent übersetzt werden. Dividendenertrag/Total-Return-Behandlung nicht als unabhängig zertifiziert darstellen. |
| Benchmark | **partially controlled** | Lokaler SPY-Buy-and-hold-Vergleich vorhanden. Unterschiedliche Exposure/Signalhaltedauer; keine Behauptung risikoadjustierten Alphas oder Renditegleichwertigkeit. Trade-Beta benutzt SPY-Exit-Session-Close als Proxy für intrabar F-Exit. |
| Auswahl-/Multiple-Research-Bias | **still open** | 2022–2026 wiederholt für Hypothesen verwendet; kein echter neuer Holdout. Prospektiv einfrieren, nicht rückwirkend umetikettieren. |

Quellen: R1/R2, C2/C3, D1/D3/D7/D8. Die Classification-/Identity-Infrastruktur ist vorhanden; ihr bloßes Vorhandensein heilt historische F-Reports nicht.

## H. Execution Realism und Brokerdifferenz

| Annahme | Modell / Bewertung |
| --- | --- |
| Entry | Nach abgeschlossener Signal-Session zum nächsten **lokal repräsentierten** Portfolio-Open; bei vollständigen Daten nächstes XNYS-Open. Keine L5/I1/R1-/First-Hour-Verzögerung. Reales Auction-/Market-Order-Verhalten und der im Simulator zum Sizing verfügbare tatsächliche Openpreis müssen prospektiv auf konkrete Ordersemantik abgebildet werden. |
| Exit | Bestehender Gap-Stop am Open vor Opening-Target; Opening-Target vor späterem Tages-Low. Innerhalb ungelöster Daily-Range Stop vor Target. Target vor Hard-Hold-Close; Entry zählt als Session 1, Hard-Hold=10. Keine sichere Intraday-Ereignisreihenfolge aus OHLC. |
| Gap/Stop | Overnight-Gaps durch tatsächliches Open berücksichtigt; Stop ist keine Verlustgarantie. R1 schlechtester Trade −12,9022 %, trotz nominal maximal 10 % Stopdistanz. Intrabar-Stop-Fill am Stop plus 5 bps bleibt idealisiert. |
| Slippage/Commission | 5 bps je Fill / 0 bps. Spread nicht separat, keine Queue, Teilfills, Market Impact oder nichtlineare Liquiditätskosten. |
| Liquidität | Mindestpreis $5, Mindestcap $1 Mrd., ADV20 $10 Mio. und History-Gates; das sind Eligibility-Filter, kein Nachweis von Opening-/Stop-Ausführbarkeit oder Capacity. |
| Fractionals/Kapital | Simulierte kontinuierliche Stückzahl und sofortige Vollausführung; Brokerpräzision, Mindestorder, Mindestcommission und Fractional-Eligibility offen. Startkapital 100 ist eine normalisierte Research-Rechnung, keine operativ festgelegte Kontogröße. |
| Short/Borrow | **not relevant** für Long-only-F. Kein künstlicher Short-/Borrow-Blocker. |
| Datenlücken | CC-01 muss als ausführbare Fail-closed-/Reconciliation-Policy gelöst werden; ein fehlender Preis darf weder Haltedauer verschieben noch eine alte Markierung als heutigen Fill ausgeben. |
| Trade Eligibility / EU | R1-Eligibility ist Alpaca-tradable, nicht nachgewiesene Handelbarkeit beim später gewählten deutschen/EU-Broker. Tatsächlicher Zielbroker und Kontomodell **NOT_AVAILABLE**. Keine aktuelle Rechts-/Brokerzulassungsbehauptung aus diesem Offline-Audit. |

Quellen: C1/C2, R1, D1. Alpaca liefert hier **IEX-Marktdaten**, während ein tatsächlicher Trading-Broker noch nicht feststeht. Bei einem anderen Broker sind Feed-/Openpreis-/Spread-Abweichungen, Instrument-/Handelsplatzidentität, Ordertypen/OCO/Stops/MOC, Fractionals, Gebühren/FX und Corporate-Action-Verarbeitung abzugleichen. Die README nennt Alpaca Paper als Projektabsicht; das ist keine beschlossene oder implementierte reale Brokerintegration. Diese Prüfung baut in diesem Task keinen Adapter. [README, C2, D1]

## I. Portfolio ist weitgehend definiert

**Ja, die ökonomische C1-Allokation ist hinreichend eindeutig für eine Implementierungsvorbereitung.** Sie braucht keine neue Alpha-/Portfolioarchitektur. [C1/C2]

- Maximal eine Long-Position, maximal 100 % Equity-Notional, sektoraler Grenzwert 2 bei C1 praktisch nicht bindend.
- Stückzahl = Minimum aus `equity * 0.01 / stop_distance`, `equity * 1.0 / entry_fill` und `cash / (entry_fill * (1 + commission_rate))`.
- Stopdistanz = `min(2 * signal_ATR14, 10 % * entry_fill)`; Ziel = `entry_fill * 1.12`. Offenes Gap-Risiko bleibt.
- Gleichzeitige Signale nach absteigendem normalisiertem Quality-/Value-Score, Symbol aufsteigend als Tie-Break. In belegtem Slot keine Rotation in bessere Signale. Verfügbarkeit/Fehlschläge unterliegen den bestehenden Engine-Gates.
- Cash wird nicht verzinst; kein Leverage-/FX-/Settlement-/Steuer-Modell belegt. Nicht allokiertes Kapital bleibt Cash. „98,78 % Sessions mit Position“ in R2 ist **keine** Kapitalexposure: R1 durchschnittliche Notional-Exposure nur 17,51 %, End-of-Day 15,09 %.
- Re-Entry erlaubt, Cooldown null; kein neuer Trigger zwingend. R1: 73 Re-Entries, davon 43 ohne frischen Trigger. Turnover 31,5971 über das gesamte R1-Fenster, ca. 2,906 Trades pro Monat; keine Tagesrate daraus ableiten.

Noch offen sind dauerhafte Portfoliozustände und die praktische Kapital-/Broker-Rundung, nicht die Definition eines neuen Kapitalallokators. [R1, R2, C2]

## J/K. Gap Analysis und Reifestufen

`primary_candidate_gap_analysis.csv` ist die vollständige priorisierte Lückenliste. **BLOCKER** meint dort die Freigabe von messbarer Shadow-/Paper-Ausführung, nicht das Beginnen ihrer Engineering-Vorbereitung.

| Reifestufe | Urteil |
| --- | --- |
| BACKTEST-READY | Für die dokumentierte historische Semantik ja; keine pauschale Datenqualitätszertifizierung. |
| PAPER-TRADING-READY | **false**: keine durable Ausführung, CC-01 und Order-/Broker-Vertrag offen. |
| SMALL-LIVE-READY | **false**: kein hinreichendes echtes Forward, unbekannter Kosten-Break-even und unbewiesene reale Ausführung. |
| PRODUCTION-READY | **false**: kein Produktionsbetrieb, Reconciliation-/Recovery-/Execution-Nachweis. |
| PAPER-TRADING-PREPARATION | **ready**: vorhandenen F einfrieren und prospektive Beobachtung konstruieren ist der nächste sinnvolle Schritt. |

**Bereits ausreichend für diese Auswahl-/Vorbereitungsentscheidung:** eindeutige Strategieidentität und Ausführungs-Config-Guard; dokumentierte Candidate-Auswahl/Ranking; C1-Risiko-/Positionsdefinition; positiver R1-Netto-Developmentpfad mit passenden R2-PnL-/Tradezahlen; unterstützende unveränderte Daily-Kontrollen; negative abgeschlossene unabhängige Alternativen. Ausreichend bedeutet hier nicht unabhängige Alpha-Bestätigung. [R1–R9, C1/C2]

**Noch nicht ausreichend belegt:** echtes F-Forward, 2×-/3×-/Commission-Kostenrobustheit, Exact LOSO, breite Bear-Regime-Robustheit, vollständige R1-Datenprovenienz/Qualifikation. Keine dieser Prüfungen wird als erledigt bezeichnet. Historische F-Reproduzierbarkeit: **PARTIALLY_REPRODUCIBLE**. Identität/Config-Guards und R2-Config-/Runtime-/Datenfingerprints existieren; R1 hat keinen expliziten Code-Revision-/Datenfingerprint-Nachweis, Originaldaten-Snapshot und mehrere historische Bundles fehlen. R2 hat Configfingerprint `fe30248a96ff8e21b092772d9dabe84a1e648de317eb6799633d116578cf2f51` und Datenfingerprint `b6d84bc778c3c3bb34476265844d1319750adbe108b99f2931df740f333ff048`; das sind historische Provenienzangaben, keine neu verifizierte Übereinstimmung der heutigen DB. [R1/R2, D3]

**Echte Blocker vor sinnvoll auswertbarer Ausführung:**

1. **Daily-Session-/Gap-Policy fehlt.** Offizielle Sessions, Missing/Stale-Bar-Flags und Haltedauer dürfen nicht von einer zufällig lückenhaften lokalen Preisreihe abhängen. Minimal: expliziter Fail-closed-/Recovery-Vertrag für F, ohne historische Reports oder Strategieparameter umzuschreiben. [CC-01, C2]
2. **Kein dauerhafter, überprüfbar eingefrorener Decision-/Intent-/Position-Zustand.** Sonst sind Strategie-/Config-/Wissensstand und ein Neustart nicht rekonstruierbar. Minimal: versioniertes Journal mit F-Identität, Config-/Source-/Datenstand, Cutoff, offiziellem Signalzeitpunkt, C1-Position/ATR/Stop/Ziel/Hold-Counter und idempotenten Intents. [D1, C1–C3]
3. **Ausführbarer Broker-/Order-Vertrag fehlt.** Sizing benutzt im Backtest den bereits bekannten Fill-Open, Paper-Orders müssen vorher mit konkreten Größen/Ordertypen/Schutzorders geplant sein. Fractionals, Stop-/Target-Verknüpfung, Hard-Hold-Close und Reconciliation sind nicht spezifiziert. Minimal: konkretes Mapping und deterministische Contract-Fixtures einschließlich Gap, Reject, Teilfill und Restart; keine neue Entry-Regel. Für reines Signal-Shadow ohne Orders ist dieser Punkt noch kein Startblocker. [C2, D1]

**Gerade in Shadow/Paper messbar:** Stabilität der täglich eingefrorenen Signale, frische vs. verspätete Filings/Bars, fehlende Handelbarkeit, geplanter vs. tatsächlicher Entry, Stop-/Target-/Close-Reihenfolge, Feed-/Spread-/Slippageabweichungen, Gebühren, Cash-/Stückzahlrundung, Teilfills, Rejects und Restart-/Reconciliation-Verhalten. Nicht im Simulator vorhandene Execution-Details müssen vorab protokolliert und als Modellabweichung ausgewiesen werden. Fehlendes echtes Forward ist Anlass für diese prospektive Messung; kein Anlass für 20 neue Development-Backtests. Survivorship-Rekonstruktion auf akademischem Idealniveau ist kein zwingender Blocker dieses kontrollierten Paper-Experiments.

## L. Research-Statuskonsistenz und Pairs-Freeze

`strategy_portfolio_status_drift.csv` vergleicht pro Familie Registry, Definitions-Lifecycle, README/Guides und letzte vorhandene Entscheidung. Historisches `status=ACTIVE` in R6/R7/R8 ist ein unveränderlicher Vor-Entscheidungs-Snapshot, kein aktueller Codefehler: aktuelle Definitions/Registry/Guides sagen REJECTED. Legacy `ACTIVE_RESEARCH`/`CHAMPION_CONTROL` sind bewusst erhaltene Reproduktionsrollen und haben nicht Vorrang vor der Family-Statusmap. [C1, D1, D5/D6]

**Pairs V1: `CODE_STATUS_STALE_VS_RESEARCH_DECISION`.** Definition und Family-Map sagen noch ACTIVE, D7-Guide ebenfalls. R9: 1.991 Trades, brutto +0,09203 %, netto −0,008484 %, PF 0,988104. Modellierter Break-even 4,577998 bps je Fill liegt unter den verwendeten 5 bps; Borrow/Commission fehlen zusätzlich. R10 besteht A/B-Symmetrie ohne Verstöße (`SYMMETRY_PASS`); Z-Vorzeichen ist labelabhängig, kein invariant wirtschaftlicher Long-/Short-Selektor. Rolling-Z-Kontraktion ist keine robuste Konvergenz zum ursprünglichen Preisratio-Referenzniveau. Der frühere R10-Vorschlag „MOVE_TO_PAIRS_V2_MODEL“ ist durch R11 überholt, keine aktuelle Handlungsempfehlung. [R9–R11]

**Pairs V2: `PAIRS_V2_FEASIBILITY_WEAK / STOP PAIRS V2`.** Lokal verifiziert: 190.134 getestete Zustände; 10.619 mit p≤0,05; nur 326 Zustände aus 73 Paaren mit p≤0,05 im Vollfenster und beiden Hälften; relativer Betawechsel dort median ca. 51,89 %; nur 25 |z|≥2-Zustände aus 15 Paaren. Das sind abhängige Zustände, keine 25 unabhängigen Trades; keine PnL-Validierung. V2 hat keine eigene Statusmap-Zeile, aber eine eindeutige dokumentierte Stop-Entscheidung. [R11]

Beide Pairs-Zweige werden hier als **RESEARCH_REJECTED / FROZEN** behandelt. **FOLLOW_UP: align registry status with research decision**, insbesondere `PAIRS_STAT_ARB_V1.status` und die daraus abgeleitete Family-Map sowie den aktuellen V1-Guide. Keine automatische Änderung in diesem Audit. Keine Optimierung V1, kein V2-Design/Backtest, kein unmittelbarer Pairs-V3-Vorschlag.

Weitere Drift: Lifecycle-Guide nennt F3/F5/F-intraday „Active next research“, während aktuelle Family-Map/D1 sie archivieren. F-Capacity-/Regime-/Lifecycle-Guides beschreiben teils noch neue/offene Research-Runden; aktuelle Entscheidung ist REJECTED. Das ist Dokumentationsdrift, kein Grund zur Wiedereröffnung. Für deren fehlende Rohbundles ist nur die Entscheidung, nicht die quantitative Begründung überprüfbar. [D1/D4/D5, C1]

## Artefaktauswahl und Verifikation

- R1 als aktueller kompatibler F-Recheck gewählt; R2 bestätigt dieselben 91 Trades und denselben Netto-PnL. Keine Aussage, dass es ein vollständiges Robustness-Bundle sei.
- ORB-Ergebnis referenziert den **v4-US-equity-Manifest**. Die v1/v2/v3-Preflights sind frühere Qualifikationsstände, keine zusätzlichen wirtschaftlichen Ergebnisse. R6 und finaler Guide haben Vorrang vor alten ACTIVE-Snapshots.
- R3 verwendet Ergebnisvergleich auf Common Support; weder das höchste Preflight-Suffix noch der beste Subsample-Return entscheidet. R4 Coverage-Support bleibt eine Forschungsstichprobe.
- Pairs: R9 final qualifizierte Validation + R10 Diagnostic + R11 finale Feasibility-Entscheidung. Historische Preflights und Zwischen-Qualification-Reports zählen nicht als zusätzliche Sieger und überschreiben die Stop-Entscheidung nicht.
- Historische `old_reports`-Links, F-Robustness-/LOSO-/Capacity-/Regime-/Lifecycle-Bundles und echtes Champion-Forward sind im Evidence-CSV als fehlend erfasst. Kein Ersatzlauf.
- Daten-/Artefaktchecks beschränken sich auf vorhandene Werte, Quellen-Hashes, CSV-/JSON-Schema, Inventarvollständigkeit und Konsistenz. Keine neue Produktions-/Audit-Pythondatei; daher keine Full Suite, keine neuen ökonomischen Tests und kein Ruff-/Formatlauf erforderlich. `git diff --check` und explizite Whitespace-/Provenienzprüfungen für die neuen Artefakte sind im Summary dokumentiert.

## Höchstens fünf nächste Engineering-Schritte

1. **P0 — F-Daily-Gap-Vertrag:** CC-01 für offizielle Sessionzählung, fehlende Held-/Entry-Bars, stale Marks und Reconciliation festlegen; kleine synthetische Contract-Fixtures, historische Backtest-Semantik getrennt erhalten. Vor auswertbarer Shadow-Ausführung abschließen.
2. **P0 — F-C1-Freeze und dauerhaftes Shadow-Journal:** Source-/Config-/Inputfingerprints, Knowledge-Cutoff, Signalrang, ATR, C1-Sizing, Entry-Intent, Stop/Ziel/Session-10-Close und Restart-State persistieren. Vorhandene fehlende Reports nur auffinden/archivieren, nicht nachrechnen. Echtes neues Forward-Fenster ab dem tatsächlichen prospektiven Freeze festhalten.
3. **P1 — Konkreten Paper-Broker-/Order-Vertrag abbilden:** Long-US-Aktien, Kontokapital/Währung, zulässige Stückelung, Open-Order-/Sizing-Semantik, Stop+12%-Target und Session-10-Close; instrumentenbezogene Eligibility und IEX-vs.-Brokerpreis konsistent protokollieren. Noch keine Live-Freigabe.
4. **P1 — Order-/Fill-Reconciliation und Betriebsgrenzen:** Idempotente Intents, Partial/Reject/Restart-Replay, C1-/1%-Risk-Grenzen und fehlende/stale Daten als Fail-closed-Ereignisse; keine stillen Ersatzfills. Danach kontrollierte Paper-Orders im separaten Engineering-Auftrag ermöglichen.
5. **P2 — Vorab definiertes Forward-Messprotokoll:** Unveränderten F neben Modell-/Paper-Ausführung vergleichen, echte Kalender-/Tradezahlen, Kosten-/Slippageabweichungen und Missingness berichten; Reviewtermine und Fortsetzungs-/Stopkriterien vor Ergebnissichtung festlegen. Keine neue Alpha- oder Parameterstudie; Pairs bleibt eingefroren.

```text
PRIMARY_CANDIDATE:
F/configured/C1

CHAMPION_DECISION:
KEEP_F_AS_PRIMARY_CANDIDATE

PAPER_TRADING_DECISION:
READY_FOR_PAPER_TRADING_PREPARATION
```
