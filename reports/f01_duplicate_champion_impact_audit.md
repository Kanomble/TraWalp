# F01 Duplicate Fact Impact — Phase A

## Entscheidung und Evidenzniveau

**Phase A: A_CHAMPION_IMPACT. Phase B: JA, ausschließlich Design.**

Basis: HEAD `917172886eb0954446540afb2e794b3304a59af3`, Arbeitsbaum zu Beginn sauber.
Datum: 12.11.2026. Keine Produktionslogik, Strategieparameter oder reale Daten geändert.
Keine Provider-/Netzwerkzugriffe, echten Syncs, Journale oder Backtests gestartet.

Die Klassifikation beruht auf einer **synthetisch reproduzierten Änderung echter
Produktions-Fundamentalwerte und F-Scores durch identische Wiederholung** bei
interleavten Accessions. Das ist keine Behauptung einer nachgewiesenen falschen
historischen Transaktion oder Performanceverschlechterung im realen Bestand.
Die begrenzte reale Vergleichsstichprobe zeigte keine wirtschaftliche Abweichung.
Zusätzlich ist ein relevanter Ingestion-/Readiness-Blocker mit dem neuen Writer
bestätigt. Die Schlussfolgerung ist daher weder „nur Storage“ noch eine Freigabe
für eine automatische Deduplizierung.

Referenzen: `reports/full_codebase_strategy_audit.md` (F01 und Legacy-Identity-Grenze),
`reports/xbrl_instant_fact_correctness_audit.md`, aktuelle Produktionsmodule.
`docs/champion_consolidation_audit.md` existiert nicht; stattdessen wurde
`reports/champion_consolidation_audit.md` konsultiert. Archivierte Aussagen wurden
gegen den aktuellen Contract geprüft, nicht als heutige Testresultate übernommen.

## A1 — Entstehung: bewiesen vs. möglich

| Aussage | Evidenz und Grenze |
| --- | --- |
| Mechanismus bewiesen | UNIQUE enthält nullable period_start. Alte INSERT/ON-CONFLICT-Implementierung ließ mehrere NULL-start-Zeilen derselben Filing-Identität zu. Task-8-Red-Test und neue synthetische Wiederholungsproben bestätigen dies. |
| Heutige Writer | `Database.upsert_facts` und `Database.upsert_sec_company_update` delegieren an `_execute_fact_upsert`. Aktueller produktiver SEC-Caller: `DataSynchronizer._sync_sec_company`. |
| Normale SEC-Wege | `sync` (Default/Full), `sync --incremental` sowie gezieltes `sync-sec-force` können Companyfacts erneut parsen und persistieren. Incremental entscheidet anhand neuer Accessions/Retry/Initialisierung; Full erzwingt Companyfacts. Bei einem Update wird das zurückgegebene Companyfacts-Paket verarbeitet, nicht ausschließlich der einzelne neue Instant-Fact. |
| Screens/Backtests | Normaler Screener, HistoricalFeatureScreenSource und Champion-Engine lesen Facts. Sie schreiben keine XBRL-Facts und benötigen dafür keinen SEC-Client. Einzelne Maintenance-/Preflight-Pfade können Bars synchronisieren; das ist keine SEC-Fact-Ingestion. CLI-Wrapper können unabhängig davor explizit Syncs auslösen. |
| Research indirekt möglich | `sec_backfill_pilot.run_pilot` → `sec_force.execute_force` → `sync_sec_full(..., companyfacts_only=True)` ist ein expliziter Research-/Repair-Pfad mit Ingestion. Andere reine Research-Replays erzeugen dadurch nicht automatisch Facts. |
| Vorhandenes Research-Artefakt | `f_2022_sec_backfill_pilot_v1_summary.json` berichtet 15 angeforderte/15 synchronisierte Symbole und 0 Fehler. Das belegt die berichtete Workflow-Aktivität, nicht die Herkunft bestimmter F01-Zeilen; kein solcher Lauf wurde hier ausgeführt. |
| Nicht beweisbar | Welche konkreten früheren Nutzeraktionen, Kommandos oder Laufzahlen die 10.368.182 zusätzlichen Zeilen erzeugten. Duplikatmultiplikatoren sind keine verlässliche Zahl vollständiger Sync-Läufe. |

