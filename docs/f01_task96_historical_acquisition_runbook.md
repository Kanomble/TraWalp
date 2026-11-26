# Task 9.6 · Historical Data Acquisition

Der Runner bindet das Task-9.5-v2-Manifest, die Plandatei, den Plan-Fingerprint und die D.6-Kohorte.
Er überprüft die Bindung eines Blockes erneut, bevor eine Netzwerkoperation beginnen kann. Alle
bestehenden Datenbanken und Original-Caches bleiben geschützt. Download, Arelle-Verarbeitung und
Accounting-Evidenz haben separate Ziele. Ein Canonical-Import ist in dieser CLI nicht enthalten.

## Offline-Bestandsabgleich

~~~powershell
Set-Location 'G:\GitHub\TraWalp'
$python = '.\.venv\Scripts\python.exe'
$script = 'scripts/run_f01_historical_acquisition.py'
& $python -B $script report
$state = & $python -B $script status | ConvertFrom-Json
$evidence = $state.active_report_directory
~~~

Der erste Report veröffentlicht den Root-Belegsatz atomar. Weitere Reportläufe legen gebundene
Snapshots ab, sobald sich Belege oder Code ändern. Der Pfad steht jeweils in der CLI-Ausgabe.
Die Einzelschritte reuse und fundamentals geben den lokalen Abgleich separat aus. Der Report
verifiziert die Bytebindungen der Originale und der gebundenen Arelle-Shards. Die E.13-Zeugen
bleiben an E.11 und seine Review-, Dataset- und Reader-Fassung gebunden. Die bisherigen Nike-Werte
behalten ihre ursprünglichen Zulassungen.

Zusätzliche DEI-Aktienzahlen sind Companyfacts-Research-Kandidaten. Issued, gewichtete Aktienzahlen
und Klasse-only-Tags werden separat ausgewiesen. Staging-Cashflow erhält eine Diagnose für vier
direkte Quartale mit beobachteter Acceptance. YTD-Werte werden dabei nicht als Quartale gezählt.
Total Debt bleibt separates Reported Total Debt. Der Runner summiert keine ungeprüften Komponenten.

## Genehmigung pro Pilotblock

Der Report enthält je einen SEC- und SPY-Pilot-Preflight sowie zwei PENDING-Entwürfe. Einen Block
erst nach Prüfung von Accessions oder Sessions, Budgets, Provider-Konditionen, Aufbewahrungsrechten
und Backup/Restore freigeben. Dazu den passenden Entwurf an einen eigenen Freigabepfad kopieren.
Die prüfende Person trägt in dieser Kopie APPROVED, ihren Namen, einen ISO-8601-Zeitpunkt mit
Zeitzone und konkrete Belegverweise in provider_terms_evidence, retention_rights_evidence und
backup_restore_evidence ein. Limits darf sie verkleinern. Den Scope-Hash unverändert übernehmen.
Den ausgefüllten Entwurf niemals über die publizierte Vorlage schreiben. Nach einem
Codewechsel auf den zum aktuellen Code passenden Report-Pfad aus status wechseln.

### SPY · Ein autorisierter SIP/all-Block

Alpaca-Schlüssel werden aus der bestehenden lokalen TraWalp-Konfiguration geladen. Feed SIP,
Adjustment all und Daily sind fest gebunden. Bereits belegte Sessions derselben Variante werden
beim Preflight abgezogen. Vor-2016-Blöcke und sämtliche Issuer-Daily-Blöcke bleiben gesperrt,
bis die jeweils ausstehenden Provider- und Identitätsbelege vorliegen.

~~~powershell
$spy = Join-Path $evidence 'spy_pilot_preflight.json'
$spyBlock = (Get-Content -Raw -LiteralPath $spy | ConvertFrom-Json).block_id
# Lokaler Pfad zur separat erteilten und ausgefuellten Genehmigung.
$spyApproval = Join-Path $env:LOCALAPPDATA 'TraWalp-task96-spy-approved.json'
$spySha = (Get-FileHash -Algorithm SHA256 -LiteralPath $spy).Hash.ToLowerInvariant()
$spyApprovalSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $spyApproval).Hash.ToLowerInvariant()
& $python -B $script execute --block-id $spyBlock --preflight $spy --preflight-sha256 $spySha --authorization $spyApproval --authorization-sha256 $spyApprovalSha
~~~

