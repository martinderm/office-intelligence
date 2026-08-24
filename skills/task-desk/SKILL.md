---
name: task-desk
description: Zentraler Workflow-Skill für die Extraktion, Triage, Priorisierung, Deduplizierung und Synchronisation von Aufgaben (Action Items) innerhalb von office-intelligence. Verwende diesen Skill, wenn aus E-Mails (mail-desk), Besprechungen (meeting-desk), Event-Dokumentationen (event-documentation) oder Chat-Anweisungen konkrete Arbeitsaufgaben entstehen, die gegen die lokalen Routing-Regeln geprüft, dedupliziert und über SaaS-Adapter (todoist-api) mit lückenloser Factored Attribution ([EVID-...]) synchronisiert werden sollen.
---

# task-desk

Zentraler Workflow- und Triage-Skill für alle operativen Aufgaben, Fristen und Folgeaktivitäten im Agenten-Workspace.

---

## 🎯 Zweck & Abgrenzung

- **`office-intelligence/task-desk` (dieser Skill)** ist die **fachliche Triage- und Kontrollschicht**:
  - Sammelt Aufgaben aus allen Eingangskanälen (`mail-desk`, `meeting-desk`, `event-documentation`, Chat).
  - Wendet die normativen Workspace-Routing-Regeln (`memory/references/todos/routing-rules.md`, `projects.json`) an.
  - Prüft gegen bereits synchronisierte Aufgaben (`memory/evidence/todos/created-tasks.json`), um Duplikate zu verhindern.
  - Formuliert den Task präzise in eigenen Worten und reichert ihn mit dem standardisierten **Beleganker (`Factored Attribution`)** im `description`-Feld an.
- **SaaS-Adapter (`todoist-api`, etc.)** sind die **technischen Treiber**:
  - Führen reine REST-Aufrufe, Quick Adds und Sync-Operationen aus.

---

## 🏛️ Wissensschichten (Dual-Evidence-Standard)

```
<workspace>/
└── memory/
    │
    ├── references/todos/                    # SÄULE 1 (Normativ / Regeln)
    │   ├── README.md                        # Struktur & Prinzipien des Task-Managements
    │   ├── todoist-usage.md                 # Leitlinien für Prioritäten, Sections & Deadlines
    │   ├── routing-rules.md                 # Zuordnungsregeln zu Projekten & Sections
    │   └── projects.json                    # Lokaler Cache bekannter Projekt- und Section-IDs
    │
    └── evidence/todos/                      # SÄULE 2 (Empirisch / State)
        ├── created-tasks.json               # Minimaler Dedupe- und Trace-Index angelegter Tasks
        └── review-queue.json                # Unklare Fälle zur menschlichen Freigabe/Triage
```

---

## 🔄 Der 4-Stufen-Task-Triage-Workflow

### 1. Ingestion & Vorprüfung
- Nimm die rohe Aufgabenstellung aus der Quelle entgegen (E-Mail, Meeting-Transcript, Konferenzprogramm oder User-Prompt).
- **Prüfe die Relevanz:** Ist dies eine echte, terminierte Aufgabe für den User/Agenten oder bloß eine informative Randnotiz?

### 2. Routing & Priorisierung
- Konsultiere `memory/references/todos/routing-rules.md` und `projects.json`:
  - Zu welchem **Projekt** (`project_id`) gehört die Aufgabe?
  - In welche **Section** (`section_id`) gehört sie?
  - Welche **Priorität** (`p1` bis `p4`) und welches **Fälligkeitsdatum** (`due_date`) sind angemessen?

### 3. Deduplizierung & Triage
- Prüfe `memory/evidence/todos/created-tasks.json`:
  - Wurde eine identische Aufgabe für diese `message_id`, `meeting_id` oder diesen Event-Slot bereits angelegt?
  - Falls ja: Überspringe die Neuanlage (Idempotenz).
  - Falls unklar: Trage den Fall in `review-queue.json` ein.

### 4. Factored Attribution & Dispatch via `todoist-api`
- Erstelle den Task strukturiert über `todoist-api` (`node .agents/skills/todoist-api/scripts/create-task.mjs`).
- **Verbindlicher Herkunftsnachweis im `description`-Feld:**
  ```markdown
  Aktion: Kurze operative Handlungsanweisung in eigenen Worten
  Quelle: [EVID-YYYY-MM-DD-XX] <Kontext-Titel>
  Trace-ID: <message_id_or_meeting_id_or_event_slug>
  ```
- Trage die erzeugte Todoist-Task-ID in `memory/evidence/todos/created-tasks.json` ein.

---

## 📚 Referenzen

- [`references/task-routing.md`](references/task-routing.md) — Regeln für Prioritäten, Fälligkeiten und Section-Zuweisung.
