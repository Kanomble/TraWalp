# F03 — Champion Screen / Shadow Coverage Parity

## Ergebnis und Basis

**F03 behoben:** `screen_champion` übergibt jetzt die kanonische History-Coverage
an `qualified_forward_screen`. Screening, Coverage und Qualifizierung finden im
selben gepinnten SIP-Snapshot statt. Erst danach wird das Forward-Input-Manifest
gebildet und dessen Content-Hash an den Report gebunden.

Baseline: HEAD `c84b4facd41c216d1bf6989e6d07cdf452badb77`, Arbeitsbaum beim Start sauber.
Referenzen: `reports/full_codebase_strategy_audit.md` (F03) und
`reports/forward_sip_coverage_causal_audit.md`.
Nur synthetische lokale Datenbanken/Artefakte und gezielte Offline-Tests verwendet.
Keine reale DB-Mutation, Provider-/Netzwerkabfrage, Backtest-Ausführung, Journal-
Initialisierung/Run, Economic Activation oder Commit/Push.

## Root Cause und Red-Test

Shadow-Readiness liest bereits aus `LocalInputs.history_coverage` die offiziellen
300 Sessions und übergibt dieses Mapping an die Qualifizierung. `screen_champion`
rief dagegen nach Schließen des Snapshots `qualified_forward_screen` ohne Mapping
auf. Dessen kompatibler Fallback vertraute dem gespeicherten Row-Count.
Das Manifest war außerdem schon vor der Coverage-/Qualifizierungsphase geschrieben.

Eine ältere Ersatzzeile außerhalb des aktuellen Fensters kann eine fehlende
Session zahlenmäßig maskieren. Ebenso zählen Zero-Volume- oder OHLC-ungültige
Zeilen zur gespeicherten Menge, attestieren aber keine nutzbare offizielle Session.

Der erste neue Regressionstest wurde vor Produktionsänderung ausgeführt:
`test_f03_300_rows_do_not_prove_official_history_parity`.
AAA hat 300 gespeicherte SIP-Bars, eine fehlende interne offizielle Session und
eine ältere Ersatzzeile. BBB hat vollständige gültige SIP-Historie. Beide erhalten
im isolierten Screen-Fixture unveränderte gültige F-Features/Scores.

**Red: 1 fehlgeschlagen**, Exit-Code 1, 3,01 s.
Assertion: `('F03 row-count fallback publishes ranking', 2, 1)`.
Standalone publiziert zwei Kandidaten; Shadow meldet genau einen unresolved
Kandidaten (`AAA: MARKET_HISTORY_UNPROVEN`) und publiziert null Kandidaten.
Andere operative Blocker liegen im Fixture nicht vor. Der Test verdeckt die
Abweichung weder durch fehlende Fundamentals noch durch einen anderen F-Guard.
Rohprotokoll: `reports/f03_coverage_parity_red.txt`.

## Minimale Produktionsänderung

Ausschließlich `src/trading_system/champion.py::screen_champion`:

1. Produktions-Screener unverändert mit `use_market_snapshots=False` ausführen.
2. Effektive Session einmal bestimmen: `effective_market_session or as_of`.
3. Für alle Raw-Report-Symbole `inputs.history_coverage(..., session,
   config.data_quality.min_market_history_days)` aufrufen.
4. Dasselbe Coverage-Objekt explizit an `qualified_forward_screen` übergeben,
   solange der ursprüngliche Snapshot offen ist.
5. Danach bestehendes Manifest-/Hash-Verfahren ausführen und qualifizierten Report
   zurückgeben; kein erneutes Qualifizieren außerhalb des Snapshots.

Es gibt **keine zweite Coverage-Berechnung**. Dieselbe Methode, derselbe Parameter
(300), dieselbe SIP-View und dieselbe Qualifizierungsfunktion werden wie in
Shadow-Readiness genutzt. `shadow/coverage.py` existiert nicht; die kanonische
Implementation liegt in `shadow/inputs.py`.

Unverändert: `shadow/qualification.py`, `shadow/readiness.py`, `LocalInputs`,
AI-Export-Schema/-Builder, F-Scores/Gewichte, Schwellen, Momentum-/Trend-Gates und
Ranking. Der allgemeine Row-Count-Fallback für andere direkte Qualification-Caller
wurde nicht global entfernt; beide hier relevanten Forward-Pfade liefern jetzt
explizit die kanonische Evidenz.

## Manifest-Contract

