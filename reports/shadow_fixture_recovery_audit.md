# Shadow Fixture Recovery Audit — Task 6A

## Umfang und Baseline

HEAD: `4e57cce93e1236df8e1f867cda4971e985578a4e` (unverändert).
Alle Eingaben sind synthetisch; Datenbanken und Journale liegen ausschließlich in
pytest-Temporärverzeichnissen. Keine Produktionsdatei, Economic Policy oder STRICT-Regel
wurde geändert. Kein Provider-/Netzwerkzugriff, kein CLI-Journal-Init/Run außerhalb der
autorisierten temporären Tests, keine reale Marktdatenbank, kein großer Backtest,
kein Commit/Push. Die kleinen bestehenden Champion-Vergleiche bleiben unverändert.

## Vorher und Nachher

Vorher: **40 fehlgeschlagen, 87 bestanden (127 Tests)**. Rohprotokoll:
`reports/shadow_fixture_recovery_before.txt`. Der Baseline-Lauf meldet zusätzlich zwei
PytestCacheWarnings wegen des bereits vorhandenen, nicht beschreibbaren `.pytest_cache`.
Nachher wird deshalb ein eigenes temporäres Cache-Verzeichnis benutzt.

Nachher: **130 bestanden, 0 fehlgeschlagen**, einschließlich drei neuer History-Negativfälle. Laufzeit 409,31 s; Exit-Code 0. Die einzige Warnung ist eine bestehende `websockets.legacy`-DeprecationWarning. Rohprotokoll: `reports/shadow_fixture_recovery_after.txt`; maschinenlesbar: `reports/shadow_fixture_recovery_after.xml`.
Ein Zwischenlauf wurde vor Abschluss beendet, um die endgültigen Assertions gemeinsam
in einem vollständigen Lauf zu prüfen; er ist kein Ergebnisnachweis.

