# F01 G3: Standalone-Copy-Migration

`scripts/run_f01_migration.py` ist ein Wrapper um den bestehenden
`canonical_migration.execute_batches`-Executor. Es gibt keine zweite
Migrationsimplementierung. Bei der Entwicklung werden ausschließlich kleine
synthetische Fixtures verwendet; Codex führt keinen dieser Befehle am realen
Bestand aus.

## PowerShell

Read-only Preflight (kann vollständige Hash-/Stream-Leseprüfungen benötigen):

```powershell
Set-Location -LiteralPath 'G:\GitHub\TraWalp'
& '.\.venv\Scripts\python.exe' '.\scripts\run_f01_migration.py'
```

Migration ausschließlich auf der bereits vorbereiteten Work-Datei:

```powershell
Set-Location -LiteralPath 'G:\GitHub\TraWalp'
& '.\.venv\Scripts\python.exe' '.\scripts\run_f01_migration.py' `
    --execute --batch-size 10000
```

Wiederanlauf, mit gültigem Commit-Ledger und unveränderter Bindung:

```powershell
Set-Location -LiteralPath 'G:\GitHub\TraWalp'
& '.\.venv\Scripts\python.exe' '.\scripts\run_f01_migration.py' `
    --execute --resume --batch-size 10000
```

Read-only Resume-Prüfung: derselbe Befehl mit `--resume` ohne `--execute`.
Batchgröße: 1–50.000. Keine Source-/Work-/Manifest-Overrides oder Environment-
Aktivierungsschalter. Sämtliche DB-Zugriffe anderer Anwendungen auf die Kopien
vorher schließen; ein exklusiver G3-Prozess-Lock verhindert konkurrierende Wrapper.

## Unveränderliche G0–G2-Bindung

Der Wrapper liest ausschließlich die veröffentlichte Vorbereitung unter:

`G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a\verified`

`seal.json` muss exakt Manifest, G0_G2_READY-JSON und Audit mit passenden Hashes
binden. Vertrag, Plan, Counts, Snapshot-Identitäten, Restore-Evidenz und die
ursprünglichen Consumer-/Input-Erklärungen müssen vollständig sein. Quelle,
Backup, Restore und Work müssen an den dokumentierten absoluten Positionen liegen.
Symlinks, Junctions, Hardlinks oder ausgetauschte Datei-IDs führen zu STOP.

Alle ursprünglich gebundenen Produktions-/Config-Dateien, externen Inputs,
Python-/SQLite-/Paketversionen und deren Fingerprints müssen unverändert sein.
Das Manifest wird weder verändert noch ergänzt. Genau dieses JSON-Objekt wird
dem Executor zur Berechnung seiner persistenten Manifestbindung übergeben.

Source/Backup werden vollständig gegen ihre gebundenen Datei-Hashes geprüft.
Source-Schema-/Fact-Stream-Hash werden mit den vorhandenen Produktionsfunktionen
geprüft. Initial wird Work zusätzlich bytegleich verifiziert: Da Source und
Backup ebenfalls bytegleich sind, gilt dadurch auch die volle initiale
Work-Stream-Bindung ohne einen redundanten zweiten Stream-Scan. Restore-Hash und
Inventar-Fingerprint werden geprüft. Der Produktivpfad `production_origin`
bleibt historische Provenienz des G1-Exports, kein neu gelesener G3-Input.

Native Handles bleiben bis nach dem Schließen der Work-Verbindung bestehen.
Referenzdateien, gebundener Code und Evidenz verweigern Write-/Delete-Sharing;
Work verweigert Delete-Sharing und wird nur für den Executor beschreibbar
geöffnet. Vorhandene inkompatible DB-Handles führen zu STOP. Keine Provider,
kein Sync, keine produktive Writer-Aktivierung und keine Shadow-Ausführung.

## Explizit geprüfte additive Änderung

Die lokale, separat geprüfte Datei
`G:\GitHub\TraWalp\reports\f01_copy_migration_additions_review.json`
bindet das ursprüngliche Manifest und exakt diese drei zusätzlichen Dateien:

