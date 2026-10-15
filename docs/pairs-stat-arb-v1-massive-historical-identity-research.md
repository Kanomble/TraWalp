# Pairs V1: Massive historical identity research

Stand: 2026-10-15. Ergebnis **C — Massive is useful only as corroborating evidence.**
Dies ist die konservative Bewertung des belegten Vertrags und lokalen Bestands,
keine abgeschlossene empirische Validierung historischer Massive-Antworten.
Für keinen Sample ist eine verifizierte Epoche nachgewiesen. Keine automatische
Promotion, kein produktiver Ingest, kein Pairs-Preflight.

## Untersuchungsgrenze und reproduzierbare Evidenz

Die [Probe-Datei](research/massive-historical-identity-probes-2026-10-15.json)
enthält aktuelle lokale Referenzen, gespeicherte Alpaca-Events, alle 28 vorgesehenen
Probe-Fälle und deren lokale Ablehnung. **HTTP-Requests: 0; HTTP-Responses: 0.**
Keine leere Event-Antwort, kein 401/403 und kein Plan-Resultat wurde beobachtet.

Ein erster Sandbox-Aufruf scheiterte vor Transport mit
`MASSIVE_RATE_LIMIT_STATE_UNAVAILABLE`. Die freigegebene Ausführung mit Zugriff auf
den bestehenden State scheiterte ebenfalls vor Transport, nun mit
`MASSIVE_RATE_LIMIT_CLOCK_ANOMALY`. Die nur lesende Diagnose ergab:

- verstrichene Wall-Zeit: 113276,954 Sekunden;
- verstrichene Monotonic-Zeit: 113266,423 Sekunden;
- Differenz: 10,531 Sekunden, bestehende Toleranz: 0,5 Sekunden;
- fünf gespeicherte Reservierungen, `in_flight=false`;
- kein Reset, kein alternativer State und keine Änderung des Guards.

Die Ursache dieser Uhrdifferenz wurde nicht weiter bestimmt. Warten allein
belegt keine Wiederherstellung konsistenter Uhrhistorie. Die Entitlements bleiben
**UNKNOWN**, unabhängig vom nachfolgend beschriebenen Dokumentationsvertrag.
Eine separate geprüfte Guard-Recovery ist vor erneuten Live-Probes erforderlich.

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
| `/v3/reference/tickers/{ticker}?date=YYYY-MM-DD` | datierter Detailzustand | UNKNOWN; lokaler Guard, kein HTTP |
| `/vX/reference/tickers/{id}/events?types=ticker_change` | experimentelle Ticker-Timeline | UNKNOWN; lokaler Guard, kein HTTP |
| `/v3/reference/tickers` mit `ticker`, `date`, `active`, `cik`, `cusip` | datierte Suche, auch inaktive Instrumente | heutiger aktiver Bestand lokal vorhanden; historische Filter nicht live geprüft |
| `/v3/reference/tickers/types` | Typenkatalog | durch bestehenden lokalen Sync verwendet; kein neuer Live-Call |
| `/vX/reference/ipos` | IPO-Ereignisse mit `listing_date`, `isin`, `us_code`, `security_type`, `last_updated` | nur Docs, nicht geprobt |

Die offiziellen Docs führen Details, Events, Ticker-Suche und IPOs als in allen
Stocks-Plänen enthalten; Basic-Historie jeweils zwei Jahre, längere Historie ab
anderen Tarifen. Das ist **keine** Bestätigung des konfigurierten Tarifs oder
einer konkreten 2021-Abfrage. Events sind ausdrücklich experimentell, nicht als
deprecated dokumentiert. Weder Rate-Limiting durch den Provider noch 401/403
wurden hier festgestellt.
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

Reale aktuelle Antwortfelder sind nur durch den bestehenden lokalen Snapshot
belegt. Die neuen historischen Detail-/Event-Antwortfelder konnten nicht empirisch
bestätigt werden. Der Research-Client speichert bei erfolgreicher späterer Nutzung
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
| CIK | optional | Datum; historische Zuordnung ungeprobt | Issuer | nein / C |
| share-class FIGI | optional | Datum; ungeprobt | Share Class | nein, ohne Intervall / C |
| composite FIGI | optional | Datum; ungeprobt | Instrument/Markt | nein, ohne Intervall / C |
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

