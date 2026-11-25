# F01 · Historical 50 Manual Ingestion Runbook

Dieser Ablauf gehört zu Task 9. Jede Phase wird einzeln in Windows PowerShell gestartet. Für den ersten Lauf ist ein NKE-10-Q ausgewählt. Die Originalbytes, DTS-Closure, Source- und Accounting-Reviews sowie die reale Aufnahme stehen aus. Die Schritte 1 und 2 sind offline. Alle Befehle ab der ausdrücklichen Ausführung sind für einen späteren manuellen Lauf vorgesehen. Verwende bei Resume dasselbe Arbeitsverzeichnis und dieselben genehmigten Eingabedateien.

## 1 · Umgebung und Read-only-Preflight

```powershell
Set-Location -LiteralPath 'G:\GitHub\TraWalp'
$Py = 'G:\GitHub\TraWalp\.venv\Scripts\python.exe'
$Cli = 'G:\GitHub\TraWalp\scripts\run_f01_historical_50_manual.py'
& $Py -B $Cli select --accession '0000320187-16-000242'
if ($LASTEXITCODE -ne 0) { throw "Task 9 preflight failed ($LASTEXITCODE)" }
```

Der Preflight prüft Branch, Code-Fingerprint, D.11-Manifest und dessen Mitgliedshashes, beide D.6-Eingänge und die ausgewählten Submissions-ZIP-Member. Er bindet CIK `0000320187`, NKE, den 10-Q vom 06.01.2016, Acceptance `2016-01-06T22:17:58Z` und das Primärdokument `nke-11302015x10q.htm`. Er schreibt JSON auf stdout. Zur Kapazitäts- und Datenträgerprüfung:

```powershell
& $Py -B -c "import shutil; print({d: shutil.disk_usage(d).free for d in ('G:/','F:/')})"
Get-Partition -DriveLetter G,F | Select-Object DriveLetter,DiskNumber,PartitionNumber
```

## 2 · Auswahl und Ausgabepfade

Setze pro Teilbatch einen neuen Namen. `select` akzeptiert ein bis vier explizite `--accession` aus dem gebundenen D.11-Entwurf. Dieser V1-Adapter lässt Original-10-K/10-Q zu. Amendments erhalten einen eigenen Supersession-Review in einer späteren Erweiterung. Die alte Sieben-Issuer- und die geplante 50er-Lineage verwenden getrennte Pfade.

```powershell
$Artifacts = 'G:\TraWalpResearch\import-artifacts\f01-task9-ingestion-preparation-v1'
$Job = Join-Path $Artifacts 'f01-task9-nke-01'
$Selection = Join-Path $Artifacts 'nke-01-selection.json'
$Target = 'G:\TraWalpResearch\research-databases\f01-sec-isolated-research-historical50-v1\f01-sec-candidate.sqlite3'

function Get-Task9Sha([string]$Path) {
    (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}
function Invoke-Task9([string[]]$Command, [int[]]$Accepted = @(0)) {
    $lines = & $Py -B $Cli @Command
    $code = $LASTEXITCODE
    if ($code -notin $Accepted) { throw "Task 9 exit $code" }
    ($lines -join "`n") | ConvertFrom-Json
}
function Save-NewTask9Json([object]$Record, [string]$Path) {
    $json = ConvertTo-Json -InputObject $Record -Depth 40
    $file = [IO.File]::Open($Path, [IO.FileMode]::CreateNew)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json + "`n")
        $file.Write($bytes, 0, $bytes.Length)
        $file.Flush($true)
    } finally { $file.Dispose() }
}

