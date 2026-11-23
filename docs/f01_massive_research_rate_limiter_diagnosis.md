# F01 · Massive-Limiter-Diagnose

**Ursache: `MASSIVE_RATE_LIMIT_CLOCK_ANOMALY`.** Der gültige Zustand vom 08.11.2026, 17:54:22 UTC, enthält fünf belegte Slots, `in_flight=false` und Lock-Marker `1`. Windows startete am 21.11.2026 um 03:15:39 Ortszeit neu. Bei der lesenden Prüfung lag der alte monotone Zähler bei 862629,656 s, der neue bei 231808,812 s. Der alte Wert lag rund 7,3 Tage vor dem aktuellen Zähler. Der vorhandene Guard verweigert diese Konstellation vor der Reservierung und dem HTTP-Aufruf.

Beide Dateien unter `%USERPROFILE%\AppData\Local\TraWalp\state` waren mit Lesefreigabe zugänglich. Die Schema- und Reihenfolgeprüfung des vorhandenen Limiters bestand. Der eigene Sandbox-Zugriffsfehler trat separat auf. Weder eine Schreibsperre noch die Schreibberechtigung wurden am echten State getestet. Der Aufruf `_REQUEST_LIMITER.transmit(send, limit=5)` ist korrekt. Der nachgewiesene Fehler im Research-Skript war die gemeinsame Sammelmeldung für alle Limiter-Ausnahmen.

| Diagnose | Bedeutung |
| --- | --- |
| `LOCAL_RATE_LIMITER_ERROR` | Lokale Sperre oder Zustands-/Uhrfehler. `limiter_error` enthält einen bekannten fixen Code. `transport_started=false` kennzeichnet den Abbruch vor dem HTTP-Aufruf. |
| `RATE_LIMITED`, HTTP 429 | Massive hat geantwortet und gedrosselt. |
| `NETWORK_ERROR` | Der HTTP-Client meldet einen Transportfehler. |
| `AUTH_FAILED`, HTTP 401 | Massive hat die Anmeldung zurückgewiesen. `CREDENTIALS_UNAVAILABLE` bleibt die lokale Meldung für fehlende verwendbare Zugangsdaten. |

Gemeinsamer Limiter und Originaldateien blieben unverändert. Drei gezielte Offline-Tests und Ruff bestanden.

## Manueller PowerShell-Ablauf

Die bestehende Recovery verweigert die Ausführung, solange ein gespeicherter Zeitwert noch in der Zukunft liegt. Der lesende Check unten stoppt in diesem Fall. Sobald er erfolgreich ist, kann die lokale Recovery unter normaler Sperre laufen. Bei einem wiederherstellbaren Uhrfehler wartet sie mindestens 60,5 Sekunden. Den Provider-Befehl separat manuell ausführen.

```powershell
Set-Location 'G:\GitHub\TraWalp'
$Py = '.\.venv\Scripts\python.exe'

@'
import io
import time
from trading_system.data.massive_rate_limit import _RollingWindowLimiter, _state_path

path = _state_path()
with path.with_suffix('.lock').open('rb') as marker:
    state = _RollingWindowLimiter()._load(path, io.BytesIO(marker.read(1)))
wall, monotonic = time.time(), time.monotonic()
pairs = [*state['timestamps'], state['last_clock']]
ready = all(pair[0] <= wall and pair[1] <= monotonic for pair in pairs)
print('Recovery prüfbar:', ready)
print('Monotone Differenz in Sekunden:', round(max(pair[1] for pair in pairs) - monotonic, 1))
raise SystemExit(0 if ready else 2)
'@ | & $Py -B -X utf8 -
if ($LASTEXITCODE -ne 0) { throw 'Recovery-Vorbedingung offen.' }

& $Py -B -m trading_system.cli recover-massive-rate-limit
if ($LASTEXITCODE -ne 0) { throw 'Lokale Limiter-Recovery verweigert.' }
```

```powershell
# Separate manuelle Netzwerkautorisierung nach erfolgreicher Recovery
Set-Location 'G:\GitHub\TraWalp'
$Run = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$Output = "G:\TraWalpResearch\import-artifacts\f01-market-$Run-massive.json"
& .\.venv\Scripts\python.exe -B -X utf8 .\reports\f01_historical_market_provider_probe.py --probe massive --authorize-provider-network --credentials-file .\.env --output $Output
```