Das bestehende content-addressierte JSON-Manifest unter
`<reports_path>/forward_inputs/<hash>.json` enthält weiterhin Feed-/Adjustment-
Contract, gelesene Market-/Fundamental-/Identity-Provenienz, effektive Session und
requested_as_of. Hinzu kommt über `inputs.provenance` die **tatsächlich verwendete**
`history_coverage` für alle Report-Symbole:

- status complete/incomplete;
- expected_sessions, usable_sessions und offizielle missing_sessions;
- listing_history=unknown, niemals ein aus fehlenden Bars erfundenes Listingdatum.

`LocalInputs.history_coverage` erzeugt diese Evidenz im offenen Snapshot und fügt
sie dessen Provenienz hinzu. Genau das zurückgegebene Objekt wird qualifiziert.
Ein Spy-Test prüft Objektidentität, eine Coverage-Abfrage, laufende Lesetransaktion
und `query_only=1` während der Qualifizierung. Erst danach wird canonical_json/digest
gebildet. Nach Verlassen des Snapshots ist die Connection geschlossen.

`ScreenReport.market_input_manifest_hash/path` binden dieses Dokument. Alle neuen
Manifesttests verifizieren den Hash nach Laden der Datei und die exakte Gleichheit
der normalisierten Coverage mit Shadow-Readiness. Requested-as-of am Wochenende
bleibt getrennt von der tatsächlichen offiziellen Analyse-Session.

Das AI-Dateiformat wird nicht erweitert: Die Kandidaten stammen aus demselben
qualifizierten Champion-Report; Manifest-Referenzen liegen weiterhin am ScreenReport
und im Forward-Input-Artefakt, nicht als neue Behauptung eines Mode-Admission-
Zertifikats im AI-Payload. Shadow-Manifeste haben zusätzlich operative Inputs wie
SPY/Readiness und müssen daher nicht denselben Gesamt-Hash wie der Standalone-Screen
besitzen. **Gemeinsame Coverage-/Ranking-Parität ist keine Gesamtmanifest-Gleichheit.**

Eine gezielte Reparatur im temporären Fixture verändert Coverage und Manifest-Hash;
das alte Manifest bleibt byte-identisch bestehen. Gleicher Hash mit manipuliertem
Dateiinhalt löst weiterhin `FORWARD_MANIFEST_HASH_COLLISION_OR_CORRUPTION` aus und
wird nicht still überschrieben. Keine historische Manifest-/Journalmigration.

## Paritätsmatrix

In allen Fällen: BBB mit vollständiger SIP/all-Historie, gültige aktuelle SPY-
Beobachtung und frische operative States. AAA variiert. Zeilenzähler und technische
Features sind im Hauptfixture getrennt, damit die Coverage-Qualifizierung isoliert
mit potenziell zulässigen F-Features erreicht wird.

| AAA-Fixture | Kanonische nutzbare Sessions | Ranking / AI | Economic-ready | Observation-ready |
| --- | ---: | --- | --- | --- |
| Vollständig gültige SIP-Historie | 300 | AAA, BBB; Score 90, alphabetischer Tie | ja | ja |
| 300 gespeicherte Bars, interne Session fehlt, ältere Ersatzzeile | 299 | ganzes Ranking zurückgehalten; AI keine Kandidaten | nein | ja |
| Historische Zero-Volume-Bar, 300 Zeilen | 299 | zurückgehalten | nein | ja |
| Historisch ungültiges OHLC, 300 Zeilen | 299 | zurückgehalten | nein | ja |
| Fehlende erste Session des benötigten Fensters | 299 | zurückgehalten | nein | ja |
| Nur aktuelle gültige SIP-Beobachtung | 1 | zurückgehalten | nein | ja |
| Keine AAA-Historie / aktuelle AAA-Bar fehlt | 0 | zurückgehalten | nein | ja |
| AAA deterministisch Quality-abgelehnt, ohne Historie | 0, wahrheitsgemäß incomplete | nur BBB; kein falscher Ranking-Block | ja | ja |
| Vollständige IEX-/Legacy-Historie, nur eine SIP/all-Bar | 1 | zurückgehalten; kein Fallback | nein | ja |
| Vollständige SIP/raw-Historie, nur eine SIP/all-Bar | 1 | zurückgehalten; kein Adjustment-Fallback | nein | ja |
| Aktuelle Zero-Volume-Bar | 299 | zurückgehalten | nein | ja |
| Aktuelle Session fehlt, ältere Ersatzzeile vorhanden | 299 | zurückgehalten | nein | ja |

