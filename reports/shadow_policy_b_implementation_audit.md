# Task 11B – Economic Policy B: Atomic Risk Management

Stand: 13.11.2026. Lokal gelesener HEAD:
`43958e8dd03513caa980face7237c569686e12a8`.
11A und dessen Bericht/Tests lagen als nicht committete Änderungen vor;
sie wurden erhalten. Auch die vorhandenen Task-8E-Dateien und `.gitignore`-
Anpassungen wurden nicht zurückgesetzt.

Referenzen: `shadow_economic_policy_audit.md` und
`shadow_policy_b_evidence_audit.md` im Reportverzeichnis. Der 11A-Bericht
beschreibt den damaligen Vorbereitungszustand; seine Aussage „Runner nicht
aktiviert“ bleibt historische Task-11A-Aussage und wird nicht rückwirkend geändert.

## Ergebnis

**Policy B ist im Code des laufenden ECONOMIC-Shadow-Runners implementiert.**
Keine operative Session wurde gestartet. Keine reale Marktdatenbank oder
existierende Journaldatei wurde geöffnet, initialisiert, aktiviert oder verändert.
Alle Ausführungen sind Offline-Tests mit eigens angelegten synthetischen Fixtures.
Keine Provider-/Netzwerkzugriffe, keine historischen Backteständerungen,
keine SQL-Migration und kein Commit/Push.

Der Runner verwendet weiterhin genau denselben `ChampionSession`-Adapter und
dieselben F-/Engine-/PositionManager-Regeln. Der produktive Code-Diff für 11B
liegt ausschließlich in `shadow/runner.py`; die 11A-Dateien `contracts.py`,
`modes.py` und `journal.py` werden für 11B nicht erneut geändert.

| Sessionzweck | Laufender Runner nach 11B |
| --- | --- |
| `FULL` | Vollständige Readiness, unveränderter Champion-F-Ausführungs-/Signalpfad; neue versionierte Admission-Evidenz. |
| `MANAGEMENT_WITH_BLOCKED_SIGNALS` | Ausschließlich `RANKING_UNRESOLVED`, Infrastruktur vollständig, STRICT und Provenienz gültig, Fingerprints unverändert. Bestehendes Positionsmanagement und alte Pending-Intents laufen normal; neue Signal-T-Intents sind verboten. |

Die Zwecke bleiben getrennt von den Modi `OBSERVATION_ONLY` und `ECONOMIC`.
Observation, Inception und Economic Activation behalten ihre bisherigen
vollständigen jeweiligen Admission-Bedingungen. Economic-Readiness wird bei
Rankinglücken nicht auf true gesetzt.

## Code-Diff und Admission

`ShadowRunner.run` behält `BEGIN IMMEDIATE`, Journal-Replay, Drift-Prüfung,
offizielle Sessionsequenz und ursprünglichen STRICT-Preflight bei.
Anschließend:

1. Observation verwendet unverändert `readiness.observation_ready`.
2. Economic darf entweder vollständige Readiness verwenden oder nur die exakte
   Ausnahme `observation_ready` plus Blockerliste `[RANKING_UNRESOLVED]`.
   Alles andere führt weiterhin zu `SHADOW_SESSION_DATA_NOT_READY` und Rollback.
3. Das ursprüngliche `evidence_manifest` wird für ECONOMIC in den vorhandenen
   11A-Envelope `shadow-session-admission/v1` mit dem gewählten Zweck eingebettet.
   Admission bindet vollständigen Vorgängerzustand, Fingerprint-Scope und Bars.
4. **Vor** Aufruf des wirtschaftlichen Adapters werden `validate_session_admission`
   und `validate_admission_preflight` aus 11A ausgeführt. Damit genügen weder
   ein falsches ready-Flag noch ein selbst behauptetes STRICT-Ergebnis.
5. Genau derselbe Preflight-Payload wird als `DATA_PREFLIGHT_PASSED` journalisiert.
   Der bestehende Adapter erhält die ursprüngliche, ehrliche Readiness mit
   unverändertem `economic_shadow_ready=False` im begrenzten Fall.
6. Ein zusätzlicher Emit-Guard verweigert `ENTRY_INTENT_CREATED` bei begrenztem
   Zweck mit `SHADOW_BLOCKED_SIGNAL_INTENT`. Das ergänzt den vorhandenen
   Adapterzweig `NO_TRADE_DATA_INCOMPLETE` und den unabhängigen Journal-Verifier.
