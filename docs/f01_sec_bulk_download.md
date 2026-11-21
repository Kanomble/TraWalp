# F01 – Manueller SEC-Bulk-Abruf für Research

## Quellen und Datenqualität

| Quelle | Paket, Inhalt und Grenze |
| --- | --- |
| Companyfacts | Komprimierter aktueller Stand der aggregierten XBRL-Facts. Erwartete Members `CIK##########.json` mit CIK, Entity und Fact-Taxonomien; Perioden, Units, Form, Filing-Datum, Accession und gegebenenfalls Frame liegen in den einzelnen Beobachtungen. Die SEC erfasst standardisierte, auf die gesamte Filing-Entity bezogene Konzepte. XBRL wurde ab 2009 eingeführt. Es gibt keine einheitliche vollständige historische Coverage pro CIK, Form, Metrik oder Kontext. [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) · [Companyfacts ZIP](https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip) |
| Submissions | Komprimierter aktueller Stand der öffentlichen Filing-Historie je Filer. Erwartete Hauptdatei `CIK##########.json`, je nach Filer zusätzliche referenzierte `CIK##########-submissions-NNN.json`. `filings.recent` enthält mindestens ein Jahr oder 1.000 Filings, je nachdem welcher Umfang größer ist. Weitere Dateien tragen ältere Felder und Zeitbereiche. Aktuelle Namen/Ticker/Börsen und ehemalige Namen unterstützen die Suche. Sie begründen keine historischen Security-Intervalle. [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) · [Submissions ZIP](https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip) |
| Daily/Quarterly Index, Original-Filings | Eigenständiger Folgeauftrag. EDGAR-Indizes reichen bis 1994 Q3 und liefern u. a. CIK, Form, Filed-Datum und Pfad. Originaldokumente, Accession-Ordner und Header dienen der Rekonstruktion konkreter Filing-Versionen. Nachträgliche Änderungen sind möglich. [Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data) · [SEC Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions) |

