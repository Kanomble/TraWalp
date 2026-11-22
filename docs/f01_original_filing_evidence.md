# F01 Original-Filing-Evidenz

Der Standardmodus prüft das lokale Metadateninventar. Der Cache und die Offline-Entwürfe liegen unter `G:\TraWalpResearch\sec-originals\filing-evidence\`.

## PowerShell

Auf `SEC-integration` ausführen. Beim SEC-User-Agent den eigenen Namen oder die Organisation und eine erreichbare E-Mail-Adresse angeben.

```powershell
Set-Location 'G:\GitHub\TraWalp'
$Python = '.\.venv\Scripts\python.exe'
$Script = '.\scripts\prepare_f01_filing_evidence.py'

# Research-Abhängigkeiten separat auf G: installieren
$Arelle = 'G:\TraWalpResearch\arelle\2.47.5'
$Site = Join-Path $Arelle 'site'
$env:PIP_CACHE_DIR = Join-Path $Arelle 'pip-cache'
$env:TEMP = Join-Path $Arelle 'temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $Arelle,$env:PIP_CACHE_DIR,$env:TEMP | Out-Null
& $Python -B -m pip --disable-pip-version-check install --only-binary ':all:' --target $Site -r '.\requirements-f01-arelle.txt'
if ($LASTEXITCODE -ne 0) { throw 'Installation der Arelle-Research-Abhängigkeiten fehlgeschlagen' }

# 1. Lokaler Metadaten-Preflight
& $Python -B $Script
if ($LASTEXITCODE -ne 0) { throw 'Metadaten-Preflight fehlgeschlagen' }

# 2. Autorisierter Download der 18 Start-Accessions
$Agent = Read-Host 'SEC User-Agent mit Name oder Organisation und Kontakt-E-Mail'
& $Python -B $Script --download --authorize-download --user-agent $Agent
if ($LASTEXITCODE -ne 0) { throw 'Download unterbrochen oder blockiert' }

# 3. Resume nach einer Unterbrechung
& $Python -B $Script --resume --authorize-download --user-agent $Agent
if ($LASTEXITCODE -ne 0) { throw 'Resume unterbrochen oder blockiert' }

# 4. Offline-Evidenzentwurf mit Arelle
$Run = Get-Date -Format 'yyyyMMdd-HHmmss'
& $Python -B $Script --extract --arelle-dir $Arelle --output "f01-original-filing-evidence-draft-$Run.json"
if ($LASTEXITCODE -ne 0) { throw 'Offline-Extraktion fehlgeschlagen' }
```

Für alle 83 Accessions `--include-additional` an Download, Resume und Extraktion anhängen. Der Preflight zeigt beide Mengen einschließlich der Probe-Zuordnungen. Für andere Emittenten akzeptiert `--inventory-file` eine JSON-Liste aus `cik` (zehn Stellen), `symbol`, `accession` und optional `primary` (SEC-Dateiname), `filed` und `acceptance` (ISO mit Offset). Der individuelle Modus extrahiert Original-Facts. Der automatische Companyfacts-Abgleich verwendet das eingebaute Pilotinventar. Individuell eingetragene Filing-Zeiten benötigen eine getrennte Quellenprüfung.

## Taxonomie-Bootstrap für die bereits gecachten 18 Filings

Die folgenden Befehle verwenden den gebundenen Entwurf und die vorhandenen Cache-Dateien. Der Netzwerkmodus bezieht ausschließlich Taxonomien und den fest gepinnten EDGAR-Transform. Für diese Abfolge ist kein Filing-Download erforderlich.

```powershell
Set-Location 'G:\GitHub\TraWalp'
$Python = '.\.venv\Scripts\python.exe'
$Arelle = 'G:\TraWalpResearch\arelle\2.47.5'
$Bootstrap = '.\scripts\prepare_f01_arelle_taxonomies.py'
$Evidence = '.\scripts\prepare_f01_filing_evidence.py'

# 1. Metadaten-Preflight mit lokalem Cache und Entwurf
& $Python -B $Bootstrap
if ($LASTEXITCODE -ne 0) { throw 'Taxonomie-Preflight fehlgeschlagen' }