**Zeitliche Einordnung:** fundamental_facts besitzt keine ingested_at-/retrieved_at-Spalte.
Filingdatum und Accession beschreiben die Veröffentlichung, nicht den lokalen Import.
ID-Reihenfolge ermöglicht relative Reader-Reihenfolge, keine Uhrzeitrekonstruktion.
Die erhaltenen Cache-Metadaten umfassen 798 Companyfacts-Payloads (fetched_at
10.–11.08.2026) und 6.072 Submissions-Payloads (10.–11.08.2026). Cache kann ersetzt
oder bereinigt werden; diese Zeiten datieren nicht die Duplikate.
Der SEC-Dataset-State meldet Abschluss 09.11.2026 17:01:50 UTC, 2.503 geprüfte und
2.329 aktualisierte Unternehmen. Auch das ist kein Fact-genaues Importprotokoll.
Kein bestimmter historischer Lauf wird daraus als Duplikatursache behauptet.

## A2 — Vollständiger read-only Census

Reale Datenbank: `data/trading_system.sqlite3`, 10.501.169.152 Byte.
Verbindung ausschließlich URI `mode=ro`, `query_only=ON`, `BEGIN` als **Lesetransaktion**;
kein Initialize, BEGIN IMMEDIATE, VACUUM, REINDEX, ANALYZE oder Sync.
Ein sequenzieller Scan mit disk-backed SQLite-GROUP-BY vermeidet Millionen zufällige
Index-/Tabellenzugriffe. SQLite-Cache auf 32 MiB begrenzt; temporäre Sortierdateien
sind Query-Scratch, keine Datenbankkopie oder Migration. Python konsumiert die
Gruppenergebnisse als Stream und hält nur aggregierte Zähler/Issuer-/Filing-Zählungen,
keine vollständige Fact-Zeilenmaterialisierung. Reale Stichproben sind auf drei Issuer
mit höchstens 10.000 gespeicherten Facts je Issuer begrenzt.

| Kennzahl | Ergebnis |
| --- | ---: |
| Fact-Zeilen insgesamt | 18.698.090 |
| period_start IS NULL | 13.764.701 |
| period_start IS NOT NULL | 4.933.389 |
| NULL-start-Identitätsgruppen | 3.396.519 |
| Doppelte NULL-start-Identitätsgruppen | 3.238.932 |
| Zeilen in diesen Gruppen | 13.607.114 |
| Zusätzliche Zeilen gegenüber einer je Identität | 10.368.182 |
| Numerisch widersprüchliche Gruppen | **0** |
| Gruppen mit abweichenden Nicht-Key-Metadaten | **180.283** |
| Davon abweichendes Source-Symbol | **180.283** |
| Abweichende Taxonomy / Fiscal Year+Period / Form | **0 / 0 / 0** |
| Numerisch und in Nicht-Key-Metadaten gleiche Gruppen | 3.058.649 |
| Doppelte Gruppen ohne Accession | 0 |

Alle bisherigen Task-8-Zähler stimmen überein. Werte wurden exakt mit Decimal-kanonischer
Darstellung verglichen, nicht als Gleitkommazahlen. Unterschiedliche Dezimalschreibweisen
sind kein numerischer Konflikt. Keine echten Finanzwerte oder Einzelissuerkennungen
werden exportiert. Nicht-NULL-Startperioden sind durch den vorhandenen vollständigen
UNIQUE-Key geschützt; keine Schemaänderung/fehlender Unique-Index festgestellt.
„NULL-start“ ist eine Speicherklassifikation: 359 doppelte Gruppen gehören zu
Flow-Metrics mit fehlendem Start, nicht zu korrekt spezifizierten Duration-Facts.

### Issuer, Filing und Wiederholungsanzahl

