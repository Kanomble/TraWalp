# TraWalp · Living Project State

## 1. Executive Summary

TraWalp ist ein lokales Python-/SQLite-System für Aktie-Screening, Point-in-Time-Fundamentaldaten, Strategieforschung, portfolioorientierte Simulation und isolierte Forward-Beobachtung. Der eingefrorene Research-Champion ist **F/configured/C1**. Die vorhandenen historischen Nachweise dienen der Reproduktion ihres jeweiligen Scopes. Eine operative Champion-, Broker- oder Canonical-Cutover-Freigabe liegt in den geprüften Quellen nicht vor.

Der neue Canonical-A-V1-Pfad funktioniert für einen real verifizierten Sieben-Emittenten-Research-Bestand. E.13 rekonstruiert fachlich freigegebene Fundamentalwerte aus exakt gebundenen Original-Filings und bewertet ihre damalige Verfügbarkeit. Für 50 Emittenten gibt es eine historische Recherchekohorte, Filing- und Markt-Gaps sowie einen kleinen Datenpilot. Die vollständige 50er-Datenbank, lückenlose Marktzeitreihen, historische Security-Epochen und die integrierte Strategiequalifikation stehen noch aus. [Architekturindex](f01_research_architecture_index.md) · [E.5 PIT-Audit](../reports/f01_historical_pit_qualification_audit.md) · [50er-Preflight](../reports/f01_historical_security_50_issuer_preflight_audit.md)

## 2. Current Status

**Aktualisiert am 25.11.2026 · Task 9.5.** Geprüfter Branch SEC-integration, sauberer Ausgangs-HEAD b3a1944d0c64925f2cee6f2cdf7defcdd12932cf. Task 9.5 liegt uncommittet vor. Der Offline-Audit und der Task-9.6-Plan sind veröffentlicht. [Task-9.5-Audit](f01_task95_historical_data_readiness.md)

| Gegenstand | Implementierung | Belegte Validierung | Freigabe oder Grenze |
| --- | --- | --- | --- |
| Core, A–F, F/configured/C1 | `IMPLEMENTED` | `VALIDATED_REAL_DATA` in gebundenen historischen Läufen | `RESEARCH_ONLY`; keine neue aktuelle qualifizierte Forward-Rangliste |
| Shadow und Observation | `IMPLEMENTED` | `TESTED_OFFLINE`, reale Readiness-Abfragen historisch dokumentiert | `BLOCKED`; der verlinkte reale Observation-Gate meldet `OBSERVATION_BLOCKED` |
| SEC-Bulk und Arelle | `IMPLEMENTED` | `VALIDATED_REAL_DATA` auf veröffentlichtem Bulk und 339 Filings | `RESEARCH_ONLY`; durchgehende fachliche und PIT-Abdeckung `PARTIAL` |
| Canonical E.11 und E.13 | `IMPLEMENTED` | `VALIDATED_REAL_DATA`, `PARITY_VERIFIED`, 77 Anker, drei freigegebene Regeln | `RESEARCH_ONLY` in exakter Daten- und Reader-Bindung |
| Historischer Fundamental-PIT | `IMPLEMENTED` | `VALIDATED_REAL_DATA` für sieben fest gebundene Issuer | `PARTIAL`; Marktdaten und historisches Trading-Universe separat |
| Copy-Rehearsal G0–G6 | `F01_LEGACY_MIGRATION_RETIRED` | Historisch G3 `G3_COMPLETE`, G6 `PASS`, V2-A für gebundenen Snapshot | Aktiver Rehearsal-Pfad entfernt. Original-Rehearsal-Bytes lokal nicht verfügbar. |
| G4 und V2-Scopes | `IMPLEMENTED` | `VALIDATED_REAL_DATA`; Scope A für gebundenen Snapshot belegt | Historisches G4 bleibt `INSUFFICIENT_EVIDENCE`. Scopes B/C offen, D `NOT_RELEASED` |
| G5-Integration | `IMPLEMENTED` | `TESTED_OFFLINE` | `BLOCKED`; keine unabhängige reale G5-Abnahme in der gültigen Belegkette |
| Historische Security und SIP | `IMPLEMENTED` für Adapter, Referenzen und Diagnose | `VALIDATED_REAL_DATA` für begrenzte lokale Stichproben | `PARTIAL`; Handelbarkeit, Vollständigkeit, Lizenz und Warm-up separat |
| 50-Issuer-Preflight | `F01_HISTORICAL_50_BATCH_PREPARATION_READY` | Offline-Regressionen, 50 CIKs und erster Review-Batch mit 20 Accessions | Acquisition und Import separat gesperrt. 0/50 historisch trading-qualifiziert. |
| Task 9 Filing-Pipeline | `TASK9_MANUAL_EXECUTION_READY` im V1-Scope | 85 gezielte Offline-Tests, inklusive synthetischer Arelle-, Import-/Replay-, Recovery- und E.13-Proben | `REAL_DATA_ACQUISITION_AND_INGESTION_PENDING_MANUAL_EXECUTION`. Ein NKE-Filing als erster Vorschlag. |
| NKE-DTS-Reparatur | Tatsächlich geladene DTS-URIs aus gebundener Quarantäne fließen in die lokale Closure ein. | Vier gezielte Offline-Tests und lesender Realabgleich von 22 auf 23 externe Abhängigkeiten. | Neue Selection im bestehenden Job. Reale Extraktion, Zulassungen und Import stehen aus. |
| Auto-Admission V1 | Separate Maschinenentscheidung mit Evidence-Digest, erneuter Prüfung und unabhängigem Importgate. | NKE-Originale, Closure und Shard lesend geprüft. Zwei Facts gleichen dem manuellen Review. | Historische NKE-Selection dient nur dem Preflight. DB-Inhalt und nutzerberichtete Receipt-/Reader-Ergebnisse bleiben unabhängig unverifiziert. |
| Zweites NKE-Filing | Job f01-task9-nke-02 mit lokaler Source-, DTS- und Extraction-Evidenz. | Task 9.5 bestätigte vier Facts, vier Occurrences, zwei Receipts und die höchste Dataset-Version 3. | Reales Replay und E.13-Erfolg für das Folgefiling stammen aus der Nutzerbaseline. Task 9.5 prüfte den Storage lesend. |
| Companyfacts Bulk-Staging | F01_D6_COMPANYFACTS_STAGING_V1 veröffentlicht. | SQLite-SHA-256 und 120.477 retrospektive Kandidaten bestätigt. | UNVERIFIED_COMPANYFACTS_RESEARCH_STAGING. Original-PIT und Canonical separat. |
| Historische Readiness | AUDIT_COMPLETE_PLAN_ONLY | D.6 und SPY lesend inventarisiert. | Vollständiger technischer oder PIT-qualifizierter D.6-Start unbelegt. [Task-9.5-Audit](f01_task95_historical_data_readiness.md) |
| Produktiver Cutover | `PLANNED` als gesonderte Entscheidung | `UNKNOWN` | `BLOCKED`; V2-D `NOT_RELEASED` |

Die letzten vorhandenen Real-Gates wurden für diese Baseline nicht erneut gestartet. Der aktuelle Betriebszustand eines eventuell extern betriebenen Journals oder Brokerkontos ist `UNKNOWN`. [Freigabematrix](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/status_matrix.csv>) · [Acceptance V2](f01_acceptance_v2.md) · [Observation-Gate](../reports/shadow_pre_inception_final_audit.md)

## 3. Project Goals & Scope

Ziel ist ein reproduzierbarer lokaler Research-Bestand, in dem Fundamentalwerte, Kurse und handelbare Wertpapiere zum damaligen Entscheidungszeitpunkt zusammenpassen. Die erste erweiterte Kohorte umfasst 50 Emittenten und den separat behandelten Benchmark SPY. Kostenlose Quellen und lokale Datenhaltung sind Projektvorgaben. Dokumentierter Tarif, erfolgreiche Datenantwort, Lizenz zur Aufbewahrung und fachliche Zulassung sind eigenständige Nachweise.

Historische Strategieaussagen setzen geprüfte Inputs und einen dokumentierten Universe-Scope voraus. Produktionsausführung, Live-/Paper-Brokerfreigabe, Portfolioerweiterung und App-Betrieb bleiben getrennte spätere Entscheidungen. [D.6 Zulassung und Datenbedarf](../reports/f01_historical_security_50_issuer_preflight_audit.md) · [D.8 Marktvertrag](f01_alpaca_historical_sip_coverage.md)

## 4. System Architecture

