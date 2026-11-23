# F01 · Alpaca Historical SIP Coverage 2016–2026

**HISTORICAL_SIP_COVERAGE_PARTIAL_TRADING_BLOCKED.** Die vorhandene Kontoprobe belegt historische SIP-Daily-Bars an einzelnen Tagen. Sie belegt weder eine lückenlose Reihe bis zum 20.11.2026 noch den kostenlosen Kontotarif oder die dauerhafte Nutzbarkeit für diesen Research-Zweck. Der 300-Session-Vorlauf für einen Strategiestart Anfang 2016 liegt vollständig vor dem dokumentierten Alpaca-Historienbeginn. Eine vollständige historische Trading- oder PIT-Freigabe bleibt offen.

## Quellen und Repository-Verträge

| Komponente | Befund |
| --- | --- |
| AlpacaDataClient und `sync-daily-history` | Adapter nimmt Feed und Adjustment entgegen. Der Standard ist IEX/all. Der Sync kann größere historische Zeiträume sowie inkrementelle Randfenster verarbeiten. Company-Scope und aktueller US-Asset-Scope ersetzen kein historisches Universe. Range-Coverage und Zustandsmarker helfen beim Resume der bestehenden Synchronisation. Der Bar-Primärschlüssel bindet nur Symbol, Timeframe und Zeitstempel. Daher dient dieser Pfad nicht als Provenienzarchiv verschiedener Feeds und Preisvarianten. |
| Forward-Marktbestand | Die dedizierte Tabelle bindet Provider, Feed, Adjustment, Session, Abrufzeit und Sync-Referenz. Vorhanden ist nur das jüngere SIP/all-Fenster. Der Forward-/STRICT-Vertrag wurde unverändert verwendet. |
| Legacy und U0 | Zwei nach Quelle getrennte Bestände. Konfigurierte Feed-Absicht und frühere Coverage-Marker belegen keine Feed- oder Adjustment-Provenienz je Bar. U0-Benchmark-Warm-up für den alten Start 2022 bedeutet keine Coverage für den Start 2016. |
| E.11 und Eingangsartefakte | Der gebundene D.6-/E.5-Beleg weist null Markt- und Security-Referenzzeilen für E.11 aus. Die vorhandene U0-Referenz, D.6-Dateimetadaten und die echten D.7-Kontoproben werden getrennt ausgewertet. D.7-Punkte bilden kleine Abruffenster. |
| Security- und Kalenderlogik | XNYS, New Yorker Sessiondatum und reguläre Sitzungen einschließlich verkürzter Börsentage. Aktuelle Asset-, CIK- und FIGI-Punkte werden nicht rückdatiert. Einzelne verifizierte Typ-/Cover-Punkte belegen keine Handelbarkeit über ein Intervall. |

Relevante Implementierungen im Repository sind `src/trading_system/data/alpaca_client.py`, `sync.py`, `database.py`, `forward_market.py`, `market_sessions.py`, `security_identity.py` und `reports/f01_historical_50_preflight_run.py`. Die vorhandene Primary-/Probe-Logik wurde erneut genutzt. Für die später geplante Beschaffung sind unveränderliche, separat qualifizierte Shards erforderlich. Produktive Sync-, Reader- und Strategy-Pfade wurden hier nicht verändert.

## Coverage und Warm-up

Die acht verbindlichen Symbole sind SPY, CAT, LLY, AAPL, MSFT, COST, NVDA und ACN. Legacy, U0 und Forward werden mit symbolbezogenen Abfragen über bestehende Indizes unter nativer Windows-Dateisperre gelesen. SQLite bleibt in `mode=ro&immutable=1` und `query_only`. Die CSV unterscheidet alle sechs dokumentierten SIP-/IEX- und Raw-/Split-/All-Kombinationen in den Proben sowie die vorhandenen Archive. `selected_rows_sha256` bindet die sortierten gezielt gelesenen Werte samt Forward-Provenienz; das ist kein Vollhash einer Datenbank. Die Zusammenfassung enthält Dateiidentität, Hashes der kleinen Eingangsartefakte, Kalender-Version und den D.6-SPY-Abgleich.

Die D.6-Referenz nennt 1.806.028 Forward-Zeilen für 5.892 Symbole vom 01.08.2025 bis 06.11.2026. Für SPY weist sie 320 von 1.732 Sessions im Fenster 2020–2026 aus. Legacy enthält 1.570 von 1.732 ab 27.07.2020. 2016–2019 fehlen in beiden Beständen alle 1.006 SPY-Sessions. Die acht Controls wurden gezielt in den aktuellen Dateien nachgezählt. Die globale Forward-Zählung stammt aus dem gebundenen D.6-Inventar. Die Summary legt offen, ob die Dateimetadaten exakt übereinstimmen.

