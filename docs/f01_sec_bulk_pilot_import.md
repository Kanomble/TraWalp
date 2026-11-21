# F01 – Canonical SEC Bulk Pilot Import

Der Importer verarbeitet ausschließlich AAPL, ACN, CAT, COST, LLY, MSFT und NVDA nach [Pilot-Spec V2](f01_sec_bulk_pilot_spec_v2.json). BAC ist als `BLOCKED_UNRESOLVED_REPORTING_ENTITY` und JSM als `BLOCKED_IDENTITY_CONFLICT` separat ausgewiesen. Ihre ZIP-Payloads werden für den Import nicht ausgewählt. [BAC-Resolution-Audit](../reports/f01_bac_reporting_entity_resolution_audit.md)

Die Quelle ist die veröffentlichte Generation `pilot01-20261121-175840`, Manifest-SHA-256 `d0392ebbba463b4da8485e786c9986d163e02a57532197fb84c33491b27dbc20`. Der vorhandene Offline-Verifier prüft `SEC_BULK_ARCHIVES_VERIFIED` einschließlich ZIP-SHA-256 und Census-/Checkpoint-Bindungen weiter gegen die unveränderte [Quellen-Spec V1](f01_sec_bulk_pilot_spec.json). Import und Admission binden separat Spec V2. Benötigte ZIP-Members der aktiven CIKs werden einzeln gelesen und auf CRC geprüft.

## Voraussetzungen

- Ein leeres, separat autorisiertes `RESEARCH_ONLY`-Bootstrap-Ziel unter `G:\TraWalpResearch\research-databases` mit eigenem Dataset-Ursprung. Das Skript prüft Schema, Version 1, internen und externen Bootstrap-Nachweis, DB-SHA-256 für eine frische DB und anschließend Dateiidentität und Receipt-Präfix. Live-/Legacy-Ziele und historische Migrationsbestände sind ausgeschlossen.
- Eine unabhängig geprüfte V2-Admission für jeden der sieben aktiven CIKs. Die 21 vorhandenen Evidence-Bindungen sind im V2-Entwurf übernommen. Die Admission entscheidet separat über die CIK-/Symbol-Beziehung für Research und pro Taxonomie über die Source-Membership. Für einen zugelassenen Fact verlangt A‑V1 die explizite Genehmigung des Canonical-Ankers am isolierten Ziel. Bei einer offenen Entscheidung eines aktiven CIKs stoppt der Importer.
- Das vorhandene Layout und hinreichend Platz für DB, Sidecars, Arbeits-/Restore-/Backup-Speicher, alle Request-Envelopes, das größte zusätzliche temporäre Envelope und die Sicherheitsreserve. Der Preflight berechnet den Bedarf pro Volume. ZIP-Zentralverzeichnisse und geplante Envelopes können zusammen einen erheblichen RAM-Bedarf verursachen.

## PowerShell

Die Befehle sind für einen manuellen Lauf aus dem geprüften Candidate-Worktree vorgesehen.

```powershell
Set-Location 'G:\GitHub\TraWalp'
if ((git branch --show-current) -ne 'SEC-integration') { throw 'Falscher Branch' }
$f01Python = '.\.venv\Scripts\python.exe'
$f01Script = '.\scripts\import_f01_sec_bulk_pilot.py'
$f01Root = 'G:\TraWalpResearch'
$f01Layout = Join-Path $f01Root 'import-artifacts\f01-sec-bulk-layout-v1.json'
$f01Target = Join-Path $f01Root 'research-databases\f01-sec-isolated-research-pilot01\f01-sec-candidate.sqlite3'
$f01Code = 'c34dd3e28a33b132e171ec39300721002b81de3e44cb7ea88b90164935cef0dc'

function Save-F01PilotJsonNew([string]$Path, [string]$Text) {
    $f01Out = [IO.File]::Open($Path, [IO.FileMode]::CreateNew)
    try {
        $f01Content = [Text.Encoding]::UTF8.GetBytes($Text)
        $f01Out.Write($f01Content, 0, $f01Content.Length)
    } finally {
        $f01Out.Dispose()
    }
}
```

