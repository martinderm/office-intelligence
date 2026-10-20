---
name: meeting-desk
description: Zentraler Workflow-Skill für die systematische Erfassung, Klassifikation, Evidenzsicherung und Nachbereitung von Besprechungen, Konferenzschaltungen und Vorträgen innerhalb von office-intelligence. Verwende diesen Skill, wenn Meetings aus SaaS-Adaptern (fireflies-api, zoom-api, Whisper) oder manuellen Uploads in den Workspace integriert, Topics/Projekten zugeordnet, mit Belegankern in Monats-Logs erfasst und Action-Items an den task-desk übergeben werden sollen.
---

# meeting-desk

Zentraler Workflow- und Wissensmanagement-Skill für die operative Besprechungs- und Aufzeichnungsverwaltung nach dem Dual-Evidence-Standard.

---

## 🎯 Zweck & Abgrenzung

- **`office-intelligence/meeting-desk` (dieser Skill)** ist die **fachliche Orchestrierungsschicht**:
  - Steuert den gesamten Lebenszyklus eines Meetings im Workspace.
  - Klassifiziert Meetings in Topics (`topics/<slug>`) oder Projekte (`projects/<slug>`).
  - Sichert die Rohdaten und KI-Zusammenfassungen in **Säule 2 (`memory/evidence/meetings/`)**.
  - Erzeugt maschinenlesbare Beleganker (`### [EVID-...]`) im monatlichen Journal (`YYYY-MM.md`).
  - Extrahiert Folgeaufgaben und übergibt sie strukturiert an **`task-desk`**.
- **SaaS-Adapter (`fireflies-api`, `zoom-api`, etc.)** sind die **technischen Treiber**:
  - Führen reine API-Aufrufe, OAuth/Token-Authentifizierungen, Audio-Uploads und VTT-Downloads aus.

---

## 🏛️ Zielarchitektur (Dual-Evidence-Standard)

Meetings und Aufzeichnungen sind **empirische Roh-Evidenzen (Säule 2)**:

```
<workspace>/
└── memory/
    │
    ├── references/                          # Säule 1 (Normativ / De Jure)
    │   ├── topics/<slug>/index.md           # Offizielle Themenkataloge & Kontakte
    │   └── projects/<slug>/                 # Projektbeschreibungen & Meilensteine
    │
    └── evidence/                            # Säule 2 (Empirisch / De Facto)
        │
        ├── meetings/                        # Nur Register + Fallback, keine Ablage
        │   ├── meetings.json                # Kanonischer Registrierungsindex aller Meetings (SSOT über Pfade)
        │   └── unrouted/                    # Fallback für ungemappte Meetings, mit Review-Marker
        │
        ├── projects/<proj_id>/meetings/     # Projektgebundene Meetings (Summary + Transkript)
        ├── topics/<topic_id>/meetings/      # Topic-only Meetings (Summary + Transkript)
        ├── {projects|topics}/<id>/events/<event-slug>/meetings/  # Event-gebundene Meetings
        │
        ├── topics/<topic_slug>/
        │   └── YYYY-MM.md                   # Monatsjournal mit [EVID-...] Belegankern
        │
        └── projects/<proj_slug>/
            └── YYYY-MM.md                   # Projektbezogenes Monatsjournal
```

**Regeln zum Ablagebaum:** Genau ein physischer Ort pro Meeting, keine Spiegelkopien, kein Kopieren — nur Verschieben. Bei Doppel-Tagging (Projekt + Topic) entscheidet das Topic — es ist der stabilere Ort, Projekte wechseln. `channel` (Fireflies-/Tool-Spezifikum) ist ausschließlich Metadatum in `meetings.json`, niemals Ordnerstruktur. `meetings.json` ist SSOT über alle Pfade. `recordings/`-Ordner sind abgelöst: Sie enthielten ohnehin nur Summary-/Transkript-Paare (keine Audio-/Videodateien) und heißen ab sofort `meetings/`. Jede physische Meeting-Datei ist registriert — Orphans sind unzulässig. (Der Legacy-Baum `memory/references/meetings/` existiert nicht mehr; alte Referenzen darauf sind zu korrigieren.)

*(Hinweis: Die Kanal-Unterordner unter `memory/evidence/meetings/<channel>/` aus Altsystemen werden beim Routing aufgelöst, nicht weitergeführt.)*

---

## 🔄 Der 5-Stufen-Meeting-Workflow

### 1. Intake & Synchronisation
- **Aus Fireflies**: Führe `node .agents/skills/fireflies-api/scripts/sync-meetings-to-memory.mjs` aus.
- **Aus Zoom**: Führe `node .agents/skills/zoom-api/scripts/sync-zoom-to-memory.mjs` aus.
- **Manuelle Transkripte / Chat-Uploads**: Lege die Datei im Meeting-Ordner ab und trage sie in `meetings.json` ein (`source: "manual"`).