$Plan = Invoke-Task9 @('select','--accession','0000320187-16-000242')
Save-NewTask9Json $Plan $Selection
$SelectionSha = Get-Task9Sha $Selection
$Common = @('--job',$Job,'--selection',$Selection,'--selection-sha256',$SelectionSha)
```

Bei Neustart `$Plan` aus der bestehenden Auswahl lesen und den zuvor gesicherten Auswahlhash verwenden. Ein abweichender Code-Fingerprint braucht eine neue fachliche Bindung. Die Auswahl selbst enthält `PENDING_MANUAL_APPROVAL`.

## 3 · SEC-Acquisition

```powershell
$Download = Invoke-Task9 (@('download') + $Common)
$DownloadDraft = Join-Path $Artifacts 'nke-01-download-draft.json'
Save-NewTask9Json $Download.authorization_template $DownloadDraft
```

Der Reviewer übernimmt `contract`, `operation` und `binding` unverändert in eine neue Datei `nke-01-download-approved.json`. Erst nach eigenständiger Prüfung trägt er `APPROVED`, seinen Namen und `reviewed_at` mit Zeitzone ein. Den SHA-256 separat vom Reviewer übernehmen. Setze `TRAWALP_SEC_USER_AGENT` für die aktuelle Session auf deinen Organisations- oder Operatornamen plus Kontakt-E-Mail. Lass ihn aus Repo, Manifesten und Protokollen heraus.

```powershell
$DownloadApproval = Join-Path $Artifacts 'nke-01-download-approved.json'
$DownloadApprovalSha = Read-Host 'SHA-256 aus dem Download-Review'
Invoke-Task9 (@('download') + $Common + @(
    '--execute','--approval',$DownloadApproval,'--approval-sha256',$DownloadApprovalSha
))
```

Der Prozess nutzt das vorhandene SEC-HTTP-Verhalten und schreibt Index, explizites Primärdokument, verfügbare separate Instanz sowie zugehörige Filing-Schemas/Linkbases unter `$Job\originals`. Er schreibt je Dokument Originalbytes und `source.json` mit URL, Identität, Retrieval-Zeit, Größe und SHA-256. Vorhandene fertig publizierte Dokumente mit passender Bindung werden erneut verifiziert und wiederverwendet. Nach Abbruch denselben Befehl starten. `.incomplete-*` sind isolierte Teiltransfers. Bei 429 bzw. unklarer Transportlage begrenzt die persistente Task-9-Sperre Wiederholungen über Prozessstarts hinweg. Eine uninterpretierbare Retry-After-Angabe bleibt gesperrt. Die Sperre nur nach separat geklärter Providerfrist und Prüfung auf laufende Prozesse behandeln. Dieser Befehl greift weder auf Companyfacts noch auf Marktdatenendpunkte zu.

## 4 · Originalbytes verifizieren

```powershell
Invoke-Task9 (@('verify') + $Common)
$Verified = Invoke-Task9 (@('verify') + $Common + @('--execute'))
$SourceSha = $Verified.manifest_sha256
$SourceScope = $Common + @('--source-sha256',$SourceSha)
```

`verify` ohne Ausführung zeigt die betrachteten Cache-Pfade. Bei vollständigen vorhandenen Originalen kann `verify --execute` direkt genutzt werden. Der historische Cache bleibt lesend. Der Verifikationslauf hasht nur die ausgewählten Index-/Dokumentdateien und schreibt exklusiv `verified-originals.json`. Fehlende oder inkonsistente Dateien blockieren. Ein erfolgreiches Byte-Resultat bindet Quelle, Retrieval und Filing-Identität technisch. Der Reporting-Entity-Review folgt separat.

## 5 · DTS und Arelle-Taxonomien

```powershell
$TaxPreview = Invoke-Task9 (@('taxonomy') + $SourceScope) @(0,3)
$TaxPreview.filings
```

Bei `TAXONOMY_PENDING` den rekursiven Abhängigkeitsplan prüfen. Dedizierte Taxonomie-Autorisierung nur für diese Quelle erteilen. Wie beim Download aus `$TaxPreview.authorization_template` eine unabhängige Review-Datei mit Operation `taxonomy` erzeugen. Der vorhandene Arelle-2.47.5- und SEC-Transformationsstand wird geprüft. Unterstützte Taxonomien und erforderliche Paketdownloads werden ausschließlich bei ausdrücklicher manueller Freigabe ergänzt.

```powershell
# Nur bei separat autorisierter Ergänzung ausführen.
$TaxApproval = Join-Path $Artifacts 'nke-01-taxonomy-approved.json'
$TaxApprovalSha = Read-Host 'SHA-256 aus dem Taxonomie-Review'
Invoke-Task9 (@('taxonomy') + $SourceScope + @(
    '--execute','--authorize-network','--approval',$TaxApproval,
    '--approval-sha256',$TaxApprovalSha
)) @(0,3)