- `scripts/run_f01_migration.py`
- `docs/f01_migration_rehearsal_script.md`
- `tests/test_run_f01_migration.py`

Jede Datei ist SHA-256-gebunden. Der Wrapper kann diese Erklärung nicht erzeugen
oder automatisch neu genehmigen. Änderungen erfordern eine neue statische Prüfung
und eine neue additive Erklärung; das versiegelte Manifest bleibt bestehen.

Eine andere Git-HEAD-ID allein ist kein Fehler. Der Git-Diff seit der ursprünglichen
HEAD darf ausschließlich diese additiven Dateien enthalten; keine Modifikationen,
Löschungen, Umbenennungen oder sonstigen neuen Dateien. Zusätzlich werden alle
alten Datei-/Input-/Runtime-Hashes tatsächlich verglichen. Ein neuer Commit mit
denselben zulässigen Additionen erzwingt deshalb keine erneute DB-Vorbereitung.
Produktions- oder Input-Drift wird niemals über eine neue HEAD oder ein neues
Fingerprint-Label akzeptiert.

## Ausführung und Resume

Standardmäßig werden nur bestehende Dateien und kalte SQLite-Snapshots gelesen;
keine Ausgabe-/Lock-/Scratch-Dateien und keine DB-Recovery. Bei einem Work-Journal
verweigert der read-only Preflight die Öffnung. Ein Hot-Journal darf ausschließlich
mit `--execute --resume` von SQLite auf Work recovered werden, nachdem sämtliche
Code-/Manifest-/Referenz-/Datei-ID-Gates bestanden wurden.

Es gibt kein automatisches Löschen, Neuaufbauen oder Kopieren von Work.
Ein Resume verlangt das existierende Ledger mit passender Manifest-/Executor-
Bindung, geprüftem Schema, unverändertem Import-Archiv, Mapping-Hashkette und
bestätigtem Cursor. Die ursprüngliche Work-Datei-ID muss erhalten sein.
Ein Abbruch vor dem ersten Vorbereitungs-Commit kann kein Resume-Ledger hinterlassen;
dann STOP und manuelle Prüfung, kein impliziter Neustart.

Der unveränderte Executor besitzt den Ablauf für DDL, Batches, Commit-Ledger,
Indexaufbau, Views und COMPLETE. Der Wrapper öffnet die gepinnte Work-Verbindung
selbst, um vor allen langen Resume-/SQLite-Operationen den Interrupt-/Progress-
Handler zu installieren. Erst nach sämtlichen Gates setzt er im eigenen Prozess
`EXECUTION_ENABLED=True` während des Executor-Aufrufs und stellt anschließend
unbedingt `False` wieder her. Die Produktionsdatei und ihr Standardwert werden
nicht geändert. Kein generischer CLI-/Environment-Bypass wird installiert.

## Verbleibendes Budget und unabhängige Überwachung

Die vier vorhandenen Kopien und der vorhandene Census-Scratch werden mit ihren
aktuellen Größen protokolliert. Sie sind bereits belegt und werden nicht erneut
als zukünftiger Freiplatzbedarf berechnet oder automatisch gelöscht.

Konservative Planbasis S ist die verifizierte Source-Dateigröße:

- Work-Endhülle 8 S einschließlich vorhandenem Import-Archiv, neuer Daten,
  Indizes und Unsicherheitszuschlag; nur noch nicht belegtes Wachstum zählt.
- Journal-Hülle 2 S abzüglich bereits belegtem Work-Journal.
- Neue G3-Sortier-Hülle 2 S abzüglich bereits belegtem aktuellen G3-Scratch.
- Mindestens 20 GiB freie Sicherheitsreserve.

Das sind beim initialen Work ungefähr 11 S + 20 GiB, nicht das ursprüngliche
15-S-Startbudget. Bereits gespeichertes Wachstum wird berücksichtigt. Ein bereits
atomar COMPLETE-Dataset benötigt für eine erneute Validierung nur die Reserve.
Die Hüllen sind vorsichtige Planannahmen, keine garantierte reale Speicherprognose.
Es wird keine Einsparung aus weniger Canonical-Facts abgeleitet.

