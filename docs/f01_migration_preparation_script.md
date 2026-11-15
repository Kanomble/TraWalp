# F01 G0–G2 mit dem Standalone-Skript vorbereiten

Das Skript wurde ausschließlich auf kleinen synthetischen Fixtures geprüft.
Es führt niemals eine Migration aus; `EXECUTION_ENABLED` bleibt False.

## Read-only Preflight

```powershell
Set-Location -LiteralPath 'G:\GitHub\TraWalp'
& '.\.venv\Scripts\python.exe' '.\scripts\prepare_f01_migration.py'
```

Dieser Modus schreibt keine Dateien und erstellt keine Kopien. Er liest nur
Quellheader/Metadaten sowie Code-/Config-/Paketmetadaten und prüft native Handles,
Pfade und Freiplatz. Kein SQL oder vollständiger DB-Hash/Census an der realen
Quelle. Auch ohne eines der beiden Bestätigungsflags bleibt es beim Preflight.

Windows ist erforderlich. Ein gefülltes WAL/Rollback-Journal, inkompatible offene
Handles, unklare Headerkonsistenz, verlinkte Pfade oder Platzmangel führen zu STOP.
Hauptdatei und vorhandene Sidecars werden nativ gegen Schreiben/Ersetzen gesperrt.
Erst unter diesen Sperren und bei leerem/abwesendem WAL ist SQLite
`mode=ro&immutable=1` erlaubt. Kein Checkpoint, kein Löschen von Quell-Sidecars.

## Erforderliche lokale Consumer-/Input-Prüfung

Der Preflight zeigt `repository_consumers` und eine `consumer_review_template`.
Automatisch werden Repository-Referenzen ermittelt; unbekannte externe SQL-,
Schema-, ID- und Cache-Consumer lassen sich nicht automatisch als geklärt erklären.
Nach tatsächlicher Prüfung die Vorlage unter folgendem lokalen Pfad ausfüllen:

`G:\GitHub\TraWalp\reports\f01_consumer_review.json`

```json
{
  "git_head": "EXAKTER_WERT_AUS_PREFLIGHT",
  "code_fingerprint": "EXAKTER_WERT_AUS_PREFLIGHT",
  "reviewer": "Name und nachvollziehbare Review-Referenz",
  "repository_consumers_reviewed": true,
  "external_inventory_complete": true,
  "inputs_complete": true,
  "unknown_consumers": [],
  "external_files": []
}
```

`external_files: []` ist die explizite Feststellung, dass keine weiteren
erforderlichen externen Dateien existieren. Andernfalls je Datei
`{"path": "ABSOLUTER_PFAD", "sha256": "VERIFIZIERTER_HASH"}` angeben.
Offene Fragen in `unknown_consumers` belassen; dann wird nicht READY erzeugt.
HEAD und Fingerprint müssen zum aktuellen Preflight passen. Der Fingerprint
bindet Code, lokale Config einschließlich `.env` als Hash ohne Geheimniswerte
sowie installierte Paketversionen, Python und SQLite. Änderungen verlangen Review.
Vor Veröffentlichung wird diese Evidenz erneut geprüft. Nicht vom Executor
unterstützte Fact-Views/-Trigger/-Fremdschlüssel werden unabhängig davon gesperrt.

## Vorbereitung starten

Vorher alle DB-Reader, DB-Browser, Syncs und Writer schließen/stoppen.

```powershell
Set-Location -LiteralPath 'G:\GitHub\TraWalp'
& '.\.venv\Scripts\python.exe' '.\scripts\prepare_f01_migration.py' `
    --prepare --confirm-no-writers `
    --consumer-evidence 'G:\GitHub\TraWalp\reports\f01_consumer_review.json'
```

Das vollständige Jobverzeichnis muss fehlen:
`G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a`.
Es werden keine bestehenden Dateien überschrieben, auch nicht die bisherigen
Phase-A-Berichte oder der Manifestentwurf. Teildateien eines fehlgeschlagenen Jobs
erfordern manuelle Prüfung; kein automatisches Löschen und kein stiller Resume.

