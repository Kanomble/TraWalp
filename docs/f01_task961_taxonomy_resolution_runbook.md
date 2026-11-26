# Task 9.6.1 · Historische Taxonomien und Offline-Arelle

Der Cache liegt unter `G:\TraWalpResearch\taxonomy-cache\f01-historical-taxonomies-v1`.
Das Offline-Inventar, die DTS-Berichte und die Freigabeentwürfe liegen unter
`G:\TraWalpResearch\import-artifacts\f01-task961-taxonomy-resolution-v1`.
Der erste Lauf veröffentlicht die Dateien direkt dort. Bei geänderten Ergebnissen gibt
der Runner den Pfad eines neuen Snapshot-Verzeichnisses aus.

## COST und ORCL offline prüfen

```powershell
Set-Location 'G:\GitHub\TraWalp'
$python = '.\.venv\Scripts\python.exe'
$runner = 'scripts/run_f01_historical_acquisition.py'
$report = & $python -B $runner taxonomy-report | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Offline-Taxonomieprüfung fehlgeschlagen' }
$report.directory
$report.filings
```

Der Befehl prüft die vorhandenen Originale gegen das SEC-Receipt. Er braucht Lesezugriff
auf beide Originalverzeichnisse und Schreibzugriff auf die neuen Research-Ziele.
`ORIGINAL_RECHECK_BLOCKED` nennt die genaue Stelle. Bei vollständiger lokaler Closure
verwendet der Runner den Task-9.6-Prozessor und Arelle 2.47.5. Der Worker bekommt eine
temporäre WebCache-Ansicht der verifizierten URIs. Socket-Aufrufe sind gesperrt. Der
Extraktionsvertrag prüft die tatsächlich geladenen DTS-Dateien gegen die gebundenen Hashes.
Extraktion und fachliche Zulassung werden als eigene Ergebnisse ausgewiesen.

## Fehlende Taxonomiequelle freigeben

Die fünf exakten Schema-URIs stammen aus den archivierten DTS-Berichten. FASB
US-GAAP/US-Types und SEC COUNTRY/DEI/INVEST haben eigene Beschaffungsblöcke. Prüfe vor
der Genehmigung die ZIP-Kandidaten und die historischen Veröffentlichungsnachweise im
Plan. Die ZIP-URLs und tatsächlichen Response-Größen wurden offline nicht verifiziert.
Die Bytegrenze im Scope ist eine Obergrenze. Ein direkter HTTPS-Abruf für jede exakte
Schema-URI ist als eigene Freigabeoption enthalten. Weitere Abhängigkeiten erscheinen nach
dem nächsten Offline-Lauf im Gap-Report.

```powershell
# Beispiel für genau einen separat geprüften Block.
$scope = Join-Path $report.directory 'acquire-fasb-2012-zip.json'
$approvalPath = Join-Path $env:LOCALAPPDATA 'TraWalp-taxonomy-fasb-2012-approved.json'
# Einmalig einen Entwurf erzeugen, ohne eine bereits bearbeitete Kopie zu überschreiben.
if (Test-Path -LiteralPath $approvalPath) { throw 'Freigabedatei existiert bereits' }
(Get-Content -Raw -LiteralPath $scope | ConvertFrom-Json).authorization_draft |
    ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $approvalPath -Encoding ascii
```

Die genehmigende Person trägt `APPROVED`, ihren Namen, einen ISO-8601-Zeitpunkt mit
Zeitzone und konkrete Nachweise für Quellenbedingungen, Aufbewahrung und Restore ein.
Der Scope-Hash bindet URL, Version, Zielschemas und Limits. Pro Scope gelten höchstens drei
Requests und 30 Requests pro Minute. Redirects werden abgelehnt. Das Filing-Receipt
erteilt keine Taxonomie-Freigabe.

```powershell
# Erst nach ausdrücklicher separater Taxonomie-Freigabe ausführen.
$scopeSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $scope).Hash.ToLowerInvariant()
$approvalSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $approvalPath).Hash.ToLowerInvariant()
& $python -B $runner taxonomy-acquire --preflight $scope --preflight-sha256 $scopeSha --authorization $approvalPath --authorization-sha256 $approvalSha --user-agent $env:SEC_USER_AGENT
```

Zur Wiederaufnahme denselben Befehl mit denselben Dateien und Hashes verwenden. Der
Checkpoint reserviert Requests und Bytes vor dem Transport. Veröffentlichte Response-Receipts
werden revalidiert und wiederverwendet. Validierungsfehler hinterlegen einen Quarantänebeleg.
Für SEC einen eigenen Scope und eine eigene Freigabe verwenden.

Ein lokal vorhandenes geprüftes ZIP lässt sich offline registrieren. Das Review-Manifest
enthält `source_url`, `provenance`, `bytes`, `sha256` und `uris`. Unter `uris` steht je
exakter URI ein Objekt mit `member` (vollständiger ZIP-Pfad), `sha256` und `namespace`
(`null` für Linkbases ohne Zielnamespace). Der Quellnachweis muss Herausgeber und Ausgabe
belegen. Native Manifeste und Katalogregeln werden geprüft. Herkömmliche ZIPs verwenden
die explizite URI-Zuordnung und bleiben als `LEGACY_ZIP` ausgewiesen.

```powershell
$reviewSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $review).Hash.ToLowerInvariant()
& $python -B $runner taxonomy-register --taxonomy-archive $archive --preflight $review --preflight-sha256 $reviewSha
& $python -B $runner taxonomy-report
```

Für spätere SEC-Blöcke liefert `process --block-id <ID>` DTS- und Beschaffungsbedarf aus
den verifizierten Originalen. Bereits gebundene Dateien werden pro URI wiederverwendet.
Canonical-Import und PIT-Trading bleiben gesperrt.
