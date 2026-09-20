# Office Intelligence — Paket System Map: Verben (Processes)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Verben  
> **Ziel**: Beschreibung aller desk-übergreifenden Abläufe, Handoff-Punkte und Datentransformationen im Zusammenspiel der 7 Sub-Skills.  
> **Gültig für**: Repository Root & konsumierende Workspaces (relativ)

---

## 1. Übersicht der übergreifenden Workflows

Die Sub-Skills arbeiten nach dem Prinzip modularer Stufenübergänge:

```
[E-Mail / Himalaya] ──► (mail-desk) ──────┬──► [Quarantäne / data/]
                                         ├──► [Dossier / memory/evidence/mail/]
                                         └──► (task-desk) ──► [Action Items]
                                                  │
[Meeting / Adapter] ──► (meeting-desk) ───────────┤
[Event / Adapter]   ──► (event-documentation) ───┘
                                                  │
                                                  ▼
                                      (project-catalog-entry)
                                      (topic-catalog-entry)
                                                  │
[Cloud Storage]     ──► (cloud-atlas) ────────────┴──► [memory/cloud/ & references/]
```

---

## 2. Handoff-Workflow: Mail-Desk → Task-Desk

Die Bearbeitung von E-Mails und die Erfassung von Aufgaben sind strikt getrennt:

1. **Intake & Triage (`mail-desk`):**
   - E-Mail wird eingelesen, klassifiziert und fachlich bewertet.
   - Falls Dateianhänge existieren: Durchlaufen der Quarantäne- und Dispositions-Pipeline (MD-Q1/MD-Q2/MD-Q3: Extraktion, Indizierung, Entscheidung/Promotion-Link/Discard).
   - `mail-desk` erstellt eine Fallakte (Dossier) und markiert identifizierte Handlungsbedarfe.
2. **Handoff an `task-desk`:**
   - Der Agent übergibt die identifizierten Punkte an `task-desk`.
   - `task-desk` prüft auf Duplikate, priorisiert nach Dringlichkeit/Impact und bereitet die Synchronisation mit dem Task-Tracker (z. B. Todoist) vor.
   - `mail-desk` selbst legt **keine** Todos im Aufgaben-Tracker an.

---

## 3. Meeting- & Event-Intake-Workflow

1. **Intake (`meeting-desk` bzw. `event-documentation`):**
   - Rohdaten (Transkripte von Fireflies, Zoom, Audio-Uploads) werden aufgenommen.
   - Trennung nach Granularität:
     - Einzel-Meeting, Call oder Vortrag → [`meeting-desk`](../../skills/meeting-desk/SKILL.md)
     - Mehrtägige Tagung, Konferenz mit Vortragsprogramm → [`event-documentation`](../../skills/event-documentation/SKILL.md)
2. **Evidenz-Erzeugung:**
   - Bereinigtes Protokoll, Kernaussagen und Teilnehmer werden als Markdown in `memory/evidence/meetings/` bzw. `memory/evidence/events/` abgelegt.
3. **Katalog-Verknüpfung:**
   - Zuordnung des Meetings zu einem Projekt (`projects.json`) oder Thema (`topics.json`).
   - Action Items werden wie bei Mail an den `task-desk` übergeben.

---

## 4. Cloud-Atlas Workspace-Integration

1. **Filemap-Generierung:**
   - `cloud-atlas` erfasst Cloud-Verzeichnisse rekursiv und erzeugt `filemap.json`.
2. **Kuratierung & Konvertierung:**
   - Binärformate (Word, Excel, PowerPoint) werden via Pandoc / LibreOffice nach Markdown übersetzt.
   - Bildbasierte PDFs durchlaufen OCR (Default: `local_derivative`).
3. **Spiegelung in den Workspace:**
   - Ablage unter `memory/cloud/projects/<project-id>/` bzw. `memory/cloud/topics/<topic-id>/`.
   - Das Projekt-Desk verlinkt diese lokalen Spiegel in den Projekt-Notizen (`memory/references/projects/<id>/`).

---

## 5. Katalog-Mutations-Lifecycle

Für Änderungen an den zentralen Registern (`projects.json`, `topics.json`) gilt ein strikter 4-Schritte-Zyklus:

```
[1. Lock anfordern] ──► [2. Schema-Validierung] ──► [3. Atomarer Replace] ──► [4. Lock freigeben]
  (workspace-lock)        (validate_projects.py)      (tempfile + os.replace)    (workspace-lock)
```

1. **Lock anfordern:** Exklusive Lease über `workspace-lock` sichern.
2. **Vorab-Validierung:** Skript prüft semantische Gültigkeit, Pflichtfelder und ID-Eindeutigkeit im Speicher.
3. **Atomarer Write:** Schreiben in temporäre Datei auf demselben Dateisystem, gefolgt von `os.replace`. Bei Abbruch bleibt der alte Stand unbeschädigt.
4. **Lock freigeben:** Freigabe der Lease für nachfolgende Harnesses.

---

## 6. Sub-Skill-spezifische Lifecycles (Verweise)

Die detaillierten Abläufe der beiden komplexen Subsysteme sind in den jeweiligen L2-Prozesskarten dokumentiert:

* **Mail-Desk Deep Dive:** [`../../skills/mail-desk/docs/system-map/processes.md`](../../skills/mail-desk/docs/system-map/processes.md)  
  *(Himalaya-Client-Flow, Mailbox-Preflight, Quarantäne-Engine MD-Q1/MD-Q2 inkl. bounded read-only Tracked-Quarantäne-Preflight `quarantine_preflight.py` FR-15/MD-E1-T02, Disposition, Verifiable Receipts & Discard Recovery Journal MD-Q3, policygebundene Anhang-Evaluierung `attachment_evaluate` mit linearer Fetch/Extraktions/Handoff-Komposition (`completed`/`handoff_ready` bzw. `still_ambiguous`) FR-15/MD-E1-T05, fail-closed Fehler-/Reason-Matrix FR-15/MD-E1-T06, MD-E1-Paketabnahme mit hermetischem End-to-End-Nachweis und Zero-Write-Garantie FR-15/MD-E1-T07, standardmäßig aktive `draft`-Integration mit einmaliger `untrusted_external`-Neuklassifikation und additivem `attachment_evaluation` FR-15/MD-E2-T01, Read-only Reconcile, Dossier-Synthese)*
* **Cloud-Atlas Deep Dive:** [`../../skills/cloud-atlas/docs/system-map/processes.md`](../../skills/cloud-atlas/docs/system-map/processes.md)  
  *(Filemap-Scan, Dokumentenkonvertierung, OCR-Verzweigung `local_derivative` vs. `enrich_source`, Mirror-Sync)*
