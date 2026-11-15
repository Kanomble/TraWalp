# F01 G4: gezielte Evidenzergänzung

`scripts/validate_f01_g4_supplement.py` ergänzt das bereits veröffentlichte G4,
ohne dessen Skript, Manifest, Bericht oder Siegel zu ändern. Standard ist ein
read-only Preflight. Manuell getrennte Modi: `--ranking`, `--identity`, `--combine`.
Kein Sync, Provider, Backtest, Shadow-Runner, Writer oder Migrationsaufruf.

## Befund und Fragestellungen

Das bisherige G4 umfasst 2.618 PIT-/Accounting-Vergleiche, 20 beobachtete Metriken
und 12 unveränderte Non-Fact-Tabellen. Es meldet keine Migrationsdifferenz.
Die alphabetisch gleichmäßig verteilten 24 Firmen sind jedoch zugleich der gesamte
Peer-Kontext dieser Probe. Der Produktions-Screener nimmt nur Kandidaten ohne
Basis-Ausschlüsse in die Peer-Tabelle auf. Er benötigt mindestens acht Mitglieder
im engsten verfügbaren SIC-4/3/2-Kontext sowie genügend gültige Werte pro Kennzahl.
Alle 24 Kandidaten haben `insufficient_peer_group`, alle 24 keinen Value-Score;
14 haben keinen Quality-Score. Die bisherigen Daten allein belegen deshalb weder
eine vollständige Ranking-Parität noch, dass ein größerer Peer-Kontext alle
Input-Lücken behebt.

Legacy wurde zum 2026-10-23, Forward/SIP zum 2026-11-06 geprüft. Diese Stichtage
werden unverändert aus dem gebundenen G4 übernommen. Kein neuer Zeitpunkt und
kein Unternehmen werden nach einem gewünschten Score oder Rank ausgewählt.

`LocalInputs.history_coverage` verlangt tatsächlich nutzbare SIP-Beobachtungen für
die erwarteten Handelssitzungen. 320 gespeicherte Zeilen beweisen diese Abdeckung
nicht. Fehlende aktuelle Beobachtung, Fundamental-/Bewertungskennzahlen,
Strategieeingaben und ungeklärte Ausschlussgründe werden vom Produktionsvertrag
als rank-relevant behandelt, sofern kein unabhängig belegter Ausschluss vorliegt.
Die konkrete Ursache wird erst der manuelle Supplement-Lauf ermitteln.

Das versiegelte Input-Inventar nennt **0 Zeilen in `symbol_identity_epochs`**.
Aktuelle SEC-/Security-Reference-Zuordnungen, Corporate-Action-Rohdaten und
Fact-Filed-Zeiträume ersetzen keinen verifizierten historischen Ownership-Zeitraum.
Ohne reale Lifecycle-Evidenz bleibt die Ticker-Reuse-Anforderung offen.

## Bindung und Wiederverwendung

Das Skript prüft G0–G2-, G3- und G4-Siegel, deren innere Fingerprints, vollständige
Manifest-/Work-/Datei-ID-Bindungen, unveränderte Produktionsdateien, Config,
externe Inputs und Runtime-Versionen. Der zusätzliche lokale Review
`reports/f01_g4_supplement_additions_review.json` bindet genau die vier neuen
Dateien einschließlich des statischen Implementierungs-Audits. Er bindet auch den
konkreten ursprünglichen G4-Fingerprint. Frühere Reviews bleiben unverändert.

Das Additions-Gate verwendet die bestehende Produktionsprüfung in einem privaten
Funktions-Namespace mit einer geprüften Dateivereinigung. Ursprüngliche G4-Bindungen
werden exakt rekonstruiert; kein bestehendes Objekt wird umgebunden. Ein neuer
Git-HEAD allein erzwingt keine neue Datenbankvorbereitung. Änderungen bestehender
gebundener Dateien oder ungeprüfte Ergänzungen führen zu STOP.

Windows-Datei-/Verzeichnispins verhindern Schreibzugriffe und Pfadaustausch.
Nach dem Pinnen werden die Metadaten erneut geprüft. Referenzen werden ausschließlich
mit `mode=ro&immutable=1` und `query_only=ON` geöffnet; die Forward-View existiert
nur im Verbindungsspeicher. WAL/SHM/Journal an Referenzen führen zum STOP.

Physische Dateihashes müssen bei manueller Ausführung erneut die Aktualität
belegen. Diese Prüfungen ersetzen keine Datenbankvorbereitung. Die bereits
erfolgreichen vollständigen G3-Streams, Ledger-/Mapping-Checks, G4-PIT-/Accounting-
Matrix und Non-Fact-Tabellen-Hashes werden **nicht erneut berechnet**.

## Ranking-Modus

