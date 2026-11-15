# F01 – Canonical Facts / Source Alias Compatibility Design (Task 8E)

Stand: 13.11.2026. Geprüfter HEAD nach Task 10:
`43958e8dd03513caa980face7237c569686e12a8`.

## Entscheidung und Freigabestatus

**Bevorzugt: A mit vollständigem, verlustfreiem Occurrence-Ledger und versionierten
Reader-Projektionen.** Ein bloßes Alias-Set oder je ein MIN/MAX pro Fact genügt nicht.
Die kanonische Relation beseitigt Mehrdeutigkeit der gespeicherten Fact-Identität;
das Ledger erhält jede ursprüngliche Source-Mitgliedschaft und Reader-Position.
Es behauptet ausdrücklich keine historische Security-Gleichheit der Labels.

**Kompatibilität im unveränderten Input-Snapshot: beweisbar**, sofern die unten
genannten Invarianten gelten. Der PoC führt die bestehenden Produktionsreader
gegen synthetische Tabellen und anschließend deren rekonstruierte View aus.
**Allgemeine Forward-/Legacy-Kompatibilität zukünftiger Ingestion: noch NO_GO.**
Konkreter Blocker: Der bestehende gemeinsame Tabellen-/Writer-Vertrag kennt weder
Occurrence-Rollen noch Source-Aufnahmeentscheidungen; ein neuer Alias-Auftritt
kann die last-wins-Reihenfolge verändern. Der kleine Writer-PoC deckt nur bestehende
Identitäten ab, nicht die vollständige neue Ingestion einschließlich Routing,
neuer Identitäten, Konkurrenz und externer Transaktionseigentümer.

**Späteres Copy-only-Rehearsal: bedingtes fachliches GO als isolierter Nachweistest,
aktuell NO_GO für Ausführung**, bis G0–G2 des Folgeauftrags erfüllt und separat
autorisiert sind. Kein Cutover-GO, keine Migrationsfreigabe und keine Behauptung
vollständig bewiesener historischer F-Gleichheit auf dem realen Bestand.

Keine produktive DB geöffnet oder kopiert, keine reale SEC-Synchronisierung,
keine Änderung an produktiven Readern/Writern/Strategie, kein Shadow-Init/Run,
kein Commit/Push. Neue SQLite-Dateien sind ausschließlich winzige synthetische
pytest-Fixtures. Die vorhandenen Counts, Alias-Klassen und Survivor-Untersuchungen
wurden nicht erneut erhoben.

## 1. Planabgleich am aktuellen Code

Maßgeblich ist das Task-8C-Addendum vor dem erhaltenen 8B-Entwurf in
`f01_fact_deduplication_migration_plan.md`, ergänzt durch
`f01_identity_survivor_resolution_audit.md`,
`f01_duplicate_champion_impact_audit.md` und
`xbrl_instant_fact_correctness_audit.md`.

| Vertrag | Aktuelle Implementierung / Bewertung |
| --- | --- |
| Identität | `database.py:_fact_rows/_execute_fact_upsert`: CIK, Metric, Tag, Unit, Start, Ende, Filed, Accession, Frame. Kein Symbol/Taxonomy im Key. Fehlende Accession/Frame → leerer String. Plan korrekt. |
| NULL-sicherer Writer | Vor Instant-Lookup Writer-Lock bei eigener Transaktion; 0 Treffer Insert, 1 Treffer Update derselben ID, >1 `AMBIGUOUS_FACT_IDENTITY`. Kein willkürlicher Survivor. Plan korrekt. |
| Korrektur | Nur value, fiscal_year, fiscal_period, form werden aktualisiert; Symbol/Taxonomy bleiben erhalten. Korrektur ist keine neue Filing-/Availability-Version. |
| Atomarer Company-Write | `upsert_sec_company_update/_execute_sec_company_update`: Company, Facts, Accessions und Entfernung des Companyfacts-Fehlerstatus in einer Transaktion. Externe Connection wird nicht selbst committed. |
| Legacy | `Database.facts_available_as_of` (Zeile 656): exaktes normalisiertes Symbol, Filed-PIT, optional Metric, `filed,period_end,id`; kein nachträglicher CIK-Filter. |
| Forward | `LocalInputs.facts_available_as_of` (Zeile 140): Companies-CIK, ungelöste SEC-Konflikte sperren, kein Symbolfallback, gleiche Sortierung, Source-Symbol bleibt erhalten. |
| Batch/Research | `iter_fact_batches` (Zeile 1248): `symbol,filed,period_end,id`; Periodenfilter und optional DENSE_RANK pro Symbol/Metric erhalten alle ausgewählten Amendments/Units. |
| Auswahl | `fundamentals/metrics.py`, `quality.py`, `debug.py`: unterschiedliche first-/last-wins-Regeln bestehen weiterhin; siehe Matrix unten. |
| Provenienz | `LocalInputs` hasht konsumierte Facts, Identity-Inputs separat. Discovery berücksichtigt zusätzlich IDs/Quellzustand, Accounting-Cache eligible_count. Physischer Umbau ist keine Zusage identischer Dataset-Fingerprints. |
| Guarded SEC-Writer | `SecWriteDatabase` besitzt explizite Schreibtabellen-Autorisierung (bisher companies, fundamental_facts, sync_state im geprüften Pfad). Neue Tabellen erfordern einen bewusst versionierten Guard-Vertrag; keinen pauschalen Bypass. |

