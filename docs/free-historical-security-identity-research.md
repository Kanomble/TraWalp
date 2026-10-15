# Kostenlose historische Security-Identity-Evidenz

Stand: 2026-10-15. **Kein promotionsfähiger Epoch-Kandidat. Alle sechs heutigen
Securities bleiben `IDENTITY_EPOCH_UNKNOWN` / `KEEP_BLOCKED`.** Die Untersuchung
liefert konkrete historische Zuordnungen und zwei exakte Ereignistermine, aber
keine vollständig belegte Ticker-Membership der heutigen Common-Share-Klassen.

Maschinenlesbare Evidence-Graphs mit Quellenreferenzen, Abfrageparametern,
HTTP-Status, Filing-Facts, Identifiern und Konflikten:
[free_identity_evidence_probe.json](research/free_identity_evidence_probe.json).
Der [bestehende Epoch-Vertrag](pairs-stat-arb-v1-identity-epochs.md) bleibt unverändert.

## Kleiner reproduzierbarer Research-Pfad

```powershell
.\.venv\Scripts\python.exe -m trading_system.data.free_identity_research
```

Der explizite Research-Befehl untersucht nur die sechs Samples. Er verwendet
zwei zuvor gefundene öffentliche Nasdaq-Trader-Meldungen, maximal fünf Jobs pro
OpenFIGI-Request und die CIKs aus dem vorhandenen Massive-Artefakt. Für jede CIK
wird höchstens ein Filing samt gegebenenfalls einem Submissions-Historienblock
geladen. Falls zum frühen Untersuchungsdatum nur ein 8-K verfügbar ist, wird
ein vorhandenes späteres periodisches Filing bevorzugt: ebenfalls nur eine
Punktbeobachtung, keine Interpolation.

SQLite wird ausschließlich mit `mode=ro` geöffnet. Es gibt keine Initialisierung,
Migration, Security-Master-/Bar-/SEC-Cache-Schreibvorgänge oder Epoch-Ingestion.
Die Epoch-Tabelle enthielt vor und nach dem Lauf **0 Rows**. Der Output ist nur
eine Research-Datei, kein Produktionsinput. Die Sample-Liste ist keine Blockliste.

Nasdaq: mindestens eine Sekunde Abstand, maximal acht Requests, keine Retries
oder Redirects. OpenFIGI: ohne API-Key, höchstens fünf Jobs je Request, mindestens
3,1 Sekunden Abstand und maximal acht Requests. Die öffentliche Dokumentation
nennt an verschiedenen Stellen fünf bzw. zehn unauthentifizierte Mapping-Jobs;
der Client verwendet die kleinere Grenze. HTTP 429 beendet weitere Mapping-Jobs.
SEC: bestehender `SecClient`, konfigurierte SEC-User-Agent-Konvention, eine Sekunde
Abstand, keine automatischen Retries. Alle neuen HTTP-Aufrufe besitzen Timeouts;
Credentials und Fehler-Bodies werden nicht protokolliert.

## Tatsächlich abgefragte Quellen

| Quelle | Begrenzter Hauptlauf einschließlich FLY-Nachprüfung | Ergebnis |
| --- | --- | --- |
| Nasdaq Trader `TraderNews.aspx` | 2 GETs, zwei konkrete Meldungen | 2 × HTTP 200; zwei Stock-Übergänge und eine separat gehaltene Warrant-Tabelle |
| OpenFIGI `/v3/mapping` | 3 POSTs, 12 Jobs: sechs CUSIPs und sechs heutige Ticker | 3 × HTTP 200; vier CUSIPs ohne Mapping, zwei mit Mapping; alle sechs Ticker mit Mapping |
| SEC Submissions | 4 GETs für bisher nicht gecachte historische CIKs; sieben lokale Caches wiederverwendet | 4 × HTTP 200 |
| SEC EDGAR Archives | 12 GETs für elf CIKs, darunter ein zusätzliches FLY-10-Q | 12 × HTTP 200; zehn Filings mit extrahierten DEI-Facts, zwei ohne Inline-DEI |
| Massive | 0 neue Requests | Vorhandene [historische Probe](research/massive-historical-identity-probes-2026-10-15.json), weiterhin `CORROBORATING_ONLY` |
| Alpaca | 0 neue Requests | Vier vorhandene `security_corporate_actions`-Rows, read-only |

