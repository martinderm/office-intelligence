# Filemap-Schema (OI-08)

`filemap.json` ist ein `cloud-filemap`-Container und kein einzelnes
`data-zone-artifact`. Der Container beschreibt den Scope, den Storage, den
gescannten Pfad und ein Inventar von Quelldateien. Die bestehenden
Inventarfelder (`version`, `mtime`, `size`, `sha256`, `description`,
Konvertierungsstatus und kuratierte Zusatzfelder) bleiben kompatibel.

Für einen konvertierten lokalen Markdown-Mirror kann ein Dateieintrag
`artifact_metadata` enthalten. Dieses Objekt ist ausschließlich der kanonische
Cloud-Metadatensatz des konkreten Mirrors und folgt dem normativen
`data-zone-artifact.schema.json` aus `Shared-Memory`; die Filemap selbst wird
nicht gegen dieses Artifact-Schema validiert. Legacy-Mirrors ohne kanonisches
Frontmatter bleiben lesbar und erhalten kein künstlich erzeugtes
`artifact_metadata`.

Die Generator-Ausgabe verwendet `schema_version: 1` und wird vor dem atomaren
Schreiben deterministisch geprüft. Dateischlüssel und Pfade müssen
workspace-relativ sein; SHA-256-Werte werden als 64-stellige Hexwerte und
Zeit-/Größenfelder in den bestehenden Filemap-Formaten geprüft. Unbekannte
Dateieintragsfelder bleiben für manuell kuratierte Metadaten zulässig.

## Einzeldatei-Merge (Issue #22)

`convert_cloud_docs.py --only-file` erzeugt oder ersetzt genau ein `files`-Mitglied
einer bestehenden Filemap per Roh-Splice; alle übrigen Mitglieder und Container-Felder
(auch `updated_at`) bleiben Zeichen für Zeichen unverändert. Vor dem Schreiben gilt ein
Runtime-Subset des Vertrags oben (Container, alle Einträge, keine doppelten JSON-Schlüssel).
Der Eintrag trägt wie beim Generator `artifact_metadata` des geschriebenen Mirrors.
Konvertierte Artefakte liegen in einem neuen Versionsverzeichnis unter
`<output_dir>/_versions/`; `markdown_mirror` und `derivative.path` zeigen dorthin und sind
**nicht stabil** über Läufe. Die Filemap ist die Quelle der Wahrheit; alte und
liegengelassene Versionen bleiben erhalten und werden **nicht** aufgeräumt (der Version-Root
hat keine Aufräumlogik). **Die Recovery entfernt nie eine Datei und verschiebt nie ein
Artefakt**, sie archiviert auch kein Journal: `planned`/`staged`-Reste bleiben an ihrem Platz
und werden als `retained_version` gemeldet — stets das real existierende
Versionsverzeichnis. Eine bereits committete, später überholte Version ist historisch und wird nicht
gemeldet. Der Filemap-Replace
ist der einzige Commit-Punkt. Das minimale Intent-Journal unter
`<output_dir>/.only-file-journal/` ist kein Filemap-Bestandteil; es koordiniert die Version
und die Recovery (SKILL.md §8) und wird **nie** umbenannt, archiviert oder gelöscht — es
bleibt nach jedem Lauf liegen. Ein früherer Lauf gilt nur dann als abgeschlossen, wenn
`published`/`committed` gespeichert ist oder der Live-Eintrag dem aufgezeichneten neuen
Digest entspricht; jeder andere Zustand ist unklar und blockiert den nächsten Lauf
fail-closed ohne jede Mutation. Vor jeder Journalmutation prüft der Lauf alle Einträge der
Ziel-Filemap (auch den der Quelle, inkl. Windows-Aliasen); beansprucht einer davon den
Journalpfad, endet er fail-closed (`journal`). Versionen werden **direkt**
in ihr endgültiges Verzeichnis geschrieben (kein Verzeichnis-Rename), der Worker löscht
keine Artefakte (DOC-Staging und LibreOffice-Profil bleiben im Versionsverzeichnis);
einzige zulässige `os.replace`-Aufrufe sind Filemap und Journal. Sein
Filemap-Temp liegt im Journal-Verzeichnis, daher müssen Journal und Filemap auf demselben
Dateisystem liegen. Details: [SKILL.md](../SKILL.md).