# 2. Autorisierter Download von historischen Taxonomien und EDGAR-Plugin
$Agent = Read-Host 'SEC User-Agent mit Name oder Organisation und Kontakt-E-Mail'
$Run = Get-Date -Format 'yyyyMMdd-HHmmss'
& $Python -B $Bootstrap --download --authorize-download --user-agent $Agent |
    Tee-Object -FilePath (Join-Path $Arelle "taxonomy-bootstrap-$Run.json")
if ($LASTEXITCODE -ne 0) { throw 'Taxonomie-Bootstrap meldet fehlende oder blockierte Quellen' }

# 3. Offline-Coverage der exakt referenzierten URLs und Abhängigkeiten
& $Python -B $Bootstrap --offline-coverage |
    Tee-Object -FilePath (Join-Path $Arelle "taxonomy-coverage-$Run.json")
if ($LASTEXITCODE -ne 0) { throw 'Offline-Coverage ist weiterhin offen' }

# 4. Erneute Extraktion der 18 vorhandenen Start-Filings
& $Python -B $Evidence --extract --arelle-dir $Arelle --output "f01-original-filing-evidence-bootstrap-$Run.json"
if ($LASTEXITCODE -ne 0) { throw 'Offline-Extraktion fehlgeschlagen' }
```

`--draft` kann einen weiteren lokalen Evidenzentwurf benennen. Der Standardentwurf ist auf seinen SHA-256 gebunden. Preflight und Coverage führen fehlende Offlinedateien und nicht unterstützte Zielpfade aus. `--download` kann nach einer Unterbrechung erneut aufgerufen werden. Dabei werden bekannte Pakete, Cache-Dateien und Plugin-Module gegen ihre Bindungen geprüft.

## Cache und Wiederanlauf

Die Dokumentliste stammt aus dem SEC-Filing-Verzeichnis `index.json`. Das Skript bezieht das lokal belegte Primärdokument, identifizierbare XBRL-Instances sowie lokale XSDs und übliche Linkbases aus demselben Verzeichnis. Ein Resume ergänzt auch XSDs und Linkbases zu bereits gecachten Dokumenten. Die wiederverwendete SEC-HTTP-Schicht begrenzt auf einen Request pro Sekunde, behandelt temporäre Fehler und `Retry-After` und sperrt Redirects. Eine Windows-Dateisperre verhindert parallele Downloads in diesem Cache.

Ein vollständig publizierter Cache-Eintrag enthält Originalbytes und `source.json` mit URL, SHA-256, Größe, CIK, Accession, Abrufzeit und Filing-Verfügbarkeit. Folgeläufe prüfen die Bindung und den Hash. Ein korrupter Eintrag blockiert und bleibt zur Diagnose erhalten. Ein Abbruch kann einen `.incomplete-*`-Ordner hinterlassen. Resume startet dieses Dokument ab Byte null und überspringt alle bereits geprüften Dokumente.

## Extraktion und Review

Der isolierte Worker verwendet `arelle-release==2.47.5` und die offizielle `arelle.api.Session` mit `workOffline=True`. Der historische Interpreter startet den Worker über `-I -S`. Nur der separate `site`-Pfad stellt Worker-Abhängigkeiten bereit. Es werden weder Pakete noch Konfigurationen in der historischen Umgebung installiert. Der Entwurf protokolliert die genaue Arelle- und Paketversion, den SHA-256 der Requirements-Datei und die verwendeten Taxonomie-Pakete. [Python-API](https://arelle.readthedocs.io/en/latest/python_api/python_api.html) · [Paketrelease](https://pypi.org/project/arelle-release/2.47.5/)

Der Bootstrap bevorzugt offizielle Versionspakete und materialisiert die benötigten Dateien im Webcache unter `$Arelle\cache` an ihrem exakten URI-Slot. Upstream-Pakete bleiben unter `$Arelle\bootstrap-packages` erhalten. Der SEC-Transform liegt unter `$Arelle\plugins` und wird aus EDGAR-Release 26.3, Commit `4891c4c9889c77af247428ceff4fa7cc52d534ea`, geladen. Die zwei offiziellen Module werden einzeln gehasht. Optional separat bereitgestellte Arelle-Taxonomiepakete können weiterhin als ZIP unter `$Arelle\packages` liegen und in `$Arelle\taxonomy-packages.json` als Array aus `{"file":"paket.zip","sha256":"…"}` inventarisiert werden. Fehlende Taxonomien bleiben im Entwurf sichtbar. Der Worker blockiert zusätzlich Python-Socket-Verbindungs- und DNS-Aufrufe. [SEC-Updates](https://www.sec.gov/data-research/structured-data/taxonomies-schemas/taxonomy-files-annual-updates) · [EDGAR Release 26.3](https://github.com/Arelle/EDGAR/releases/tag/26.3)

Arelle liefert exakte QNames, Zahlen, Originaltext, Scale, Sign, Decimals, Units, Perioden, Entity-Identifier, explizite und typisierte Dimensionen sowie verfügbare Calculation- und Presentation-Beziehungen. Das Skript ergänzt Dokumentbindung, Quellzeile und gegebenenfalls HTML-Tabelle und Caption. Gewöhnliches HTML bleibt als Originalbeleg im Cache und wird als ungeeignet für die XBRL-Fact-Extraktion gekennzeichnet. Bei vorhandener verifizierter Instance übernimmt diese die Fact-Extraktion. Pro Filing zeigt `diagnostic_summary` die Zahl der Fehler je Kategorie und die Join-Status. Die zugrunde liegenden Fehler bleiben erhalten.

| Status | Bedeutung |
| --- | --- |
| `VERIFIED_SOURCE_FACT` | Eindeutiger Original-Fact mit übereinstimmendem CIK, QName, Unit, Periode und Wert. |
| `CANDIDATE_EQUIVALENCE` | Eine rechnerische Relation ist mit den gemeldeten Decimals vereinbar. |
| `UNRESOLVED_CONTEXT` | Mehrdeutigkeit, Reporting-Kontext, Zeitbindung oder Transformation erfordert Prüfung. |
| `CONFLICT` | Vergleichbare Werte widersprechen einander oder die geprüften Rundungsintervalle sind disjunkt. |
| `MISSING_EVIDENCE` | Dokument, passender Fact oder erforderliche Decimals fehlen. |

Der Entwurf enthält normalisierte Facts im JSON-Format von `FundamentalFact` und getrennt Rohkonzepte ohne bestehendes Mapping. Ein Join führt über die Dokumentposition zum Quellhash. Die getrennt zu pflegende Review-Datei kann exakte `FundamentalFact`-Identitäten mit `ReportingContext` und einem Originalbeleg dokumentieren. Dazu kommen belegte Acceptance-Zeiten sowie gegebenenfalls bestätigte Amendment-Verkettung oder Debt-Komplettheit. Im automatisch erzeugten Entwurf bleiben die vier `Evidence`-Felder `contexts`, `acceptance`, `amendments` und `debt_coverage` leer. Ein exakter Zahlenabgleich gibt keine Accounting-Äquivalenz frei. [Research-API](../src/trading_system/fundamentals/semantic.py)

## Extended Research Evidence · sieben Emittenten

**SEC_EXTENDED_FILING_EVIDENCE_READY** bezeichnet den vorbereiteten Erfassungs- und Review-Ablauf. Der lokale Metadaten-Preflight auf der gebundenen Pilot-Spec V2 ergab 339 eindeutige Filings für AAPL, ACN, CAT, COST, LLY, MSFT und NVDA. Primärdokument und Acceptance-Zeit sind im heutigen gebundenen Submissions-Bestand für alle 339 vorhanden. Der Bestand enthält 18 Start-Filings, 65 weitere Referenzen der 49 Stichtagsproben und 256 zusätzliche Filings. Der Batch umfasst 142 mit Warm-up-Kennzeichnung. BAC und JSM bleiben außerhalb des Scopes.

| Emittent | Start | Aus den 49 Proben zusätzlich | Erweiterung | Gesamt |
| --- | ---: | ---: | ---: | ---: |
| AAPL | 1 | 8 | 39 | 48 |
| ACN | 3 | 1 | 43 | 47 |
| CAT | 2 | 7 | 42 | 51 |
| COST | 3 | 20 | 27 | 50 |
| LLY | 8 | 28 | 12 | 48 |
| MSFT | 1 | 1 | 46 | 48 |
| NVDA | 0 | 0 | 47 | 47 |

Die Vereinigung verwendet CIK und Accession und speichert getrennte Abdeckungsgründe. Sie enthält die im Accounting-Audit genannten Accessions und die durch Submissions gebundenen 10-K, 10-Q und Amendments vom 01.01.2015 bis zum qualifizierten Pilotende 20.11.2026. Die ältere Diagnose kann zusätzlich frühere Quellfilings beisteuern. Der Zeitraum begrenzt die neu inventarisierten Filings über das Filing-Datum. Alle Facts und Vergleichsperioden innerhalb dieser Filings bleiben erhalten. Die regelmäßigen ACN-Filings liefern die historischen Deckblätter für Class A und Class X. Die Zuordnung zum handelbaren Wertpapier und die Freshness an jedem Stichtag benötigen weiterhin einen Review.

### Manueller Ablauf auf G:

```powershell
Set-Location 'G:\GitHub\TraWalp'
$Python = '.\.venv\Scripts\python.exe'
$Filing = 'scripts\prepare_f01_filing_evidence.py'
$Tax = 'scripts\prepare_f01_arelle_taxonomies.py'
$Resolve = 'scripts\resolve_f01_accounting_evidence.py'
$Research = 'G:\TraWalpResearch'
$Cache = Join-Path $Research 'sec-originals\filing-evidence'
$Arelle = Join-Path $Research 'arelle\2.47.5'
$Artifacts = Join-Path $Research 'import-artifacts'
$Run = Get-Date -Format 'yyyyMMdd-HHmmss'
$CacheName = "f01-extended-cache-$Run.json"
$CacheManifest = Join-Path $Cache $CacheName
$TaxCoverage = Join-Path $Arelle "taxonomy-extended-coverage-$Run.json"
$ExtractionName = "f01-extended-filing-evidence-$Run"
$Extraction = Join-Path $Cache $ExtractionName
$Manifest = Join-Path $Extraction 'manifest.json'
$Review = Join-Path $Artifacts "f01-extended-accounting-evidence-$Run"

