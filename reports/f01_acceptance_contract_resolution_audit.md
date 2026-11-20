# F01 Migration Acceptance Contract Resolution

Task 8F.2D; Repository-HEAD: `c374bdb6d8b05eb5c7a2d1685aa587604904701d`.

**Status dieses Dokuments: VERTRAGSVORSCHLAG – NICHT GENEHMIGT ODER AKTIVIERT.**
Die gespeicherten G4-/Supplement-Status bleiben `INSUFFICIENT_EVIDENCE`.
Kein neues `REHEARSAL_GO`, keine operative Freigabe und kein Cutover.

## 1. Ergebnis der getrennten Bewertung

| Dimension | Anhand der vorhandenen Evidenz | Was weiterhin offen ist |
| --- | --- | --- |
| **A. STORAGE_AND_SEMANTIC_PARITY** | Für den gebundenen realen Snapshot belegt: vollständiger Speicher-/Stream-Erhalt, unveränderte Reader-/Accounting-Ergebnisse, vollständige Source-/Work-Vergleichsergebnisse für 5.913 Unternehmen einschließlich identischer Ablehnungen. | Diese fachliche Feststellung ist kein nachträglich aktiviertes G4-PASS. Sie umfasst weder historische Ownership-Wahrheit noch produktive Ingestion, Recovery oder aktuelle Handelsbereitschaft. |
| **B. QUALIFIED_FORWARD_RANKING_PARITY** | Identische Forward-Berechnungen, identische Qualification und identische Sperrung belegt. | Ein tatsächlich qualifiziertes Forward-Ranking ist **nicht nachgewiesen**: 334 rank-relevante Kandidaten sind auf beiden Seiten ungelöst. Keine gültige Forward-Rank-1-Evidenz. |
| **C. HISTORICAL_IDENTITY_EVIDENCE** | Vollständiger realer Provenienz-Census und synthetische Reader-/Reuse-Regression belegt. | **Keine autoritativ belegten historischen Identity-Epochen**, kein realer verifizierter Ticker-Reuse-Witness. Synthetische Evidenz ist kein historischer Tatsachennachweis. |
| **D. OPERATIONAL_CHAMPION_READINESS** | Aus A lässt sich keine Betriebsfreigabe ableiten. Die unveränderte Qualification sperrt den untersuchten Forward-Querschnitt. | Nicht freigegeben: B offen, produktive Canonical-SEC-/DataSynchronizer-Integration unbewiesen, G5/G6 im geprüften Ergebnisbestand nicht abgeschlossen, aktuelle Readiness und produktiver Cutover nicht geprüft. |

**Fachliche Empfehlung:** A eigenständig anerkennen können, ohne B, C oder D als
erfüllt auszugeben. Dafür ist eine explizite versionierte Vertragsentscheidung
erforderlich. Dieses Dokument schlägt sie vor; es ändert keinen bestehenden Gate.

## 2. Untersuchungsgrenzen und verifizierte Artefakte

Es wurden ausschließlich vorhandene JSON-/Markdown-/JSONL-Artefakte und Quelltexte
statisch ausgewertet. Die äußeren Datei-Seals und inneren JSON-Fingerprints von
Vorbereitung, G3, G4 sowie beiden Supplements wurden anhand dieser Berichtsdateien
überprüft. Die Source-/Work-Kontext-Fingerprints des Ranking-Supplements wurden
gegen dessen gespeicherte vollständige Vergleichsevidenz geprüft. Die
Manifest-/G3-/G4-/Work-Bindungen beider Supplements stimmen überein.

Beim statischen Abgleich für 8F.2D stimmten alle 18 versiegelten
Berichtsdateien sowie die zehn in den drei Additions-Reviews aufgeführten
Dateihashes. Der Auditbericht selbst liegt außerhalb dieser Seals.
Die bestehende Regel `reports/*` in [.gitignore](../.gitignore) erfasst ihn.
Der Bericht bleibt ein lokales Ergebnis. Eine Änderung der Ignore-Regel
würde den Git-Drift-Gate des Rehearsals auslösen, weil sie eine bereits
vorhandene Datei verändert.

**Keine Datenbank wurde geöffnet, gehasht, kopiert oder migriert.** Keine Ranking-,
Census-, Test-, Backtest-, Sync- oder Shadow-Ausführung. Keine produktive
Code-/Config-Änderung und keine Änderung vorhandener Reviews, Berichte oder Seals.
Die Auswertung hat keine SQLite-Verbindung und keinen Import produktiver
Prüfskripte verwendet. Neu gehasht wurden ausschließlich vorhandene Berichtsdateien
zur Seal-/Fingerprint-Prüfung, keine Datenbankdateien.

Die Datenbank-Frische und ein heutiger Runtime-Zustand wurden damit **nicht neu
bestätigt**. Aussagen über unveränderte Datenbanken beziehen sich auf die in den
versiegelten Läufen dokumentierte Bindung und deren `references_unchanged`-Evidenz.
Vor einer späteren schreibenden Probe ist die Dateibindung erneut zu prüfen.

### 2.1 Belegkette

Gemeinsame Basis:
`reports/f01_copy_rehearsal/20261115_8f2a/`.

| Artefakt | Ergebnis / gespeicherter Fingerprint |
| --- | --- |
| [G0–G2-Manifest](f01_copy_rehearsal/20261115_8f2a/verified/f01_copy_rehearsal_manifest.json), `verified/seal.json` | `G0_G2_READY`; Manifest-Datei-SHA256 `722d53aa7e64e1db389c2b88ed727961a416b1334b8b0fbf8a296f2d452c9b00` |
| [G3-Bericht](f01_copy_rehearsal/20261115_8f2a/g3_runs/20261115T120802_b1cbe578e8f94f969ba6474140477dd2/result/f01_copy_migration_g3_report.json), `result_seal.json` | `G3_COMPLETE`; Ergebnis-Fingerprint `c512967c774e84f60cc85f2cbb7fac496aa4b9c2b994f68a1e8bfcdd804d7deb` |
| [Ursprüngliches G4](f01_copy_rehearsal/20261115_8f2a/acceptance_runs/2026-11-15T151756_0000_g4_747e7c788562433d969f4aca340fffaa/result/f01_migration_acceptance_g4.json), `result_seal.json` | `INSUFFICIENT_EVIDENCE`; `a10c4b3ae512c5763d298e0eade73b790aa63530745f27e276ca1d3e3e988263` |
| [Ranking-Supplement](f01_copy_rehearsal/20261115_8f2a/g4_supplement_runs/2026-11-15T175717_0000_ranking_1104d6b9c7324358bf8d89fad8ca3a42/result/f01_g4_supplement_report.json), `seal.json` | `INSUFFICIENT_EVIDENCE`; `556deadced38e7287e7658415c0d10d058c6af4cc5675234468cc3af170d4de4` |
| [Identity-Supplement](f01_copy_rehearsal/20261115_8f2a/g4_supplement_runs/2026-11-15T193911_0000_identity_41123deee1424f239adb90baf2ee22fd/result/f01_g4_supplement_report.json), `seal.json` | `INSUFFICIENT_EVIDENCE`; `8de04da946c7389620ff6a7b02889aec65e39f24a540644f93c8966ba3916680` |

