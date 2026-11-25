# F01 · Task 9.3 · Zweites NKE-Filing vorbereiten

NKE `0000320187-16-000264` (CIK `0000320187`) ist ein Original-10-Q vom 05.04.2016 für den 29.02.2016. Die gebundenen Submissions belegen Primärdokument `nke-2292016x10q.htm` und Acceptance `2016-04-05T20:14:43Z`. Das Folgequartal desselben Reporting Issuers hat eine eigene Accession. Der frühere NKE-Receipt bleibt bestehen. BBBY und BG bringen Reporting-Entity-Konflikte mit, BG enthält außerdem ein Amendment. HEI hat einen Share-Class-Scope und ALOT eine offene historische Universe-Zuordnung. [D.11-Entwurf](</G:/TraWalpResearch/import-artifacts/f01-historical-50-preparation-v1/first_batch_manifest.json>)

Im Standard-Filing-Cache und den vorhandenen Task-9-Jobs fand sich für die 20 D.11-Accessions ausschließlich das Originalverzeichnis des Referenzfilings. Index, Primär- und XBRL-Dokumente sowie Filing-Linkbases für den neuen Fall fehlen in diesen Pfaden. Der D.6-gebundene lokale Companyfacts-Member enthält je einen passenden USD-Hinweis auf **20.987.000.000 Assets** und **3.044.000.000 Cash**. Ob die Originale die Werte unter den V1-Regeln eindeutig und mit konsolidierter Bilanzrolle belegen, bleibt offen. Der SEC-Plugin und 23 externe Abhängigkeiten des Referenzfilings wurden lokal neu geprüft. Die DTS-Closure des neuen Filings ist erst anhand seiner eigenen Originale feststellbar. [Offline-Evidenz](</G:/TraWalpResearch/import-artifacts/f01-task9-ingestion-preparation-v1/f01-task9-nke-02/append-preflight-9a7a6a97f8c982651920b2be78faacf0b2795c2a993fdf173fbbcaf352220833.json>)

## 1 · Read-only-Start

Verwende `$Py`, `$Cli`, `$Artifacts`, `Get-Task9Sha`, `Invoke-Task9` und `Save-NewTask9Json` aus [Schritt 1 und den Hilfsfunktionen in Schritt 2](f01_historical_50_manual_ingestion_runbook.md#2--auswahl-und-ausgabepfade). Die Selection ist bereits veröffentlicht. Die NKE-Beispielpfade für den ersten Job werden durch die folgenden Werte ersetzt.

```powershell
$Job = Join-Path $Artifacts 'f01-task9-nke-02'
$Selection = Join-Path $Job 'selection.json'
$SelectionSha = '0ae3ffaa9b973dc4874f862545c2eaba0ab895acbf9b8ff5e3c62ff931aee0f6'
if ((Get-Task9Sha $Selection) -ne $SelectionSha) { throw 'Selection SHA mismatch' }
$Common = @('--job',$Job,'--selection',$Selection,'--selection-sha256',$SelectionSha)
$Download = Invoke-Task9 (@('download') + $Common)
Invoke-Task9 (@('verify') + $Common)
```

Der read-only Abruf-Preflight meldet `ACQUISITION_PENDING`. Bei Code-/Candidate-Wechsel den Preflight mit einer neuen, separat benannten Selection vorbereiten. Die publizierte Selection bleibt als Beleg erhalten.

## 2 · Erst nach ausdrücklicher Abruffreigabe

Aus `$Download.authorization_template` einen Entwurf erstellen. Der autorisierte Reviewer gibt die genehmigte Datei und ihren SHA-256 getrennt weiter. In der Session `TRAWALP_SEC_USER_AGENT` mit gültigem Kontakt setzen. Die folgenden Befehle einzeln ausführen und bei Fehlern stoppen.

```powershell
$DownloadApproval = Read-Host 'Pfad zur separat genehmigten Abruffreigabe'
$DownloadApprovalSha = Read-Host 'SHA-256 aus der Abruffreigabe'
Invoke-Task9 (@('download') + $Common + @(
    '--execute','--approval',$DownloadApproval,'--approval-sha256',$DownloadApprovalSha
))
$Verified = Invoke-Task9 (@('verify') + $Common + @('--execute'))
$SourceScope = $Common + @('--source-sha256',$Verified.manifest_sha256)
$Tax = Invoke-Task9 (@('taxonomy') + $SourceScope + @('--execute')) @(0,3)
if ($Tax.status -ne 'TAXONOMY_CLOSURE_READY') { throw 'DTS pending' }
$TaxScope = $SourceScope + @('--taxonomy-report',$Tax.path,'--taxonomy-sha256',$Tax.report_sha256)
Invoke-Task9 (@('extract') + $TaxScope)
$Extraction = Invoke-Task9 (@('extract') + $TaxScope + @('--execute'))
$Evidence = $TaxScope + @('--extraction-sha256',$Extraction.manifest_sha256)
$Auto = Invoke-Task9 (@('auto-admit') + $Evidence)
$Auto.summary
```

Bei fehlender DTS-Closure oder Quarantäne zunächst den Befund prüfen. Bei später entdeckten lokalen Abhängigkeiten den Offline-Taxonomieschritt wiederholen. Für Taxonomien mit fehlenden lokalen Bytes gilt eine eigene Freigabe. Unklare DEI-Evidenz, Amendments und eindeutige Provenienzfehler blockieren. Mehrdeutige gleichartige Facts und Alternativen außerhalb der V1-Regeln erscheinen als Ausschlüsse. `AUTO_ADMISSION_READY` kann einen zugelassenen Teilumfang ausweisen. Deshalb beide Metrikentscheidungen und alle Ausschlüsse prüfen.

## 3 · Ablegen und Append rein lesend vorprüfen

Die bestehende isolierte Lineage hat Dataset-Version 2. Der gebundene Bootstrap, der initiale Receipt, die Disjunktheit der neuen Accession, die CIK-Zuordnung und das Fehlen des neuen Receipts wurden read-only geprüft. Duplicate Assurance meldete zwei Facts, zwei Occurrences, einen Receipt und null Verletzungen. E.13 wurde hier nicht erneut ausgeführt.

```powershell
$Report = Invoke-Task9 (@('auto-admit') + $Evidence + @('--execute'))
$Bootstrap = Join-Path $Artifacts 'nke-01-bootstrap-task91.json'
$BootstrapSha = '45ca8010d15d777d6eb18cd1fb6e57fd1b6cdca82866f22e9b3138145de8fa9f'
$Append = $Evidence + @(
    '--append','--auto-admission',$Report.path,'--auto-admission-sha256',$Report.manifest_sha256,
    '--bootstrap-manifest',$Bootstrap,'--bootstrap-sha256',$BootstrapSha
)
$AppendPreflight = Invoke-Task9 (@('import') + $Append)
$AppendPreflight.authorization_template
```

`auto-admit --execute` schreibt nur den neuen Maschinenbericht. `import` bleibt in diesem Block rein lesend. Ein tatsächlicher Append braucht eine separat geprüfte und hashgebundene Importfreigabe. Nach einem später genehmigten Append Duplicate Assurance und den E.13-Reader gemäß [Haupt-Runbook](f01_historical_50_manual_ingestion_runbook.md) ausführen. Der frühere NKE-Import wird nicht wiederholt.