Ein eigener Sampler prüft jede Sekunde Freiplatz, RSS/Peak-RSS, Laufzeit sowie
Work-/Journal-/Scratch-Größen. Er läuft unabhängig von SQLite-Callbacks, auch
während Indexaufbau, Integritätsprüfungen und langen SQL-Operationen. Bei
Budgetverletzung fordert er STOP an und ruft `Connection.interrupt()` auf. Der
SQL-Progress-Handler prüft zusätzlich die Abbruchanforderung. Keine langen
SQLite-Operationen ohne Beobachtung bis zum nächsten Batch-Hook.

Anzeige: Phase, bestätigte Occurrences/Gesamtzahl, Batch-Commits, Prozent,
Durchsatz, Import-ETA, RSS und Freiplatz. Durchsatz gilt für neue Imports dieses
Versuchs; ein alter Resume-Cursor wird nicht als in Sekunden neu importiert
gezählt. Import-ETA schätzt keine Index-/Validierungsdauer und ist ohne neue
Imports unbekannt. 100 % Occurrences allein bedeutet noch keinen G3-Abschluss.

Strg+C fordert kontrollierten Abbruch an. Die aktuelle SQLite-Transaktion wird
zurückgerollt; bestätigte Batches bleiben erhalten. Bei hartem Prozessabbruch
greift beim ausdrücklich autorisierten Resume die SQLite-Journal-Recovery.

## G3-Abnahme und Artefakte

Die versiegelten Manifest-Sollzahlen sind die Autorität; im vorbereiteten
Bestand: 18.698.090 Occurrences, 8.329.908 Canonical Facts, 8.617.872 Fact/Symbol-
Memberships und 180.283 Aliasgruppen. Nicht durch hart codierte Toleranzen ersetzen.

Der vorhandene Executor vergleicht beide vollständigen geordneten Legacy-/Forward-
Streams mit dem Originalarchiv, prüft Integrity/FKs und publiziert atomar COMPLETE.
Vor diesem finalen Commit prüft der Wrapper zusätzlich Memberships, Aliasgruppen,
Instant-/Duration-Zähler und die vollständige eindeutige Original-ID-Zuordnung.
Fehlschlag rollt die Finalisierung einschließlich COMPLETE zurück. Ein bereits
COMPLETE-Resume wird erneut gelesen/validiert. Source-/Backup-Hashes werden nach
dem Lauf erneut geprüft; Work erhält nach geschlossenen Connections unter einer
zusätzlichen Schreibsperre einen Datei-Hash und einen Ergebnis-Fingerprint.

Jeder `--execute`-Versuch erhält ein neues Verzeichnis unter
`...\20261115_8f2a\g3_runs\<UTC>_<UUID>`; bestehende Artefakte werden nicht ersetzt.
Auch ein späterer BLOCKED-/ABORTED-Versuch ist vom vorherigen getrennt.

- `progress.jsonl`: dauerhafter Fortschritt einschließlich Ressourcenmessungen.
- `result\f01_copy_migration_g3_audit.md`
- `result\f01_copy_migration_g3_report.json`
- bei Erfolg `result\f01_copy_migration_g3_fingerprint.json`
- `result\result_seal.json`: SHA-256 der Ergebnisartefakte.

Nur das atomar veröffentlichte `result` mit passendem Seal zählt. `result.pending`
ist kein Erfolgsnachweis. Status: `G3_COMPLETE`, `BLOCKED` oder `ABORTED`.
Die interne Work-Tabelle kann nach einem erfolgreichen Commit bereits COMPLETE
sein, obwohl eine nachgelagerte Datei-/Artefaktprüfung fehlschlägt; dann bleibt
der Versuch BLOCKED und ein expliziter Resume kann die Abnahme erneut durchführen.

Exitcodes: 0 = read-only Preflight erfolgreich oder G3_COMPLETE, 2 = BLOCKED,
130 = Strg+C/ABORTED. `G3_COMPLETE` ist weder G4–G6-GO noch Cutover-GO.
Sensible Dateien bleiben lokal im ignorierten `reports`-Bereich. Kein Upload,
kein Commit/Push und kein Umschreiben historischer Research-Artefakte.