| Ebene | Tatsächliche Komponenten | Datenfluss und Grenzen |
| --- | --- | --- |
| CLI und Konfiguration | [`cli.py`](../src/trading_system/cli.py), [`config.py`](../src/trading_system/config.py), [`strategy.yaml`](../config/strategy.yaml), [Packaging](../pyproject.toml) | `trading-system` lädt Settings und expliziten Workflow. Strategie-, Risiko-, Filter-, Kosten- und Speicherparameter sind zentral. Secrets bleiben außerhalb dieses Dokuments. |
| Operativer Dateneingang | [`alpaca_client.py`](../src/trading_system/data/alpaca_client.py), [`sec_client.py`](../src/trading_system/data/sec_client.py), [`sync.py`](../src/trading_system/data/sync.py), [`database.py`](../src/trading_system/data/database.py) | Asset-Scope und SEC-Beobachtungen speisen Firmen/Facts. Alpaca liefert Daily- und Intraday-Bars. Legacy speichert den Bars-Key `(symbol, timeframe, timestamp)`. |
| Screening und Ranking | [`screener.py`](../src/trading_system/strategy/screener.py), [`scoring.py`](../src/trading_system/strategy/scoring.py), [`screen_strategies.py`](../src/trading_system/backtest/screen_strategies.py) | Qualifizierte Inputs → Eligibility, Peers und Komponenten → Strategy-Gates → Ranking. Fehlende rangrelevante Inputs können die Ausgabe sperren. Identität und Feed sind eigenständige Eingangsverträge. |
| Portfolio und simulierte Execution | [`engine.py`](../src/trading_system/backtest/engine.py), [Research-Registry](../src/trading_system/backtest/research_registry.py), [Positionsregeln](position-management.md) | Historischer Replay verwendet zeitgebundene Features plus Preise, Risiko, Positionsgröße, Holds, Kosten und Fill-Modell. F-Signale aus abgeschlossenen Sessions setzen nicht rückwirkend Fills. |
| Forward und Shadow | [`forward_market.py`](../src/trading_system/data/forward_market.py), [Readiness](../src/trading_system/shadow/readiness.py), [Admission](../src/trading_system/shadow/admission.py), [Runner](../src/trading_system/shadow/runner.py), [Journal](../src/trading_system/shadow/journal.py) | Separates Alpaca SIP/all/1d-Archiv → STRICT und Inception-Admission → lokales Journal. Observation und Economic haben unterschiedliche Readiness. Shadow erzeugt interne Beobachtungen bzw. modellierte Intents/Fills innerhalb seiner Verträge. |
| Research-Fundamentals | [Bulk-Runbook](f01_sec_bulk_download.md), [Filing Evidence](f01_original_filing_evidence.md), [`canonical_facts.py`](../src/trading_system/data/canonical_facts.py), [`canonical_research.py`](../src/trading_system/fundamentals/canonical_research.py) | Originale / Companyfacts → Parser/Arelle und geprüfte Semantik → isolierter Canonical-Writer/Receipts → beleggebundener E.13-Reader. Legacy ist weiterhin ein eigener, kompatibler Laufzeitpfad. |
| Historische Identitäten | [`security_identity.py`](../src/trading_system/data/security_identity.py), [`security_lifecycle.py`](../src/trading_system/data/security_lifecycle.py), [Security Reference](security-reference.md) | CIK ist die Reporting-Entity. Handelbare Klassen/Security-IDs werden über zeitgebundene Belege, Symbole und Corporate Actions zugeordnet. Punktbeobachtungen bleiben Punkte. |
| 50er-Erweiterung | [Offline-Preflight und Ziel-Lineage](f01_historical_50_research_preparation.md), [Task-9-Architektur](f01_historical_50_ingestion_architecture.md) | D.6/D.11-Bindung → SEC-/DTS-/Arelle-Phasen → manuelle oder evidenzbasierte Admission → separat autorisierter Canonical-Append → Duplicate Assurance und E.13-Grenzprobe. Der zweite NKE-Fall nutzt den vorhandenen Bootstrap. |
| Companyfacts-Staging | [Task-9.4-Vertrag](f01_task94_historical50_bulk_staging.md) | Gebundene D.6-Member → Rohbeobachtungen und konservative Metrikzuordnung → eigener SQLite-Zielordner mit `retrospective_fundamentals` und Coverage pro CIK, Jahr und Metrik. Historische Originale und PIT bleiben eigene Evidenz. |
| Historische Readiness | [Task-9.5-Audit](f01_task95_historical_data_readiness.md) | Gebundene Quellen → Offline-Coverage und Gap-Katalog → separat autorisierbare Beschaffungsblöcke. |

Der aktive produktive Pfad und das E.11-Research-Paket teilen Teile der Fachlogik. Die operative Readiness und Provenienz jeder Datenquelle bleiben separate Verträge. [Code-Inventar mit SHA-256](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/architecture_inventory.json>)

## 5. Historical Development Timeline

| Zeitraum | Belegte Entwicklung | Verbleibende Grenze |
| --- | --- | --- |
| 09.–24.08.2026 | Commits initialisieren Sync/Screening, SEC-Normalisierung und SQLite, inkrementellen Screen, historische Daily-Daten, Intraday-Varianten und Strategievergleich. | Frühe Meilensteine sind Codegeschichte. Ein Commit allein belegt keine Daten- oder Betriebsfreigabe. |
| Ende August bis Mitte September | F-Intraday, Robustheit, Kapazität und Lifecycle untersucht. September-Konsolidierung friert F/configured/C1. ORB V1, Intraday Reversal V1 und Market Intraday Momentum V1 bleiben historische abgelehnte bzw. reproduzierbare Research-Artefakte. | Kein automatischer Champion-Wechsel oder Forward-OOS-Beweis. |
| Oktober | Massive-Free-Reference und dauerhafte Rate-Limits, SEC-/Nasdaq-/CIK-Identitätsprüfung, Quarantäne und Pairs-Research. Der 2022-Universe-Audit legt Survivorship-Risiken offen. Rekonstruierter U0-Snapshot erhält eigene Replay-Autorität. | U0 bleibt heutiger Source-Scope. Exaktes U1 ist wegen Intervalllücken geparkt. |
| Ende Oktober bis 13.11. | Shadow-Infrastruktur, Forward-SIP, STRICT-Sharing und Inception-Admission. Task 11A beschreibt Evidence-Schema, 11B atomare Economic-Management-Policy B. | Die reale Evidence-Kette meldet keine zulässige Inception. Kein belegter Brokerbetrieb. |
| 12.–20.11. | Deduplikationsproblem und Champion-Auswirkung untersucht. F01 bewahrt sämtliche Occurrences und historische Projektionen. Kopierprobe G0–G3, G4, getrennte V2-Scopes und G6-Recovery. | Keine pauschale Bereinigung von Alt-Facts. G5, B/C/D und Cutover bleiben getrennt. |
| 21.11. | SEC-Persistence und isolierte DataSynchronizer-Anbindung; kontrollierter Bulk- und Canonical-Bootstrap. Report- und Import-Scope getrennt, BAC und JSM ausgeschlossen. | Offline-Integration belegt, reale G5-Abnahme offen. |
| 22.11. | Offline-Arelle-Verarbeitung lokaler Filings, Taxonomie-/EDGAR-Transformationsregeln, Accounting-Evidenz, E.11 Handoff, E.12 und E.13. | Accounting-Fact-Abdeckung bleibt ungleichmäßig; nur sieben zugelassene Issuer. |
| 22.–23.11. | E.5 rekonstruiert Filed-/Acceptance- und Ablaufereignisse. D.6 bindet 50 historisch begründete Kandidaten, D.7/Zusätze untersuchen kostenlose Provider und Limiter, D.8 zählt SIP- und Warm-up-Gaps getrennt. | 0/50 qualifizierte Trading-Historien. Providerzugriff und Lizenz lassen sich aus Stichproben nicht ableiten. |
| 23.11. | Unter ID 8F.3D.9 wurde der frühere Markt-Runner-Auftrag abgebrochen. Der nachfolgende Codebase-/Storage-Audit unter derselben ID wurde abgeschlossen. D.10 fror die startende Living-State-Baseline ein. | Keine erkennbare Runner-Implementierung aus dem Abbruch. |
| 24.11. | D.11 legt den aktiven Copy-Rehearsal-Pfad still, sichert gleichschlüsselige Canonical-Konflikte ab und erstellt den ersten Offline-Batch für die 50er-Erweiterung. | Neue Source-Gates, Datenbank und Trading bleiben offen. |
| 24.11. | Task 9 übernimmt den zuvor als 8F.3D.12 geplanten Ausbau und implementiert die manuelle Filing-Pipeline. | Erstes NKE-Filing vorgeschlagen. Reale Downloads, Extraktion und Import bleiben manuell. |
| 25.11. | Task 9.5 verifiziert das veröffentlichte Staging und quantifiziert Markt-, Security- und SEC-Lücken sowie die Eingangsanforderungen von C1. | Keine Download- oder Trading-Freigabe. |

[Vollständige Git-Chronologie](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/git_chronology.csv>) · [Quellengebundene Task-Timeline](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/task_timeline.csv>)

## 6. Implemented Components

- **Core:** Workflow-CLI, SQLite-Schema, Sync-Stages, Qualität/Value/Opportunity/Timing, technisches Screening und Ranking, Portfolio- und Fill-Modell. Belegte historische Regressionen und Drawdown-/Kostenanalysen gehören zum jeweiligen Freeze. [Champion-Konsolidierung](../reports/champion_consolidation_audit.md)
- **Market und Security:** Alpaca-Daily/Intraday, explizites Forward-SIP, XNYS-Kalender, Vergleichs- und Gap-Prüfungen, CIK/FIGI-/Lifecycle-Recherche, streng begrenzte Massive-Tools. Ein manueller reboot-aware Recovery-Pfad für den bestehenden Massive-Limiter ist vorhanden und offline getestet; ein erfolgreicher Dry-run ist keine automatische Zustandsänderung. [Daily-Runbook](historical-daily-backfill.md) · [Reboot-Recovery](f01_massive_reboot_recovery_audit.md)
- **SEC-Capture und Canonical:** Retrieval und lokale Bulk-Integrität, Source-Membership-Admission, Original-Filing-Evidenz, versionierte Arelle-Taxonomien/Transformationen, verlustfreie Occurrences, Current/Revision-/Dataset-Versionen, Receipt und Request-Archiv. Canonical-Anbindung des DataSynchronizer im isolierten Pfad ist realisiert und offline getestet. [Integrationsaudit](../reports/f01_data_synchronizer_integration_audit.md)
- **Replay und Zeitsemantik:** E.13 prüft Accepted-/Filed-Zeitpunkte und Periodenkontext, starke Supersession-Bindung für widersprüchliche Amendments und freigegebene E.12-Fallbacks. Nicht belegte Angaben erscheinen mit konkretem Missing-Status. Die Freigabe bindet Regel-, Source-, Schema-, Daten- und Code-Stand. [E.13 Audit](../reports/f01_canonical_research_reader_audit.md)
- **Canonical Deduplication:** Der A-V1-Key und seine partiellen Unique-Indizes gelten weiter. Widersprüchliche Inhalte für denselben Key innerhalb eines Requests werden atomar abgewiesen. Die isolierte Duplicate Assurance prüft Storage und optionale PIT-Zeugen. [Canonical-Vertrag](f01_canonical_deduplication_contract.md)
- **Historische Filing-Aufnahme:** Die Task-9-CLI verbindet manuellen Erwerb und Byteverifikation, DTS, resümierbare Arelle-Shards, zwei typisierte Reviews und einen atomaren Filing-Receipt. Der erste Adapter lässt geprüfte konsolidierte USD-Instant-Facts für Assets und Cash zu. [Runbook](f01_historical_50_manual_ingestion_runbook.md)
- **Historische Daten-Readiness:** Die CLI inventarisiert 50 CIKs und SPY schreibgeschützt und veröffentlicht Coverage, Fundamental-Gaps, C1-Readiness und den priorisierten Backfill-Plan. [Task-9.5-Audit](f01_task95_historical_data_readiness.md)
- **Wartung:** D.9 erstellte Repository-/Storage-Inventar, Dependency-Graph und Cleanup-Manifest. D.11 entfernte den ausschließlich migrationsbezogenen Rehearsal-Code und verlagerte gemeinsame Research-Fingerprints. [D.9 Abschluss](</G:/TraWalpResearch/import-artifacts/f01-comprehensive-audit-20261123-v1/final_audit.md>) · [Retirement](f01_legacy_migration_retirement.md)

