# Pairs V1: verbleibende Daily-Gaps nach Full-Window-Sync

Diagnose vom 2026-10-14, Codebasis `dec8424750e8ca0a8f204ec4d0380c51b5cf5ddc`.
Keine fachliche Logik, Datenbankdaten oder bestehenden Reports wurden verändert.
Keine neuen Provider-Anfragen, Syncs, historischen Preflights, Validierungen oder
Backtests wurden ausgeführt. Es wurden bestehende Reports gelesen und gezielte
SQLite-Abfragen für sieben Symbole mit `mode=ro` und `query_only=ON` ausgeführt.

## Ergebnis und Grenzen der Aussage

Die Ursache der **weiterhin blocking bleibenden Klassifikation** ist eindeutig:
Pairs leitet interne Gaps aus fehlenden lokalen Inputs nach der ersten lokalen
Beobachtung des **Tickerstrings** ab. Der Anker hat keine historische
Instrument-/Issuer-Zuordnung. Erfolgreiche Provider-Zeitfenster werden erst
anschließend diagnostisch hinzugefügt und ändern diese Klassifikation nicht.

Bei allen sieben Samples liegen alte Bars vor dem angefragten Sync-Fenster,
dann eine mehrjährige Lücke und ein neuer Datenblock ab 2025/2026. Der alte Block
setzt den Anker. Die heutigen Company-/CIK-/SIC-/Common-Stock-Metadaten werden
auf historische Auswahltermine angewendet. Daher gelten selbst Sessions vor dem
neuen Datenblock als interne Selection-Gaps statt als nicht belegte historische
Instrument-Aktivität. Das System kann einen echten fehlenden Datenpunkt nicht
von einer Lücke zwischen verschiedenen historischen Tickeridentitäten unterscheiden.

**Nicht abschließend lokal belegbar:** welcher alte Issuer zu jeder alten Bar
gehörte, exakte Listing-/Delisting-/Tickerwechsel-Grenzen und ob ein anderer
Provider-Feed Daten hätte. Das Bar-Schema enthält keine solche Evidenz. Die
lokalen SEC-Registrierungsdaten stützen Lifecycle-/Ticker-Reuse-Verdacht, ersetzen
aber keinen verifizierten Listingkalender des konkreten Wertpapiers.

`LOCAL_MISSING_FETCHABLE` bedeutet hier somit nicht: »Beim Provider nachweislich
vorhanden und erneut herunterladbar«. Der konservative Block schützt vor einer
unbelegten Top-5-Auswahl; die Aussage über Fetchability ist nicht belegt.

## Verwendete vorhandene Evidenz

- `reports/pairs_stat_arb_v1_preflight_2024-01-02_2026-08-12_v4_coverage.csv`
- `reports/pairs_stat_arb_v1_preflight_2024-01-02_2026-08-12_v4_summary.json`
- `data/trading_system.sqlite3`: ausschließlich symbolbezogene Bars und Metadaten,
  SEC-Cache und gespeicherter letzter Daily-Sync-Status.

Der Report-Suffix `_v4` ist nicht die Manifest-Version: der Report nutzt Manifest V5.
Der vorhandene Coverage-Report enthält **25.369 blocking Symbol/Session-Einträge
für 114 Symbole**, davon 25.334 nur mit Selection-Gap-Rolle und 35 zusätzlich mit
Kalibrierungsrolle. Alle 25.369 haben `provider_range_verified=True` und
`provider_session_absence=NOT_ESTABLISHED`. Es gibt 9.145 unresolved SIC2/Session-
Kombinationen; `selection_membership_resolved=false`,
`company_security_types_complete=true`, `ready_for_local_validation=false`.
Diese Zahlen stammen aus vorhandenen Reports, nicht aus einem neuen historischen Lauf.

Gespeicherter Sync unter `sync_state`, `source=dataset`, `key=daily_history`:

```text
status                  success
last_started_at         2026-10-14T12:35:10.194843+00:00
last_success_at         2026-10-14T12:35:37.802489+00:00
requested_start         2023-10-05
requested_end           2026-08-19
incremental             false
feed / adjustment       iex / all
symbols_requested       116
identity_conflicts_skipped 0
errors / invalid_bars / duplicate_bars 0 / 0 / 0
bars_received           52.531
bars_inserted           522
bars_updated            5.439
bars_unchanged          46.570
symbols_without_older_data 49
symbols_without_data    1
```