Es wurde keine neue Census-Inkonsistenz gefunden. Das Addendum bleibt für die
bisherige Single-Row-Teilprojektion gültig: 180.283 Aliasgruppen dort ungelöst,
Teilprojektion kein globaler NULL-safe UNIQUE-Fix. Dieser Entwurf ersetzt deren
historische Identity-Evidenz nicht. Er löst die Repräsentation durch verlustfreie
Zerlegung, statt Aliasgruppen zu Single-Row-Legacy-Facts zu erklären.

## 2. Variantenvergleich

| Prüfpunkt | A: Canonical + Occurrences | B: Legacy-Version + kanonische Forward-Daten |
| --- | --- | --- |
| Legacy-Ticker-Membership | Occurrence hält jedes Originalsymbol; auch mehrere CIKs unter demselben Symbol bleiben sichtbar. | Original-DB unverändert und zwingend explizit geroutet; neue kanonische DB allein kann die Membership nicht garantieren. |
| Forward-CIK | CIK bleibt Teil des kanonischen Keys; aktuelle verifizierte Zuordnung und Quarantäne vor Projektion. | Ebenso erforderlich; Trennung der Dateien löst keine Identity-Prüfung. |
| Historische Source-Symbole | Unveränderte Source-Labels pro Auftritt, nicht auf heutige Company umgeschrieben. | Im Legacy-Snapshot erhalten; neue Forward-Facts brauchen zusätzlich Source-Provenienz. |
| Share Classes | CIK ist Issuer-, kein Security-Key. Source-Occurrence erzeugt keine Epoch, FIGI-Verbindung oder Klassengleichheit. | Gleiche Grenze; eingefrorene falsche/ungeklärte Zuordnung wird dadurch nicht fachlich richtig. |
| Cash/Equity/Debt-Anker | Vollständiger Stream erhält alle ersten/letzten Auftritte je Membership, auch bei interleavten Keys. | Legacy unverändert; kanonischer Forward-Single-Row-Stream braucht mindestens metricspezifische Anker und neue Versionsabnahme. |
| Shares/TTM | Vollständige Rekonstruktion erhält alle Selectorinputs, Taxonomy und Duration-Reihenfolge. | Legacy gleich; Forward nur nach separatem vollständigem Nachweis. |
| Filing/Amendment-PIT | Filed/Accession/Start/Ende bleiben im Key, kein Amendment-Merge. | Ebenso; Dataset-Routing muss zum Replay passen. |
| Vollständige Reader-Reihenfolge | Original-ID als occurrence_order, niemals fact_id als Tie-Break. | Originaldatei beweist Legacy; reine Forward-Kanonisierung beweist vollständigen Stream nicht. |
| SEC-Atomarität | Neue Writer-Transaktion umfasst Canonical, Occurrence, Receipt, Company, Accessions und Status. Noch zu implementieren. | Kein atomarer Dual-Write über zwei unabhängig publizierte Dateien. Bevorzugt immutable Legacy, nur Forward schreiben; aktive Legacy-Fortschreibung wäre zusätzliche Architektur. |
| Readerkompatibilität | Read-View kann alte Spalten und IDs exakt projizieren. Query-/Schema-/Performance-Consumer separat abnehmen. | Alte Reader explizit zur alten DB; Fehlrouting muss hart scheitern, kein stiller Fallback. |
| Writerkompatibilität | Alter Writer kann View nicht sicher schreiben: Versionssperre; keine generischen INSTEAD-OF-Trigger. | Alte Writer niemals gegen neue DB; eingefrorene Legacy-Version read-only. |
| Fingerprints | Fact-Stream-Digest kann gleich bleiben; physischer Source-/Schema-/Code-/Dataset-Hash wird neu. | Legacy-Hashes unverändert; neue Forward-Version neue Provenienz, auch bei gleicher Wirtschaft. |
| Langfristige Kosten | Occurrence-Ledger kostet Platz; kein vorweggenommener Storage-Gewinn. Ein Modell für beide Reader. | Dauerhaft zwei Datenverträge, Routing und getrennte Ergebniswelten; Legacy wächst ohne weiteres Design nicht fort. |

B ist eine gute Archiv-/Recovery-Komponente und erhält historische Replays durch
das Original. B allein beweist jedoch nicht die verlangte Gleichheit beider Reader
auf kanonischen Forward-Daten. Fügt man die vollständigen Ordnungs-/Source-Daten
hinzu, nähert sich der relevante Forward-Teil wieder A. Deshalb A als Zielmodell,
zusätzlich unveränderte Legacy-Archive als Recovery und Research-Referenz.