- 5.461 CIKs mit Duplikaten; 1 bis 1.923 doppelte Fact-Identitäten je betroffenem CIK.
- 193.074 verschiedene betroffene `(CIK,filed,accession)`-Gruppen; je 1 bis 104 doppelte Fact-Identitäten.
- Filingjahre 2009–2026; Filingdaten sind keine Ingestionsdaten.
- Multiplikatoren 2–37. Am häufigsten 4-fach: 1.517.936 Gruppen;
  5-fach: 800.280; 3-fach: 579.750; 2-fach: 154.046.
  Vollständige Verteilung steht aggregiert in `reports/f01_impact_inventory.json`.
- `filed` und `period_end` sind identitätsintern gleich. Die IDs unterscheiden sich
  zwangsläufig und bestimmen bei Reader-Gleichständen die stabile Reihenfolge.
  Source-Symbol-Abweichungen ändern außerdem die Zugehörigkeit zum Legacy-Reader.

### Doppelte Identitäten nach XBRL-Tag

| Tag | Gruppen |
| --- | ---: |
| CashAndCashEquivalentsAtCarryingValue | 509.653 |
| CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents | 163.607 |
| AssetsCurrent | 302.503 |
| LiabilitiesCurrent | 301.563 |
| LongTermDebtCurrent | 98.879 |
| LongTermDebtNoncurrent | 131.586 |
| CommonStockSharesOutstanding | 272.463 |
| EntityCommonStockSharesOutstanding | 125.177 |
| Assets | 400.084 |
| StockholdersEquity | 594.231 |
| StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest | 164.428 |
| ShortTermBorrowings | 28.536 |
| DebtLongtermAndShorttermCombinedAmount | 12.904 |
| LongTermDebtAndCapitalLeaseObligations | 42.170 |
| LongTermDebt | 46.094 |
| NotesPayableCurrent | 14.403 |
| DebtCurrent | 20.412 |
| DebtAndCapitalLeaseObligations | 8.712 |
| InterestExpense | 27 |
| LongTermNotesAndLoans | 1.168 |
| InterestAndDebtExpense | 25 |
| NetCashProvidedByUsedInOperatingActivities | 5 |
| PaymentsToAcquirePropertyPlantAndEquipment | 61 |
| AmortizationOfIntangibleAssets | 23 |
| IncomeTaxExpenseBenefit | 58 |
| DepreciationDepletionAndAmortization | 15 |
| Revenues | 14 |
| EarningsPerShareDiluted | 11 |
| NetIncomeLoss | 31 |
| OperatingIncomeLoss | 13 |
| RevenueFromContractWithCustomerExcludingAssessedTax | 14 |
| EarningsPerShareBasicAndDiluted | 46 |
| ProfitLoss | 14 |
| Depreciation | 2 |

## A3 — Tatsächlicher F-Datenfluss und Verwendungsnachweis

`SEC Companyfacts → parse_company_facts/TAG_ALIASES → fundamental_facts →
Legacy- oder Forward-CIK-Reader → accounting_state_as_of/build_ttm/balance_sheet_as_of →
calculate_fundamental_metrics → score_quality/score_valuation + PIT-Peergruppen →
evaluate_variant_entry(F) → champion_ranking → Rank-1 bei freiem C1-Slot`.

Aktueller F/configured/C1-Contract: Quality ≥70, Valuation ≥60; F kombiniert nur
Quality und Valuation mit den konfigurierten Gewichten, ohne Legacy-Total-/Opportunity-/
Timing-Schwelle. Momentum-/Trend-Gates bleiben aktiv. C1 = maximal eine Position.
Keine dieser Einstellungen wurde geändert.

