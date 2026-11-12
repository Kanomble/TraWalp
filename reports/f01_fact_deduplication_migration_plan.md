# Task 8C — maßgebliches Resolution-Addendum (12.11.2026)

Baseline Task 8C: `cb0c1b632f8c24113d22a603136bdc70c3711aaf`.
Dieses Addendum präzisiert den darunter erhaltenen Task-8B-Entwurf. Details und
Nachweise: `reports/f01_identity_survivor_resolution_audit.md`.
**Keine Migration, Kopie, Bereinigung oder produktive Semantikänderung ausgeführt.**

## Verbindliches Verdict und Klassen

- **Vollmigration NO_GO:** alle 180.283 Aliasgruppen bleiben im bisherigen
  Single-Row-/Legacy-Tickervertrag ungelöst. Keine bestätigte historische
  Tickerwechselzuordnung: 0 verifizierte Epochen und 0 passende Corporate Actions.
  Gleicher CIK oder heutiger SEC-/Reference-Match genügt nicht zur Security-Gleichsetzung.
- **Teilkonzept fachlich GO nur für SAFE_TO_DEDUP:** 3.058.649 vollständige
  Ein-Symbol-Gruppen / 9.150.908 Zusatzzeilen sind anhand des vollständigen
  Snapshot-Keys und gleicher endlicher Werte/Nicht-Key-Metadaten isolierbar.
  Diese Aussage ist keine Ausführungsfreigabe. Ein separater Copy-only-Dry-Run
  benötigt weiterhin Original-ID-/Referenzprüfung, Backup, Abnahme und Autorisierung.
- **MUST_PRESERVE:** komplette Alias-/Konfliktgruppen, alle legitimen verschiedenen
  CIKs/Units/Tags/Accessions/Frames/Filingdaten/Perioden sowie Duration-Facts und
  der nachgewiesene Survivor-Anker. Keine Skips oder Umlabelungen.
- **SELECTION_SENSITIVE:** falsches MIN/MAX kann Wert, Scores und Rank 1 ändern.
  Das Flag kann eine ansonsten reine Gruppe betreffen; SAFE ist sie nur mit dem
  vollständig nachgewiesenen Reader-Anker, niemals mit beliebiger globaler Regel.
- **AMBIGUOUS_IDENTITY / INSUFFICIENT_EVIDENCE:** insbesondere die historische
  Beziehung der 180.283 Aliasgruppen bleibt offen. Unbekannte Consumer, neue
  Metadata-/Wertkonflikte oder nicht abgenommene externe ID-Verträge sind STOP-Gates.
  Diese Prüf-/Erhaltungsflags sind keine disjunkten zusätzlichen Zählmengen.

## Geschlossener semantischer Teilvertrag

Nur vollständig gleiche Gruppen mit bekanntem Consumer dürfen später auf einen
Survivor reduziert werden:

1. Cash, Equity, Assets/Current Assets/Current Liabilities, direkte Total-Debt-
   Vertretung: frühester ursprünglicher ID-Auftritt (MIN).
2. Debt Current/Noncurrent: spätester ursprünglicher ID-Auftritt (MAX).
   Konkurrenz zwischen Direct Debt und Komponenten bleibt unverändert.
3. Shares: vorhandene Date-/Taxonomy-/Accession-/Tag-/Wert- und Alterspriorität
   unverändert; bei völlig gleichem Inhalt deterministisch MIN.
4. NULL-start-Flows/EPS: nicht als Quartale ausgewertet, deterministisch MIN nur
   im vollständig gleichen bekannten Consumer-Scope. Duration-Facts nie bereinigen.
5. Alle Gruppen mit mehreren Source-Symbolen oder anderen relevanten Varianten
   vollständig beibehalten, auch bei Wertgleichheit. Gruppenzugehörigkeit im
   gesamten Snapshot prüfen, nicht aus einem Legacy-Symbolslice ableiten.