Die Source definiert vor der Analyse das vollständige Universe aus
`Database.list_tradable_companies()`. Alle Firmen werden eingeschlossen, auch
später ausgeschlossene oder unvollständige. Work muss dasselbe Universe und
dieselben Identity-Konflikte liefern. Die Produktionsfunktionen entscheiden über
Basis-Ausschlüsse und Peer-Teilnahme; keine Vorselektion nach Scores.

Legacy-Ticker und Forward-CIK/SIP werden separat ausgewertet und vollständig
verglichen:

- Prepared Inputs vor der Score-Berechnung;
- vollständige Peer-Tabelle einschließlich Mitglieder, Kennzahlen und Mediane;
- Quality-/Value-Scorefaktoren, Eligibility, Ausschlussgründe und Warnungen;
- kanonische F-Evaluierungen und vollständige sortierte Rankings samt Rank 1;
- Forward-Qualification-Census, Rejections und alle rank-relevanten Input-Lücken.

Es werden nur `Screener._prepare`, `_peer_table`, `report_from_prepared`,
`evaluate_variant_entry`, `champion_ranking`, `history_coverage` und
`qualified_forward_screen` aus der unveränderten Produktion verwendet.
Company-, Markt-, Fundamental-, Peer- und Konfigurationsinputs bleiben eingefroren.

Für die ursprünglichen 24 Firmen wird anhand von `peer_diagnostics` gezeigt, ob der
vollständige Kontext deren Peer-Lücke tatsächlich schließt. Verbleibende Lücken im
vollständigen Source-Universe gelten als eingefrorene Daten-/Qualification-Lücke,
sofern sie auf Work exakt gleich sind. Eine Abweichung führt hingegen zum STOP.

Forward-Diagnostik enthält die unveränderten Produktionsgründe, jede fehlende
History-Sitzung, Prepared Inputs, verfügbare Scorefaktoren, Peer-/Median-Kontext,
rohe Provider-/Feed-/Adjustment-/Timeframe-Gruppen sowie Provenienz-/Nullvolumen-
Zähler. Admission-Guards werden nicht verändert. Bei unresolved Inputs bleiben die
öffentlichen Forward-Rankinglisten leer. Rohe Vergleichsrecords und einzelne
F-Evaluierungen sind dann ausdrücklich diagnostisch, kein gültiges Ranking.

Kein Rank 1 an diesem festen Stichtag bleibt fehlende reale Witness-Evidenz.
Es gibt keine automatische Datumssuche oder Strategieanpassung.

## Identity-Modus

Ein neuer, gezielter Symbol-/CIK-Census streamt den bestehenden
`occurrence_legacy_reader`-Index der unveränderten Work-Datei. Es findet kein
erneuter Fact-Wert-/Accounting-/G3-Streamvergleich statt. Die vollständige
G3-Stream-Bindung belegt die Source-Äquivalenz dieses Provenienz-Census.
Der Queryplan muss ohne temporären Sortier-Baum auskommen.

Der Census deckt auch alle Symbole außerhalb der 24er-Stichprobe ab. Er unterscheidet:

- `ALIAS_OR_SHARE_CLASS`: mehrere Labels zum selben CIK; kein Reuse-Nachweis;
- `UNRESOLVED_MULTI_CIK_PROVENANCE`: mehrere CIKs unter einem Label ohne
  ausreichenden historischen Ownership-Nachweis;
- `VERIFIED_TICKER_REUSE`: verschiedene CIKs mit konkreten Source-Occurrences und
  eindeutigen, verifizierten, nicht überlappenden Produktions-Lifecycle-Epochen.

Epochs werden durch `SymbolIdentityEpoch` und `current_identity_epoch` geprüft.
Filed-Daten oder aktuelle Referenz-Snapshots werden niemals zu effektiven
Ownership-Daten umgedeutet. Aktuelle SEC-/Company-Disagreements, Quarantäne-Einträge
und vorhandene Corporate Actions werden separat dokumentiert.

Alle belegten Reuse-Symbole werden auf beiden Produktionsreadern verglichen.
Zusätzlich werden die ersten acht außerhalb der alten Stichprobe liegenden Aliase,
Multi-CIK-Fälle und aktuellen Konfliktlabels je Kategorie deterministisch geprüft.
Zeitpunkte sind vorhandene Filed-/Epoch-Grenzen und der ursprüngliche Forward-Stichtag.
Forward bleibt beim aktuellen eingefrorenen Company-CIK; es erhält weder eine neue
historische Ticker-Auflösung noch einen CIK-Fallback.

Eine sehr kleine In-Memory-Regression prüft zusätzlich Reuse, Alias-Abgrenzung,
Reader-Parität und No-Fallback. Sie wird als **synthetisch** markiert und zählt
niemals als realer Reuse-Witness. Fehlt reale Evidenz, bleibt G4 ohne eine gesonderte
Vertragsentscheidung `INSUFFICIENT_EVIDENCE`. Es gibt keinen Waiver-Schalter.