# Offline erneut prüfen und Closure-Report publizieren.
$Tax = Invoke-Task9 (@('taxonomy') + $SourceScope + @('--execute')) @(0,3)
if ($Tax.status -ne 'TAXONOMY_CLOSURE_READY') { throw 'DTS not ready' }
$TaxScope = $SourceScope + @('--taxonomy-report',$Tax.path,'--taxonomy-sha256',$Tax.report_sha256)
```

Der Netzschritt ergänzt ausschließlich benötigte, unterstützte Taxonomien im Arelle-Cache, Package-Staging und Provenienzdateien nach den bestehenden Verträgen. Bei nicht unterstützten Hosts, fehlenden Quellen oder nicht verifizierter SEC-Transformation bleibt die Phase gesperrt. Die Runtime bleibt bei 2.47.5. Offline wird ein neuer `taxonomy-<hash>.json` im Job publiziert. Das Ausführen mit Netzwerk ersetzt weder die Offline-Nachprüfung noch die tatsächlich durch Arelle geladene Closure.

## 6 · Offline-Arelle ausführen

```powershell
Invoke-Task9 (@('extract') + $TaxScope)
$Extraction = Invoke-Task9 (@('extract') + $TaxScope + @('--execute'))
$ReviewScope = $SourceScope + @('--extraction-sha256',$Extraction.manifest_sha256)
```

Der Worker bleibt offline. Er schreibt temporäre Filing-Kopien für Arelle und publiziert pro Filing einen Shard mit Dokumentmodus, Original-SHA, QName, Kontext, Dimensionen, Unit, Decimals, Wert, Alternativen, DTS-Dokumenten und Findings. `extraction-manifest.json` bindet die Shards und erscheint nach vollständig erfolgreicher Verarbeitung. Bei einem fehlenden oder nicht hashgebundenen DTS werden Findings in `<accession>-quarantine-*.json` abgelegt. Aus der Quarantäne entdeckte Taxonomie-URLs fließen in einen erneuten Taxonomie-Preflight ein. Erstelle bei geänderter Evidenz einen neuen Taxonomie-Report und führe die betroffene Extraktion erneut aus. Gleich gebundene erfolgreich publizierte Shards sind Resume-Punkte.

### Task 9.1 · NKE nach `UNBOUND_ACTUAL_DTS` fortsetzen

Der Preflight verwendet auch die tatsächlich geladenen DTS-URIs des ausgewählten Dokuments aus einer hashbenannten Quarantäne desselben Filings, derselben Originale und derselben Arelle-Version. Die beobachteten Pfade sind reine Diagnose. Nur lokal nach dem bestehenden Cache- oder Wheel-Vertrag verifizierte Bytes können die Closure ergänzen. Beim NKE-Erstfall ergänzte der lesende Abgleich `http://www.xbrl.org/2006/xbrldi-2006.xsd` mit SHA-256 `03bbb0a8df76c9b6c309991c7a898ff67ae5356d12c74ca824f5c2ddc93f1045`. Die 22 früher gebundenen Hashes stimmten überein. Die separate XBRL-Instanz bleibt der Extraktionspfad.

Nach dem Fix **eine neue Selection**, aber **denselben Job** verwenden. Die alte Selection bleibt als historischer Beleg. Die erneute Verifikation schreibt bei geänderter Selection ein zusätzliches `verified-originals-<selection-sha256>.json`. Alte Quarantäne, Closure und Verifikationsberichte bleiben verfügbar. Mit den PowerShell-Helfern aus Schritt 2:

```powershell
$Job = Join-Path $Artifacts 'f01-task9-nke-01'
$Selection = Join-Path $Artifacts 'nke-01-selection-task91.json'
$Plan = Invoke-Task9 @('select','--accession','0000320187-16-000242')
Save-NewTask9Json $Plan $Selection
$Common = @('--job',$Job,'--selection',$Selection,'--selection-sha256',(Get-Task9Sha $Selection))
$Verified = Invoke-Task9 (@('verify') + $Common + @('--execute'))
$SourceScope = $Common + @('--source-sha256',$Verified.manifest_sha256)
$Tax = Invoke-Task9 (@('taxonomy') + $SourceScope + @('--execute'))
$TaxScope = $SourceScope + @('--taxonomy-report',$Tax.path,'--taxonomy-sha256',$Tax.report_sha256)
Invoke-Task9 (@('extract') + $TaxScope)
$Extraction = Invoke-Task9 (@('extract') + $TaxScope + @('--execute'))
```

Bei `TAXONOMY_PENDING` oder einem Hashfehler anhalten. Dieser Wiederanlauf setzt keine Netzwerkfreigabe und führt keine Source-/Accounting-Admission aus.

