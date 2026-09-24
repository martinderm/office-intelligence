# OI-14a — Refactor-Map für `mail-desk`

Stand: Bestandsaufnahme von `skills/mail-desk/SKILL.md` mit 651 Zeilen.
Dieses Dokument ist nur die Zuordnungs- und Erhaltungsplanung für die progressive
Disclosure. Es ändert keine Fachregel, entscheidet keinen Widerspruch und ist keine
operative Anleitung.

## Zielräume und Paketgrenzen

| Kürzel | Konkretes Ziel | Verantwortliches Paket | Inhaltliche Grenze |
| --- | --- | --- | --- |
| `KERN` | künftiges kompaktes `SKILL.md` | OI-14d, nach den gezielten Cuts in OI-14b und OI-14c | Einstieg, Reihenfolge des fachlichen Einzelfalls, harte Sicherheits- und Identitätsinvarianten, Routingentscheidung und gezielte Links. |
| `FOLDER` | vorhandenes `references/folder-rules.md` | OI-14d verlinkt; kein Auslagern in OI-14a | Geschäftliche Zielbildung, fehlende Zielordner und Spam-Quarantäne-Ziele. |
| `GMAIL` | vorhandenes `references/backends/gmail.md` | OI-14b | Gmail-Zugriff, Label-/Inbox-Semantik, Locator und Verifikation; OI-14b lagert diese Bestände aus `SKILL.md` aus und ersetzt sie dort durch einen Link. |
| `HIMALAYA` | vorhandenes `references/backends/himalaya.md` | OI-14b | Himalaya/IMAP-Zugriff, Account-/Preflight-/Envelope- und Kopier-/Move-Semantik; OI-14b lagert diese Bestände aus `SKILL.md` aus und ersetzt sie dort durch einen Link. |
| `BATCH` | vorhandenes `references/batch-runner.md` | OI-14c | Batch-Modi, Manifeste, Beispiele, Fortschritt, temporäre Artefakte und Batch-Fehlerverhalten; OI-14c lagert diese Bestände aus `SKILL.md` aus und ersetzt sie dort durch einen Link. |
| `SCHEMA` | vorhandenes `references/log-schema.md` | OI-14c | JSONL-/Index-/Progress-Schemata, Feldbedeutungen, Idempotenz und Archivformate; OI-14c lagert diese Bestände aus `SKILL.md` aus und ersetzt sie dort durch einen Link. |
| `LEGACY` | vorhandenes `references/legacy-cli-adapter.md` | OI-14c | Befristeter Adapter, historische Formen und dessen Migrations-/Entfernungsgrenze; OI-14c lagert die zugehörigen Detailbestände aus `SKILL.md` aus und ersetzt sie dort durch einen Link. |
| `CLI` | neue `references/cli-operations.md` | OI-14c | Gemeinsame kanonische CLI-Envelope-Regel, Script-Zuständigkeiten, zugelassene Script-Aufrufe, Final-Index-Zugriffsregel und Compliance-Output; OI-14c lagert diese Bestände aus `SKILL.md` aus und ersetzt sie dort durch einen Link. Kein Backenddetail, kein Manifest-Schema. |

OI-14b darf nur `GMAIL` und `HIMALAYA` inhaltlich ausbauen, die genau zugeordneten
Backendbestände aus `SKILL.md` entfernen und sie dort durch präzise Links ersetzen.
OI-14c darf nur `BATCH`, `SCHEMA`, `LEGACY` und die neue Datei `CLI` bearbeiten,
die genau zugeordneten Manifest-/CLI-/Compliance-/Schema-Bestände aus `SKILL.md`
entfernen und sie dort durch präzise Links ersetzen. OI-14d verdichtet anschließend
nur den verbleibenden `KERN`, führt den finalen Router-Cut aus und prüft Links sowie
Verlustfreiheit. OI-14a verschiebt nichts.

## Vollständige Abschnittszuordnung

Die Zeilen sind absichtlich nach Unterbereichen gesplittet, wenn ein heutiger
Abschnitt mehrere Zielräume enthält. Damit hat jeder Textbestand genau einen
Folgezielraum. Ein Abschnitt ohne Split bleibt vollständig in seinem genannten Ziel.