7. Nach Verarbeitung folgt wie bisher ein `SESSION_COMMITTED`, vollständige
   Verifikation innerhalb derselben SQLite-Transaktion und erst dann SQL-COMMIT.

Es gibt keine zweite Engine, keine alternative Stop-/Target-/Sizinglogik, keinen
Risk-Teilcommit und keine nachträgliche Signalphase.

### Kanonische Reihenfolge

```text
Journal-Replay / Drift / Sessiongrenze / STRICT
Readiness + versionierte Admission + STRICT-/Source-Verifikation
SESSION_STARTED
DATA_PREFLIGHT_PASSED
SESSION_OPEN: bestehender Holding Counter und Open-Management
altes Pending: ENTRY_EXECUTED oder ENTRY_REJECTED am vorgesehenen Open
Intrabar-Management und Marking / lokale ATR-Reads
SCREEN_EVALUATED mit vollständiger erweiterter Source-Provenienz
Close-Management einschließlich Session-10-Time-Exit
Signalentscheidung:
  FULL: unveränderte F-Entscheidung
  begrenzt: DECISION_NO_TRADE / NO_TRADE_DATA_INCOMPLETE
SESSION_COMMITTED
Journal-Verifikation und ein SQL-COMMIT
```

`SCREEN_EVALUATED` liegt wie bisher vor den Close-Transitions, weil diese
zulässige Scorediagnostik aus dem Screen verwenden. Die Signalentscheidung liegt
nach dem Close-Management. Diese bestehende Reihenfolge wurde nicht umgestellt.

## Neue Admission-Invarianten und Fehlergrenzen

- Ausnahme ausschließlich für isolierte Rankingunvollständigkeit. SEC-/Asset-/
  SIP-/SPY-Fehler, Struktur-/Analysefehler und Drift werden nicht toleriert.
- Die gespeicherten Upstream-Zustände werden durch 11A nochmals auf Status,
  Fehler, 96-Stunden-Frische und SEC-Abschluss nach Analyse-Close geprüft.
  Die bestehende enge SEC-Regel für technisch fehlerfreie, deterministisch
  ausgeschlossene Identity-Konflikte wird nicht erweitert.
- Kritische Held-/Pending-Bars müssen STRICT bestehen und aktuellen SIP/all/1d-
  Source-Beobachtungen entsprechen. SPY muss ebenfalls nutzbar sein. Keine
  synthetischen Fills aus fehlenden Bars, kein Stale-/IEX-/Legacy-Fallback.
- Vor jedem wirtschaftlichen Adapteraufruf sind Zustand, komplette Runtime-/
  Config-Fingerprints, Qualification, Readiness und kritische Inputs gebunden.
  Management darf Source-Manifeste erweitern, aber vorhandene Quellen nicht
  ersetzen. Der finale Screenbeleg wird vor SQL-COMMIT gegen Admission geprüft.
- T−1-Pending ist unabhängig von der heutigen Rankinglücke. Es wird weder neu
  gerankt noch verschoben oder wegen unvollständigem Signal T still verworfen.
  `_open_position` entscheidet weiterhin kanonisch über Fill oder Ablehnung.
- Flat plus Rankinglücke darf als wirtschaftlich unveränderte, aber ausdrücklich
  begrenzte Session fortschreiten. Danach ist T endgültig: Source-Reparatur
  erzeugt weder neuen Intent noch zweiten Commit für T.
- Fehler vor Abschluss rollen Events **und neue Manifeste** zurück. Der eigene
  CC-01-Failure-Beleg bei fehlendem kritischem Bar bleibt der bisherige
  nicht wirtschaftliche Sonderfall; kein Sessioncommit und kein Zustandsfortschritt.
- Readiness-/Sourcefehler werden nicht als No-Trade-Erfolg oder falscher
  CC-01-Barfehler gebucht. Ein eigenständiges persistentes Readiness-Rejection-
  Event wurde in diesem Auftrag nicht eingeführt.

## Atomarität, Replay und Recovery

Die vorhandene `Journal.connection(write=True)` serialisiert mit
`BEGIN IMMEDIATE` vor Replay/Source-Lesen. `BaseException` einschließlich
synthetischem Prozessabbruch führt zum vollständigen Rollback. Ein zweiter
Runner sieht nach dem ersten Commit dessen neuen Zustand und gibt für dieselbe
Session `ALREADY_PROCESSED` zurück.