## 3. Versionierter A-Vertrag

Vorgeschlagene Vertragskennung: `F01_CANON_OCC_V1`, inkompatibel zu alten Writern.
Keine automatische Schemaanlage in initialize, kein produktives Feature-Flag hier.

1. `canonical_fact`: surrogate fact_id, vollständiger bisheriger Key und Wert/
   korrigierbare Metadaten. Alle bisherigen NOT-NULL-Regeln erhalten. Bestehender
   Duration-UNIQUE-Key plus partieller Instant-UNIQUE-Key ohne Start bei Start=NULL.
   Keine Datumssentinels; kein Symbol im Key. Die PoC-CTAS-Tabelle ist absichtlich
   kein fertiges Produktions-DDL und ersetzt dessen NOT-NULL-/Typprüfungen nicht.
2. `source_occurrence`: Dataset-Version, originale Fact-ID, fact_id-FK, ursprüngliches
   Source-Symbol, Taxonomy und globale originale Reihenfolge. Bei Snapshot-Import
   **jede Zeile**, einschließlich unbekannter Metrics und identischer Wiederholungen.
   Original-ID und neue fact_id nicht austauschbar. Source bedeutet Beobachtungslabel,
   keine verifizierte Security-Identity.
3. `ingestion_receipt`: neue Batch-/Payload-/Input-Hashes, Retrievalzeit und
   Source-Aufnahmeentscheidung. Wiederholte Requests sind Provenienz, keine neuen
   accounting-wirksamen Tie-Auftritte. Alte Retrievalzeiten sind unbekannt und
   bleiben NULL; historische lokale Importzeiten nicht aus Filed/ID erfinden.
4. Reader-Projektionen: `legacy_occ_v1` und `forward_occ_v1`. Beim Snapshot-Import
   enthalten beide alle alten Occurrences; Filter bleiben jeweils Symbol vs.
   autorisierte CIK. Alle SELECTs sortieren explizit nach Filed, Ende, Original-ID.
   Batchprojektion zusätzlich nach Symbol; kein Vertrauen auf View-Scanreihenfolge.
5. Für künftige neue Source-Labels benötigt die Membership eine getrennte Rolle:
   `legacy_admitted` mit Source-Nachweis und `forward_order_admitted`. Ein zusätzlicher
   Alias desselben bestehenden Facts darf nicht automatisch einen neuen Forward-
   Reihenfolgeanker setzen. Vorhandene Anker bleiben stabil; neue Fact-Identitäten
   erhalten genau einen frischen globalen Ordnungsplatz in Eingabereihenfolge.
   Ein erstmals belegtes Legacy-Label erhält eine neue, versionierte Membership.
   Dessen historisch vorher fehlende Membership darf nicht rückwirkend als alter
   Snapshot ausgegeben werden. Gegebenenfalls nur Receipt speichern und Entscheidung
   blockieren; niemals das gesamte Fact als erfolgreich überspringen.

Source-/Taxonomy-Varianten werden erhalten. Unterschiedliche Werte oder gemeinsame
Metadaten derselben gespeicherten Identität beim Snapshot-Import sind dagegen ein
STOP mit vollständigem Konfliktmanifest; keine Auswahl nach MIN/MAX, Mehrheitswert
oder heutiger Company. Konfliktaufklärung ist ein eigener Auftrag. Künftige echte
Providerkorrekturen derselben eindeutigen Identität aktualisieren dieselben vier
Felder wie heute, behalten Positionen/Source-Labels und publizieren eine neue
Dataset-Version. Das kann Outputs ändern, weil der Input korrigiert wurde; es ist
keine verletzte Gleichheit bei identischen Inputs. Der alte Snapshot bleibt lesbar.
Das Problem wird ausdrücklich nicht durch Hinzufügen einer Acquisition-PIT-Regel
zu bisherigen Filed-PIT-Queries verdeckt.

### Vollständige Selector-Matrix und Gleichheitsbeweis

