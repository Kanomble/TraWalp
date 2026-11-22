# F01 Original-Filing-Evidenz

Der Standardmodus prüft das lokale Metadateninventar. Der Cache und die Offline-Entwürfe liegen unter `G:\TraWalpResearch\sec-originals\filing-evidence\`.

## PowerShell

Auf `SEC-integration` ausführen. Beim SEC-User-Agent den eigenen Namen oder die Organisation und eine erreichbare E-Mail-Adresse angeben.

```powershell
Set-Location 'G:\GitHub\TraWalp'
$Python = '.\.venv\Scripts\python.exe'
$Script = '.\scripts\prepare_f01_filing_evidence.py'

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

# 4. Offline-Evidenzentwurf
$Run = Get-Date -Format 'yyyyMMdd-HHmmss'
& $Python -B $Script --extract --output "f01-original-filing-evidence-draft-$Run.json"
if ($LASTEXITCODE -ne 0) { throw 'Offline-Extraktion fehlgeschlagen' }
```

Für alle 83 Accessions `--include-additional` an Download, Resume und Extraktion anhängen. Der Preflight zeigt beide Mengen einschließlich der Probe-Zuordnungen. Für andere Emittenten akzeptiert `--inventory-file` eine JSON-Liste aus `cik` (zehn Stellen), `symbol`, `accession` und optional `primary` (SEC-Dateiname), `filed` und `acceptance` (ISO mit Offset). Der individuelle Modus extrahiert Original-Facts. Der automatische Companyfacts-Abgleich verwendet das eingebaute Pilotinventar. Individuell eingetragene Filing-Zeiten benötigen eine getrennte Quellenprüfung.

## Cache und Wiederanlauf

Die Dokumentliste stammt aus dem SEC-Filing-Verzeichnis `index.json`. Das Skript bezieht das lokal belegte Primärdokument und einen identifizierbaren XBRL-Instance-Kandidaten aus demselben Verzeichnis. Die wiederverwendete SEC-HTTP-Schicht begrenzt auf einen Request pro Sekunde, behandelt temporäre Fehler und `Retry-After` und sperrt Redirects. Eine Windows-Dateisperre verhindert parallele Downloads in diesem Cache.

Ein vollständig publizierter Cache-Eintrag enthält Originalbytes und `source.json` mit URL, SHA-256, Größe, CIK, Accession, Abrufzeit und Filing-Verfügbarkeit. Folgeläufe prüfen die Bindung und den Hash. Ein korrupter Eintrag blockiert und bleibt zur Diagnose erhalten. Ein Abbruch kann einen `.incomplete-*`-Ordner hinterlassen. Resume startet dieses Dokument ab Byte null und überspringt alle bereits geprüften Dokumente.

## Extraktion und Review

Die XML-Extraktion erfasst exakte QNames, Zahlen und Originaltext, Scale, Sign, Decimals, Units, Perioden, Entity-Identifier, explizite und typisierte Dimensionen sowie den Dokumentbezug. Wenn vorhanden, nennt sie die HTML-Tabelle und ihre Caption. XML-kompatibles iXBRL und XBRL-Instances werden mit der Standardbibliothek verarbeitet. Unlesbares HTML kann einen dokumentierten Instance-Fallback verwenden. Unbekannte Transformationen bleiben offen. Arelle wäre eine separate Option für eine spätere Erweiterung.

| Status | Bedeutung |
| --- | --- |
| `VERIFIED_SOURCE_FACT` | Eindeutiger Original-Fact mit übereinstimmendem CIK, QName, Unit, Periode und Wert. |
| `CANDIDATE_EQUIVALENCE` | Eine rechnerische Relation ist mit den gemeldeten Decimals vereinbar. |
| `UNRESOLVED_CONTEXT` | Mehrdeutigkeit, Reporting-Kontext, Zeitbindung oder Transformation erfordert Prüfung. |
| `CONFLICT` | Vergleichbare Werte widersprechen einander oder die geprüften Rundungsintervalle sind disjunkt. |
| `MISSING_EVIDENCE` | Dokument, passender Fact oder erforderliche Decimals fehlen. |

Der Entwurf enthält normalisierte Facts im JSON-Format von `FundamentalFact` und getrennt Rohkonzepte ohne bestehendes Mapping. Ein Join führt über die Dokumentposition zum Quellhash. Die getrennt zu pflegende Review-Datei kann exakte `FundamentalFact`-Identitäten mit `ReportingContext` und einem Originalbeleg dokumentieren. Dazu kommen belegte Acceptance-Zeiten sowie gegebenenfalls bestätigte Amendment-Verkettung oder Debt-Komplettheit. Im automatisch erzeugten Entwurf bleiben die vier `Evidence`-Felder `contexts`, `acceptance`, `amendments` und `debt_coverage` leer. Ein exakter Zahlenabgleich gibt keine Accounting-Äquivalenz frei. [Research-API](../src/trading_system/fundamentals/semantic.py)