Die ursprünglichen IDs, Sequenz-/Hash-/Reducer-/C1-/Cash-Conservation-Prüfungen
bleiben unverändert. Nach einem abgebrochenen Versuch bleibt T die exakt nächste
offizielle Session; Retry verwendet einen neuen gepinnten Source-Snapshot und
erzeugt deterministisch dieselbe Position-ID. Es gibt keinen halben Fill, Exit,
Holding-Counter oder dauerhaften `SESSION_STARTED`-Tail.

Crash nach SQLite-COMMIT, aber vor Antwort, ist weiterhin über Session-Idempotenz
abgedeckt: Wiederholung liefert `ALREADY_PROCESSED`. Die neuen Tests injizieren
Crashes vor SQL-COMMIT einschließlich nach Append des `SESSION_COMMITTED`-Events;
ein echter OS-/Stromausfalltest oder Broker-Reconciliation wird nicht behauptet.

## Offline-Tests und wirtschaftliche Parität

### Voraussetzung 11A

Vor der Runneränderung erneut ausgeführt: **24 Evidenztests bestanden**, 2,73 s.
Keine Abnahme allein aus dem älteren Bericht übernommen.

### Neue Runner-Integration

`tests/test_shadow_policy_b_runner.py`: 22 Fälle auf den vorhandenen durch
Task 6A korrigierten Fixtures. `test_shadow_execution.case` persistiert eine
vollständige synthetische 300-Session-SIP-Historie und prüft deren Coverage.
Für die fremde Rankinglücke wird genau eine reale synthetische BBB-Beobachtung
in diesem Fenster entfernt. AAA bleibt vollständig; kein Guard wird gemockt,
um eine unvollständige History als vollständig auszugeben.

| Fälle | Nachweis |
| --- | --- |
| Held, Gap-Stop, Opening-Target, Intrabar-Stop/Target-Konflikt, Target, Session 10 | Kritische AAA-Bars verfügbar; korrekte Exit-Priorität, Preise, Quantity, Cash/PnL, unveränderte Position-ID und exakte Holding-Counter trotz fremder BBB-Lücke. Marking bei Hold. |
| Altes Pending | Signal aus vollständigem T−1; Fill an vorgesehenem T, vor Screen; Holding=1, genau eine Positionsequenz, kein neues Pending T. |
| Flat | Begrenzter Commit ohne Exposure; nach Reparatur kein nachträglicher Intent T. Erst eine spätere FULL-Session erzeugt wieder normalen Intent. |
| Kritischer Bar fehlt, Pending und Held | CC-01 verweigert; unveränderter Zustand/Cursor, genau ein Failure-Beleg; Repair/Retry derselben Session erfolgreich begrenzt. |
| SEC stale, SPY fehlt, Asset-/SIP failed, Runtime-Drift | Kein Sessioncommit, keine wirtschaftlichen Events, vollständige Erhaltung alten Pendings. |
| Crash nach Entry, Management, Exit, Entscheidung, Commit-Event | Events und Manifeste vollständig zurückgerollt; Restart/Retry exakt einmal, gleiche Position-ID. Entry-Bar-Exit ebenfalls abgedeckt. |
| Zwei konkurrierende Runner | Ein begrenzter Commit, ein ALREADY_PROCESSED, genau ein Fill und position_sequence=1. |
| Fehlerhafter Adapter versucht neuen Intent | Emit-Guard erzwingt vollständigen Rollback statt verbotenen Intents. |

Die erste neue Probe hatte eine falsche erwartete Bezeichnung des bereits
bestehenden Qualification-Reasons (`INSUFFICIENT_MARKET_HISTORY` statt
`MARKET_HISTORY_UNPROVEN`). Nur die Assertion wurde korrigiert; keine
Readiness-/Qualification-Regel geändert. Danach vollständiger fokussierter Lauf.

### FULL-Parität gegen eingefrorenen Champion

