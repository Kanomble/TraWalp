# F01 – Canonical SEC Bulk Pilot Import

Der Importer verarbeitet ausschließlich AAPL, ACN, BAC, CAT, COST, LLY, MSFT und NVDA nach der [Pilot-Spec](f01_sec_bulk_pilot_spec.json). JSM bleibt gesperrt. Die Quelle ist die veröffentlichte Generation `pilot01-20261121-175840`, Manifest-SHA-256 `d0392ebbba463b4da8485e786c9986d163e02a57532197fb84c33491b27dbc20`. Preflight und Ausführung verwenden den bestehenden Offline-Verifier für `SEC_BULK_ARCHIVES_VERIFIED`, einschließlich ZIP-SHA-256 und seiner Census-/Checkpoint-Bindungen. Benötigte ZIP-Members werden einzeln gelesen und auf CRC geprüft.

## Voraussetzungen

- Ein leeres, separat autorisiertes `RESEARCH_ONLY`-Bootstrap-Ziel unter `G:\TraWalpResearch\research-databases` mit eigenem Dataset-Ursprung. Das Skript prüft Schema, Version 1, internen und externen Bootstrap-Nachweis, DB-SHA-256 für eine frische DB und anschließend Dateiidentität und Receipt-Präfix. Live-/Legacy-Ziele und historische Migrationsbestände sind ausgeschlossen.
- Eine unabhängig geprüfte Admission für jeden der acht CIKs. Die Pilot-Spec und das Bulk-Manifest qualifizieren die Quellenstruktur. Die Admission entscheidet separat über die CIK-/Symbol-Beziehung für Research und pro Taxonomie über die Source-Membership. Für einen zugelassenen Fact verlangt A‑V1 explizite Genehmigung des Canonical-Ankers am isolierten Ziel. Der Importer leitet diese Genehmigung weder aus aktuellen Tickersymbolen noch aus Namen oder aus Fact-Präsenz ab.
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
$f01Code = '81cc7e8fc1694a90cbb46a67ebdd7c1048f73f9b8bf01d7b37bcd7b0310eb08f'

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

**Separater Bootstrap eines neuen Ziels.** Wenn bereits ein leeres, geprüftes Bootstrap-Ziel am angegebenen Pfad existiert, diesen Block überspringen. Der zweite Aufruf ist der ausdrücklich schreibende Bootstrap-Schritt.

```powershell
$f01Seed = Join-Path $f01Root 'import-artifacts\f01-bootstrap-sec-bulk-pilot01.json'
Save-F01PilotJsonNew $f01Seed (([ordered]@{
    contract = 'F01_CLEAN_CANONICAL_RESEARCH_BOOTSTRAP_V1'
    classification = 'RESEARCH_ONLY'
    research_scope = 'F01 SEC-Bulk-Pilot 2020 bis zum qualifizierten Ende 2026'
} | ConvertTo-Json))
$f01SeedSha = (Get-FileHash -LiteralPath $f01Seed -Algorithm SHA256).Hash.ToLowerInvariant()
$f01SeedArgs = @('--target', $f01Target, '--manifest', $f01Seed,
    '--manifest-sha256', $f01SeedSha, '--expect-code-fingerprint', $f01Code)
& $f01Python -B .\scripts\bootstrap_f01_research_db.py @f01SeedArgs
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap-Preflight blockiert' }
& $f01Python -B .\scripts\bootstrap_f01_research_db.py @f01SeedArgs --execute --confirm-create-research-only
if ($LASTEXITCODE -ne 0) { throw 'Bootstrap blockiert' }
```

**Admission-Vorlage erzeugen.** In einer getrennten Review-Datei ausfüllen. Die Vorlage allein ist gesperrt.

