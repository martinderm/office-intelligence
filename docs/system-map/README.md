# Office Intelligence — Paket System Map

> **Typ**: ICM Form 6 (`system-map`), Föderierte Paket-Ebene (L1)  
> **Ziel**: Kompakte, agentenlesbare Architektur- und Navigationskarte des `office-intelligence`-Skill-Bundles zur Vermeidung von Context-Bloat und Attention Drift bei bereichsübergreifenden Refactorings, Audits und Integrationen.  
> **Gültig für**: Repository Root (relativ)

---

## 1. Systemübersicht

`office-intelligence` ist ein **Shared Skill Bundle** für nachvollziehbare Office-, Wissens- und Verwaltungsarbeit nach dem Dual-Evidence-Ansatz.

```
┌────────────────────────────────────────────────────────────────────────┐
│                   Konsumierender Agent-Workspace                       │
│  (Data Zones: memory/references/, memory/evidence/, memory/cloud/, data/)│
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Workspace-Lock Guard & CLI-Aufrufe
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                 office-intelligence (Skill Bundle Root)                │
│  ├─ SKILL.md (Router für Fach-Desks)                                   │
│  ├─ docs/system-map/ (Paket System Map)                                │
│  └─ skills/ (7 modulare Sub-Skills)                                    │
└──────┬──────────────────────┬──────────────────────┬───────────────────┘
       │                      │                      │
       ▼                      ▼                      ▼
┌──────────────┐       ┌──────────────┐       ┌──────────────────────────┐
│  mail-desk   │       │ cloud-atlas  │       │ Schlanke / Katalog-Desks │
│ (257 Dateien)│       │ (65 Dateien) │       │ - project-catalog-entry  │
│ Deep Map L2  │       │ Deep Map L2  │       │ - topic-catalog-entry    │
│              │       │              │       │ - task-desk              │
│              │       │              │       │ - meeting-desk           │
│              │       │              │       │ - event-documentation    │
└──────────────┘       └──────────────┘       └──────────────────────────┘
```

### Abgrenzung & Rolle
* **Kein Agent-Workspace:** Das Bundle verfügt über keine eigene Agent-Control-Plane (kein `.agents/`, keine Session-Lifecycles, keine Dummy-Data-Zones). Es ist eine Funktionsbibliothek und Desk-Föderation für konsumierende Agent-Workspaces (vgl. [`COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md`](../../COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md)).
* **Single Responsibility pro Sub-Skill:** Jeder Sub-Skill kapselt genau eine fachliche Domäne.

---

## 2. Sub-Skill-Topographie & Komplexitätsmatrix

Das Bundle ist intern stark asymmetrisch aufgebaut. Zur Vermeidung von Context-Overload ist die System Map föderiert:

| Sub-Skill | Komplexitäts-Klasse | Dateien / Tests | Dokumentationspfad | Primäre Aufgabe |
| :--- | :--- | :--- | :--- | :--- |
| [`mail-desk`](../../skills/mail-desk/SKILL.md) | **Schwergewicht** (L2 System Map) | 111 getrackte Dateien (`git ls-files`)<br>54 getrackte Dateien unter `scripts/` (davon 43 unter `scripts/core`)<br>42 Testmodule<br>706 Tests | [`../../skills/mail-desk/docs/system-map/README.md`](../../skills/mail-desk/docs/system-map/README.md) | Mail-Ingest, Klassifikation, versionierter Quarantäneindex (MD-Q1/MD-Q2/MD-C1 Schema 1 mit additiven Coverage-Feldern), Coverage-Vertrag (MD-C1), Disposition, Verifiable Receipts & Discard-Recovery-Journal (MD-Q3), kontextgebundene Receipt-Klassen-Grenze mit interner Maschinen-Autorisierung (FR-15/MD-E1-T03), policygebundene Anhang-Evaluierung mit linearer Fetch/Extraktions/Handoff-Komposition (FR-15/MD-E1-T05) und fail-closed Fehler-/Reason-Matrix (FR-15/MD-E1-T06), MD-E1-Paketabnahme mit hermetischem End-to-End-Nachweis Inspect → Fetch → Extract → Handoff und Zero-Write-Garantie (FR-15/MD-E1-T07), standardmäßig aktive `draft`-Integration mit fail-closed-Härtung, kanonischer Handoff-Revalidierung und deterministischer `already_fetched`-Idempotenz (FR-15/MD-E2-T01/T02), opt-in `inspect`-`manifest_proposal` (FR-15/MD-E2-T03), MD-E2-Paketabnahme mit hermetischem Real-Pfad-Nachweis bis zum persistierten Projekt-`DraftManifest` (FR-15/MD-E2-T04), Himalaya-Adapter, Dossier-Synthese. |
| [`cloud-atlas`](../../skills/cloud-atlas/SKILL.md) | **Schwergewicht** (L2 System Map) | 65 Dateien<br>41 Tests | [`../../skills/cloud-atlas/docs/system-map/README.md`](../../skills/cloud-atlas/docs/system-map/README.md) | Filemap-Generierung (`gen_filemap.py`), Dokumentkonvertierung & OCR (`convert_cloud_docs.py`), Cloud-Sync. |
| [`project-catalog-entry`](../../skills/project-catalog-entry/SKILL.md) | Kompakt (Paket-Map) | 24 Dateien | [`objects.md#project-catalog-entry`](objects.md#31-projektkatalog-project-catalog-entry) | Validierung und Migration von `memory/references/projects/projects.json` und Workpackages. |
| [`topic-catalog-entry`](../../skills/topic-catalog-entry/SKILL.md) | Schlank (Paket-Map) | 3 Dateien | [`objects.md#topic-catalog-entry`](objects.md#32-themenkatalog-topic-catalog-entry) | Pflege von `memory/references/topics/topics.json` und Subtopic-Strukturen. |
| [`task-desk`](../../skills/task-desk/SKILL.md) | Schlank (Paket-Map) | 2 Dateien | [`processes.md#task-desk`](processes.md#2-handoff-workflow-mail-desk--task-desk) | Action-Item-Extraktion, Priorisierung und Todoist-Vorbereitung. |
| [`meeting-desk`](../../skills/meeting-desk/SKILL.md) | Schlank (Paket-Map) | 3 Dateien | [`processes.md#meeting-desk`](processes.md#3-meeting--event-intake-workflow) | Evidenz-Überführung einzelner Meetings (Fireflies, Zoom). |
| [`event-documentation`](../../skills/event-documentation/SKILL.md) | Schlank (Paket-Map) | 9 Dateien | [`processes.md#meeting-desk`](processes.md#3-meeting--event-intake-workflow) | Umfassende Dokumentation von Konferenzen und Symposien. |

---

## 3. Navigationsmatrix der Paket-Map

| Dimension | Dokument | Inhalt auf Paket-Ebene |
| :--- | :--- | :--- |
| **Nomen** (Struktur & Zustand) | [`objects.md`](objects.md) | Konsumierende Datenzonen, globale Kataloge (`projects.json`, `topics.json`), Lock-Leases, CLI-Envelopes. |
| **Verben** (Ablauf & Transformation) | [`processes.md`](processes.md) | Desk-übergreifende Workflows, Handoffs (Mail → Task, Meeting → Katalog, Cloud → Mirror). |
| **Seiteneffekte** (Umwelt & Grenzen) | [`effects.md`](effects.md) | Bundle-weite Invarianten, Lock-Zwang, Data-Zone-Containment, Zero-Mutation im Bundle. |

---

## 4. Die 5 fundamentalen Bundle-Invarianten

1. **Zero Runtime Mutation im Bundle-Root:** Das Repository `office-intelligence` mutiert sich zur Laufzeit niemals selbst. Skripte schreiben ausschließlich in den deklarierten Ziel-Workspace.
2. **Workspace-Lock Ownership:** Jede schreibende Operation (Ersetzen, Erstellen, Löschen) in einem Ziel-Workspace erfordert eine gültige, lease-gebundene `workspace-lock`-Autorisierung. Legacy-Bypässe (`allow_legacy=True`/`WORKSPACE_LOCK_ALLOW_LEGACY`) sind normativ verboten und im `mail-desk`-Attachment-Fetch-/Extract-/Quarantäne-Pfad **geschlossen** (FR-15/MD-E1-T01): der shared Guard läuft ausnahmslos mit `allow_legacy=False`, und weder Env-, Parameter- noch Manifest-Werte können Legacy reaktivieren; zulässig bleibt nur die vertrauenswürdige Lease-/Conversation-ID der Harness-Control-Plane. Ergänzend ist der bounded read-only Produktions-Preflight gegen getrackte Quarantäne **implementiert** (FR-15/MD-E1-T02): nach der Ownership-Prüfung prüft `quarantine_preflight.py` den Git-Index am `workspace_root`, und ein getrackter Quarantänepfad (oder ein nicht sicher lesbarer Index) stoppt fail-closed vor dem ersten Quarantäne-/Derivat-Write. Die kontextgebundene Receipt-Klassen-Grenze ist ebenfalls **implementiert** (FR-15/MD-E1-T03, `attachment_authorization.py`): die interne Maschinen-Autorisierung (`receipt_class: "machine"`, `receipt_type: "attachment_auto_evaluation"`, prozessinterne Provenienz) wird ausschließlich im `evaluation`-Kontext akzeptiert, während Filing/Promotion/Export/Disposition/Apply-Discard/direkter Fetch sie fail-closed abweisen und typenlose menschliche Receipts unverändert bleiben. Der policygebundene Evaluierungs-Orchestrator `attachment_evaluate` ist mit **FR-15/MD-E1-T04** als Skeleton begonnen, mit **FR-15/MD-E1-T05** um die positive Auswertungsstrecke erweitert und mit **FR-15/MD-E1-T06** um die fail-closed Fehler-/Reason-Matrix geschlossen (`skills/mail-desk/scripts/core/attachment_evaluation.py`): er revalidiert echte MIME-Kandidaten, prüft vorab den Workspace-Lock, komponiert die bestehenden kanonischen Seams linear (`op_attachment_fetch` → `extract_attachment_content` → `build_attachment_analysis_handoff` → `validate_attachment_handoff`) unter einer gemeinsamen `run_id` und gibt das staged `attachment_evaluation` (`used_for_classification: false`, `classifier_revision: null`) plus den validierten `attachment_analysis_handoff` zurück. Der erfolgreiche Pfad endet `completed`/`handoff_ready`/`auto_evaluated` mit befüllten sicheren `files[]`; ein validierter `blocked_on_required_attachment`-Handoff bleibt `completed`/`still_ambiguous` (niemals `supplementary`). T06 bildet exakt erwartete kanonische Ausnahmen und den terminalen Extraktionsstatus auf bounded `failed`-Envelopes ab (`lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`, `handoff_invalid`) mit `authorization: "not_applicable"`, leerem `files[]` und ohne Handoff-Geschwister; kanonisch gültige, unvollständige erforderliche Evidenz bleibt `required_for_decision`. Die `DraftManifest`-Installation bleibt MD-E2. Mit **FR-15/MD-E1-T07** ist MD-E1 als Paket abgenommen: ein hermetischer End-to-End-Test belegt Inspect → policygebundenen Fetch → begrenzte Extraktion → validierten Handoff bei null Mailbox-/Promotion-/Export-/Dispositions-Writes; Reklassifikation und `DraftManifest`-Installation bleiben **MD-E2**; MD-E2-T01, MD-E2-T02 und MD-E2-T03 (fail-closed-Härtung, kanonische Handoff-Revalidierung, AST-/Katalog-/Hash-gebundene Revision, deterministische `already_fetched`-Idempotenz und opt-in `inspect`-`manifest_proposal`) sind implementiert; mit **FR-15/MD-E2-T04** ist die Paketabnahme über den hermetischen Akzeptanztest `skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py` abgeschlossen (ein einziger realer Pfad Body/Full-Read → mehrdeutig → realer `text/plain`-Anhang → genau eine `untrusted_external`-Neuklassifikation → persistiertes Projekt-`DraftManifest` mit bounded `attachment_evaluation`, `used_for_classification: true` und 64-Hex-`classifier_revision` bei null Mailbox-/Netzwerk- und null Execute-/Promote-/Export-/Filing-/Dispositions-/Katalog-/Cloud-Seiteneffekten). **FR-15 ist geschlossen.** Details: [`effects.md`](effects.md) §2 und die L2-Karte [`skills/mail-desk/docs/system-map/effects.md`](../../skills/mail-desk/docs/system-map/effects.md) §3.
3. **Strikte Data-Zone-Konformität:** Erzeugte Artefakte dürfen nur in den 4 kanonischen Zonen des Ziel-Workspaces abgelegt werden:
   - `memory/references/`: Dauerhafte Wissens- und Katalogstrukturen
   - `memory/evidence/`: Feste Nachweise und Sitzungsprotokolle
   - `memory/cloud/`: Lokale Arbeits- und Konvertierungsspiegel
   - `data/`: Flüchtige oder maschinengenerierte Betriebsdaten (z. B. Mail-Quarantäne)
4. **Dual Evidence Prinzip:** Trennung von operativer Evidenz (Mails, Transkripte, Downloads) und konsolidierter Referenz (Kataloge, Notizen, Action Items).
5. **Structured CLI Envelopes:** Skriptausgaben erfolgen ausnahmslos als parsebares JSON über standardisierte Hüllkurven mit deterministischen Stopcodes.

---

## 5. FR-15/MD-E2-T01 + T02 + T03 + T04 Status (Mail-Desk)

**FR-15/MD-E2-T01** implementiert die standardmäßig aktive `draft`-Anhang-Auswertung, die
gegenseitig exklusiven Optionen `--evaluate-attachments`/`--no-evaluate-attachments` (nur
direktes `draft`/`inspect`; `draft` default an, `inspect` default aus) und die genau einmalige
Neuklassifikation mit additivem finalem `attachment_evaluation` je Draft-Item
([`../../skills/mail-desk/SKILL.md`](../../skills/mail-desk/SKILL.md),
[`../../skills/mail-desk/references/batch-runner.md`](../../skills/mail-desk/references/batch-runner.md)).
Nur ein unklares Item ruft `attachment_evaluate`; der validierte Handoff bleibt als
`untrusted_external` gekapselt und wird nie persistiert. **FR-15/MD-E2-T02** härtet die Grenze
fail-closed: die vollständige bounded MD-E1-Outcome-Matrix bleibt item-lokal in Review/`INBOX`,
Identitäts-/Quellen-Pairing-Fehler sind Bindungsfehler (`handoff_invalid`), der `ready`-Handoff
wird vor der Klassifikation kanonisch revalidiert, fortbestehende Mehrdeutigkeit erhält
`completed`/`still_ambiguous` mit `auto_evaluated` und sicheren `files[]`, `classifier_revision`
bindet den normalisierten AST des Classifier-Regelmoduls plus Kataloge und konsumierte Hashes,
unerwartete Backend-/Programmiervertragsfehler schlagen fail-loud über
`AttachmentReclassificationContractError` fehl, und ein deterministischer, PII-freier Run-ID je
Nachricht erreicht im zweiten Default-Lauf MD-E1 `already_fetched` ohne Doppel-Fetch.
**FR-15/MD-E2-T03** verdrahtet den opt-in `inspect`-Vorschlag
([`../../skills/mail-desk/scripts/core/modes/inspect.py`](../../skills/mail-desk/scripts/core/modes/inspect.py)):
`inspect` bleibt ohne Opt-in rein lesend; nur `evaluate_attachments: true` ergänzt einen
top-level, nicht ausführbaren `manifest_proposal`, der denselben
`draft_manifest`- + `install_draft_attachment_evaluations`-Flow wie `draft` nutzt, und eine
ausführbare Batch-Manifest-Datei entsteht weiterhin nur bei explizitem `manifest_file`.
Mit **FR-15/MD-E2-T04** ist die Paketabnahme abgeschlossen
([`../../skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py`](../../skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py)):
der hermetische Akzeptanztest beweist in einem einzigen realen Pfad Body/Full-Read →
mehrdeutig → realer `text/plain`-Anhang → genau eine `untrusted_external`-Neuklassifikation
→ persistiertes Projekt-`DraftManifest` mit bounded `attachment_evaluation`
(`used_for_classification: true`, 64-Hex-`classifier_revision`) und null
Mailbox-/Netzwerk- sowie null Execute-/Promote-/Export-/Filing-/Dispositions-/Katalog-/
Cloud-Seiteneffekten. **FR-15 ist geschlossen.** Mail-Desk-Karten: L2
[`README.md`](../../skills/mail-desk/docs/system-map/README.md) §5, `processes.md` §3.2/§3.3,
`objects.md` §6, `effects.md` §6.