## Reale Samples: vorhandener Current-State

Lokaler Massive-Snapshot beobachtet 2026-10-14T11:39:42.702427+00:00;
Provider-Updates derselben Tagesreferenz. Bei allen sechs: `type=CS`, `active=1`,
`primary_exchange=XNAS`; lokale CIK und Massive-CIK stimmen aktuell überein.
Dies bestätigt keine historische oder damalige Common-Share-Kontinuität.

| Current ticker | lokale / Massive-CIK | share-class FIGI | composite FIGI | Ergebnis für historische Epoch-Nutzung |
| --- | --- | --- | --- | --- |
| CAI | 0002019410 | BBG00255V2P2 | BBG00255V2N4 | CORROBORATING_ONLY |
| FLY | 0001860160 | BBG00NGNL3Y4 | BBG00NGNL3X5 | CORROBORATING_ONLY |
| JATT | 0002112446 | NULL | NULL | INSUFFICIENT |
| ALPS | 0002025774 | NULL | NULL | INSUFFICIENT |
| BLUW | 0002050501 | NULL | NULL | INSUFFICIENT |
| DSAC | 0002082149 | NULL | NULL | INSUFFICIENT |

### Historische Probe-Punkte und Events

| Symbol | Details-Probe-Daten | Events-Probe-ID(s) | tatsächliches Resultat |
| --- | --- | --- | --- |
| CAI | 2021-06-15, 2024-01-02, 2025-08-12, 2026-10-14 | CAI | lokaler Guard, kein HTTP |
| FLY | 2021-06-15, 2024-01-02, 2025-08-12, 2026-10-14 | FLY | lokaler Guard, kein HTTP |
| JATT | 2022-06-15, 2023-03-20, 2024-01-02, 2026-10-14 | JATT, ZURA | lokaler Guard, kein HTTP |
| ALPS | 2022-06-15, 2026-10-14 | ALPS, ALPSQ | lokaler Guard, kein HTTP |
| BLUW | 2021-06-15, 2026-10-14 | BLUW, CRXT | lokaler Guard, kein HTTP |
| DSAC | 2022-06-15, 2026-10-14 | DSAC, NOTE | lokaler Guard, kein HTTP |

Für **jeden** historischen Probe-Punkt sind Resultat-Ticker, historische CIK,
beide FIGIs, Typ, Activity-/Listingstatus sowie historische Provider-Zeitstempel
UNKNOWN. Die Zahl gefundener Massive-Events, deren Datumswerte und Old-/New-Ticker
sind ebenfalls UNKNOWN, nicht null Events.

Für **alle sechs** sind gleiche konkrete Security, gleicher historischer Issuer,
Share-Class-Kontinuität und exakte Epochengrenzen **nicht bewiesen**. Es wurde
kein Reuse und kein Listing-/Delistingdatum aus Bars oder fehlenden Antworten
abgeleitet. Eine Gleichheit der beiden aktuellen CAI-/FLY-FIGIs mit historischen
FIGIs wurde nicht beobachtet und wird nicht behauptet.

## FIGI, Ticker-Reuse, Multi-Hop und Reorganisation