Interpreter: `G:\GitHub\TraWalp\.venv\Scripts\python.exe` (PyCharm SDK, Python 3.12.7).

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/test_shadow_execution.py tests/test_shadow_hardening.py tests/test_shadow_observation.py tests/test_shadow_journal.py -q --tb=short --basetemp=.tmp_fixture_recovery_before
& '.venv/Scripts/python.exe' -u -m pytest tests/test_shadow_execution.py tests/test_shadow_hardening.py tests/test_shadow_observation.py tests/test_shadow_journal.py -o addopts='' -v --tb=short -o cache_dir=.tmp_fixture_recovery_cache --basetemp=.tmp_fixture_recovery_final --junitxml=reports/shadow_fixture_recovery_after.xml
```

## Vertrag und Ursachen

`LocalInputs.history_coverage` prüft aus der gepinnten SIP-View die offiziellen
Sessions aus `full_history_request_window(session, 300)`. Jede Session benötigt eine
gültige, positive Daily-OHLC-Bar mit positivem Volumen. Ein gespeicherter Row-Count
oder ein erfolgreicher Dataset-Sync ersetzt diesen Nachweis nicht. Fehlende History
beweist keine kurze Listing-Historie (`listing_history="unknown"`).

Die alte gemeinsame Fixture hatte nur 15 Januar-Sessions und eine zusätzliche
SPY-Bar vom 29.12.2023. Das reicht nicht für die potenziell zulässigen AAA/BBB.
`qualify_f_inputs` hält sie daher mit `MARKET_HISTORY_UNPROVEN` zurück. Die meisten
Fehler entstehen vor Fill, Exit, Crash oder Tampering. Der reine Qualification-Test
hatte ebenfalls keine Coverage-Evidenz und `record()` setzt keinen History-Nachweis.

Klassifikation: **A: 39, B: 1, C: 0, D: 0**.
Observation-Census-Erwartungen wurden nicht gelockert: Erwartet bleiben ein ungelöster
Kandidat im absichtlich unvollständigen Fall und kein ungelöster Kandidat im vollständigen
Fall. Der alte Census von zwei war korrekt für die alte, versehentlich unvollständige Fixture.

## Alle 40 ursprünglichen Fehler

A = unvollständige synthetische SIP-Historie; B = falscher Coverage-/Census-Testvertrag;
C = Produktionsregression; D = anderer Fehler. Parametrisierte Fälle sind einzeln gezählt.

| Nr. | Ursprünglicher Test | Klasse | Befund |
| --- | --- | --- | --- |
| 1 | `tests/test_shadow_execution.py::test_default_next_official_session_and_duplicate_are_deterministic` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 2 | `tests/test_shadow_execution.py::test_current_incomplete_future_and_no_new_session` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 3 | `tests/test_shadow_execution.py::test_signal_pending_next_open_sizing_and_c1[2]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 4 | `tests/test_shadow_execution.py::test_signal_pending_next_open_sizing_and_c1[0.1]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 5 | `tests/test_shadow_execution.py::test_signal_pending_next_open_sizing_and_c1[50]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 6 | `tests/test_shadow_execution.py::test_strict_preflight_no_economic_commit_and_no_deferral[1-PENDING_ENTRY_BAR_MISSING]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 7 | `tests/test_shadow_execution.py::test_strict_preflight_no_economic_commit_and_no_deferral[2-HELD_POSITION_BAR_MISSING]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 8 | `tests/test_shadow_execution.py::test_strict_preflight_no_economic_commit_and_no_deferral[10-HELD_POSITION_BAR_MISSING]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 9 | `tests/test_shadow_execution.py::test_strict_api_is_mandatory` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 10 | `tests/test_shadow_execution.py::test_focused_champion_equivalence[gap_stop-90-120-89-stop_loss]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 11 | `tests/test_shadow_execution.py::test_focused_champion_equivalence[opening_target-120-121-90-profit_target]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 12 | `tests/test_shadow_execution.py::test_focused_champion_equivalence[ambiguity_stop_first-100-120-90-stop_loss]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 13 | `tests/test_shadow_execution.py::test_focused_champion_equivalence[target-100-120-99-profit_target]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 14 | `tests/test_shadow_execution.py::test_focused_champion_equivalence[max_hold-100-101-99-time_exit]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 15 | `tests/test_shadow_execution.py::test_reentry_after_exit_matches_champion` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 16 | `tests/test_shadow_execution.py::test_source_revisions_do_not_rewrite_old_decisions` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 17 | `tests/test_shadow_execution.py::test_crash_after_intent_rolls_back_and_restart_retries_once` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 18 | `tests/test_shadow_execution.py::test_concurrent_runners_commit_one_session` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 19 | `tests/test_shadow_execution.py::test_source_snapshot_is_read_only_and_provenance_changes_with_inputs` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 20 | `tests/test_shadow_execution.py::test_default_production_screen_path_is_used_read_only` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 21 | `tests/test_shadow_execution.py::test_strategy_unchanged` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 22 | `tests/test_shadow_execution.py::test_incomplete_economic_cross_section_preserves_state_until_complete` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 23 | `tests/test_shadow_execution.py::test_critical_stale_and_invalid_bars_use_cc01_errors[STALE-PENDING_ENTRY]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 24 | `tests/test_shadow_execution.py::test_critical_stale_and_invalid_bars_use_cc01_errors[STALE-HELD_POSITION]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 25 | `tests/test_shadow_execution.py::test_critical_stale_and_invalid_bars_use_cc01_errors[INVALID-PENDING_ENTRY]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 26 | `tests/test_shadow_execution.py::test_critical_stale_and_invalid_bars_use_cc01_errors[INVALID-HELD_POSITION]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 27 | `tests/test_shadow_hardening.py::test_complete_input_f_ranking_and_entry_unchanged` | B | Reiner Qualification-Test liefert keine History-Coverage-Evidenz; zwei Kandidaten bleiben ungelöst. |
| 28 | `tests/test_shadow_hardening.py::test_irrelevant_gaps_allow_rank_one_but_missing_selected_atr_does_not` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 29 | `tests/test_shadow_hardening.py::test_newest_daily_workflow_and_explained_sec_partial` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 30 | `tests/test_shadow_hardening.py::test_v1_pending_record_remains_verifiable_without_new_observation_field` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 31 | `tests/test_shadow_observation.py::test_observation_init_and_blocked_session` | A | AAA zusätzlich zu absichtlich unvollständigem BBB ungelöst; Census 2 ist für die alte Fixture korrekt. |
| 32 | `tests/test_shadow_observation.py::test_complete_observation_does_not_automatically_trade_and_explicit_activation` | A | Als vollständig vorgesehene Kandidaten ohne SIP-Historie; RANKING_BLOCKED statt Aktivierung. |
| 33 | `tests/test_shadow_observation.py::test_complete_economic_events_identical_to_pre_mode_runner` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 34 | `tests/test_shadow_journal.py::test_chain_validates_and_reconstructs_portfolio` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 35 | `tests/test_shadow_journal.py::test_tampering_detected_before_run[UPDATE events SET payload='{}' WHERE sequence_number=2-event hash mismatch]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 36 | `tests/test_shadow_journal.py::test_tampering_detected_before_run[UPDATE events SET previous_event_hash='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' WHERE sequence_number=2-previous hash linkage]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 37 | `tests/test_shadow_journal.py::test_tampering_detected_before_run[DELETE FROM events WHERE sequence_number=2-sequence continuity]` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 38 | `tests/test_shadow_journal.py::test_rehashed_inconsistent_state_still_rejected` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 39 | `tests/test_shadow_journal.py::test_rehashed_duplicate_session_commit_rejected` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |
| 40 | `tests/test_shadow_journal.py::test_uncommitted_tail_rejected` | A | Fehlende 300-Session-SIP-Historie; RANKING_UNRESOLVED verhindert vorgesehenen Pfad. |