| Metric/Tags | Relevanz |
| --- | --- |
| Revenue-Aliase, EPS-Aliase, NetCashProvidedByUsedInOperatingActivities, OperatingIncomeLoss | Wachstum, positive OCF, Operating Margin; direkte Quality-Faktoren. |
| Cash-Aliase, alle direkten und komponentenweisen Debt-Aliase, Equity-Aliase | ROIC/investiertes Kapital und Debt/EBITDA; Cash/Debt zusätzlich Enterprise Value und Valuation. |
| EntityCommonStockSharesOutstanding / CommonStockSharesOutstanding | Marktkapitalisierung, Universe-Gate, EV, FCF-Yield; Auswahl nach Datum, DEI-Priorität, Accession/Tag/Wert und Alter. |
| NetIncomeLoss/ProfitLoss und IncomeTaxExpenseBenefit | Effektive Steuerquote → ROIC. |
| Capex-Aliase, D&A-/Depreciation-/Amortization-Aliase | FCF und EBITDA, dadurch Quality-/Valuation-Faktoren und Fallbacks. |
| AssetsCurrent, LiabilitiesCurrent, Assets | Werte selbst gehen nicht direkt in die F-Scoreformeln ein. Ihre ausgewählten Periodenenden können aber balance.period_end und damit den Prior-Balance-Cutoff für ROIC beeinflussen; keine pauschale Irrelevanzbehauptung. |
| InterestExpense-Aliase | Im TTM-Modell enthalten, derzeit kein direkter Quality-/Valuation-Faktor. |

Die vollständige Aliasliste definiert `data/xbrl_parser.py:TAG_ALIASES`; nicht nur
bevorzugte Aliase werden als potenziell relevant betrachtet. Der Parser verwendet
Alias-/Unit-Priorität und dedupliziert innerhalb einer Payload anders als der DB-Key;
ein gespeicherter Tag allein beweist keine Auswahl.

**Datenklassifikation:** 2.234.423 doppelte Gruppen betreffen direkte Instant-
Faktorinputs (Cash, Debt, Equity, Shares). Weitere 1.004.150 betreffen die erwähnten
Assets-/Liabilities-/Periodenpfade: **CODEPATH_RELEVANT**, nicht automatisch ausgewählt.
Die 359 NULL-start-Flow-Gruppen werden von `discrete_quarters` wegen fehlenden Starts
nicht als TTM-Quartale verwendet: **DATA_PRESENT_BUT_NOT_SELECTED** in diesem
Berechnungspfad; Raw-Provenienz und Datenverfügbarkeitsdiagnostik bleiben getrennt.

### Kandidatenbezug: keine Scheingenauigkeit

| Population | Aggregat | Einordnung |
| --- | ---: | --- |
| Aktuell tradable CIKs laut lokaler Assets-/Companies-Zuordnung (vor Quarantäne-/Static-/Market-/Score-Gates) | 5.913 | Universe, keine aktuelle F-Eligibility. |
| Davon mit Duplikaten | 5.303 CIKs / 3.171.420 Gruppen | CODEPATH_RELEVANT; kein Nachweis einer F-Auswahl. |
| Symbole in einem vorhandenen historischen F-Trades-Artefakt | 18 | SELECTED_BY_CHAMPION nur auf Instrument-/Artefaktebene des damaligen Replays. |
| Duplikatgruppen bei heutiger CIK-Zuordnung dieser 18 Symbole | 22.238 | Proxy: historische CIK-/Source-Fact-Auswahl nicht verifiziert. |
| Tatsächlich ausgewählte aktuelle/historische F-Fact-Identitäten | **UNKNOWN** | Keine vollständige Decision→Fact-Identity-Lineage im untersuchten Artefakt. Keine Full-Screens zum Erfinden eines Zählers ausgeführt. |

Artefakt: `champion_f_recheck_2024-01-02_2026-08-12_v1_trades.csv` und zugehörige JSON
(Variant F / configured). Seine damalige Konfiguration/Codeversion ist nicht automatisch
identisch mit dem heutigen eingefrorenen Contract. Ein Ticker-Match über die heutige
Companies-Tabelle beweist weder historische CIK-Kontinuität noch ausgewählte Fact-Tags.

### Reader-, Unit- und Provenienzgrenzen

Legacy: `symbol`, `filed <= as_of`, Sortierung `filed,period_end,id`;
keine zusätzliche CIK-Sperre. Forward: verifizierte aktuelle CIK, identischer PIT-/
Sortiervertrag, Identity-Quarantäne ohne Tickerfallback; ursprüngliche Source-Ticker
bleiben im Fact erhalten. Legacy kann unter einem Ticker mehrere CIKs enthalten;
F01-Deduplizierung ist kein Anlass, diese separate Legacy-Semantik still zu ändern.

