# Pairs V1: Massive historical identity research

## Manueller Clock-Recovery-Pfad

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli recover-massive-rate-limit
```

Der Befehl benötigt keine Settings, Credentials oder Datenbank und ruft keinen
Provider auf. Er verwendet dieselbe Limiter-Instanz und denselben stabilen
OS-Lock wie normale Massive-Requests. Nur formal gültiger State mit einer
Clock-Fortschrittsabweichung ist recoverbar. Beschädigtes JSON/Schema, fehlender
initialisierter State, negative/nichtfinite oder relativ zu den aktuellen Uhren
zukünftige gespeicherte Werte bleiben fail-closed. Der Befehl ist kein allgemeiner
State-Reparaturpfad. Ein konsistenter State liefert `RECOVERY_NOT_REQUIRED`
ohne Wartezeit oder Mutation.

Bei einer recoverbaren Anomalie bleibt die komplette alte Historie zunächst
unverändert. Unter durchgehend gehaltenem Thread-/OS-Lock müssen **beide** neuen
Uhren mindestens 60,5 Sekunden konsistent fortschreiten. Kurze/unterbrochene
Sleeps verkürzen dieses Fenster nicht. Auch kumulativer Drift über 0,5 Sekunden
oder eine neue Uhrkorrektur bricht ab. Währenddessen kann kein anderer Actor
reservieren oder transportieren. Ein altes `in_flight=true` gilt als potentiell
übertragen und bekommt dieselbe vollständige Wartezeit ab Lock-Erwerb.

Erst nach dem vollständigen Expiry-Fenster wird atomar `timestamps=[]`,
`last_clock=[neue Wall-Zeit, neue Monotonic-Zeit]`, `in_flight=false` gespeichert.
Bei Unterbrechung während des Wartens bleibt der vorherige State bytegleich;
der Lock wird freigegeben, CLI-Status `RECOVERY_INTERRUPTED`, Exit 130.
Erfolg liefert `RECOVERED` mit Wartezeit und vorheriger Slot-/In-flight-Zahl,
ohne exakte Clock-Timestamps. Der normale `transmit()`-Vertrag, seine Uhren-
Toleranz und sein gemeinsames 5er-Budget sind unverändert. Recovery startet
weder Research noch einen Sync automatisch.

Stand: 2026-10-15. Ergebnis **C — Massive is useful only as corroborating evidence.**
Nach manueller Recovery wurden die ursprünglichen **28 Probes real ausgeführt**:
28 HTTP-Antworten, davon 22 × 200 und 6 × 404. Keine lokale Blockade, keine
401/402/403, keine 429/5xx und keine Retries. Die Einstufung wurde anhand dieser
Live-Evidenz neu bewertet. Historische Identitätsunterschiede sind sichtbar,
vollständige verifizierte Security-Intervalle weiterhin nicht belegt.
Keine automatische Promotion, kein produktiver Ingest, kein Pairs-Preflight.

## Untersuchungsgrenze und reproduzierbare Evidenz

Die [Probe-Datei](research/massive-historical-identity-probes-2026-10-15.json)
enthält lokale Referenzen, gespeicherte Alpaca-Events, die ursprüngliche lokale
Ablehnung unter `pre_recovery_probe` sowie die neuen Live-Beobachtungen.
`requests_attempted` zählt logische Probe-Aufrufe; `http_attempts` zählt den
Eintritt in den HTTP-Transport. `requests_transmitted` ist hier durch erhaltene
HTTP-Antworten belegt; unbestätigte Transportversuche werden separat gezählt
(hier 0). `http_responses` enthält ausschließlich Status-/Klassifikationsdaten,
keine Fehler-Bodies, Credentials oder Header.

| Endpoint | geplant / aufgerufen | übertragen / Antworten | HTTP 200 / 404 | Records | lokal blockiert | Entitlement / Rate-Limit-Fehler |
| --- | --- | --- | --- | --- | --- | --- |
| Historical Details | 18 / 18 | 18 / 18 | 14 / 4 | 14 Snapshots | 0 | 0 / 0 |
| Ticker Events | 10 / 10 | 10 / 10 | 8 / 2 | 8 Events | 0 | 0 / 0 |
| Gesamt | 28 / 28 | 28 / 28 | 22 / 6 | 22 | 0 | 0 / 0 |

Die [reale Recovery-Ausgabe](research/massive-rate-limit-recovery-2026-10-15.json)
meldet `RECOVERED`, `previous_slots=5`, `previous_in_flight=false`,
`waited_seconds=60.50059461593628`, `state_reset=true`. Sie wurde als separater
CLI-Befehl ausgeführt; erst danach starteten die Probes über denselben Guard.

### Erstaudit vor Recovery (archiviert)

Ein erster Sandbox-Aufruf scheiterte vor Transport mit
`MASSIVE_RATE_LIMIT_STATE_UNAVAILABLE`. Die freigegebene Ausführung mit Zugriff auf
den bestehenden State scheiterte ebenfalls vor Transport, nun mit
`MASSIVE_RATE_LIMIT_CLOCK_ANOMALY`. Die nur lesende Diagnose ergab:

- verstrichene Wall-Zeit: 113276,954 Sekunden;
- verstrichene Monotonic-Zeit: 113266,423 Sekunden;
- Differenz: 10,531 Sekunden, bestehende Toleranz: 0,5 Sekunden;
- fünf gespeicherte Reservierungen, `in_flight=false`;
- kein Reset, kein alternativer State und keine Änderung des Guards.

Die Ursache der ursprünglichen Uhrkorrektur wurde nicht weiter bestimmt.
Der alte persistierte Anker wurde bei Ablehnung nicht aktualisiert; deswegen
wiederholte sich die Abweichung in Folgeprozessen. Im Erstaudit waren Entitlements
UNKNOWN. Dies ist durch die obigen echten Provider-Antworten jetzt abgelöst;
der konkrete Account-Tarif wurde nicht abgefragt.

## Bestehende Integration

`massive_reference.py` verwendet ausschließlich:

- `GET /v3/reference/tickers?market=stocks&active=true...`, sequenzielle Pagination;
- `GET /v3/reference/tickers/types?asset_class=stocks&locale=us`.

Der Contract heißt `STOCKS_BASIC_FREE_COMPATIBLE`, `paid_features_allowed=False`.
Dies ist eine lokale Nutzungsbeschränkung, keine Abfrage des tatsächlichen
Account-Tarifs. Die vorhandenen aktuellen Referenzen belegen einen früheren
erfolgreichen Ingest; sie bestätigen keine heutigen historischen Entitlements.
Die normale Allowlist erlaubt weder Details, Events, historische `date`-Filter
noch inaktive Tickers. Sie wurde nicht verändert.

`load_settings()` lädt die projektbezogene `.env` ohne bestehende Environment-
Werte zu überschreiben. Der explizite Reference-Sync liest anschließend
`MASSIVE_API_KEY` aus dem Environment und übergibt ihn an den Client; der Key ist
kein Settings-/Report-Feld. Der Research-Aufruf verwendet denselben Ablauf.

Gemeinsamer Guard: maximal fünf HTTP-Versuche je rollendem 60,5-Sekunden-Fenster,
persistente Reservierungen unter `AppData/Local/TraWalp/state`, Thread-/Prozess-Lock,
Reservierung vor Transport, konservative In-flight-Recovery und Uhrenprüfung.
Jeder Retry verbraucht ebenfalls einen Slot. Session ohne Environment-Proxies,
Bearer-Header, HTTPS nur zu `api.massive.com`, keine Redirects/automatischen
Requests-Retries, Connect-/Read-Timeout `(10, 30)`. Höchstens vier Versuche bei
Netzwerkfehlern, 429 oder 5xx; Backoff 2/4/8 Sekunden, bei HTTP mindestens
`Retry-After`. 401/402/403 werden ohne Retry gemeldet.

Die Current-State-Tabelle `security_reference` speichert `symbol`, CIK,
`share_class_figi`, `composite_figi`, normalisierten `security_type`, ursprünglichen
`provider_security_type`, Markt/Locale, `active`, Börse und `provider_updated_at`
aus `last_updated_utc`. Hinzu kommen lokale `observed_at`, Snapshot-ID und
Fingerprint. Der aktive Snapshot ersetzt den vorherigen Referenzbestand; dies
ist keine historische Ticker-/Security-Membership-Tabelle. Provider-Updatezeit
und Beobachtungszeit sind keine Epochengrenzen.

## Endpoints, Felder und Access

| Endpoint | Dokumentierter Zweck / Felder | Access im konfigurierten Account |
| --- | --- | --- |
| `/v3/reference/tickers/{ticker}?date=YYYY-MM-DD` | datierter Detailzustand | erreichbar; 14 × 200, 4 × 404; auch 2021 liefert Daten |
| `/vX/reference/tickers/{id}/events?types=ticker_change` | experimentelle Ticker-Timeline | erreichbar; 8 × 200, 2 × 404 |
| `/v3/reference/tickers` mit `ticker`, `date`, `active`, `cik`, `cusip` | datierte Suche, auch inaktive Instrumente | heutiger aktiver Bestand lokal vorhanden; historische Filter nicht live geprüft |
| `/v3/reference/tickers/types` | Typenkatalog | durch bestehenden lokalen Sync verwendet; kein neuer Live-Call |
| `/vX/reference/ipos` | IPO-Ereignisse mit `listing_date`, `isin`, `us_code`, `security_type`, `last_updated` | nur Docs, nicht geprobt |

Die offiziellen Docs führen Details, Events, Ticker-Suche und IPOs als in allen
Stocks-Plänen enthalten; Basic-Historie jeweils zwei Jahre, längere Historie ab
anderen Tarifen. Das ist **keine** Bestätigung des konfigurierten Tarifs oder
anderer historischer Zeiträume. Die konkreten 2021-Probes für CAI, FLY und BLUW
waren erfolgreich; daraus folgt kein allgemeiner Unlimited-History-Contract.
Events sind experimentell, nicht als deprecated dokumentiert. Provider-Rate-
Limiting oder 401/402/403 wurden im Live-Lauf nicht festgestellt. Ein 404
ist `ENDPOINT_OR_RECORD_UNAVAILABLE`, kein Entitlement-Fehler und kein Beweis
historischer Nicht-Existenz; der jeweilige Endpoint ist durch andere 200er erreichbar.
[Details](https://massive.com/docs/rest/stocks/tickers/ticker-overview),
[Events](https://massive.com/docs/rest/stocks/corporate-actions/ticker-events),
[Ticker-Suche](https://massive.com/docs/rest/stocks/tickers/all-tickers),
[IPOs](https://massive.com/docs/rest/stocks/corporate-actions/ipos).

Details dokumentiert `ticker`, `cik`, `share_class_figi`, `composite_figi`, `type`,
`active`, `primary_exchange`, `list_date`, `delisted_utc`, Markt/Locale. Viele
Identitätsfelder sind optional. Ein `last_updated_utc` ist im Detail-Schema nicht
dokumentiert, aber im Suchschema; fehlt ein Feld, bleibt es unbekannt.
`delisted_utc` beschreibt den letzten Handelstag, nicht automatisch ein exklusives
Ende der Symbolzuordnung. `list_date` beschreibt die erste öffentliche Listung,
nicht jeden späteren Tickerwechsel.
[Detailfelder](https://massive.com/docs/rest/stocks/tickers/ticker-overview).

Reale historische/current Details sowie Events sind jetzt im Probe-JSON belegt.
Alle 14 erfolgreichen Details liefern `ticker`, `cik`, `type`, `active`,
Markt/Locale und Börse. FIGIs und `list_date` sind nicht durchgehend vorhanden;
`delisted_utc`, `last_updated_utc` und CUSIP fehlen in den ausgewählten realen
Detail-Resultaten. Alle acht erfolgreichen Event-Antworten enthalten genau einen
`ticker_change`-Eintrag mit Datum und Ticker, ohne CIK-/Class-ID im Event.
Der unveränderte Research-Client speichert
eine Auswahl unveränderter Identitätsfelder, vorhandene Feldnamen, Request-ID und
Response-Hash; keine Fehler-Bodies, Header oder Credentials.

## Evidence-Matrix

Bewertung relativ zum **vollständigen Epoch-Contract**, nicht zur allgemeinen
Qualität des Providers. `C` = `CORROBORATING_ONLY`, `U` = `UNKNOWN`.
`AUTHORITATIVE` wird hier für keinen vollständigen Intervallnachweis vergeben.
`CURRENT_STATE_ONLY` gilt für die bereits lokale aktive Referenz.
PIT bezeichnet unten einen datierten Zustand; historisch damalige
Informationsverfügbarkeit ist damit nicht garantiert.

### Historical Ticker Details

| Evidenz | vorhanden laut Docs? | PIT? | Security-spezifisch? | ausreichend für Epoch? / Klasse |
| --- | --- | --- | --- | --- |
| ticker | ja | Datum | allein nein | nein / C |
| CIK | real vorhanden | Datum; historische Unterschiede beobachtet | Issuer | nein / C |
| share-class FIGI | real teilweise vorhanden | Datum; CAI/FLY unterschieden | Share Class | nein, ohne Intervall / C |
| composite FIGI | real teilweise vorhanden | Datum; CAI/FLY unterschieden | Instrument/Markt | nein, ohne Intervall / C |
| CUSIP | kein dokumentiertes Resultatfeld | U | U | nein / U |
| effective date | kein Membership-Intervallfeld | U | U | nein / U |
| list date | `list_date` optional | datierter Inhalt | Zuordnung separat prüfen | nein / C |
| delist date | `delisted_utc` optional | datierter Inhalt | Zuordnung separat prüfen | nein / C |
| security type | `type` optional | Datum | Klassifikation | nein / C |
| ticker-change event | nein | U | U | nein / U |
| issuer continuity | kein eigener Nachweis | nur Einzelzustand | Issuer | nein / C |
| share-class continuity | kein eigener Nachweis | nur Einzelzustand | FIGI als Anhaltspunkt | nein / C |

### Ticker Events

| Evidenz | vorhanden laut Docs? | PIT? | Security-spezifisch? | ausreichend für Epoch? / Klasse |
| --- | --- | --- | --- | --- |
| ticker | `ticker_change.ticker` | Event-Datum | unter angefragter Entity | nein / C |
| CIK | kein Eventfeld | U | U | nein / U |
| share-class FIGI | kein Eventfeld | U | U | nein / U |
| composite FIGI | als Request-ID möglich; kein Eventfeld | nicht pro Event | Kontext | nein / C |
| CUSIP | als Request-ID möglich; kein Eventfeld | nicht pro Event | Kontext | nein / C |
| effective date | `events[].date`, kein expliziter Epoch-Vertrag | Ereignistag | unvollständiger Kontext | nein / C |
| list date | kein eigener Eventtyp | U | U | nein / U |
| delist date | kein eigener Eventtyp | U | U | nein / U |
| security type | kein Eventfeld | U | U | nein / U |
| ticker-change event | `type=ticker_change` | Ereignistag | Entity-Timeline | nein / C |
| issuer continuity | keine CIK vor/nach Event | U | U | nein / U |
| share-class continuity | keine Class-ID vor/nach Event | U | U | nein / U |

Das dokumentierte Event enthält `date`, `type`, `ticker_change.ticker`, aber kein
explizites `old_symbol/new_symbol`-Paar, keine Action-ID und keinen Merger-/SPAC-
Klassifikator. Das alte Symbol aus dem vorherigen Timeline-Eintrag abzuleiten wäre
bereits eine zusätzliche Interpretation. Für einen Ticker wird die **heutige**
Entity adressiert. Frühere Entity-Timelines benötigen zuerst deren historischen
Identifier, der beispielsweise aus datierten Details stammen könnte.
[Event-Vertrag](https://massive.com/docs/rest/stocks/corporate-actions/ticker-events).

### Ergänzende Referenz-Endpoints

| Evidenz | datierte Ticker-Suche | IPOs | Epoch-Bewertung |
| --- | --- | --- | --- |
| ticker | ja, Datum | ja, Listing-Event | C |
| CIK | optional | kein dokumentiertes Feld | C / U |
| share-class FIGI | optional | kein dokumentiertes Feld | C / U |
| composite FIGI | optional | kein dokumentiertes Feld | C / U |
| CUSIP | Suchfilter, nicht zurückgegeben | `us_code`, nicht pauschal gleichsetzen | C |
| effective date | kein Intervall | `listing_date`, kein ganzes Intervall | U / C |
| list date | kein dokumentiertes Feld | `listing_date` | U / C |
| delist date | optional `delisted_utc` | kein dokumentiertes Feld | C / U |
| security type | optional `type` | `security_type` | C |
| ticker-change event | nein | nein | U |
| issuer continuity | Einzelzustände | nicht belegt | C / U |
| share-class continuity | Einzelzustände | nicht belegt | C / U |

Die Ticker-Suche gibt CUSIPs laut Docs aus rechtlichen Gründen nicht zurück.
Ein IPO-Datensatz kann einen Startpunkt corroborieren, ersetzt aber weder die
CIK-/Class-Bindung noch spätere Wechsel-/Endgrenzen. Splits und Dividenden wären
Corporate-Action-Kontext, kein vollständiger Security-Master; sie wurden nicht
zusätzlich abgefragt.

## Snapshot, Intervall und Informationszeit

**Eigene Contract-Bewertung:** Zwei gleiche Snapshots beweisen keinen lückenlosen
Zustand dazwischen. Ein fehlender/404-Snapshot beweist weder Nicht-Existenz noch
eine exakte Listungsgrenze. Ein Datum in einer Event-Timeline beweist ohne
zugehörigen Scope-/Kontinuitätsvertrag kein vollständiges `[start,end)` der
konkreten heutigen Security. `active=true` am letzten Snapshot erlaubt keine
unbefristete Fortschreibung. `delisted_utc + 1 Kalendertag` wird nicht zu einem
exklusiven Epoch-Ende umgerechnet.

Zusätzlich ist `date=` kein garantierter Knowledge-PIT-Schnitt: Die Details-Docs
ordnen SEC-Daten dem Berichtsperiodendatum zu, auch bei späterer Einreichung.
Das betrifft einen anderen Zeitbegriff als den Identity-Gültigkeitszeitraum und
muss bei einer späteren PIT-Nutzung separat geprüft werden.
[Datumssemantik](https://massive.com/docs/rest/stocks/tickers/ticker-overview).

Für den bestehenden Contract fehlen somit ein vollständiger temporal gültiger
Security-/Ticker-Mapping-Nachweis, sichere CIK-/Share-Class-Bindings pro Segment,
exakte Start-/Endsemantik und eine Abgrenzung von Security-Replacement und
bloßem Rename. Kein `SymbolIdentityEpoch(verified=True)` wird daraus erzeugt.

## Reale Samples: aktuelle Details und historischer Vergleich

Die sechs Live-Details zum Abfragedatum 2026-10-14 stimmen hinsichtlich CIK/FIGIs
mit dem lokalen Snapshot vom 2026-10-14T11:39:42.702427+00:00 überein.
Bei allen sechs: `type=CS`, `active=true`, `primary_exchange=XNAS`.
Dies bestätigt keine historische Common-Share-Kontinuität.

| Current ticker | lokale / Massive-CIK | share-class FIGI | composite FIGI | Ergebnis für historische Epoch-Nutzung |
| --- | --- | --- | --- | --- |
| CAI | 0002019410 | BBG00255V2P2 | BBG00255V2N4 | CORROBORATING_ONLY |
| FLY | 0001860160 | BBG00NGNL3Y4 | BBG00NGNL3X5 | CORROBORATING_ONLY |
| JATT | 0002112446 | NULL | NULL | INSUFFICIENT |
| ALPS | 0002025774 | NULL | NULL | INSUFFICIENT |
| BLUW | 0002050501 | NULL | NULL | INSUFFICIENT |
| DSAC | 0002082149 | NULL | NULL | INSUFFICIENT |

### Historische Details (zusätzlich zu den sechs aktuellen Snapshots)

`—` bedeutet fehlend/unbekannt, nicht bewiesene Nicht-Existenz. Alle erfolgreichen
Resultate enthalten den angefragten Ticker und `active=true`; 404 liefert keinen
Identitätsdatensatz. Die in dieser Tabelle wiedergegebenen `list_date`-Werte
stammen direkt vom Provider und werden nicht als Epochengrenzen übernommen.

| Ticker | Abfragedatum | HTTP | CIK | Share-Class FIGI | Composite FIGI | Typ / Börse | `list_date` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CAI | 2021-06-15 | 200 | 0001388430 | BBG001SQBPD7 | BBG00JYJ6VB2 | CS / XNYS | — |
| CAI | 2024-01-02 | 404 | — | — | — | — | — |
| CAI | 2025-08-12 | 200 | 0002019410 | BBG00255V2P2 | BBG00255V2N4 | CS / XNAS | 2025-06-18 |
| FLY | 2021-06-15 | 200 | 0001407298 | BBG001SPTMR2 | BBG00JYGRW87 | ADRC / XNYS | — |
| FLY | 2024-01-02 | 404 | — | — | — | — | — |
| FLY | 2025-08-12 | 200 | 0001860160 | BBG00NGNL3Y4 | BBG00NGNL3X5 | CS / XNAS | 2025-08-07 |
| JATT | 2022-06-15 | 200 | 0001855644 | — | — | CS / XNYS | 2021-09-03 |
| JATT | 2023-03-20 | 200 | 0001855644 | — | — | CS / XNYS | 2021-09-03 |
| JATT | 2024-01-02 | 404 | — | — | — | — | — |
| ALPS | 2022-06-15 | 404 | — | — | — | — | — |
| BLUW | 2021-06-15 | 200 | 0001817944 | — | — | CS / XNAS | — |
| DSAC | 2022-06-15 | 200 | 0001823466 | — | — | CS / XNAS | 2020-11-30 |

CAI und FLY besitzen 2021 andere CIKs **und** andere FIGIs als 2025/2026;
FLY zusätzlich einen anderen Typ (ADRC gegenüber heutiger CS). Das belegt
unterschiedliche datierte Provider-Zuordnungen, keine lückenlose Übergangshistorie.
Die aktuellen CAI-/FLY-Identifier stimmen dagegen zwischen den beiden abgefragten
2025-/2026-Snapshots überein; dies beweist nicht das ganze Intervall dazwischen.
JATT, BLUW und DSAC zeigen historische CIKs, die von den heutigen abweichen;
ihre konkrete Share-Class-Identität bleibt mangels FIGIs unresolved.
Für ALPS liefert der untersuchte historische Punkt nur 404.

### Reale Event-Antworten

| Request-ID | HTTP | erhaltene Events | Event-Datum | `ticker_change.ticker` |
| --- | --- | --- | --- | --- |
| CAI | 200 | 1 | 2025-06-18 | CAI |
| FLY | 200 | 1 | 2025-08-07 | FLY |
| JATT | 200 | 1 | 2026-04-17 | JATT |
| ALPS | 200 | 1 | 2025-10-31 | ALPS |
| BLUW | 200 | 1 | 2025-07-31 | BLUW |
| DSAC | 200 | 1 | 2026-01-29 | DSAC |
| ZURA | 200 | 1 | 2023-03-21 | ZURA |
| ALPSQ | 404 | kein Resultat | — | — |
| CRXT | 404 | kein Resultat | — | — |
| NOTE | 200 | 1 | 2025-09-02 | NOTE |

Bei den sechs heutigen Tickers stimmt der Eventtag mit dem `list_date` der
aktuellen Details überein. Das sind zwei passende Provider-Angaben, aber keine
unabhängigen Quellen und keine ausdrückliche Security-/Issuer-Kontinuitätszusage.
Die Events enthalten weder altes Symbol noch CIK, CUSIP oder Class-FIGI.
Bei **allen sechs** bleiben vollständige Epochengrenzen unbelegt; kein
`VERIFIED_EPOCH_POSSIBLE`. Bars oder Bar-Lücken wurden nicht zur Identitätsevidenz.

## FIGI, Ticker-Reuse, Multi-Hop und Reorganisation

OpenFIGI unterscheidet globale Share-Class-IDs, marktbezogene Composite-IDs und
Venue-IDs. Ein reiner Ticker-/Namenswechsel lässt eine bestehende FIGI bestehen;
auch Delisting entfernt sie nicht. Merger/Reorganisation bedeuten nicht pauschal,
dass alle beteiligten oder neu ausgegebenen Instrumente dieselbe FIGI hätten.
Das sind Allokationsregeln, keine empirische Bestätigung der Massive-Samples.
[FIGI-Regeln](https://www.openfigi.com/about/allocation-rules).

Massive unterscheidet in diesen echten Snapshots frühere und heutige
CIK-/FIGI-Zuordnungen für CAI und FLY. Reuse lässt sich damit auf der Ebene
datierter Identitäten untersuchen, aber die Grenzen sind nicht vollständig
bestimmt. Für JATT/BLUW/DSAC ist nur der CIK-Unterschied belegt. Ein heutiger
Ticker-Events-Request entdeckt nicht automatisch alle früheren Issuer dieses
Strings: JATT liefert beispielsweise nur den 2026-Eintrag, nicht die alte
JATT→ZURA-Geschichte. Fehlende IDs oder uneindeutige Zuordnungen bleiben
fail-closed; identischer Name oder CIK alleine genügt nicht.

Keine der acht erfolgreichen Probes liefert mehr als einen Timeline-Eintrag.
Das ist ein Sample-Befund, keine allgemeine Garantie der API. Eine vollständige
`AAA → BBB → CCC`-Kette wurde nicht beobachtet. Für eine belastbare Rekonstruktion
fehlen pro Hop bewiesene Issuer-/Class-
Kontinuität, vollständige Grenzen und Abdeckung. SPAC-Kombination, Merger,
neue Share Class und Security-Replacement lassen sich aus `type=ticker_change`
allein nicht unterscheiden. TraWalp setzt weiterhin keine Multi-Hop-Ketten zusammen.

## Kombination mit vorhandenen Alpaca-Events

Nur lokal gespeicherte Events wurden gelesen, Alpaca wurde nicht neu abgefragt.

| Old → New | Alpaca `process_date` | CUSIP vorher/nachher | Provider-Action-ID | Massive-Abgleich |
| --- | --- | --- | --- | --- |
| JATT → ZURA | 2023-03-21 | G9TY5A101 / G9TY5A101 | 43df2d22-e18c-48d9-aeff-ccb797d742e1 | ZURA: gleicher neuer Ticker und Kalendertag; altes Symbol nicht im Massive-Event |
| ALPS → ALPSQ | 2023-07-21 | 021009105 / 021009105 | a091598c-74e6-4db9-af64-f1b9bc0b4bc1 | ALPSQ: 404; heutiges ALPS nur Event 2025-10-31 |
| BLUW → CRXT | 2021-09-21 | 18271L107 / 18271L107 | 88107c3f-3a53-42f4-8601-81f739e79a7a | CRXT: 404; heutiges BLUW nur Event 2025-07-31 |
| DSAC → NOTE | 2022-08-01 | 337655104 / 337655104 | d10b9584-27db-4978-8b67-23fd0a8083a6 | NOTE: Event 2025-09-02, kein Abgleich zum 2022-Ereignis möglich |

Die Werte sind Evidence-Records, keine Bindings an die heutigen sechs Securities.
Bei ZURA corroboriert der gleiche Kalendertag den neuen Ticker. Bei NOTE liegt
ein anderer datierter Eintrag vor; ohne historische Entity-/Security-Bindung
ist weder dieselbe Corporate Action noch ein Provider-Widerspruch bewiesen.
404 widerlegt keine historische Alpaca-Action. `process_date` bleibt **kein**
`effective_date`. Weiterhin nötig sind eine geprüfte CUSIP↔FIGI↔CIK-/Share-Class-Bridge, vollständige
Membership-Intervalle und Regeln für Replacement/Reuse. Current-State plus
Alpaca-NameChange schließt diesen Vertrag nicht von selbst.

## Umsetzung und Checks

`MassiveIdentityResearchClient` ist ein isolierter read-only Probe-Client mit
zwei Methoden: `historical_ticker_details(ticker, date)` und `ticker_events(id)`.
Er verwendet die vorhandene Session-/Credential-Initialisierung, denselben
persistenten Limiter, dieselben Timeouts und begrenzten Retry-Regeln. Exakte
Identifier verhindern zusätzliche Pfade/Query-Parameter. Es gibt keine neue
Dependency, produktive CLI-Anbindung oder DB-Schreibfunktion. Die beiden
Research-Methoden paginieren nicht.
Die produktive Allowlist und sämtliche Strategy-Pfade bleiben unverändert.

Im Recovery-Task bestanden **55 unterschiedliche gezielte Fälle**: die Recovery-,
bestehenden persistenten Rate-Limit- und Research-Client-Tests (54 im gemeinsamen
Lauf, zusätzlich ein Test gegen kumulativen Clock-Drift während Recovery).
Geprüft wurden insbesondere unveränderte Historie während Lock/Expiry-Wait,
kein paralleler Transport, `in_flight`, beschädigte/future States, Interrupts,
weiterhin fünf Requests pro Fenster und normale Research-Fehlerbehandlung.
Ruff-Lint, Ruff-Formatprüfung und `git diff --check` ergänzen die Patch-Prüfung.
Kein Test verwendet einen echten Key oder Netzwerkzugriff. Die dokumentierten
Provider-Schemas wurden über die offiziellen Dokumentationsseiten geprüft.
Produktive SQLite-Zugriffe waren
read-only; Epoch-Rowcount vor/nach der Probe jeweils **0**. Es gab weder
historischen Preflight noch Backtest, Sync oder vollständigen pytest-Lauf.

## Empfehlung

**C — Massive is useful only as corroborating evidence.** Neu bewertet anhand
von 28 echten Provider-Antworten: historische Details sind erreichbar und zeigen
unterschiedliche Identitäten; ein Zugriffs-/Planproblem wurde nicht beobachtet.
Trotzdem fehlen vollständige Intervallzusagen, Stable-IDs für vier aktuelle
Samples sowie Security-/Share-Class-Kontinuität in allen erhaltenen Events.
Gleicher neuer Ticker/Tag bei ZURA und die CIK-/FIGI-Snapshots schließen diese
Lücken auch mit den bestehenden Alpaca-Daten nicht. Kein produktiver
Massive-Lifecycle-Ingestion-Patch empfohlen.

Als nächste externe Quelle gezielt **algoseek US Equities Security Master und
SecId Chains Lookup** untersuchen. Die offiziellen Endpoint-Cards nennen
historische/delistete Securities, Identifier-Querverweise und Symbolketten als
eigenen Datenbereich. Zu prüfen sind exakte effektive Intervalle, CIK-/Share-Class-
Bridge, Reuse/Replacement-Semantik und tatsächliche Lizenz-/Accessbedingungen.
Eine Kette über Security-Restructures wäre nicht automatisch dieselbe Security;
eine Erfüllung unseres Contracts wird noch nicht behauptet.
[Offizielle Endpoint-Referenz](https://algoseek.com/docs/rest-api/appendix/endpoints).