| Bestehende Überschrift | Abgedeckter Unterbereich | Zielraum | Umsetzungshinweis / Abhängigkeit |
| --- | --- | --- | --- |
| `## Modell- und Edit-Hinweis` | Modellwarnung, Einzelmail-Grundhaltung und vorsichtige Skill-Änderungen | `KERN` | Kompakt belassen; nicht mit den späteren operativen Compliance-Regeln vermischen. |
| `## Modell- und Edit-Hinweis` | Relative Script-/Hilfsdateipfade und Verbot konkurrierender Pfadvarianten | `CLI` | OI-14c zentralisiert die technische Pfad-/Aufrufkonvention. |
| `## Mutations- und Lock-Vorbedingung` | Gesamter Abschnitt | `KERN` | Harte Präbedingung; muss vor jedem Link auf mutierende Backend- oder CLI-Operationen stehen. |
| `## Backend wählen` | Auswahlpflicht, genau ein Adapter, dauerhafte `message_id` | `KERN` | Nur Auswahl und Identitätsgrenze im Router behalten. |
| `## Backend wählen` | Gmail-Suche, Thread-/Nachrichtenlesen, Routing, Label-Zielverifikation und Gmail-Locator-Felder | `GMAIL` | OI-14b ergänzt nur die Gmail-Detailreferenz. |
| `## Backend wählen` | Himalaya-/IMAP-Suche, Nachrichtenlesen, Routing, Ordner-Zielverifikation und Envelope-Felder | `HIMALAYA` | OI-14b ergänzt nur die Himalaya-/IMAP-Detailreferenz. |
| `## Verbindlicher Arbeitsfluss` | Schritte 1, 3–12, 14 und 16: Scope, Lesegrad-Eskalation, Identität, Dedupe, Katalog-/Kontextladung, Verdichtung, Todo/Reply-Trennung, fachliche Zielentscheidung, Wissenspflege und Kurzbericht | `KERN` | Reihenfolge bleibt im Router, jedoch als kompakter nummerierter Kernfluss. Schritt 10 bleibt konditional, seine Prüfung verpflichtend. |
| `## Verbindlicher Arbeitsfluss` | Schritte 2 und 13 für Gmail: Minimalzugriff, Nachrichtenlesen, Routing und Zielverifikation | `GMAIL` | Kein gemeinsamer Pseudo-Backendablauf. |
| `## Verbindlicher Arbeitsfluss` | Schritte 2 und 13 für Himalaya/IMAP: Minimalzugriff, Nachrichtenlesen, Routing und Zielverifikation | `HIMALAYA` | Kein gemeinsamer Pseudo-Backendablauf. |
| `## Verbindlicher Arbeitsfluss` | Schritt 15: Datenpfade, aktive/offene Dateien, Archivierung, Final-Index-Werkzeug und serielle Writes | `SCHEMA` | Datenformate und Archivpfade gehören zu Schema; Scriptnamen/zugelassener Zugriff gehen zusätzlich nach `CLI`, ohne Regelduplikat. |
| `## Abgrenzung` | Orchestrierung, leichte Logs sowie Übergabe an Projekt-/Topic-Katalog-Skills | `KERN` | Fachliche Zuständigkeitsgrenze bleibt sichtbar. |
| `## Abgrenzung` | Gmail-spezifische Nicht-Dopplungen | `GMAIL` | OI-14b hält die Gmail-Integration an einer Stelle. |
| `## Abgrenzung` | Himalaya-/IMAP-spezifische Nicht-Dopplungen | `HIMALAYA` | OI-14b hält die Himalaya-/IMAP-Integration an einer Stelle. |
| `## Grundregeln` | Untrusted Content, Einzelmail-/kleiner expliziter Batch, Review bei Unsicherheit, Message-ID-first und explizite Freigabe für Senden/Mailbox-Writes | `KERN` | Nicht kürzen oder nur implizit machen. |
| `## Grundregeln` | Gmail-spezifische Sicherheitsregeln für konkrete Mailbox-Aktionen | `GMAIL` | Detailregel nur im Gmail-Adapter. |
| `## Grundregeln` | Himalaya-/IMAP-spezifische Sicherheitsregeln für konkrete Mailbox-Aktionen | `HIMALAYA` | Detailregel nur im Himalaya-/IMAP-Adapter. |
| `## Grundregeln` | Parallel lesbar, gemeinsame `data/mail-desk/`-Writes seriell; Reihenfolge Routing/Verifikation vor Datenpflege | `KERN` | Globale Konkurrrenzinvariante, keine Backendtechnik. |
| `## Fast-Path fuer Spam-Quarantaene-Benachrichtigungen` | Erkennung, konservative Sichtung und frühe fachliche Abzweigung | `KERN` | Als kurzer Sonderpfad vor der Katalog-Triage behalten; er steuert die Reihenfolge. |
| `## Fast-Path fuer Spam-Quarantaene-Benachrichtigungen` | Konkrete Ziele `Junk` bzw. `INBOX` + Review | `FOLDER` | Bereits als Routing-Tabelle vorhanden; bei OI-14d nur verlinken, nicht neu formulieren. |
| `## Lesegrad-Entscheid vor Inhaltsauswertung` | Gesamter Abschnitt: Minimalzugriff, Achsen, Modi, Eskalation, Heuristiken und Arbeitsverdichtung | `KERN` | Fachlicher Qualitätskern, nicht Backend-/CLI-Detail. Bei OI-14d verdichten, ohne die Modi, Eskalationsrichtung oder Verdichtungspflicht zu verlieren. |
| `## Verbindliche Kontextladung vor Klassifikation` | Gesamter Abschnitt | `KERN` | Kataloge vor Klassifikation; fehlende Kataloge stoppen Mailbox-Aktionen. |
| `## Regelbetrieb: Sent-Items-Auswertung (verbindlich)` | Sent-Items als fachliche Quelle, Matching offener Reply-Fälle und Wissensrückführung | `KERN` | Semantik und Reihenfolge bleiben im Kern. |
| `## Regelbetrieb: Sent-Items-Auswertung (verbindlich)` | `sent-index.jsonl`-Felder und Archiv-/Close-Repräsentation | `SCHEMA` | Keine Feldliste im Router duplizieren. |
| `## Regelbetrieb: Sent-Items-Auswertung (verbindlich)` | Gmail-Sent-/Thread-Lesen und Gmail-Locator-spezifische Ausführung | `GMAIL` | OI-14b trennt die Gmail-Transportdetails. |
| `## Regelbetrieb: Sent-Items-Auswertung (verbindlich)` | Himalaya-/IMAP-Sent-Lesen und Envelope-spezifische Ausführung | `HIMALAYA` | OI-14b trennt die Himalaya-/IMAP-Transportdetails. |
| `## Verschiebe-Regel` | Klare Zuordnung, Review-Ausnahmen und Pflicht zum tatsächlichen Routing statt nur Loggen | `KERN` | Unterliegt weiterhin der expliziten Freigabe aus den Grundregeln. |
| `## Verschiebe-Regel` | Konkrete Zielordner und Zielbildung | `FOLDER` | Nicht parallel zur vorhandenen Routing-Tabelle halten. |
| `## Zielordner-Regeln` | Gesamter Abschnitt | `FOLDER` | Bereits inhaltlich doppelt in der vorhandenen Referenz; OI-14d ersetzt ihn durch einen Link. |
| `## Verbindlicher Compliance-Gate (neu)` | Erledigt-Definition, kleinstmöglicher belastbarer Nachweis und Reihenfolge der fachlichen Verifikation | `KERN` | Kurz als Abschluss-Gate im Router behalten. |
| `## Verbindlicher Compliance-Gate (neu)` | Daten-/Final-Index-Update und konkrete Script-gebundene Nachweisführung | `CLI` | OI-14c konsolidiert die Werkzeugebene; `SCHEMA` bleibt für Datenformate maßgeblich. |
| `### Harte Regel: kein manueller Final-Index-Write` | Kein Rohtext-Lesen/-Schreiben, zugelassene Index-Operationen, Umgebungs-/Pfad-Overrides, Normalisierung und Shell-Quoting | `CLI` | Vollständig nach OI-14c; im Kern nur die harte Verweisung „script-basiert, nie manuell“ erhalten. |
| `### Automatisierte Hilfsskripte & Modulare Architektur` | `scripts/core/`, kanonische Envelope-Regel, gemeinsame Script-Zuständigkeiten | `CLI` | OI-14c verhindert zwei konkurrierende CLI-Listen. |
| `### Automatisierte Hilfsskripte & Modulare Architektur` | Batch-Runner, Manifest-Inspektor, Progress/ETA, JSON-Manifeste, Beispiele und Batch-Aufrufe | `BATCH` | OI-14c ordnet die derzeit doppelt nummerierten Toolpunkte ein; keine Details im Router. |
| `### Automatisierte Hilfsskripte & Modulare Architektur` | Himalaya-/IMAP-Client und seine JSON-Input-Regel | `HIMALAYA` | OI-14b; `HIMALAYA` verlinkt bei Manifestform auf `BATCH` statt das Schema zu kopieren. |
| `### Final-Index- und Batch-Regeln` | Finale Location, `upsert-final|patch`, temporäre JSONL-Batches und Cleanup | `BATCH` | Batch-Lebenszyklus und Importregeln sind OI-14c-Manifestdetails. |
| `### Final-Index- und Batch-Regeln` | Final-Index-Feld- und Locator-Bedeutung | `SCHEMA` | Das Schema bleibt alleinige Feldquelle; Backend-Verifikation bleibt im jeweiligen Adapter. |
| `### Pflicht-Output pro verarbeiteter Mail` | Gesamter Abschnitt | `CLI` | OI-14c sammelt die maschinennahe Compliance-Ausgabe; `KERN` verweist am Ende nur darauf, dass der Block Pflicht ist. |
| `## Leichte Daten unter \`data/mail-desk/\`` | Standardpfade, Archivstruktur, Datenminimierung, Kontextsparsamkeit, Index-/Sent-Index-Felder und Idempotenzzugriff | `SCHEMA` | Vollständig in der bestehenden Schemareferenz bündeln; sie ist die einzige Pfad-/Feldquelle. |
| `## Leichte Daten unter \`data/mail-desk/\`` | Verbot manuellen Indexzugriffs und konkrete Index-CLI-Beispiele | `CLI` | Identische Invariante nicht ein zweites Mal im Schema pflegen; `SCHEMA` verlinkt auf `CLI`. |
| `## Erledigungsregel und Archivierung` | Originaleintrag aktualisieren, Close-Schlüssel, Status-/Resolution-Felder, ISO-Archivpfad und Open-vs-Closed-Regel | `SCHEMA` | Bereits im vorhandenen Schema angelegt; OI-14c führt ohne Bedeutungsänderung zusammen. |
| `## Erledigungsregel und Archivierung` | `mail_desk_resolve_case.py`-Aufruf | `CLI` | Nur der konkrete Script-Aufruf wechselt nach `CLI`. |
| `## Zusätzliche Erkennungsregeln (verbindlich)` | Gesamter Abschnitt | `KERN` | Fachliche Pflichtprüfung bei internem Forward plus starkem Fachbetreff. |
| `## Entscheidungskriterien` | Gesamter Abschnitt | `KERN` | Project-/Topic-/Reply-Entscheidung und Sent-Items-Prüfreihenfolge bleiben im Kern. |
| `## Verbindliche Doppelbearbeitung: Routing + Wissenspflege` | Gesamter Abschnitt | `KERN` | Beide Säulen und ihr Zusammenhang sind nicht optional. |
| `## Wissenspflege aus Mails` | Quellengebundene, belastbare Wissenspflege, Zuständigkeitsübergabe, Evidence-Pflicht, Update-Ziele, Unsicherheits-Review und Berichtsinhalt | `KERN` | Inhaltliche Qualitäts- und Quelleninvarianten bleiben beim Mail-Entscheidungsfluss; keine Schema-/CLI-Auslagerung. |
| `### Subtopic-/Workpackage-Regel (verbindlich)` | Gesamter Unterabschnitt | `KERN` | Beibehaltung der Subtopic-/Workpackage-Aktualisierung einschließlich Quellenbezug und Review bei Unsicherheit. |
| `## Review statt Aktion` | Gesamter Abschnitt | `KERN` | Reviewgründe, Ablage und Abgrenzung zu `pending-decisions` bleiben im Router. |
| `## Abschluss-Checkliste (operativ, verpflichtend)` | Punkte 1, 7–9: fachliche Verifikation, Quellen-/Evidence-Prüfung und kontextsparende Prüfung | `KERN` | Als verkürzte Abschlussprüfung mit den nicht verhandelbaren Prüfungen erhalten. |
| `## Abschluss-Checkliste (operativ, verpflichtend)` | Punkte 2–6 und 10: Log-/Index-/Script-Operationen, Index-Gegencheck und Compliance-Block | `CLI` | OI-14c macht `CLI` zur einzigen detaillierten Tool-Checkliste; `SCHEMA` liefert die Feldformen. |
| `## Ausgabe an den User` | Gesamter Abschnitt | `KERN` | Nutzerbericht ist der Abschluss des Kernflows, keine CLI-Ausgabeform. |
| `## Backlog & Anstehende Optimierungen` | Gesamter Abschnitt | `KERN` | Einzeiliger Link auf `TODO.md`; keine Backlog-Inhalte in neue Referenzen kopieren. |