**Vorhandenen Bootstrap verwenden.** Für `f01-sec-isolated-research-pilot01` ist kein erneuter Bootstrap erforderlich. Der bestehende Code-Fingerprint im Bootstrap-Bericht dokumentiert die damalige Erstellung. Der neue Candidate-Fingerprint bindet den Import separat. Der manuelle Preflight prüft weiterhin die unveränderte Bootstrap-Datei und ihre Lineage. Den folgenden optionalen Block nur verwenden, wenn das vorhandene Ziel seine Bootstrap-/Leerheitsprüfung nicht erfüllt und ein neues, getrenntes Ziel benötigt wird. Das bisherige Ziel bleibt erhalten.

```powershell
$f01Target = Join-Path $f01Root 'research-databases\f01-sec-isolated-research-pilot02-v2\f01-sec-candidate.sqlite3'
$f01Seed = Join-Path $f01Root 'import-artifacts\f01-bootstrap-sec-bulk-pilot02-v2.json'
Save-F01PilotJsonNew $f01Seed (([ordered]@{
    contract = 'F01_CLEAN_CANONICAL_RESEARCH_BOOTSTRAP_V1'
    classification = 'RESEARCH_ONLY'
    research_scope = 'F01 SEC-Bulk-Pilot V2 mit sieben Research-Emittenten'
} | ConvertTo-Json))
$f01SeedSha = (Get-FileHash -LiteralPath $f01Seed -Algorithm SHA256).Hash.ToLowerInvariant()
$f01SeedArgs = @('--target', $f01Target, '--manifest', $f01Seed,
    '--manifest-sha256', $f01SeedSha, '--expect-code-fingerprint', $f01Code)
& $f01Python -B .\scripts\bootstrap_f01_research_db.py @f01SeedArgs
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap-Preflight blockiert' }
& $f01Python -B .\scripts\bootstrap_f01_research_db.py @f01SeedArgs --execute --confirm-create-research-only
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap blockiert' }
```

**Finale Admission-Review.** Zuerst die 21 Belege und die dokumentierten Ausschlüsse unabhängig prüfen. Der folgende Block verifiziert den unveränderten V2-Entwurf und die Evidence-Hashes. Er verlangt Reviewer-Namen und ausdrückliche Freigabe vor Veröffentlichung. Die V1-Dateien und die BAC-Belege werden dabei nicht umgeschrieben.

```powershell
$f01Artifacts = Join-Path $f01Root 'import-artifacts'
$f01DraftPath = Join-Path $f01Artifacts 'f01-pilot-admission-draft-v2.json'
$f01Admission = Join-Path $f01Artifacts 'f01-pilot-admission-reviewed-v2.json'
$f01DraftSha = '68e80a54421cdff28ab84fdf63d5d8fcc193467c957e1e550f8b2dde2e481ad4'
if (Test-Path -LiteralPath $f01Admission) { throw 'Review-Ziel existiert bereits' }
if ((Get-FileHash -LiteralPath $f01DraftPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $f01DraftSha) {
    throw 'V2-Entwurf hat einen anderen SHA-256'
}
$f01Draft = Get-Content -LiteralPath $f01DraftPath -Encoding UTF8 -Raw | ConvertFrom-Json
if ($f01Draft.contract -ne 'F01_SEC_BULK_PILOT_ADMISSION_V2' -or $f01Draft.reviewer) {
    throw 'Ungültiger V2-Entwurf'
}
foreach ($f01Entry in $f01Draft.issuers.PSObject.Properties) {
    $f01Issuer = $f01Entry.Value
    if ($f01Issuer.identity_decision -ne 'ADMIT_RESEARCH') { throw "Identity offen: $($f01Issuer.symbol)" }
    $f01Refs = @($f01Issuer.identity_evidence)
    foreach ($f01Rule in $f01Issuer.memberships.PSObject.Properties) {
        if ($f01Rule.Value.decision -ne 'ADMIT_RESEARCH' -or $f01Rule.Value.canonical_anchor -ne $true) {
            throw "Membership offen: $($f01Issuer.symbol)/$($f01Rule.Name)"
        }
        $f01Refs += $f01Rule.Value.evidence
    }
    foreach ($f01Ref in $f01Refs) {
        $f01RefPath = (Resolve-Path -LiteralPath $f01Ref.path).Path
        if (!$f01RefPath.StartsWith($f01Artifacts + '\', [StringComparison]::OrdinalIgnoreCase) -or
            (Get-FileHash -LiteralPath $f01RefPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $f01Ref.sha256) {
            throw "Belegbindung ungültig: $($f01Issuer.symbol)"
        }
    }
}
$f01Reviewer = Read-Host 'Unabhängiger Reviewer nach Abschluss der Prüfung'
if ([string]::IsNullOrWhiteSpace($f01Reviewer) -or
    (Read-Host 'Explizite Freigabe: ADMISSION_V2_FREIGEGEBEN eingeben') -cne 'ADMISSION_V2_FREIGEGEBEN') {
    throw 'Keine Review-Freigabe'
}
$f01Draft.reviewer = $f01Reviewer.Trim()
$f01Temp = Join-Path $f01Artifacts ('.f01-admission-v2-' + [guid]::NewGuid().ToString('N') + '.incomplete')
Save-F01PilotJsonNew $f01Temp ($f01Draft | ConvertTo-Json -Depth 20)
[IO.File]::Move($f01Temp, $f01Admission)
Get-FileHash -LiteralPath $f01Admission -Algorithm SHA256
```