Alle sieben untersuchten Symbole stehen ausdrücklich im gespeicherten
`symbols_without_older_data_sample`.
Ihre Coverage-Metadaten markieren `2020-01-02` bis `2026-09-02` exklusiv mit
`checked_at=2026-10-14T12:35:34.140653+00:00`, Feed IEX, Adjustment all.
`_daily_coverage_value` vereinigt zuvor geprüfte überlappende Bereiche; die große
Spanne bedeutet nicht, dass der letzte Lauf nochmals die ganze Spanne ab 2020
abgerufen hat. Sein tatsächliches Fenster ist oben separat belegt.

## Konkrete lokale Historien

Die Blöcke sind Beschreibungen vorhandener Bars, **keine bestätigten Listingintervalle**.
Die Daten beziehen sich auf `timeframe=1d`. Alle untersuchten vorhandenen Bars
bestehen die Preis-/Volumenprüfung von `load_daily`; keine ungültige Sample-Bar
wurde gefunden. Das letzte lokale Daily-Datum ist für alle sieben **2026-09-01**.

| Symbol | Vorhandene alte Blöcke | Neuer Block ab | Daily-Bars gesamt | Bars im Sync-Fenster | Blocking Gap-Sessions im Report |
| --- | --- | --- | ---: | ---: | --- |
| ALPS | 2022-09-29–2023-07-20: 202 Bars | 2025-10-31: 209 Bars | 411 | 200 | 2023-12-04–2025-10-30: 479 |
| BLUW | 2021-02-09–2021-09-09: 47 Bars | 2025-07-31: 274 Bars | 321 | 265 | 2023-12-04–2025-07-30: 414 |
| BPAC | 2022-01-27–2022-04-04: 12; 2022-06-21–2023-06-05: 221 Bars | 2026-01-06: 165 Bars | 398 | 156 | 2023-12-04–2026-01-05: 523 |
| CAI | 2020-07-27–2021-11-22: 332 Bars | 2025-06-18: 303 Bars | 635 | 294 | 2023-12-04–2025-06-17: 385 |
| DSAC | 2020-12-08–2022-07-29: 213 Bars | 2026-02-05: 144 Bars | 357 | 135 | 2023-12-04–2026-02-04: 544 |
| FLY | 2020-07-27–2021-08-02: 256 Bars | 2025-08-07: 269 Bars | 525 | 260 | 2023-12-04–2025-08-06: 419 |
| JATT | 2021-09-10–2022-04-11: 29; 2022-08-02–2023-03-20: 159 Bars | 2026-04-17: 95 Bars | 283 | 86 | 2023-12-04–2026-04-16: 593 |

Die Gap-Intervalle enthalten jeweils **jede offizielle Session zwischen den
genannten Grenzen**; Feiertage/Wochenenden wurden nicht als Gaps gezählt.
Die sieben Samples umfassen zusammen 3.357 blocking Symbol/Session-Einträge.

### Gegenwärtige Identität im historischen Auswahluniversum

Alle sieben Assets sind lokal tradable US_EQUITY auf NASDAQ. Alle sieben haben
Massive `CS -> COMMON_STOCK`; lokale Company-CIK, cached current SEC CIK und
Massive CIK stimmen jeweils überein. Deshalb löst die aktuelle Drei-Quellen-
Quarantäne keinen Konflikt aus. Sie prüft keine Issuer-Zuordnung alter Bars.

| Symbol | Aktuelle lokale Company | CIK in allen drei aktuellen Quellen | SIC / SIC2 |
| --- | --- | --- | --- |
| ALPS | Alps Group Inc | 0002025774 | 8000 / 80 |
| BLUW | Blue Water Acquisition Corp. III | 0002050501 | 6770 / 67 |
| BPAC | Blueport Acquisition Ltd | 0002064177 | 6770 / 67 |
| CAI | Caris Life Sciences, Inc. | 0002019410 | 8071 / 80 |
| DSAC | Daedalus Special Acquisition Corp. | 0002082149 | 6770 / 67 |
| FLY | Firefly Aerospace Inc. | 0001860160 | 3760 / 37 |
| JATT | JATT II Acquisition Corp. | 0002112446 | 6770 / 67 |

