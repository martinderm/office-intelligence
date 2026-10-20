# Meeting Workflow & Triage Guidelines

> **Skill:** `office-intelligence/meeting-desk`

---

## 1. Klassifikations-Entscheidungsbaum

```
[Neues Meeting synchronisiert]
        │
        ▼
Ist das Meeting Teil eines Events / einer Konferenz?
  ├── JA  ──► Verschiebe Summary + Transkript nach memory/evidence/{projects|topics}/<id>/events/<event-slug>/meetings/
  │           (Bestehende recordings/-Ordner werden nach meetings/ umbenannt.)
  │           Passe summary_path + transcript_path in meetings.json an. Kein Kopieren.
  │           Bei Doppel-Tagging (Projekt + Topic) route nach Topic (stabilerer Ort).
  │
  └── NEIN ─► Route nach Zugehörigkeit: memory/evidence/projects/<proj_id>/meetings/ bzw. memory/evidence/topics/<topic_id>/meetings/, unmapped nach memory/evidence/meetings/unrouted/ (mit Review-Marker)
              Ordne Topic oder Projekt zu (topic_slug / project_slug in meetings.json).
              channel bleibt Metadatum, wird nie Ordnerstruktur.
```

---

## 2. Synthese & Qualitätskriterien für Zusammenfassungen

Wenn automatische Zusammenfassungen unvollständig sind:
1. **Workspace-Kontext laden:** Vor jeder Synthese und jeder Korrektur relevante `topics.json`- / `projects.json`-Einträge, Topic-/Projekt-Dokumente (Dossier, Annex, Feldinventar, Hands-on-Dateien) sowie Operations-Notizen mindestens oberflächlich einbeziehen — ohne Beleganker keine Korrektur, Fundstelle als „offen/unklar" markieren.
2. **Transkript lesen:** Vollständigen Text durchgehen.
3. **Sprecher zuordnen:** Wer vertritt welche Position? Jede Aussage bekommt ihren konkreten Sprecher; Unsicherheit ausweisen („Sprecher unklar"), nie pauschal zuordnen.
4. **Ergebnis festhalten:** Was wurde verbindlich vereinbart?
5. **Fristen notieren:** Gibt es konkrete Datumszusagen?
6. **Action-Items exportieren:** Direkte Übergabe an `skills/task-desk`.
7. **Transkript-Provenienz sichern:** Roh-Transkript (`<slug>.transcript.raw.md`) unangetastet lassen; Korrekturen nur in der bereinigten Fassung (`<slug>.transcript.md`) als lesbares Prosa-Paar `„Original" → „Korrektur"` mit Beleganker (Kategorie + Workspace-Referenz), nur entity-level (Eigennamen, Abkürzungen, IDs, Fachbegriffe), Bereinigungsliste am Dateikopf.