Der früheste bzw. späteste Auftritt erhält den jeweiligen Gewinner gegenüber
anderen interleavten Keys. Filed-/Periodenfilter sind identitätsintern gleich;
Source-/CIK-Mitgliedschaft ist bei den zulässigen Ein-Symbol-Gruppen gleich.
Die Aliasgruppen bleiben komplett im Stream. Somit verändert die Teilprojektion
weder deren Membership noch die für die nachgewiesenen Reader relevante Reihenfolge.
Der Beweis gilt für wirtschaftliche und ausgewählte Debug-Ausgaben, nicht für
identische Raw-Listen, Counts, Cache-Keys oder Input-Fingerprints.

29 gezielte Offline-Tests bestanden, einschließlich sechs AABB-Interleavings je
neun Metric-Klassen und zwei Readern, Alias-/Wertkonfliktisolation, CIK/Unit/Perioden-
Grenzen sowie SEC-Abbruch/Readiness. Die zwei danach verstärkten Rank-1-Proben
bestanden ebenfalls. Reale vorab definierte Alias-Stichprobe: 18 Vergleiche,
0 Fundamental-/Scoreänderungen, 6 Rankingvergleiche ohne Unterschied unter der
Teilregel; 12 geänderte Input-Digests. Das ersetzt keine historische Vollvalidierung.

## Aktuelle Sollzähler und verbleibende operative Grenzen

Quelle: 18.698.090 Facts. Rechnerisch nach ausschließlich zulässiger Teilprojektion:
**9.547.182 Facts**, davon 4.933.389 unveränderte Duration-Facts.
Rest: **180.283 mehrdeutige Aliasgruppen / 1.217.274 Zusatzzeilen**.
Diese Zahlen sind keine ausgeführten Migrationsresultate und keine Page-/GB-Prognose.

Ein globaler partieller UNIQUE-Index für alle NULL-start-Facts bleibt bei solchen
Restgruppen unmöglich. Kein Key um Symbol erweitern und keine Ausnahme/Skip-Logik
für den Task-8-Writer. Ein Companyfacts-Paket, das eine ungelöste Gruppe enthält,
bleibt Fail-closed und kann die ganze Company-Transaktion zurückrollen; eine
Teilbereinigung verspricht daher keine uneingeschränkt erfolgreiche SEC-Ingestion.

Für die spätere Teilumsetzung muss das Ausführungsmanifest ausschließlich vollständige
SAFE-Gruppen enthalten, mit ihrer Source-/Metadata-Signatur, ursprünglichem gewähltem
ID-Anker, Plan-/Reader-/Config-Version und Snapshot-Hash. Ein Teilbestand darf nicht
anhand aktueller Symbole, Preis-/Scoreergebnissen oder gewünschter Performance gewählt
werden. Bei Source-/Code-/Schemaänderung, unbekanntem Consumer oder neuem Konflikt
muss Klassifikation/Beweis erneut geprüft werden; andernfalls NO_GO.

Alle Backup-, Platz-, WAL-/Recovery-, Provenienz- und Rollback-Gates des folgenden
Plans gelten fort. Historische Forschungsartefakte/Journale bleiben unverändert.
Der separate Alias-/Reader-/Schema-Entwurf ist nur für eine spätere **vollständige**
Migration erforderlich; er darf nicht still zur Ausweitung der sicheren Teilmenge dienen.

---

## Erhaltener ursprünglicher Task-8B-Entwurf

# F01 Fact Deduplication — Conditional Migration Design (Phase B)

## Status und Auslöser

**Nur Design. Keine Migration, Datenbankkopie, Bereinigung oder Constraint-Anlage ausgeführt.**
Ausgelöst durch Phase A **A_CHAMPION_IMPACT**: synthetisch bestätigte Änderung
produktiv berechneter Fundamentalwerte/F-Scores durch identische Wiederholung sowie
bestätigte Ingestion-/Readiness-Fehlerkette. Eine historisch falsche reale F-Auswahl
ist nicht nachgewiesen; die begrenzte reale Stichprobe war wirtschaftlich gleich.
Siehe `reports/f01_duplicate_champion_impact_audit.md`.

