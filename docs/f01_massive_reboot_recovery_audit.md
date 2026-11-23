# F01 · Massive-Recovery nach Windows-Neustart

**Real geprüft: `REBOOT_RECOVERY_ELIGIBLE`, `state_reset=false`.** Das Windows-Kernel-General-Ereignis 12 mit Record-ID 94975 meldet den Start am 21.11.2026 um 02:15:31,5 UTC. CIM meldet 02:15:39,953684 UTC. Die Kernel-Uptime stimmt mit diesen Zeiten innerhalb der konservativen 15-Sekunden-Toleranz überein. Der gültige State vom 08.11.2026 enthält fünf Slots, `in_flight=false` und den Lock-Marker `1`. Das sichere Zeitfenster war bereits abgelaufen. Der lesend bestätigte SHA-256 lautet `263e0db223245fe4255f7aeab2d28f4c815e9aaea4bedf51ad3bacafb0e80af3`.

## Verhalten

- `--windows-reboot` liest State, Lock-Marker und unabhängige lokale Boot-Evidenz. Das Ergebnis ist eine Vorprüfung. Der Dry-run beansprucht keine exklusive Sperre und prüft, ob sich der gelesene State während der Diagnose geändert hat.
- `--windows-reboot --apply` erwirbt die vorhandene Sperre mit dem gleichen nativen Lock-Primitiv und prüft alles erneut. Eine rückwärts verstellte Wanduhr, fehlender oder beschädigter State, falscher Marker, In-flight-Status, konkurrierender Lock-Owner oder widersprüchliche Boot-Belege blockieren. Die jüngste alte Beobachtung muss mit Sicherheitsabstand vor dem Boot liegen. Kernel-Uptime, konservative Boot- und letzte Request-Zeit müssen jeweils mindestens das bestehende 60,5-Sekunden-Fenster abdecken.
- Unter der Sperre wird ein unveränderlicher `PREPARED`-Receipt neben dem State atomar veröffentlicht. Er enthält die exakten alten Bytes als Base64, ihren SHA-256, den Lock-Marker, die Boot-Evidenz und den erwarteten neuen State-Hash. Erst danach ersetzt die bestehende atomare State-Speicherung den Zustand. Nach der Hashprüfung wird ein gebundener `COMMITTED`-Receipt separat atomar veröffentlicht. Bei einem Abbruch zwischen beiden Receipts bleibt die alte Evidenz erhalten. Dann zuerst die vorhandenen Bytes gegen die beiden Hashes im `PREPARED`-Receipt vergleichen.
- Der normale Provider-Pfad und der bestehende manuelle Befehl für gewöhnliche Uhrfehler bleiben unverändert. Der Research-Aufruf nutzt weiterhin denselben dauerhaft gespeicherten Fünf-Slots-Limiter.

Vier synthetische Offline-Tests decken Neustart und Receipts, fehlende oder unklare Neustart-Evidenz, noch laufendes Zeitfenster, Wall-Clock-Rollback, State-/Markerfehler, In-flight und konkurrierenden Lock ab. Ruff ist grün. Der echte Zustand wurde ausschließlich im Dry-run geprüft. Es gab keine Provider-Aufrufe und keine Datenbankänderungen. Die Änderung betrifft das Code-Inventar. Neuer Candidate-Fingerprint `6bce687f9aa1e63728cb2189474efe37fe5f2b43895512c39aed5df5277e2a8d`. Historisch gebundene Fingerprints bleiben als Referenz erhalten.

## Manuelle PowerShell-Aufrufe

Dry-run unter demselben Windows-Benutzer wie die spätere Massive-Probe:

```powershell
Set-Location 'G:\GitHub\TraWalp'
& .\.venv\Scripts\python.exe -B -m trading_system.cli recover-massive-rate-limit --windows-reboot
```

Separate lokale Freigabe nach `REBOOT_RECOVERY_ELIGIBLE`:

```powershell
& .\.venv\Scripts\python.exe -B -m trading_system.cli recover-massive-rate-limit --windows-reboot --apply
```

Eigenständige Netzwerkautorisierung nach erfolgreicher Recovery:

```powershell
$Run = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$Out = Join-Path 'G:\TraWalpResearch\import-artifacts' "f01-market-$Run-massive.json"
& .\.venv\Scripts\python.exe -B -X utf8 .\reports\f01_historical_market_provider_probe.py --probe massive --authorize-provider-network --credentials-file .\.env --output $Out
```

