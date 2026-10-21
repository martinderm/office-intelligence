# Meeting Workflow & Triage Guidelines

> **Skill:** `office-intelligence/meeting-desk`

---

## 1. Klassifikations-Entscheidungsbaum

```
[Adapter/Operator erzeugt genau einen meeting-record/1]
        │
        ▼
ingest_meeting.py validiert Record und Katalogauswahl
        │
        ├── Fehler vor dem Write ──► Exit 2, keine Mutation
        ├── offenes Journal ──────► Exit 3, zuerst --inspect/--recover
        │
        ▼
Routing (vertrauenswürdig, nicht aus Record-Hints)
  ├── --event <slug> + --topic/--project
  │      ──► memory/evidence/{topics|projects}/<id>/events/<event>/meetings/
  ├── --topic <slug>  ──► memory/evidence/topics/<id>/meetings/
  ├── --project <slug> ─► memory/evidence/projects/<id>/meetings/
  └── keine Auswahl
         ├── bestehende menschliche Zuordnung bleibt erhalten
         └── neu ──► memory/evidence/meetings/unrouted/ (unmapped, Review)
```

Topic gewinnt bei Doppelzuordnung (stabilerer Ort), das Event muss zum gewählten
katalogisierten Owner gehören. `channel` bleibt Metadatum, nie Ordnerstruktur.
Es wird verschoben, nicht kopiert; die Registerpfade sind SSOT.

---

## 2. Record, Writer und Register

- Adapter liefern Offline-Records (`meeting-record/1`, siehe
  [`data-model.md`](data-model.md)); sie schreiben weder Dateien noch Register.
- Der Writer prüft Record-Version, Typen, Größen/Tiefe, Eigentümer-Lease,
  Katalogbindung und alle Zielpfade **vor** der ersten Mutation.
- Neue Dateien werden no-clobber erstellt; generierte Updates nur gegen den
  gebundenen Ownership-Hash. Nicht Writer-eigene Dateien werden nicht
  überschrieben. Raw bleibt unveränderlich.
- Ein unveränderter Reingest ist ein No-op und erhält `accepted`-Inhalte sowie
  menschliche Reviewentscheidungen. Neuer/geänderter Inhalt wird `provisional`
  und reviewpflichtig; ein Import ist kein Content-Approval.
- Manuell/lokal: Plan/Dry-run ist Standard, Apply verlangt den Plan-Hash.
  `--local --summary ... [--transcript ... [--raw ...]]` nutzt denselben Record
  und Writer; Quellbytes bleiben exakt, Raw unverändert, Quellen werden erst
  nach der Register-Publikation hashgebunden verschoben. Unbekannte Optionen
  (z. B. `--upgrade`) sind Fehler, keine stille Migration.
- Fehlendes Register ≠ beschädigtes Register: ein fehlendes Register wird neu
  angelegt, ein beschädigtes oder nacktes Array wird abgewiesen.

---

## 3. Synthese & Qualitätskriterien für Zusammenfassungen

Wenn automatische Zusammenfassungen unvollständig sind:
1. **Workspace-Kontext laden:** Vor jeder Synthese und jeder Korrektur relevante `topics.json`- / `projects.json`-Einträge, Topic-/Projekt-Dokumente (Dossier, Annex, Feldinventar, Hands-on-Dateien) sowie Operations-Notizen mindestens oberflächlich einbeziehen — ohne Beleganker keine Korrektur, Fundstelle als „offen/unklar" markieren.
2. **Transkript lesen:** Vollständigen Text durchgehen.
3. **Sprecher zuordnen:** Wer vertritt welche Position? Jede Aussage bekommt ihren konkreten Sprecher; Unsicherheit ausweisen („Sprecher unklar"), nie pauschal zuordnen.
4. **Ergebnis festhalten:** Was wurde verbindlich vereinbart?
5. **Fristen notieren:** Gibt es konkrete Datumszusagen?
6. **Action-Items exportieren:** Direkte Übergabe an `skills/task-desk`.
7. **Transkript-Provenienz sichern:** Roh-Transkript (`<slug>.transcript.raw.md`) unangetastet lassen; Korrekturen nur in der bereinigten Fassung (`<slug>.transcript.md`) als lesbares Prosa-Paar `„Original" → „Korrektur"` mit Beleganker (Kategorie + Workspace-Referenz), nur entity-level (Eigennamen, Abkürzungen, IDs, Fachbegriffe), Bereinigungsliste am Dateikopf.
   Für den Builder gelten [CLI und Prüfschritte](../scripts/README.md): geprüfte Korrekturdatei, keine Gesprächskontext-Deutung ohne Workspace-Beleg, stille Regeln nur für bedeutungsneutrale Schreibweisen. Der Builder publiziert ausschließlich über den Writer auf den registrierten `transcript_path`; `--check` bleibt read-only. Überlappende Änderungen müssen in der Korrekturdatei aufgelöst werden, nicht durch eine implizite Reihenfolge.

---

## 4. Journal, Teilabbruch und Recovery

- Ein offenes Journal sperrt jede mutierende Writer-Operation, auch den Builder.
  Ergebnis ist `recovery_required` (Exit 3), nie Erfolg.
- `--inspect` ist read-only; `--recover` ist explizit, lease-gebunden und
  forward-only. Fremde Bytes, fehlende Ziele, korrupte oder mehrere Journale
  stoppen ohne Mutation, ebenso ein nicht passender Workspace-Fingerprint.
- Wiederaufnahme verlangt unveränderte Journal-/Stage-Hashes und pro Datei
  zulässigen Vor- oder Nachzustand. Raw-Evidenz wird nie verändert; Recovery ist
  keine Bestandsmigration.
- Keine Multi-File-ACID-/Power-Loss-Garantie, keine Race-Freiheit gegen einen
  bösartigen gleichzeitigen Dateisystemakteur, keine globale Snapshot-Isolation
  für nicht journalbewusste Leser.
