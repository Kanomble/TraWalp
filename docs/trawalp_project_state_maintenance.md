# TraWalp · Pflege des Living Project State

`docs/trawalp_project_state.md` ist der laufende Projektstand. Die bei 8F.3D.10 erzeugte Baseline ist ein eingefrorener Belegsatz. Bei späteren Tasks wird das Projektdokument anhand neuer Evidenz fortgeschrieben. Baseline, alte Audits, Deployments, Seals, Fingerprints und Datenbankdateien behalten ihren historischen Stand.

## Nach jedem abgeschlossenen Task

1. Zu Beginn den Projektstand, die Repo-Regeln und den für den Task maßgeblichen Vertrag lesen. Branch, Git-HEAD, vorhandene Änderungen und den tatsächlichen Daten-/Runtime-Scope erfassen.
2. Am Ende nur geprüfte Änderungen, beobachtete Ergebnisse und tatsächlich ausgeführte Tests übernehmen. Quellenpfad, Ergebnisstatus, Scope, Datum und Code-/Datenbindung angeben. Anforderungen, geplante Arbeit und ausgeführte Arbeit getrennt führen.
3. Die betroffenen Architektur-, Status-, Freigabe-, Blocker- und Roadmap-Abschnitte direkt aktualisieren. Die aktualisierte Statuszeile enthält Datum, Branch und den verifizierten HEAD. Bei uncommitteten Änderungen den Arbeitsstand ausdrücklich nennen.
4. Den Task im Completion Log unter seiner eindeutigen ID erfassen. Auch ein abgeschlossener Task ohne fachliche Änderungen erhält einen kurzen Eintrag. Bei Teilabschluss, Sperrung oder Abbruch nur den tatsächlich erreichten Stand protokollieren und den nächsten konkreten Schritt nennen.
5. Links, Lesbarkeit, Task-ID-Eindeutigkeit und `git diff --check` prüfen. Im Abschlussbericht das aktualisierte Dokument und gegebenenfalls den Hinderungsgrund nennen.

## Task-Log-Format

#### [Task-ID] – [Task-Titel]

- **Datum:** YYYY-MM-DD
- **Git-Stand:** Commit oder ausdrücklich uncommitteter Arbeitsstand
- **Status:** COMPLETED / PARTIAL / BLOCKED
- **Ziel:** Kurze Aufgabenbeschreibung
- **Änderungen:** Zwei bis fünf Sätze
- **Tests:** Tatsächlich ausgeführte Prüfungen
- **Ergebnis:** Wichtigster technischer oder fachlicher Fortschritt
- **Neue Blocker:** Belegte neue Abhängigkeiten oder „keine“
- **Geschlossene Blocker:** Nur mit konkret nachgewiesenem Abschluss
- **Nächster Schritt:** Konkrete Folgeaufgabe und ggf. Freigabe
- **Evidenz:** Relevante Berichte, Artefakte oder Quellpfade

## Idempotenz, Evidenz und Gate-Schutz

Vor dem Einfügen nach der exakten ID im Completion Log suchen. Existiert sie bereits, wird ihr Eintrag nur bei nachgewiesener neuer Evidenz ergänzt oder präzisiert. Belegte Korrekturen und bislang gültige Entscheidungen chronologisch erkennbar halten. Eine wiederverwendete Text-ID zunächst anhand Titel, Scope und Evidenz eindeutig zuordnen. Die historische abgebrochene D.9-Aufgabe ist im Baseline-Belegsatz als eigener Scope unter der wiederverwendeten ID dokumentiert.

Für widersprüchliche Aussagen gilt die Rangfolge Originalbeleg, gültiges gebundenes Ergebnis, späterer Audit mit identischem Scope, älterer Bericht, Plan. Unterschiedliche Dataset-Versionen, Review-Signaturen, Zeitfenster oder Entscheidungsdimensionen getrennt halten. Bei fehlendem Beleg `UNVERIFIED` oder `UNKNOWN` verwenden. Ein angezeigter Provider-Status, ein Commit, ein synthetischer Test oder ein isolierter PASS ersetzt keine damit unbelegte operative Freigabe.

Vor einer Aktualisierung prüfen, ob der aktuelle Checkout einen exakten historischen Git-/Acceptance-Nachweis reproduzieren muss. In diesem Fall den ursprünglichen gebundenen Checkout und dessen Runtime respektieren. Wenn eine Änderung am Projektstand durch diesen Scope, fehlende Evidenz oder vom Benutzer vorgegebene Schreibbeschränkungen ausgeschlossen ist, die konkrete Grenze im Task-Abschluss protokollieren. Historische Guards, Additions-Reviews und Seals nicht ändern, um Dokumentationsdrift zu akzeptieren. Die Repo-Anweisung setzt den Pflegeprozess für abgeschlossene Tasks fest. Ein abgebrochener Prozess kann den Eintrag hinterlassen oder ihn unerledigt lassen.