Die 300 vorherigen Sessions vor dem ersten Börsentag 2016 reichen vom 23.10.2014 bis 31.12.2015. `gaps.csv` nennt alle fehlenden Sessiondaten sowie zusammenhängende Blöcke im Handelskalender. Session-Sollwerte sind Referenzwerte, wenn eine historische Listing-Epoche fehlt. Bei einer belegten Epoche gilt ihr halboffenes Intervall. Die vorhandenen punktuellen Security-Belege tragen hier kein solches Intervall. Deshalb kann selbst eine technisch vollständige Jahreszeile fachlich `BLOCKED` bleiben.

## Corporate Actions und Security

- **AAPL:** Alpaca liefert Ex-Date 31.08.2020 und Rate 4:1. Massive meldet für dasselbe Execution Date 1:4 im getrennten Feldpaar Split From/To. Die SIP-Probe zeigt vor dem Split Raw/Split-Schlusskursquotienten um 4 und Split/Raw-Volumen um 4. Ab 31.08. liegen beide bei 1. Raw/All liegt vorher bei etwa 4,13 und danach bei 1,033. Damit ist die technische Abgrenzung der Preisvarianten belegt. Der historische Bekanntgabezeitpunkt sowie die zur Entscheidung kompatible EPS- und Shares-Basis bleiben offen.
- **ACN:** Massive liefert CIK 0001467373 und XNYS/Common Stock für den 23.10.2018 und 19.11.2026. Der jüngste Alpaca-Asset-Punkt und die vorhandenen SEC-Cover-Belege ergänzen die Class-A-Evidenz. Durchgängige Security-Epoche und wirtschaftlicher Issuer-Shares-Scope bleiben offen.
- **TWTR:** SIP/IEX zeigen den 27.10.2022 als letzte beobachtete Session im kleinen Fenster. Alpaca meldet Cash-Merger zum 28.10.2022 und Rate 54,20. Der inaktive Massive-Punkt meldet `delisted_utc` 31.10.2022 04:00 UTC und trägt die historischen FIGIs; der lokale SEC-Index enthält separaten Form-25-Beleg. Letzte tatsächlich bestätigte Börsenorder-/Handelsmöglichkeit, Auszahlung und damalige Veröffentlichung sind getrennt zu belegen. Die Rate ist ein Provider-Exit-Indiz.
- **JATT/ZURA:** Massive datiert JATT 2023 auf CIK 0001855644 und JATT 2026 auf CIK 0002112446. ZURA 2026 gehört zu 0001855644. Alpaca zeigt getrennte alte und neue Barfenster und eine gleichbleibende CUSIP im Name-Change-Beleg mit Process Date 21.03.2023. Im Action-Beleg fehlt Effective Date. Keine rückwirkende Alias-/CIK-Verschmelzung.

## Provider-Qualifikation

| Eigenschaft | Dokumentiert | Lokal beobachtet | Offen |
| --- | --- | --- | --- |
| Kostenloser Zugang | Basic und historischer SIP mit mindestens 15 Minuten altem Abfrageende | Vorhandener Alpaca-Report mit 25 HTTP-Antworten | Konto-Tarif und exakte Berechtigungen |
| SIP-Historie / Daily | Dokumentierter Start 2016 | Je fünf SPY-Bars in den ausgewählten Fenstern 2016, 2018, 2019 und 2025. Je fünf für AAPL 2020 sowie ACN 2026. Jüngerer SIP/all-Forward-Bestand | Alle nicht beobachteten historischen Zeitfenster und fünf Controls ohne entsprechende alte Probe |
| IEX historisch | Eigenständiger Börsenfeed | SPY 2016/2018/2019 in den Stichproben leer, spätere Treffer vorhanden | Grenzen je Titel und Zeitraum. Keine SIP-Substitution |
| Raw, Split, All | Unterschiedliche Anpassungen | AAPL-Überlappung für alle drei SIP-Varianten | Kompatibilität mit damaligem EPS, Shares und wirtschaftlicher Preisbasis |
| Corporate Actions / Symbolwechsel | Dokumentierte Endpunkte und `asof` | AAPL, TWTR, JATT/ZURA | Vollständigkeit, Effective und damalige Publikationszeiten |
| Delistete Titel | Asset-Lookup und historische Bars modellierbar | TWTR inaktiv, kleine Alt-Bar-Reihe, Merger-Evidenz | Vollständige Asset-Historie und belastbare Exits |
| API-Limite | Basic 200 historische Requests pro Minute | Begrenzte kleine erfolgreiche Kontoprobe | Nachhaltige tatsächliche Drosselung und Pagination |
| Persistente Speicherung | Vorliegende technische Dokumentation beantwortet die konkrete Langzeit-/Research-Lizenzfrage nicht | Lokale Artefakte belegen frühere Abrufe und Ablage | Schriftlich dokumentierte Nutzungs-/Aufbewahrungsrechte für den konkreten kostenlosen Account und Zweck |