Ungültiges OHLC wird ausschließlich durch gezielte SQL-Korruption einer temporären
Fixture erzeugt; der Produktions-Persist-Validator wird nicht abgeschwächt.

Ein unresolved AAA darf BBB nicht als Rang 1 nachrücken lassen. Bei vollständiger
Evidenz sind dagegen die gesamten Record-/Score-/Ranking-Ausgaben identisch mit
`champion_ranking(raw, config)` und Shadow. Eine aktuelle gültige Beobachtung ersetzt
weiterhin keine Historie. Deterministische aktuelle Ablehnungen brauchen nicht alle
nachgelagerten Inputs; ein fehlender History-Beleg ist selbst kein F-Ausschlussbeweis.

## CLI, AI und Modus-Admission

Die echten CLI-Dispatch-Pfade `screen --champion` und `export-ai --champion` wurden
für vollständige, maskiert lückenhafte und deterministisch abgelehnte Fixtures
aufgerufen, ausschließlich mit temporärer DB und Output-Pfaden. Beide verwenden
`screen_champion`; AI exportiert nur dessen eligible/ranked Records.

- Gültig: gleiche Kandidatenfolge, Quant-Scores und F/configured/C1-Label.
- Ranking unresolved: Screen liefert einen diagnostischen Report ohne Ranking;
  CLI-Screen bleibt regulär Exit-Code 0. AI-Export liefert Exit-Code 1 und legt
  keine Kandidatendatei an. Kein stiller Fallback oder ungeprüfter Zweit-Score.
- Die Forward-Market-Zeilen bleiben vor/nach den CLI-Aufrufen gleich.

Read-only `evaluate_inception_admission` prüft zusätzlich beide expliziten Modi,
ohne Journalobjekt/-Datei anzulegen:

- Vollständig: Economic und Observation zugelassen, falls operativ bereit.
- Ranking unresolved als einziger Blocker: Observation-only zugelassen,
  Economic gesperrt.
- Operativer Asset-State failed/errors=1 bei vollständiger Coverage: beide
  Readiness-Modi gesperrt; der lokale Standalone-Screen darf trotzdem qualifizierte
  Daten zeigen. Ein Screen ist keine Economic-Activation-/Mode-Admission.

Diese unterschiedlichen Admission-Verträge bleiben ausdrücklich erhalten.

## Snapshot- und echte Screener-Evidenz

Ein konkurrierender Writer revidiert nach dem Screening eine historische Bar im
**temporären** WAL-Fixture auf Zero-Volume. Die Coverage des noch offenen ersten
Snapshots bleibt complete und bindet den ursprünglichen Datenstand; erst der nächste
Aufruf sieht 299 nutzbare Sessions und hält das Ranking zurück. Damit ist die
Snapshot-Pinnung praktisch geprüft, nicht nur durch Call-Reihenfolge behauptet.

Zusätzlich wurde der Produktions-Screener ohne Feature-/Screener-Mock mit synthetischem
Asset, CIK und nur einer aktuellen SIP-Bar verwendet. Standalone und Readiness liefern
dieselben Records und dieselbe manifestgebundene unvollständige Coverage; Observation
bleibt zulässig, Economic nicht. Andere fehlende Fundamentals werden in dieser Probe
nicht als isolierter F03-Beweis ausgegeben; dafür dient der gezielte Red-Test.

## Regressionen und verbotene Ausführungen

Erster Green-Lauf: 22 neue Tests bestanden (37,88 s).
Danach neuer + unmittelbar betroffener Lauf: 78 bestanden (76,74 s).
Zusätzliche Produktions-Screener-Probe: 1 bestanden (2,67 s).
Abschließender kombinierter Lauf: **79 bestanden**, Exit-Code 0, 77,46 s; eine bereits bestehende websockets.legacy-DeprecationWarning. Ruff für alle drei geänderten Python-Dateien bestanden; git diff --check ohne Whitespace-Fehler.

| Datei / Auswahl | Fälle im Abschlusslauf |
| --- | ---: |
| test_f03_coverage_parity.py | 23 |
| test_shadow_qualification.py | 19 |
| test_forward_history_audit.py | 17 |
| test_ai_export.py | 5 |
| test_champion_consolidation.py: Ranking-/AI-Parität und Current-Snapshot-Abschaltung | 2 |
| test_forward_sip_contract.py: Feed-/Provenienz-/Coverage-/Fingerprint-/CC01-Auswahl | 13 |
| Gesamt | 79 |