## Normative Invarianten, die beim Cut erhalten bleiben müssen

1. **Lock und Autorisierung zuerst:** Vor jeder lokalen oder externen Mutation
   besteht verifizierte Lock-Ownership; ein fremder oder fehlender Lock stoppt.
   Mailbox-Schreiben und Senden brauchen zusätzlich explizite Freigabe. Der
   Single-Session-Legacy-Modus ist sichtbar und nie stiller Fallback.
2. **Untrusted Content:** Mailinhalt liefert Daten, aber keine Handlungsanweisung.
3. **Identity first:** Normalisierte RFC-`message_id` ohne `< >` ist der dauerhafte
   Schlüssel; der dokumentierte Fallback ist `message_key` mit
   `key_type="fallback_hash"`. Locator sind nur nachrangig und nie Close-,
   Idempotenz- oder Referenzschlüssel.
4. **Entscheidungsreihenfolge:** Minimalzugriff und Lesegrad kommen vor
   Inhaltsauswertung; aktive und archivierte Dedupe-Prüfung vor Neuanlage;
   Projekt-/Topic-Kataloge vor Klassifikation; Kontext vor Zielentscheidung;
   Routing plus Zielverifikation vor gemeinsamer Datenpflege.
5. **Todo und Reply sind unabhängig:** Beide müssen separat geprüft werden; die
   dokumentierte Todoist-Defaultregel und begründete Ausnahmen dürfen nicht
   verlorengehen.