## 7. SEC, Arelle & Fundamental Research

**Task 9.5.** Das reale Staging ist veröffentlicht und hashverifiziert. 2.613 der erwarteten 2.952 CIK-/Accession-Paare fehlen in den geprüften Caches. 337 lokale Einträge sind byteunverifiziert, zwei NKE-Originalsätze gegen ihre publizierten Hashes geprüft. [Task-9.5-Audit](f01_task95_historical_data_readiness.md)

**Task 9.4 Bulk-Preflight.** Alle 50 Companyfacts- und Submissions-Member wurden gegen die D.6-Bindungen geprüft. 1.189.487 Beobachtungen gelesen, davon 1.140.816 im Zeitraum oder undatierbar. 4.591 mehrdeutige Beobachtungen sind aus der normalisierten Sicht ausgeschlossen. Alle 120.477 retrospektiven Kandidaten haben eine Submissions-Acceptance-Bindung. Der Bulk-Pfad qualifiziert keine historischen Originalwerte. Bei 2.613 periodischen Filing-Paaren fehlte ein Verzeichnis im geprüften Cache. 2.585 Accessions erfüllen die Kriterien für 130 offline erstellte Beschaffungsgruppen. [Coverage und Filing-Plan](f01_task94_historical50_bulk_staging.md)

**Task 9.3 Folgefiling.** NKE `0000320187-16-000264` ist der nächste Original-10-Q mit derselben Reporting-CIK. Companyfacts liefert für den 29.02.2016 passende Hinweise auf Assets und Cash. Originale fehlen in den geprüften Caches. Die 23 externen Abhängigkeiten des Referenzfilings wurden erneut lokal gebunden. Die neue Closure erfordert die eigenen Originale. [PowerShell-Runbook](f01_task93_nke_append_runbook.md)

**Task 9 Erstfall.** NKE `0000320187-16-000242` liegt mit `SOURCE_VERIFIED`, 23 externen DTS-Abhängigkeiten, `EXTRACTION_COMPLETE` und veröffentlichten manuellen Reviews vor. Task 9.2 verifizierte die Original-/DTS- und Artefaktbindungen lesend. Assets 22.583.000.000 USD und Cash 3.851.000.000 USD samt Observation-IDs stimmen exakt mit dem manuellen Review überein. Der Nutzer meldete Import in Dataset `f01-research-d2a3d4ecbe8947b8a3be4a15dbbac08b` Version 2, erfolgreichen identischen Replay und E.13 mit zwei Treffern. Die Datenbank blieb in Task 9.2 geschlossen. [Offline-Preflight](f01_historical_50_manual_ingestion_runbook.md)

**Bulk.** Das real vorhandene finalisierte Source-Manifest bindet `pilot01-20261121-175840` und die Originalarchive. Der historische Strukturcensus nennt 20.609 Companyfacts-Members, davon 18.136 strukturell gültig und 2.473 kontrolliert ausgeschlossen. Submissions enthält 999.458 geprüfte Members einschließlich des explizit nicht importierbaren Platzhalters. Die Archive sind aktuelle Provider-Rekonstruktionen; ihre Strukturfreigabe belegt keine historischen Provider-Wertversionen oder fachliche Admission. [Bulk-Finalisierung](../reports/f01_sec_bulk_finalization_audit.md) · [Finales Source-Manifest](</G:/TraWalpResearch/sec-originals/pilot01-20261121-175840/f01_sec_bulk_source_manifest.json>)

**Original-Filings und Arelle.** Aus Companyfacts-Ankern werden Accessions und Originale bestimmt. Der gebundene Arelle-Stand 2.47.5 verarbeitet offline XBRL/Inline-XBRL oder geeignete separate XML-Instanzen. DTS, SEC-Transformationen, QNames, Units, Contexts, Dimensionen, Decimals, Acceptances und Source-Hashes sind getrennt belegbar. Der erweiterte Lauf vom 22.11.2026 erfasste 339 Filings, 476.282 ausgewählte Originalbeobachtungen und 141.449 Zuordnungen. 139.734 waren technisch verifiziert und 1.715 offen. Der Audit berichtete 889 Filing-Issues unterschiedlicher Schwere. Drei alte CAT-Instanzen bleiben wegen DTS unaufgelöst. EdgarTools 6.3.0 ergänzt die Review-Ansicht und lieferte keine zusätzlich qualifizierten Total-Debt-/ROIC-Werte. [Arelle-Runbook](f01_original_filing_evidence.md) · [Erweiterte Evidenz](../reports/f01_extended_accounting_evidence_qualification_audit.md) · [EdgarTools](../reports/f01_edgartools_accounting_standardization_audit.md)

**Semantik.** Raw-Tags, Taxonomie, Provenienz und Abstimmungsregeln bleiben erhalten. Parent-/Consolidated-Income, Total-/Continuing-OCF, Total Debt, Finance Leases und CapEx brauchen passenden ökonomischen Scope. Der E.10-Term-Debt-Teilbetrag ist eine separate versionierte Kennzahl. E.12 ergänzt drei geprüfte Fallback-Regeln ohne Überschreiben belegter Werte oder Vermischen inkompatibler Inputs. ROIC- und EBITDA-Definitionen von TraWalp gelten weiter. [Mapping](../reports/f01_sec_fundamental_mapping_correction_audit.md) · [Semantische Auswahl](../reports/f01_semantic_fact_selection_audit.md) · [E.12](../reports/f01_accounting_fallback_resolver_audit.md)

**Zulassung und nachgewiesene Daten.** Der erste Sieben-Emittenten-Pilot auf `f01-research-adb7d5d14b5b4cf889c8a8e5e63fa185` hat 26.102 Facts/Occurrences. Das getrennte E.11 auf `f01-research-051f55358e844dd0b752ad0c10fa2e84` hat Version 8, sieben Receipts und 26.184 Facts. Beide behalten ihren Scope. AAPL, ACN, CAT, COST, LLY, MSFT und NVDA sind zugelassen. BAC bleibt wegen des Reporting-Entity-Konflikts, JSM wegen Identity-Konflikts ausgeschlossen. Der E.13-Report meldet 77 Paritätsanker, 4.234 gebundene Originalreferenzen, 31 verwendete Fallback-Proofs und null Findings. [Scope V2](../reports/f01_pilot_scope_reduction_audit.md) · [E.11 Handoff](../reports/f01_canonical_accounting_handoff_audit.md) · [E.13 Ergebnis](</G:/TraWalpResearch/import-artifacts/f01-e13-reader-validation-20261122-211700.json>)

**Filing-Lücken.** D.6 maß 2.352 unterschiedliche fehlende periodische Original-Accessions für die erweiterte Kohorte. D.11 fand für diese 2.352 Paare kein Originalverzeichnis im geprüften Cache. Für weitere 336 periodische Paare waren Index- und Dokumentnamen gelistet und die Arelle-Shard-Größen abgleichbar. Originalbytes und neue fachliche Zulassung bleiben unverifiziert. Offene Fachfälle umfassen ACN Issuer Shares, CAT/COST Warm-up-OCF, NVDA PPE-only-CapEx, vollständige konsolidierte Debt-Komponenten und ROIC. E.13-Paritätsanker lieferten Total Debt 4/77, Operating Income 65/77, EBITDA 47/77, CapEx 54/77, OCF 65/77, FCF 46/77 und ROIC 0/77. [D.11 Preflight](f01_historical_50_research_preparation.md) · [D.6 Inventar](../reports/f01_historical_50_preflight.json) · [E.5 Findings](../reports/f01_historical_pit_qualification_audit.md)

## 8. Market Data & Historical Coverage

**Task 9.5.** Ab 2010 umfasst der XNYS-Horizont 4.238 Sessions bis 06.11.2026. Der SIP/all-Plan enthält 217.076 Emittenten-Symbolsessions als Obergrenze und 4.238 fehlende SPY-Sessions einschließlich 320 exakter Vorlaufsessions. Historische Listing- und Exit-Grenzen bestimmen die Bestellmengen. [Task-9.5-Audit](f01_task95_historical_data_readiness.md)

Der D.11 Fundamental-Batch plant ohne vorausgesetzte Trading-Freigabe. Feed, Adjustment, Corporate Actions, Security-Epochen und SPY-Warm-up bleiben eigenständige PIT-Evidence-Gates. [50er-Vorbereitung](f01_historical_50_research_preparation.md)