Gespeicherter Work-SHA256:
`8eda288cbb94f9461e15360517ea27a0f66dab6c7578c6155ca0c0e814c2228c`.
Ursprünglicher Produktions-/Config-/Runtime-Code-Fingerprint:
`1271b3f88fb0898ec55750b4dd2b695c70dcf5d8591ed7594391b1417c23ddbe`.
Beides wurde aus Artefakten übernommen, nicht durch neue Datenbank- oder
Runtime-Prüfungen ersetzt.

Die zusätzliche ZIP-Datei neben dem Ranking-JSON gehört nicht zur maßgeblichen
Seal-Liste und wurde nicht als eigenständiger Nachweis verwendet. Im geprüften
Verzeichnisbestand liegen das ursprüngliche G4 und zwei Supplement-Ergebnisse;
kein veröffentlichter G5-/G6-Abschluss und kein separater kombinierter
G4-Abschlussbericht. Diese statische Vertragsbewertung gibt keinen solchen Lauf vor.

## 3. A – Speicher- und semantische Bestandsgleichheit

G3 bindet folgende tatsächliche Snapshot-Sollzahlen und erfolgreichen Prüfungen:

| Gegenstand | Nachweis |
| --- | --- |
| Source-Occurrences | 18.698.090, alle Original-IDs erhalten, genau eine Mapping-Zuordnung pro Original |
| Canonical Facts | 8.329.908, davon 3.396.519 Instant und 4.933.389 Duration |
| Occurrence-Typen | 13.764.701 Instant und 4.933.389 Duration |
| Source-Memberships / Symbol-Taxonomy-Memberships | jeweils 8.617.872 |
| Aliasgruppen | 180.283 |
| Canonical-Inhaltskonflikte | 0; dies ist keine Aussage, dass sämtliche Identity-Zuordnungen geklärt sind |
| Vollständige Streams | geordnete Legacy-/Forward-Projektionen einschließlich Originalspalten/-IDs erhalten |
| Datenbankintegrität | `integrity_check` und `foreign_key_check` im versiegelten G3 erfolgreich |

Das ursprüngliche G4 ergänzt 2.618 erfolgreiche PIT-/Accounting-Vergleiche,
20 beobachtete Fundamentalmetriken und 12 unveränderte Non-Fact-Tabellen. Dazu
gehören die bereits geprüften Filing-/Amendment-, Share-Alters-, TTM-/Prior-,
Source-/Tie-Break- und Reader-Verträge; deren erfolgreiche Matrix wird wiederverwendet.
Die Fallabdeckung ist dokumentierte Boundary-Abdeckung, keine vollständige
historische Backtest-Simulation.

Das Ranking-Supplement verwendet das vollständige source-definierte Produktions-
Universe aus 5.913 Unternehmen, unveränderte Konfiguration und je Reader denselben
gebundenen Stichtag auf Source und Work. Prepared Inputs, Peer-Tabelle/-Mediane,
Quality-/Value-/F-Auswertungen, Ausschlussgründe, Qualification und Rankings stimmen
innerhalb jedes Reader-Vertrags vollständig überein.

| Reader / Stichtag | Gleichheitsnachweis | Tatsächliches Ranking |
| --- | --- | --- |
| Legacy-Ticker, 2026-10-23 | Source-/Work-Kontext `f453c22d3481229ec4ff34a7a256ed80e2116aad84edc799b0f2a8aafe1bd613` | 16 Quality-/Value-Ränge; Rank 1 **MU**, Score 86,93010558609947. Drei F-Ränge; Rank 1 **PTC**, Score 78,35875389959303. |
| Forward-CIK/SIP, 2026-11-06 | Source-/Work-Kontext `92e730dc9d40eceff669577a0f62068f1adf5e891d00d2da2a46558bf37c7986` | Identische Berechnungen und Sperrung; keine gültige veröffentlichte Rankingliste. |

Die ursprüngliche Peer-Probe war für Rankings zu klein. Im vollständigen Kontext
wird für vier der ursprünglichen Legacy-Firmen und neun Forward-Firmen die
Peer-Lücke als Stichprobenbegrenzung aufgelöst. Andere Fälle haben weiterhin
Produktions-Basis-Ausschlüsse oder unzureichende Peers/Kennzahlen. Deshalb wäre
die Aussage „alle offenen Inputs waren nur ein Stichprobenproblem“ falsch.

**A ist für diesen Snapshot und diese Code-/Input-Bindung fachlich belegt.**
Das ist ein eigenständiger positiver Paritätsbefund. Es besagt nicht, dass die
Quelldaten wirtschaftlich vollständig/richtig sind oder zukünftige Ingestion,
neue Inputs und geänderte Produktionsversionen damit automatisch abgenommen wären.
Legacy und Forward werden jeweils gegen sich selbst verglichen; ihre
unterschiedlichen CIK-/Ticker-/Marktverträge oder Stichtage werden nicht gleichgesetzt.

## 4. B – Qualifiziertes Forward-Ranking bleibt offen

Die vorhandene Forward-Qualification liefert auf beiden Datenbanken:

| Kategorie | Unternehmen |
| --- | ---: |
| Universe | 5.913 |
| Deterministisch ausgeschlossen | 5.566 |
| Vollständig evaluiert | 13 |
| Rank-relevant ungelöst | **334** |

Die Summe entspricht dem vollständigen Universe. Die 334 sind keine zusätzlich
entstandenen Work-Fehler. Das Supplement belegt dieselben fehlenden Inputs und
Sperrgründe bereits auf Source. Innerhalb dieser 334 Fälle:

| Befund | Fälle |
| --- | ---: |
| Unvollständige SIP-History | 212 |
| Vollständige History, dennoch weitere offene Inputs | 122 |
| `VALUATION_UNAVAILABLE` | 326 |
| `QUALITY_UNAVAILABLE` | 291 |
| Keine PIT-Fundamentals | 74 |
| Aktuelle Beobachtung fehlt / Nullvolumen / falsche Session | 39 / 37 / 4 |

Die Gründe überlappen und dürfen nicht addiert werden. Auch ein bloßes Auffüllen
der 212 Histories würde die 122 anderen Fälle nicht beheben und behebt die
Fundamental-/Bewertungslücken der übrigen Fälle nicht automatisch.

Maßgeblich sind [qualification.py](../src/trading_system/shadow/qualification.py),
`qualify_f_inputs` und `qualified_forward_screen`: Ungelöste rank-relevante Inputs
verhindern das ganze Ranking. Die 13 vollständig evaluierten Unternehmen bilden
kein zulässiges Ersatz-Universe, da die 334 Unbekannten den Rank 1 verändern könnten.
Gespeicherte Zeilenzahlen sind kein Ersatz für nutzbare erwartete SIP-Sitzungen.
Kurze History ist ohne autoritative Listing-Evidenz kein unabhängig bewiesener
Ausschluss; `_independent_history_failure` liefert derzeit ausdrücklich False.

**Bewertung B:** Parität der Qualification und des Fail-closed-Verhaltens bestanden;
Parität eines tatsächlich qualifizierten Forward-Rankings nicht nachgewiesen.
Gleiche leere Listen sind kein gültiger Forward-Rank-1-Nachweis. Weder der
Legacy-Gewinner PTC noch ein diagnostischer F-Score darf diese Lücke schließen.

Der Ranking-Supplement-Bericht beansprucht nur die beiden Legacy-Clearances
für Quality/Value und F. Für B braucht es einen späteren gepaarten Vergleich
mit identisch gebundenen qualifizierenden Inputs auf Source und Candidate,
demselben vorab festgelegten Stichtag, dem vollständigen Universum und
unveränderter Qualification. Beide Seiten müssen ein gültiges Forward-Ranking
mit übereinstimmenden Scores, Reihenfolge und Rank 1 liefern. Ein späterer
Readiness-Lauf allein auf dem Zielbestand belegt dessen Betriebszustand für D.
Ob eine neue gepaarte Evidenz den ursprünglichen Stichtagsnachweis ersetzen
darf, muss der versionierte Vertrag ausdrücklich entscheiden.

Vor einer operativen Freigabe müssen die rank-relevanten Inputs nach dem
unveränderten Vertrag vollständig qualifiziert sein. Erforderliche zusätzliche
SEC-/SIP-/Identity-Daten wären ein eigener, autorisierter Datenauftrag mit neuer
Input-Provenienz und gezielter Qualification-Abnahme. Kein Herausfiltern der
334 Fälle, Feed-Fallback, erfundene History, geänderte Peer-Schwelle oder neue
positive Stichtagsauswahl als Ersatz.

## 5. C – Historische Identität und synthetische Vertragsregression

Der versiegelte Identity-Census umfasst alle 6.483 Symbole und 18.698.090
Occurrences; 6.459 Symbole liegen außerhalb der ursprünglichen 24er-Stichprobe.
Der Census scannt die Work-Occurrences vollständig. Die Source-Äquivalenz
derselben Provenienz folgt aus den gebundenen vollständigen G3-Streams und der
Non-Fact-Gleichheit in G4. Die 58 Reader-Witnesses sind zusätzliche direkte
Source-/Work-Vergleiche an ausgewählten Grenzen.

| Klasse / Befund | Anzahl |
| --- | ---: |
| `SINGLE_CIK_PROVENANCE` | 5.011 |
| `ALIAS_OR_SHARE_CLASS` | 1.471 |
| `UNRESOLVED_MULTI_CIK_PROVENANCE` | 1 |
| Autoritativ verifizierte Reuse-Witnesses | **0** |
| Identity-Epochen | **0** |
| Persistierte Quarantäne-Symbole | 74 |
| Company-/Cached-SEC-Disagreements | 6 |
| Reale Reader-Witness-Vergleiche | 58 |

Der Multi-CIK-Fall heißt **JSM**: 2.478 Occurrences zu `0001593538` mit Filed-Daten
2014-05-09 bis 2026-08-07 sowie 33 zu `0002152623` mit Filed-Datum 2026-11-04.
Das ist konkrete Provenienz, aber kein autoritativ belegter Wechsel des
Ticker-Eigentümers. Nicht überlappende Filed-Daten sind keine effektiven
Security-/Ownership-Intervalle.

Die 74 Quarantäne-Labels sind ein anderer Zähler als die sechs Company-/SEC-
Disagreements und die sechs `identity_conflict`-Ausschlüsse im Forward-Universe.
Keine dieser Zahlen darf mit „0 Canonical-Inhaltskonflikte“ gleichgesetzt werden.

Die synthetische In-Memory-Regression hat Reader-Parität, Alias-/Reuse-Abgrenzung,
keine CIK-Vermischung und keinen CIK-Fallback bestanden. Ihr Material ist explizit
`SYNTHETIC_IN_MEMORY_ONLY`, `real_reuse_credit=false`. Es wurden keine synthetischen
Epochen in reale Daten oder Manifeste übernommen.

Der aktuelle Forward-Fact-Reader in
[inputs.py](../src/trading_system/shadow/inputs.py), `facts_available_as_of`,
verwendet den aktuellen eingefrorenen Company-CIK und blockiert ungelöste
Identitäten. Er löst nicht anhand historischer Epochen einen anderen CIK auf.
Die Lifecycle-Verträge in
[security_identity.py](../src/trading_system/data/security_identity.py) werden
unter anderem für historische Universe-/Pairs-Anwendungen benötigt. Daraus folgt
keine pauschale neue Epoch-Pflicht für jeden aktuellen Champion-F-Reader.

