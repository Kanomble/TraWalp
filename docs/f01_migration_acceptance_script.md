# F01: manuelle Migration Acceptance G4–G6

`scripts/validate_f01_migration.py` führt ausschließlich lokale Abnahmeprüfungen
aus. Ohne Phasenflag läuft ein lesender Preflight. Die produktive Datenbank wird
nicht geöffnet. Source bedeutet den versiegelten Snapshot in
`reports/f01_copy_rehearsal/20261115_8f2a/reference/`, niemals `production_origin`.
Ein Rehearsal-GO autorisiert keinen produktiven Cutover.

## Bindung und Schutz

Das ursprüngliche Manifest und alle G3-Ergebnisse bleiben unverändert. Das Skript
prüft Vorbereitungssiegel, G3-Siegel, Ergebnis-Fingerprint, Runtime, jede ursprünglich
gebundene Produktions-/Config-/Input-Datei und die beiden additiven Reviews.
Der zusätzliche lokale Review `reports/f01_acceptance_additions_review.json` bindet
exakt Skript, dieses Dokument und den kleinen synthetischen Test an G0–G3.
Ein neuer HEAD allein invalidiert die Vorbereitung nicht. Änderungen bestehender
gebundener Dateien oder nicht geprüfte Ergänzungen stoppen den Lauf.

Windows-Datei- und Verzeichnishandles sperren Pfadaustausch und Schreibzugriffe auf
Source, Backup und die erfolgreiche Work-Datei. Nach dem Sperren werden Bindungen
erneut geprüft. Alle drei Dateihashes werden vor und nach einer Phase verifiziert.
SQLite öffnet diese Dateien mit `mode=ro&immutable=1` und `query_only=ON`.
Vorhandene WAL-, SHM- oder Journal-Dateien an Referenzen führen zum STOP.
Es gibt keinen Checkpoint und keine Sidecar-Löschung. `EXECUTION_ENABLED` bleibt
standardmäßig False. Nur ein Recovery-Kindprozess aktiviert ihn zeitweise für
seine neue, gebundene Kopie und verwendet den bestehenden Batch-Executor.

## Phasen und Aussagegrenzen

G4 verifiziert unveränderte Nicht-Fact-Tabellen gegen das gebundene vollständige
Inventar. Die unveränderten G3-Dateihashes binden den vollständigen Nachweis beider
geordneten Fact-Streams samt Original-IDs; deshalb werden sie nicht erneut komplett
gescannt. Die reproduzierbare Auswahl verwendet gleichmäßig verteilte, alphabetisch
sortierte Firmen (Standard 24, maximal 64), vorhandene Filing-/Amendment-/Perioden-
und Share-Altersgrenzen. Accounting, Source-Auswahl, TTM/Prior, PIT, DENSE_RANK,
Screener und F-Ranking nutzen Produktionsfunktionen. Legacy und Forward/SIP werden
getrennt verglichen. Marktpreise werden ausschließlich aus dem eingefrorenen Bestand
gelesen, Peers aus derselben festen Querschnittsmenge berechnet; Konfiguration,
Companies und Konfliktinputs sind identisch. Kein Backtest und kein Shadow-Runner.
Fehlende Rank-1-/Boundary-/Identity-Witnesses oder Marktinputs verhindern PASS.
Die Querschnittsränge sind keine Behauptung einer vollständigen Universe-Rangliste.

G5 benötigt `--authorize-g5-copy` und erstellt genau eine neue Wegwerfkopie. Nur
`IsolatedCanonicalSecWriter` schreibt darauf. Wiederholungen, Receipts, Korrekturen,
historische Reader-Versionen, neue Facts/Accessions/Amendments, Memberships, Anker,
externe Transaktionen, sechs injizierte Fehlerstellen und konkurrierende Writes
werden geprüft. Archiviertes Companyfacts-JSON wird aus dem eingefrorenen SEC-Cache
über ein Provider-Double und den Produktionsparser erneut eingelesen. Python-Socket-
Verbindungen sind während G5 gesperrt. Schema-Kreuzkombinationen werden auf der
Wegwerfkopie beziehungsweise einem zusätzlichen kleinen Legacy-Schema-Double gesperrt.
Fehlende archivierte Inputs und
fehlende Integrationsnachweise bleiben explizite Evidenzlücken. Regulärer SEC-Sync,
Force und Legacy-Backfill bleiben gemäß R2 ausgeschlossen; der Adapter ist keine
produktive Writer-Integration. Diese offene Readiness-/Sync-Integration wird niemals
automatisch als bestanden markiert.

G6 benötigt `--authorize-recovery-copies`. Es prüft das vollständige G3-Ledger und
Mapping, stellt die unveränderte Backup-Referenz in einer neuen Datei wieder her
und führt eine unabhängige SQLite-Restore-Probe der erfolgreichen Work-Datei auf
einer weiteren neuen Kopie aus. Beide Restore-Dateien erhalten Integrity-/FK-Prüfungen;
die Work-Restore-Probe zusätzlich vollständige logische Legacy-/Forward-Stream-Prüfungen.
Ohne reale Crash-/Resume-Evidenz bleibt das Ergebnis unzureichend. Nur bei diesem
konkreten Nachweisbedarf erlaubt zusätzlich `--rebuild-recovery` **einen** weiteren
Neubau aus dem Source-Snapshot. Dieser eine Lauf wird vor/nach dem ersten Batch-
Commit und während SQLite-VM-Arbeit im Indexaufbau absichtlich beendet und jeweils
mit demselben Manifest fortgesetzt. Falsche Manifest-/Ledger-/Schema-Bindungen werden
auf dieser Kopie in zurückgerollten Transaktionen geprüft. Die erfolgreiche Work-Datei wird dabei niemals
beschrieben. Physische Dateihashes werden separat aufgezeichnet; logische Mapping-
und Stream-Hashes müssen reproduzierbar sein, physische Layouts dürfen abweichen.
Keine automatische Löschung, kein Zurücksetzen existierender Kopien.