Cash/Assets/Equity und direkte Debt-Vertretung verwenden `max(period_end,filed)`;
bei Gleichstand gewinnt die erste Zeile. Debt-Komponenten überschreiben in stabiler
filed-Reihenfolge und wählen bei Gleichstand die letzte Zeile. Shares besitzen eigene
Prioritäten; Flow-Quartale werden nach Perioden/Unit mit letzter Filing-Version reduziert.
Nicht jeder Balance-Reader filtert Währungseinheiten: unterschiedliche Units bleiben
getrennte Identitäten und dürfen nicht im Rahmen dieses Audits zusammengeführt werden.

Raw-Fact-Digests ändern sich schon durch Anzahl/Reihenfolge gleicher Facts.
`LocalInputs.provenance.fundamental_inputs` hasht die gelesene Fact-Liste;
`discovery_snapshot.data_fingerprint` berücksichtigt auch Zeilen/IDs und Source-Daten.
Die Accounting-Cache-Keys enthalten eligible_count. Wirtschaftlich gleiche Outputs
bedeuten daher weder gleiche Input-Hashes noch gleiche Cache-/Storage-Kosten.
Historische Manifeste/Journale müssen unverändert bleiben.

## A4 — Wirtschaftliche Vergleichsevidenz

### Synthetische Produktionsproben

`tests/test_f01_duplicate_impact.py`: temporäre SQLite-Fixtures, echte Legacy- und
Forward-Reader, build_ttm, Balance, analyze_fundamentals, Quality/Valuation und
champion_ranking. Produktionscode und eingefrorene Konfiguration bleiben unverändert.

1. Identische, nicht konkurrierend interleavte Wiederholungen: gleiche TTM-/Balance-
   Metriken, Scoreverfügbarkeit, Quality/Valuation, Eligibility und alphabetische
   Ranking-Ties über drei PIT-Daten und beide Reader. Bei vollständigen Inputs sind
   tatsächlich zwei Kandidaten eligible; kein bloßes Gleichheitsbestehen hinter einem Guard.
2. **Gegenbeispiel zur allgemeinen Gleichwertigkeit:** Debt-Identität A=10,
   legitime andere Accession B=200, beide gleiches Filing-/Periodenende, danach erneut
   exakt A=10. Debt-Komponenten wählen vor Wiederholung B, danach A. Keine numerischen
   Konflikte innerhalb der jeweiligen Identität erforderlich.
   Ausschließlich synthetische Zahlen:

| Produktionsoutput | Vor identischer A-Wiederholung | Danach |
| --- | ---: | ---: |
| Debt/EBITDA | 1,590909 | 0,151515 |
| Quality | 94,068182 | 96,098485 |
| F-Score | 95,356679 | 96,806277 |

   Beide Fact-Reader reproduzieren die Abweichung. Eine früheste Survivor-Auswahl
   ergibt wieder den anderen Debt-Wert; eine späteste Auswahl kann dagegen Cash
   verändern. Keine generelle MIN(id)-/MAX(id)-Regel ist wirtschaftlich neutral.
   Eine Eligibility- oder reale historische Rank-1-Änderung wird aus diesem
   Score-Unterschied nicht zusätzlich behauptet.
3. Source-Symbol-Varianten derselben CIK/Identität: naives globales Deduplizieren
   kann beim Legacy-Ticker die Cash-Verfügbarkeit entfernen; Forward-CIK bleibt im
   Beispiel wirtschaftlich gleich. Dieses Datenmuster ist im Census real vorhanden,
   die Auswahl dieses Cash-Facts durch einen realen Champion-Lauf ist nicht bewiesen.
4. Amendments bleiben bis zum Filingdatum unsichtbar; Accession-, Unit- und CIK-
   Grenzen bleiben im Fixture erhalten. Eine metricspezifische Survivor-Probe
   erhält Cash- und Debt-Ausgaben des gemischten Gegenbeispiels. Das ist ein
   Designnachweis im Fixture, kein ausgeführter Migrationsalgorithmus.