```powershell
$f01TemplatePath = Join-Path $f01Root 'import-artifacts\f01-pilot-admission-template.json'
$f01Template = & $f01Python -B $f01Script --admission-template
if ($LASTEXITCODE -ne 0) { throw 'Admission-Vorlage nicht erstellt' }
Save-F01PilotJsonNew $f01TemplatePath ($f01Template -join "`n")
```

Die unabhängig geprüfte Datei als `G:\TraWalpResearch\import-artifacts\f01-pilot-admission-reviewed.json` bereitstellen. Für jeden Eintrag den Reviewer, belegten CIK, die Research-Entscheidung und die Hashreferenz auf einen geprüften Beleg setzen. Die referenzierten JSON-Belege liegen unter `import-artifacts` und haben Vertrag `F01_SEC_BULK_RESEARCH_EVIDENCE_V1`, `scope: "RESEARCH_ONLY"`, den exakten `cik`, `symbol`, die erwartete `taxonomy` (bei Identity `null`) und eine nachvollziehbare `source_reference`. `ADMIT_RESEARCH` für eine tatsächlich geprüfte Membership setzt `canonical_anchor: true` voraus. Eine ausdrückliche Ablehnung setzt `DENY` und `canonical_anchor: false`. JSON und SHA‑256 binden die Review-Dateien. Die fachliche Richtigkeit des angeführten Belegs muss der Reviewer prüfen.

**Read-only Preflight**

```powershell
$f01Admission = Join-Path $f01Root 'import-artifacts\f01-pilot-admission-reviewed.json'
$f01AdmissionSha = (Get-FileHash -LiteralPath $f01Admission -Algorithm SHA256).Hash.ToLowerInvariant()
$f01PilotArgs = @(
    '--layout', $f01Layout, '--target', $f01Target
    '--admission', $f01Admission, '--admission-sha256', $f01AdmissionSha
    '--expect-code-fingerprint', $f01Code
)
& $f01Python -B $f01Script @f01PilotArgs
if ($LASTEXITCODE -ne 0) { throw 'Pilot-Preflight blockiert' }
```

Den Preflight-Output auf acht Emittenten, Ausnahmen, Admissions und Kapazität prüfen. Dann den ersten schreibenden Lauf getrennt autorisieren.

```powershell
& $f01Python -B $f01Script @f01PilotArgs --execute --authorize-research-import
if ($LASTEXITCODE -ne 0) { throw 'Pilotimport unterbrochen oder blockiert' }
```

**Resume nach einem unterbrochenen Lauf mit veröffentlichtem Import-Intent**

```powershell
& $f01Python -B $f01Script @f01PilotArgs --execute --authorize-research-import --resume
if ($LASTEXITCODE -ne 0) { throw 'Pilot-Resume blockiert' }
```

Zieldatei, Checkpoints, Request-Archiv und `binding.json` unverändert erhalten. Mit veröffentlichter Bindung ist `--resume` der erlaubte Wiederanlauf. Abweichender Code, geänderte Evidence oder ein fremder Receipt brechen ab. Bereits bestätigte Requests werden über das bestehende Receipt wiedergefunden. Hinterlassene, als solche erkennbare Request-Tempdateien bleiben erhalten. Unbekannte Einträge und SQLite-WAL/SHM bleiben gesperrt. Ein Rollback-Journal wird nur im autorisierten Resume unter der vorhandenen Stage-Sperre von SQLite selbst behandelt. Verwaiste Result-Staging-Verzeichnisse gelten als unvollständig.

## Fact-, Filing- und PIT-Vertrag

Der Pilot verwendet die bestehende Tag-/Unit-Normalisierung und die bestehenden Formregeln `10-K`, `10-K/A`, `10-Q`, `10-Q/A`, `20-F`, `20-F/A` und `40-F`. Ein expliziter Parser-Modus liefert jede gemappte Beobachtung vor der bisherigen Reader-Deduplikation. Die Canonical-Identität und der Writer entscheiden über Fact, Occurrence, Membership und Revision. Die gesamte verfügbare und zugelassene Historie bleibt ohne Jahresfilter erhalten. Einzelne Werte mit verschiedenem Tag, Frame, Periodenkontext oder Accession bleiben unterscheidbar.

Ein Fact mit unbelegter, widersprüchlicher oder nach Filing-Datum/Form abweichender Submissions-Zuordnung bleibt in der Quarantäne und im gebundenen Original nachvollziehbar. In einem einzelnen Anbieter-Snapshot lässt sich bei gleichem Canonical-Key und abweichenden Mutable-Werten keine zeitliche Rangfolge nachweisen. Alle diese Beobachtungen werden als Konflikt dokumentiert. Original-Filings und Amendments behalten ihre getrennten Accessions. Ein automatisch angenommener Link vom Amendment zu einer Originalfassung ist kein Bestandteil des Imports.

Die Request-Archive enthalten Haupt- und historische Submissions-Payloads, Member-Hashes sowie die tatsächlichen Abrufzeiten beider Endpoints. Nichtganzzahlige Companyfacts-Werte werden ohne binäre Rundung als ursprüngliche numerische Lexeme für Decimal normalisiert. Die unveränderten ZIPs bleiben der Beleg für das genaue Rohformat. Fehlende oder ungezonte Acceptance-Werte bleiben ungeklärt. Wirtschaftsperioden, Filed-Datum, belegte Acceptance, tatsächlicher Retrieval-Stand und Dataset-Version haben unterschiedliche Semantik. Der aktuelle Provider-Stand liefert keinen Nachweis historischer Kenntnisstände. `dataset/sec` und Champion-Readiness werden durch den Pilot nicht aktualisiert.

## Ergebnisartefakte und Messung

Nach acht bestätigten Receipts erscheinen unter `import-artifacts/f01-pilot-<dataset_id>/result/` atomar `f01_sec_bulk_pilot_report.json`, `f01_sec_bulk_pilot_issuer_stats.csv`, `f01_sec_bulk_pilot_import_audit.md` und `f01_sec_bulk_pilot_fact_quarantine.json`. `SEC_BULK_PILOT_IMPORTED` bezeichnet die erfolgreich persistierte Research-Quelle.

Der Bericht zählt Rohzeilen, zugelassene und ausgeschlossene Facts, Canonical-Facts, Occurrences, Memberships, Konflikte und Revisionen. Die Duplikatreduktion misst zugelassene Beobachtungen minus gespeicherte Occurrences. Größen und Stichproben für Sidecars/Volume werden pro CIK und Gesamtlauf erfasst. Die Windows Working-Set-Spitze gilt für den aufrufenden Prozess. Laufzeit- und Peak-Angaben eines Resume beziehen sich auf diese Ausführung. Die lineare Hochrechnung aus acht nicht repräsentativen Emittenten bildet ein Pilot-Szenario für Datenbankwachstum einschließlich Indizes und Envelope-Archive. Vor einem Vollimport sind Verteilung, Reserve, Journal, Temp, Backup und Quellenarchive separat zu bemessen. G5, historische PIT-, historische Universe- und Produktivfreigaben bleiben offen.