Der Runner speichert rohe Provider-Seiten, Request-Query, Hash und Zeitstempel unter
G:\TraWalpResearch\market-data\f01-task96-acquisition-v1. Er prüft Session, Symbol,
Timestamp, OHLCV und Pagination. Fehlende Sessions bleiben explizit offen. 401/403 blockieren
den Scope. 429 beachtet belegtes Retry-After. Bei ungeklärtem Retry-After bleibt der Limiter
gesperrt. Providerantworten und Request-Budgets werden ohne Zugangsdaten gespeichert.

### SEC · Ein autorisierter Filing-Block

Den Operator- und Kontakt-User-Agent lokal setzen. Die Freigabe bindet den vollständigen
im Preflight enthaltenen Downloadscope. Nochmals vor Transport geprüft vorhandene
lokale Originale werden zur Wiederverwendung oder Prüfung zurückgestellt.

~~~powershell
$sec = Join-Path $evidence 'sec_pilot_preflight.json'
$secBlock = (Get-Content -Raw -LiteralPath $sec | ConvertFrom-Json).block_id
$secApproval = Join-Path $env:LOCALAPPDATA 'TraWalp-task96-sec-approved.json'
$secSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $sec).Hash.ToLowerInvariant()
$secApprovalSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $secApproval).Hash.ToLowerInvariant()
# SEC User-Agent lokal setzen, z.B. aus dem bereits verwendeten Kontaktwert.
& $python -B $script execute --block-id $secBlock --preflight $sec --preflight-sha256 $secSha --authorization $secApproval --authorization-sha256 $secApprovalSha --user-agent $env:SEC_USER_AGENT
~~~

Neuer SEC-Cache und Checkpoints liegen unter
G:\TraWalpResearch\sec-originals\f01-task96-acquisition-v1. Resume verwendet erfolgreich
verifizierte Dokumente weiter. Die separaten Metadata-/Amendment-Review-Paare gehören nicht
zum Netzwerkscope. Der SEC-Runner nutzt den vorhandenen Task-9-Lock und den persistenten SEC-Limiter.

## Resume und offline weiterverarbeiten

Bei Prozessabbruch dieselben Befehle mit resume anstelle von execute und exakt denselben
Freigabe- und Preflight-Dateien verwenden. Request-Attempts werden vor Transport gezählt.
Bei ungeklärter Übertragung wird die reservierte Bytegrenze konservativ auf das Budget
angerechnet. Ein fachlich gesperrter oder ausgeschöpfter Block braucht eine neue Prüfung.
Bereits publizierte Belege bleiben unverändert.

~~~powershell
# Verifizierte Originale eines definierten Planblocks offline verarbeiten.
& $python -B $script process --block-id $secBlock
# Neues Inventar mit verbleibenden Luecken ausgeben.
& $python -B $script report
~~~

Arelle nutzt die vorhandene Offline-Runtime und die lokale DTS-Closure. Neue Shards liegen
separat unter G:\TraWalpResearch\arelle-evidence\f01-task96-acquisition-v1. Die
bestehende Maschinenregel prüft ausschließlich Assets und Cash. Die übrigen Originale
erhalten Match- oder Review-Evidenz, keine erweiterte Zulassung. Ein Prozess mit fehlenden
DTS-Abhängigkeiten bleibt blockiert. Netzwerkscope ist per Preflight einzeln prüfbar.

~~~powershell
& $python -B $script preflight --block-id $secBlock
~~~

Exitcode 0 steht für einen erfolgreichen Befehl, dessen Ergebnis auch explizite Scope-Blocker
enthalten kann. Exitcode 2 kennzeichnet CLI-Syntaxfehler. Exitcode 3 signalisiert einen
Ausführungs- oder Bindungsblocker. Ein Ausführungsreport erteilt keine Import- oder Trading-Freigabe.