**C bleibt als historischer Tatsachennachweis offen.** Der bisherige G4-Vertrag
verlangt den fehlenden realen Witness weiterhin. Für einen eng begrenzten
aktuellen F-Betrieb könnte eine separat genehmigte Scope-Entscheidung festhalten,
dass keine neue historische Auflösung beansprucht wird und C historisch offen
bleibt. Das wäre eine ausdrückliche Vertragsänderung, kein C-PASS und keine
automatische Cutover-Erlaubnis. Anwendungen mit tatsächlicher Abhängigkeit von
historischen Security-/Listing-Epochen benötigen weiterhin reale Evidenz.

## 6. Expliziter Vertragsvorschlag

### 6.1 Vorgeschlagener Entscheidungstext

> Für die Abnahme der unveränderten F01-Snapshot-Migration gilt vollständige reale
> Speicher-/Stream-/Input-Gleichheit zusammen mit erfolgreicher synthetischer
> Identity-Reader-Regression als ausreichende Evidenz für die Erhaltung des
> bestehenden Identity-Verhaltens in Dimension A. Dies gilt nur ohne neue
> historische Zuordnung, Alias-/CIK-Zusammenführung, Fallback oder Lockerung
> bestehender Quarantäne-/Admission-Regeln. Synthetische Daten belegen keine reale
> historische Ticker-Wiederverwendung; Dimension C bleibt ausdrücklich unbewiesen.
> Dimension B verlangt ein tatsächlich qualifiziertes Forward-Ranking und bleibt
> bei unresolved Inputs offen. Dimension D benötigt zusätzlich produktive
> Writer-/Sync-/Recovery- und aktuelle Readiness-Nachweise. Alte Ergebnisse,
> Manifeste und Seals behalten ihren ursprünglichen Status.

Dieser Vorschlag ersetzt für **A** einen auf dem konkreten Bestand nicht
verfügbaren realen Randfall durch einen expliziten verhaltensbezogenen
Regressionsnachweis. Er erklärt weder die Source zur historischen Ground Truth
noch die 334 unvollständigen Kandidaten zu zulässigen Ausschlüssen.

Der Ersatz ist auf die Kombination aus vier Befunden begrenzt. Die realen
Occurrence- und Reader-Streams sind vollständig erhalten. Die unabhängigen
Non-Fact- und Reader-Vergleiche zeigen dieselben Zuordnungen und Sperren. Der
vollständige reale Census lässt ungeklärte Provenienz sichtbar. Die
synthetische Regression deckt den im Bestand unbelegten Reuse-Pfad ab.
Fehlt einer dieser Befunde oder ändert sich die Identity-Semantik, ist die
vorgeschlagene Anerkennung von A für diese Bindung erneut zu beurteilen.

Voraussetzungen einer späteren Genehmigung:

1. Die vollständige versiegelte G0–G4-/Supplement-Kette samt Source-/Backup-/Work-,
   Code-/Config-/Input-Bindung bleibt nachvollziehbar. Vor weiteren Aktionen wird
   die notwendige physische Aktualität separat geprüft.
2. Alle Original-IDs, geordneten Reader-Streams, Nicht-Fact-Inputs und der reale
   Identity-Census bleiben erhalten. Insbesondere wird JSM weder automatisch
   vereinigt noch als verifizierter Reuse etikettiert.
3. Synthetisches Material wird nur als Contract-Regression ausgewiesen. Kein
   Umbenennen in reale Evidenz, keine nachträglichen Epochen in der Source.
4. B, C, G5-/G6-Lücken und operative Sperren bleiben als eigenständige Ergebnisse
   sichtbar. Ein erfolgreicher Storage-Paritätsnachweis reicht für keine
   produktive Freigabe.
5. Entscheidung und Scope erhalten eine eigene Version, Begründung,
   Genehmiger-/Zeitangabe und Bindung an die genannten Fingerprints. Ohne diese
   ausdrückliche Genehmigung bleibt der bisherige Gesamtvertrag maßgeblich.

Eine mögliche **rein illustrative**, nicht implementierte Bewertungsform ist:

```json
{
  "acceptance_contract": "F01_ACCEPTANCE_SCOPES_V2_PROPOSED",
  "approval_state": "PROPOSED_NOT_APPROVED",
  "A_STORAGE_AND_SEMANTIC_PARITY": "EVIDENCE_SATISFIED_FOR_BOUND_SNAPSHOT",
  "B_QUALIFIED_FORWARD_RANKING_PARITY": "INSUFFICIENT_EVIDENCE",
  "C_HISTORICAL_IDENTITY_EVIDENCE": "NOT_ESTABLISHED",
  "C_IDENTITY_READER_REGRESSION": "SYNTHETIC_PASSED",
  "D_OPERATIONAL_CHAMPION_READINESS": "NOT_RELEASED",
  "legacy_g4_status_unchanged": "INSUFFICIENT_EVIDENCE",
  "rehearsal_go": false,
  "production_cutover_authorized": false
}
```

### 6.2 Vor einem produktiven Cutover zwingend zu klären

| Gegenstand | Erforderliche Entscheidung / Evidenz |
| --- | --- |
| Aktiver Abnahmevertrag | Explizite Entscheidung über den Scope-Vorschlag oder vollständige Erfüllung des bisherigen Vertrags. Keine automatische Gate-Umdeutung. |
| Forward-Qualification | Keine ungelösten rank-relevanten Inputs im vorgesehenen vollständigen Betriebs-Universe; qualifiziertes Ergebnis nach unveränderten Regeln. |
| Historische Identität | Reale Evidenz für tatsächlich benötigte historische Auflösungen; alternativ ausdrücklich genehmigter enger Current-F-Scope ohne solche Behauptungen. C bleibt als historischer Nachweis offen. |
| Aktuelle Identity-Sicherheit | Konflikte nach bestehender Produktionspolitik zuverlässig quarantänisiert; keine issuerübergreifende Ingestion. Nicht pauschal alle 74 Labels zu „gelöst“ erklären. |
| Produktiver SEC-Writer / Synchronizer | Getestete Canonical-Persistenz einschließlich Receipt-/Commit-/Fehler-/Cursor-/Status-Kette; kein Legacy-Writer auf A-V1. |
| G6 / Rückweg | Reale isolierte Recovery-/Restore-/Reproduzierbarkeitsnachweise; zusätzlich konsistenter Rückweg für nachfolgende produktive Ingestion. |
| Consumer | R1 bleibt Legacy-only und für das isolierte Rehearsal ausgeschlossen. `f_data_gap_audit.py` ist dadurch nicht produktiv kompatibel geworden; vor Cutover klären. R2 darf nicht stillschweigend aufgehoben werden. |
| Aktuelle Betriebsbereitschaft | Frische Asset-/SEC-/Forward-SIP-States, verwertbare SPY-/Session-Inputs, vollständige Qualification und gültiger Code-/Dataset-Vertrag. Feste historische Vergleichsstichtage belegen keine heutige Readiness. |

