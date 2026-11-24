# F01 · Historical 50-Issuer Research Preparation

**Stand 24.11.2026.** Der Offline-Preflight ist ausführbar. Der erste Review-Batch enthält 20 Accessions. Acquisition, Canonical-Import und Trading bleiben gesperrt, bis ihre jeweiligen Freigaben vorliegen. [Batch-Manifest](</G:/TraWalpResearch/import-artifacts/f01-historical-50-preparation-v1/first_batch_manifest.json>)

## Gebundene Kohorte und lokaler Befund

`F01_HISTORICAL_50_RESEARCH_BATCH_V1` prüft die SHA-256-Werte beider D.6-Eingänge und gleicht alle 50 historischen Labels, CIKs und Kategorien zwischen CSV und SEC-Metadaten ab. AAPL, ACN, CAT, COST, LLY, MSFT und NVDA bleiben Kontrollen. 43 weitere CIKs stammen aus D.6. SPY wird separat als Marktbenchmark geführt. Ein expliziter CLI-Scope kann die Bindung nur einschränken. Das Datum ist das Filing-Datum, einschließlich des von D.6 abgeleiteten Fundamentalvorlaufs ab 29.12.2011 bis 20.11.2026. [Universe](../reports/f01_historical_50_pilot.csv) · [D.6 Inventar](../reports/f01_historical_50_preflight.json)

| Beobachtung | Anzahl bzw. Grenze |
| --- | --- |
| Periodische CIK-/Accession-Paare im gebundenen Zeitraum | 2.688 |
| Historische D.6-Gaps, über überlappende Zeitfenster dedupliziert | 2.352 |
| Gaps mit physisch fehlendem Originalverzeichnis im geprüften Filing-Cache | 2.352 |
| Paare mit gelistetem Cache-Dokument und Index, reguläre Datei/Größe im aktuellen Zugriff nicht bestätigt | 336 |
| Paare mit Arelle-Shard in der historischen Manifestliste und passender Dateigröße | 336 |
| Neue fachlich qualifizierte Filings oder neue vollständig zugelassene Issuer | 0 |

Die Cache-Zählung gilt für den angegebenen Cache-Root, keine systemweite Suche. Ein gelisteter Cache-Pfad bzw. eine passende Shard-Größe braucht vor Wiederverwendung eine Verifikation der Bytes gegen den jeweiligen Hash. Das 339-Filing-Manifest umfasst drei zusätzliche Einträge außerhalb des D.6-Fensters. Der Arelle-Beleg für 2.47.5 listet 167 gebundene URLs. Er belegt keinen vollständigen DTS je neuem Filing. Im bestehenden Audit fehlt das historische 2009er DTS bei drei CAT-Instanzen. Diese Fälle liegen außerhalb des Batch-Fensters. [Filing-Gap-Inventar](</G:/TraWalpResearch/import-artifacts/f01-historical-50-preparation-v1/filing_gap_inventory.csv>) · [Arelle-Findings](../reports/f01_extended_accounting_evidence_qualification_audit.md)

## Erster Review-Batch

Die Regel bevorzugt überprüfbare Originale, SEC-Metadaten mit Acceptance, Akten in der Nähe der Jahresgrenze 2016 und bei Gleichstand Jahresberichte. Sie streut die Auswahl über bis zu fünf D.6-Kategorien bzw. Emittenten und zieht dann pro Emittent reihum. Fehlende bzw. nur historisch bekannte Handelbarkeit bleibt im Manifest sichtbar. Scores und Renditen gehen nicht ein. Die Auswahl kombiniert NKE als datierten Kandidaten, BBBY als Lifecycle-/Exit-Fall, HEI als Klassenfall, ALOT als außerhalb der damaligen aktuellen Quelle geführten Kandidaten und BG als Reporting-Entity-Fall. Der Entwurf enthält 10-K, 10-Q und ein BG-Amendment. Die ursprüngliche BG-Accession und Supersession müssen im Review unabhängig belegt werden. [Ausgewählte Accessions und Blocker](</G:/TraWalpResearch/import-artifacts/f01-historical-50-preparation-v1/first_batch_accessions.csv>)

Auf Basis der D.6-Pilotmittel liegen die geplanten 20 Originale bei rund 159 MB und die Extraktionsbelege bei rund 324 MB. Staging, Arbeitskopien, offene Taxonomien, Request-Archiv, SQLite-Sidecars, Reserve und Backup kommen hinzu. Die gemessene freie Kapazität steht im Batchbericht. Die Schätzung ist keine Kapazitätsfreigabe. [Kapazität](</G:/TraWalpResearch/import-artifacts/f01-historical-50-preparation-v1/first_batch_capacity.json>)

## Offline-Betrieb