Die Runtime prüft die Belege weiterhin nach `F01_SEC_BULK_RESEARCH_EVIDENCE_V1`. Bei einer fachlichen Änderung am vorbereiteten Entwurf ist eine erneute unabhängige Review mit passender Evidence und neu gebundenem Entwurfs-Hash erforderlich. BAC und JSM stehen in der gepinnten Ausschlussliste statt im aktiven Admission-Objekt. BACs Strategieausschluss bleibt bestehen.

**Read-only Preflight**

```powershell
$f01Admission = Join-Path $f01Root 'import-artifacts\f01-pilot-admission-reviewed-v2.json'
$f01AdmissionSha = (Get-FileHash -LiteralPath $f01Admission -Algorithm SHA256).Hash.ToLowerInvariant()
$f01PilotArgs = @(
    '--layout', $f01Layout, '--target', $f01Target
    '--admission', $f01Admission, '--admission-sha256', $f01AdmissionSha
    '--expect-code-fingerprint', $f01Code
)
& $f01Python -B $f01Script @f01PilotArgs
if ($LASTEXITCODE -ne 0) { throw 'Pilot-Preflight blockiert' }
```

Den Preflight-Output auf sieben aktive Emittenten, sieben erwartete Receipts, BAC-/JSM-Ausschlüsse, Admissions und Kapazität prüfen. Dann den ersten schreibenden Lauf getrennt autorisieren.

```powershell
& $f01Python -B $f01Script @f01PilotArgs --execute --authorize-research-import
if ($LASTEXITCODE -ne 0) { throw 'Pilotimport unterbrochen oder blockiert' }
```

**Resume nach einem unterbrochenen Lauf mit veröffentlichtem Import-Intent**

```powershell
& $f01Python -B $f01Script @f01PilotArgs --execute --authorize-research-import --resume
if ($LASTEXITCODE -ne 0) { throw 'Pilot-Resume blockiert' }
```

Zieldatei, Checkpoints, Request-Archiv und `binding.json` unverändert erhalten. Mit veröffentlichter V2-Bindung ist `--resume` der erlaubte Wiederanlauf. Ein früherer V1-Intent, ein veränderter Scope, abweichender Code, geänderte Evidence oder ein fremder Receipt brechen ab. Bereits bestätigte Requests werden über das bestehende Receipt wiedergefunden. Hinterlassene, als solche erkennbare Request-Tempdateien bleiben erhalten. Unbekannte Einträge und SQLite-WAL/SHM bleiben gesperrt. Ein Rollback-Journal wird nur im autorisierten Resume unter der vorhandenen Stage-Sperre von SQLite selbst behandelt. Verwaiste Result-Staging-Verzeichnisse gelten als unvollständig.

