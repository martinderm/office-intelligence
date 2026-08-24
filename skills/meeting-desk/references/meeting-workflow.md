# Meeting Workflow & Triage Guidelines

> **Skill:** `office-intelligence/meeting-desk`

---

## 1. Klassifikations-Entscheidungsbaum

```
[Neues Meeting synchronisiert]
        │
        ▼
Ist das Meeting Teil eines Events / einer Konferenz?
  ├── JA  ──► Verschiebe Aufzeichnung nach memory/evidence/topics/<topic>/events/<event-slug>/recordings/
  │           Passe summary_path in meetings.json an.
  │
  └── NEIN ─► Belasse Datei in memory/evidence/meetings/<channel>/
              Ordne Topic oder Projekt zu (topic_slug / project_slug in meetings.json).
```

---

## 2. Synthese & Qualitätskriterien für Zusammenfassungen

Wenn automatische Zusammenfassungen unvollständig sind:
1. **Transkript lesen:** Vollständigen Text durchgehen.
2. **Sprecher zuordnen:** Wer vertritt welche Position?
3. **Ergebnis festhalten:** Was wurde verbindlich vereinbart?
4. **Fristen notieren:** Gibt es konkrete Datumszusagen?
5. **Action-Items exportieren:** Direkte Übergabe an `skills/task-desk`.
