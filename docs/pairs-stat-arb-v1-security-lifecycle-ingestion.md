# Pairs V1: Security-Lifecycle-Evidenz-Ingestion

Der manuelle Befehl `sync-security-lifecycle` liest Alpaca-NameChange-Ereignisse
für **1–7 ausdrücklich angegebene Symbole**, speichert rohe Evidenz und berichtet
unresolved Zuordnungen. Ohne separat geprüfte vollständige Security-Intervalle
entstehen **keine** verifizierten Epochen. Pairs-Selection und Execution wurden
in diesem Task nicht verändert.

## Provider-Capability: tatsächlich installiertes SDK und API

Untersucht: `alpaca-py 0.44.0`, `CorporateActionsClient.get_corporate_actions`,
`CorporateActionsRequest`, `CorporateActionsSet`, die Modelle und der gemeinsame
REST-/Pagination-Pfad in `.venv/Lib/site-packages/alpaca/`.

Client-Pfad: `GET https://data.alpaca.markets/v1/corporate-actions`.
Requests erlauben `symbols`, `cusips`, `types`, `start`, `end`, `ids`, `limit`,
`sort`. Start/End sind inklusive; die API dokumentiert Sortierung nach `process_date`.
`ids` ist laut Dokumentation mit anderen Filtern inkompatibel. Das SDK behandelt `limit` als
Gesamtlimit und aggregiert Seiten; danach ist `next_page_token` nicht mehr im
Rückgabewert. Der neue Adapter begrenzt Ergebnisse auf 100 standardmäßig, höchstens
1000, HTTP-Versuche auf drei, Transportzeit auf 5/15 Sekunden und deaktiviert Retries.
Bei erreichtem Ergebnislimit wird keine Promotion erlaubt. Der SDK-Parameter
`raw_data=True` funktioniert im tatsächlich installierten Code und in der Live-Probe.
Die offizielle API kann Events verzögert oder unvollständig liefern; eine
erfolgreiche Abfrage ist keine vollständige Lifecycle-Bestätigung.
[Alpaca API](https://docs.alpaca.markets/us/reference/corporateactions-1)

| SDK-Request-Typ | Modell / relevante Felder zusätzlich zu `id` und `process_date` |
| --- | --- |
| `name_change` | `NameChange`: `old_symbol`, `new_symbol`, `old_cusip`, `new_cusip` |
| `forward_split` | `ForwardSplit`: `symbol`, `cusip`, Raten, `ex_date`, optionale Record/Payable/Due-Bill-Daten |
| `reverse_split` | `ReverseSplit`: `symbol`, `old_cusip`, `new_cusip`, Raten, `ex_date`, Record/Payable |
| `unit_split` | `UnitSplit`: old/new/alternate Symbole und CUSIPs, Raten, `effective_date`, Payable |
| `stock_dividend`, `cash_dividend` | jeweilige Dividend-Modelle: `symbol`, `cusip`, `ex_date`, Record/Payable und typabhängige Zusätze |
| `spin_off` | `SpinOff`: source/new Symbole und CUSIPs, `ex_date`, Record/Payable/Due-Bill |
| `cash_merger`, `stock_merger`, `stock_and_cash_merger` | jeweilige Merger-Modelle: acquirer/acquiree Symbole und CUSIPs, `effective_date`, Payable |
| `redemption`, `worthless_removal` | jeweilige Modelle: `symbol`, `cusip`; Redemption zusätzlich Rate/Payable |
| `rights_distribution` | `RightsDistribution`: source/new Symbole und CUSIPs, `ex_date`, Record/Payable/Expiration |

Alle 13 Modelle erben die Action-UUID `id`; `corporate_action_type` wird beim
SDK-Modellparsing aus dem Response-Bucket ergänzt. Kein separater `SymbolChange`
und kein `declaration_date` in diesen SDK-Modellen. `NameChange` besitzt **kein**
`effective_date`, `ex_date`, `record_date`, `payable_date`, CIK, FIGI, Asset-ID oder
Common-Share-Klassifikationsfeld. Andere Modelle haben einige dieser Datumsfelder,
belegen aber nicht automatisch Kontinuität derselben Security. Die aktuelle API
dokumentiert zusätzliche Typen/Filter, die dieses installierte SDK nicht modelliert.
Der neue Pfad fordert ausschließlich `name_change` an, nicht den gesamten Markt.

## CUSIP und Evidenzqualität

CUSIP bezeichnet eine konkrete Issue, nicht deren CIK. Es gibt lokal keinen
aktuellen CUSIP und keine geprüfte CUSIP→FIGI/CIK-Historie. SDK-Asset-UUIDs sind
andere Identifier als Action-UUIDs und werden im bisherigen TraWalp-Assetmodell
nicht persistiert. Die aktuelle Asset-Referenz verbindet sie nicht mit diesen Events.

CGS beschreibt seit 2021-10-01 CUSIP-Permanence für reine Namensänderungen; früher
konnten Equity-Namensänderungen neue CUSIPs erhalten. Reverse Splits und neue
rechtliche Einheiten können weiterhin einen neuen Identifier benötigen. Gleiche
oder verschiedene Event-CUSIPs werden deshalb nie allein als vollständiger
Security-/Issuer-/Share-Class-Kontinuitätsbeweis verwendet.
[CGS Identifiers](https://www.cusip.com/identifiers.html?section=CUSIP),
[CGS Permanence FAQ](https://www.cusip.com/pdf/news/CUSIP-Permanence-FAQ.pdf)

| Qualität | Bewertung |
| --- | --- |
| A: ausreichend | NameChange plus unabhängig geprüfte vollständige Intervalle, CUSIP-Zuordnung, gleiche stabile konkrete Security/Share-Class und CIK, heutige Bindung widerspruchsfrei |
| B: teilweise | Vollständiges NameChange-Event mit Symbolen/CUSIPs: belegter Provider-Event, aber fehlende effektive Intervallgrenzen und heutige Security-/Issuer-/Class-Bindung |
| C: Hinweis | Unvollständiger Event oder nur heutige Namen/CIKs/FIGIs, Bars, Lücken oder einzelne SEC-Forms |

`process_date` wird gespeichert und **niemals** als `effective_start/end`
umbenannt. Ein NameChange liefert außerdem weder den vollständigen Anfang des
Old-Ticker-Intervalls noch den gesamten weiteren Verlauf des New-Tickers.

## Persistenz, Promotion und CLI

Additive Tabelle `security_corporate_actions`: `provider`, `event_key`,
`provider_action_id`, `action_type`, `old_symbol`, `new_symbol`, `old_cusip`,
`new_cusip`, `process_date`, optional tatsächlich geliefertes `effective_date`,
`observed_at`, `request_reference`, `raw_fingerprint`, `raw_payload`.
Fehlende Felder bleiben NULL. Der Raw-Payload ist das unveränderte Event-Objekt
aus dem SDK-Raw-Response, kein vollständiges HTTP-Envelope und keine Epoche.

Optional `--verified-evidence PATH`: explizit extern geprüfte Zusatzquelle als
JSON-Array. Jeder Eintrag enthält `provider="ALPACA"`, `provider_action_id`,
`old_cusip`, `new_cusip` und `epochs` mit **genau zwei** vollständigen
`SymbolIdentityEpoch`-Records (vorher/nachher). Das ist keine automatische
Dokumentenprüfung; `verified=true` ist hier die bereits extern geprüfte
Lifecycle-Aussage gemäß dem [bestehenden Vertrag](pairs-stat-arb-v1-identity-epochs.md).
Raw-Provider-JSON ist kein solcher Nachweis. Quellen ausschließlich aus Alpaca-
Corporate-Actions werden als Zusatzquelle nicht akzeptiert.

Der Resolver prüft Action-ID, Symbole und CUSIPs gegen das reale Event. Beide
Intervalle müssen verifiziert, Common-Share-spezifisch, vollständig, mit gleicher
CIK und nichtleerer gleicher stabiler Security-ID belegt sein. Old-End muss exakt
New-Start entsprechen; derselbe Ticker auf beiden Seiten gilt nicht als Symbolwechsel.
Die Nachher-Security muss zur heutigen lokalen CIK/FIGI/Common-Stock-Referenz passen,
inklusive SEC-/Massive-CIK-Konfliktprüfung und Gültigkeit am Referenz-Beobachtungstag.
Effektivgrenzen kommen ausschließlich aus der Zusatzquelle. Beide Quellen werden
in den Epoch-Referenzen dokumentiert; ohne diese Zusatzquelle ist das Ergebnis B/C.

Raw-Events sind unter Provider/Action-ID idempotent, ohne ID unter einem natürlichen
Hash-Schlüssel. Beobachtungszeit wird bei Wiederholung nicht überschrieben.
Geänderte Payloads unter demselben Schlüssel: Konflikt, Original bleibt erhalten.
Epoch-Promotion nutzt `upsert_symbol_identity_epochs(..., protect_existing=True)`:
atomarer Batch, identische Intervalle unverändert, widersprüchliche Schlüssel oder
überlappende verifizierte Intervalle erzeugen `IDENTITY_EVIDENCE_CONFLICT` und Rollback.
Bestehende manuelle/verifizierte Epochen werden nicht ersetzt oder gelöscht.
Die bisherige explizite manuelle API bleibt verfügbar; der neue Sync nutzt nur den
geschützten Modus. Bei Konflikten oder Ergebnislimit werden keine neuen Epochen promoted.

Die CLI zeigt die angeforderten/empfangenen/gespeicherten/verifizierten Counts,
Konflikte, Fehler sowie resolved/unresolved Symbole mit kurzen Reasons.
Diese Symbol-Listen bewerten die eingehende Evidenz dieses Laufs, nicht den
Verifizierungsstatus aller bereits gespeicherten Epochen.
Exit 0 bedeutet erfolgreichen Evidenz-Sync, nicht Identity-Verifizierung.
Fehler/Konflikte liefern Exit 1. Fehlgeschlagene Requests ändern die Datenbank nicht.
Keine Bar-Synchronisation, Pairs-Validation, automatische Promotion oder SEC-Abfrage.

## Andere vorhandene Quellen und Ticker-Reuse

Massive dokumentiert den experimentellen `/vX/reference/tickers/{id}/events`-
Endpoint mit `ticker_change`, datierten Tickers und Abfragen nach Ticker, CUSIP
oder Composite FIGI. Ein Ticker-Request meint die heute damit verbundene Entity;
frühere Entities benötigen ein passendes historisches Identifier-Mapping.
Datierte `/v3/reference/tickers/{ticker}`-Details liefern unter anderem CIK,
FIGIs und Referenzdaten. Diese Kombination ist ein möglicher weiterer Prüfpfad,
aber ein Snapshot allein ist kein vollständiges Lifecycle-Intervall. Die
Dokumentation begrenzt Free-Historie auf zwei Jahre; konkrete Entitlements wurden
hier nicht abgefragt. Der bisherige Massive-Allowlist-/Sync-Pfad blieb unverändert.
[Ticker Events](https://massive.com/docs/rest/stocks/corporate-actions/ticker-events),
[Ticker Overview](https://massive.com/docs/rest/stocks/tickers/ticker-overview)

SEC-Submissions/Ticker-CIK-Cache, heutige FIGIs und Alpaca-Assets können ergänzend
Bindings prüfen, liefern lokal aber keine vollständige CUSIP-/Class-/Ticker-Historie.
Ein Symbolwechsel-Event derselben Issue rekonstruiert **keine vollständige Ticker-Reuse-
Historie verschiedener Issuer**. Eine leere Antwort beweist keine Kontinuität;
alte Bars bleiben bis zur verifizierten heutigen Epoche unzugeordnet.

## Reale kleine Probes und Ingestion am 2026-10-14

Die erste Sandbox-Probe scheiterte vor einer HTTP-Antwort. Außerhalb des Netzwerk-
Sandbox liefen anschließend zwei kleine GETs erfolgreich: Samples CAI/FLY/JATT
für 2023-01-01–2026-08-19 und Kontrolle FB/META für 2022-06-08–2022-06-10,
jeweils ausschließlich NameChange und Limit 20.

| Probe | UUID | Old → New | `process_date` | `old_cusip` → `new_cusip` | `effective_date` |
| --- | --- | --- | --- | --- | --- |
| Sample | 43df2d22-e18c-48d9-aeff-ccb797d742e1 | JATT → ZURA | 2023-03-21 | G9TY5A101 → G9TY5A101 | nicht geliefert |
| Kontrolle | ed6f16b1-fc46-443e-8dba-898ed9007a7a | FB → META | 2022-06-09 | 30303M102 → 30303M102 | nicht geliefert |

Der neue CLI-Sync wurde einmal für CAI/FLY/JATT im Sample-Zeitraum mit Limit 100
ausgeführt: `symbols_requested=3`, `corporate_actions_received=1`,
`symbol_changes_received=1`, `raw_evidence_inserted=1`, `verified_epochs_created=0`,
`unverified_evidence=1`, `identity_conflicts=0`, `errors=0`, resolved `[]`,
unresolved `[CAI,FLY,JATT]`. Die produktive Datenbank enthält jetzt einen Raw-Event
und weiterhin null Epocheneinträge. Das FB/META-Kontrollereignis wurde nicht ingestiert.

| Current symbol | Current CIK | Composite FIGI / Share Class FIGI | Events im geprüften Fenster | Verified epoch möglich? / Reason |
| --- | --- | --- | --- | --- |
| CAI | 0002019410 | BBG00255V2N4 / BBG00255V2P2 | 0 NameChange-Events | Nein; keine zurückgegebene Lifecycle-Evidenz |
| FLY | 0001860160 | BBG00NGNL3X5 / BBG00NGNL3Y4 | 0 NameChange-Events | Nein; keine zurückgegebene Lifecycle-Evidenz |
| JATT | 0002112446 | NULL / NULL | 1: JATT → ZURA, siehe oben | Nein; keine Zuordnung des alten Events zur heutigen JATT-Security/CIK und keine vollständigen effektiven Intervalle |

Für CAI/FLY sind Old/New-Symbol, CUSIPs, Action-ID und Effektivdatum mangels
zurückgegebenem Event unbekannt. Für **alle drei heutigen Securities** sind
Security-, Issuer- und Share-Class-Kontinuität nicht bewiesen; Status weiterhin
`IDENTITY_EPOCH_UNKNOWN`. Die lokale heutige ZURA-CIK 0001855644 ist lediglich
Kontext: ohne historischen CUSIP-Bridge wird sie nicht dem Event-Issuer zugeordnet.
Keine Aussage über ein Listingdatum oder abschließend bewiesenen Ticker-Reuse.

Es fehlen weiterhin unabhängig geprüfte vollständige Security-Master-/Börsen-
Intervalle, historische CUSIP↔Share-Class/FIGI↔CIK-Zuordnungen und Grenzen für
Neu-Listings, Delistings und Ticker-Reuse. NameChange allein schließt diese Lücke nicht.

Ausgeführt: 16 kleine Lifecycle-Testfälle und zwei vorhandene Pairs-Epoch-Checks,
alle 18 grün; SDK-/Request-Tests verwenden synthetische Antworten. Zusätzlich
die oben genannten realen Probes, ein kleiner Drei-Symbol-Sync, lesende SQLite-
Diagnose, Ruff für die geänderten Python-Dateien und `git diff --check`.
Kein Markt-Backfill, Daily-Sync, Pairs-Preflight, Backtest oder vollständiger pytest-Lauf.