| Consumer | Unveränderter Auswahlvertrag |
| --- | --- |
| Cash, Equity, Assets, Current Assets/Liabilities | max(Ende, Filed), erster vollständiger Tie, ggf. End-Cutoff. Keine neue Unit-Konversion. |
| Direct Total Debt | Ebenfalls erster Tie. |
| Debt Current/Noncurrent | Stabile Filed-Sortierung, letzter Auftritt je Ende; neuestes gemeinsames Ende, Summe beider Komponenten. |
| Direct gegen Komponenten | max(Ende, Filed), Direct zuerst und gewinnt kompletten Gleichstand. |
| Shares | unit=shares, positiver Wert, Ende/Filed≤as_of, End-Cutoff; max(Ende, Filed, DEI-vor-GAAP, Accession, Tag, Wert); beide 460-Tage-Grenzen. Kein Addieren von Share Classes. |
| Flow/EPS | Nur Start vorhanden und 60–400 Tage; stabil nach Filed, letzte Version je Start/Ende/Unit. Direkte Quartale bevorzugt, sonst kohärente kumulative Differenzen. NULL-start bleibt ausgeschlossen. |
| TTM | Vier Quartale, Unit des letzten Quartals, keine Unit-Mischung, Abstände 60–130 Tage. Metric-spezifische Prior-TTM-Cutoffs 300 Tage; Prior Balance relativ zum gewählten Balance-Ende. |
| Debug/Accounting | Dieselben Source-Ties und Reihenfolgen; eligible_count identisch im unveränderten Snapshot. |
| Raw-/externe Consumer | Original-IDs, Multiplizität und Spalten durch View erhalten; unbekannte direkte SQL-/Schema-/Index-Erwartungen gesondert inventarisieren. |

Sei R die originale, nach dem jeweiligen Readerfilter sortierte Zeilenfolge und
J die Occurrence-zu-Canonical-Projektion. Für jede Originalzeile existiert genau
eine Occurrence mit derselben ID, demselben Symbol/Taxonomy und derselben
Key-/Wert-/Metadatenprojektion. Kein Occurrence-FK darf fehlen, keine Join-
Vervielfachung auftreten. Dann ist J vor Filterung zeilenweise gleich dem
Original. Symbol-, CIK-, Filed-, Metric- und Periodenfilter ergeben deshalb exakt
R, ebenso DENSE_RANK. Original-ID macht die Ordnung vollständig. Alle obigen
deterministischen Consumer erhalten dieselbe geordnete Eingabe und produzieren
gleiche Werte, Source-Debug-Daten und Scores. Mit identischen sonstigen Inputs
folgen gleiche Eligibility und Rankings. Das Argument gilt unabhängig von
unbekannter Metric-Semantik, weil keine Zeile entfällt.

Die frühesten Cash-/Equity- und spätesten Debt-Anker sind hier **beide enthalten**,
je Source-Mitgliedschaft und global. Die 8C-Ankerregeln werden nicht durch eine
neue globale Survivor-Regel ersetzt. Nur wenn später Occurrences komprimiert
werden sollen, ist ein neuer vollständiger Beweis nötig.

Bei B ist R im unveränderten Legacy-Archiv trivial gleich. Für reine kanonische
Forward-Daten gilt J=R nicht: A, B, A mit gleichem Filed/Ende und verschiedenen
Accessions kann bei last-wins von A zu B wechseln. Ein einziges kanonisches
Source-Symbol kann zusätzlich Legacy-Membership verlieren. Diese Gegenbeispiele
genügen für NO_GO einer pauschalen Single-Row-Kompatibilitätszusage.

## 4. Writer-Entwurf, ausschließlich zur späteren Implementierung

Der heutige `_execute_fact_upsert` und seine Ambiguitätssperre bleiben unverändert.
Ein neuer expliziter V1-Writer wird nur nach erfolgreicher Schema-/Dataset-Prüfung
und Abnahme geladen. Alter Writer auf neuem Schema und neuer Writer auf altem
Schema müssen vor dem ersten Write hart scheitern.

Transaktion pro SEC-Company:

1. Writer-Lock (`BEGIN IMMEDIATE` bei Eigentümer), Schema-/Source-Version prüfen.
   Extern besessene Transaktion dokumentieren; Fehler propagieren, niemals selbst
   committen. Company-/Payload-CIK, verifizierte Request-Zuordnung und offene
   Konflikte prüfen. Source-Label ist kein Ersatz für CIK-Evidenz.
2. Vollständige geparste Eingabe vorhalten; Reihenfolge, NULL-Normalisierung,
   Parser-Aliaswahl und Duration-Vertrag unverändert. Unbekannte Metric speichern
   oder gesamte Company mit explizitem Fehler zurückrollen, nicht überspringen.
3. NULL-sicherer kanonischer Lookup: >1 ist harter Integrity-Fehler; 0 legt neue
   Identität und Ordnungsplatz an; 1 erhält die ID und aktualisiert ausschließlich
   die heute korrigierbaren vier Felder. UNIQUE-Indizes sichern konkurrierende Writes.
4. Source-Receipt/Membership atomar schreiben. Bekannte Wiederholung erhält alle
   existierenden Auftritte und erzeugt keinen neuen accounting-wirksamen Anker.
   Neue Source-Labels benötigen explizite Beleg-/Admission-Entscheidung und die
   getrennten Reader-Rollen aus Abschnitt 3. Kein stilles Security-Merge.
5. Company, Facts, Occurrences, Receipts, Accessions, Cache-/Statusänderungen,
   soweit sie zum bisherigen Company-Commit gehören, gemeinsam committen.
   Bei beliebigem Fehler alles zurückrollen, auch bereits gültige vorherige Facts.
   Request-/Fehlertelemetrie darf separat fehlgeschlagenen Versuch dokumentieren;
   kein erfolgreicher Accession-Cursor und kein neuer last_success bei Fehler.