Basis-HEAD: `917172886eb0954446540afb2e794b3304a59af3`.
Dieser Plan genehmigt keine Ausführung. Er ist für eine vollständige Migration
**noch nicht ausführungsreif**, insbesondere wegen 180.283 Source-Symbol-Variantgruppen.
Eine spätere Umsetzung braucht einen eigenen Auftrag und eine geprüfte Ausführungsfreigabe.

## B1 — Identität, Survivor-Semantik und Grenzen

Unveränderte fachliche Identität:
`(cik,metric,tag,unit,period_start,period_end,filed,accession_number,frame)`;
NULL-start ist ausdrücklich gleich NULL-start, niemals ein erfundenes Datum.
Fehlende Accessions/Frames bleiben wie im Writer auf leeren String normalisiert.
Duration-Starts, Filingdaten, Accessions, Units, CIKs, Tags und Frames nicht zusammenführen.
Taxonomy/Source-Symbol gehören bislang nicht zum DB-Key; keine verdeckte Änderung
dieses Vertrags im Rahmen einer Bereinigung.

### Warum MIN(id) und MAX(id) allein nicht genügen

Die produktiven Reader sortieren `filed,period_end,id`, Forward zusätzlich nach
verifizierter CIK-Mitgliedschaft. Bei sonst gleichem Zeitraum/Filing gilt:

| Verbraucher | Bestehende Tie-Semantik | Bedingt zulässiger Anker |
| --- | --- | --- |
| Cash, Current Assets/Liabilities, Total Assets/Equity | max(period_end,filed), erste tied row | Frühester ursprünglicher Reader-Auftritt je vollständiger Identität und Reader-Mitgliedschaft. |
| Direkte Total-Debt-Repräsentation | Erste tied row | Frühester Anker. Konkurrenz zwischen direkter und komponentenweiser Repräsentation unverändert lassen. |
| Debt Current/Noncurrent | Stabiles filed-Sortieren, dictionary overwrite, letzte tied row | Spätester ursprünglicher Auftritt je Identität und Reader-Mitgliedschaft. |
| Shares Outstanding | period_end, filed, Taxonomy-Priorität, Accession, Tag, Wert; keine Summierung | Nur bei vollständig gleicher relevanter Metadaten-/Wertsemantik beliebiger gleichwertiger Treffer; deterministisch frühester Anker. Alters-/Unit-Grenzen erhalten. |
| NULL-start-Flow-Einträge | discrete_quarters schließt sie aus | Frühester Anker nur nach Prüfung sonstiger Reader-/Diagnostik-/Alias-Verträge. Keine Startperiode nachtragen. |
| Korrekte Duration-Facts | Bestehender UNIQUE-Key | Unverändert; keine F01-Bereinigung. |

**Begründung:** Bei identischen Identity-/Metadatenwerten beseitigt der früheste
Anker keinen früheren relevanten Auftritt; der späteste Anker beseitigt keinen
letzten relevanten Auftritt. Damit bleibt der Gewinner gegenüber konkurrierenden
anderen Tags/Accessions/Units mit gleichem Perioden-/Filingdatum erhalten.
Da Filed, Periodenende und Identitätsmitgliedschaft unverändert sind, gilt die
Ordnungsargumentation auch für PIT-Cutoffs. Sie ist für jede Verbraucherfunktion
und jeden Readerfilter zu prüfen, nicht als allgemeines SQL-MIN/MAX-Axiom zu behandeln.

Die neuen Fixturetests belegen explizit:
- Pauschal frühester Survivor kann Debt-Werte und F-Scores ändern.
- Pauschal spätester Survivor kann Cash ändern.
- Metricspezifische Anker erhalten im gemischten Gegenbeispiel beide Werte und F-Ausgaben.