## Änderungen und wiederverwendbare Helpers

- `tests/shadow_fixtures.py`: `official_history` liefert genau 300 offizielle Sessions;
  `current_sip_observation` validiert jede synthetische Bar; `seed_history` persistiert
  Alpaca/SIP/all-Evidenz mit explizit steuerbaren Lücken. `history_evidence` erzeugt
  konsistente Evidenz für reine Qualification-Tests; `assert_history` gleicht diese
  mit dem tatsächlichen unveränderten DB-Adapter ab. `incomplete_candidate`,
  `rejected_candidate` und `assert_census` machen Absicht und Erwartungen explizit.
- `tests/test_shadow_execution.py`: Gemeinsame Fixture erhält vor Inception die
  vollständige Historie bis 29.12.2023 für AAA, BBB und SPY. Bestehende Januar-Bars
  erweitern sie kausal. Assertions prüfen 300/300 sowohl an Inception als auch am
  ersten Signal-Tag. Zusätzlich ist die erste Cross-Section nachweislich ready und
  enthält zwei vollständig ausgewertete, eligible Kandidaten. Die Default-Row ist
  weiterhin eine deterministische Quality-Ablehnung (50 < F-Minimum 65).
- `tests/test_shadow_hardening.py`: Pure Qualification nutzt explizite Coverage.
  Der vollständige Ranking-Test verlangt genau zwei ausgewertete Kandidaten und
  LOWs `quality_threshold`-Ablehnung. Drei neue Fälle prüfen vollständig fehlende
  History, eine fehlende offizielle Session und eine Zero-Volume-History-Bar.
- `tests/test_shadow_observation.py`: Kontrolliert unvollständige BBB-Row über Helper;
  vollständiger Census für blocked (2 = 0 + 1 + 1) und complete (2 = 0 + 2 + 0).
- `tests/test_shadow_journal.py`: Unverändert; übernimmt die reparierte gemeinsame
  `case`-Fixture. Alle ursprünglichen Manipulations- und Verifikationsprüfungen bleiben.
- `.gitignore`: Ausnahme ausschließlich für diesen angeforderten Audit-Bericht,
  da `reports/*` standardmäßig ignoriert ist. Rohlogs und XML bleiben lokale Artefakte.

## Reachability und erhaltene Negativtests