### 2. Qualitätsprüfung & Zusammenfassungs-Synthese
- Prüfe, ob die importierte Zusammenfassung vollständig und aussagekräftig ist.
- **Korrektur bei Qualitätsmängeln** (z. B. aufgebrauchte Fireflies-Credits oder unzureichende Notizen): Erstelle die Zusammenfassung **aktiv neu anhand des immer vollständigen Transkripts** (Aufbau: Kernthemen, Beschlüsse, Standpunkte der Teilnehmer, nächste Schritte).
- **Workspace-Kontext-Pflicht:** Beziehe bei Ersterfassung und jeder nachträglichen Korrektur die Workspace-Wissensbasis mindestens oberflächlich ein — relevante `topics.json`- / `projects.json`-Einträge, Topic-/Projekt-Dokumente (Dossier, Annex, Feldinventar, Hands-on-Dateien), Operations-Notizen. Typische Ziele: Speech-to-Text-Fehlzuordnungen bei Eigennamen (Projekte, Personen, Abkürzungen wie USAGE-NG, AUCEN, MESHE, KI), Projektname-Konfusionen, Rollen-/Kontextfehler. **Ohne Beleganker in die Workspace-Wissensbasis bleibt das Original stehen** und wird in der Erfassungs-Notiz als „offen/unklar" markiert — es wird nichts geraten.
- **Speaker-Attribution:** Jede Aussage in der Summary ist dem konkreten Sprecher zugeordnet. Bei Unsicherheit ist das auszuweisen (z. B. „(Sprecher unklar)"), statt pauschal zuzuordnen.

### 2b. Transkript-Korrekturen mit Provenienz
- Das Roh-Transkript (`<slug>.transcript.raw.md`) bleibt unantastbar — Beweisebene, so wie es vom Adapter ankam.
- Die bereinigte Fassung (`<slug>.transcript.md`) enthält Korrekturen **lesbar als Prosa-Paar** `„Original" → „Korrektur"` mit nachgestelltem Beleganker (Kategorie + Workspace-Referenz, z. B. `korr: Hörfehler → USAGE-NG-Projekt`).
- **Nur entity-level:** Eigennamen, Abkürzungen, IDs, Fachbegriffe — kein stilistisches Umschreiben von Inhalten.
- **Bereinigungsliste am Dateikopf:** alle Korrekturen des Transkripts zusätzlich als zentrale Liste; optional `corrections_count` in `meetings.json`.
- Die bereinigte Fassung ist aus Rohtranskript plus Korrekturliste deterministisch ableitbar.

### 3. Thematische Klassifikation & Verortung
- Ordne das Meeting einem bestehenden Topic (aus `memory/references/topics/`) oder Projekt (aus `memory/references/projects/`) zu.
- **Event-Sonderfall**: Handelt es sich um den Vortrag einer Konferenz, verschiebe Summary und Transkript in den Event-Ordner `memory/evidence/{projects|topics}/<id>/events/<event-slug>/meetings/` und passe die Pfade in `meetings.json` an. Es wird verschoben, nicht kopiert — keine Doppelhaltung.

### 4. Evidenzsicherung & Beleganker im Monats-Log
- Trage eine Zusammenfassung des Meetings in das zentrale Monats-Journal (`memory/evidence/topics/<topic>/YYYY-MM.md`) ein.
- **Verbindlicher Beleganker:**
  ```markdown
  ### [EVID-YYYY-MM-DD-XX] Besprechung "<Titel>"
  * **Datum:** YYYY-MM-DDTHH:MM:SS+02:00
  * **Teilnehmer:** <Name 1>, <Name 2>
  * **Quelle:** Meeting (`meeting_id: <id>`, `summary: memory/evidence/.../meetings/<slug>.summary.md` — kanonischer Pfad aus `meetings.json`)
  * **Wesentliche Beschlüsse:**
    - Beschluss 1
    - Beschluss 2
  * **Tags:** `[meeting, <topic>, <stichwort>]`
  ```

### 5. Action-Item Extraktion & Übergabe an `task-desk`
- Extrahiere alle offenen Arbeitsaufgaben, Deadlines und Zusagen aus dem Meeting.
- Übergebe sie an den **`task-desk`** (`skills/task-desk`), der die Deduplizierung, Projekt-Routing und Synchronisation mit Todoist (unter Mitführung des `[EVID-...]`-Belegankers) übernimmt.

---

## 📚 Referenzen

- [`references/data-model.md`](references/data-model.md) — Detailliertes JSON-Schema für `meetings.json` und Frontmatter-Attribute.
- [`references/meeting-workflow.md`](references/meeting-workflow.md) — Vertiefte Handlungsanweisungen für Klassifikation und Sonderfälle.