Diese Ankerregel ist ein **Designkandidat**, kein implementierter Migrationscode.
Eine technische ID-Neuvergabe oder VACUUM-basierte implizite Reihenfolge darf die
bewiesene relative Ordnung nicht ersetzen. Rowid-basierte externe Referenzen separat prüfen.

### Metadaten-/Source-Alias-Gate — derzeit STOP

Census: 3.058.649 Gruppen sind numerisch und in Nicht-Key-Metadaten gleich;
180.283 Gruppen haben unterschiedliche Source-Symbole. Keine Taxonomy-, Fiscal-
oder Form-Varianten und keine numerischen Wertkonflikte gefunden. Vor Ausführung
neu prüfen; der Bericht ist keine dauerhaft gültige Constraint-Freigabe.

Eine kanonische Zeile pro CIK-/Filing-Identität kann nicht gleichzeitig mehrere
Legacy-Symbol-Mitgliedschaften in derselben bisherigen Tabellenrepräsentation bewahren.
MIN/MAX oder Auswahl des heutigen Companies-Symbols verliert historische Source-
Mitgliedschaft. Der Fixturetest belegt den Verlust der Cash-Verfügbarkeit beim
Legacy-Ticker, obwohl der Forward-CIK-Reader wirtschaftlich gleich bleibt.

**Unzulässiger Shortcut:** den Key einfach um symbol erweitern. Das widerspricht
dem jetzigen NULL-sicheren Writer und lässt dessen mehrdeutige CIK-Identität bestehen.
Ebenso unzulässig: Source-Ticker pauschal umlabeln, unaufgelöste CIK-Konflikte mergen
oder Legacy-Research still auf Forward-CIK-Semantik umstellen.

Vor vollständiger Migration ist eine separate Architektur-/Kompatibilitätsentscheidung nötig:

1. **Bevorzugter vollständiger Entwurf zur weiteren Prüfung:** eine eindeutige
   kanonische Fact-Relation plus Source-Alias-/Occurrence-Relation. Diese hält pro
   ursprünglichem Source-Symbol die Mitgliedschaft und frühesten/spätesten Reader-
   Anker; Werte/Metadaten bleiben an kanonische Identität gebunden. Legacy-Projektion
   verwendet Symbol-Mitgliedschaft und passenden Anker, Forward liest kanonische CIK-
   Facts mit global passendem Anker. Quelle, CIK und legitime Amendments bleiben trennbar.
   Reader-/Writer-Anpassungen, Schema-/View-Verträge und Umgang mit neuen Source-
   Labels brauchen einen eigenen Auftrag; sie sind **nicht** Teil dieses Audits.
2. **Alternative mit getrennten Datenversionen:** Originalsnapshot für historische
   Legacy-Replays erhalten, neue kanonische DB ausschließlich für ausdrücklich
   versionierte Forward-Nutzung. Das erhält Originalartefakte, ist aber keine
   Gleichheitsgarantie für Legacy-Abfragen auf der neuen DB. Pfad-/Dataset-Routing
   und zukünftige Ingestion benötigen ebenfalls eine ausdrückliche Entscheidung.
3. Eine Teilbereinigung ausschließlich freigegebener gleicher Gruppen kann ein
   späterer gestufter Vorschlag sein; sie beseitigt nicht alle F01-Mehrdeutigkeiten
   und erlaubt keinen globalen NULL-safe UNIQUE-Index, solange Aliasgruppen verbleiben.
   Sie erfüllt damit ausdrücklich noch nicht die vollständigen Abnahmekriterien.

Ohne Entscheidung und nachgewiesene Legacy-/Forward-Kompatibilität: **keine vollständige
Migration empfehlen oder starten**. Der Task-8-Fail-closed-Writer bleibt unverändert.

### References und Fingerprints