Der bestehende Readiness-Vertrag in
[readiness.py](../src/trading_system/shadow/readiness.py),
`evaluate_shadow_input_readiness`, ist eine lesende Funktion. Er erlaubt
SEC-Partial-Status nur für nachweislich konfliktbedingte, fehlerfreie Ausschlüsse
und prüft Ranking-Vollständigkeit unabhängig davon. Provider-/Parse-/DB-Fehler
bleiben blockierend. Weder Observation-Bereitschaft noch `canonical_migration.COMPLETE`
ist gleichbedeutend mit wirtschaftlicher Champion-Readiness.

Im vorgeschlagenen engen Current-F-Scope muss die Freigabe die erlaubten
Entrypoints und Consumer benennen. Reader und Writer müssen dieselbe belegte
aktuelle Symbol-/CIK-Zuordnung anwenden und bei Konflikten gesperrt bleiben.
Historische Consumer, die effektive Security-/Listing-Intervalle oder
Ticker-Eigentümer zum damaligen Stichtag benötigen, bleiben außerhalb des
freigegebenen Scopes, bis die dafür relevanten Intervalle belegt sind. Ein
künftiger einzelner Reuse-Witness könnte die konkrete alte G4-Anforderung
erfüllen. Für einen historischen Consumer zählt die Abdeckung seiner
tatsächlichen Symbole und Zeiträume.

## 7. Kompatibilität der vorhandenen G5-/G6-Skripte

Geprüft wurden
[validate_f01_migration.py](../scripts/validate_f01_migration.py),
[run_f01_migration.py](../scripts/run_f01_migration.py) und
[validate_f01_g4_supplement.py](../scripts/validate_f01_g4_supplement.py).
Dies ist eine statische Bewertung, kein Aufruf der Skripte.

### 7.1 Konkrete Integrationshindernisse

1. **Additions-Gate:** Der alte G4–G6-Einstieg vereinigt ausschließlich den G3-
   und ursprünglichen Acceptance-Review. Das Supplement-Skript und dessen weitere
   Dateien fehlen in seiner erlaubten Menge. Der statische Vergleich mit dem
   gebundenen Base-Commit zeigt neun Git-Additions, darunter die drei
   versionierten Supplement-Dateien. `repository_evidence` erfasst das neue
   `scripts/validate_f01_g4_supplement.py`. Bei erfolgreicher Prüfung der
   vorgelagerten Bindungen stoppt `verify_bundle` deshalb bereits mit
   `UNREVIEWED_NEW_PRODUCTION_OR_INPUT_FILE`. Auch der anschließende
   `git_changes`-Check würde Supplement-Dateien außerhalb der alten Allowlist
   ablehnen. Der Supplement-Review bindet zusätzlich seinen lokalen Auditbericht
   und hat keinen Einfluss auf den alten Einstieg.
2. **Artefaktformat:** `read_phase` erwartet
   `f01_migration_acceptance_g4.json`, `result_seal.json` und den ursprünglichen
   Acceptance-Fingerprint-Vertrag. Supplements verwenden dagegen
   `f01_g4_supplement_report.json`, `seal.json` und `F01_G4_SUPPLEMENT_V1`. Sie sind
   kein austauschbarer Wert für `--g4-result`.
3. **Gesamtentscheidung:** `assessment` verlangt drei G4-/G5-/G6-Phasen mit PASS
   und ohne Gaps. Auch der Supplement-Kombinator lässt ungelöste Forward-Inputs
   oder fehlende reale Reuse-Evidenz ausdrücklich offen. Der vorliegende Vorschlag
   kann daher nicht durch einen vorhandenen Flag aktiviert werden. Der alte
   Finalizer verlangt zudem für jede Phase exakte Gleichheit des vollständigen
   Binding-Objekts. Die um neue Reviews erweiterte Ausführungsbindung darf daher
   beim Lesen der alten G4- und Supplement-Ergebnisse nicht als erwartete
   historische Bindung eingesetzt werden. Sowohl `--finalize` als auch
   `--combine` prüfen physische Referenzfrische über `verify_files`.
   Für diesen statischen Auftrag blieben beide CLI-Pfade unaufgerufen.
4. **G5-Abdeckung:** `g5_cases` dokumentiert die fehlende
   Readiness-/DataSynchronizer-Integration weiterhin als Gap und liefert bewusst
   `INSUFFICIENT_EVIDENCE`. Bestehende Adapter-Fälle können erfolgreich sein, ohne
   dass die vollständige produktive Fehlerkette bestanden wäre.
5. **G6-Abdeckung:** Der vorhandene Executor kann Ledger-/Mapping-/Restore-Checks
   und einen isolierten Wiederaufbau mit Crash vor/nach Batch-Commit und während
   Indexaufbau durchführen. Ohne die zusätzlich autorisierten realen Crash-Proben
   bleibt G6 unzureichend. G3-COMPLETE und frühere synthetische Tests ersetzen dies
   nicht. Im betrachteten Bestand liegt kein versiegelter G6-Abschluss vor.

G5 und G6 lesen vor ihren eigentlichen Phasen G0–G3-Bindungen; ein G4-PASS ist
im alten Branch nicht als eigener Vorabaufruf implementiert. Das ist keine
fachliche Freigabe, die offenen Gates zu übergehen. Nach einer späteren expliziten
Evidenz-/Additions-Brücke können diese isolierten Nachweise unabhängig von der
Beseitigung der 334 Source-Input-Lücken vorbereitet werden.

### 7.2 Vorgeschlagene additive Evidenz-Brücke

Ein **neuer, separat geprüfter Wrapper**, statt Änderungen an versiegelten
Skripten, soll folgende Aufgaben übernehmen:

- G0–G3, ursprüngliches G4 und beide Supplements jeweils nach ihrem unveränderten
  Format verifizieren; Original-Bindungen exakt rekonstruieren.