## 7 · Source Admission

```powershell
$SourceDraft = Join-Path $Artifacts 'nke-01-source-draft.json'
Save-NewTask9Json (Invoke-Task9 (@('source-template') + $ReviewScope)) $SourceDraft
$SourceReview = Join-Path $Artifacts 'nke-01-source-reviewed.json'
$SourceReviewSha = Read-Host 'SHA-256 aus dem Source-Review'
$SourceAdmitted = $ReviewScope + @('--source-review',$SourceReview,'--source-review-sha256',$SourceReviewSha)
```

Der Reviewer prüft Original-Provenienz, CIK, Accession, Reporting Entity, Vollständigkeit, Date/Acceptance, Filing-Type und Hashes. Der Erstfall braucht `ORIGINAL_FILING` und eine ausdrücklich belegte `us-gaap`-Membership. Mit neuem Pfad und separatem Datei-SHA-256 freigeben. [Source-Admission-Vertrag](f01_historical_50_source_admission.md)

## 8 · Accounting Admission

```powershell
$AccountingDraft = Join-Path $Artifacts 'nke-01-accounting-draft.json'
Save-NewTask9Json (Invoke-Task9 (@('accounting-template') + $SourceAdmitted)) $AccountingDraft
$AccountingReview = Join-Path $Artifacts 'nke-01-accounting-reviewed.json'
$AccountingReviewSha = Read-Host 'SHA-256 aus dem Accounting-Review'
$Admitted = $SourceAdmitted + @(
    '--accounting-review',$AccountingReview,'--accounting-review-sha256',$AccountingReviewSha
)
```

Ein fachlicher Reviewer bewertet jede Observation anhand ihrer ursprünglichen Berichtsstelle. Der erste Adapter unterstützt ausschließlich die gebundenen USD-Instant-Facts `total_assets` und `cash`, ohne Dimensionskonflikt, mit explizitem `CONSOLIDATED` / `AS_FILED`. Der erwartete Dezimalwert ist als Text zu prüfen. Andere Metriken und Fallbacks bleiben blockiert. [Accounting-Admission-Vertrag](f01_historical_50_accounting_admission.md)

## 9 · Isolierten Bootstrap autorisieren

### Task 9.2 · Automatische Admission als Offline-Preflight

Die neue Phase `auto-admit` prüft Selection, Source-Manifest, Originalbytes, lokale DTS-Closure und Extraction-Shards und gibt einen Batchbericht aus. `F01_AUTOMATED_ADMISSION_V1` trägt `decision_authority=AUTOMATED`, Ruleset-ID und -Version, Ruleset-SHA, Code- und Artefaktbindungen, Befunde und einen Evidence-Digest. Die unveränderten manuellen Reviews bleiben ein eigener Freigabepfad.

Für den bereits importierten NKE-Referenzfall mit den PowerShell-Helfern aus Schritt 2:

```powershell
$Job = Join-Path $Artifacts 'f01-task9-nke-01'
$Selection = Join-Path $Artifacts 'nke-01-selection-task91.json'
$AutoNke = Invoke-Task9 @(
    'auto-admit','--job',$Job,'--selection',$Selection,
    '--selection-sha256',(Get-Task9Sha $Selection),
    '--source-sha256','6d6fa7de33e163b997b81fbadee9b54de2f15da599eaf5da324cdadefaacf109',
    '--taxonomy-report',(Join-Path $Job 'taxonomy-e788dadc6d1a.json'),
    '--taxonomy-sha256','e788dadc6d1a781b9a171b604ef95432b79896fe2a788346bfc60fa82759e3c7',
    '--extraction-sha256','d13d83502f935b5ab6b7098f2f7d71629a73602eb6ca94d9259855cc3d58cea6'
)
$AutoNke.summary
```

Für NKE dient der ursprüngliche Candidate ausschließlich der historischen Prüfung. `current_selection=false` sperrt diesen Beleg für den Auto-Import. Die numerischen Werte und Observation-IDs können mit dem damaligen Review abgeglichen werden. Den existierenden NKE-Job und Receipt für einen Audit lesend verwenden. Die bereits initial importierte Accession ist zudem durch die Disjunktheitsprüfung vom automatischen Append ausgeschlossen.

