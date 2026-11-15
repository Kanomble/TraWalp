# Task 11A – Economic Policy B: Versioned Admission Evidence

Stand: 13.11.2026. Lokal gelesener HEAD:
`43958e8dd03513caa980face7237c569686e12a8`.
Die Baseline enthielt bereits eine geänderte `.gitignore` sowie die untracked
Task-8E-Dateien. Diese Änderungen wurden erhalten. Keine reale Datenbank für
diesen Auftrag geöffnet; Datenkorrekturen werden weder zurückgesetzt noch erneut
ausgeführt.

## Ergebnis und Abgrenzung

Der neue semantische Vertrag `shadow-session-admission/v1` erlaubt dem Verifier,
zwei **Sessionzwecke** ausdrücklich zu unterscheiden:

- `FULL`: vollständige Economic-Readiness; neue Signal-T-Intents grundsätzlich
  nur gemäß den bestehenden weiteren Guards zulässig.
- `MANAGEMENT_WITH_BLOCKED_SIGNALS`: ausschließlich Rankingunvollständigkeit;
  kritisches Management und Ausführung/Ablehnung alter T−1-Pending-Intents
  zulässig, **kein neuer Signal-T-Intent**.

Die Zwecke sind keine Shadow-Modi. `MODES` bleibt exakt `OBSERVATION_ONLY` und
`ECONOMIC`. Der neue Vertrag wird ausschließlich für laufende ECONOMIC-Sessions
interpretiert. SQL-Schema und `SCHEMA_VERSION=1` bleiben unverändert.

**Policy B ist nicht im Runner aktiviert.** `ShadowRunner.run` verlangt im
ECONOMIC-Modus weiterhin `readiness.ready`. Der Runner erzeugt weiterhin den
bisherigen Evidenzvertrag. Inception, Observation-Inception und Economic
Activation sind unverändert. Es gibt keinen neuen CLI-Schalter, keine automatische
Journalumstellung und keinen wirtschaftlichen Policy-Wechsel.

## Analyse der sechs Module

| Modul | Befund und Änderung |
| --- | --- |
| `shadow/contracts.py` | Canonical JSON, Hashes und Runtime-/Config-Fingerprints bestehen fort. Ergänzt nur Konstanten für Session-Evidenzversion und Zwecke; keine Schemaänderung. |
| `shadow/modes.py` | Bestehender `shadow-observation-evidence/v1`-Validator bleibt unverändert streng, insbesondere `economic=True` für Activation. Ergänzt separaten Manifest-Builder, Session-Admission-Validator und STRICT-/SIP-Provenienzprüfung. |
| `shadow/journal.py` | Dispatch anhand expliziter Evidenzversion beim Sessionstart. Neue Session-Evidenz ist nur in ECONOMIC zulässig; Zweck wird vor jeder Transition über Sessionzustand getragen. Preflight und Screen werden an Admission gebunden, neuer Intent bei begrenztem Zweck verworfen. Alte Replaypfade bleiben erhalten. |
| `shadow/readiness.py` | Unverändert. `ready`/`economic_shadow_ready` bleiben bei Rankinglücken false; Observation-Readiness trennt Ranking von Infrastruktur. Der neue Validator verwendet dessen bestehende Readiness-Version und 96-Stunden-Grenze, prüft gespeicherte Upstream-Zustände nochmals. |
| `shadow/runner.py` | Unverändert. STRICT vor Screen, vollständige Economic-Admission vor ChampionSession; init/activate behalten vollständige Bedingungen. Kein neuer Builder-Aufruf im Runner. |
| `shadow/execution.py` | Unverändert. Vorhandene Reihenfolge: Open-Management, altes Pending, Intrabar, Screen, Close-Management, Signalentscheidung. Bei unvollständigem Screen existiert bereits `NO_TRADE_DATA_INCOMPLETE`; dieser Zweig wird durch 11A nicht im laufenden Runner freigeschaltet. |

Champion-, Engine-, PositionManager-, Sizing-, Stop-/Target-, Tag-10- und
Scoringregeln wurden nicht geändert. Die neuen Tests dürfen den vorhandenen
Adapter direkt mit synthetischen Inputs ausführen, um replaybare Events zu
erzeugen; das ist keine Aktivierung eines Economic-Runners.

## Evidenzformat und Bindungen

Ein neues Manifest wird über die vorhandene hashadressierte Manifestablage
referenziert: `SESSION_STARTED.readiness_evidence_hash` zeigt auf:

```text
contract: shadow-session-admission/v1
purpose: FULL | MANAGEMENT_WITH_BLOCKED_SIGNALS
evidence:
  contract: shadow-observation-evidence/v1
  session: T
  input_manifest_hash: <gebundene Source-Provenienz>
  readiness: <unveränderte Readiness/v1 samt Digest>
  qualification: <vollständiger Qualification-Beleg samt Census>
state_digest: <gesamter Zustand vor T>
fingerprints_digest: <vollständiger Fingerprint-Scope des Sessionstarts>
critical_bars_digest: <exakt DATA_PREFLIGHT_PASSED.observed_bars>
strict_contract_version: CC01/validate_session_position_data/v1
```

Die innere Qualification ist durch `qualification_digest`, die Readiness durch
`input_readiness_digest`, die Source durch Manifesthash und die Admission durch
deren eigenen Manifesthash gebunden. Die vorhandene Eventhashkette bindet den
Sessionstart, Preflight, alle Transitionen und den atomaren Abschluss.
Der Builder bereitet nur Daten vor; er erteilt keine Admission und setzt kein
Readiness-Flag.

Der Fingerprint-Scope des Sessionstarts muss unverändert mit den im Journal
gespeicherten Inception-Fingerprints übereinstimmen. Er enthält insbesondere
Runtime, Engine und Economic-Config. Die zusätzliche Admission-Bindung muss mit
diesem bereits geprüften Scope übereinstimmen. Keine aktuelle Neuklassifizierung
historischer Journale und kein Rebaseline.

## Verifier-Invarianten

1. **FULL bleibt vollständig:** `validate_evidence(..., economic=True)` verlangt
   Economic-Readiness und null unresolved Kandidaten. Der neue FULL-Vertrag
   prüft zusätzlich gespeicherte Upstream-Zustände, STRICT und SIP-Sourcebindung.
   Alte FULL-Evidenz erhält ihren ursprünglichen strengen Vertrag; keine
   automatische Interpretation als begrenzte Admission.
2. **Exakte Ausnahme:** Für begrenzten Zweck ist `blocking_reasons` exakt
   `[RANKING_UNRESOLVED]`, unresolved count > 0, Observation-Infrastruktur bereit,
   Observation-Blocker leer. `ready` und `economic_shadow_ready` müssen explizit
   false bleiben. Census, eindeutige Symbole, Digest und Blockertrennung werden
   vom bestehenden V1-Validator geprüft. Unknown Purpose/Version scheitert geschlossen.
3. **Upstream unabhängig von Flags:** Asset-, SEC- und SIP-Dataset müssen success
   sein und keine errors/database_failures enthalten. Die bereits bestehende
   SEC-Ausnahme für ausschließlich deterministisch ausgeschlossene Identity-
   Konflikte bleibt möglich: partial nur mit identity_conflicts > 0, null Fehlern
   und gültiger last_completed_at. Andere partial/failed/running fehlen als
   Admissiongrund. Kein Tolerieren technischer SEC-Fehler.
4. **Frische:** Zeitstempel dürfen weder zukünftig noch älter als die bestehende
   `SNAPSHOT_MAX_AGE` sein. SEC-Abschluss muss mindestens den Analyse-Close
   erreichen. SPY muss OBSERVATION_PRESENT sein und eine gültige aktuelle
   SIP-Quellbeobachtung besitzen. Manipulierte Bereitschaftsflags alleine
   überdecken keine alten oder fehlerhaften Dataset-Zustände.
5. **STRICT tatsächlich erneut prüfen:** Vor jeglicher wirtschaftlichen Transition
   rekonstruiert der Verifier DailyBars aus der Evidenz und ruft die unveränderte
   CC-01-Funktion auf: offizielle Session/Sequenz, abgeschlossener Close,
   Positions-Entry und T−1-Pending-Signal, passende aktuelle Symbole/Timeframes,
   positive konsistente OHLC, Volumen > 0. Ein fehlender kritischer Bar bleibt
   Fehler, auch wenn ein Angreifer beide Bardigests passend neu berechnet.
6. **SIP-Provenienz:** Für jedes Held-/Pending-Symbol und SPY muss die Source-
   Provenienz aktuelle Beobachtungen mit alpaca/sip/all/1d, Retrieval-Zeitzone
   und nichtleerem sync_reference enthalten. Kritische Managementbars müssen
   exakt der normalisierten Source-Beobachtung entsprechen. SPY wird zusätzlich
   mit demselben CC-01-Barvertrag geprüft. Kein Legacy-/IEX-Fallback.
7. **Screenbindung:** Qualification, Readiness und incomplete_inputs müssen der
   Admission entsprechen. FULL-Screen vollständig, begrenzter Screen unvollständig.
   Der finale Screen-Inputmanifest darf die ursprüngliche Provenienz ergänzen,
   aber keinen bestehenden Wert oder bereits erfassten Source-Eintrag ersetzen
   oder entfernen. Alle geladenen Manifesthashes werden geprüft.