# 1. Metadaten-Preflight und Zielvolume
Get-PSDrive -Name G | Select-Object Name,Used,Free
& $Python -B $Filing --extended |
    Tee-Object -FilePath (Join-Path $Artifacts "f01-extended-inventory-$Run.json")
if ($LASTEXITCODE -ne 0) { throw 'Filing-Inventar blockiert' }

# 2. Autorisierter Download der fehlenden Originale
$Agent = Read-Host 'SEC User-Agent mit Name oder Organisation und Kontakt-E-Mail'
& $Python -B $Filing --extended --download --authorize-download --user-agent $Agent
if ($LASTEXITCODE -ne 0) { throw 'Download unterbrochen. Verwende bei Wiederanlauf den Resume-Befehl.' }

# Alternativer Wiederanlauf nach einem Abbruch
# & $Python -B $Filing --extended --resume --authorize-download --user-agent $Agent

# 3. Quellen binden und Taxonomien vorbereiten
& $Python -B $Filing --extended --cached-inputs --output $CacheName
if ($LASTEXITCODE -ne 0) { throw 'Filing-Cache-Bindung blockiert' }
& $Python -B $Tax --draft $CacheManifest
if ($LASTEXITCODE -ne 0) { throw 'Taxonomie-Preflight blockiert' }
& $Python -B $Tax --draft $CacheManifest --download --authorize-download --user-agent $Agent
if ($LASTEXITCODE -ne 0) { throw 'Taxonomie-Bootstrap blockiert' }
& $Python -B $Tax --draft $CacheManifest --offline-coverage |
    Tee-Object -FilePath $TaxCoverage