## Kapazität, Abbruch und Ergebnisse

Mindestens 20 GiB freie Reserve sind auf Arbeits- und Temp-Volume erforderlich.
Bereits angelegte Referenzkopien und Census-Dateien werden nicht erneut berechnet.
G5 budgetiert konservativ drei gemessene Work-Größen plus 1 GiB. G6 benötigt für
die beiden Restores zwei Work-Größen plus eine Source-Größe. Ein Recovery-Neubau
addiert `max(Source, 2 × Work)` für die neue Ergebnisdatei und jeweils
`max(Source, Work)` für Journal und Sortier-Scratch. Dies verwendet die bereits
gemessene erfolgreiche Work-Größe mit Layout-Puffer, keine angenommene Ersparnis
aus der Canonical-Fact-Anzahl.
Bereits angelegte neue Kopien/Journal-/Scratch-Bytes werden vom Restbudget abgezogen.
Ein unabhängiger Sampler zeigt jede Sekunde Phase, Prüfungsfortschritt, Laufzeit,
Parent-/Recovery-Kindprozess-RSS und Freiplatz;
SQLite-Progresshandler und Interrupt unterstützen kontrollierten Abbruch.
Bei Strg+C bleiben sämtliche Kopien und bestätigten Batch-Commits erhalten.
Ein abgebrochener Abnahmelauf wird nicht als erfolgreich veröffentlicht. Ein
Abnahmelauf hat kein automatisches Resume: erhaltene Recovery-Kopien werden weder
gelöscht noch stillschweigend wiederverwendet. Ein weiterer Neubau erfordert erneut
die bewusste Wahl von `--rebuild-recovery` wegen weiterhin fehlender Crash-Evidenz. Neue
manuelle Phasen erhalten neue Verzeichnisse, bestehende werden nie überschrieben.

Ergebnisse stehen unter `.../acceptance_runs/<UTC>_<phase>_<uuid>/result/`:

- `f01_migration_acceptance_g4.json`, `_g5.json` oder `_g6.json` je Phase;
- `f01_migration_acceptance_audit.md`, `progress.jsonl`,
  `result_fingerprint.json`, `result_seal.json`;
- die Gesamtbewertung schreibt `f01_migration_acceptance_assessment.json`.

Nur veröffentlichte Verzeichnisse mit gültigem Siegel und identischer G0–G3-/Code-
Bindung zählen. `failure.json` und `unpublished/` sind keine erfolgreichen Ergebnisse.
Fehlende Phasen/Nachweise ergeben `INSUFFICIENT_EVIDENCE`, nachgewiesene Fehler
`REHEARSAL_NO_GO`. Nur drei vollständig bestandene Phasen ohne offene Evidenz ergeben
`REHEARSAL_GO`. Lokale Reports/Reviews und Datenbankkopien enthalten sensible Daten
und sind durch die bestehende `.gitignore` ausgeschlossen. Nicht auf GitHub laden.

## PowerShell

Im Repository mit dem unveränderten gebundenen Python-Environment ausführen:

```powershell
Set-Location 'G:\GitHub\TraWalp'
$Python = 'G:\GitHub\TraWalp\.venv\Scripts\python.exe'
$G3 = 'G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a\g3_runs\20261115T120802_b1cbe578e8f94f969ba6474140477dd2\result'

# Ausschließlich lesender Preflight
& $Python -B scripts/validate_f01_migration.py --g3-result $G3

# Jede Phase separat starten; Ausgabe nennt ihr result-Verzeichnis.
& $Python -B scripts/validate_f01_migration.py --g3-result $G3 --g4
& $Python -B scripts/validate_f01_migration.py --g3-result $G3 --g5 --authorize-g5-copy
& $Python -B scripts/validate_f01_migration.py --g3-result $G3 --g6 --authorize-recovery-copies

# Nur falls fehlende reale Crash-/Resume-Evidenz den zusätzlichen Neubau erfordert:
& $Python -B scripts/validate_f01_migration.py --g3-result $G3 --g6 --authorize-recovery-copies --rebuild-recovery --batch-size 5000
```

Für die Schlussbewertung die drei **konkret ausgegebenen** `result`-Verzeichnisse
verwenden. Ein fehlgeschlagener Versuch darf stattdessen mit seinem Verzeichnis
einschließlich `failure.json` angegeben werden; er führt zu NO_GO. `unpublished/`
zählt niemals als erfolgreiche Phase:

```powershell
$G4Result = Read-Host 'Absolutes veröffentlichtes G4-result-Verzeichnis'
$G5Result = Read-Host 'Absolutes veröffentlichtes G5-result-Verzeichnis'
$G6Result = Read-Host 'Absolutes veröffentlichtes G6-result-Verzeichnis'
& $Python -B scripts/validate_f01_migration.py --g3-result $G3 --finalize --g4-result $G4Result --g5-result $G5Result --g6-result $G6Result
```

Exitcodes: 0 = Preflight verifiziert oder Phase bestanden; 2 = blockiert,
unzureichende Evidenz oder NO_GO; 130 = kontrollierter Abbruch.
Keine reale Abnahme wurde bei der Implementierung durchgeführt.