Massive lieferte im vorhandenen Account-Lauf 403 für Bars 2018 und 2022, neuere Bars und einzelne ältere Referenzpunkte. Das stützt Security-Recherche punktuell. Es belegt keinen Ersatz für historische SIP-Bars. Provider-Quellen und HTTP-Witnesses stehen im Provider-JSON. Kein fehlender Provider-Treffer wird als Nachweis der Nichtexistenz eines Wertpapiers gewertet.

## Beschaffungsplanung und nächster Schritt

Zuerst SPY-Warm-up prüfen. Dafür wird wegen des dokumentierten Gratisstarts 2016 kein Bulk-Request budgetiert. Die offene Frage ist eine kostenlose, rechtlich nutzbare Alternativquelle für genau den fehlenden Vorlauf. Danach SPY 2016–2019, SPY 2020–2026, die sieben Emittenten 2016–2019 und 2020–2026, zuletzt offene Action-/Security-Belege. Die JSON-Planung verwendet jahresbegrenzte Lückenblöcke, gruppiert maximal fünf Symbole und respektiert die tatsächlich beobachteten Werte derselben Variante. Die gruppierte Liste vermeidet erneute Abrufe bereits beobachteter Symboltage. Eine spätere Wiederverwendung setzt identische Feed-/Adjustment-Verträge, Epoch- und Lizenzprüfung sowie gegebenenfalls Adjustment-Revisionsabgleich voraus. Alte unbekannte Provenienz wird nicht gegengerechnet.

SIP/all und SIP/raw werden getrennt geplant. Split bleibt ein gezielter Nachweis bei Aktionen. Pro Batch wird eine erste Antwortseite geschätzt, zwei Seiten dienen als Planungsszenario. Die Dauer bei 30/min ist ein Taktwert; Provider-Latenz, mögliche Wiederholungen und Pagination erhöhen sie. Das beobachtete tägliche Anfragevolumen hat den dokumentierten 200/min-Tarif nicht getestet. Die Speicherprognose nutzt die vorhandenen Alpaca-Antwortgrößen und die 30 früher gemessenen SIP/all-Beobachtungen. 4 KiB je Batch für Manifeste und 128 Byte je angefragtem Slot für Validierung sind explizite Planannahmen. Backups, Transfer und Validierung werden im Szenario getrennt ausgewiesen.

Resume-Key umfasst Provider, Feed, Adjustment, Security-Epoche, Symbol, `asof`, Intervall, Schemaversion und Request-Manifest-Hash. Antwortseiten streamen, SHA-256 und Sessions prüfen, stabile Shards unveränderlich aufbewahren und ein atomares Completion-Manifest publizieren. Eine unvollständige Seite gilt nie als abgeschlossener Batch. Kein Cross-Feed-Stitching und kein Überschreiben qualifizierter Shards.

**Kleinster nächste manuelle Nachweis:** Zuerst kostenlosen Tarif und Speicherrechte festhalten. Anschließend ein separat autorisierter Request für SPY, SIP/all, 11.–15.01.2016, exaktes Symbol, höchstens eine Antwortseite. Damit wird ein angrenzendes ungemessenes Fenster der vorgesehenen All-Variante geprüft. Auch fünf vollständige Tage rechtfertigen eine repräsentative nächste Stichprobe und keinen Full-Download. Es gibt in diesem Task keinen ausführbaren Beschaffungsaufruf.

## Abnahme

Benchmark, Kontinuität, Feed, Adjustments und Actions sind `PARTIAL`. Vorlauf ist `MISSING`. Lizenz ist `UNVERIFIED`. Security-Epochen, PIT und historische Trading-Qualifikation bleiben `BLOCKED`. Der Bericht bezieht sich nur auf die acht Controls; TWTR und JATT/ZURA sind ausschließlich Kontrollfälle für historische Übergänge. Vier gezielte Offline-Tests und Ruff bestanden. Keine API-Anfragen oder Datenbankänderungen.