- Die Reviews `f01_copy_migration_additions_review.json`,
  `f01_acceptance_additions_review.json` und
  `f01_g4_supplement_additions_review.json` sowie einen neuen Wrapper-/Contract-
  Review als versionierten Evidenzgraph verknüpfen. Doppelte Dateieinträge nur bei
  identischen Hashes zulassen; keine unbeschränkten zusätzlichen Dateien.
- Alte Produktions-/Config-/Input-/Runtime-Bindungen und neue additive Hashes
  getrennt erhalten. Als Vorlage dient `verify_metadata` aus dem Supplement:
  Altes G4 mit exakt dem G3-plus-Acceptance-Binding über `read_phase` lesen,
  Ranking und Identity jeweils mit dem ursprünglichen Supplement-Binding über
  `load_supplement` lesen und Claims mit `validate_clearances` prüfen.
  Genau ein Ergebnis pro benötigter Phase akzeptieren. Fehlende Evidenz bleibt
  offen. Ein unversiegeltes Failure-Artefakt kann eine Sperre dokumentieren.
  Einen privaten Funktions-Namespace für die geprüfte
  Additions-Vereinigung verwenden, ohne globale Allowlist, alte Review-Datei,
  Receipt- oder Manifest-Bindung umzuschreiben.
- Dimensionen A–D nach einer **erst noch zu genehmigenden** Contract-Version
  ausgeben. Den alten Status daneben unverändert dokumentieren.
- Vorhandene `g5_phase`-/`g6_phase`-Primitiven für separat genehmigte isolierte
  Kopien weiterverwenden. Der neue Einstieg muss auch die Sicherheitslogik des
  alten `run` erhalten, einschließlich erneuter Bindungsprüfung unter nativen
  Pins, exklusivem Acceptance-Lock, Referenzprüfung vor und nach der Phase,
  atomarer Veröffentlichung und Ressourcenbudget mit 20 GiB Reserve.
  G5 benötigt `--authorize-g5-copy`, G6
  `--authorize-recovery-copies` und der einzelne isolierte Crash-Neubau
  zusätzlich `--rebuild-recovery`. Der G6-Kindprozess muss weiterhin an den
  geprüften Executor und die konkrete Recovery-Kopie gebunden sein.
- G5-/G6-Ergebnisse mit ursprünglicher Manifest-/Work-Bindung, Supplement-Seals,
  Additions-Reviews, Contract-Entscheidung und eigenen Ergebnis-Fingerprints neu
  veröffentlichen. Alte Status nicht in eine kopierte JSON-Datei hineinübersetzen.

G6 muss die unveränderte erfolgreiche G3-Work als Basis prüfen, nicht die durch
G5 absichtlich veränderte Wegwerfkopie. Nach Ingestion zusätzliche Occurrences/
Dataset-Versionen sind kein passender Input für das ursprüngliche Snapshot-
Migrationsledger mit festen G3-Sollzahlen. Runtime-Writer-Recovery benötigt
eigenständige Receipt-/Versionsevidenz auf einer gesonderten Kopie.

Die Brücke ist hier weder implementiert noch genehmigt. Sie löst allein durch
Artefaktaufnahme weder die operative G5-Integrationslücke noch B oder C.

## 8. Konkreter Vorschlag für SEC-Writer-/DataSynchronizer-Integration

### 8.1 Statischer Ist-Befund

Der reguläre Pfad ist
`DataSynchronizer._sync_sec → _sync_sec_company →
Database.upsert_sec_company_update → _execute_sec_company_update`
([sync.py](../src/trading_system/data/sync.py),
[database.py](../src/trading_system/data/database.py)).
Der letzte Schritt verlangt weiterhin den Legacy-Fact-Writer. Auf A-V1 entsteht
absichtlich `LEGACY_WRITER_ON_CANONICAL_SCHEMA`. Das Entfernen dieses Guards wäre
keine Integration.

Heute liegt der Company-Commit in `_sync_sec` außerhalb der Fehlerübersetzung
von `_sync_sec_company`. Die Schleife fängt `SecCompanySyncError`; ein roher
Commit-Fehler gelangt deshalb bis zum Stage-Handler statt in die vorgesehenen
Company-DB-Fehlerzähler. Der Daily-Index-Cursor wird zudem vor dem Abschluss
von `_run_stage` gesondert committed. Beide Übergänge brauchen einen expliziten
Abschlussvertrag für den Canonical-Pfad.

Der isolierte
[IsolatedCanonicalSecWriter](../src/trading_system/data/canonical_sec_adapter.py)
ruft dagegen
[write_company](../src/trading_system/data/canonical_facts.py) mit Schema-Guard,
RequestIdentity, Membership-Admissions, Payload-/Request-Hash, Retrieval-Zeit und
erwarteter Dataset-Version auf. Er liefert eine **Dataset-Version**, nicht die
Legacy-Anzahl `facts_processed`. Er ist kein installierter Synchronizer-Backend.

`write_company` enthält bereits atomare Company-/Canonical-/Revision-/Occurrence-/
Receipt-/Accession- und Companyfacts-Status-Updates. Es besitzt keinen
Submissions-Statusabschluss und keine vollständige Stage-/Daily-Index-Cursor-/
Readiness-Orchestrierung. Diese liegen aktuell im Synchronizer.

### 8.2 Zielarchitektur, als separater Implementierungsauftrag

**Eine explizite SEC-Persistenzschnittstelle**, beispielsweise
`SecCompanyPersistence`, wird dem Synchronizer injiziert:

| Baustein | Vorgeschlagene Verantwortung |
| --- | --- |
| Legacy-Backend | Bisherige Speicherung ausschließlich auf Legacy-Schema; vorhandene Sperre bleibt. |
| Canonical-Backend | Bestehendes A-V1-`write_company` und dessen Guard wiederverwenden; keine zweite Fact-/Dedup-Implementierung. |
| Request-/Admission-Planer | Validierte Company-/CIK-Entscheidung, vollständige unveränderte Parserausgabe, explizite Source-Membership-Entscheidungen, Accessions und reproduzierbarer Request-Envelope. |
| Synchronizer | Provider-/Parse-Orchestrierung, Transaktionseigentum, Commit-Bestätigung, Zähler, Cursor und Stage-Ergebnis. |
| Readiness | Weiterhin unabhängiger lesender Konsument von korrekt persistierten States und qualifizierten Inputs; kein manuelles READY-Setzen. |

