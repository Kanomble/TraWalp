# Pairs V1: belegte Security-/Identity-Epochen

Der Pairs-Datenpfad qualifiziert historische Sessions jetzt zuerst gegen eine
quellenbelegte Epoche der heutigen Common-Share-Security. Der globale erste
Ticker-Bar ist keine Identity-Evidenz. Unbekannte Zuordnungen blockieren die
SIC2/Session vor Preis-Gate, ADV20 und Top-N-Ranking.

## Evidenzvertrag und Persistenz

`SymbolIdentityEpoch` und die additive Tabelle `symbol_identity_epochs` enthalten
Symbol, optionale stabile Security-ID und CIK, `effective_start`, `effective_end`,
Quelle, Evidenztyp, Verifizierungsflag, Beobachtungszeit und Evidenzreferenz.
Intervalle sind links geschlossen und rechts offen; `effective_end=NULL` behauptet
fortbestehende Membership. `observed_at` ist Auditzeit, kein Listingdatum und kein
Nachweis, dass die Information schon damals bekannt war.

Verifiziert werden darf ausschließlich
`AUTHORITATIVE_COMMON_STOCK_SYMBOL_LIFECYCLE`: eine extern geprüfte Quelle muss
die vollständige zeitliche Tickerzuordnung **dieser konkreten Common-Share-Klasse**
und ihre Verbindung zur heutigen Security tragen. Nur ein historischer Snapshot
oder eine begrenzte Beobachtungsperiode reicht nicht für die Outside-Entscheidung.
`source` und `evidence_reference` müssen die Prüfung nachvollziehbar machen.
Das API validiert diesen Vertrag, prüft aber keine externen Dokumentinhalte selbst.
Es existiert bewusst kein automatischer Converter von heutigen Metadaten,
Bars, Bar-Lücken, Company-Namen oder SEC-Forms zu verifizierten Intervallen.

Für `verified=True` sind eine nichtleere CIK **und** eine konkrete stabile
Security-/Share-Class-ID verpflichtend, zusätzlich zum autoritativen Lifecycle-
Evidenztyp, einer Quelle, Evidenzreferenz und timezone-aware `observed_at`.
CIK allein identifiziert den Issuer, nicht dessen konkrete Share Class.
Für Pairs müssen CIK und stabile ID exakt zur heutigen Referenz passen; FIGIs
werden als `SHARE_CLASS_FIGI:<id>` oder `COMPOSITE_FIGI:<id>` gespeichert.
Explizite `stable_security_identity` wird ebenfalls nur exakt verglichen.
Fehlt eine stabile ID in der Epoche oder heutigen Referenz, gibt es kein Match.
Überlappende verifizierte Symbolzuordnungen,
unverifizierte aktuelle Zuordnungen und mehrere passende Intervalle bleiben
unresolved. V1 verbindet auch mehrere belegte Intervalle derselben Security
absichtlich nicht automatisch.

`Database.initialize()` legt die leere Tabelle gemäß der bestehenden
Schema-Konvention an. `Database.upsert_symbol_identity_epochs()` ist die kleine
explizite Ingestion-Schnittstelle für zuvor geprüfte `SymbolIdentityEpoch`-Objekte.
Geschützte, atomare Insert-only-Ingestion ist der Default (`protect_existing=True`);
`False` wird abgelehnt. Es gibt keinen Replace-Pfad. Identische Evidenz bleibt
unverändert, schwächere oder widersprüchliche Evidenz erzeugt
`IDENTITY_EVIDENCE_CONFLICT`. Leere Ingestion ändert nichts.
Sie befüllt nichts aus existierenden Daten. Research-Reads führen keine Migration
aus; eine ältere Datenbank ohne die Tabelle liefert Unknown. Legacy-Rows mit
`verified=true`, aber ohne CIK/stabile ID bleiben unverändert gespeichert und
werden beim Auflösen als `IDENTITY_EPOCH_UNKNOWN` behandelt; keine Reparatur/Promotion.

Multi-Hop-Tickerketten werden nicht aus Current-State-Bindings rekonstruiert:
`AAA → BBB → CCC` bleibt ohne separat geprüfte Lifecycle-Evidenz und erforderliche
Binding-Evidenz für jeden Hop unresolved. Die heutige CCC-Bindung belegt keinen
früheren Hop nach BBB. Details im
[Lifecycle-Ingestion-Vertrag](pairs-stat-arb-v1-security-lifecycle-ingestion.md).

## Untersuchte bestehende Quellen

- Alpaca-Assets/TraWalp-Assetmodell: heutige Referenz, keine effektiven Intervalle.
  Die SDK-Asset-ID wird bisher nicht persistiert. `GetAssetsRequest` hat kein As-of.
- Installiertes `alpaca-py` **0.44.0**: `StockBarsRequest.asof` bezeichnet das
  As-of-Datum der angefragten Symbole. `get_stock_bars` reicht es an `/stocks/bars`
  durch und gibt `BarSet` zurück; kein verifiziertes Identity-Intervall oder
  Bar-Security-Mapping. TraWalp setzt den Parameter nicht. Kein Einsatz als Evidenz.
- Das SDK besitzt Corporate-Action-Modelle, unter anderem `NameChange` mit
  alten/neuen Symbolen und CUSIPs. Der Repository-Adapter liest/persistiert sie
  nicht; keine lokalen Corporate-Action-/Ticker-History-Tabellen vorhanden.