Für neue Filings dieselbe Phase mit aktueller Selection, frischen gebundenen Shards und lokalen Originalen verwenden. `auto-admit --execute` publiziert ausschließlich einen neuen, nach vollständigem Datei-SHA benannten Maschinenbericht im Job. Gleich gebundene Wiederholung verwendet denselben Bericht. Fehlende oder widersprüchliche Quellen blockieren; nicht qualifizierte Fact-Kandidaten und mehrdeutige Metriken bleiben mit Befund sichtbar. V1 berücksichtigt nur positive, finite USD-Instant-Werte für Assets und Cash am Report-Date, mit dokumentierter konsolidierter Bilanzrolle und ohne Fallback. Code- oder Ruleset-Wechsel erfordern einen neu berechneten Bericht. Für Aufnahme unter einem neuen Code-Fingerprint braucht es außerdem eine neue Selection und entsprechend gebundene Extraktion.

Für einen später separat autorisierten neuen Teilbatch nehmen `import --append` und `reader --append` die Argumente `--auto-admission <Pfad>` und `--auto-admission-sha256 <SHA>` anstelle der menschlichen Review-Argumente. Die Source-, Extraction-, Taxonomy- und Bootstrap-Bindungen bleiben erforderlich. Der Import berechnet die Maschinenentscheidung erneut, vergleicht den gesamten Bericht und prüft initiale Receipts und exaktes Research-Ziel. Der Schreibmodus verlangt weiterhin die separate Importfreigabe mit Hash. Duplicate Assurance und E.13-Grenzprüfung folgen wie bei der manuellen Aufnahme. Der bestehende einmalige NKE-Import wird für den Rollout nicht wiederholt.

### Manueller Bootstrap-Befehl

```powershell
$Bootstrap = Join-Path $Artifacts 'nke-01-bootstrap-reviewed.json'
Save-NewTask9Json (Invoke-Task9 (@('bootstrap-manifest') + $Admitted)) $Bootstrap
$BootstrapSha = Get-Task9Sha $Bootstrap
$BootArgs = @('--target',$Target,'--manifest',$Bootstrap,'--manifest-sha256',$BootstrapSha,
              '--expect-code-fingerprint',$Plan.candidate.fingerprint)
& $Py -B scripts/bootstrap_f01_research_db.py @BootArgs
if ($LASTEXITCODE -ne 0) { throw "Bootstrap preflight exit $LASTEXITCODE" }

# Erst nach separater Freigabe zur Anlage des neuen Research-Ziels.
& $Py -B scripts/bootstrap_f01_research_db.py @BootArgs --execute --confirm-create-research-only
if ($LASTEXITCODE -ne 0) { throw "Bootstrap exit $LASTEXITCODE" }
```

Das vorhandene Bootstrap publiziert eine leere Canonical-A‑V1-Datenbank, seine Manifestkopie und den Ergebnisbericht mit neuer Dataset-ID. Ein vorhandenes Ziel oder Staging blockiert. Nach Abbruch den Stagingzustand prüfen und anhand des Bootstrap-Berichts entscheiden. Das bestehende Ziel beim Resume eines Imports erneut verwenden. Für spätere disjunkte Slices desselben D.11-Batches dient das ursprüngliche Bootstrap-Manifest mit `--append` bei Import und Reader. Die ursprünglichen Receipts müssen vollständig sein.

## 10 · Canonical-Import und Resume

```powershell
$ImportScope = $Admitted + @('--bootstrap-manifest',$Bootstrap,'--bootstrap-sha256',$BootstrapSha)
$ImportPreflight = Invoke-Task9 (@('import') + $ImportScope)
$ImportDraft = Join-Path $Artifacts 'nke-01-import-draft.json'
Save-NewTask9Json $ImportPreflight.authorization_template $ImportDraft

$ImportApproval = Join-Path $Artifacts 'nke-01-import-approved.json'
$ImportApprovalSha = Read-Host 'SHA-256 aus der separaten Importfreigabe'
$ImportCommand = @('import') + $ImportScope + @(
    '--execute','--approval',$ImportApproval,'--approval-sha256',$ImportApprovalSha
)
$Imported = Invoke-Task9 $ImportCommand
Save-NewTask9Json $Imported (Join-Path $Artifacts 'nke-01-import-result.json')
```