Backend-Wahl muss explizit an den erwarteten versionierten Dataset-/Writer-Vertrag
gebunden sein. Ein Marker darf eine Inkompatibilität erkennen, aber keine
unfreigegebene produktive Aktivierung auslösen. Keine generische CLI-/Environment-
Umgehung, kein In-Place-Auto-Migrate und kein Fallback zum Legacy-Writer.

Erster Integrationsscope sind die regulären Full- und Incremental-SEC-Pfade
einschließlich ihres Aufrufs aus dem Gesamt-Sync. Die Wahl und Validierung des
Backends muss vor Stage-Status, Providerzugriff und SEC-Nebenwrites erfolgen.
SEC-Force mit `companyfacts_only` und Legacy-Backfill bleiben bis zur eigenen
Abnahme auf A-V1 gesperrt. Der Force-Pfad hat eine eigene lokale Identity-Auflösung
und den Stage-Key `sec_force_companyfacts`, während die Champion-Readiness
`dataset/sec` liest. Eine isolierte oder gefilterte Teilmenge darf keinen zuvor
fehlenden oder veralteten vollständigen SEC-Universe-Status durch `success`
ersetzen.

Ein Outcome wie `SecPersistOutcome(dataset_version, receipt_id, replayed,
facts_processed)` trennt Version von Zählung. `facts_processed` kann weiterhin die
Anzahl akzeptierter normalisierter Input-Facts bezeichnen; es ist weder die
Dataset-Version noch die Anzahl neu angelegter Canonical Facts.

### 8.3 Transaktions- und Fehlervertrag

1. Providerzugriff/Archivlesen und Parsen erfolgen vor der SQLite-Schreibsperre.
   Ein Request-Envelope bindet Company, CIK, normalisierte Fact-Reihenfolge,
   Accessions, Membership-Entscheidungen samt Evidenz, Payload-Hashes,
   Parser-/Policy-Version und Retrieval-Zeit.
   Im regulären Canonical-Pfad die normalisierte CIK der beobachteten Submissions-
   und Companyfacts-Payloads mit der Request-CIK abgleichen. Archivierte
   Retrieval-Zeit und Filed-Zeit erfüllen verschiedene Funktionen und bleiben
   nach Replay erhalten.
2. Derselbe logische Request behält bei Retry nach Prozessneustart denselben
   `request_id` und exakt denselben Envelope. Der vorhandene Input-Hash umfasst
   auch `retrieved_at` und Admissions. Ein bloßer Payload-Hash als Request-ID bei
   jedes Mal neuer Retrieval-Zeit würde daher `RECEIPT_INPUT_CONFLICT` provozieren.
   Neue Beobachtung und Retry müssen unterscheidbar bleiben.
3. Der Envelope wird vor dem Commit dauerhaft und überprüfbar archiviert,
   beispielsweise in einem getrennten append-only Ingestion-Archiv mit
   inventarisierten Hashes. Keine ungeprüfte Inbox-Tabelle zum gebundenen
   SQLite-Schema hinzufügen. Falls ein neuer Schema-Vertrag nötig wird, diesen
   ausdrücklich versionieren und abnehmen.
4. Pro Company genau ein äußerer Transaktionseigentümer. Unter dieser Transaktion
   liegen auch die bisher davor ausgeführte Löschung von
   `sec_submissions_status` und alle zugehörigen Status-/Accession-Änderungen.
   Auf dedizierter, mit FK und Writer-Guard geprüfter Verbindung mit
   `BEGIN IMMEDIATE` starten und erst unter Schreibsperre die aktuelle Version,
   Identity, Accession-State und die für den Plan relevanten Memberships lesen
   oder erneut bestätigen. A-V1 gibt bei externer Transaktion seinen Savepoint
   frei. Der Eigentümer bestätigt den Commit. Bei einer Write-/Commit-Ausnahme
   die aktive Company-Transaktion zurückrollen und den ursprünglichen Fehler
   erhalten. Bei unklarem Commit-Ausgang das Receipt über eine frische Verbindung
   abgleichen, bevor dieselbe Company erneut verarbeitet wird.
5. Schema/FK/COMPLETE-Guard, RequestIdentity und Version werden geprüft.
   Memberships werden nicht pauschal zugelassen: unveränderte vorhandene
   Memberships wiederverwenden; neue Fact-Identität benötigt einen zugelassenen
   Forward-Anker; neuer Alias einer bestehenden Identität bekommt durch die
   bestehende Writer-Regel keinen neuen Forward-Tie-Anker. Offene CIK-Konflikte
   bleiben blockierend. Unbekannte normalisierte Metrics oder Taxonomy-Varianten
   werden nicht writerseitig verworfen.
6. Unveränderte Wiederholung, Korrektur und Amendment bleiben getrennt.
   Korrekturen erzeugen nur bei Inhaltsänderung Revisionen, historische
   Dataset-Versionen bleiben lesbar. Ein neues Filing/Amendment bleibt eine neue
   Identität. Original-ID-Reihenfolge, Filed-PIT und Selektoren bleiben unverändert.
7. Erfolgszähler und In-Memory-Accession-State werden erst nach erfolgreichem
   äußerem Commit fortgeschrieben. Nach Commit mit verlorener Antwort muss das
   bestehende Receipt genau einmalige Verarbeitung bestätigen.
   Beim Receipt-Replay keine nachgelagerte Statuslöschung oder erneute
   Accession-Persistenz ausführen. Ein älteres Receipt darf neueren
   Submissions- oder Accession-State nicht zurücksetzen. Zähler für Replays
   explizit ausweisen und bei Wiederanlauf keine zweite Mutation ausweisen.
8. `STALE_DATASET_VERSION`, Busy-/Lock-, Receipt- und Identity-Fehler werden
   sichtbar weitergereicht. Bounded Retry darf nicht den Request stillschweigend
   umschreiben oder zwischenzeitliche Accessions verlieren. Vor erneutem Versuch
   Receipt prüfen; falls ohne Receipt ein neuer Admission-/Accession-Plan nötig
   wird, diesen als neuen überprüfbaren Envelope behandeln. `expected_version`
   ist eine Commit-Vorbedingung außerhalb des aktuellen Receipt-Input-Hashs.
   Eine reine Aktualisierung dieser Vorbedingung ist erst nach erneuter Prüfung
   desselben Plans zulässig.