### Begrenzte reale Stichprobe

Vorab definierte Auswahl: erste drei lexikographische aktuelle CIKs mit mindestens
1 und höchstens 10.000 gespeicherten Fact-Zeilen. As-of-Daten 31.05.2024, 31.05.2025,
09.11.2026; beide Produktionsreader. Je Vergleich höchstens 10.000 Facts; keine echte
DB-Kopie. Deduplizieren nur auf lokalen Listen der Stichprobe, frühester Reader-Auftritt
pro NULL-sicherer Identität, Reihenfolge der Survivors erhalten.

- 18 Reader-/Metrik-Vergleiche; in Summe über diese sich überlappenden Abfragen
  109.798 gelesene gegenüber 49.364 gedanklich deduplizierten Zeilen.
  Das sind keine verschiedenen Gesamtbestandszeilen.
- 0 Unterschiede der Fundamentalmetriken, 0 Quality-/Valuation-Unterschiede im
  ursprünglichen Vergleich ohne Referenzmedians; 18/18 geänderte Raw-Fact-Digests.
- Ergänzender Score-/Ranking-Vergleich mit **festem hypothetischem Preis 100,
  gleichem synthetischem TechnicalSnapshot und festen Referenzmedians**:
  18 Records, Quality verfügbar in 12, Valuation in 16; 0 Score-Differenzen.
  6 Drei-Instrument-Rankingvergleiche, 0 Eligibility-/Rankingdifferenzen,
  insgesamt 2 eligible Records. Es sind kontrollierte Gleichheitsproben,
  keine tatsächlichen aktuellen/historischen Champion-Entscheidungen.
- Gesamte reine Reader-Zeit der ersten Stichprobe: 2,5442 s; kein kausaler
  Vorher-/Nachher-Geschwindigkeitsbenchmark einer real migrierten DB.

Identische Scores dieser Auswahl beweisen keine allgemeine historische
Gleichwertigkeit; die synthetischen Gegenbeispiele widerlegen eine solche Garantie.

## A5 — Operativer Befund und Dringlichkeit

Normale Aktualisierungen mit zurückgelieferten alten Identitäten treffen den neuen
Fail-closed-Writer. Full/Force erzwingt den Paketpfad; Incremental tut es unter anderem
bei erkannten neuen Accessions. Unveränderte Unternehmen können korrekt übersprungen
werden; nicht jeder tägliche Aufruf ist zwangsläufig betroffen.

Ein zusätzlicher begrenzter **reiner Cache-Replay ohne Sync/Writer-Aufruf** prüfte die
ersten drei lokalen, nichtleeren us-gaap-Companyfacts-Payloads ≤10 MB, nach CIK sortiert,
höchstens zehn Instant-Identitäten je Payload: **22 von 22 geprüften Identitäten sind
im echten Bestand mehrdeutig**. Die drei zuerst untersuchten beliebigen Cache-Payloads
≤2 MB enthielten keine verwendbaren Instant-Facts; dieser Vorversuch ist kein
Negativnachweis und wurde nicht als Überlappung gezählt.

Zwei synthetische End-to-End-Proben mit unverändertem DataSynchronizer (Incremental
und Full), Fake-Companyfacts und temporärer DB bestätigen:
`AMBIGUOUS_FACT_IDENTITY → SecCompanySyncError(database,structured_facts) →
Rollback → database_failures/errors=1 → Dataset sec status=partial`.
Readiness enthält dann SEC_STATE_PARTIAL und SEC_ERRORS_PRESENT; Economic- und
Observation-Readiness sind gesperrt, selbst wenn eine frühere last_success_at besteht.
Bei ungefangenen Stage-Fehlern setzt `_run_stage` stattdessen status=failed.