6. DataSynchronizer-Fehlerkette erhalten: Database Failure → Rollback →
   errors/database_failures → partial/failed. Readiness bleibt gesperrt; weder
   erfolgreicher No-op noch READY erzwingen. SecWriteDatabase-Tabelle-/Operation-
   Allowlist gezielt versionieren, ohne Autorisierungsumfang pauschal zu erweitern.

Die globale kanonische Korrektur wirkt in der aktuellen neuen Dataset-Version auf
alle verknüpften Source-Occurrences. Das muss ausdrücklich abgenommen werden:
der alte mehrdeutige Writer hätte abgebrochen. Es ist keine heimlich behauptete
Gleichheit mit einem erfolgreichen alten Alias-Write. Für bisher eindeutige
Identitäten muss die Korrektur exakt dem bisherigen Upsert entsprechen.

## 5. Synthetischer PoC und Testgrenzen

Neu: `tests/test_f01_alias_ingestion_design.py`. Nur selbst erzeugte pytest-DBs.
`normalize_fixture` zerlegt kleine Tabellen in Canonical/Occurrence, behält eine
Oracle-Tabelle und stellt `fundamental_facts` als verlustfreie Read-View bereit.
Es gibt keinen CLI-Einstieg für reale DBs und keinen allgemeinen Migrationscode.

- Sechs AABB-Interleavings: gleiche CIK/mehrere Source-Symbole, gleiche Ticker/
  unterschiedliche CIK, mehrere Accessions, identische Instant-Wiederholung,
  Cash-/Debt-Ties, Amendment vor/am Filing, unbekannte Metric. Je neun Reader-/
  PIT-Kombinationen plus Batch/DENSE_RANK. Vergleich vollständiger Listen,
  Fact-Digests, Accounting, Debug, Metriken, Scores und Rankingresultate.
- Replay bestehender Identität erzeugt keine zusätzliche Zeile; Korrektur 10→20
  hält die Occurrences und korrigiert auch OLD. Eingefrorene alte Eingaben bleiben 10.
- Fehlerhafte Company-CIK oder fehlende Source-Autorisierung nach einem vorherigen
  gültigen Update: vollständige Gleichheit von Canonical, Occurrences, Companies,
  sync_state vor/nach Rollback. Keine erfolgreiche Teil-Company.
- Inhaltlicher Identitätskonflikt stoppt die Zerlegung. B-Gegenprobe zeigt
  Debt-/Score-Differenz einer kanonischen Liste ohne erhaltene Auftrittsordnung.

Zusätzlich bestehende gezielte Regressionen aus `test_f01_survivor_resolution.py`
und `test_xbrl_fact_upsert.py`: Share-Class-Epoch-Nichtgleichheit, Shares/Alter/
Taxonomy, EPS/TTM/Unit/Amendments, alle Survivor-Klassen, echte Rank-1-Gegenprobe,
NULL-safe Guards, konkurrierende Writer, externer Connection-Vertrag und atomarer
Company-/Accession-Rollback. Full/Incremental-SEC-Regression nutzt Provider-Doubles
und prüft partial sowie Readiness ohne Shadow-Init/Run.

**Resultat:** final **11 neue PoC-Tests bestanden**, 18,50 s; **47 bestehende
gezielte Regressionen bestanden** (29 Survivor-/Identity-/Readiness-Fälle und
18 Instant-Writer-Fälle). Insgesamt 58 verschiedene bestandene Fälle über die
gezielten Läufe. Ruff check besteht, git diff --check besteht. Eine bestehende
`websockets.legacy`-DeprecationWarning; keine neue Warnung.

Der erste kombinierte Lauf hatte 55 bestandene Fälle und eine fehlerhafte neue
Fixture-Assertion: Sie verglich auch den legitimen Debt-Vorjahres-Fact mit der
Liste für das aktuelle Jahr. Der Periodenfilter wurde korrigiert, die neue Datei
anschließend vollständig grün ausgeführt und um zwei zusätzliche Fälle ergänzt.
Kein Produktionsfehler und keine geänderte wirtschaftliche Erwartung. Das initiale
JUnit `f01_alias_ingestion_design_tests.xml` bleibt als Zwischenstand erhalten;
der maßgebliche finale PoC-Nachweis ist `f01_alias_ingestion_design_poc.xml`.

Die zusätzliche neue Alias-Probe beweist den Writer-Blocker ausdrücklich: Nach
Aufnahme eines neuen Source-Labels für A in A,B wechselt Forward last-wins wieder
auf A, obwohl kein neuer wirtschaftlicher Fact hinzukam. Der Test besteht, weil
er genau diese unsichere Wirkung nachweist. Die neue Quarantäne-Probe bestätigt,
dass die normalisierte View keinen Forward-Tickerfallback erzeugt. Der Rollback-
Test fügt vor dem CIK-Fehler eine neue Source-Occurrence ein und weist auch deren
vollständiges Zurückrollen nach.