```powershell
$F01 = 'G:/TraWalpResearch/sec-originals/filing-evidence'
& ./.venv/Scripts/python.exe -B scripts/prepare_f01_historical_50_batch.py `
  --cache $F01 --cache-manifest "$F01/f01-extended-cache-20261122-124810.json" `
  --extraction-manifest "$F01/f01-extended-filing-evidence-20261122-124810/manifest.json" `
  --taxonomy-inventory 'G:/TraWalpResearch/arelle/2.47.5/taxonomy-extended-coverage-20261122-124810.json'
```

Standardmäßig schreibt das Programm JSON auf stdout. `--plan --output <neues-absolutes-f01-historical-50-Verzeichnis>` publiziert ausschließlich neue Dateien. Das Manifest wird als Abschlussmarker zuletzt angelegt und bindet CSV-/JSON-Inhalte mit SHA-256. Der Plan-Fingerprint bindet den Kandidaten-Code, die Eingangsmanifeste, die ausgewählten CIKs, den Zeitbereich und die Accessions. Flüchtige Kapazität fließt nicht in die Resume-Identität ein. Der Preflight führt keine SQLite-Operation, keinen Download und keine Arelle-Extraktion durch. [Preflight-CLI](../scripts/prepare_f01_historical_50_batch.py)

## Lineage und Aufnahmebedingungen

Das vorgesehene Ziel ist `G:\TraWalpResearch\research-databases\f01-sec-isolated-research-historical50-v1\f01-sec-candidate.sqlite3`. Der bestehende Bootstrap erzeugt später seine eigene Dataset-ID in einem exklusiv angelegten Ordner. Das separat zu reviewende Bootstrap-Manifest muss in `research_scope` auf die Hashes für Universe und Source-Generation sowie den genehmigten Batch verweisen. Der Bootstrap bindet das Manifest und den bei Ausführung erneut zu prüfenden Code-Fingerprint. Die vorhandene V2-Pilot-Spec ist weiterhin die Zulassung für genau sieben Emittenten. Der nächste Task braucht eine eigene versionierte Expansion-Admission und einen Filing-Importpfad, der den bestehenden Canonical-Writer und seine Atomarität wiederverwendet. [Bootstrap](../src/trading_system/data/canonical_bootstrap.py) · [Canonical-Vertrag](f01_canonical_deduplication_contract.md)

Pro Filing gehören Submissions-CIK, Accession, vertraute Original-Hashes, Acceptance, Arelle-Runtime und vollständiger DTS, Auswahlkontext, Dimensionen, Units, Review und Accounting-Admission zusammen. Vor Persistenz braucht es eine unveränderliche Source-Generation mit expliziten zulässigen Memberships. Der Receive-/Resume-Pfad bindet den konkreten Filing-Manifest-Hash, Request-Envelope, Zulassung und Ziel-Lineage. Erst nach Commit und Receipt ist die Dataset-Version sichtbar. E.13 wählt aus separat qualifizierten Witnesses. Für die 43 zusätzlichen Emittenten sind Issuer Shares, OCF, CapEx/PPE, Debt/Leases, EBITDA, ROIC und Consolidated-versus-Parent je Filing offen. Die Accounting-Matrix bindet die bekannten Siebener-Grenzen. Fehlende Werte werden weiterhin als fehlend behandelt. [Accounting-Matrix](</G:/TraWalpResearch/import-artifacts/f01-historical-50-preparation-v1/accounting_readiness.csv>)

## Markt und Security

D.8 dokumentiert im jüngeren Archiv SIP/all sowie Vergleichsstichproben mit Raw, Split und All. Für SPY sind 320/1.732 Sessions im Forward-Archiv und 1.570/1.732 in Legacy im Zeitraum 2020–20.11.2026 vorhanden. In beiden Beständen fehlen 1.006 Sessions 2016–2019; zusätzlich fehlen die 300 Pre-2016-Warm-up-Sessions. Legacy hat keine gleichwertige Bar-Provenienz. Vollständige Epochen für Klassen, Listing/Delisting, Symbol-Reuse und Exits bleiben offen. AAPL-Split und TWTR-Exit liefern einzelne Witnesses. Massive liefert Referenzpunkte und keine durchgehende SIP-Ersatzserie. Die Research-Fundamentals können unabhängig entstehen. Trading benötigt den separat qualifizierten PIT-Join zu Marktpreis, Action und handelbarer Security. [D.8 Marktvertrag](f01_alpaca_historical_sip_coverage.md)

**Nächster Task · 8F.3D.12 – Historical 50-Issuer SEC Acquisition & Canonical Batch Ingestion.** Den Review-Entwurf auf einen ausdrücklich autorisierten Teilumfang begrenzen, Source- und Taxonomie-Closure prüfen, die Expansion-Admission und den Filing-Importpfad abnehmen und erst danach in einer neuen isolierten Research-Lineage persistieren.