Der Read-only-Preflight öffnet ausschließlich die isolierte, exakt gebundene Datenbank. Die Ausführung prüft Zugang, Manifest- und Review-Hashes sowie die Filing-Bytes erneut. Pro Filing gibt es eine atomare Transaktion, einen Receipt und eine Dataset-Version. Bei Konflikten bleiben Fehlerdetails im Job oder auf stderr. Beim Resume dieselbe freigegebene Importanweisung wiederholen. Bereits bestätigte Receipts behalten Version und ID. Eine veränderte Zulassung oder Payload unter derselben ID wird abgewiesen. Ein vorhandenes Rollback-Journal sperrt Read-only-Prüfungen. Den identisch autorisierten Import erneut starten. SQLite kann nach Prüfung des exakten Bootstrap-Manifests das Journal auf dem isolierten Ziel nativ zurückrollen. WAL und SHM bleiben gesperrt.

## 11 · Duplicate Assurance und identischen Replay prüfen

```powershell
function Read-Task9Assurance {
    $output = & $Py -B scripts/validate_f01_canonical_assurance.py --target $Target `
        --expect-bootstrap-manifest-sha256 $BootstrapSha --authorize-isolated-read
    if ($LASTEXITCODE -ne 0) { throw "Assurance exit $LASTEXITCODE" }
    ($output -join "`n") | ConvertFrom-Json
}