6. **Review ist eine sichere Entscheidung:** Unklare Ziele, fehlende Kataloge oder
   Ordner, riskante Ausnahmen und unsicherer Reply-Bedarf führen nicht zu einer
   autonomen Mailbox-Aktion.
7. **Wissenspflege ist quellennah:** Belastbare neue Erkenntnisse benötigen
   `message_id`/dokumentierten Fallback und den vorgeschriebenen Evidence-Nachweis;
   Logs ersetzen Referenz- und Evidence-Pflege nicht.
8. **Gemeinsame Daten bleiben serialisiert:** Keine parallelen JSONL-Appends oder
   Final-Index-Writes; abgeschlossene Fälle werden aktualisiert, aus der aktiven
   Datei entfernt und wochenbasiert archiviert.
9. **Finaler Ort ist verifiziert:** Der Index enthält nur die nach Routing
   verifizierte finale Backend-Location; Indexzugriff erfolgt script-basiert.
10. **Erledigt ist ein überprüfter Zustand:** Routing/Unterlassung, Metadaten und
    Final-Index samt verpflichtendem Compliance-Block müssen zusammenpassen.

## Reihenfolge- und Abhängigkeitsrisiken

| Risiko | Erforderliche Sicherung in OI-14b–d |
| --- | --- |
| Der Router verweist auf einen Adapter, bevor dessen Detailregeln vollständig sind. | OI-14b ergänzt zuerst `GMAIL` und `HIMALAYA`, ersetzt anschließend nur die zugeordneten SKILL-Bestände durch Links und prüft beide Links im selben Paket. |
| Backendverifikation wird mit der fachlichen Zielentscheidung verwechselt. | `KERN` behält Entscheidung und Reihenfolge; Adapter enthalten nur Transport, Locator und Verifikation. |
| Feldsemantik wird zugleich in Router, CLI-Referenz und Log-Schema gepflegt. | OI-14c macht `SCHEMA` zur alleinigen Datenfeldquelle, entfernt die Detailfeldlisten aus `SKILL.md` und belässt dort nur den Link; `CLI` beschreibt nur den zugelassenen Werkzeugzugriff und verweist auf `SCHEMA`. |
| Die Final-Index-Regel geht beim Split verloren oder wird abgeschwächt. | OI-14c ersetzt die vollständige Zugriffsvorschrift durch den präzisen `CLI`-Link und die Struktur durch den `SCHEMA`-Link; die harte Kurzregel verbleibt im `KERN`. OI-14d prüft die vollständige Verweiskette. |
| Batch- oder Himalaya-Manifeste erzeugen eine zweite, abweichende CLI-Lehre. | OI-14c verlegt Batch-Manifeste nach `BATCH` und ersetzt ihre SKILL-Bestände durch Links; OI-14b hält Himalaya-Details in `HIMALAYA`, das auf `BATCH` verweist; `CLI` benennt nur den gemeinsamen Envelope-/Script-Rahmen. |
| Technische Massenpipeline-Details verbleiben neben dem Einzelfallrouter. | OI-14c verlegt sie nach `BATCH` und entfernt sie aus `SKILL.md`; OI-14d verdichtet nur die verbleibende ausdrücklich erlaubte kleine Batch-Grenze im `KERN`. |
| Routing erfolgt vor Katalog-/Kontextprüfung oder Datenpflege vor Zielverifikation. | Die Reihenfolge aus Invariante 4 bleibt im kompakten Kernfluss nummeriert. |
| Der Compliance-Block wird als bloßes Reporting statt als Erledigt-Gate behandelt. | OI-14c verlegt die Detaildefinition nach `CLI` und ersetzt sie im `SKILL.md` durch einen Link; `KERN` nennt ihn weiterhin ausdrücklich als Abschlussvoraussetzung. |