Im geprüften Schema wurden keine Foreign Keys auf fundamental_facts gefunden.
Das schließt externe Artefakt-/SQL-/Cache-Referenzen auf Fact-IDs nicht aus. Ein späterer
Implementierungsschritt muss SQL-Verbraucher, View-/Trigger-Abhängigkeiten und ID-
Referenzen erneut inventarisieren; Anker-/ID-Mapping verlustfrei im Migrationsmanifest halten.

Raw-Fact-Listen, Source-/Input-Digests und ID-basierte Discovery-Fingerprints werden
bei Deduplizierung erwartbar anders. **Keine Hash-Gleichheitsgarantie.**
Alte Research-Manifeste, Exporte, Journale und Input-Artefakte bleiben unverändert;
neue Datenversion erhält neue Dataset-/Provenienzidentität. Journal-Drift-/Replay-
Prüfungen nicht umgehen. Wirtschaftliche Output-Gleichheit und technische
Provenienzänderung getrennt abnehmen.

## B2 — Sichere spätere Ausführung und Recovery

### Gate 0 — noch fehlende Freigaben

- Alias-/Dataset-Routing und sämtliche Reader-Survivor-Verträge schriftlich entscheiden.
- Verbraucherbeweise und Offline-Fixtures gegen konkreten Migrationscode reviewen.
- Immutable Source-Manifest, erwartete Counts und erlaubte wirtschaftliche Änderungen festlegen.
- Erst danach Copy-only-Dry-Run und später gegebenenfalls Cutover separat genehmigen.

### Backup und isolierte Arbeitskopie

1. Alle realen Writer stoppen und aktive Leser/WAL-Verhältnisse erfassen.
   Kein einfaches Kopieren nur der Hauptdatei bei aktivem WAL.
2. Später genehmigtes konsistentes SQLite-Online-Backup oder verifiziertes Offline-
   Backup **außerhalb des Audits** erzeugen. Alle committed WAL-Daten müssen im
   Snapshot enthalten sein. Original inklusive seiner Artefaktreferenzen behalten.
3. Backup durch Öffnen im Read-only-Modus, `PRAGMA integrity_check`, Schema-/Key-
   Zähler und kryptografischen Snapshot-Hash verifizieren; Restore-Probe durchführen.
4. Eigene Arbeitskopie vom verifizierten Backup erstellen. Alle Bereinigungs-,
   Schema- und Constraint-Schritte ausschließlich dort ausführen. Produktionsdatei
   unverändert, bis ein gesonderter Cutover genehmigt ist.

### Speicher und Transaktionsgröße

Audit-Snapshot: Hauptdatei 10.501.169.152 Byte, WAL im vorliegenden Bestand ohne
committed Zusatzinhalt laut vorherigem Task-8-Inventar; Sidecarzustand vor Ausführung
neu prüfen. Gemessener freier Platz im Audit: 203.612.635.136 Byte auf dem abgefragten
Volume. Das ist ein Zeitpunktwert, keine Ressourcenreservierung. Tatsächlichen
System-Temp-Pfad und dessen Volume separat prüfen; nicht aus dem Workspace-Laufwerk
auf alle Temp-/Backup-Volumes schließen.

Konservative Planung vor Copy-only-Rehearsal: mindestens vier bis fünf Hauptdateigrößen
zusätzlich für Backup, Arbeitskopie, WAL/Journal, Indexaufbau/Sortier-Scratch, ggf. neue
kanonische Datei; also grob 42–53 GB plus Sicherheitsreserve und separate laufende
WAL-/Backup-Budgets. Das ist eine Kapazitätsannahme, kein gemessener Ergebnis-DB-Umfang.
Keinen erwarteten Byte-Gewinn allein aus der Anzahl gelöschter Zeilen ableiten.