**Vorhandener realer SEC-State:** partial, errors=0, identity_conflicts=71.
Das ist gerade **kein protokollierter F01-Fehler**. Die spezielle Readiness-Ausnahme
für frisch abgeschlossene, fehlerfreie Identity-Conflict-Partial-States bleibt gültig.
Ein gegenwärtiger F01-bedingter Readiness-Ausfall wird daraus nicht erfunden.
Die Relevanz des künftigen Blockers folgt aus echter Cache-Überlappung plus geprüfter
Fehlerkette, nicht aus einer ausgeführten realen Ingestion.

Storage/Query-Belastung: 10.368.182 zusätzliche gespeicherte Zeilen und deren
Indexeinträge sind bestätigt; die reale Stichprobe transportiert 60.434 zusätzliche
Fact-Objekte über die wiederholten Abfragen. Census inklusive Metadatenprüfung:
198,315 s, gesamter Scan plus Stichprobe 204,685 s. Das misst diesen Audit, keine
Produktionslatenz. Kein exakter Page-/GB-Gewinn, kein beschleunigter Sync und keine
Speicherfreigabe durch DELETE wird ohne Migration/VACUUM behauptet.

**Empfehlung:** relevante Ingestion nicht blind starten und keine automatische
Bereinigung. Kontrolliertes weiteres Design/Rehearsal ist erforderlich, insbesondere
für Source-Aliase und Reader-Gleichstände. Keine Notfall-Löschung oder unkontrollierte
Sofortmigration; keine Freigabe von Economic Activation aus diesem Audit.

## Ergebnis, Grenzen und nächster Schritt

Phase B ist fachlich ausgelöst: reproduzierter F-Score-Einfluss und relevanter
operativer Blocker. Der separate Plan enthält absichtlich harte Stop-Gates.
Die 180.283 Source-Symbol-Variantgruppen verhindern eine vollständige kanonische
Bereinigung mit unverändertem Legacy-Tickervertrag ohne zusätzliche Designentscheidung.

Offen: tatsächliche historische Decision→Fact-Lineage, reale interleavte Auswahl-
abweichungen außerhalb der Stichprobe, historische CIK-Kontinuität der 18 Artefakt-
Symbole, Zeit/Ursprungsprozess jeder Duplikatgruppe, exakter physischer Storage-/
Performancegewinn und zulässige Behandlung von Source-Alias-Kompatibilität.
Keine Profitabilitäts- oder generelle historische Gleichheitsbehauptung.

Nächster Schritt: separaten Alias-/Survivor-Vertrag entscheiden und in isolierten
kleinen Fixtures absichern; erst danach einen freizugebenden Copy-only-Dry-Run planen.
Eine reale Migration ist mit diesem Bericht weder genehmigt noch ausführungsreif.

## Validierung und Artefakte

**14 gezielte Offline-Tests bestanden**, 1 bestehende websockets.legacy-
DeprecationWarning, 16,30 s. Ruff für die neue Testdatei: bestanden.
Ein erster Probelauf hatte einen ungeeigneten synthetischen Daily-Index-Kalender;
dieser Fixturefehler wurde korrigiert, bevor die operative Schlussfolgerung gezogen wurde.
Keine Produktionskorrektur vorgenommen.

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/test_f01_duplicate_impact.py -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_f01_impact_cache --basetemp=.tmp_pytest_f01_impact_verified --junitxml=reports/f01_impact_tests.xml
```

Artefakte: `reports/f01_impact_inventory.py` (read-only Census mit Sample-Definition),
`reports/f01_impact_inventory.json`, `reports/f01_real_score_comparison.json`,
`reports/f01_operational_overlap.json`, `reports/f01_impact_tests.txt/.xml`.
Zusätzliche Probeausgaben enthalten nur Zähler und ausdrücklich synthetische Werte.
Größe/Änderungszeit der realen Hauptdatei blieben bei allen Scans identisch;
SQL-Verbindungen waren ro/query_only. Keine Quellen oder historischen Artefakte überschrieben.
`git diff -- src config` ist leer. Änderungen: neue Audit-Tests, diese beiden Berichte
und ausschließlich deren `.gitignore`-Ausnahmen. Kein Commit/Push.

