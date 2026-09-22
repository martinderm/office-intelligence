---
name: office-intelligence
description: Router für die sieben Office-Intelligence-Sub-Skills. Verwende diesen Bundle-Skill zur Auswahl des passenden Fach-Desks für Cloud-Atlas, Mail-, Meeting-, Event-, Aufgaben-, Projekt- oder Topic-Arbeit; die fachliche Ausführung bleibt beim jeweiligen Sub-Skill.
---

# Office Intelligence

`office-intelligence` ist ein **Skill-Bundle und Router**, kein Agent-Workspace. Es
legt keine eigene Workspace-Control-Plane oder Agent-Lifecycle-Regeln fest. Für eine
konkrete Aufgabe den passenden Sub-Skill öffnen; dessen Anweisungen sind maßgeblich.

## Gemeinsame Mutations- und Lock-Regel

Lesende Auswertungen benötigen keinen Lock. Vor jeder lokalen oder externen Mutation,
die ein Sub-Skill in einem konsumierenden Workspace ausführt, muss dessen
`workspace-lock` mit einer Lease des ausführenden Harnesses erworben sein. Ein aktiver
fremder Lock stoppt die Mutation; ein eindeutig stale Lock darf nur über das reguläre
Tier-2-Takeover übernommen werden. Force-Unlock/-Override erfordert explizite Human
Approval. Fehlt eindeutig verifizierbare Lock-Ownership, wird nicht mutiert.

Die technische Ownership-Prüfung erfolgt vor dem Fach-Skript mit
`workspace-lock/scripts/workspace_lock_guard.py`; der ausführende Harness verwendet
`require_workspace_lock()` mit seiner Lease- oder Conversation-ID. Der Guard bleibt
im gemeinsamen `workspace-lock`-Skill und wird nicht in dieses Bundle kopiert.

Ein lockfreier Lauf ist ausschließlich ein expliziter Single-Session-Legacy-Modus:
keine parallelen Writer, sichtbare Warnung und dokumentierte Ausnahme. Details zu
Mutationen, Nachweisen und ggf. externen Preconditions stehen im zuständigen Sub-Skill.

## Routing

| Wenn die Aufgabe … | Verwende |
| --- | --- |
| projekt- oder topicbezogene Cloud-Verzeichnisse kartiert, konvertiert oder als Filemaps und lokale Mirrors synchronisiert | [cloud-atlas](skills/cloud-atlas/SKILL.md) |
| eine einzelne Mail fachlich beurteilt, routet, auf Antwortbedarf und Todos prüft oder leicht protokolliert | [mail-desk](skills/mail-desk/SKILL.md) |
| Meetings, Konferenzschaltungen oder Vorträge aus einem Adapter oder Upload in Workspace-Evidenz überführt | [meeting-desk](skills/meeting-desk/SKILL.md) |
| eine größere Konferenz, Tagung oder ein Seminar als Event mit Programm, Aufzeichnungen und Folgeaufgaben dokumentiert | [event-documentation](skills/event-documentation/SKILL.md) |
| konkrete Action Items aus Mail, Meeting, Event oder Chat priorisiert, dedupliziert und zur Aufgaben-Synchronisation vorbereitet | [task-desk](skills/task-desk/SKILL.md) |
| einen Eintrag in `memory/references/projects/projects.json` oder die zugehörige Projektstruktur anlegt oder pflegt | [project-catalog-entry](skills/project-catalog-entry/SKILL.md) |
| einen Eintrag in `memory/references/topics/topics.json` oder die zugehörige Topic-/Subtopic-Struktur anlegt oder pflegt | [topic-catalog-entry](skills/topic-catalog-entry/SKILL.md) |

Abgrenzung: `mail-desk` bearbeitet Mailfälle, `task-desk` entscheidet über
nachverfolgbare Aufgaben, und die Katalog-Desks pflegen strukturierte Projekt- bzw.
Topic-Daten. `meeting-desk` behandelt einzelne Meetings; `event-documentation` die
umfassende Dokumentation größerer Veranstaltungen. `cloud-atlas` ist für
Cloud-Speicher und deren lokale Spiegel zuständig, nicht für die Katalogpflege selbst.

## Externe Nachbar-Skills

Technische Integrationen bleiben getrennte, nicht zum Bundle gehörende Skills:
beispielsweise `fireflies-api` oder `zoom-api` für Meeting-Intake, `todoist-api` für
Aufgaben-Synchronisation sowie passende Mailbox-Adapter für Transport und
Mailbox-Aktionen. Der jeweilige Fach-Desk definiert, wann diese Adapter einzubeziehen
sind.

## Entwicklungs- und Architektur-Navigation (System Map)

Für Coding- und Maintenance-Agenten (wie Daedalus oder Antigravity), Refactorings,
Schemas und Invarianten steht die föderierte System Map (ICM Form 6) bereit:
- [Paket System Map (Ebene 1)](docs/system-map/README.md): Gesamtarchitektur, Router-Topologie, gemeinsame Datenzonen und Bundle-Invarianten.
- [Mail-Desk System Map (Ebene 2A)](skills/mail-desk/docs/system-map/README.md): Quarantäne Schema 1, Himalaya-Adapter, Dossier-Modi, 17 Pflichtfelder.
- [Cloud-Atlas System Map (Ebene 2B)](skills/cloud-atlas/docs/system-map/README.md): Filemap-Engine, Dokumentkonvertierung, OCR-Policies, Frontmatter-Schemas.