Es gelten die dokumentierten Datenpfade:

- `reference\source.sqlite3`: konsistenter SQLite-Backup-Export; nur das neue
  Ziel wird bei Bedarf auf DELETE-Journal normalisiert.
- `reference\backup.sqlite3`: verifizierte bytegleiche Sicherung.
- `restore\restore.sqlite3`: unabhängiger SQLite-Restore, mit Integrity-/FK-/Schema-,
  Tabellen-/Inhaltshash- und Fact-Stream-Abgleich.
- `work\canonical_rehearsal.sqlite3`: bytegleiche Backup-Kopie ohne Migration.
- `scratch\`: eigener Census und Sortierbereich. TMP/TEMP werden nur im aktuellen
  Prozess geändert und anschließend zurückgesetzt.

Alle Snapshot-Tabellen erhalten Zähler, Spalten-/FK-Inventar und geordnete
Inhaltshashes. Das umfasst vorhandene Company-/Asset-/Identity-, SEC-/Accession-
und SIP-/Marktdaten. Schemaobjekte, PRAGMAs, Code-/Config-/Runtime-Fingerprints,
Consumer-Review und externe Inputs werden manifestiert. Fehlende Evidenz wird
nicht durch eine automatische Vollständigkeitsbehauptung ersetzt.

Die tatsächlichen Fact-/Canonical-/Membership-/Alias-/Inhaltskonfliktzahlen werden
erhoben und strikt mit den Audit-Sollwerten verglichen. Keine automatische Toleranz
und kein CLI-Bypass. Bei Konflikten enthält `content_conflicts.jsonl` sämtliche
betroffenen Originalzeilen; Zählerabweichungen stehen in `count_differences.json`.

Startbudget: 15 Quell-Dateigrößen + 20 GiB auf dem Zielvolume. Es umfasst vier
Kopien, Archiv, Canonical/Occurrence/Revision, Indizes/Ledger, Journal, Scratch
und Unsicherheit. Kein garantierter Storage-Gewinn. Freiplatz, Schritt, Laufzeit,
RSS und Peak-RSS werden spätestens alle fünf Sekunden geprüft/protokolliert;
unter 20 GiB STOP. Keine tatsächliche Platzreservierung. Volle Hash-/Inventar-/
Census-Läufe können lange dauern.

## Erfolg und Abbruch

Erfolg gilt ausschließlich nach atomarer Veröffentlichung unter `verified`:

- `verified\f01_copy_rehearsal_manifest.json`
- `verified\f01_real_migration_preparation_audit.md`
- `verified\f01_preparation_report.json`
- `verified\seal.json` mit zusammenpassenden SHA-256-Werten

`publication.pending` ist niemals eine Freigabe, auch wenn darin schon fertige
Berichtstexte stehen. Vorhandene Produktionsfunktionen für Datei-/Schema-/Fact-
Hashes und Manifestvalidierung werden wiederverwendet. `G0_G2_READY` autorisiert
keinen Migrationslauf, keine Provider-/Sync-Aktion und keine Writer-Aktivierung.

Bei Fehler/Strg+C bleiben Teildateien erhalten; `failure.json` und `failure.md`
werden soweit möglich exklusiv neu geschrieben. Exitcodes: 0 = reiner Preflight
oder vollständig veröffentlichte Vorbereitung; 2 = STOP/BLOCKED; 130 = Strg+C.
Eine vorhandene SQLite-Datei allein ist nie ein Erfolgssignal.

Manifest und Betriebsdaten bleiben im ignorierten `reports`-Bereich. Das Skript
prüft die Git-Ignore-Regel. Keine sensiblen Artefakte mit `git add -f` oder Uploads
veröffentlichen. Produktive Konfiguration und Executor werden nicht verändert.