**Speicherverträge.** Legacy/U0 zählen historisch sichtbare Bars, aber Legacy bindet Feed und Adjustment nicht je Zeile. Der explizite Forward-Vertrag `SIP_DAILY_ALL_FAIL_CLOSED_V1` bindet Alpaca/SIP/all/1d, UTC-Retrieval und Session. Das Archiv dient dem operativen Lookback. Raw, Split und All sind getrennte Preisbasen; IEX ersetzt keine fehlenden SIP-Sessions. Intraday-Replay hat eigenständige Coverage-, Session- und Ausführungsverträge. [Forward SIP](../reports/f_forward_sip_shadow_contract_audit.md) · [Execution Fidelity](../reports/market_execution_fidelity_audit.md) · [Intraday-Runbook](intraday-market-data.md)

**Lokale Belege D.6/D.8.** Im gebundenen Bestand lag Forward bei 1.806.028 Zeilen und 5.892 Labels zwischen 01.08.2025 und 06.11.2026. SPY hat separat im Forward-Bestand 320/1.732 nutzbare Sessions für 2020–20.11.2026, im Legacy-Bestand 1.570/1.732 ab 27.07.2020. Für 2016–2019 fehlen in diesen beiden Beständen die 1.006 SPY-Sessions. D.8 zählt Proben getrennt und lieferte 864 Matrixzeilen und 807 Lückenblöcke für die acht Kontrollen. Die 300 Sessions vor dem Start 2016 vom 23.10.2014 bis 31.12.2015 fehlen lokal. [D.8 Ergebnis](</G:/TraWalpResearch/import-artifacts/f01-alpaca-sip-coverage-2016-2026-v1/f01-alpaca-sip-coverage-2016-2026-v1_summary.json>)

**Provider.** Die protokollierte Alpaca-Probe vom 23.11.2026 weist 25 HTTP-Antworten und kleine historische SIP-Fenster aus 2016/2018/2019/2025 aus. Der historische Basic-Rahmen mit verzögertem SIP ist dokumentiert. Die Tarifzuordnung dieses Laufes und dauerhafte Archivierungsrechte sind in den Probe-Artefakten `UNVERIFIED`. Massive wies Bars aus 2018 und 2022 mit HTTP 403 ab und lieferte neuere Bars sowie einzelne ältere Referenzpunkte. Das ist kein Ersatz für eine lückenlose SIP-Reihe oder einen Security-Master. Die D.8-Schätzung von 73–146 Antwortseiten gilt nur für acht Kontrollsymbole. Der 50er-Download-Runner wurde im abgebrochenen Auftrag nicht erkennbar implementiert. [D.7 Audit](../reports/f01_historical_market_feasibility_audit.md) · [D.8 Vertragsauswertung](f01_alpaca_historical_sip_coverage.md)

## 9. Databases, Migration & Storage

**Task 9.5.** Legacy-/Forward-Bars, U0, das Task-9.4-Staging und drei Canonical-Bestände wurden schreibgeschützt geöffnet. Der Backfill ist auf rund 104 GiB inklusive Reserve und rund 80 GiB Backup geschätzt. Freier Platz beim Audit mindestens 177 GiB auf G: und 118 GiB auf F:. Backup-Trennung und Restore separat belegen. [Task-9.5-Audit](f01_task95_historical_data_readiness.md)

| Bestand | Befund und Schutzstatus |
| --- | --- |
| Produktive Originaldatenbank `data/trading_system.sqlite3` | Physisch vorhanden. Metadaten am 23.11.2026: 10.501.169.152 Bytes. WAL/SHM gehören zum geschützten Bestand. In diesem Task kein SQLite-Zugriff. |
| Legacy-Projektionen und U0-Snapshot | Alte Bars-/Fact-Verträge und der historisch rekonstruierte U0-Baseline-Snapshot bleiben separate Referenzen. U0 ist maßgeblich für seine eingefrorene Replay-Abnahme. Historische Altberichte bleiben Zeitzeugen und enthalten nicht zwangsläufig reproduzierbare Candidate-Lineage. |
| G0–G6 Copy-Rehearsal | Original-Jobpfad `reports/f01_copy_rehearsal/20261115_8f2a` nach manueller Vorab-Löschung abwesend. D.11 entfernte 393 weitere eindeutig zugeordnete Dateien mit 39.909.268 logischen Dateibytes. [Retirement](f01_legacy_migration_retirement.md) |
| Sieben-Emittenten-Piloten | Unter `G:\TraWalpResearch\research-databases`. E.11 SQLite am 23.11.2026 per Metadaten 16.842.752 Bytes. D.9 maß 67.380.056 Bytes für den gesamten E.11-Ordner inkl. Requests/Metadaten. E.13 öffnet den gebundenen Bestand als Reader. |
| Zukünftige 50er-Lineage | Task 9 bindet den späteren Bootstrap an exakt dieses neue Research-Ziel, die geprüfte Startauswahl und den aktuellen Code. Canonical-Writer, Receipt-Replay und native Journal-Recovery wurden synthetisch geprüft. Der reale Zielpfad ist abwesend. [Kapazitätsplan](</G:/TraWalpResearch/import-artifacts/f01-task9-ingestion-preparation-v1/capacity_plan.json>) |

**Historische Abnahme.** Das alte Problem war eine mögliche fachliche Wirkung identisch wiederholter Facts auf Auswahl und Scores. Der Migrationspfad bewahrt 18.698.090 Occurrences bei 8.329.908 eindeutigen Canonical-Facts. G3 ist vollständig. Historisches V1-G4 und Supplements behalten `INSUFFICIENT_EVIDENCE`. V2 akzeptiert A (Storage-/Semantikparität) für den gebundenen Snapshot, hält B (qualifiziertes Forward-Ranking) mit je 334 offenen Kandidaten offen und C (historische Identity) unbelegt. D (operative Champion-Readiness) bleibt `NOT_RELEASED`. G6-PASS belegt Restore- und Crash-/Resume-Verhalten. G5 braucht eine unabhängige reale Integrationsevidenz und hat weiterhin keinen belegten PASS. [V2](f01_acceptance_v2.md) · [G5-Input](../reports/f01_g5_input_evidence_preparation_audit.md)

Der gebundene V2-Bericht nennt 2.618 PIT-/Accounting-Vergleiche, zwölf unveränderte Nicht-Fact-Tabellen, 5.913 verglichene Unternehmen und 6.483 Symbole im Identity-Census. Der synthetische Reuse-Test zählt für A. Diese Zahlen enthalten keine autoritativ qualifizierten historischen Identity-Epochen für C. [V2-Bridge-Audit](../reports/f01_acceptance_bridge_audit.md)

**Kapazität.** D.9 berichtete am 23.11.2026 **90,020 GiB** frei, **2,037 GiB** bedingt rückgewinnbar und **0 Bytes** zur Löschung freigegeben. Der neue Quellenlauf maß davon getrennt **96.656.535.552 freie Bytes** auf G: (rund 90,018 GiB). Eine erneute Speicherinventur des gesamten Datenträgers fand nicht statt. **75,37 GiB** zusätzlicher SEC-Arbeitsbedarf und **128,75 GiB** mit vollständiger neuer Kopie auf G: bleiben D.9-Schätzungen auf Basis des 339-Filing-Pilots, 2.352 Lücken und Reserve. Ein zweites physisches Laufwerk ist nach Projektvorgabe eine geplante Option; Buchstabe, Kapazität, Archivpfad, Dateisystem und geprüfte Restore-Strategie sind `UNKNOWN`. Pfadgebundene alte Verifikation kann eine reine Verschiebung verweigern. [D.9 Speicher](</G:/TraWalpResearch/import-artifacts/f01-comprehensive-audit-20261123-v1/final_audit.md>) · [Freigabeplan](</G:/TraWalpResearch/import-artifacts/f01-comprehensive-audit-20261123-v1/cleanup_authorization_plan.md>)

## 10. Historical Security & PIT Correctness

**Task 9.5.** Für die D.6-Erweiterung ist kein vollständiger technischer oder PIT-qualifizierter C1-Start belegt. Die 92 datierten Security-Zeugen tragen keine durchgehende Epoch- oder Corporate-Action-Freigabe. Die U0-Rekonstruktion 2022–2026 bleibt separat gebunden. [Task-9.5-Audit](f01_task95_historical_data_readiness.md)

Task 9 prüft den E.13-Übergang direkt vor und an der Filing-Acceptance mit exakt zugelassenen Originalwerten und der Receipt-Version. Der neue V1-Pfad sperrt Amendments und bietet keine E.12-Fallbacks. Spätere Supersession, dynamisches Reporting-Entity-Mapping und die Zuordnung zur handelbaren Security brauchen eigene Evidenz. [Task-9-Architektur](f01_historical_50_ingestion_architecture.md)

D.11 bindet die ausgewählten Filings an die historischen D.6-CIKs und Accessions. Die dazugehörigen D.6-Security-Blocker stehen pro ausgewähltem Filing im Batch. Markt- und Trading-Qualifikation folgen erst nach Audit zu Security, Zeit und Scope. [Batch-Entwurf](f01_historical_50_research_preparation.md)

CIK, handelsfähige Security-ID, Share Class, Symbol, Börse, effektives Intervall, Beobachtungszeit und Corporate Action sind unterschiedliche Felder. Ein heutiger Typ wird nicht rückdatiert. Eine geschlossene Epoche braucht belastbare Grenzen und die Abgrenzung zu Name-Change, Fusion, Delisting, Austausch oder Wiederverwendung. Bei JATT sind unterschiedliche CIK-Punkte 2023 und 2026 belegt, ZURA liefert zusätzliche Transition-Evidenz. Vollständige Epochen und historische Exits bleiben separate Prüfungen. ACN-Class-A-Belege schließen den wirtschaftlichen Issuer-Shares-Scope nicht. AAPL-Split- und TWTR-Merger-Stichproben zeigen, welche zusätzliche Action-/Exit-Provenienz benötigt wird. [D.6 Security-Audit](../reports/f01_historical_security_50_issuer_preflight_audit.md) · [D.8 Action-Belege](f01_alpaca_historical_sip_coverage.md)