Quellen: Asset-Feld aus Alpaca, CompanyIdentity/SIC aus lokalem SEC-Datenbestand,
Ticker/CIK aus cached `sec_reference/ticker_to_cik`, Security-Type aus aktivem
Massive-Referenzsnapshot. Assets und Massive-Snapshot wurden am 2026-10-14
beobachtet; die Company-Rows am 2026-08-15 aktualisiert. Diese Metadaten sind
**Current-State, nicht Point-in-Time**. Die Manifest-Semantiken nennen dies
ausdrücklich: `CURRENT_UNIVERSE_ONLY`, `NOT_SURVIVORSHIP_CLEAN`,
`CURRENT_REFERENCE_SECURITY_TYPE_NOT_PIT`, `CURRENT_LOCAL_SIC_NOT_PIT`.

### Vollständiger Pfad für drei Beispiele

Für den Signaltermin **2024-01-02** ist eine der angeforderten ADV20-Sessions
**2023-12-04**. In allen drei Beispielen gibt es für diese Session keine lokale
Bar; der Report nennt diesen Signaltermin als `affected_signal_sessions`.

| Diagnosefeld für Gap 2023-12-04 | CAI | FLY | JATT |
| --- | --- | --- | --- |
| Erste lokale Bar / verwendeter Anker | 2020-07-27 | 2020-07-27 | 2021-09-10 |
| Vorherige vorhandene Bar | 2021-11-22 | 2021-08-02 | 2023-03-20 |
| Nächste vorhandene Bar | 2025-06-18 | 2025-08-07 | 2026-04-17 |
| Letzte lokale Bar | 2026-09-01 | 2026-09-01 | 2026-09-01 |
| `local_bar_present` | false | false | false |
| `provider_range_verified` | true | true | true |
| `provider_session_absence` | NOT_ESTABLISHED | NOT_ESTABLISHED | NOT_ESTABLISHED |
| `gap_role` | LIQUIDITY_SELECTION_INTERNAL_GAP | LIQUIDITY_SELECTION_INTERNAL_GAP | LIQUIDITY_SELECTION_INTERNAL_GAP |
| `gap_status` | LOCAL_MISSING_FETCHABLE | LOCAL_MISSING_FETCHABLE | LOCAL_MISSING_FETCHABLE |
| `validation_blocking` | true | true | true |

Der Pfad ist für jedes Beispiel:

1. `pairs_base_company_universe` und `company_universe` nehmen die heutige,
   nicht-OTC Company mit den oben aufgeführten SEC-/Massive-Metadaten auf.
2. `load_daily` liefert im historischen ADV20-Fenster keine Bars des Symbols.
3. `load_history_anchors` liefert trotzdem die alte erste Bar aus der gesamten
   lokalen Symbolhistorie, ohne CIK-/FIGI-/Listingintervall-Prüfung.
4. `selection_inputs`: Anker <= T und fehlende Session >= Anker. Die Session
   wird deshalb als intern klassifiziert. Ein direkt vorheriger Schlusskurs
   fehlt ebenfalls; der Preis-Gate kann keine deterministische Exklusion liefern.
5. Die SIC2/Session wird `SELECTION_MEMBERSHIP_UNRESOLVED`, bevor Top-5-Ranking
   stattfinden darf. `provider_range_verified` wird erst beim Coverage-Export
   ergänzt. Es ändert den zuvor erzeugten Gap nicht.

Damit ist das in der Aufgabe genannte **Code-Muster bestätigt**. Ob jede konkrete
alte Bar zu einem anderen Issuer gehört, ist ohne historische Identitätsevidenz
nicht abschließend feststellbar. Ein weiterer identischer Full-Window-Sync kann
den unveränderten Anker-/Current-Metadata-Mechanismus nicht korrigieren.

### Ergänzende lokale SEC-Evidenz, ohne Netzwerkanfragen

In cached SEC `submissions` der heutigen CIKs finden sich diese Registrierungen:

| Symbol | Cached Form 8-A12B | Cached Form 424B4, soweit vorhanden |
| --- | --- | --- |
| ALPS | 2025-10-27 | nicht als Beleg verwendet |
| BLUW | 2025-06-09 | 2025-06-11 |
| BPAC | 2025-11-10 | 2025-11-12 |
| CAI | 2025-06-13 | 2025-06-20 |
| DSAC | 2025-12-08 | 2025-12-10 |
| FLY | 2025-08-06 | 2025-08-08 |
| JATT | 2026-04-16 | 2026-04-17 |

Diese Daten passen zu einer neueren Identitäts-/Listingphase, während der
historische Anker Jahre älter ist. Eine 8-A-/Prospekt-Datumsangabe allein ist
jedoch keine vollständige Common-Share-Handelshistorie: konkrete Wertpapierklasse,
Units versus getrennte Common Shares, Tickerzuordnung und Handelsbeginn müssen
verifiziert werden. Insbesondere lässt sich aus dem ersten IEX-Bar-Datum kein
Listingdatum ableiten. Alte Issuer-/Tickerwechsel-Daten fehlen in dieser Prüfung.

## Status-Erzeugung und Blocking-Gründe

| Feld/Status | Codepfad | Auslösung und Bedeutung |
| --- | --- | --- |
| `first_observed_session_on_or_before_T` | `pairs_stat_arb_v1_data.load_history_anchors`, `pairs_stat_arb_v1_selection.selection_inputs` | MIN des lokalen Daily-Timestamps pro Symbol; nur Anker <= T gilt. Auch ein ungültiger älterer Preisrecord belegt eine Beobachtung. Kein Listing-/Issuer-Anker. |
| `LIQUIDITY_SELECTION_INTERNAL_GAP` / `LOCAL_MISSING_FETCHABLE` | `pairs_stat_arb_v1_selection.selection_inputs` | Fehlender/unbrauchbarer Previous-Close-/ADV20-Input an/nach dem Anker, sofern der vorherige Schlusskurs das Preis-Gate nicht bereits verfehlt. Wird unabhängig von Provider-Absence/Listing erzeugt. |
| `internal_selection_gaps` | dieselbe Funktion | Pro Symbol/fehlender Session aggregierte Evidenz mit allen betroffenen Signalterminen. Kein erneuter Provider-Abgleich. |
| `SELECTION_MEMBERSHIP_UNRESOLVED` | dieselbe Funktion, blocked SIC2 | Mindestens ein Symbol hat einen internen Input-Gap. Ganze SIC2/Session wird vom Ranking entfernt; kein Rank-6-Ersatz. |
| `selection_membership_resolved` | dieselbe Funktion | `not unresolved`; false, sobald eine SIC2/Session ungeklärt ist. |
| `provider_range_verified` | `data.qualification.provider_range_verified`, Aufruf in `prepare_pairs_data` | Erfolgreich abgefragter Bereich gemäß lokalem `daily_history_coverage`, passender Feed/Adjustment. Kein Beweis für Session-Vollständigkeit oder Listing. |
| `provider_session_absence` | `pairs_stat_arb_v1_data.prepare_pairs_data` | Im Pairs-Coverage-Export fest `NOT_ESTABLISHED`, auch bei erfolgreich abgefragtem Fenster. Kein gespeicherter per-Session-Negativnachweis. |
| `symbols_without_older_data` | `data.sync.Synchronizer._sync_daily_history` | Erste lokale Bar **innerhalb des angefragten Fensters** später als erste erwartete Session. Nicht: keine älteren Bars in der gesamten Datenbank. Nicht: verifiziertes IPO-Datum. Nicht blocking für Sync. |
| `ready_for_local_validation` | `pairs_stat_arb_v1_research.summary_metadata` | Benötigt Kandidaten, vollständige Security-Qualifikation, aufgelöste Selection und keine blocking fehlenden Daily-Requirements. |

Zusätzlich erzeugt `prepare_pairs_data` `LOCAL_MISSING_FETCHABLE` für fehlende
explizite ADV-/Kalibrierungs-/mögliche Outcome-Requirements. Die 35 kombinierten
Rollen im Report sind daher keine bloßen Textduplikate einer zweiten Fetch-Anfrage.

CLI-Dispatch: `cli.py` Pairs-Zweig -> `run_pairs_stat_arb_v1` ->
`prepare_pairs_data` -> `company_universe` -> `load_daily`/`load_history_anchors`
-> `discover_pairs` -> `selection_inputs` -> Coverage/Requirements/Summary.
Der Pipelinepfad wurde gelesen, nicht erneut historisch ausgeführt.

