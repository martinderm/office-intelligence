---
name: office-intelligence
description: Umfassende Orchestrierungs-Suite für alle täglichen Office-, Wissens- und Verwaltungs-Workflows nach dem Dual-Evidence-Standard. Verwende diesen Root-Skill zur Orientierung und wähle für konkrete Aufgaben den passenden Fach-Desk unter skills/ (mail-desk, meeting-desk, event-documentation, task-desk, topic-catalog-entry, project-catalog-entry).
---

# Office Intelligence

Zentrale Orchestrierungs- und Workflow-Suite für alle operativen Büro-, Kommunikations- und Wissensmanagement-Aufgaben im Agenten-Workspace.

---

## 🧭 Verfügbare Fach-Desks unter `skills/`

Wähle für konkrete Arbeitsabläufe direkt den spezialisierten Sub-Skill:

1. **`skills/mail-desk` — E-Mail-Management & Posteingang**
   - Sichten, Priorisieren, Klassifizieren und Beantworten von E-Mails.
   - Sichern von Mail-Signalen als Belege in Monats-Logs (`memory/evidence/topics/<slug>/YYYY-MM.md`).

2. **`skills/meeting-desk` — Besprechungen & Aufzeichnungen**
   - Lifecycle-Management für Meetings aus SaaS-Adaptern (`fireflies-api`, `zoom-api`) oder manuellen Uploads.
   - Transkript-Synthese, Qualitätskontrolle von Zusammenfassungen und Ablage in `memory/evidence/meetings/`.
   - Thematische Zuordnung und Übergabe von Action-Items an den `task-desk`.

3. **`skills/event-documentation` — Konferenzen & Veranstaltungen**
   - 2-Säulen-Dokumentation für größere Events.
   - Trennung von offiziellen Programmen (`references/.../events/index.md`) und Aufzeichnungen/Notizen (`evidence/.../events/`).

4. **`skills/task-desk` — Aufgaben-Triage & Todoist-Synchronisation**
   - Zentrale Bündelung aller Folgeaufgaben aus Mails, Meetings, Events und Chat.
   - Anwenden von Routing-Regeln (`references/todos/routing-rules.md`) und Deduplizierung (`evidence/todos/created-tasks.json`).
   - Synchronisation mit Todoist unter lückenloser **Factored Attribution** (`description: "Quelle: [EVID-...]"`).

5. **`skills/topic-catalog-entry` — Themen-Taxonomie**
   - Strukturierte Neuanlage und Pflege von Fachthemen (`memory/references/topics/<slug>/`).

6. **`skills/project-catalog-entry` — Projekt-Katalog**
   - Strukturierte Neuanlage und Pflege von Projekten (`memory/references/projects/<slug>/`).

---

## 🛠️ Zusammenspiel mit SaaS-Adaptern

`office-intelligence` steuert die fachlichen Arbeitsabläufe und delegiert technische API-Calls an die zustandslosen Adapter:
- **Audio & Transkripte:** `fireflies-api`, `zoom-api`
- **Aufgaben-Sync:** `todoist-api`
- **Mailbox-Transport:** `himalaya`, `gmail`