OpenFIGI unterscheidet globale Share-Class-IDs, marktbezogene Composite-IDs und
Venue-IDs. Ein reiner Ticker-/Namenswechsel lässt eine bestehende FIGI bestehen;
auch Delisting entfernt sie nicht. Merger/Reorganisation bedeuten nicht pauschal,
dass alle beteiligten oder neu ausgegebenen Instrumente dieselbe FIGI hätten.
Das sind Allokationsregeln, keine empirische Bestätigung der Massive-Samples.
[FIGI-Regeln](https://www.openfigi.com/about/allocation-rules).

Massive könnte Reuse durch verschiedene historische CIKs/FIGIs an datierten
Snapshots sichtbar machen. Das ist ein prüfbarer Kandidat, hier **nicht real
validiert**. Ein heutiger Ticker-Events-Request entdeckt nicht automatisch alle
früheren Issuer dieses Strings. Fehlende IDs oder uneindeutige Zuordnungen
bleiben fail-closed; identischer Name oder CIK alleine genügt nicht.

Eine Timeline könnte mehrere Ticker als Kandidaten für `AAA → BBB → CCC` liefern.
Für eine belastbare Rekonstruktion fehlen aber pro Hop bewiesene Issuer-/Class-
Kontinuität, vollständige Grenzen und Abdeckung. SPAC-Kombination, Merger,
neue Share Class und Security-Replacement lassen sich aus `type=ticker_change`
allein nicht unterscheiden. TraWalp setzt weiterhin keine Multi-Hop-Ketten zusammen.

## Kombination mit vorhandenen Alpaca-Events

Nur lokal gespeicherte Events wurden gelesen, Alpaca wurde nicht neu abgefragt.

| Old → New | Alpaca `process_date` | CUSIP vorher/nachher | Provider-Action-ID | Massive-Abgleich |
| --- | --- | --- | --- | --- |
| JATT → ZURA | 2023-03-21 | G9TY5A101 / G9TY5A101 | 43df2d22-e18c-48d9-aeff-ccb797d742e1 | UNKNOWN, beide Timeline-Probes blockiert |
| ALPS → ALPSQ | 2023-07-21 | 021009105 / 021009105 | a091598c-74e6-4db9-af64-f1b9bc0b4bc1 | UNKNOWN, beide Timeline-Probes blockiert |
| BLUW → CRXT | 2021-09-21 | 18271L107 / 18271L107 | 88107c3f-3a53-42f4-8601-81f739e79a7a | UNKNOWN, beide Timeline-Probes blockiert |
| DSAC → NOTE | 2022-08-01 | 337655104 / 337655104 | d10b9584-27db-4978-8b67-23fd0a8083a6 | UNKNOWN, beide Timeline-Probes blockiert |

Die Werte sind Evidence-Records, keine Bindings an die heutigen sechs Securities.
Es ist weder gleicher Termin noch abweichender Termin noch ein Widerspruch
zwischen Providern festgestellt. `process_date` bleibt **kein** `effective_date`.
Selbst ein später beobachteter identischer Eventtag würde nur corroborieren:
weiterhin nötig wären eine geprüfte CUSIP↔FIGI↔CIK-/Share-Class-Bridge, vollständige
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

Zehn synthetische Research-Testfälle prüfen Request-/Feldsemantik, Credential-
Redaktion, verbotene Identifier, Access-Denial, den gemeinsamen Rate-Limit-Budget,
Retry-After und Fail-closed bei Guard-Fehlern. Mit einem vorhandenen gezielten
Rolling-Limit-Test wurden **11 Fälle erfolgreich ausgeführt**. Ruff-Lint,
Ruff-Formatprüfung und `git diff --check` gehören zur lokalen Patch-Prüfung.
Kein Test verwendet einen echten Key oder Netzwerkzugriff. Die dokumentierten
Provider-Schemas wurden über die offiziellen Dokumentationsseiten geprüft.
Produktive SQLite-Zugriffe waren
read-only; Epoch-Rowcount vor/nach der Probe jeweils **0**. Es gab weder
historischen Preflight noch Backtest, Sync oder vollständigen pytest-Lauf.

## Empfehlung

**C — Massive is useful only as corroborating evidence.** A/B ist durch die
hier belegte Evidenz nicht gedeckt. D wäre eine unbelegte Aussage über den
Account-Plan: historische Entitlements sind wegen des lokalen Guards unbekannt.
Auch ein späterer erfolgreicher Request allein würde diese Einstufung nicht
automatisch aufheben. Kein produktiver Massive-Lifecycle-Ingestion-Patch empfohlen.

Als nächste externe Quelle gezielt **algoseek US Equities Security Master und
SecId Chains Lookup** untersuchen. Die offiziellen Endpoint-Cards nennen
historische/delistete Securities, Identifier-Querverweise und Symbolketten als
eigenen Datenbereich. Zu prüfen sind exakte effektive Intervalle, CIK-/Share-Class-
Bridge, Reuse/Replacement-Semantik und tatsächliche Lizenz-/Accessbedingungen.
Eine Kette über Security-Restructures wäre nicht automatisch dieselbe Security;
eine Erfüllung unseres Contracts wird noch nicht behauptet.
[Offizielle Endpoint-Referenz](https://algoseek.com/docs/rest-api/appendix/endpoints).