## Betrieb und Ergebnisse

Mindestens 20 GiB Reserve auf Arbeits- und Temp-Volume; manuelle Phasen budgetieren
zusätzlich 1 GiB für Ergebnisdateien/Logs. Keine zusätzlichen großen Datenbankkopien.
SQLite-Cache ist begrenzt. Fortschritt, Phase, Laufzeit, RSS und
Freiplatz werden jede Sekunde angezeigt und protokolliert. Der vollständige
Ranking-Kontext benötigt mehr RAM und CPU als die alte Stichprobe; beide vollständigen
Vergleichsergebnisse werden für den exakten Vergleich vorübergehend gehalten.
Strg+C unterbricht kontrolliert. Eine unterbrochene Phase wird nicht als erfolgreich
veröffentlicht; vorhandene Artefakte werden nicht gelöscht oder überschrieben.

Jeder Lauf schreibt in ein neues Verzeichnis unter
`reports/f01_copy_rehearsal/20261115_8f2a/g4_supplement_runs/`.
Erst ein atomar veröffentlichtes `result/` mit gültigem `seal.json` zählt.
Enthalten sind `f01_g4_supplement_report.json`, `f01_g4_supplement_audit.md`,
`progress.jsonl` und `fingerprint.json`. `unpublished/` und `failure.json` sind kein PASS.
Die Ergebnisbindung enthält Manifest, G3-Fingerprint, ursprünglichen G4-Fingerprint
und -Seal, Work-SHA256, ursprünglichen Code-Fingerprint und neue Additions-Hashes.
Ein fehlgeschlagener Versuch kann bei der Kombination mit seinem Verzeichnis
und gebundenem `failure.json` angegeben werden. Diese unversiegelte negative
Evidenz führt ausschließlich zu NO_GO; sie kann keine Anforderung schließen.

`--combine` liest nur die beiden Supplements und die ursprünglichen Nachweise;
es berechnet keine Rankings und keinen Census neu. Nur belegte Anforderungen
werden einzeln geschlossen. Unbekannte zusätzliche G4-Lücken bleiben bestehen.
Synthetische Evidenz, leere Rankings oder unresolved Forward-Inputs dürfen kein
erforderliches reales Witness ersetzen. `PASS` bedeutet ausschließlich ergänztes G4,
kein G5-/G6-/Cutover-GO.

Das statische Audit `reports/f01_g4_evidence_recovery_audit.md` beschreibt die
Implementierung und bleibt selbst an den Additions-Review gebunden. Manuelle
Ergebnisse erscheinen in eigenen versiegelten Ergebnisverzeichnissen. Die lokalen
Reviews und Ergebnisdaten sind durch die bestehende `.gitignore` ausgeschlossen.

## PowerShell

```powershell
Set-Location 'G:\GitHub\TraWalp'
$Python = 'G:\GitHub\TraWalp\.venv\Scripts\python.exe'
$Base = 'G:\GitHub\TraWalp\reports\f01_copy_rehearsal\20261115_8f2a'
$G3 = Join-Path $Base 'g3_runs\20261115T120802_b1cbe578e8f94f969ba6474140477dd2\result'
$G4 = Join-Path $Base 'acceptance_runs\2026-11-15T151756_0000_g4_747e7c788562433d969f4aca340fffaa\result'

# Read-only Preflight; keine Ranking-/Identity-Phase
& $Python -B scripts/validate_f01_g4_supplement.py --g3-result $G3 --g4-result $G4

# Zwei getrennte, bewusst manuell gestartete Prüfungen
& $Python -B scripts/validate_f01_g4_supplement.py --g3-result $G3 --g4-result $G4 --ranking
& $Python -B scripts/validate_f01_g4_supplement.py --g3-result $G3 --g4-result $G4 --identity

# Die konkret ausgegebenen result-Verzeichnisse verwenden
$RankingResult = Read-Host 'Absolutes Ranking-Supplement-result-Verzeichnis'
$IdentityResult = Read-Host 'Absolutes Identity-Supplement-result-Verzeichnis'
& $Python -B scripts/validate_f01_g4_supplement.py --g3-result $G3 --g4-result $G4 --combine --ranking-result $RankingResult --identity-result $IdentityResult
```

Exitcodes: 0 = verifizierter Preflight oder PASS; 2 = unzureichende Evidenz /
Blockierung / NO_GO; 130 = kontrollierter Abbruch. Bestehende G4–G6-Skripte erkennen
dieses neue Supplement-Format nicht automatisch: kein altes Ergebnis ersetzen,
keine alte Schlussbewertung durch Änderung ihrer Seals erzwingen.