Der U0→U1-Scope untersuchte 710 Identitäten und 305.537 Identity-Sessions. v3 erfasste 1.957, v4 im separaten U0-gebundenen Nachweis 12.472, rund 4,08 %. Fünfzig ausgewählte CIKs sind damit nicht pauschal abgedeckt. Der 2022-Universe-Audit fand 76 lokale Daily-Symbole außerhalb der heutigen Source-Auswahl. U1 ist geparkt, U0 bleibt der gebundene Forward-/Research-Kontext. [U1-Status](../reports/f_u1_research_parked_status.md) · [2022 Universe Audit](../reports/f_2022_historical_universe_audit_v1_audit.md)

E.5 behandelt für 2016–2019 268 und für 2020–20.11.2026 466 Issuer-Ereignisse. Eine Originalbeobachtung wirkt erst bei nachgewiesenem Filing, Acceptance und Periodenverfügbarkeit. Spätere Vergleichszahlen und Amendments dürfen die früher sichtbare Version nicht rückwirkend verändern. E.11 enthält im Audit keine Markt-, Asset-, Action- oder Security-Historienzeilen in den sechs geprüften Tabellen. Die rekonstruierte Fundamentals-Verfügbarkeit macht weder den historischen Anbieter-Snapshot noch Trading-PIT vollständig. [E.5](../reports/f01_historical_pit_qualification_audit.md)

## 11. Trading Strategies & Validation

| Familie | Belegte Rolle |
| --- | --- |
| A, B, C | Quality+Value, zusätzlich Opportunity, bzw. zusätzlich Timing. Etablierte Recovery-Vergleichsfamilie und Legacy-Kompatibilität. |
| D und E | Loss-aware Recovery mit zusätzlichem Veto bzw. Quality/Value mit Trend-Pullback. Opt-in Research. |
| F | Quality/Value plus Momentum-Gates. Frozen F/configured/C1 bindet Capacity 1, dokumentierte ATR-/Target-/Hold-/Reentry- und Kostenannahmen. Der eingefrorene Entwicklungscutoff ist 12.08.2026. |
| Intraday-Hybrid und Dynamic Execution | C-Auswahl mit eingefrorenem F0-Control, Intraday-Management-/Entry-Varianten und eine getrennte F-Intraday-Research-Linie. F1/F2/F4-Evidenz bleibt archiviert. Kein Austausch des Daily-Champions. |
| Robustheit, Regime, Kapazität, Lifecycle | Bestehende Sensitivitäten und Capacity 1/2/3/5, SPY-Regimevarianten, Holds/Exits, Slippage und Kosten. Varianten bleiben Hypothesen mit eigener Entwicklungs- und Forward-/OOS-Abgrenzung. |
| Shadow Observation und Economic | Observation ist mode-spezifisch. Economic Policy B erlaubt bei ausschließlich Ranking-Unvollständigkeit kontrolliertes Management bzw. alte Pending-Intents und sperrt neue Signal-Intents. STRICT und andere Fehler lösen weiterhin Fail-closed aus. |

Die originalen Development-Perioden, Kostenannahmen und Feed-Caveats sind maßgeblich. Der eingefrorene U0-Doppelreplay meldet zwei deterministische wirtschaftliche Läufe mit jeweils 3.429 Signal-Candidate-Events und 143 abgeschlossenen Positionen. Das ist Reproduzierbarkeit auf diesem Snapshot. Der spätere Alpha-Audit weist keinen eigenständigen prospektiv validierten Quality-/Value-Beitrag nach. Abweichende SIP-/IEX-, Universe-, Exit- und Ausführungsbasis begrenzt eine Übertragung auf reale Fills. [Strategie-Registry](strategy-research-lifecycle.md) · [Robustheit](f-configured-robustness-validation.md) · [U0-Doppelreplay](../reports/f_reconstructed_u0_double_replay_audit.md) · [Alpha-Audit](../reports/f_champion_strategy_improvement_audit.md) · [Policy B](../reports/shadow_policy_b_implementation_audit.md)

Der Champion-Audit bindet eine Position, 100 % Notional-Obergrenze, 1 % Risikobudget pro Trade, Stop-Distanz `min(2 × ATR14, 10 % des Entry-Fills)`, 12 % Ziel, maximal zehn Sessions, 5 bps modellierten Slippage je Fill und 0 bps Kommission. Reentry bleibt im damaligen Vertrag ohne Cooldown erlaubt. Diese Parameter sind Bestandteil der eingefrorenen Referenz. [Champion-Konsolidierung](../reports/champion_consolidation_audit.md)

## 12. Completed Development Milestones

1. Lokaler Sync, Screening-/Ranking-Engine und A–F-Referenzen sind implementiert; der September-Freeze definiert F/configured/C1.
2. Der rekonstruierte U0-Stand erhielt auf seinem Snapshot exakte Replay-Evidenz. Forward-SIP und Shadow-Admission wurden getrennt vom historischen Legacy-Replay aufgebaut.
3. F01 konservierte alle Occurrences samt Legacy- und Forward-Projektionen. G0–G3 und der später versiegelte G6-PASS belegen Migration bzw. Recovery. V2 trennt die Freigaben in A–D.
4. Finalisierter Bulk, Canonical-Bootstrap, Sieben-Emittenten-Import, Arelle-Originalbelege, E.11-Handoff, E.12-Review und E.13-Parität sind mit vorhandenen Artefakten belegt.
5. D.6 band die historische Kohorte, D.7 prüfte die Machbarkeit kostenloser Quellen, D.8 maß die lokalen SIP-/Warm-up-Gaps. D.9 band Storage-Kandidaten an einen separaten Freigabeprozess.
6. D.11 legte den Copy-Rehearsal-Pfad still und veröffentlichte den Offline-Batch-Vertrag und die synthetisch geprüfte Duplicate Assurance.
7. Task 9 implementierte die einzeln ausführbare Pipeline vom D.11-Teilmanifest bis zum Filing-Receipt und der E.13-Grenzprobe.

Die [Task-Timeline](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/task_timeline.csv>) identifiziert 47 Task-IDs einschließlich dieses Tasks. Git-Implementierungsbelege, validierte Resultate und der unter bereits vergebener D.9-ID erfasste Abbruch werden separat gewertet. Ältere Git-Meilensteine ohne eindeutige Task-ID bleiben datierte Chronologie.

## 13. Outstanding Issues & Blockers

| Prio | Offener Nachweis und Abnahmekriterium |
| --- | --- |
| P0 | **Kapazität der neuen Lineage.** Task 9 erfasste 190.956.511.232 freie Bytes auf G: und 127.081.201.664 auf F:. Physische Trennung und Restore-Fähigkeit von F: brauchen eine eigene Prüfung. |
| Historisch | **G0–G6-Reproduzierbarkeit.** Alte Bindungen und Resultate bleiben dokumentiert. Prüfungen mit entfernten Originaldatenbankbytes sind lokal nicht mehr ausführbar. |
| Historisch | **Reale G5-Abnahme.** Der historische 8F.3C-Gate bleibt unbelegt. Die neue Lineage braucht ihre eigene Receipt-/Resume-Abnahme. |
| P1 | **Original-Accessions.** 2.613 fehlen im Task-9.4-Scope, davon 2.585 in 138 Task-9.6-Planblöcken. DTS, Accounting und PIT separat qualifizieren. |
| P0 | **Accounting-Lücken.** CAT-DTS und OCF, ACN Issuer Shares, NVDA PPE-only, Debt/ROIC sowie betroffene Filing-Versionen mit passenden Quellen klären. |
| P0 | **Provider- und Speicherrechte.** Kostenfreien Alpaca-Scope, Archive-Rights und konkreten authorized Batch dokumentieren. |
| P0 | **SIP und Warm-up.** SPY, 300 Mindest-/320 Ladevorlaufsessions und nach Security qualifizierte Daily-Lücken schließen. Geeignete Quellen vor 2016 und aktuelle Rechte bleiben unklar. |
| P0 | **Security-Epochen und Exits.** Historische Klassen, Symbolwechsel/Reuse, Listing-/Delisting-Grenzen, Merger-/Exit-Werte und Adjustments qualifizieren. |
| P1 | **50er-Neubestand und PIT-Trading.** Eigenständige Lineage und zeitgebundenen Market-/Fundamental-Join abnehmen; keine Übertragung des Siebener-Passes. |
| P1 | **Champion Readiness/Produktion.** Forward-Ranking, aktuelle STRICT-/Shadow-Admission sowie unabhängige OOS-/Execution-Evidenz separat nachweisen. |

[Maschinenlesbare Blockerliste](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/open_blockers.csv>)

## 14. Technical Debt & Known Risks