## Duplikate und offene Reviewpunkte

Diese Punkte sind Befunde, keine in OI-14a aufgelösten Regeln.

| Befund | Betroffene Bestände | Reviewbedarf |
| --- | --- | --- |
| Zielordnerregeln und Spam-Quarantäne-Ziele sind in `SKILL.md` und `folder-rules.md` doppelt. | `## Fast-Path ...`, `## Zielordner-Regeln`, `folder-rules.md` | OI-14d soll nach Linkprüfung nur die frühe Erkennungs-/Reihenfolgelogik im Kern halten und die Zielmatrix einmalig in `FOLDER` führen. |
| Final-Index-Zugriff und CLI-Beispiele sind mindestens in Compliance-Gate, Datenabschnitt und Schema wiederholt. | `### Harte Regel ...`, `## Leichte Daten ...`, `log-schema.md` | OI-14c muss die Zuständigkeit `CLI` (Zugriff) versus `SCHEMA` (Felder) klar verlinken, ohne die harte Regel zu relativieren. |
| Abschlussanforderungen stehen im Flow, Compliance-Gate, Pflicht-Output und Abschluss-Checkliste mehrfach. | `## Verbindlicher Arbeitsfluss`, `## Verbindlicher Compliance-Gate`, `### Pflicht-Output ...`, `## Abschluss-Checkliste` | OI-14d behält nur den fachlichen Gate im Kern und verlinkt den detaillierten Tool-/Output-Check nach `CLI`. |
| Die Tool-Liste ist doppelt nummeriert und vermischt allgemeine, Batch- und Himalaya-Operationen. | `### Automatisierte Hilfsskripte ...` | OI-14c bereinigt die Dokumentstruktur in `CLI`/`BATCH`; OI-14b hält nur Himalaya-spezifische Nutzung in `HIMALAYA`. |
| Der Claim „keine Massenpipeline“ im Frontmatter steht neben Batch-Runner-, `pipeline`- und autonomen Batchdetails. | Frontmatter, Arbeitsfluss/Grundregeln, `batch-runner.md` | **Durch FR-24/MD-R9 erledigt (2026-09-24):** die Phrase ist aus dem Frontmatter entfernt; Batch-/Stapelverarbeitung läuft fachlich durch diesen Skill. Restfrage „Batch-Runner-Details im `KERN` vs. `BATCH`" bleibt Teil des OI-14-Refactors. |
| Der Arbeitsfluss nennt `final_index_lookup.py` und `final_index_upsert.py`, während die späteren Abschnitte den vorhandenen `mail_desk_final_location_index.py` nennen. | Arbeitsfluss Schritt 15, Compliance-/Daten-/Schemaabschnitte | Vor OI-14d gegen tatsächliche Scriptoberfläche prüfen und bewusst entscheiden, welche Namen kanonisch bleiben; OI-14a ändert nichts. |
| Evidence-Pfade sind nicht einheitlich beschrieben. | Batch-Beispiel `memory/references/projects/.../evidence/`; Wissenspflege `memory/evidence/projects|topics/...` mit Legacy-Fallback | Pfad-SSOT und Lesefallback benötigen einen separaten fachlichen Review; beim Refactor nur als unverändert gekennzeichnete Regel übernehmen. |
| Die Schema-Beispiele enthalten teils rohe Message-IDs mit `< >`, während der Skill die operative Normalform ohne Klammern fordert. | `log-schema.md`, Arbeitsfluss, Wissenspflege und Index-CLI-Hinweise | Normalisierungsdarstellung vor einer Schemaänderung prüfen; keine Beispielwerte still umschreiben. |
| `pending-review`/`replies-needed` verwenden generisches `backend_locator`, während die Durable-Identity-Regel backend-spezifische Locatorfelder unterscheidet. | `log-schema.md` | Feldvertrag prüfen, bevor OI-14c die Schema-Dokumentation umordnet. |
| „Klare Zuordnung soll verschoben werden“ und „keine Mailbox-Schreibaktion ohne explizite Freigabe“ müssen zusammen gelten. | `## Verschiebe-Regel`, `## Grundregeln` | Beim Router-Cut Freigabe als übergeordnete Präbedingung explizit vor der Verschiebe-Regel erhalten. |

## Deterministische Abnahme für OI-14a

Vor Übergabe dieses Analysepakets prüfen:

```powershell
$source = Get-Content skills/mail-desk/SKILL.md | Where-Object { $_ -match '^(##|###) ' }
$map = (Get-Content -Raw skills/mail-desk/references/refactor-map.md) -replace '\\`', '`'
$missing = $source | Where-Object { -not $map.Contains($_) }
"SOURCE_HEADINGS=$($source.Count)"; "MISSING_HEADINGS=$($missing.Count)"; $missing
rg -n "references/(folder-rules|batch-runner|log-schema|legacy-cli-adapter|backends/gmail|backends/himalaya|cli-operations)\.md" skills/mail-desk/references/refactor-map.md
git diff --check
git status --short
```

Erwartung: `SOURCE_HEADINGS=28` und `MISSING_HEADINGS=0`; alle Ziele sind relative,
konkrete Referenzpfade oder das künftige `SKILL.md`; der Diff enthält ausschließlich
diese neue, ungestagte Datei. Die Normalisierung im Check behandelt die in
Markdown-Tabellen nötigen escaped Backticks als die ursprüngliche Überschrift.