Reproduktion aus dem Repository mit dem konfigurierten Python-SDK (vor jedem
Aufruf die IDE-Umgebung prüfen):

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/test_f01_alias_ingestion_design.py -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_8e_cache --basetemp=.tmp_pytest_8e_complete --junitxml=reports/f01_alias_ingestion_design_poc.xml
& '.venv/Scripts/python.exe' -m pytest tests/test_f01_survivor_resolution.py tests/test_xbrl_fact_upsert.py -o addopts='' -q --tb=short -o cache_dir=.tmp_pytest_8e_cache --basetemp=.tmp_pytest_8e_regression
& '.venv/Scripts/python.exe' -m ruff check tests/test_f01_alias_ingestion_design.py
```

Die 47 Regressionen wurden im initialen kombinierten Lauf vollständig ausgeführt;
der zweite Befehl ist die isolierte Wiederholungsanweisung, kein behaupteter
zusätzlicher Lauf. Temporäre Fixtures liegen unter den genannten pytest-Pfaden.
Der Bericht und JUnit-Dateien liegen lokal im bereits ignorierten reports-Bereich;
`.gitignore` wurde nicht geändert.

Grenzen: Kein realer Input-Vollvergleich, keine historischen Screens, kein
Performance-/Storage-Test. Ein Einzelkandidat-Ranking in den neuen sechs Fällen
beweist keinen aktiven Zwei-Kandidaten-Wettbewerb; dafür dienen die bestehenden
Rank-1-Fälle. Writer-PoC unterstützt absichtlich keine neuen Identitäten und keine
vollständige Companyfacts-Stage. Source-Autorisierung ist dort nur ein Testparameter,
keine implementierte Evidenzprüfung. Eine Archivversion B wird als unveränderte
Eingabeliste modelliert; ein produktiver Dataset-Router wurde nicht gebaut.

## 6. Source-/Input-Fingerprints und offene Identity-Risiken

Zwei Ebenen getrennt abnehmen:

- **Kompatibilitätsstream:** geordnete Spalten/IDs plus Fact-Digest müssen bei A
  im unveränderten Snapshot identisch sein. Der PoC weist auch LocalInputs-
  Listeninhalt nach; Identitätsmapping und Quarantäne bleiben eigene Inputs.
- **Neuer Datenbestand:** neues Dataset-ID, Schema-/Reader-/Writer-Version,
  Source-Snapshot-Hash, Plan-/Config-/Parser-/Codehash, physischer DB-Hash,
  Occurrence-/ID-Mappinghash und Receipt-Hashes. Physische Source-/Discovery-
  Fingerprints dürfen sich ändern, aber nur erklärt und neu versioniert.

Alte Journale, Research-Manifeste, Cache-Keys und Exporte bleiben unverändert.
Kein Umschreiben von Snapshot-Labels und kein Deaktivieren von Drift-Prüfungen.
Für neue Versionen Cache-Namespace trennen; alte Cachewerte nicht übernehmen.

Es bleiben alle bekannten historischen Identity-Risiken: 0 verifizierte historische
Epochen im vorhandenen Audit, keine autoritative Tickerkette, mögliche verschiedene
Share Classes desselben CIK, Ticker-Reuse über mehrere CIKs, heutige Reference-
Abweichungen, fehlende lokale Erwerbszeit und fehlende Decision→Fact-Lineage.
Das Ledger bewahrt Beobachtungen, löst diese Risiken nicht. Gegenwärtige CIK-
Quarantäne bleibt aktiv. Historische Legacy-Replays behalten ihre damalige
Semantik einschließlich deren Grenzen; daraus wird keine Forward-Identity-Freigabe.

## 7. Separater Folgeauftrag: Copy-only-Rehearsal und Recovery

Task-Vorschlag: **8F – implementierte A-V1-Projektion und Writer ausschließlich auf
verifizierten Kopien prüfen**. Dieser Abschnitt ist ein ausführbarer Arbeitsablauf
mit Abnahmeobjekten, kein heute auszuführender Shell-/Migrationsbefehl.

### G0 – Autorisierung und Scope

Eigener Auftrag nennt absolute Quell-, Backup-, Arbeits- und Ergebniswege.
Keine Produktionspfad-Umschaltung, kein Netz, kein echter SEC-Sync, keine
Shadow-Läufe. Migrationstool akzeptiert nur manifestgebundene Arbeitskopien,
verweigert Quelle/Backup sowie gleiche Datei über Hardlink/Symlink/samefile.
Produktive Runtime darf Ergebnis niemals automatisch entdecken.
Implementierung von Reader-Rollen, neuen Identitäten, Receipt-Idempotenz,
Source-Admission, externen Connections und Versionssperren vorab reviewen.
Ohne diese Punkte NO_GO für ein vollständiges Ingestion-Rehearsal.

### G1 – Konsistente Sicherung und vollständiger Input-Snapshot

1. Writer-/Reader-/WAL-Zustand erfassen und zulässiges Sicherungsfenster festlegen.
   Konsistentes SQLite-Online-Backup mit allen committed WAL-Inhalten oder
   koordiniertes Offline-Backup; keine isolierte Hauptdateikopie bei aktivem WAL.
2. Immutable Backup + separate Arbeitskopie erzeugen. Quellbestand bleibt erhalten.
   Backup read-only mit integrity_check, Schema-/Zählern und SHA-256 verifizieren;
   Restore-Probe auf weiterer Wegwerfkopie. Keine Recovery gegen Original.
3. Vollständigen Input-Snapshot manifestieren: alle DB-Tabellen, IDs, Views,
   Trigger, Indizes, PRAGMAs, Companies/Assets, Konflikte, Identity-/Security-
   Referenzen/Epochen/Corporate Actions, Marktinputs und deren Provenienz,
   Sync-/Accession-/Dataset-State, archivierte Companyfacts/Submissions samt
   Hashes/Cachezeiten; außerdem Konfiguration, Kalender, Peer-/Medianinputs,
   Code/Parser/Reader-Version und referenzierte externe Dateien. Fehlende Inputs
   ausdrücklich als fehlend erfassen, niemals aus heutigem Netz nachfüllen.
4. Fremdschlüssel, externe ID-/SQL-/Cache-/Schema-Consumer inventarisieren. Unknown
   Consumer ohne kompatiblen Vertrag ist STOP, keine unbekannten Facts überspringen.
5. Freien Platz auf Backup-, Arbeits- und tatsächlichem Temp-Volume prüfen und
   Budget für Originalbackup, Work, Rebuild, WAL/Journal, Sortier-/Index-Scratch
   reservieren. Frühere 42–53 GB sind nur Planannahme, keine A-Speicherprognose.
   Da A alle Occurrences erhält, kann mehr Platz nötig sein. Schwellen und
   Abbruchüberwachung vor Start schriftlich festlegen.

### G2 – Schema-/Reader-Vertrag und Sollzahlen vor dem Umbau einfrieren

Keine neue Alias-/Survivor-Untersuchung ohne neue konkrete Inkonsistenz.
Die bisherigen Auditwerte gelten nur, falls Snapshot und Vergleichsmanifest passen:

| Erwartung im bekannten Snapshot | Soll |
| --- | ---: |
| Original und vollständige A-Occurrences | 18.698.090 |
| Darunter Instant-Occurrences | 13.764.701 |
| Unveränderte Duration-Identitäten/-Occurrences | 4.933.389 |
| Kanonische Instant-Identitäten | 3.396.519 |
| A Canonical insgesamt | **8.329.908** |
| Ausgelagerte zusätzliche Instant-Auftritte | 10.368.182 |
| Aliasgruppen unverändert nachvollziehbar | 180.283 |
| Bekannte numerische Konfliktgruppen | 0 |
| B Legacy-Archiv | 18.698.090 |
| B vollständiger kanonischer Forward-Bestand, falls gebaut | 8.329.908, ohne Gleichheitsgarantie |

8.329.908 = 3.396.519 + 4.933.389; keine neue Messung. 9.547.182 aus 8C ist
hingegen die **andere**, begrenzte Single-Row-Teilprojektion, kein Soll für A.
Zahl verschiedener Fact/Symbol-Memberships wurde hier nicht neu ermittelt;
keine Zahl erfinden. Im Ausführungsmanifest vorab aus dem gebundenen Snapshot
ableiten und mit dem Mapping abgleichen. Jede Abweichung am Snapshot ist STOP
und benötigt Erklärung/neu gebundene Erwartungen, keine automatische Toleranz.

Versioniertes explizites DDL, View-Spalten/-Typen, ID-Namensräume, beide Reader-
Rollen und alle Versionssperren einfrieren. In B Legacy-Version unverändert und
read-only routen; kanonische Forward-Version niemals als Legacy-Ersatz deklarieren.

### G3 – Deterministischer Neubau nur auf Work

Source-Hash und Plan-ID prüfen. Neubau streamend in begrenzten Batches mit Ledger:
Keybereich, Inputzeilen, Canonical-/Occurrence-Zähler, Mappinghash, Commit-ID.
Jede Source-ID muss genau einmal vorkommen; jede Occurrence genau einen gültigen
Canonical-FK haben. Alle Quellspalten verlustfrei rekonstruierbar. Konfliktgruppen
stoppen den gesamten Publish-Vorgang; kein partiell als erfolgreich publiziertes
Dataset. Ergebnis währenddessen für Runtime unzugänglich halten. Keine Original-
Löschungen/VACUUM; kein impliziter ID-Neuaufbau der Kompatibilitätsprojektion.

### G4 – PIT-/Score-/Ranking-Abnahme

Auf Source-Backup und Ergebnis mit identischer eingefrorener Konfiguration:

- Vollständiger geordneter Streamvergleich der alten Spalten/IDs; gleiche
  Symbol-/CIK-/Metric-Membership, Source-Taxonomy, Multiplikatoren und DENSE_RANK-
  Batchresultate. Liste aller Filed-Grenzen vor/am/nach Amendment, End-Cutoffs,
  Share-Altersgrenzen und TTM-/Prior-Grenzen deterministisch vorab bilden.
- Beide Reader gegen identische Companies-/Konfliktinputs. Grenzfälle Ticker-Reuse,
  mehrere Share Classes und unresolved CIK müssen dieselben Sperren ergeben.
- Accounting/TTM/Balance einschließlich Periodenenden und ausgewählter Sources,
  Metrikverfügbarkeit/None, Quality/Valuation/F, Eligibility, vollständige sortierte
  Ranglisten und Rank 1 vergleichen. Feste Markt-/Technical-/Peerinputs, unveränderte
  Strategieparameter. Kein Shadow-Run; reine Offline-Reader-/Funktionsproben.
- Wirtschaftlich identische Inputs: **0 unerklärte Differenzen**, auch bei gleichen
  Beträgen mit anderen Sources/Periodenenden. Scores nicht als Toleranzfilter für
  Streamverluste verwenden. Neues Source-/Input-Manifest zusätzlich vergleichen;
  neue physische Hashes erklären, alte Artefakte unverändert nachweisen.

### G5 – SEC-Ingestion-Regressionsmatrix auf weiterer Wegwerfkopie

Nur archivierte/synthetische Companyfacts und Provider-Doubles; Netzwerk sperren.
Full/Incremental, bekannte/neue Accession, identische Wiederholung, gemischte
Instant-/Duration-Eingabereihenfolge, neue Identität, gleiche Identität mit
legitimer Korrektur, neue Source-Membership, gleiche Ticker/falsche CIK, offene
Identity-Konflikte, Taxonomy-Varianten, unbekannte Metrics, fehlende optionale
Keys, Lock-/Parallelitätsfehler, Retry nach Restart, interleavte Cash-/Debt-Keys,
externe Connection mit Rollback. Korrektur nicht als Amendment umdeuten.

Fehler nach bereits gültigem Fact, beim Occurrence-/Receipt-/Accession-Write
und beim Company-/Statusabschluss injizieren. Company, Canonical, Occurrences,
Receipts, Accessions und Erfolgscursor vollständig zurückgesetzt; Fehlerzähler
und partial/failed korrekt, last_success unverändert. Alte/neue Writer-Schema-
Kreuzkombinationen müssen gesperrt sein. Kein skip-as-success, kein READY-Override.

### G6 – Crash, Recovery, Abschlussentscheidung

Crash vor/nach Batch-Commit und während Indexaufbau simulieren. Wiederanlauf nur
mit demselben Snapshot-/Plan-/Schemahash und verifiziertem Commit-Ledger.
Uncommitted Work durch SQLite-Recovery zurückrollen; committed Batches genau
einmal prüfen. Bei falschem Ledger, Hashdrift, Integrity-/FK-Fehler oder unklarer
Fortsetzung Work verwerfen und aus verifiziertem Backup neu erstellen.
WAL/SHM niemals bei offenen Verbindungen löschen/umbenennen. Checkpoint nur an
isolierten Kopien im genehmigten Ablauf. Ergebnis erst nach geschlossenen
Verbindungen final hashen; Source-Manifest und Backup unverändert verifizieren.

Recovery-Abnahmeobjekte: absichtlich unterbrochener Lauf, erfolgreicher Restore,
erneut identische Output-/Mappinghashes, dokumentierter Platzbedarf und Laufzeit.
Bei Abbruch bleiben Source und Backup Referenz; Ergebnis erhält FAILED/NO_GO.
Kein produktiver Rollback nötig, da keine Pfade umgestellt werden. Ein späterer
Cutover braucht eigenen Auftrag inklusive Sicherung nachfolgender Ingestion;
bloßer Code-Downgrade wäre kein konsistenter Datenrollback.

**GO für abgeschlossenes Rehearsal** nur bei G0–G6 vollständig bestanden,
0 unaufgeklärten Konflikten/Consumer-Verträgen, korrekten Sollzahlen, vollständigem
Mapping, 0 PIT-/Score-/Ranking-Differenzen bei identischen Inputs, allen bestandenen
SEC-/Fehler-/Recovery-Proben und explizit versionierter Provenienz.
**NO_GO** bei jedem fehlenden Nachweis, insbesondere unerhaltener Reader-Ordnung,
Source-Membership-Verlust, CIK-Fallback, Skip unbekannter Facts, Writer-Guard-
Aufweichung, READY-Override oder rückwirkendem Research-Umetikettieren.
Auch ein GO für dieses Rehearsal autorisiert keinen produktiven Cutover.