| Pfad | Ausführbarer Nachweis |
| --- | --- |
| Zulässiges Ranking | Gemeinsame Fixture: `readiness.ready`, zwei vollständig ausgewertete und zwei eligible Kandidaten. |
| Intent und Fill | Drei ATR-Sizing-Fälle: ausgewähltes AAA, Pending für nächste offizielle Session, genau ein `ENTRY_EXECUTED`, Positionsgröße, C1-Stop und Target. |
| Exit | Fünf fokussierte Champion-Vergleiche verlangen je genau einen Exit und prüfen Stop/Target/Time-Exit samt ökonomischer Gleichheit. |
| Reentry | Zwei ausgeführte Entries, `is_reentry`, Entry-Datum und Exit-PnL gegen Champion. |
| Fehlende kritische Bars | Vor Manipulation jetzt explizit Pending/Intent beziehungsweise Position/Fill vorhanden; anschließend exakter CC01-Fehler, `DATA_PREFLIGHT_FAILED` und unveränderter wirtschaftlicher State. |
| History fehlt | Neue Fälle beweisen Coverage-Lücke, ausschließlich `RANKING_UNRESOLVED`, genau AAA mit `MARKET_HISTORY_UNPROVEN`, BBB vollständig ausgewertet; keine Journaländerung. Nach Reparatur genau ein Intent. |
| Ranking/ATR unvollständig | Bestehende Negativtests für selected ATR, stale Cross-Section, leere Cross-Section und fehlende Quality bleiben; kein vorschnelles No-Trade oder Hochstufen von Rang zwei. |
| Crash | `SystemExit` wird erst nach `ENTRY_INTENT_CREATED` ausgelöst; Rollback auf Genesis, danach Restart mit genau einem Intent/Fill. Observation-Crash erreicht `SESSION_COMMITTED` vor Rollback. |
| Concurrency | Genau ein wirtschaftlicher Commit beziehungsweise eine Observation trotz zwei Runnern; zweiter Aufruf `ALREADY_PROCESSED`. |
| Journal-Tampering | Bestehende Tests erzeugen erst echte Commit-Events und prüfen danach Hash-Link, Sequence, State-Replay, Duplicate-Session-Index oder uncommitted Tail mit genauer Fehlerursache. |
| Drift | Konfigurations-, Engine-, Ingestion- und Runtime-Drift-Tests bleiben einschließlich unverändertem Journal erhalten. |
| Observation | Blocked/complete Census explizit; keine wirtschaftlichen Transitions; Monkeypatch verhindert unbemerkten Economic-Adapter-Aufruf. |
| Unerlaubte Economic-Transitions | Bestehende Tests für injizierten Economic-Screen, unzulässigen Mode-Wechsel und fehlende Aktivierungsreife bleiben unverändert. |

Die `LocalInputs`-Coverage, Qualification, Entry-/Exit-Engine und Journal-Verifikation
werden nicht gemockt oder abgeschwächt. Fehlende Historie wird niemals als
wirtschaftlicher Ausschluss umgedeutet. Vorhandene Netzwerksperren bleiben aktiv.

## Produktionsregressionen

Keine Produktionsregression in den vier geprüften Dateien identifiziert; keine ungelösten Fehler. Alle 40 ursprünglichen Fehlschläge sind durch Fixture-/Testvertragsanpassungen behoben. Diese Aussage ist auf die vier angeforderten Dateien begrenzt; eine globale Regression-Suite wurde nicht ausgeführt.


## Abschließende Verifikation

| Datei | Vorher bestanden | Vorher fehlgeschlagen | Nachher bestanden | Nachher fehlgeschlagen |
| --- | ---: | ---: | ---: | ---: |
| test_shadow_execution.py | 32 | 26 | 58 | 0 |
| test_shadow_hardening.py | 39 | 4 | 46 | 0 |
| test_shadow_observation.py | 10 | 3 | 13 | 0 |
| test_shadow_journal.py | 6 | 7 | 13 | 0 |
| Gesamt | 87 | 40 | 130 | 0 |

Die drei zusätzlichen Hardening-Fälle prüfen `absent`, `one_session` und `zero_volume`.
Alle Reachability-Assertions aus der obigen Tabelle wurden im vollständigen Nachher-Lauf
ausgeführt. Insbesondere die zuvor blockierten Fill-/Exit-/Crash-/Journal-Pfade sind grün.
`ruff check` für alle vier geänderten Python-Dateien: **All checks passed**.
`git diff --check`: keine Whitespace-Fehler. `git diff --name-only -- src config`:
keine Änderungen. Test-Temporärverzeichnisse und der eigene Cache wurden nach dem Lauf
entfernt; Vorher-/Nachher-Rohprotokolle und JUnit-XML bleiben im Report-Verzeichnis.