Die SEC aktualisiert ihre JSON-APIs laufend und publiziert die Bulk-ZIPs nachts, ungefähr 03:00 Uhr Eastern Time. Der Abruf protokolliert pro Endpoint getrennte UTC-Zeiten, HTTP-Header und Hashes. Beide Dateien können verschiedene SEC-Generationen repräsentieren. Der gemeinsame lokale Ordner belegt die Zusammenstellung und enthält die Klassifikation `CURRENT_PROVIDER_RECONSTRUCTION`. [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

Die SEC erwartet einen identifizierbaren User-Agent mit Kontaktadresse und begrenzt automatisierte Zugriffe insgesamt auf höchstens zehn Requests pro Sekunde pro Nutzer, auch bei verteilten Clients. Das Skript sendet sequenziell höchstens einen Request pro Sekunde, nutzt 30 Sekunden Socket-Timeout und bis zu drei Versuche bei vorübergehenden Fehlern. `Retry-After` wird bis 60 Sekunden eingehalten. Längere Wartezeiten, 429 ohne brauchbaren Hinweis und permanente Fehler stoppen den Lauf. Andere parallele SEC-Clients beim eigenen Gesamtbudget berücksichtigen. [Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data) · [SEC Privacy and Security Policy](https://www.sec.gov/about/privacy-information)

## Zielpfade und Capacity-Vertrag

Fünf vorhandene, absolute, nicht verschachtelte Pfade auf lokalen festen Windows-Volumes anlegen. `originals` und `temporary` benötigen dasselbe Volume für die Veröffentlichung. Separate Verzeichnisse für Research-DB, Importartefakte und Backup verwenden. Backups können auf einem zweiten festen Volume liegen. Reserven werden je physischem Volume berechnet. Der Code weist Reparse-Points, verlinkte Dateien und die Repository-Bäume `data/` und `reports/` ab. Das autorisierte Arbeitsverzeichnis soll nur von den vorgesehenen Nutzern und Prozessen beschreibbar sein.

| Posten | Berechnung |
| --- | --- |
| ZIP-Originale und temporärer Download | Beide `Content-Length`-Werte addieren. Derselbe Satz Bytes wandert per Rename von Staging zu Originalen. Bereits belegte Bytes einer gebundenen Resume-Generation werden bei der benötigten zusätzlichen Downloadmenge abgezogen. Der vollständige ZIP-Betrag wird für den Backup-Satz separat angesetzt. |
| Pilot-Datenbank, Request-Archiv, SQLite-Sidecars | Drei getrennte Plangrößen in Bytes. Vor dem Pilot als begründete Budgets festlegen und danach mit gemessenen Werten ersetzen. |
| Import-/Indexaufbau, Restore | Zusätzlicher Arbeitsraum im Importpfad, einschließlich gezielter Extraktion, Sortier-/Index-Temp und Restore-Arbeitskopie. SQLite-/System-Temp-Umgebung später auf das geplante Volume binden. |
| Backup-Generation und Reserve | Backup enthält Platz für beide Original-ZIPs und den angegebenen zusätzlichen Sicherungssatz. Reserve pro Volume nur einmal zählen. Vorhandene Dateien sind bereits in der gemessenen freien Kapazität berücksichtigt. |

Der lokale Default zeigt ohne gespeicherte HEAD-Evidenz `CAPACITY_UNDETERMINED` mit Exitcode 3. `CAPACITY_OK` verlangt bekannte Größen für beide ZIPs und ausreichend freien Platz für sämtliche Budgets. Der Download wiederholt HEAD und Capacity-Prüfung unmittelbar vor den GETs. Falls der Server keine verlässliche Länge liefert, bleibt der Bulk-Transfer gesperrt. Die Budgetwerte für den Vollimport ergeben sich erst aus dem Pilot, einschließlich Peak von ZIP-Member-Decoding, SQLite, Indizes, Sidecars, Request-Archiv und Restore. Eine vollständige Extraktion ist nicht vorgesehen.

## PowerShell-Ablauf

Die folgenden Pfade sind Beispiele. Lokale Laufwerke und freien Platz vorher prüfen. Den Block je Planung einmal mit neuen Eigentümerpfaden ausführen. Er erzeugt Verzeichnisse und das Layout-JSON. An diesem Punkt entstehen keine Netzwerkanfragen.

```powershell
Set-Location 'G:\GitHub\TraWalp'
if ((git branch --show-current) -ne 'SEC-integration') { throw 'Falscher Branch' }
$f01Python = '.\.venv\Scripts\python.exe'
$f01Script = '.\scripts\download_f01_sec_bulk.py'
$f01Root = 'R:\TraWalpResearch'
$f01BackupRoot = 'S:\TraWalpResearchBackup'
$f01Paths = [ordered]@{
    originals = Join-Path $f01Root 'sec-originals'
    temporary = Join-Path $f01Root 'sec-staging'
    research_db = Join-Path $f01Root 'research-databases'
    import_artifacts = Join-Path $f01Root 'import-artifacts'
    backup = Join-Path $f01BackupRoot 'sec-backups'
}
foreach ($f01Path in $f01Paths.Values) {
    if (!(Test-Path -LiteralPath $f01Path)) {
        New-Item -ItemType Directory -Path $f01Path -ErrorAction Stop | Out-Null
    }
}
function Save-F01JsonNew([string]$Path, [string]$Json) {
    $f01File = [IO.File]::Open($Path, [IO.FileMode]::CreateNew)
    try {
        $f01Bytes = [Text.Encoding]::UTF8.GetBytes($Json)
        $f01File.Write($f01Bytes, 0, $f01Bytes.Length)
    } finally {
        $f01File.Dispose()
    }
}
$f01Layout = Join-Path $f01Paths.import_artifacts 'f01-sec-bulk-layout-v1.json'
$f01Paths['budget_bytes'] = [ordered]@{
    pilot_database = [long](Read-Host 'Platzbedarf Pilot-Datenbank in Bytes')
    request_archives = [long](Read-Host 'Platzbedarf SEC-Request-Archive in Bytes')
    sqlite_sidecars = [long](Read-Host 'Platzbedarf SQLite-Sidecars in Bytes')
    import_index_work = [long](Read-Host 'Platzbedarf Import und Indexaufbau in Bytes')
    backup_generation = [long](Read-Host 'Backup-Budget zusaetzlich zu beiden ZIPs in Bytes')
    restore_workspace = [long](Read-Host 'Zusaetzlicher Restore-Arbeitsraum in Bytes')
    safety_reserve = [long](Read-Host 'Positive Reserve pro Volume in Bytes')
}
Save-F01JsonNew $f01Layout ($f01Paths | ConvertTo-Json -Depth 4)
```

**1. Lokaler Capacity-Preflight.** Exitcode 3 mit unbekannten Größen beim ersten Aufruf ist erwartbar.

```powershell
& $f01Python -B $f01Script --layout $f01Layout
if ($LASTEXITCODE -notin @(0, 3)) { throw 'Lokaler Preflight fehlgeschlagen' }
```

**2. Autorisierter Netzwerk-Preflight.** Die zwei HEAD-Requests geben keine ZIP-Nutzdaten aus. Den Kontakt in der Form `Name/Organisation kontakt@domain.tld` eingeben. Bei fehlender Kapazität die JSON-Diagnose sichern und die Volumenplanung überarbeiten.

```powershell
$f01Ua = Read-Host 'SEC User-Agent mit echtem Betreiber und Kontaktadresse'
$f01Heads = & $f01Python -B $f01Script --layout $f01Layout --network-preflight --authorize-network --user-agent $f01Ua
$f01HeadExit = $LASTEXITCODE
if ($f01Heads) {
    $f01Sizes = Join-Path $f01Paths.import_artifacts 'f01-sec-bulk-head-pilot01.json'
    Save-F01JsonNew $f01Sizes ($f01Heads -join "`n")
}
if ($f01HeadExit -ne 0) { throw 'Netzwerk-/Capacity-Preflight blockiert' }
& $f01Python -B $f01Script --layout $f01Layout --sizes-from $f01Sizes
if ($LASTEXITCODE -ne 0) { throw 'Kapazitaetspruefung blockiert' }
```

**3. Manuell autorisierter Bulk-Download.** Eine neue, eindeutige Generation wählen. Dieser Schritt liest beide großen Archive, berechnet SHA-256 und prüft alle ZIP-Members. Fortschritt und Verifikation erscheinen auf stderr, der Ergebnisdatensatz auf stdout.

```powershell
$f01Generation = 'pilot01-' + (Get-Date).ToUniversalTime().ToString('yyyyMMdd-HHmmss')
$f01DownloadArgs = @(
    '--layout', $f01Layout, '--generation', $f01Generation, '--download',
    '--authorize-network', '--authorize-download', '--user-agent', $f01Ua
)
$f01DownloadReceipt = & $f01Python -B $f01Script @f01DownloadArgs
if ($LASTEXITCODE -ne 0) { throw 'Download nicht publiziert. Staging und Fehlerursache pruefen.' }
$f01Receipt = ($f01DownloadReceipt -join "`n") | ConvertFrom-Json
```

**Resume nach geeignetem Abbruch.** In derselben PowerShell-Sitzung die identische Generation und Argumente mit `--resume` verwenden. Das vorhandene Staging darf vorher weder umbenannt noch bearbeitet worden sein.

```powershell
$f01DownloadReceipt = & $f01Python -B $f01Script @f01DownloadArgs --resume
if ($LASTEXITCODE -ne 0) { throw 'Resume abgewiesen. Staging zur Pruefung erhalten.' }
$f01Receipt = ($f01DownloadReceipt -join "`n") | ConvertFrom-Json
```

Resume verlangt einen synchronisierten lokalen Checkpoint mit exakter Prefix-Länge und Prefix-SHA-256, denselben offiziellen Endpoint, dieselbe Gesamtgröße und denselben starken ETag. Für einen angefangenen, unvollständigen Member-Transfer muss der Server Bytes-Ranges ausweisen und mit exakt passendem HTTP 206, `Content-Range` und `Content-Length` antworten. Der Request trägt `If-Match` und `If-Range`. HTTP 200 als Antwort auf einen Range-Request, geänderte oder schwache ETags und unklare lokale Checkpoints stoppen die Fortsetzung vor dem Anhängen. Ein harter Prozessabbruch kann Bytes hinter dem letzten Checkpoint hinterlassen. In diesem Fall bleibt die Generation zur Prüfung erhalten. Eine neu autorisierte Generation erhält einen neuen Namen. Ob die SEC die notwendigen Validatoren aktuell liefert, zeigt erst der autorisierte Lauf.

**Vollständig heruntergeladene ZIPs offline finalisieren.** In derselben PowerShell-Sitzung die bereits verwendete Generation angeben. Nach einem Neustart `$f01Generation` auf den Namen des vorhandenen Staging-Verzeichnisses ohne den Suffix `.incomplete` setzen. Der Modus prüft beide Complete-Checkpoints, Bytezahlen, SHA-256, CRC und ZIP-Struktur, bevor er die beiden Originale samt Manifest veröffentlicht. Er benötigt keinen User-Agent und akzeptiert keine Netzwerkfreigabe. Fehlende oder unvollständige Checkpoints und ein bereits vorhandenes Ziel blockieren die Veröffentlichung.

```powershell
$f01OfflineReceipt = & $f01Python -B $f01Script --layout $f01Layout --finalize-offline --generation $f01Generation
if ($LASTEXITCODE -ne 0) { throw 'Offline-Finalisierung blockiert. Staging erhalten.' }
$f01Receipt = ($f01OfflineReceipt -join "`n") | ConvertFrom-Json
```

**4. Bereits vorhandene ZIPs lokal verifizieren.** Dieser Schritt erzeugt keine Netzwerkanfragen und entpackt keine Dateien. Er hasht beide Originale und prüft den CRC aller Members, sichere flache Namen, CIK-Abgleich, ZIP-Struktur sowie Verweise auf ältere Submissions-Members. Bei einzeln archivierten Fremddownloads muss Größe und SHA-256 aus unabhängig geprüfter Evidenz stammen.

```powershell
$f01ManifestSha = $f01Receipt.manifest_sha256
& $f01Python -B $f01Script --layout $f01Layout --verify --generation $f01Generation --manifest-sha256 $f01ManifestSha
if ($LASTEXITCODE -ne 0) { throw 'Archivverifikation fehlgeschlagen' }
# Einzelarchiv-Alternative unter dem Originals-Pfad
# & $f01Python -B $f01Script --layout $f01Layout --verify-archive $f01ExternalZip --endpoint companyfacts --expected-bytes $f01ReviewedBytes --expected-sha256 $f01ReviewedSha
```

Ein Companyfacts-Member mit exakt leerem JSON-Objekt `{}` erscheint unter `empty_companyfacts` mit Klassifikation `EMPTY_COMPANYFACTS`, Anzahl, sortierten Dateinamen und `import_eligible: false`. Der Member zählt zur ZIP-Struktur und wird weiterhin auf CRC geprüft. Der Zähler `issuers` enthält ausschließlich erfolgreich geprüfte, nicht leere Emittenten-Payloads. Nichtleere Payloads brauchen einen internen CIK, der zum Dateinamen passt. Submissions behalten ihre bestehenden Validierungsregeln. Beim späteren Import die in `archives.companyfacts.integrity.empty_companyfacts.members` erfassten Dateien ausschließen. Ein für den Pilot benötigter leerer Member ist eine Coverage-Lücke.

**5. Quellenmanifest übernehmen.** Download publiziert ZIPs und `f01_sec_bulk_source_manifest.json` gemeinsam in `originals/<generation>/`. Vorhandene Generationen und deren Originale sind schreibgeschützt und werden vom Skript nie ersetzt. Unter `temporary/<generation>.incomplete/` bleiben die Checkpoints erhalten. Erfolg wird ausschließlich am publizierten Originalordner mit geprüftem Manifest festgemacht. Archive des jeweiligen Backups mitsamt Manifest und extern gespeichertem Manifest-SHA-256 sichern. Ein späterer Backup-Restore an anderem Pfad benötigt ein getrenntes Review der Pfadbindung. Importer erhalten ausschließlich Leserechte auf die Originale.

```powershell
$f01ManifestPath = $f01Receipt.manifest
$f01ActualManifestSha = (Get-FileHash -LiteralPath $f01ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($f01ActualManifestSha -ne $f01ManifestSha) { throw 'Quellenmanifest veraendert' }
Get-Content -LiteralPath $f01ManifestPath -Raw
```

**6. Pilotimport vorbereiten.** Dieser Befehl bindet nur Metadaten. Das Layout reserviert einen isolierten DB-Pfad. Sobald der Benutzer das leere Research-Ziel separat mit dem Bootstrap provisioniert hat, liest der Befehl dessen Ergebnis-JSON, um die neue Dataset-ID zu binden. Er öffnet keine DB.

```powershell
$f01PilotSpec = (Resolve-Path -LiteralPath '.\docs\f01_sec_bulk_pilot_spec.json').Path
$f01ResearchTarget = Join-Path $f01Paths.research_db 'f01-sec-isolated-research-pilot01'
$f01BootstrapReport = Join-Path $f01ResearchTarget 'bootstrap_result.json'
$f01Bootstrap = Get-Content -LiteralPath $f01BootstrapReport -Raw -ErrorAction Stop | ConvertFrom-Json
if ($f01Bootstrap.lineage.classification -ne 'RESEARCH_ONLY') { throw 'Research-Lineage fehlt' }
$f01Handoff = [ordered]@{
    contract = 'F01_SEC_BULK_PILOT_HANDOFF_V1'
    source_generation = $f01Generation
    source_manifest = $f01ManifestPath
    source_manifest_sha256 = $f01ManifestSha
    pilot_spec = $f01PilotSpec
    pilot_spec_sha256 = (Get-FileHash -LiteralPath $f01PilotSpec -Algorithm SHA256).Hash.ToLowerInvariant()
    dataset_id = $f01Bootstrap.lineage.dataset_id
    bootstrap_manifest_sha256 = $f01Bootstrap.lineage.manifest_sha256
    bootstrap_report = $f01BootstrapReport
    bootstrap_report_sha256 = (Get-FileHash -LiteralPath $f01BootstrapReport -Algorithm SHA256).Hash.ToLowerInvariant()
    review_status = 'PENDING_IDENTITY_MEMBERSHIP_AND_IMPORT_AUTHORIZATION'
}
Save-F01JsonNew (Join-Path $f01Paths.import_artifacts "f01-pilot-handoff-$f01Generation.json") ($f01Handoff | ConvertTo-Json -Depth 5)
```

Exitcodes `0` erfolgreiche Prüfung oder Veröffentlichung, `2` ungültige Flags/Autorisierung, `3` fehlende Größe, unzureichende Kapazität oder abgewiesene Evidenz, `4` I/O- oder Transportfehler, `130` Tastaturabbruch.

## Vollständiger Offline-Struktur-Census

Der Diagnosemodus liest beide ZIPs aus `temporary/<generation>.incomplete/publish/` und die dazugehörigen Download-Checkpoints. Er berechnet SHA-256, prüft die Länge und liest jeden sicher lesbaren Member bis zur CRC-Prüfung. Er extrahiert keine Dateien und sammelt fachliche Befunde über beide Endpoints hinweg. Beschädigte oder vom Größenlimit betroffene Member werden im Integritätsteil ausgewiesen. Ist ein Zentralverzeichnis unlesbar, bleibt die Memberliste für dieses Archiv unbelegt. Der Agent hat den Census nur an kleinen synthetischen ZIPs erprobt.

Die Variablen aus dem Runbook weiterverwenden. Nach einem Neustart `$f01Generation` auf den bestehenden Namen ohne `.incomplete` setzen.

```powershell
$f01CensusResult = & $f01Python -B $f01Script --layout $f01Layout --structure-census --generation $f01Generation
$f01CensusExit = $LASTEXITCODE
if (!$f01CensusResult -or $f01CensusExit -notin @(0, 3)) { throw 'Census-Bericht nicht erstellt' }
$f01Census = ($f01CensusResult -join "`n") | ConvertFrom-Json
$f01Census.report
```

Der Bericht `f01_sec_bulk_structure_census.json` liegt unter `import_artifacts/<generation>-structure-census/`. Exitcode 0 meldet `CENSUS_COMPLETE`. Exitcode 3 mit Bericht meldet `CENSUS_BLOCKED` und enthält die beobachteten Befunde. Ein bereits vorhandenes Berichtsverzeichnis wird geschützt. Die Optionen für Download, Netzwerk und Publikation werden mit dem Census nicht kombiniert.

| Berichtsteil | Bedeutung |
| --- | --- |
| `archives.<endpoint>.integrity` | Erwartete und beobachtete Bytegröße und SHA-256, erfasste ZIP-Members, CRC-geprüfte Members und Integritätsbefunde. Link-, Pfad- und Duplikatverletzungen sowie fehlende Download-Bindung blockieren die Vertrauenswürdigkeit des Archivs. |
| `archives.<endpoint>.categories` | Anzahl pro Kategorie und Endpoint. Unter `key_groups` stehen die sortierten JSON-Schlüssel und die Dateinamen mit genau dieser Schlüsselmenge. `null` bedeutet, dass keine Objektschlüssel festgestellt werden konnten. Bei mehreren Auffälligkeiten erscheint derselbe Member in mehreren Kategorien. |
| `pilot.issuers` | Separater Befund je Companyfacts- und Submissions-Endpoint für alle acht Pilot-CIKs, einschließlich zugehöriger älterer Submissions-Chunks. `AFFECTED` zeigt einen konkreten Befund oder fehlenden Hauptmember an. `UNDETERMINED` zeigt fehlende Integrität ohne isolierten Pilotbefund an. |

`VALID` steht für die geprüfte Struktur. Das Feld `import_eligible` ist dafür nur bei belegter Archivintegrität wahr. Die eigentliche Identity-/Membership- und Importfreigabe ist davon unabhängig. Alle anderen Kategorien führen zu Quarantäne. `EMPTY_COMPANYFACTS` bezeichnet exakt `{}`; `EMPTY_FACTS` ein leeres `facts`-Objekt. `MISSING_INTERNAL_CIK` erfasst fehlende CIK-Felder bei Hauptpayloads. In diesem Fall wird der Dateiname als Fundort protokolliert. Er liefert keinen Ersatz für die interne Identität. `CIK_MISMATCH` erfasst auch intern gesetzte ungültige Werte wie `null`. `MISSING_FACTS`, `INVALID_PAYLOAD`, `INVALID_SUBMISSIONS` und `UNKNOWN_STRUCTURE` erfassen fehlende Pflichtstruktur, fehlerhafte JSON-/Taxonomie-Struktur, inkonsistente Submissions oder unerwartete ZIP-Member.

### Kriterien für eine spätere Publikationsentscheidung

Der Census selbst publiziert keine Archive. Der bestehende Finalisierer toleriert bereits `EMPTY_COMPANYFACTS` mit explizitem Importausschluss. Eine spätere Erweiterung um weitere Quarantänefälle bedarf einer separat geprüften, an Archiv-SHA-256 und Census-Hash gebundenen Regel. Dafür gelten folgende enge Kriterien.

1. Beide Originalarchive sind vollständig und unverändert. Checkpoints, Größe, SHA-256, ZIP- und alle CRC-Prüfungen bestehen. Archivpfade und Membernamen sind sicher und eindeutig.
2. Eine Allowlist kann ausschließlich bekannte Companyfacts-Formen aufnehmen. Neben `{}` kommen ein leeres `facts`-Objekt und ansonsten strukturell stimmige Payloads mit fehlendem internem CIK infrage. Regeln und konkrete Dateinamen, Schlüssel und Counts müssen im Review gebunden sein. Eine Ausnahme erteilt keine Importzulassung und ergänzt keinen CIK.
3. `CIK_MISMATCH`, `MISSING_FACTS`, `INVALID_PAYLOAD`, `INVALID_SUBMISSIONS`, `UNKNOWN_STRUCTURE` sowie jede Abweichung außerhalb der Allowlist bleiben blockiert. Ein fehlender interner CIK in Submissions bleibt blockiert.
4. Der Pilot besteht nur bei belegter Integrität beider Archive und soweit alle acht CIKs im vorgesehenen Scope ohne Struktur- oder Coverage-Befund bleiben. Eine Abweichung braucht eine separate fachliche Entscheidung. Der später verwendete Importer muss das gebundene Quarantäneinventar ausschließen.

## Pilot und historische Evidenz

Die [Pilot-Spec](f01_sec_bulk_pilot_spec.json) enthält acht deterministische Emittenten aus mehreren Sektoren und Fiskalkalendern. ACN/LLY haben lokale Vergleichs-Payloads. Die LLY-Accession `0000950123-10-070596` vom 02.08.2010 ist ein belegter 10-Q/A-Fall außerhalb des angestrebten Zeitfensters. JSM ist eine getrennte gesperrte Identity-Negativprobe mit zwei CIKs. Eine historische Security-Epoche oder Membership muss unabhängig geprüft werden. Je Emittent die primären Members und alle referenzierten historischen Submissions-Members direkt aus dem ZIP lesen. Für die spätere DB den genauen Scope erst nach Fact-/Accession- und Kapazitätsprüfung festlegen. [Lokale G5-Evidenz](../reports/f01_g5_input_evidence_preparation_audit.md) · [G4-Identity-Census](../reports/f01_acceptance_contract_resolution_audit.md)

Den Testzeitraum 2020 bis zur letzten für den jeweiligen Scope qualifizierten Session 2026 planen. Der vorhandene Reader kann ab Ende 2015 und punktuell früher liegende Inputs brauchen. Deshalb zunächst die vollständigen relevanten Companyfacts-Beobachtungen der Pilot-CIKs erhalten. Filing-Zeit, Wirtschaftsdaten von–bis und Retrieval-Zeit getrennt führen. Für die Validierung 10-K, 10-Q, Instant- und Duration-Kontexte, Units, Accessions sowie Referenzzeiträume nachweisen. [Historischer Reader](../src/trading_system/backtest/features.py#L279) · [Machbarkeitsaudit](../reports/f01_clean_database_rebuild_feasibility_audit.md)

Bei unzureichendem Bulk-Platz kommt nach separater Autorisierung ein CIK-Pilot über die offiziellen Companyfacts-/Submissions-Endpunkte in Frage. Größen und tatsächliche Retrievalläufe je CIK und je älterem Submissions-Member erfassen, Rohdaten und Hashes getrennt archivieren, identische Fair-Access- und Reviewregeln anwenden. Dieser kleinere CIK-Abruf ist ein separater Quellentyp mit eigenem Manifest. Das Bulk-Skript fragt diese Endpunkte nicht automatisch ab. [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)

Für PIT separat autorisierte Original-Filings und Indexdateien je CIK und Accession binden. Für ein Amendment Original und Amendment-Accession, Form, Filed-/Acceptance-Zeit und tatsächliche Dokumentversion getrennt belegen. Für Same-Key-Korrekturen zwei zeitlich gehashte Beobachtungen desselben kanonischen Schlüssels oder eine belegte Korrekturkette verlangen. Die historische Verfügbarkeit braucht eine konservativ belegte Session-Grenze, weil Acceptance und erste Verfügbarkeit auseinanderfallen können. Heutige Sicht niemals durch historisches `filed` oder spätere Revision rückdatieren. Bootstrap-Manifest, neue Dataset-ID, unveränderter A‑V1-Guard und Source-Manifest bleiben getrennt nachvollziehbar. Bei einem später ausgeführten Bootstrap den Code-Fingerprint aus Task 8F.3D.2 prüfen und per `--expect-code-fingerprint` übergeben. [SEC Webmaster FAQ](https://www.sec.gov/about/webmaster-frequently-asked-questions) · [Bootstrap-Audit](../reports/f01_clean_canonical_bootstrap_audit.md) · [Akquisitionsaudit](../reports/f01_sec_bulk_acquisition_audit.md)