Aus test_forward_sip_contract wurden Runner-/Inception-/Journalfälle ausdrücklich
nicht ausgeführt. Aus test_champion_consolidation wurden keine Backtest- oder
kombinierten Screen/Export/Backtest-Fälle ausgeführt. Bestehende Acquisition-Tests
verwenden ausschließlich lokale Fake-Provider; keine echte Providerabfrage.

Der ältere Current-Snapshot-Test übergab None als Marktdatenbank und ein
unqualifiziertes Mock-Record. Er wurde mit einer tatsächlichen temporären DB,
300 synthetischen SIP-Sessions und expliziter aktueller Observation an den
Forward-Vertrag angepasst. Keine Produktions-Sonderbehandlung für Testdoubles.

Legacy-Screener und `run_champion_backtest` wurden nicht geändert. Der Wrapper-
Funktionskörper und sein LEGACY_RESEARCH-Default bleiben identisch; es wurde zur
Verifikation kein historischer Backtest gestartet. Keine Profitabilitäts- oder
allgemeine historische Performancebehauptung.

## Geänderte Dateien und Grenzen

- src/trading_system/champion.py: kanonische Coverage und Qualifizierung vor Manifest,
  innerhalb des ursprünglichen gepinnten Snapshots.
- tests/test_f03_coverage_parity.py: neue gezielte Offline-Regressionen.
- tests/test_champion_consolidation.py: bestehende Screen-Fixture vervollständigt.
- .gitignore: Ausnahme ausschließlich für diesen angeforderten Bericht.
- reports/f03_champion_coverage_parity_audit.md: dieser Bericht.

Keine Änderung an Coverage-Algorithmen, Qualifikations-/Readiness-Regeln, Scores,
Gates, Ranking-Reihenfolge, AI-Schema oder Konfiguration. SIP-only/all und die
bestehenden Provenienzfilter bleiben wirksam. Kein IEX-/Legacy-/Raw-Fallback.

Größere Coverage-Evidenz kann neue Manifest-Hashes und größere Artefakte erzeugen;
alte Artefakte werden nicht aktualisiert. Es wurde kein Produktions-Lasttest und
kein vollständiger historischer Screen ausgeführt. Bestehende Pipeline-Fingerprints
berücksichtigen champion.py; deren Drift-Guards werden nicht umgangen, und kein
bestehendes Journal wurde ausgeführt oder angepasst.


## Reproduktion und Abschlussartefakte

PyCharm-Projektinterpreter: `.venv/Scripts/python.exe`, Python 3.12.7.

```powershell
$f03TestTargets = @(
  'tests/test_f03_coverage_parity.py',
  'tests/test_shadow_qualification.py',
  'tests/test_forward_history_audit.py',
  'tests/test_ai_export.py',
  'tests/test_champion_consolidation.py::test_champion_ranking_screen_and_ai_share_f_gates_and_order',
  'tests/test_champion_consolidation.py::test_screen_champion_disables_current_snapshots',
  'tests/test_forward_sip_contract.py::test_migration_feed_identity_no_relabelling',
  'tests/test_forward_sip_contract.py::test_no_iex_fallback_or_session_mix',
  'tests/test_forward_sip_contract.py::test_corrupt_retrieval_provenance_is_not_usable',
  'tests/test_forward_sip_contract.py::test_cc01_sip_critical_fail_closed',
  'tests/test_forward_sip_contract.py::test_fingerprints_bind_contract_and_pipeline',
  'tests/test_forward_sip_contract.py::test_champion_screen_uses_forward_adapter_and_pins_effective_session',
  'tests/test_forward_sip_contract.py::test_unresolved_forward_inputs_block_candidate_ranking',
  'tests/test_forward_sip_contract.py::test_fresh_identity_only_sec_partial_does_not_mask_ranking_block'
)
& '.venv/Scripts/python.exe' -m pytest @f03TestTargets -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_f03_cache --basetemp=.tmp_pytest_f03_final --junitxml=reports/f03_coverage_parity_final.xml
```

Red-Protokoll: `reports/f03_coverage_parity_red.txt`.
Finales Rohprotokoll: `reports/f03_coverage_parity_final.txt`.
Maschinenlesbar: `reports/f03_coverage_parity_final.xml`.
Temporäre pytest-Datenbanken/Reports und der separate Cache wurden nach Abschluss
entfernt; die Abschlussprotokolle bleiben lokal erhalten. HEAD blieb unverändert.