Sync-Pfad: `sync_daily_history` -> `_sync_daily_history` ->
`AlpacaDataClient.daily_bars`/`bars` -> `StockBarsRequest` ->
`Database.upsert_bars_with_stats` -> `_daily_coverage_value` -> `_run_stage`.
`--full-window` nutzt `incremental=false`; auch leere erfolgreiche Antworten
zählen als erfolgreich abgefragte Ranges. Upserts löschen ältere Bars nicht.

Das Bar-Schema ist `(symbol, timeframe, timestamp)` plus OHLCV, Trade Count und
VWAP. Es enthält keine CIK, historische Security-ID, Listinggrenzen oder
Bar-bezogene Issuer-/Feed-Provenienz. Der lokale Alpaca-SDK unterstützt `asof`,
aber der Adapter setzt diesen Parameter nicht und besitzt keine historische
Symbol-Mapping-Schicht. Daraus folgt keine Aussage über den konkreten Provider-
Default; es fehlt schlicht ein im TraWalp-Contract gebundenes Symbol-As-of.

## Unterscheidung der acht angefragten Fälle

1. **Lokal fehlt, beim Provider vorhanden:** Pairs kann dies lokal nicht beweisen.
   Es kennt nur die fehlende Bar; der Status behauptet mehr als die Evidenz.
2. **Provider liefert keine Bar:** Erfolgreiche Requests können leer/teilweise
   sein. Coverage dokumentiert die Bereichsabfrage, nicht einzelne Absences.
   Bei IEX ist dies außerdem keine Aussage über alle Handelsplätze/Feeds.
3. **Noch nicht gelistet:** Ohne ältere Ticker-Bar wird ein fehlender Prefix
   nonblocking `INSUFFICIENT_SELECTION_HISTORY`; das ist ausdrücklich kein
   Listingnachweis. Ein alter, möglicherweise fremder Ticker-Anker verhindert
   diese Prefix-Klassifikation.
4. **Bereits delistet:** Es gibt keine historische Delistinggrenze. Fehlende
   spätere erforderliche Bars werden intern/blocking, sofern der Preis-Gate
   nicht vorher ausschließt.
5. **Tickerwechsel:** Kein lokales zeitabhängiges Mapping. Fehlende aktuelle
   Ticker-Bars werden nicht auf einen belegten früheren Ticker aufgelöst.
6. **Ticker-Reuse:** Heutige Drei-Quellen-CIK-Konflikte werden quarantiniert;
   heutige CIK-Übereinstimmung prüft alte Bars nicht. Verschiedene Epochen unter
   demselben String können den Anker und eine Kalibrierung kontaminieren.
7. **Current-Metadaten historisch:** Im Code und in allen sieben Samples
   bestätigt; diese bekannte Nicht-PIT-Semantik ist auch im Manifest benannt.
8. **Echte Dateninkonsistenz:** Ungültige/duplizierte lokale Session-Bars werden
   durch `load_daily` unbrauchbar und innerhalb etablierter Historie blocking.
   Bei den sieben Samples fanden sich keine solchen ungültigen lokalen Bars;
   der gespeicherte letzte Sync meldet ebenfalls null Invalids/Duplicates.

## Klassifikation der Samples, keine Hochrechnung

Die Kategorien überlappen; sie sind keine disjunkte Summe.

| Kategorie | Belastbare Aussage für die sieben Samples |
| --- | --- |
| A: tatsächlich fetchable lokale Lücke | 0 nachgewiesen. Kein Beleg, dass im abgefragten IEX/all-Kontext die fehlenden Bars erneut geliefert würden. |
| B: Provider ohne historische Bar | 7 Samples mit weiterhin fehlender verwendbarer Bar nach gespeichertem erfolgreichem Full-Window-Sync, ohne Invalids. Starke Indikation für keine gelieferte Bar in diesem Kontext; einzelne rohe Negativantworten wurden nicht persistiert. Keine Aussage über SIP/alle Provider. |
| C: pre-listing/post-delisting | Keine abschließend verifizierten Instrumentgrenzen. Lokale neue Registrierungen und alte/neue Bar-Blöcke stützen Lifecycle-Verdacht bei allen sieben; exakte Sessions bleiben ungeklärt. |
| D: Ticker-/Identity-History-Problem | Strukturell bei allen sieben vorhanden: unspezifizierter alter Ticker-Anker und heutige Identität ohne Epochengrenze. Konkrete Wiederverwendungs-/Wechselereignisse sind noch zu belegen. |
| E: Current-Metadaten historisch angewendet | 7/7 bestätigt. Die Samples liefern 3.357 solcher blocking Anforderungen. |
| F: andere Ursache | Keine ungültigen Sample-Bars oder Feed-/Adjustment-Konflikte gefunden. Ungeprüfte echte Feed-Lücken sind nicht ausgeschlossen. |

