# F01 · Canonical Deduplication und PIT

**Canonical Deduplication bedeutet nicht, alle ähnlichen Datenzeilen zu löschen.** Die Eindeutigkeit gilt pro vollständigem Fact-Key. Zugelassene Quellenbeobachtungen und Revisionen bleiben erhalten. Emittenten, handelbare Securities und historische Stände brauchen ihre eigenen Identitäten und Zeitgrenzen.

## Persistenzvertrag

| Vertrag | Implementierung |
| --- | --- |
| Fact-Identität | Geordnetes `KEY = (cik, metric, tag, unit, period_start, period_end, filed, accession_number, frame)`. Symbol und Taxonomie sind Source-Membership, Wert, Geschäftsjahr, Geschäftsperiode und Formular sind revidierbarer Inhalt. Der bestehende Storage-Adapter normalisiert unter anderem fehlende Accession/Frame. |
| Instant und Duration | Partielle Unique-Indizes `canonical_instant` für `period_start IS NULL` ohne `period_start` im Index und `canonical_duration` für `period_start IS NOT NULL` mit dem vollen Key. |
| Occurrences | Primärschlüssel `id`, bei importierten historischen Zeilen zusätzlich eindeutige `original_id`. Der Online-/Research-Writer behandelt `(fact_id, symbol, taxonomy)` als Membership und kann eine spätere explizite Legacy-Admission dokumentieren. Neue Memberships verändern den ursprünglichen Forward-Anker nicht. Mehrere reale Originalbelege mit sonst gleicher Membership bleiben über das Archiv, die Original-Shards und deren Dokument-/Kontext-/Quellenprovenienz identifizierbar. Die Tabelle `source_occurrence` selbst modelliert keine beliebige Wiederholungsanzahl gleicher Memberships beim aktuellen Writer. |
| Revisionen | `(fact_id, version)` bewahrt Änderungen an `value, fiscal_year, fiscal_period, form`. Die versionierten Views wählen die letzte Revision bis zur gebundenen Dataset-Version. Eine andere Accession oder ein anderes Filed-Datum bildet eine eigene Identität. |
| Konflikt | Abweichender Inhalt für denselben Key **innerhalb eines Requests** führt zu `UNRESOLVED_CONTENT_CONFLICT_IN_REQUEST` und Rollback. Der Bulk-Pilot klassifiziert seine gleichschlüsseligen Konfliktgruppen vor dem Writer und schreibt Quarantänebelege. Ein späterer separater zugelassener Request kann eine Revision anlegen. Fachlich konkurrierende Originalkontexte bleiben beim Original-/Accounting-Review. |
| Source-Admission | Identität und CIK werden geprüft. Jede aktuelle `(symbol, taxonomy)`-Membership braucht explizite Evidenz. Neu eingeführte Facts brauchen einen Forward-Anker. Die Expansion braucht zusätzlich geprüfte Filing-Zugehörigkeit und eigene Accounting-Admission je Issuer/Filing. |
| Idempotenz | Eindeutige Receipt-ID plus Hash aus normalisierten Eingaben, Identität, Admissions, Payload-Hash und Retrieval-Zeit. Exakter Replay gibt die bestehende Version zurück. Geänderter Input unter derselben ID wird abgewiesen. Ein neuer Request kann Receipt und Version hinzufügen, auch wenn seine Facts bereits vollständig bekannt sind. |
| Atomarität | Der direkte Writer verwendet eine eigene Transaktion oder Savepoint. Der Persistence-Adapter archiviert den Envelope separat, übernimmt eine Write-Transaktion und reconciliert einen unsicheren Commit über den Receipt. Ein archivierter Envelope alleine belegt keinen veröffentlichten Import. |

[Schema, Views und Writer](../src/trading_system/data/canonical_facts.py) · [Envelope und Persistence](../src/trading_system/data/sec_persistence.py) · [DataSynchronizer](../src/trading_system/data/sec_sync_persistence.py) · [Bulk-Quarantäne](../scripts/import_f01_sec_bulk_pilot.py)

## Point in time

E.13 schneidet Canonical-Kandidaten mit exakt bezeugten Original-Facts. Die Auswahl wird auf einen zeitzonenbewussten Entscheidungspunkt, Periodenkontext und die belegte Filing-Verfügbarkeit begrenzt. Widersprüchliche Amendments brauchen ihre eigene Acceptance und eine belegte Supersession. Fehlende Metriken bleiben fehlend oder erhalten einen begründeten Status. Ausschließlich die drei gebundenen E.12-Regeln dürfen im ursprünglichen genehmigten Scope nach ihren fachlichen Bedingungen ergänzen. Eine ältere Dataset-Version ist zusätzlich nötig, wenn der geprüfte Datenstand von neueren Revisionen abgegrenzt werden soll. Die SQL-Views bilden keine vollständige Original-PIT-Zulassung ab. [E.13 Reader](../src/trading_system/fundamentals/canonical_research.py) · [E.13 Audit](../reports/f01_canonical_research_reader_audit.md)

Die Expansion erbt diese Regeln. Eine CIK identifiziert die Reporting Entity. Historisch handelbare Security, Share Class und Symbol-Epoche brauchen eigene Belege. Preise binden Provider, Feed, Adjustment und Session. Später bekannt gewordene Corporate Actions und unbekannte oder fehlende Sessions brauchen separate Behandlung.

## Offline-Prüfung

Die Regressionen prüfen gleiche Keys, echte Memberships, getrennte Filings/Amendments, getrennte CIKs und Periodentypen, Dataset-Versionen, Membership-Sperre, Quarantäne, atomaren Fehler und Resume, Missing-Verhalten und E.13-PIT. D.6/D.8-Tests prüfen Identity-Epochen und die Separierung von Feed und Adjustment. [Writer-Regressionen](../tests/test_f01_canonical_ingestion.py) · [Persistence](../tests/test_sec_persistence_integration.py) · [E.13](../tests/test_canonical_research_reader.py) · [Marktvarianten](../tests/test_f01_alpaca_historical_sip_coverage.py)

Die [Duplicate Assurance](../scripts/validate_f01_canonical_assurance.py) zählt Facts, Occurrences, Revisionen, Dataset-Versionen und Receipts und prüft Key-Kollisionen, Schema, Admission-Verweise, Membership-Folge, Revisions-/Receipt-Bindung und Foreign Keys. Optionale PIT-Zeugen prüfen einen bereits identifizierten `fact_id`, die gebundene `dataset_version`, `accession`, gespeicherten `value` als Text sowie `accepted_at` und `decision_at`. Die Stichprobe setzt unabhängig geprüfte Zeugen voraus. Vollständige E.13-Zulassung und historische Feed-/Security-/Accounting-Prüfungen bleiben eigenständige Evidence-Gates. Ein grüner Storage-Report allein belegt sie nicht.