8. **Keine neuen Intents:** `ENTRY_INTENT_CREATED` wird bei begrenztem Zweck
   ausdrücklich verworfen, auch vor Auswertung eines vermeintlich vollständigen
   Signalpayloads. Das gilt ausschließlich für neue Intents T.
   `ENTRY_EXECUTED`/`ENTRY_REJECTED` behalten ihre bestehenden T−1-Pending-,
   Session- und Reducerprüfungen; alte Intents dürfen nicht verschoben werden.
9. **Unveränderte Transitions- und Commitregeln:** Preflight zwingend zuerst,
   Ledger-/C1-Prüfungen, alte Pending-Ausführung vor Screen, Signalentscheidung
   zuletzt, genau ein vollständiger atomarer Sessionabschluss. Ein begrenzter
   Screen kann nur mit `NO_TRADE_DATA_INCOMPLETE` abgeschlossen werden.
   Uncommitteter Tail und ungültige Transitionen bleiben Fehler.
10. **Keine Aktivierungs-Hintertür:** MODE_TRANSITION bleibt an vollständige
    aktuelle Observation, leeren wirtschaftlichen Zustand, vollständige
    Activation-Evidenz, Fingerprints und zukünftige erste Economic-Session
    gebunden. Der neue Session-Envelope ist kein Activation-Evidenceformat.

## Rückwärtskompatibilität und Fingerprints

Weiter verifizierbar sind V1-Journale vor Einführung von `shadow_mode` ohne
Session-Admission sowie Journale mit `shadow-observation-evidence/v1` und
vollständiger Economic-Readiness. Ein explizit vorhandener neuer Admission-Hash
wird auch bei älterer Genesis geprüft, niemals wegen deren Alter ignoriert.
Unbekannte Versionen werden nicht auf den alten Vertrag zurückgestuft.
Keine bestehenden Events oder Evidence-Versionen werden umgeschrieben.

Die Codeänderungen verändern regulär den Runtime-/Engine-Fingerprint für
zukünftige Runs, weil `shadow/*.py` im Fingerprint-Scope liegt. **Verifizierbarkeit
alter Journalbytes ist nicht gleich Driftfreiheit für deren Fortsetzung mit
neuem Code.** Der vorhandene Driftguard bleibt aktiv; keine automatische
Freigabe oder Aktualisierung alter Inception-Fingerprints.

## Offline-Tests und Resultate

Neu: `tests/test_shadow_policy_b_evidence.py`. Journaldaten ausschließlich in
SQLite `:memory:`; kein `Journal.initialize`, kein echtes Journalfile, kein
Runnerstart. Dataset-/Source-Barinputs und Qualification sind synthetisch.
Netzwerk wird durch die vorhandene Offline-Fixture blockiert. Die Readiness
wird durch die produktive Funktion erzeugt; History-Evidence ist hier eine
explizite reine In-Memory-Testquelle und keine Aussage über reale Coverage.

Abgedeckt:

- neue FULL- und legitime begrenzte Evidenz, alte vollständige V1-Sessions,
  Pre-Mode-Journale ohne Admission und identischer erneuter Replayhash;
- alte V1-Evidenz erhält keine begrenzte Erlaubnis; künstliches Economic-ready
  wird verworfen;
- vollständiger Signal-T-Intent in einer FULL-Session, danach tatsächlicher
  T−1-Pending-Fill in einer begrenzten Session, Holding=1, kein neues Pending;
- manipulierte Zwecke/Versionen, State-/Fingerprint-/STRICT-Bindungen;
- unresolved + expliziter SEC-stale-Blocker sowie neu gehashte, versteckte
  SEC-stale/failed-, Asset-running-, SIP-error- und SPY-missing-Zustände;
- kritischer Pending-Bar fehlt trotz neu berechneter Digests;
- unerlaubter neuer Intent, Transition vor Preflight und ungültiger Modewechsel;
- auf IEX manipulierte, neu gehashte Source-Provenienz.

Gezielte bestehende Regressionen: `test_shadow_journal.py`,
`test_shadow_observation.py`, `test_shadow_admission.py`,
`test_shadow_execution.py`. Sie verwenden ausschließlich temporäre synthetische
Markt-/Journal-Fixtures und Provider-/Netzwerksperren; keine echte Journaldatei
und keine produktiven Datenpfade. Insbesondere charakterisieren die bestehenden
Execution-Fälle weiterhin die strikte heutige Runner-Policy bei Rankinglücken.

**Resultat:** 24 neue Evidenzfälle sowie 113 bestehende Regressionen bestanden,
insgesamt **137 verschiedene Tests** über die gezielten Läufe. Ruff check und
`git diff --check` bestehen. Eine bestehende `websockets.legacy`-
DeprecationWarning, keine neue Warnung.