- Massive: eigener Requests-Adapter, kein installiertes Massive-/Polygon-SDK.
  Der bestehende GET-Allowlist-Vertrag erlaubt heutige aktive Tickers und Types,
  keine datierten historischen Zuordnungen. Er wurde nicht erweitert.
- Der aktive Massive-Snapshot enthält aktuelle CIKs und optionale FIGIs, aber
  keine verifizierten effektiven Grenzen. Snapshot-/Updatezeit ist kein Startdatum.
- SEC-Ticker/CIK-Cache und `CompanyIdentity`: Current-State. Cached Submissions
  und einzelne 8-A-/424B4-Daten liefern keine vollständige Security-/Ticker-Membership.

## Verhalten und Reportvertrag

| Situation | Pairs-Verhalten |
| --- | --- |
| Verifiziert außerhalb | Kein historisches Mitglied; kein Gap, kein Ranking, kein SIC2-Block |
| Verifiziert innerhalb, Inputs vollständig | Preis/ADV/Ranking mit ausschließlich Epoch-Bars |
| Verifiziert innerhalb, benötigte Bar fehlt | `LOCAL_MISSING`, validation-blocking; kein Ersatzmitglied |
| Epochenevidenz fehlt oder ist widersprüchlich | `IDENTITY_EPOCH_UNKNOWN`, SIC2/Session unresolved/blocking |
| Warmup benötigt Sessions vor belegtem Start | Insufficient History; keine Berechnung über die Grenze |

Der History-Anker ist die erste gültige, eindeutige Daily-Bar innerhalb der
verifizierten Epoche. Ohne Epoche gibt es keinen Instrument-Anker. Fehlende Inputs
innerhalb eines belegten Intervalls blockieren auch vor der ersten lokalen Bar.
Previous Close, ADV20, beide Calibration-Legs inklusive T und jede mögliche
Outcome-Session werden getrennt gegen dieselbe Epoche geprüft. Calibration-Crossing
liefert `IDENTITY_EPOCH_CROSSING` ohne Korrelation/Spread-Statistik; Outcome-Crossing
ist eine blocking Identity-Anforderung und macht Ready false.

`LOCAL_MISSING_FETCHABLE` wird ausschließlich im Pairs-Pfad zu `LOCAL_MISSING`.
`provider_range_verified` bleibt kompatibel erhalten, zusätzlich wird
`PROVIDER_RANGE_CHECKED` ausgewiesen. Per-Session-Absence bleibt
`NOT_ESTABLISHED` / `PROVIDER_SESSION_ABSENCE_UNKNOWN`, Fetchability `UNKNOWN`.
Es gibt keine lokale Quelle für bestätigte individuelle Provider-Absences;
eine Range-Prüfung erzeugt daher keinen solchen Nachweis. Die normalen Screen-,
F-, C-, Hybrid-, Compare-, SEC-Quarantäne- und Massive-Allowlist-Pfade sind unverändert.

Manifest V6 bindet Epochenevidenz und Semantik an die Fingerprints und verwirft
alte Manifeste. Requirements V4 behält die bisherigen Berichtsfelder und ergänzt
`identity_epoch_requirements`, getrennt von fehlenden Bar-Sessions.

## Lokaler Smoke-Test am 2026-10-14

Nur CAI, FLY und JATT; ehemalige Gap-Session **2023-12-04**, betroffene
Signal-Session **2024-01-02**. Neun lesende SQLite-Queries, zwei Selection-Sessions,
keine Netzwerkzugriffe und keine Änderung der produktiven Datenbank. Dort existiert
die neue Tabelle noch nicht; die vorhandenen Quellen enthalten keine Epochenevidenz.

| Symbol | Session | Resolved epoch | Source | Verified | Inside/outside/unknown | Global first bar | Epoch first bar | Selection status | Blocking |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| CAI | 2023-12-04 | NULL | NULL | false | IDENTITY_EPOCH_UNKNOWN | 2020-07-27 | NULL | SELECTION_MEMBERSHIP_UNRESOLVED | true |
| FLY | 2023-12-04 | NULL | NULL | false | IDENTITY_EPOCH_UNKNOWN | 2020-07-27 | NULL | SELECTION_MEMBERSHIP_UNRESOLVED | true |
| JATT | 2023-12-04 | NULL | NULL | false | IDENTITY_EPOCH_UNKNOWN | 2021-09-10 | NULL | SELECTION_MEMBERSHIP_UNRESOLVED | true |

Diese Ergebnisse behaupten weder ein Listingdatum noch einen konkreten Ticker-Reuse.
Vor einem zuverlässigen Gesamt-Preflight fehlen geprüfte historische
Security-Master-/Börsenreferenzdaten: effektive Ticker-/Security-/Issuer-Zuordnung,
konkrete Common-Share-Klasse, Listing-/Ticker-Wechsel-/Delistinggrenzen und ihre
Verbindung zur heutigen CIK/Security-ID. Danach müssen weiterhin echte interne
Input-Gaps behoben werden. Die Epochenschicht rekonstruiert kein vollständiges
survivorship-clean Universum; SIC und Security-Type bleiben aktuelle Referenzen.

Ausgeführt wurden 17 neue Epoch-Regressionfälle und 21 gezielt ausgewählte
bestehende Checks, insgesamt 38 unterschiedliche Fälle. Zusätzlich: Ruff für die
geänderten Python-Dateien und `git diff --check`. Kein vollständiger Preflight,
Backtest, Sync oder vollständiger pytest-Lauf.