9. Die `SecCompanySyncError`-Kette erhält die DB-/Commit-Fehler. Ein Company-Fehler
   erhöht `errors/database_failures`; ein Daily-Index-Cursor wird nicht über
   fehlgeschlagene Eingaben fortgeschrieben. Bereits committed andere Companies
   werden beim Restart über Receipts/Accessions sicher wiedererkannt.
10. `_run_stage` bleibt Run-Aggregation über mehrere Company-Commits. Bei Fehlern
    ergibt sich `partial`/`failed`, `last_success_at` bleibt unverändert.
    Für einen erfolgreichen Canonical-Incremental-Lauf den Cursor und den
    zugehörigen SEC-Stage-Abschluss in derselben kurzen finalen Transaktion
    veröffentlichen. Bei Ausfall davor bleiben die alten Cursordaten erhalten
    und der unvollständige Run bleibt für Readiness blockierend. Bereits
    bestätigte Company-Commits werden beim Wiederanlauf über Receipts und
    Accessions erkannt. Parallele SEC-Runs am Einstieg serialisieren, damit
    ein älterer Abschluss keinen jüngeren Cursor oder Stage-State überschreibt.

404-Negativcache, Initialisierung ohne neue Facts und Quarantäne sind eigene
Statuspfade. Cache-/Accession-Änderungen pro Company müssen gemeinsam committen.
Authentische 404-Ereignisse behalten ihre dokumentierte Verfügbarkeitssemantik.
Timeouts, Parse-, CIK- oder DB-Fehler dürfen weder als 404 noch als erfolgreicher
Companyfacts-Refresh verbucht werden. Ob die verbleibenden Inputs genügen,
bewertet anschließend die lesende Qualification.

Cache-/Archiv-Schreibzugriffe erhalten eine getrennte, eng begrenzte Zuständigkeit;
die A-V1-Company-Authorizer-Allowlist wird dafür nicht pauschal erweitert oder
temporär abgeschaltet. Snapshot-Migrationsledger und Runtime-Ingestion-Receipts
haben unterschiedliche Aufgaben und dürfen nicht gegenseitig umgedeutet werden.

### 8.4 Noch erforderlicher Integrationsnachweis

Ein späterer, ausdrücklich autorisierter G5-Auftrag muss den **tatsächlichen**
DataSynchronizer mit dem Canonical-Backend auf isolierten Kopien und mit
archivierten SEC-Inputs/Provider-Doubles prüfen. R2 benötigt dafür eine gezielte
Scope-Erweiterung für diese Integrationstests; eine produktive Sync-Erlaubnis
folgt daraus nicht.

Die Abnahme muss Full/Incremental, neue Accessions, unveränderte Requests,
Korrekturen, Amendments, Alias-Admission, gemischte Perioden, unbekannte normalisierte
Facts, externe Rollbacks, Commit-Fehler, Busy/Parallelität und Restart prüfen.
Besonders nachzuweisen sind:

- Fehler nach Fact-, Occurrence-, Receipt-, Accession-, Company- und Status-Write:
  keine partielle Company-Einheit, kein voreiliger Erfolgscursor;
- Commit erfolgreich, Prozess/Antwort vor Stage-Abschluss verloren:
  Wiederanlauf ohne doppelte Occurrence oder geänderten Request;
- Klassifikation im Synchronizer, korrekte `partial/failed`-States,
  unverändertes `last_success_at` und echte Weitergabe an die lesende
  Readiness-/Qualification-Funktion;
- Restart nach erfolgreichem Company-Commit bei gescheitertem gemeinsamem
  Cursor-/Stage-Abschluss, ein älteres Receipt nach neuerem Company-State
  sowie ein gefilterter Sync, der keinen vollständigen SEC-State attestiert;
- getrennte Pfade für echte 404, Timeout, falsche Response-CIK und
  Negative-Cache-Ablauf einschließlich ihrer Auswirkungen auf die Qualification;
- positiver Kontrollfall mit vollständig gültigen eingefrorenen Fixture-Inputs,
  ohne READY-Override oder entfernte Admission-Guards;
- Legacy-/Canonical-Schema-Kreuzkombinationen bleiben gesperrt.

Für diese Funktionsproben ist weder ein Shadow-Runner noch ein echter Provider
erforderlich. Ein neues produktives Integrations-Changeset erhält einen eigenen
Code-Fingerprint und expliziten Diff-/Scope-Review. Es ist **keine bloß additive
Änderung** am ursprünglichen Produktionscode. Die alte Paritätsbindung bleibt als
historischer Nachweis erhalten; die neue Candidate-Code-/Input-Bindung und ihre
Tests werden separat abgenommen. Kein Aktualisieren alter Hashfelder, um Drift
unsichtbar zu machen.

## 9. Empfohlene nächste Entscheidungen

1. Den Scope-Vorschlag aus Abschnitt 6 ausdrücklich annehmen, ändern oder ablehnen.
   Bis dahin bleiben G4 und beide Supplements unverändert unzureichend nach dem
   aktiven bisherigen Vertrag.
2. Eine additive Evidenz-Brücke separat implementieren und synthetisch prüfen.
   Sie darf A ausweisen und G5/G6-Evidenz aufnehmen, aber keine operative Lücke
   oder fehlende historische Tatsache in PASS umetikettieren.
3. Isolierte G5-/G6-Nachweise mit eigener Autorisierung vervollständigen. Ein
   Recovery-Neubau ist nur bei konkreter fehlender Crash-Evidenz gerechtfertigt;
   kein erneuter G3-Migrationslauf als routinemäßige Paritätsprüfung.
4. Die Canonical-/Synchronizer-Integration als eigenes Changeset nach Abschnitt 8
   entwickeln und mit Netzwerk-Doubles in der vollständigen Fehlerkette abnehmen.
5. Forward-Input-Qualität und aktuelle Betriebsbereitschaft in einem getrennten,
   später autorisierten Daten-/Readiness-Auftrag herstellen. Dabei keine
   Strategieoptimierung und keine stille Verkleinerung des Universums.
6. Einen produktiven Cutover erst danach separat planen und freigeben, mit
   frischer Sicherung, Consumer-Kompatibilität und nachgewiesenem Rückweg auch
   nach neuer Ingestion. Ein bloßer Code-Downgrade wäre kein Datenrollback.

Dieses Dokument hat ausschließlich die Evidenz eingeordnet und konkrete
Vertrags-/Integrationsvorschläge formuliert. Kein vorgeschlagener Gate, Backend
oder Workflow wurde aktiviert.