Der erste Regressionlauf endete mit 112 bestanden / 1 Driftfehler in 379,70 s:
Während des Laufs wurde die fingerprintrelevante `modes.py` weiterbearbeitet.
Dadurch erkannte ein vorhandener No-Candidates-Test korrekt eine Änderung zwischen
Inception und Run. Nach Ende aller Codeänderungen wurde genau dieser Fall
zusammen mit allen 24 neuen Fällen wiederholt: **25 bestanden**, 5,28 s.
Der Driftguard wurde weder umgangen noch angepasst. Kein zusätzlicher vollständiger
113-Fälle-Lauf wird behauptet; die übrigen 112 Fälle hatten bereits bestanden.

Artefakte: `shadow_policy_b_evidence_tests.xml` (24 neue Fälle),
`shadow_policy_b_evidence_regression.xml` (initialer 113-Fälle-Lauf einschließlich
Driftfehler) und der maßgebliche finale Nachweis
`shadow_policy_b_evidence_verified.xml` (24 neue Fälle + wiederholte Regression)
im lokalen Reportverzeichnis.

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/test_shadow_policy_b_evidence.py -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_11a_cache --basetemp=.tmp_pytest_11a_final --junitxml=reports/shadow_policy_b_evidence_tests.xml
& '.venv/Scripts/python.exe' -m pytest tests/test_shadow_journal.py tests/test_shadow_observation.py tests/test_shadow_admission.py tests/test_shadow_execution.py -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_11a_cache --basetemp=.tmp_pytest_11a_regression --junitxml=reports/shadow_policy_b_evidence_regression.xml
& '.venv/Scripts/python.exe' -m pytest tests/test_shadow_policy_b_evidence.py 'tests/test_shadow_execution.py::test_no_trade_reasons_are_explicit[no_candidates-NO_ELIGIBLE_CANDIDATES]' -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_11a_cache --basetemp=.tmp_pytest_11a_verified --junitxml=reports/shadow_policy_b_evidence_verified.xml
```

Vor jedem Python-Aufruf wurde die PyCharm-SDK-Auswahl abgefragt; verwendet wird
die Projekt-venv mit Python 3.12.7. Keine Pakete installiert.

## Geänderte Dateien und Task 11B

11A ändert ausschließlich:

- `src/trading_system/shadow/contracts.py`
- `src/trading_system/shadow/modes.py`
- `src/trading_system/shadow/journal.py`
- `tests/test_shadow_policy_b_evidence.py`
- diesen Report sowie eine gezielte zusätzliche `.gitignore`-Ausnahme dafür.

Die vorhandene `.gitignore`-Änderung und Task-8E-Dateien bleiben erhalten.
Readiness, Runner, Execution, Admission/Inception, Champion und Engine sind
unverändert. Keine SQL-Migration, kein Providerzugriff, kein echter Journalstart,
kein Commit/Push.

**Verbleibender Auftrag 11B, separat zu autorisieren:**

1. Im laufenden ECONOMIC-Runner nach STRICT den Zweck anhand der ehrlichen
   Readiness auswählen: FULL oder ausschließlich die hier geprüfte Ausnahme.
   Alle übrigen Fehler sperren weiterhin. Init/Activation unverändert lassen.
2. Neue Session-Evidenz erzeugen, initiale und nach Management erweiterte Source-
   Manifeste korrekt binden; den vorhandenen Adapter unverändert wirtschaftlich
   nutzen und neue Signal-T-Intents sicher unterdrücken. Kein Risk-Teilcommit.
3. Ablehnungs-/Versuchsevidenz für Readinessfehler getrennt entwerfen, falls
   gefordert; nicht als falschen CC-01-Barfehler oder No-Trade-Erfolg ausgeben.
4. Breite Runner-Integration für gehaltene Position, Stop/Target, Tag 10, altes
   Pending mit Fill/Reject/Entry-Bar-Exit, flat, wechselnde FULL/begrenzte Sessions,
   keine rückwirkende Signalergänzung; vollständige Pfade wirtschaftlich identisch.
5. STRICT-/Source-/SEC-/Asset-/SPY-/Drift-Ablehnungen, Crash an Fill/Exit/Commit,
   Retry und konkurrierende Runner inklusive genau-einmal-Fortschritt prüfen.
6. Weitere feindliche Evidenztests nach produktiver Manifestintegration und
   Abgleich aller Source-Erweiterungen. Aktueller Verifier prüft aufgezeichnete
   Evidenz und Hashkonsistenz, keine externe Provider-Authentizität. Vollständig
   erfundene, intern konsistente Daten können ohne signierten externen Anker
   nicht kryptografisch als erfunden erkannt werden.

11A liefert damit die Evidenz-/Verifier-Vorbereitung, keine operative Economic-
Activation oder Freigabe zur Fortsetzung vorhandener Journale unter geändertem Code.