if ($LASTEXITCODE -ne 0) { throw 'Offline-Taxonomie-Coverage unvollständig' }

# 4. Offline-Extraktion der gebundenen Originale
& $Python -B $Filing --extended --extract --cache-manifest $CacheManifest `
    --taxonomy-coverage $TaxCoverage --output $ExtractionName
if ($LASTEXITCODE -ne 0) { throw 'Offline-Extraktion blockiert' }

# 5. Accounting-Evidenzentwurf
$ManifestSha = (Get-FileHash -LiteralPath $Manifest -Algorithm SHA256).Hash.ToLowerInvariant()
$TaxSha = (Get-FileHash -LiteralPath $TaxCoverage -Algorithm SHA256).Hash.ToLowerInvariant()
& $Python -B $Resolve --draft $Manifest --draft-sha256 $ManifestSha `
    --taxonomy $TaxCoverage --taxonomy-sha256 $TaxSha --cache-manifest $CacheManifest --output $Review
if ($LASTEXITCODE -ne 0) { throw 'Accounting-Evidenzabgleich blockiert' }
```

Der Download und sein Resume prüfen vorhandene Originale gegen ihre SHA-256-Bindungen. Resume lädt nur fehlende Dokumente. Die Taxonomiephase verwendet die historische Arelle-Version und das versionsgebundene SEC-Plugin. Wenn Arelle weitere auflösbare Taxonomie-URLs erst während der Extraktion meldet, verwende bei Taxonomie-Preflight, autorisiertem Bootstrap und Offline-Coverage zusätzlich `--extraction-diagnostics $Manifest` zusammen mit demselben `--draft $CacheManifest`. Verwende danach neue Ergebnisnamen für Coverage, Extraktion und Accounting-Entwurf. Ändert sich der Filing-Cache, erstelle zuerst eine neue Cache-Bindung. Fehlende Dokumente, nicht unterstützte Taxonomie-Quellen und Arelle-Fehler stehen im Filing-Ergebnis.

Die erweiterte Extraktion schreibt je Filing eine gehashte JSON-Datei. Die Manifest-Publikation erfolgt gemeinsam mit den Shards durch Umbenennung des Staging-Verzeichnisses auf demselben Volume. Nach einem Abbruch bleiben unvollständige Staging-Verzeichnisse einzeln erkennbar. Starte die Extraktion bzw. den Accounting-Abgleich erneut mit einem neuen Ergebnisnamen. Der Original-Download muss dafür nicht wiederholt werden. Für den Platzbedarf zählen Originale, der Taxonomie-Cache, temporäre Kopien der Dokumente eines Filings, Extraktions- und Accounting-Shards sowie die Reserve. Die Größen dieses erweiterten Korpus sind vor dem vollständigen Download unbekannt. Der Batch prüft verfügbaren Platz pro Filing und erfasst die tatsächlichen Ergebnisbytes im Manifest.

Der Accounting-Entwurf indexiert Kandidaten für Aktienklassen, Debt/Leases, Gewinn- und Eigenkapitalabgrenzung, OCF, CapEx, Zins und Operating Income sowie EBITDA-/ROIC-Inputs. Die Verweise führen zur Originaldatei, zum exakten QName und Kontext, zur Filing-Version und zu vorhandenen Calculation- und Presentation-Rollen. Die Review-Felder bleiben leer. Für die fachliche Abnahme sind insbesondere Class-A-Market-Cap, Scope von Debt/Leases, getrennte Parent-/Konzerngewinnreihen, Cashflow-Kumulativen und LLY-Zins-/EBITDA-Scope einzeln zu prüfen. Filings behalten ihre belegte Acceptance-Zeit. Spätere Vergleichswerte bleiben an die spätere Version gebunden. Das Ergebnis ist eine Review-Grundlage für Research.

Der übernommene Candidate-Fingerprint lautet `5b063005e4bd70446b4dca1251c417d000c7997ffc029996629c73f6e0f5a1f6`. Der erweiterte Candidate auf `SEC-integration` mit HEAD `df8376e949a36dfecd04b0073dc24a0909683d2d` hat den Fingerprint `4db32c8ef4e6b900aee5f7cea5d09401fc70e23e9643810990cd2e89454bc480` nach dem bestehenden Codeinventar. Der Code-Fingerprint und die Hashes der Cache-, Taxonomie- und Extraktionsmanifeste werden bei der manuellen Ausführung geprüft.
