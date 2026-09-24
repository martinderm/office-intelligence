# office-intelligence

`office-intelligence` ist ein Skill-Bundle für nachvollziehbare Office-, Wissens- und
Verwaltungsarbeit nach dem Dual-Evidence-Ansatz. Der Root-[`SKILL.md`](SKILL.md) ist
ein Router: Er wählt einen Fach-Desk aus, ersetzt ihn aber nicht. Das Bundle ist kein
Agent-Workspace und bringt keine eigene Workspace-Control-Plane oder
Agent-Lifecycle-Verwaltung mit.

## Enthaltene Sub-Skills

| Sub-Skill | Einsatzbereich | Nicht zuständig für |
| --- | --- | --- |
| [cloud-atlas](skills/cloud-atlas/SKILL.md) | Projekt- und Topic-Cloudspeicher, Konvertierung, Filemaps und lokale Markdown-Mirrors | Projekt-/Topic-Katalogeinträge |
| [mail-desk](skills/mail-desk/SKILL.md) | Fachliche Bearbeitung einzelner Mails sowie Batch-/Stapelverarbeitung (draft→execute→verify): Routing, Reply-/Todo-Entscheidungen, Anhang-Bewertung und leichte Mail-Logs | Mailbox-Transport und vollständige Aufgaben-Triage |
| [meeting-desk](skills/meeting-desk/SKILL.md) | Intake, Klassifikation, Evidenz und Nachbereitung einzelner Meetings, Konferenzschaltungen und Vorträge | Dokumentation eines gesamten größeren Events |
| [event-documentation](skills/event-documentation/SKILL.md) | Konferenzen, Tagungen und Seminare mit Programm, Aufzeichnungen und Action-Item-Triage | allgemeiner Meeting-Intake außerhalb eines Events |
| [task-desk](skills/task-desk/SKILL.md) | Action-Item-Extraktion, Priorisierung, Deduplizierung und Vorbereitung der Aufgaben-Synchronisation | Mailbox-Routing oder Pflege von Katalogstrukturen |
| [project-catalog-entry](skills/project-catalog-entry/SKILL.md) | `projects.json` und zugehörige Projektarbeits- und Wissensstruktur | Topic-/Subtopic-Katalogpflege |
| [topic-catalog-entry](skills/topic-catalog-entry/SKILL.md) | `topics.json` und zugehörige Topic-/Subtopic-Arbeits- und Wissensstruktur | Projektkatalogpflege |

`mail-desk` ist grundsätzlich ein kontrollierter Fall-für-Fall-Workflow; Batch- und
Stapelverarbeitung (draft→execute→verify) läuft fachlich durch den mail-desk-Skill —
der Batch-Runner und seine Werkzeuge setzen dessen Verträge (Review-Hash-Bindung,
Final-Index-Hardrules, JSON-Manifest-Client, Katalogpflege-Router) durch. Jede Mail
durchläuft denselben vollständigen Compliance-Flow; die Desk-Regeln bestimmen die
Grenzen. Consumer-Pipelines (z. B. eine Batch-SOP im konsumierenden Workspace) laden
vor allen Phasen die Pflicht-Referenzen des Skills und ersetzen sie nicht
(„Pipeline und Fachvertrag").

## Installation und Nutzung

Installiere oder verlinke den vollständigen Ordner `office-intelligence/` einschließlich
aller sieben Ordner unter `skills/`. Öffne zunächst den Root-[`SKILL.md`](SKILL.md) und
danach nur den für die konkrete Aufgabe passenden Sub-Skill. Konsumierende Workspaces
stellen ihre eigenen Kataloge, Evidenz- und Datenpfade bereit; die dort geltenden
Workspace-Regeln bleiben maßgeblich.

Vor einer Mutation im konsumierenden Workspace ist dessen `workspace-lock` mit einer
Lease des ausführenden Harnesses erforderlich. Lesende Auswertungen sind lockfrei. Ein
lockfreier Schreiblauf ist nur als ausdrücklich gewählter, dokumentierter
Single-Session-Legacy-Modus zulässig; die Details stehen in den jeweiligen Sub-Skills.

## Adapter und Nachbar-Skills

Die sieben oben aufgeführten Ordner sind die Mitglieder dieses Bundles. Technische
Adapter gehören nicht dazu und werden bei Bedarf separat eingebunden, etwa
`fireflies-api` oder `zoom-api` für Meeting-Intake, `todoist-api` für Aufgaben-Sync
sowie passende Gmail-, Himalaya- oder IMAP-Adapter für Mailbox-Zugriff. Die fachliche
Entscheidung bleibt jeweils beim zuständigen Office-Intelligence-Desk.

## Referenzmodell

Die Katalog-Desks arbeiten mit `memory/references/projects/projects.json` und
`memory/references/topics/topics.json`. Fachliche Struktur liegt in den zugehörigen
Projekt- und Topic-Unterordnern; operative Nachweise bleiben von diesen Referenzen
getrennt. Konkrete Pfade, Formate und Schreibregeln sind absichtlich nur in den
zuständigen Sub-Skills beschrieben.

## Architektur und System Map

Für Entwickler, Refactorings und Coding-Agenten (wie Daedalus) existiert eine vollständige,
föderierte System Map nach ICM Form 6:
- [Paket System Map (L1)](docs/system-map/README.md): Router, Zusammenspiel der 7 Desks, Data Zones, Bundle-Invarianten.
- [Mail-Desk System Map (L2)](skills/mail-desk/docs/system-map/README.md): Quarantäne-Engine, Himalaya-Adapter, Batch-Runner/-Verträge, Dossier-Modi.

Ein kompaktes Änderungsprotokoll liegt im [`CHANGELOG.md`](CHANGELOG.md).
- [Cloud-Atlas System Map (L2)](skills/cloud-atlas/docs/system-map/README.md): Filemap-Generierung, Dokumentkonvertierung, OCR-Verzweigung.