Keine monolithische, ungeprüfte Transaktion über zehn Millionen Zeilen im realen
Bestand. Auf der Kopie entweder deterministischer Neubau aus immutable Quelle oder
kontrollierte, begrenzte Batches mit persistentem Ledger verwenden. Ledger protokolliert
Source-Hash, Plan-/Schema-Version, Schlüsselbereich, erwartete Gruppen/Zeilen,
gewählte Source-Anker und verifizierte Commits. Leser sehen die Arbeitskopie nie
während Zwischenzuständen; Source-Daten werden nicht parallel verändert.

### Crash und Wiederanlauf

- Original/Backup niemals als Resume-Ziel verwenden.
- Wiederanlauf nur mit identischem Source-Snapshot-Hash, Plan-Version und vollständig
  verifiziertem Batch-Ledger; keine Live-Datenbank als stillschweigend geänderte Quelle.
- Uncommitted Batch über SQLite-Recovery zurückrollen. Committed Batches anhand
  Ledger/Counts prüfen; nicht erneut ohne Idempotenz-/Schlüsselbereichsprüfung anwenden.
- Bei fehlendem/inkonsistentem Ledger, abweichendem Hash oder Integrity-Fehler:
  Arbeitskopie verwerfen und aus verifiziertem Backup neu aufbauen.
- WAL-/SHM-Dateien nicht während aktiver Verbindungen löschen/umbenennen. Vor
  abschließendem Konsistenz-/Hashvergleich alle Verbindungen koordiniert schließen;
  Checkpoint nur im später genehmigten Ablauf und nur an Kopie/isoliertem Ziel.

### Constraints gegen Wiederholung

Nach vollständiger, freigegebener kanonischer Reconciliation: partieller UNIQUE-
Index auf `(cik,metric,tag,unit,period_end,filed,accession_number,frame)`
`WHERE period_start IS NULL`. Bestehenden Duration-UNIQUE-Key erhalten.
Bei Aliasnormalisierung gehört dieser Index auf die **kanonische** Relation,
nicht auf eine mehrfach projizierte Legacy-View. Neue Writes müssen den genehmigten
kanonischen/Source-Alias-Vertrag atomar einhalten. Task-8-Writer bleibt bis dahin
als Anwendungsschutz aktiv; kein automatisches Anlegen eines Index in initialize.

### Cutover und Rollback

Cutover nur im Wartungsfenster nach vollständiger Abnahme, mit geschlossenen Writer-
und Reader-Verbindungen, explizit versionierten DB-Pfaden und unverändertem Originalbackup.
Nach Cutover gezielte Read-only-PIT-/Constraint-Prüfungen sowie genehmigte synthetische
Ingestion auf einer weiteren Kopie, keine implizite Live-Test-Ingestion.
Rollback: neue Writer stoppen, betroffene Verbindungen schließen, verifizierten
Originalsnapshot inklusive konsistenter WAL-Verhältnisse wieder aktivieren.
Zwischenzeitliche neue Eingaben separat sichern; kein stiller Verlust und keine
Vermischung von Datenversionen. Rollback erhält alte Duplikate; F01-Fail-closed-
Verhalten und dessen operative Grenzen kehren damit zurück. Code-Downgrade wäre
keine Datenreparatur und kann die ursprüngliche Duplikationsursache reaktivieren.

## B3 — Abnahmekriterien vor jeder Produktionsfreigabe