| Dimension | Reife und Begründung |
| --- | --- |
| Softwarearchitektur | **Klar abgegrenzt.** Writer, Reader, Provider, Replay und Shadow haben getrennte Verträge. Ausbau sollte diese Bindungen wiederverwenden. |
| Codequalität | **Gut prüfbar im Kern.** D.9 fand 153 AST-Gleichheitsgruppen und 11 scheinbar private unreferenzierte Definitionen, aber keinen nachweislich sicheren Löschfall. Alte Gate-/Fixture-Pfade haben Windows-Pfadlängen- und Git-Drift-Grenzen. |
| Datenverfügbarkeit | **Teilweise.** Reales 50er-Staging, Original-Caches und getrennte Marktserien. Vor-2016-SIP-Alternativen, Shares-/OCF-PIT und durchgängige Security-Epochen bleiben offen. |
| Datenqualität | **Heterogen nach Quelle.** Canonical und Originale sind punktuell technisch und fachlich geprüft. Legacy fehlt Feed-Provenienz pro Bar, und Reportingscopes sind oft kontextabhängig. |
| Historische Reproduzierbarkeit | **Scope-gebunden.** G0–G6-Ergebnisse bleiben historische Nachweise. Vollständige Replays der entfernten Datenbanken sind lokal nicht mehr verfügbar. U0 bleibt separat an seinen eigenen Snapshot gebunden. |
| PIT-Korrektheit | **Teilweise belegt.** Historisches E.13 für sieben Issuer. Task 9 prüfte den neuen engen Filing-Witness-Pfad synthetisch. Reale Zeugen, Amendment-Supersession und historische Markt-Joins fehlen für die Expansion. |
| Strategische Validierung | **Reproduzierbare historische Research-Ergebnisse.** Reine OOS-Aussage, Feed-Wechselwirkung und inkrementelles Alpha sind offen. |
| Trading-Ausführbarkeit | **Gesperrt.** Die vorhandene Modell-Execution und Shadow-Infrastruktur belegen keine Broker-, Inception- oder aktuelle operative Freigabe. |
| Betriebssicherheit | **Konservativ im Scope.** Native Pins, Atomic Receipts, Fail-closed und Guard-Verträge sind implementiert. Bei anderer Dateiidentität greifen Request-/Restore-Bindungen; eine blinde Übernahme alter Pending-Requests ist gesperrt. |
| Dokumentationsqualität | **Reichhaltig, historisch verteilt.** Unterschiedliche Datenstände und Scope-Bezeichnungen können widersprüchlich wirken. Der neue Index kodiert Belegpriorität und offene Lücken. |

## 15. Development Roadmap

| Reihenfolge | Prio und Arbeit | Abhängigkeit und prüfbares Ergebnis | Freigabe / offline |
| --- | --- | --- | --- |
| 0 | P0 **Task 9.5 abgeschlossen** | Realer Offline-Audit und Beschaffungsplan. [Task-9.5-Audit](f01_task95_historical_data_readiness.md) | Separate Autorisierung je Datenblock. |
| 1 | P0 **Task 9.6 SPY und Provider** | Rechte, exakten Scope, Warm-up und Budget für den ersten Block binden. [Task-9.5-Audit](f01_task95_historical_data_readiness.md) | Separate Autorisierung je Datenblock. |
| 2 | P0 **Task 9.6 Security und Issuer-Daily** | Epochen, Actions und Exits klären. Daily-Plan darauf begrenzen. [Task-9.5-Audit](f01_task95_historical_data_readiness.md) | Separate Autorisierung je Datenblock. |
| 3 | P1 **Task 9.6 SEC-Originalblöcke** | Lokal wiederverwenden, Accessions und SHA binden, Resume prüfen. [Task-9.5-Audit](f01_task95_historical_data_readiness.md) | Separate Autorisierung je Datenblock. |
| 4 | P1 **Isolierten 50er-Aufbau und fachliche Qualifikation** | SEC/Arelle/Accounting wiederverwenden, eigene Canonical-Lineage und Marktshards prüfen, E.13-Erweiterung und reproduzierbaren PIT-Join abnehmen. | Neue Datenbank-/Import- und Review-Freigabe. Bereits vorhandene Evidenz offline prüfen. |
| 5 | P1 **Strategien und operative Entscheidung** | Erforderliche Coverage und Universe/PIT bestanden. Fixierte A–F-/F-Scopes, costs, echtes neues OOS/Robustheit, auf spätere Readiness begrenzter Entscheidungsauftrag. | Kein Replay oder Betrieb ohne separaten Scope und Gates. |

## 16. Decisions & Superseded Approaches

- **F bleibt Frozen Champion.** Intraday-Control und neuere Varianten, Kapazitäts- und Exit-Hypothesen liefern getrennte Research-Evidenz.
- **Parallel Research** ersetzt den stillgelegten Copy-Rehearsal-Pfad. Canonical A-V1, der gemeinsame Writer und verfügbare historische Belege bleiben erhalten. Die 50er-Lineage braucht eine eigene geprüfte Zulassung. [Retirement](f01_legacy_migration_retirement.md) · [Deduplication](f01_canonical_deduplication_contract.md)
- **Scoped V2** folgt dem Vorschlag aus 8F.2D. Alte G4-/Supplement-Status bleiben als historische Resultate sichtbar. G6 ist ein separater PASS.
- **E.13 hat den frühen Pending-Stand überwunden.** Die spätere freigegebene Review-Datei und 77 Read-only-Proben belegen die drei Regeln. Diese Freigabe gilt für die ursprüngliche Reader-/Runtime-Bindung.
- **Source/Scope getrennt.** 26.102 vs. 26.184 Facts sind getrennte Dataset-Lineages. 320 SIP/all vs. 1.570 Legacy-SPY-Sessions und v3 vs. v4 Identity-Sessions sind abweichende Quellen/Versionen und dürfen nicht kombiniert werden.
- **Frühe README-Einstiege** beschreiben teils einen Stand ohne Shadow-CLI. Die tatsächliche Implementierung und neuere Inception-Audits sind für den heutigen technischen Vertrag maßgeblich. Operation bleibt blockiert.
- **D.9 Abbruch versus Abschluss.** Die unvollendete Runner-Aufgabe und der abgeschlossene Storage-Audit tragen dieselbe Text-ID. Die zuletzt vorhandene Coverage-Datei ist kein Download-Runner.

[Entscheidungslog](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/decision_history.csv>) · [Widerspruchsmatrix](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/documentation_conflicts.csv>)

## 17. Task Completion Log

Historische Task-IDs und Commit-/Auditbindungen stehen in der [vollständigen Timeline](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/task_timeline.csv>). Ein Commit ohne explizite Ergebnisprüfung ist dort als Implementierungsbeleg markiert. Das alte D.9-Runner-Vorhaben ist als Abbruch mit offenem Ergebnis getrennt.

#### 8F.3D.10 – Historischer Projektaudit und Living Project State

- **Datum:** 2026-11-23
- **Git-Stand:** `SEC-integration`, Ausgangs-HEAD `4c98ac0e04ba2c6b72402fc96e9e6a94e4e712b9`; Dokumentation uncommittet.
- **Status:** COMPLETED
- **Ziel:** Vorhandene Historie und heutige Evidenz in einem dauerhaft gepflegten Projektstand zusammenführen.
- **Änderungen:** Zentrales Projektdokument, Pflegeanweisung, Root-`AGENTS.md` und ein separater, reproduzierbarer Quellen- und Evidenzsatz. Historische Scopes und abweichende Entscheidungen bleiben getrennt nachvollziehbar.
- **Tests:** Quellen-/Link- und Tabellenkonsistenz, Code-Inventar-Fingerprint, fünf V2-Siegelabgleiche, Ruff für die neuen Offline-Quellenhelfer und `git diff --check`. Kein historischer Replay oder G0–G6-Lauf.
- **Ergebnis:** `TRAWALP_HISTORICAL_PROJECT_AUDIT_COMPLETE` und `LIVING_PROJECT_STATE_INITIALIZED` für den zugänglichen dokumentierten Scope.
- **Neue Blocker:** Keine fachliche Freigabeänderung. 16 ACL-gesperrte alte Fixture-Unterbäume sind als Quellenlücke erfasst.
- **Geschlossene Blocker:** Fehlende zentrale Orientierung und Pflegevorgabe.
- **Nächster Schritt:** Zweites Laufwerk und einen rein lesenden Archiv-/Restore-Vertragsplan für geschützte G0–G6-Evidenz prüfen.
- **Evidenz:** [Baseline-Audit und Manifest](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1>), [Pflegeanweisung](trawalp_project_state_maintenance.md).

#### 8F.3D.11 – Legacy Migration Retirement, Canonical Deduplication Assurance & 50-Issuer Research Preparation

- **Datum:** 2026-11-24
- **Git-Stand:** `SEC-integration`, sauberer Ausgangs-HEAD `1a869020d0f4ef54c1437b5fbfa2becad6c0a5ba`; D.11 uncommittet.
- **Status:** COMPLETED
- **Ziel:** Copy-Rehearsal beenden, Canonical-Deduplikation absichern und die nächste D.6-Expansion offline vorbereiten.
- **Änderungen:** 393 Dateien mit 39.909.268 logischen Dateibytes entfernt. Drei aktive Research-CLIs auf den neutralen Evidence-Helfer umgestellt. Gleichschlüsselige Widersprüche innerhalb eines Requests werden atomar abgewiesen. Versionierter Batch-Preflight und isolierte Duplicate Assurance ergänzt.
- **Tests:** 134 gezielte Offline-Regressionen, Ruff, Import- und CLI-Help-Smoke, `git diff --check`, Linkprüfung der neuen und aktualisierten Quellen, Manifest- und Artefakthashes sowie synthetischer read-only Duplicate-Assurance-Lauf. Vollständiger Helfervergleich im identischen Baum vor der Entfernung.
- **Ergebnis:** `F01_LEGACY_MIGRATION_RETIRED`, `F01_CANONICAL_DEDUPLICATION_ASSURANCE_PASSED` im synthetisch geprüften Umfang und `F01_HISTORICAL_50_BATCH_PREPARATION_READY` als Offline-Plan. 2.352 fehlende Originalverzeichnisse im geprüften Cache. Review-Batch mit 20 Accessions für NKE, BBBY, HEI, ALOT und BG.
- **Neue Blocker:** 336 im Original-Cache gelistete Paare unverifiziert. 254 nicht zur Löschung freigegebene Inventarpositionen mit offenen Bindungen oder Zugriffsfragen blieben erhalten. Import braucht ausdrückliche Autorisierung, Source-/Accounting-Admission und vollständige DTS-Evidenz.
- **Geschlossene Blocker:** Der aktive Copy-Rehearsal-Pfad ist stillgelegt. Offline-Planung für die 50er-Kohorte ist ausführbar.
- **Nächster Schritt:** 8F.3D.12. Ersten autorisierten Teilumfang beschaffen, offline per Arelle prüfen und nach separater Admission in die neue isolierte Lineage schreiben.
- **Evidenz:** [Retirement](f01_legacy_migration_retirement.md), [Canonical-Vertrag](f01_canonical_deduplication_contract.md), [50er-Preflight](f01_historical_50_research_preparation.md).