Für die übrigen 107 blocking Symbole wurde keine Root-Cause-Klassifikation
erstellt. Die 25.369 Report-Einträge sind keine Zahl bewiesener Ticker-Reuse-Fälle.

## Sinn eines weiteren identischen Syncs

Für diese sieben Muster ist ein identischer Full-Window-Sync **kein sinnvoller
nächster Reparaturschritt**: der gespeicherte Lauf hat das Fenster erfolgreich
abgefragt, die relevanten Bars fehlen weiterhin, und der Code interpretiert
dieselben alten Anker erneut gleich. Neue Provider-Korrekturen könnten später
Daten ändern; dafür gibt es in dieser Diagnose keinen Beleg. Der Sync kann
weder historische Issuergrenzen noch PIT-Membership herstellen. Eine Erweiterung
des Fensters rückwärts beseitigt den falsch zugeordneten Anker ebenfalls nicht.

## Kleinster fachlich sauberer Fix, noch nicht implementiert

1. **Evidenz und Benennung trennen:** erfolgreiche Abfrage, rohe per-Session-
   Absence, ungültig verworfene Bar und unbekannte Fetchability getrennt
   dokumentieren, mit Symbol/Session/Feed/Adjustment/Abfragezeit und Symbol-As-of.
   Eine Provider-Absence allein darf nicht global nonblocking werden.
2. **Verifizierte Wertpapier-/Issuer-Epochen nutzen:** lokale, quellenbelegte
   Symbol-zu-Security-/CIK-Zuordnung mit effektiven Listing-, Delisting- und
   Tickergrenzen. Erst den für T gültigen Scope bestimmen; Anker und 60-Session-
   Historie dürfen nicht über Identitätswechsel hinweg zusammengeführt werden.
   Ein nachweislich noch nicht/mehr gelistetes Instrument erzeugt zu T keine
   Auswahlmembership. Das ist kein pauschaler heutiger Symbolausschluss.
3. **Unbekannte Fälle bleiben fail-closed:** ohne belegte Epochengrenze als
   ungeklärte Lifecycle-/Identitätsmembership ausweisen, statt eine wiederholte
   Fetch-Reparatur zu versprechen. Niemals IPO/Delisting aus erster/letzter Bar,
   langer Pause oder Namen allein ableiten. Die vorhandenen SEC-Caches sind
   Hinweise; ohne belastbare Effektivdaten ist keine rein lokale automatische
   PIT-Reparatur gerechtfertigt.

Nur den Anker auf den ersten Bar im Sync-/Research-Fenster zu setzen wäre falsch:
das könnte echte interne Lücken verstecken. Ebenso falsch wäre `Ready=true`,
pauschales Ignorieren, ein Rank-6-Ersatz oder das Zusammenkopieren früherer
Ticker-Bars ohne verifizierte Instrumentkontinuität. IEX-Absence ist keine
universelle Nicht-Handels-Evidenz. Eine Epochenschicht für aktuelle Kandidaten
allein rekonstruiert außerdem noch kein survivorship-clean Gesamtuniversum.

Es wurde **kein Fix implementiert**. Pairs-Economics, ADV20-Fail-Closed-Regeln,
Manifest, F, SEC-Konfliktguard, Massive-Allowlist und Limiter bleiben unverändert.
Keine Codeänderung bedeutet: keine Unit-Tests nötig; nur die beschriebenen
kurzen lesenden Diagnoseabfragen wurden ausgeführt. Die einzige neue Datei ist
dieser Diagnosebericht.