| Kriterium | Konkreter Nachweis |
| --- | --- |
| Keine mehrdeutigen F01-Identitäten | Vollständiger NULL-sicherer Census auf kanonischem Ziel: 0 duplicate groups. Bei unverändertem Quellumfang wäre die kanonische Grundzahl 18.698.090 − 10.368.182 = 8.329.908 Facts; Aliaszeilen separat zählen. Keine Phantomgleichheit unterschiedlicher CIKs/Units/Accessions. |
| Metadata-Quarantäne | Alle 180.283 Symbol-Variantgruppen einem genehmigten Alias-/Versionsvertrag zugeordnet; 0 unbehandelte Wert-/Taxonomy-/Fiscal-/Form-Konflikte. Bei neuer Abweichung STOP. |
| Amendment-PIT | Unveränderte Auswahl vor, am und nach Filed-Grenzen; Accessions und Units erhalten; keine Nutzung einer späteren Filing-Version vor Verfügbarkeit. |
| Reader-Ties | Cash/Balance/Direct Debt erster, Debt-Komponenten letzter relevanter Auftritt, Shares-Prioritäten und Source-Mitgliedschaft erhalten. MIN/MAX-Gegenbeispiele müssen bestehen. |
| Gleichheit der erforderlichen Werte | Für Legacy und Forward mit separatem Contract: gleiche ausgewählte TTM-/Balance-/Fundamentalwerte und gleiche Missingness bei allen getesteten As-of-/Perioden-/Unit-/CIK-Grenzen. Grenzproben an jeder untersuchten Filing-Änderung, nicht nur neueste Scores. |
| F-Score/Eligibility/Ranking | Bei gleichen wirtschaftlichen Inputs und Peer-/Technical-Cross-Sections identische Quality/Valuation, Faktorverfügbarkeit, F-Eligibility, Tie-Reihenfolge und Rank-1. Keine Veränderung der eingefrorenen Ökonomie. |
| Ingestion | Wiederholter kompletter Batch, Neustart, gleiche-Identität-Korrektur, unterschiedliche Amendments/Units/Perioden und konkurrierende Writer auf isolierter Kopie korrekt/idempotent; kein Alias-/CIK-Verlust. |
| Integrität | PRAGMA integrity_check = ok auf Ziel/Backup, Foreign-Key-/Referenzprüfungen, Counts/Hash-/Anker-Ledger konsistent; keine unerwarteten Nicht-Fact-Tabellenänderungen. |
| Artefakte/Provenienz | Historische Forschungsartefakte und Journale byte-identisch unverändert. Neue Dataset-Fingerprints ausdrücklich anders/versioniert; kein Umschreiben alter Manifeste zur Erzeugung vermeintlicher Gleichheit. |
| Rollback | Restore- und Crash-/Resume-Proben anhand des geplanten Ledgers bestanden, einschließlich Schutz zwischenzeitlicher Eingaben. |

### Welche wirtschaftlichen Werte dürfen/müssen sich ändern?

Phase A hat eine **synthetische** Reihenfolgeabhängigkeit bestätigt, aber keinen
konkret fachlich falschen realen Geldbetrag identifiziert. In der synthetischen
A/B/A-Probe würde blindes MIN-Deduplizieren den aktuell ausgewählten Debt-/Quality-/
F-Wert ändern; das ist eine **zu verhindernde Migrationsabweichung**, keine genehmigte
wirtschaftliche Korrektur. Es gibt daher derzeit keinen realen Wert, der im Ziel
berechtigterweise zwingend geändert werden müsste.

Sollten spätere reale Selection-Proben falsche Werte oder fachlich erforderliche
Amendment-Prioritäten bestätigen, ist vor Ausführung eine separate Korrekturliste
mit betroffenen Identitäten/As-of-Intervallen, Sollwert-Evidenz und unvermeidbaren
Score-/Rankingänderungen nötig. Dann ist Gleichheit gerade **nicht** das richtige
Abnahmekriterium für diese ausdrücklich genehmigten Korrekturfälle. Keine solche
Ausnahme wurde hier erfunden oder freigegeben.

## Empfohlener nächster Schritt

Gezielten Folgeauftrag für Source-Alias-/Reader-Kompatibilität und metricspezifische
Survivor-Spezifikation erstellen. Danach implementierungsbezogenes Offline-Rehearsal
auf kleinen Fixtures; erst mit erfüllten Stop-Gates eine isolierte Copy-only-Probe
freigeben. Dieser Audit startet weder diese Probe noch eine reale Migration.