#### Task 9 – Historical SEC Acquisition & Canonical Research Database Ingestion

- **Datum:** 2026-11-24
- **Git-Stand:** `SEC-integration`, sauberer Ausgangs-HEAD `ec6a8158564bdf864295dea1479f132104d12543`; Task 9 uncommittet.
- **Status:** COMPLETED für den beschriebenen V1-Entwicklungs-Scope.
- **Ziel:** Einen manuell ausführbaren, isolierten Filing-Pfad für den ersten D.6-Teilbatch entwickeln.
- **Änderungen:** Neue phasenbasierte CLI für `select`, `download`, `verify`, `taxonomy`, `extract`, beide Review-Entwürfe, Bootstrap-Manifest, `import` und `reader`. Typisierte Zulassungen, persistente SEC-Pacing-Evidenz, atomare Receipts, Journal-Recovery und PowerShell-Runbook ergänzt.
- **Tests:** 85 gezielte Offline-Tests, darunter 11 Task-9-Tests. Die vorhandene Arelle-2.47.5-Runtime wurde für synthetische Filings lesend verwendet. Ruff, CLI-/Import-Smoke, PowerShell-Parser, Artefakt- und Schemaabgleich, Links und `git diff --check` geprüft. Kein realer Abruf, historischer Arelle-Lauf, realer Bootstrap/Import, Markt-Backfill, Backup oder Strategiereplay.
- **Ergebnis:** `TASK9_HISTORICAL_50_INGESTION_PIPELINE_IMPLEMENTED` und `TASK9_MANUAL_EXECUTION_READY` für den V1-Scope. Der erste NKE-Teilumfang bleibt `REAL_DATA_ACQUISITION_AND_INGESTION_PENDING_MANUAL_EXECUTION`.
- **Neue Blocker:** Neue Datei- und DTS-Evidenz, getrennte Source-/Accounting-Reviews und manuelle Autorisierungen fehlen. Amendments und komplexere Metriken bleiben vorerst außerhalb der V1-Zulassung.
- **Geschlossene Blocker:** Manuell steuerbarer und synthetisch geprüfter Ablauf vom gebundenen Teilbatch bis zum Filing-Receipt und zur Acceptance-Grenze liegt vor.
- **Nächster Schritt:** Read-only `scripts/run_f01_historical_50_manual.py select --accession 0000320187-16-000242` ausführen. Danach die Auswahl für den separat genehmigten ersten Abruf prüfen.
- **Evidenz:** [Runbook](f01_historical_50_manual_ingestion_runbook.md) · [Task-9-Belegsatz](</G:/TraWalpResearch/import-artifacts/f01-task9-ingestion-preparation-v1/task9_final_report.md>).

#### Task 9.1 – Repair Actual DTS Dependency Closure (NKE)

- **Datum:** 2026-11-24
- **Git-Stand:** `SEC-integration`, Basis-HEAD `7c984f46f6859b17a952a00a2b6de90f0002b3bd`, sauberer Ausgangsbaum, Reparatur uncommittet.
- **Status:** COMPLETED für die DTS-Korrektur.
- **Ziel:** Den NKE-Blocker beheben und die vollständig gebundene DTS-Closure wiederholbar machen.
- **Änderungen:** Passende, hashbenannte Quarantäne liefert die tatsächlichen DTS-URIs zur lokalen Byteprüfung. Extraktion verlangt URI und SHA-256 gemeinsam. Eine neue Selection kann im selben Job einen eigenen Verifikationsbericht erhalten.
- **Tests:** Vier gezielte Offline-Tests, Ruff, `git diff --check`, lesender Abgleich der NKE-Closure und bestehender Report-Hashes. Kein erneuter realer Arelle-Lauf, Download oder Datenbankzugriff.
- **Ergebnis:** 23 externe Abhängigkeiten mit lokalem Hash-Nachweis. Die 22 gemeinsamen Hashes und die drei geprüften historischen Reports blieben gleich.
- **Neue Blocker:** Der Candidate-Fingerprint erfordert eine neue Selection. Erneute reale Extraktion sowie Source-/Accounting-Admission stehen aus.
- **Geschlossene Blocker:** Der Preflight kann die zuvor übersehene Abhängigkeit `xbrldi-2006.xsd` aus dem vorhandenen Quarantänebericht ableiten.
- **Nächster Schritt:** Offline-Wiederanlauf mit neuer Selection und demselben NKE-Job.
- **Evidenz:** [Runbook](f01_historical_50_manual_ingestion_runbook.md) · [Pipeline](../scripts/run_f01_historical_50_manual.py) · [Regressionen](../tests/test_f01_task9_manual_ingestion.py).

#### Task 9.2 – Automated Evidence-Based Admission

- **Datum:** 2026-11-24
- **Git-Stand:** `SEC-integration`, Basis-HEAD `20fc366dcffb3cdfb3cd95ae3cba4104d58e82db`, sauberer Ausgangsbaum, Änderungen uncommittet.
- **Status:** COMPLETED im V1-Scope, neuer realer Auto-Import ausstehend.
- **Ziel:** Einen separaten deterministischen Admission-Pfad mit belegbarer Original-Provenienz schaffen.
- **Änderungen:** `F01_AUTOMATED_ADMISSION_V1` bindet Original-, DTS- und Extraction-Evidenz, Code und Ruleset. Der Preflight publiziert auf Wunsch einen unveränderlichen Batchbericht. Auto-Import und Reader nutzen denselben Canonical-Pfad und erfordern eine aktuelle Auswahl sowie ein separates Importgate.
- **Tests:** 24 gezielte Offline-Tests, Ruff und `git diff --check`. Lesender NKE-Abgleich mit zwei identischen Observation-IDs und Fact-Modellen. Zwölf Altdateien blieben hashgleich. Kein Netzwerk, Bootstrap, Import oder Datenbankzugriff.
- **Ergebnis:** Ein NKE-Filing und zwei von zehn V1-Kandidaten auto-qualifiziert. Acht historische oder dimensionierte Alternativen ausgeschlossen. Der NKE-Beleg ist wegen der historischen Auswahl nur für Read-only-Audit geeignet.
- **Neue Blocker:** Ein neuer disjunkter Batch benötigt neue aktuelle Selection/Extraktion und eine separate Importfreigabe. DB-Receipt, Version und Reader-PASS des manuellen Laufs bleiben nutzerberichtete Baseline.
- **Geschlossene Blocker:** Evidence-Based Admission als eigenständige, maschinell nachvollziehbare Entscheidung vorhanden.
- **Nächster Schritt:** Einen neuen D.11-Teilbatch offline prüfen, Quarantäne bewerten und erst nach separater Freigabe den isolierten Append und E.13 prüfen.
- **Evidenz:** [Runbook](f01_historical_50_manual_ingestion_runbook.md) · [Ruleset](../scripts/f01_task9_auto_admission.py) · [Tests](../tests/test_f01_task9_auto_admission.py).

#### Task 9.3 – Next Real Filing Auto-Admission & Append Preparation

- **Datum:** 2026-11-25
- **Git-Stand:** `SEC-integration`, Basis-HEAD `15d02c5b9d1ae31f1ad8363839d6eb66ac2a8097`, sauberer Ausgangsbaum. Dokumentation uncommittet.
- **Status:** COMPLETED für die Vorbereitung.
- **Ziel:** Einen zweiten, disjunkten D.11-Originalfall und seine sichere spätere Aufnahme vorbereiten.
- **Änderungen:** NKE `0000320187-16-000264` ausgewählt. Neue Selection und hashbenannter Beleg im eigenen Job `f01-task9-nke-02`. Keine Codeänderung.
- **Tests:** Vier gezielte Offline-Tests, Ruff, `git diff --check`, Hashvergleich und read-only CLI-Preflights. Isolierte Lineage mit Dateisperre, `mode=ro&immutable=1` und `query_only` geprüft.
- **Ergebnis:** Bootstrap-SHA, initialer Receipt, Version 2, zwei Facts, zwei Occurrences, null Storage-Verletzungen und fehlende neue Accession bestätigt. Keine Mutation. E.13 bleibt nutzerberichtete Baseline.
- **Neue Blocker:** Originale fehlen. Exact DTS, Source- und Accounting-Evidenz für das neue Filing stehen aus. Kein Abruf, Bootstrap, Import oder Replay ausgeführt.
- **Geschlossene Blocker:** Konkreter zweiter Scope, gebundene Selection und überprüfte strukturelle Append-Voraussetzungen liegen vor.
- **Nächster Schritt:** Abruffreigabe separat einholen. Danach Verification, Offline-DTS, Arelle und Auto-Admission ausführen.
- **Evidenz:** [Task-9.3-Runbook](f01_task93_nke_append_runbook.md) · [Job und Belege](</G:/TraWalpResearch/import-artifacts/f01-task9-ingestion-preparation-v1/f01-task9-nke-02>).

#### Task 9.4 – Historical 50 Bulk Fundamentals Ingestion