$Before = Read-Task9Assurance
Save-NewTask9Json $Before (Join-Path $Artifacts 'nke-01-assurance-before-replay.json')
# Nach Freigabe für die identische Wiederholung mit demselben Manifest.
$Replay = Invoke-Task9 $ImportCommand
Save-NewTask9Json $Replay (Join-Path $Artifacts 'nke-01-import-replay.json')
$After = Read-Task9Assurance
Save-NewTask9Json $After (Join-Path $Artifacts 'nke-01-assurance-after-replay.json')
if ((ConvertTo-Json $Before.counts -Compress) -ne (ConvertTo-Json $After.counts -Compress)) {
    throw 'Unexpected count drift'
}
for ($i = 0; $i -lt $Imported.receipts.Count; $i++) {
    if (!$Replay.receipts[$i].replayed -or
        $Replay.receipts[$i].request_id -ne $Imported.receipts[$i].request_id -or
        $Replay.receipts[$i].dataset_version -ne $Imported.receipts[$i].dataset_version) {
        throw 'Unexpected receipt or version drift'
    }
}
```

Der Report prüft Fact-/Occurrence-/Receipt-Counts, Revisionen, Duplicate Keys, Schema, Foreign Keys und Storage-Bindungen. Source, Accounting und Amendment-Findings werden zusätzlich über die Jobbelege und Quarantäne geprüft.

## 12 · E.13 Acceptance-Grenzen prüfen

```powershell
Invoke-Task9 (@('reader') + $ImportScope)
$Boundary = Invoke-Task9 (@('reader') + $ImportScope + @('--execute'))
Save-NewTask9Json $Boundary (Join-Path $Artifacts 'nke-01-reader-boundaries.json')
```

Der Reader lädt ausschließlich die zum Filing-Receipt gebundene Dataset-Version. Für die unabhängig geprüften Original-Facts muss eine Abfrage direkt vor der Acceptance leer bleiben. An der Acceptance muss sie den belegten Wert liefern. Dieser Nachweis gilt für genau diese Filing-Witnesses und die beiden unterstützten Bilanzmetriken. Die historische E.13-Paritätsfreigabe für sieben Unternehmen bleibt ein eigener Scope. Historische Security-Epochen, Marktfeed, Adjustments und Exits sind eigenständige Trading-Gates.

## Exitcodes, Dateien und Speicher

Task-9-CLI meldet `0` für die erfolgreich angefragte Phase oder einen Preflight, `2` bei CLI-Syntax, `3` bei sperrenden Bindungen oder fehlendem DTS, `4` bei Laufzeit-/I/O-Fehlern und `130` bei Abbruch. Der vorhandene Bootstrap verwendet `3` für blockierte und `4` für fehlgeschlagene Operationen. Duplicate Assurance liefert `1` bei Findings und `2` bei Blockierung. Nach einem Fehler die genaue Phase und vorhandene Receipts/Shards prüfen. Die nächste Phase erst nach ihrem eigenen Preflight starten.

Originale, Extraktions-Shards, Request-Bindungen, SQLite/Sidecars, Taxonomien, temporäre Dateien, Reserve und Backup benötigen getrennte Budgets. Der Download-Preflight liefert eine konservative Größenobergrenze. Für Backup auf F: zuerst physische Trennung und verfügbaren Speicher prüfen. Backup und Restore benötigen einen separaten, manuell genehmigten Ablauf. Für einen SQLite-Kopierbeleg müssen alle Writer geschlossen sein und Sidecars fehlen. Eine Dateisperre, die fremde Writes und Deletes verweigert, muss bis zum Ende des Kopier- und Hashabgleichs gehalten werden. Restore an einem getrennten, neuen Research-Pfad testen und für den betroffenen Stand Duplicate Assurance und Manifestbindung ausführen. [Architektur und Kapazität](f01_historical_50_ingestion_architecture.md)

## Optional · Separat autorisiertes Datenbank-Backup

Dateien und Metadaten dieses Ablaufs gehören zu einem eigenen Backupauftrag. Für die Read-only-Vorprüfung die folgenden Befehle ausführen. Den Zielpfad separat wählen und den File-/Dataset-Status sichern.

```powershell
$Partitions = Get-Partition -DriveLetter G,F
if (($Partitions | Select-Object -ExpandProperty DiskNumber -Unique).Count -ne 2) {
    throw 'Physical backup separation not proved'
}
$Partitions | Get-Disk | Select-Object Number,FriendlyName,UniqueId
Get-Volume -DriveLetter F | Select-Object FileSystemLabel,SizeRemaining
$Proof = Read-Task9Assurance
$DatabaseBytes = (Get-Item -LiteralPath $Target).Length
$BackupFolder = 'F:\TraWalpResearch-Backups\f01-task9-nke-01'
if (Test-Path -LiteralPath $BackupFolder) { throw 'Choose a new backup directory' }
```

Erst nach eigener Freigabe `$BackupFolder` als neuen lokalen Ordner anlegen. Das Backup umfasst die SQLite-Datei und die beiden Bootstrap-Artefakte. Die zugehörigen Originale, Reviews und Shards benötigen ihre eigene Archivbindung. Stelle vor dem Start ausreichend freien Speicher für Kopie, Nachprüfung und Restore-Probe sicher.

```powershell
$parentDir = [IO.Path]::GetDirectoryName($BackupFolder)
$part = Get-Item -LiteralPath $parentDir
while ($part) {
    if ($part.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Redirected backup path' }
    $part = $part.Parent
}
if ([System.IO.DriveInfo]::new('F').AvailableFreeSpace -lt $DatabaseBytes) {
    throw 'Insufficient backup capacity'
}
New-Item -Path $BackupFolder -ItemType Directory -ErrorAction Stop | Out-Null
$In = [IO.File]::Open($Target, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
try {
    foreach ($suffix in @('-journal','-wal','-shm')) {
        if (Test-Path -LiteralPath ($Target + $suffix)) { throw 'Database is not quiescent' }
    }
    $BackupDatabase = Join-Path $BackupFolder 'f01-sec-candidate.sqlite3'
    $Out = [IO.File]::Open($BackupDatabase, [IO.FileMode]::CreateNew)
    try { $In.CopyTo($Out); $Out.Flush($true) } finally { $Out.Dispose() }
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $In.Position = 0
        $Expected = [BitConverter]::ToString($hash.ComputeHash($In)).Replace('-','').ToLowerInvariant()
    } finally { $hash.Dispose() }
    if ((Get-Task9Sha $BackupDatabase) -ne $Expected) { throw 'Backup hash mismatch' }
    foreach ($name in @('bootstrap_manifest.json','bootstrap_result.json')) {
        $original = Join-Path (Split-Path -LiteralPath $Target) $name
        [IO.File]::Copy($original, (Join-Path $BackupFolder $name), $false)
    }
    Save-NewTask9Json @{ sha256 = $Expected; dataset_id = $Proof.dataset_id; bootstrap_sha256 = $BootstrapSha } `
        (Join-Path $BackupFolder 'backup-verified.json')
} finally { $In.Dispose() }
```

Bei einem Abbruch fehlt der Abschlussmarker. Den unvollständigen Zielordner behalten und einen neuen ausdrücklich freigegebenen Versuch wählen. Für die Restore-Probe in einen neuen Ordner mit Namenspräfix `f01-sec-isolated-research-` kopieren, SHA-256 mit `backup-verified.json` vergleichen und Duplicate Assurance mit dem ursprünglichen `$BootstrapSha` auf dieser Kopie ausführen. Erst die dokumentierte Restore-Probe belegt die Wiederherstellbarkeit.