Die fünf vorhandenen `test_focused_champion_equivalence`-Szenarien wurden
verstärkt: Gap-Stop, Opening-Target, Stop/Target-Konflikt, Target und Time-Exit.
Sie vergleichen echte `run_champion_backtest(..., STRICT)`-Ergebnisse auf denselben
qualifizierten synthetischen Inputs mit jeweils 13 fortlaufenden Shadow-Sessions.
Kein Backtest-/Champion-/Engine-Produktionscode wurde verändert.

Vergleich ohne wirtschaftliche Toleranz oder Rundung:

- Symbol, Position-ID, Entry-/Exit-Datum und Entry-/Exit-Preis;
- Quantity, Stop, Target, Exitgrund, PnL und Holding Counter;
- Cash, Portfolio-Equity und realisierter PnL **pro Session**;
- Next-Session-Intents nach Signal-/Ausführungsdatum, Symbol, F-Score, Rank und
  Signal-ATR. Die erwarteten Intents werden direkt am unveränderten kanonischen
  `_entry_orders` beobachtet und vor Shadow-Ausführung wieder entkoppelt;
- unveränderte synthetische Source-DB während der Reader-/Runner-Ausführung.

**Ergebnis: 27/27 bestanden**, 127,33 s: 22 neue Runnerfälle plus fünf verstärkte
FULL-Paritätsfälle. Null wirtschaftliche Abweichungen in diesen fünf Szenarien,
65 vollständige Shadow-Sessions. Die Aussage gilt für diese deterministischen
qualifizierten Proben, nicht als historische oder Live-Performance-Zertifizierung.

### Bestehende Regressionen

Fokussierter weiterer Lauf: alle 24 11A-Evidenztests, Journal-, Observation-,
Inception-/Admission- und übrige Execution-Tests sowie ausgewählte History-/
ATR-Hardening-Fälle. Bereits ausgeführte fünf Paritätsfälle werden nicht unnötig
wiederholt. Die frühere A-Erwartung „Rankinglücke sperrt gesamten Economic-Tag“
wird nur in den betroffenen Tests auf den nun beauftragten begrenzten No-Trade-
Commit angepasst. Leeres Source-Universe, kritische Datenfehler und sämtliche
Activation-/Inception-Grenzen bleiben Sperren.

**Abschließender Regressionlauf: 136 bestanden, 5 bereits geprüfte Paritätsfälle
deselektiert**, 328,73 s. Damit **163 verschiedene Fälle bestanden** über beide
abschließenden Läufe (27 + 136), ohne offene Fehler. Der vorherige Voraussetzungslauf
und die drei erfolgreichen Sizing-Smoke-Proben sind darin nicht doppelt gezählt.
Ruff check für alle in 11B geänderten Python-Dateien und `git diff --check`
bestanden. Einzige Warnung: bestehende `websockets.legacy`-DeprecationWarning.
Während der abschließenden Läufe blieb fingerprintrelevanter Produktionscode
unverändert. Testartefakte im lokal ignorierten Reportbereich:

- `shadow_policy_b_prerequisite_tests.xml`
- `shadow_policy_b_runner_initial.xml` (erster Assertionfehler, Zwischenstand)
- `shadow_policy_b_implementation_tests.xml` (27 bestanden)
- `shadow_policy_b_implementation_regression.xml`

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/test_shadow_policy_b_evidence.py -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_11b_cache --basetemp=.tmp_pytest_11b_prerequisite --junitxml=reports/shadow_policy_b_prerequisite_tests.xml
& '.venv/Scripts/python.exe' -m pytest tests/test_shadow_policy_b_runner.py tests/test_shadow_execution.py::test_focused_champion_equivalence -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_11b_cache --basetemp=.tmp_pytest_11b_policy --junitxml=reports/shadow_policy_b_implementation_tests.xml
& '.venv/Scripts/python.exe' -m pytest tests/test_shadow_policy_b_evidence.py tests/test_shadow_journal.py tests/test_shadow_observation.py tests/test_shadow_admission.py tests/test_shadow_execution.py tests/test_shadow_hardening.py::test_selected_atr_unknown_does_not_promote_rank_two tests/test_shadow_hardening.py::test_synthetic_history_gaps_block_signals_without_retrospective_repair -k 'not focused_champion_equivalence' -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_11b_cache --basetemp=.tmp_pytest_11b_regression --junitxml=reports/shadow_policy_b_implementation_regression.xml
```

Python aus dem vor jedem Aufruf abgefragten PyCharm-SDK:
`G:/GitHub/TraWalp/.venv/Scripts/python.exe`, Python 3.12.7. Keine Paketinstallation.

## Event-/Fingerprint-Versionen und Inception-Auswirkung

| Ebene | Version / Auswirkung |
| --- | --- |
| SQL-/Eventhülle | Unverändert `SCHEMA_VERSION=1`; keine neuen Tabellen, Trigger oder Eventtypen. |
| Laufende ECONOMIC-Admission | Ab jetzt `shadow-session-admission/v1` aus 11A, sowohl FULL als auch begrenzt. Referenz über vorhandenes readiness_evidence_hash-Feld. |
| Innere Readiness-/Qualification-Evidenz | Unverändert `shadow-observation-evidence/v1` und `shadow-input-readiness/v1`; Flags bleiben ehrlich. |
| STRICT | Unverändert `CC01/validate_session_position_data/v1`. |
| Observation / Activation | Bisheriger Evidenzvertrag unverändert, kein automatischer Modewechsel. |
| Engine-/Runtime-Fingerprint | Runnerdatei gehört zu `shadow/*.py`; ihr neuer Inhalt ändert engine_files und engine_fingerprint regulär. Kein neuer Hashalgorithmus oder künstlich gleichgehaltener Fingerprint. |
| Bestehende Journale | Alte Bytes und alte Evidenzversionen bleiben gemäß 11A verifizierbar. Fortsetzung mit geändertem Code bleibt durch Drift gesperrt; kein automatisches Rebaseline. |

Eine künftige separat autorisierte Inception würde regulär die neuen Codehashes
binden. Dieser Auftrag führt sie nicht aus. Eine vorhandene Observation-Inception
kann nicht durch stilles Umschreiben ihrer Fingerprints für Activation passend
gemacht werden. Code-/Runtime-Drift und die Frage eines neuen autorisierten
Forward-Starts müssen vor einem operativen Auftrag ausdrücklich geklärt werden.

## Geänderte Dateien in 11B

- `src/trading_system/shadow/runner.py`: begrenzte Admission und unveränderter
  Adapterpfad, frühe Evidenzvalidierung, zusätzlicher Intent-Emit-Guard.
- `tests/test_shadow_policy_b_runner.py`: neue Integration/Recovery-Fälle.
- `tests/test_shadow_execution.py`: verstärkte FULL-Parität und neue Erwartung
  bei isoliertem stale-Candidate-Rankingproblem.
- `tests/test_shadow_observation.py`: Economic-Rankingprobe erwartet begrenzten
  Commit; Observation-/Activation-Regeln bleiben streng.
- `tests/test_shadow_hardening.py`: fehlende Ranking-ATR/History blockiert neue
  Signale; erfolgreiche begrenzte Session ist nach Reparatur endgültig.
- dieser Bericht und gezielte zusätzliche `.gitignore`-Ausnahme.

11A-Diff bleibt separat als bestehende uncommittete Baseline erhalten. Insbesondere
`execution.py`, `readiness.py`, Champion/Engine, Strategieparameter und historische
Research-/Backtestergebnisse werden durch 11B nicht verändert.

## Offene Risiken vor Economic Activation

Keine Freigabe für Live-/Paper-Trading oder Economic Activation.

- Der neue Runner benötigt zu seinen Codehashes passende, ausdrücklich
  autorisierte Inception-Fingerprints. Alte Journale werden nicht umetikettiert.
- Technische Upstream-Fehler bleiben auch bei gültigen Positionsbars sperrend.
  Policy B löst die isolierte Rankingkopplung, keine allgemeine Datenverfügbarkeit.
- Der Verifier prüft gespeicherte Evidenz, Provenienz und Hashkonsistenz; keine
  externe Provider-Authentizität, Brokerzustände oder Corporate-Action-Ledger.
- Die Prüfungen zeigen gezielte synthetische Parität und Transaktions-/Replay-
  Sicherheit, keinen produktiven Lasttest, echte SEC-Frische oder Full-Universe-
  Laufzeitnachweis. Replay vollständiger Manifeste kann weiter teuer sein.
- Operative Readiness, Clock-/Close-Grenzen, Source-Snapshots, Recovery und der
  zulässige Weg zu einer neuen Economic-Inception/Activation bedürfen eines
  separaten Auftrags. Keine produktive Journaldatei wurde hierfür angelegt.