- **Datum:** 2026-11-25
- **Git-Stand:** `SEC-integration`, sauberer Ausgangs-HEAD `b58e717677d44ccd8f378411a13bda8e4a9a6a53`. Task 9.4 uncommittet.
- **Status:** PARTIAL. Vollständiger realer Offline-Preflight und synthetische Veröffentlichung abgeschlossen. Reale Staging-Veröffentlichung braucht separate Importfreigabe.
- **Ziel:** Historische Companyfacts für alle 50 D.6-Emittenten ab 2010 für statistische Forschung isoliert bereitstellen.
- **Änderungen:** CLI und Regeln für Rohbeleg, strikte Normalisierung, Periodenprüfung, Deduplikation, Quality/Coverage und Issuer-Resume implementiert. Separates Staging-Ziel und reine Beschaffungsplanung für fehlende Originale dokumentiert.
- **Tests:** Vier gezielte Offline-Tests einschließlich synthetischer 50er-Veröffentlichung und Resume. Ruff und `git diff --check`. Vollständiger lesender 50er-Preflight auf lokalen Archiven und Berichtshash-/Coverage-Abgleich.
- **Ergebnis:** 120.477 retrospektive Kandidaten, 17.000 Coverage-Zeilen und 130 Beschaffungsgruppen. Keine reale SQLite-Erstellung, Downloads oder Canonical-Schreiboperationen.
- **Neue Blocker:** Separate Staging-Importfreigabe fehlt. Originale, PIT und breiterer Download-Vertrag bleiben separate Gates.
- **Geschlossene Blocker:** D.6-gebundene Bulk-Auswertung und Staging-Resume im synthetischen Scope nachgewiesen.
- **Nächster Schritt:** Wenn autorisiert, den gebundenen Bulk-Staging-Import gemäß Runbook ausführen.
- **Evidenz:** [Bulk-Staging-Runbook](f01_task94_historical50_bulk_staging.md) · [Coverage](</G:/TraWalpResearch/import-artifacts/f01-task94-historical50-preflight-v1/coverage.csv>).

#### Task 9.5 – Historical Data Completeness, Champion Readiness & Acquisition Planning

- **Datum und Git** 2026-11-25, SEC-integration, sauberer Ausgangs-HEAD b3a1944d0c64925f2cee6f2cdf7defcdd12932cf.
  Arbeitsänderungen uncommittet.
- **Änderung** Offline-CLI für Quelleninventur, C1-Anforderungsmatrix, Coverage und priorisierten Task-9.6-Plan.
- **Tests** Neun gezielte Offline-Regressionen, Ruff, git diff --check, CLI-Help und SHA-256-Abgleich
  für alle elf im Real-Audit publizierten Ausgabedateien.
- **Ergebnis** 50 Emittenten und SPY geprüft. 2.613 Originale fehlen. 2.585 sind in SEC-Planblöcken.
  217.076 referenzielle Emittenten-Daily-Lücken und 4.238 SPY-Lücken. v2 erfasst 320 exakt
  vorherige Sessions und erhält v1 als Vorgängerbeleg. Keine Datenbankänderung, Backtests oder Netzwerkanfragen.
- **Blocker** Kein belegter vollständiger technischer oder PIT-Start für D.6. Historische Epochen,
  Kapitalmaßnahmen, Original-Fundamentals, Vor-2016-Quellen und Providerrechte offen.
- **Folgetask** Task 9.6 mit lokalem Reuse und separat autorisiertem SPY-/Warm-up-Block beginnen.
  [Task-9.5-Audit](f01_task95_historical_data_readiness.md)

## 18. Evidence & Source Index

| Thema | Maßgebliche Quellen |
| --- | --- |
| Repository- und historische Arbeitswege | [F01-Architekturindex](f01_research_architecture_index.md), [D.9 Audit](</G:/TraWalpResearch/import-artifacts/f01-comprehensive-audit-20261123-v1/final_audit.md>) |
| Strategy, Portfolio, Kosten und Regime | [Lifecycle](strategy-research-lifecycle.md), [Champion](../reports/champion_consolidation_audit.md), [Capacity](f-configured-capacity-research.md), [Regime](f-regime-capacity-research.md), [Alpha](../reports/f_champion_strategy_improvement_audit.md) |
| Inception, Forward und Economic | [Shadow-Vertrag](f-shadow-execution-v1.md), [Forward](../reports/f_forward_sip_shadow_contract_audit.md), [Reales Observation-Gate](../reports/shadow_pre_inception_final_audit.md), [Policy B](../reports/shadow_policy_b_implementation_audit.md) |
| D.11 Retirement und Expansion | [Retirement](f01_legacy_migration_retirement.md), [Canonical-Vertrag](f01_canonical_deduplication_contract.md), [50er-Vorbereitung](f01_historical_50_research_preparation.md) |
| Task 9 manuelle Aufnahme | [Runbook](f01_historical_50_manual_ingestion_runbook.md), [Architektur](f01_historical_50_ingestion_architecture.md), [Source Review](f01_historical_50_source_admission.md), [Accounting Review](f01_historical_50_accounting_admission.md) |
| Task 9.3 zweites NKE-Filing | [PowerShell-Ablauf](f01_task93_nke_append_runbook.md), [Selection und Read-only-Belege](</G:/TraWalpResearch/import-artifacts/f01-task9-ingestion-preparation-v1/f01-task9-nke-02>) |
| Task 9.4 Bulk Fundamentals | [Staging-Vertrag und PowerShell](f01_task94_historical50_bulk_staging.md), [50er-Preflight](</G:/TraWalpResearch/import-artifacts/f01-task94-historical50-preflight-v1/preflight.json>) |
| Task 9.5 Historische Readiness | [Task-9.5-Audit](f01_task95_historical_data_readiness.md), [v2-Manifest](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/manifest.json>), [Task-9.6-Plan](</G:/TraWalpResearch/import-artifacts/f01-task95-historical-data-readiness-v2/acquisition_plan.json>) |
| G0–G6 historisch | [Copy-Preparation](f01_migration_preparation_script.md), [G3](f01_migration_rehearsal_script.md), [G4–G6 V1](f01_migration_acceptance_script.md), [Acceptance V2](f01_acceptance_v2.md), [Approval](f01_acceptance_v2_approval.json) |
| SEC, Arelle, Canonical und E.13 | [Bulk](f01_sec_bulk_download.md), [Filing](f01_original_filing_evidence.md), [Pilotimport](f01_sec_bulk_pilot_import.md), [E.13](../reports/f01_canonical_research_reader_audit.md), [E.5](../reports/f01_historical_pit_qualification_audit.md) |
| Historical Market und Security | [D.6](../reports/f01_historical_security_50_issuer_preflight_audit.md), [D.7](../reports/f01_historical_market_feasibility_audit.md), [D.8](f01_alpaca_historical_sip_coverage.md), [U1](../reports/f_u1_research_parked_status.md) |
| Quellenbestand der Baseline | [Inventar](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/source_inventory.csv>), [Belegmatrix](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/evidence_matrix.csv>), [Baseline-Manifest](</G:/TraWalpResearch/import-artifacts/f01-project-state-baseline-v1/baseline_manifest.json>) |

## 19. Audit Methodology & Limitations

Der Offline-Lauf inventarisierte und las vollständig 169 zugängliche Projekt-Markdowns, 160 im Repository und neun im Research-Baum. Er klassifizierte den Inhalt einschließlich Überschriften, Entscheidungen, Validierungsangaben, Status-/Gap-Aussagen, Querverweisen und ursprünglichen SHA-256-Werten. Der strukturierte Inhalt ist je Quelldokument im Belegsatz erhalten. Die fachliche Synthese gleicht maßgebliche Aussagen mit Git, Code, gebundenen Berichten und nachfolgenden unabhängigen Results ab. Automatisch extrahierte Statuswörter sind keine Freigaben. Primärbelege haben Vorrang vor älteren Audits und Plänen. Unterschiedliche Daten-/Zeit-/Freigabescopes bleiben vergleichbar beschrieben und getrennt.

16 ACL-gesperrte Fixture-Unterbäume unter `reports/` wurden als nicht lesbar protokolliert. Kein einzeln identifizierter Markdown-Pfad war unlesbar. Die Anzahl etwaiger Dokumente in den gesperrten Unterbäumen ist `UNKNOWN`. Installierte Drittanbieterumgebungen, Git-Interna, neue Auditdateien und synthetische/duplizierte Test-Checkouts waren außerhalb des fachlichen Dokumentationsscopes. Zwei D.9-Textdateien enthalten bereits beschädigte Zeichen; numerische Kernaussagen wurden gegen ihre CSV-/JSON-Evidenz abgeglichen. Vollständige nicht lokale Chat-Historien wurden nicht als Quelle vorausgesetzt.

Die D.10-Baseline prüfte die fünf kleinen Originalseals gegen V2. Historische Code- und Runtime-Bindungen bleiben dokumentiert. Bytegebundene Rehearsal-Prüfungen sind mit den lokal entfernten Originalmitgliedern nicht reproduzierbar. D.11 öffnete keine geschützte Datenbank und führte keine Downloads, Provider-Calls, Strategiereplays oder G0–G6-Gates aus.

## 20. Next Recommended Task

**Task 9.6 · Historical Data Acquisition & Resume.** Den Task-9.5-v2-Plan als Evidenzeingang binden.
Vor dem ersten Provideraufruf Scope, Kosten, Rechte, Rate-Limits und Kapazität prüfen und separat
freigeben. Zuerst lokale Belege und SPY/Warm-up, danach geklärte Security-Epochen und Issuer-Daily.
Nach eigener Freigabe können SEC-Originalblöcke mit Cache-Reuse und Resume parallel vorbereitet werden.
[Task-9.5-Audit](f01_task95_historical_data_readiness.md)