## Fact-, Filing- und PIT-Vertrag

Der Pilot verwendet die bestehende Tag-/Unit-Normalisierung und die bestehenden Formregeln `10-K`, `10-K/A`, `10-Q`, `10-Q/A`, `20-F`, `20-F/A` und `40-F`. Ein expliziter Parser-Modus liefert jede gemappte Beobachtung vor der bisherigen Reader-Deduplikation. Die Canonical-Identität und der Writer entscheiden über Fact, Occurrence, Membership und Revision. Die gesamte verfügbare und zugelassene Historie bleibt ohne Jahresfilter erhalten. Einzelne Werte mit verschiedenem Tag, Frame, Periodenkontext oder Accession bleiben unterscheidbar.

Ein Fact mit unbelegter, widersprüchlicher oder nach Filing-Datum/Form abweichender Submissions-Zuordnung bleibt in der Quarantäne und im gebundenen Original nachvollziehbar. In einem einzelnen Anbieter-Snapshot lässt sich bei gleichem Canonical-Key und abweichenden Mutable-Werten keine zeitliche Rangfolge nachweisen. Alle diese Beobachtungen werden als Konflikt dokumentiert. Original-Filings und Amendments behalten ihre getrennten Accessions. Ein automatisch angenommener Link vom Amendment zu einer Originalfassung ist kein Bestandteil des Imports.

Die Request-Archive enthalten Haupt- und historische Submissions-Payloads, Member-Hashes sowie die tatsächlichen Abrufzeiten beider Endpoints. Nichtganzzahlige Companyfacts-Werte werden ohne binäre Rundung als ursprüngliche numerische Lexeme für Decimal normalisiert. Die unveränderten ZIPs bleiben der Beleg für das genaue Rohformat. Fehlende oder ungezonte Acceptance-Werte bleiben ungeklärt. Wirtschaftsperioden, Filed-Datum, belegte Acceptance, tatsächlicher Retrieval-Stand und Dataset-Version haben unterschiedliche Semantik. Der aktuelle Provider-Stand liefert keinen Nachweis historischer Kenntnisstände. `dataset/sec` und Champion-Readiness werden durch den Pilot nicht aktualisiert.

## Ergebnisartefakte und Messung

Nach sieben bestätigten Receipts erscheinen unter `import-artifacts/f01-pilot-<dataset_id>/result/` atomar `f01_sec_bulk_pilot_report.json`, `f01_sec_bulk_pilot_issuer_stats.csv`, `f01_sec_bulk_pilot_import_audit.md` und `f01_sec_bulk_pilot_fact_quarantine.json`. Bei einem leeren Bootstrap ist dann Dataset-Version 8 erreicht. Der JSON-Bericht und das Lauf-Audit weisen BAC und JSM getrennt aus. `SEC_BULK_PILOT_IMPORTED` bezeichnet die erfolgreich persistierte Research-Quelle.

Der Bericht zählt Rohzeilen, zugelassene und ausgeschlossene Facts, Canonical-Facts, Occurrences, Memberships, Konflikte und Revisionen. Die Duplikatreduktion misst zugelassene Beobachtungen minus gespeicherte Occurrences. Größen und Stichproben für Sidecars/Volume werden pro CIK und Gesamtlauf erfasst. Die Windows Working-Set-Spitze gilt für den aufrufenden Prozess. Laufzeit- und Peak-Angaben eines Resume beziehen sich auf diese Ausführung. Die lineare Hochrechnung aus sieben nicht repräsentativen Emittenten bildet ein Pilot-Szenario für Datenbankwachstum einschließlich Indizes und Envelope-Archive. Vor einem Vollimport sind Verteilung, Reserve, Journal, Temp, Backup und Quellenarchive separat zu bemessen. G5, historische PIT-, historische Universe- und Produktivfreigaben bleiben offen.