**Bewusste Scope-Reduktion:** keine Fremdreferenz-Ermittlung (Schutz nur gegen die eigene
Ziel-Filemap), keine physische Quarantäne, keine Archivierung, kein Aufräumen, keine
Sibling-Koordination und kein Vollscan-Anteil. Ein regulärer Vollscan kennt den Version-Root
nicht; er kann versionierte Quellen erneut konvertieren und versionierte Mirror als Waisen
entfernen.

## Retention-Klassifikation (Issue #97)

Die Filemap bleibt die einzige Autorität für die aktive Version: Ein
Versionsverzeichnis unter `<output_dir>/_versions/<quell-key>/<run>/` ist `active`,
wenn `markdown_mirror` oder `derivative.path` desselben Eintrags dorthin zeigt;
Windows-Pfadalias (case-insensitiv, abschließende Punkte/Leerzeichen je Komponente
ignoriert) gilt **nur auf Windows**, auf case-sensitiven Plattformen muss der Pfad
exakt passen (sonst `unresolved`). Der separate, rein lesende `retention_audit.py`
klassifiziert weitere Versionen als `retained` (neueste 2 inaktive Versionen je
Quell-Key oder ≤ 90 Tage) oder `quarantine_candidate` (> 365 Tage oder außerhalb des
Counts und > 90 Tage). **Die 365-Tage-Regel hat Vorrang:** eine >365-Tage-Version ist
auch dann Kandidat, wenn sie unter den neuesten zwei liegt oder ≤ 90 Tage alt wäre.
Das Alter wird ungerundet aus der mtime berechnet. Referenzierte Link-/Junction-Runs
bleiben `active` und werden nie traversiert; Cloud-Platzhalter in Quell-Key-, Run-
oder Inhaltsverzeichnissen werden vor Auflösung/Traversierung geprüft und nicht
traversiert. Ein unlesbarer Quell-Key mit aktiver Referenz wird `unresolved`
(`potentially_active: true`), nicht Kandidat. Eine Filemap ohne deklariertes
`output_dir` wird fail-closed abgewiesen. Die Identität wird über die nur gelesenen
`projects.json`/`topics.json` an `output_json` + `output_dir` gebunden (inkl.
`filemap_json`/`cloud_mirror_dir` und synthetisierter Defaults); jeder
Katalog-Lese-/Parsefehler verhindert `verified`, und ohne genau einen fehlerfreien
Treffer ist `classification_authoritative: false` und Kandidaten sind
nicht-autoritative Prüfhinweise. Output, Filemap und Katalogpfade werden
komponentenweise vor `realpath`/`scandir`/`open` geprüft; Links/Junctions/
Platzhalter in Output-/Filemap-Vorfahren brechen den Audit ab, in
Katalog-Vorfahren verhindern sie nur das Lesen/Verifikation. Das sind Empfehlungen, keine
Speicherobergrenze und keine Löschfreigabe: Der Audit schreibt, verschiebt, löscht
und archiviert nichts, legt `<output_dir>/_quarantine/` nicht an und liest keine
fremden Filemaps. Journale bleiben `journal_not_managed`.
Alle Einträge tragen `action: "none"`, `foreign_reference_risk: "unknown"`,
`safe_to_delete: false` und `safe_to_move: false`; manuelles Verschieben/Löschen
liegt außerhalb des Produktvertrags. Die #22-Felder der Filemap ändern sich durch
diese Policy nicht. Details: [SKILL.md](../SKILL.md#retention-policy).

## Scan-Provenienz: `exclude_dirs` (Issue #2)

Neue Filemaps enthalten zusätzlich `exclude_dirs`: die normalisierten,
scan-relativen, literalen Verzeichnisausschlüsse des Storages (Standard `[]`).
Das additive Feld bleibt in Schema 1 optional, damit bestehende Maps gültig
bleiben. Der Runtime-Validator prüft kanonische Ausschlüsse und weist weiterhin
inventarisierte Dateien aus ausgeschlossenen Verzeichnissen zurück. Das JSON-Schema
prüft Array und sichere relative Einträge; Normalisierung, Komponentenvergleich
und Inventar-Scope sind semantische Runtime-Prüfungen.
Die Konverter-Ausgabe und die lesbare Filemap tragen dieselbe Provenienz.
Konfiguration und konservatives Cleanup sind in [SKILL.md](../SKILL.md#verzeichnisausschlüsse-pro-storage-exclude_dirs-issue-2)
beschrieben. Ausgeschlossene Alt-Artefakte bleiben auf Disk, fehlen aber im aktiven Inventar.
