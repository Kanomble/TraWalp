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

## Cache und Wiederanlauf

Die Dokumentliste stammt aus dem SEC-Filing-Verzeichnis `index.json`. Das Skript bezieht das lokal belegte Primärdokument, identifizierbare XBRL-Instances sowie lokale XSDs und übliche Linkbases aus demselben Verzeichnis. Ein Resume ergänzt auch XSDs und Linkbases zu bereits gecachten Dokumenten. Die wiederverwendete SEC-HTTP-Schicht begrenzt auf einen Request pro Sekunde, behandelt temporäre Fehler und `Retry-After` und sperrt Redirects. Eine Windows-Dateisperre verhindert parallele Downloads in diesem Cache.

Ein vollständig publizierter Cache-Eintrag enthält Originalbytes und `source.json` mit URL, SHA-256, Größe, CIK, Accession, Abrufzeit und Filing-Verfügbarkeit. Folgeläufe prüfen die Bindung und den Hash. Ein korrupter Eintrag blockiert und bleibt zur Diagnose erhalten. Ein Abbruch kann einen `.incomplete-*`-Ordner hinterlassen. Resume startet dieses Dokument ab Byte null und überspringt alle bereits geprüften Dokumente.

## Extraktion und Review

Der isolierte Worker verwendet `arelle-release==2.47.5` und die offizielle `arelle.api.Session` mit `workOffline=True`. Der historische Interpreter startet den Worker über `-I -S`. Nur der separate `site`-Pfad stellt Worker-Abhängigkeiten bereit. Es werden weder Pakete noch Konfigurationen in der historischen Umgebung installiert. Der Entwurf protokolliert die genaue Arelle- und Paketversion, den SHA-256 der Requirements-Datei und die verwendeten Taxonomie-Pakete. [Python-API](https://arelle.readthedocs.io/en/latest/python_api/python_api.html) · [Paketrelease](https://pypi.org/project/arelle-release/2.47.5/)

Für historische Taxonomien den versionsgebundenen Webcache unter `$Arelle\cache` bereitstellen. Geprüfte lokale Arelle-Taxonomiepakete als ZIP unter `$Arelle\packages` ablegen und in `$Arelle\taxonomy-packages.json` als JSON-Array aus `{"file":"paket.zip","sha256":"…"}` inventarisieren. Der Worker verwendet nur lokale Paketpfade. Lokale Taxonomie-Dokumente, die Arelle auflöst, werden nach Möglichkeit per SHA-256 gebunden. Fehlende Taxonomien werden als fehlende Evidenz protokolliert. Der Worker blockiert zusätzlich Python-Socket-Verbindungs- und DNS-Aufrufe. Für den Bezug noch fehlender Taxonomiedateien ist ein separater manueller Vorbereitungsschritt nötig.

Arelle liefert exakte QNames, Zahlen, Originaltext, Scale, Sign, Decimals, Units, Perioden, Entity-Identifier, explizite und typisierte Dimensionen sowie verfügbare Calculation- und Presentation-Beziehungen. Das Skript ergänzt Dokumentbindung, Quellzeile und gegebenenfalls HTML-Tabelle und Caption. Jede vorhandene Originalseite oder Instance wird über Arelle geprüft. Ein per Arelle gelesener alternativer Instance-Kandidat und Fehler in der Primärdatei erscheinen getrennt in der Extraktionsspur.

| Status | Bedeutung |
| --- | --- |
| `VERIFIED_SOURCE_FACT` | Eindeutiger Original-Fact mit übereinstimmendem CIK, QName, Unit, Periode und Wert. |
| `CANDIDATE_EQUIVALENCE` | Eine rechnerische Relation ist mit den gemeldeten Decimals vereinbar. |
| `UNRESOLVED_CONTEXT` | Mehrdeutigkeit, Reporting-Kontext, Zeitbindung oder Transformation erfordert Prüfung. |
| `CONFLICT` | Vergleichbare Werte widersprechen einander oder die geprüften Rundungsintervalle sind disjunkt. |
| `MISSING_EVIDENCE` | Dokument, passender Fact oder erforderliche Decimals fehlen. |

Der Entwurf enthält normalisierte Facts im JSON-Format von `FundamentalFact` und getrennt Rohkonzepte ohne bestehendes Mapping. Ein Join führt über die Dokumentposition zum Quellhash. Die getrennt zu pflegende Review-Datei kann exakte `FundamentalFact`-Identitäten mit `ReportingContext` und einem Originalbeleg dokumentieren. Dazu kommen belegte Acceptance-Zeiten sowie gegebenenfalls bestätigte Amendment-Verkettung oder Debt-Komplettheit. Im automatisch erzeugten Entwurf bleiben die vier `Evidence`-Felder `contexts`, `acceptance`, `amendments` und `debt_coverage` leer. Ein exakter Zahlenabgleich gibt keine Accounting-Äquivalenz frei. [Research-API](../src/trading_system/fundamentals/semantic.py)