Zusätzlich gab es vor Erstellung des Hauptreports zwei explorative GETs derselben
Nasdaq-Seiten und einen OpenFIGI-POST mit vier CUSIPs, jeweils HTTP 200. Keine
marktweite Suche, kein bezahlter Feed, keine neue Dependency. Öffentliche
Websuchen dienten nur zum Auffinden konkreter Meldungen. Für DSAC und ALPS wurde
dabei keine passende Nasdaq-Trader-Meldung gefunden; das ist **keine exhaustive
Negativsuche**. Auch eine 404 oder ein fehlendes Mapping beweist keine Nichtexistenz.

## Nasdaq: gefundene Felder und Grenzen

### BLUW → CRXT

[ECA2021-196](https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2021-196)
betrifft einen Business-Combination-Vorgang. Die Stock-Tabelle nennt
`Current Symbol`, `New Symbol`, `Current CUSIP`, `New CUSIP`, die alten/neuen
Issue-Typen und `Marketplace Effective Date`.

- Common-Stock-Übergang: **BLUW / 09607T104 → CRXT / 18271L107**.
- Expliziter Wirksamkeitstermin: **2021-09-10**, Veröffentlichung 2021-09-09.
- Die separate BLUWW/CRXTW-Warrant-Tabelle wird nicht mit der Aktie verbunden.
- Die Meldung belegt einen Ereignispunkt. Unterschiedliche CUSIPs und eine
  Business Combination beweisen keine Fortsetzung derselben konkreten Share Class.

### JATT → ZURA

[DTN2023-7](https://www.nasdaqtrader.com/TraderNews.aspx?id=dtn2023-7)
enthält datierte Spalten für NYSE-Ticker/CUSIP bis **2023-03-20** und Nasdaq-
Ticker/CUSIP ab **2023-03-21**. Die ausdrücklich bezeichnete Ordinary-Leg lautet
**JATT / G50752107 → ZURA / G9TY5A101**. Units und Warrants werden nicht gekoppelt.
Das ist eine belegte Transfer-/Änderungsgrenze, kein vollständiges historisches
Membership-Intervall und keine Bindung an den heutigen JATT-II-Issuer.

Die Parser verwenden ausschließlich die expliziten Wirksamkeitsfelder bzw.
datierten Transfer-Spalten. Veröffentlichungsdatum, SEC-Filing-Datum und
Alpaca-`process_date` werden niemals zu `effective_start` umgedeutet. Für andere
Meldungs-/Ereignislayouts besteht keine allgemeine Parsing-Abdeckung.

## OpenFIGI: Identifier-Bridges, keine Intervalle

Verwendet wird ausschließlich das öffentliche
[Mapping-API](https://www.openfigi.com/api/documentation). Reale Felder sind
`figi`, `shareClassFIGI`, `compositeFIGI`, `ticker`, `exchCode`, `marketSector`,
`securityType` und `securityType2`. CUSIP-Jobs setzen
`includeUnlistedEquities=true`; Ticker-Jobs filtern auf `exchCode=US`.

| CUSIP | Rückgabe-Ticker | Share-Class FIGI | Composite FIGI | Bewertung |
| --- | --- | --- | --- | --- |
| 021009105 | ALPSQ | BBG001T8ZD74 | BBG012V1Q8J1 | Common Stock; Bridge zum historischen Hinweis, kein heutiges ALPS-Binding |
| 18271L107 | CRXTQ | BBG00Z6DQKF8 | BBG00Z6DQJL4 | Common Stock; kein automatischer CRXT→CRXTQ-Hop |
| 09607T104 | — | — | — | Kein Identifier gefunden |
| G50752107 | — | — | — | Kein Identifier gefunden |
| G9TY5A101 | — | — | — | Kein Identifier gefunden |
| 337655104 | — | — | — | Kein Identifier gefunden |

Alle sechs heutigen Ticker liefern Common-Stock-FIGIs (siehe Ergebnistabelle).
Nur für CAI/FLY werden diese IDs auch im vorhandenen aktuellen Massive-Datensatz
mit CIK geführt. Für die anderen vier sind die neuen Ticker-Mappings
**Identifier-Hinweise ohne unabhängige CIK-/Share-Class-Brücke**; der Research-Code
schreibt sie weder in die lokale Referenz noch in Epochs. OpenFIGI liefert hier
keine CIK, kein As-of und keine effektiven Ticker-Intervalle. Gleicher Ticker und
heutige SEC-CIK reichen nicht, um diese fehlende historische Bindung zu ersetzen.

## SEC: belegte Issuer-/TradingSymbol-Punkte

Der Parser liest explizite Inline-XBRL-Facts `dei:TradingSymbol`,
`dei:EntityCentralIndexKey`, `dei:EntityRegistrantName`, `dei:DocumentPeriodEndDate`
und `dei:DocumentType`. Eine bestätigte Bindung verlangt eine eindeutige Dokument-
CIK passend zum angefragten Issuer; die Auswertung prüft den exakten Ticker.
SEC-Submissions-Tickerlisten bleiben ausdrücklich `CURRENT_STATE_ONLY`.

| Symbol | Historisches SEC-Filing | Neueres SEC-Filing / CIK | Befund |
| --- | --- | --- | --- |
| CAI | [10-Q, 2021-04-30](https://www.sec.gov/Archives/edgar/data/1388430/000138843021000007/cai-20210331x10q.htm), CIK 0001388430, CAI | [10-Q, 2025-08-12](https://www.sec.gov/Archives/edgar/data/2019410/000201941025000012/cai-20250630.htm), 0002019410, CAI | Derselbe Ticker bei zwei verschiedenen Registranten belegt |
| FLY | [20-F, 2021-03-01](https://www.sec.gov/Archives/edgar/data/1407298/000114036121006687/brhc10020948_20f.htm), 0001407298, FLY | [10-Q, 2026-05-04](https://www.sec.gov/Archives/edgar/data/1860160/000186016026000009/fly-20260331.htm), 0001860160, FLY | Derselbe Ticker bei zwei verschiedenen Registranten belegt |
| JATT | 10-Q, 2022-05-13, 0001855644, JATT | 10-Q, 2026-05-29, 0002112446, JATT | Unterschiedliche historische/heutige Issuer belegt |
| ALPS | Kein altes Filing im begrenzten Scope | 20-F, 2026-08-06, 0002025774, ALPS | Nur heutiger Issuer-Punkt |
| BLUW | 10-Q, 2021-05-26, 0001817944; keine Inline-DEI-Facts extrahiert | 10-Q, 2026-05-15, 0002050501, BLUW | Alte CIK aus Massive/Submissions; historischer Ticker nicht durch diesen Inline-Parser bestätigt |
| DSAC | 10-Q, 2022-05-16, 0001823466, DSAC | 10-Q, 2026-05-12, 0002082149, DSAC | Unterschiedliche historische/heutige Issuer belegt |

Referenzen/Accessions aller Filings stehen im JSON. FLYs erstes abgefragtes 8-K
vom 2025-08-08 enthält keine erkannten Inline-DEI-Facts; genau ein späteres 10-Q
wurde nachgeprüft. Fehlende Inline-Facts bedeuten fehlende Parser-Evidenz, nicht
fehlende historische Existenz. Kein Filing-Datum wird als Listinggrenze benutzt.

CAI- und FLY-Ticker-Reuse durch verschiedene Registranten ist damit aus kostenlosen
SEC-Punktbelegen bestätigt, zusätzlich zu unterschiedlichen historischen/heutigen
Massive-CIKs und FIGIs. Das beweist **keine exakten vollständigen Intervalle**.
FLYs historischer Massive-Typ ist ADRC, der heutige CS: auch die Security-Type-
Differenz darf nicht in eine gemeinsame Common-Share-Historie aufgelöst werden.

## Alpaca-Abgleich und Konflikte

| Übergang | Alpaca CUSIP vorher → nachher | `process_date` | Abgleich |
| --- | --- | --- | --- |
| JATT→ZURA | G9TY5A101 → G9TY5A101 | 2023-03-21 | Nasdaq nennt vorher G50752107; historische Zuordnung ungeklärt |
| BLUW→CRXT | 18271L107 → 18271L107 | 2021-09-21 | Nasdaq nennt vorher 09607T104; historische Zuordnung ungeklärt |
| ALPS→ALPSQ | 021009105 → 021009105 | 2023-07-21 | OpenFIGI bestätigt Identifier-Hinweis ALPSQ; kein Nasdaq-Termin gefunden |
| DSAC→NOTE | 337655104 → 337655104 | 2022-08-01 | Kein CUSIP-Mapping / Nasdaq-Termin; vorhandenes Massive-NOTE-Event 2025-09-02 löst dies nicht |

JATT und BLUW erhalten konservativ `CONFLICT`, Scope
`HISTORICAL_TRANSITION_UNRECONCILED_EVENT_ALIGNMENT`: bei gleicher gerichteter
Tickerkante und gleichem resultierendem CUSIP widersprechen sich die behaupteten
Vorher-CUSIPs. Das beweist weder denselben Provider-Event noch einen Providerfehler.
Unterschiedliche Datumssemantik wird **nicht** als Datumswiderspruch gewertet.
Die historischen Kanten sind außerdem keine Bindung an die heutigen JATT-/BLUW-
Securities. Es gibt keinen belegten Konflikt der heutigen CAI-/FLY-FIGIs.

## Ergebnis je heutiger Security

Die FIGIs in dieser Tabelle sind aktuelle OpenFIGI-Ticker-Hinweise. CAI/FLY
stimmen zusätzlich exakt mit den vorhandenen Massive-CIK/FIGI-Tupeln überein.

| Symbol | Klassifikation | CIK heute | Share-Class FIGI / Composite FIGI | Effektive Grenze | Empfehlung |
| --- | --- | --- | --- | --- | --- |
| CAI | CORROBORATING_ONLY | 0002019410 | BBG00255V2P2 / BBG00255V2N4 | Keine belegte vollständige aktuelle Membership | KEEP_BLOCKED |
| FLY | CORROBORATING_ONLY | 0001860160 | BBG00NGNL3Y4 / BBG00NGNL3X5 | Keine belegte vollständige aktuelle Membership | KEEP_BLOCKED |
| JATT | CONFLICT | 0002112446 | BBG020ZK3QW8 / BBG020ZK3PR6 | 2023-03-21 nur für alte JATT→ZURA-Kante | KEEP_BLOCKED |
| ALPS | INSUFFICIENT | 0002025774 | BBG0151PS6V2 / BBG0151PS6S6 | Kein belegter Lifecycle-Termin | KEEP_BLOCKED |
| BLUW | CONFLICT | 0002050501 | BBG01THFR1T0 / BBG01THFR0W8 | 2021-09-10 nur für alte BLUW→CRXT-Kante | KEEP_BLOCKED |
| DSAC | INSUFFICIENT | 0002082149 | BBG01X61Y223 / BBG01X61Y143 | Kein belegter Lifecycle-Termin | KEEP_BLOCKED |

Für **keines** der sechs heutigen Instrumente sind vollständige Membership,
historische Bindung derselben konkreten Common-Share-Klasse und alle effektiven
Grenzen gemeinsam belegt. Heutige Issuer-Bindings sind für alle sechs vorhanden.
Historische andere Issuer sind für CAI/FLY/JATT/DSAC aus DEI-Facts bestätigt;
für BLUW corroboriert Massive eine andere alte CIK, für ALPS fehlt ein alter
Issuer-Beleg. Stable IDs sind vorhanden bzw. als aktuelle Hinweise verfügbar;
ihre Verfügbarkeit ersetzt keine zeitliche Security-Bindung.

## Klassifikations- und Quarantänevertrag

`VERIFIED_EPOCH_CANDIDATE` verlangt eine **separat geprüfte vollständige**
Security-/Ticker-Membership mit CIK, stabiler ID, Common-Stock-Typ, gültigem
Intervall, zeitbewusster Beobachtungszeit und prüfbarer Review-/Quellenreferenz.
Zusätzlich müssen ein passender Nasdaq-Wirksamkeitspunkt, eine eindeutige
CUSIP→Common-Stock-FIGI-Brücke und ein SEC-Issuer-/Ticker-Beleg zusammenpassen.
Kein Parser erzeugt `reviewed_interval` aus einer Meldung oder aus Snapshots;
im realen Report ist es immer `null`. Auch ein Kandidat bliebe bis zu einem
separaten Promotion-Task im Produktionsstatus UNKNOWN.

Ohne aktuelle CIK und unabhängig gebundene stabile Security-ID lautet das
Ergebnis `INSUFFICIENT`; brauchbare Referenz mit unvollständigem Intervall ist
`CORROBORATING_ONLY`; ungeklärte materielle Zuordnungswidersprüche sind
`CONFLICT`. Keine dieser Klassen entsperrt ein Symbol. Keine Schlussfolgerung aus
Bars, Lücken, Namen, ähnlichen Daten, CIK-Gleichheit oder fehlenden API-Ergebnissen.
Multi-Hop-Ketten bleiben ohne separat belegte Bindung jedes Hops ungeklärt.

Die bestehende Pairs-Implementierung bleibt unverändert: UNKNOWN blockiert,
verifiziert OUTSIDE ist Nonmember, verifiziert ACTIVE benötigt vollständige
epochengebundene Inputs; interne Lücken blockieren weiterhin. Eine punktuelle
Nasdaq-Grenze oder zwei SEC-/Massive-Snapshots erfüllen den Outside-Vertrag nicht.

## Tatsächlich ausgeführte Checks und nächster Schritt

**20 gezielte Tests bestanden:** Parser-/Klassifikationstests einschließlich Effective-/Publication-Date-
Trennung, CUSIP→FIGI, fehlender stabiler ID, CIK-only, Identifier-Konflikten und
fehlendem vollständigem Intervall; dazu drei bestehende Pairs-Epoch-Regressionen
für UNKNOWN, OUTSIDE/History-Anchor und ACTIVE mit internem Gap. Netzwerk ist in
den neuen Unit-Tests gesperrt. Ruff und Format-Check für alle fünf neuen Python-
Dateien bestanden; Diff-Whitespace-Check für die hinzugefügten Dateien bestanden.
Keine Strategy-Economics geändert.

**Empfehlung für jedes Sample: `KEEP_BLOCKED`.** Als weitere kostenlose
Einzelfall-Recherche wären konkrete öffentliche Exchange-Meldungen und
SEC-Transaktionsdokumente mit alten/neuen Common-Share-Identifiern nötig, die die
fehlende vollständige Membership tragen und die CUSIP-Abweichungen auflösen.
Ohne solche Belege bleibt UNKNOWN das beabsichtigte Ergebnis; keine kostenpflichtige
Quelle, kein weiterer Sync oder Preflight wird vorausgesetzt oder gestartet.
