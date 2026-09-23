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
│ (141 Dateien)│       │ (28 Dateien) │       │ - project-catalog-entry  │
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
| [`mail-desk`](../../skills/mail-desk/SKILL.md) | **Schwergewicht** (L2 System Map) | Kurzstatus: [§2.1](#21-mail-desk--verantwortlichkeits-detail-status-je-fr-paket) · aktuelle Metrik: [§3](#3-navigationsmatrix-der-paket-map) | [`../../skills/mail-desk/docs/system-map/README.md`](../../skills/mail-desk/docs/system-map/README.md) | Mail-Ingest, Klassifikation, Quarantäne, Receipts, Draft-/Evaluierungs-Pipeline, Classifier-Routing, Himalaya-Adapter, Dossier-Synthese — Detailstatus je FR-Paket: [§2.1](#21-mail-desk--verantwortlichkeits-detail-status-je-fr-paket) |
| [`cloud-atlas`](../../skills/cloud-atlas/SKILL.md) | **Schwergewicht** (L2 System Map) | 28 Dateien<br>138 Tests | [`../../skills/cloud-atlas/docs/system-map/README.md`](../../skills/cloud-atlas/docs/system-map/README.md) | Filemap-Generierung (`gen_filemap.py`), Dokumentkonvertierung & OCR (`convert_cloud_docs.py`), Cloud-Sync. |
| [`project-catalog-entry`](../../skills/project-catalog-entry/SKILL.md) | Kompakt (Paket-Map) | 24 Dateien | [`objects.md#project-catalog-entry`](objects.md#31-projektkatalog-project-catalog-entry) | Validierung und Migration von `memory/references/projects/projects.json` und Workpackages. |
| [`topic-catalog-entry`](../../skills/topic-catalog-entry/SKILL.md) | Schlank (Paket-Map) | 3 Dateien | [`objects.md#topic-catalog-entry`](objects.md#32-themenkatalog-topic-catalog-entry) | Pflege von `memory/references/topics/topics.json` und Subtopic-Strukturen. |
| [`task-desk`](../../skills/task-desk/SKILL.md) | Schlank (Paket-Map) | 2 Dateien | [`processes.md#task-desk`](processes.md#2-handoff-workflow-mail-desk--task-desk) | Action-Item-Extraktion, Priorisierung und Todoist-Vorbereitung. |
| [`meeting-desk`](../../skills/meeting-desk/SKILL.md) | Schlank (Paket-Map) | 3 Dateien | [`processes.md#meeting-desk`](processes.md#3-meeting--event-intake-workflow) | Evidenz-Überführung einzelner Meetings (Fireflies, Zoom). |
| [`event-documentation`](../../skills/event-documentation/SKILL.md) | Schlank (Paket-Map) | 9 Dateien | [`processes.md#meeting-desk`](processes.md#3-meeting--event-intake-workflow) | Umfassende Dokumentation von Konferenzen und Symposien. |


#### 2.1 Mail-Desk – Verantwortlichkeits-Detail (Status je FR-Paket)

Kanonische Langfassung der mail-desk-Zelle der Komplexitätsmatrix (§2). Die Zelle
in §2 nennt nur Kurzstatus + Anker; Änderungen an Verantwortlichkeiten erfolgen
ausschließlich hier (Null-Informationsverlust, zitierfähige Anker).

- **Mail-Ingest, Klassifikation, versionierter Quarantäneindex** (MD-Q1/MD-Q2/MD-C1
  Schema 1 mit additiven Coverage-Feldern), **Coverage-Vertrag** (MD-C1),
  **Disposition, Verifiable Receipts & Discard-Recovery-Journal** (MD-Q3).
- **Kontextgebundene Receipt-Klassen-Grenze** mit interner Maschinen-Autorisierung
  (FR-15/MD-E1-T03).
- **Policygebundene Anhang-Evaluierung** mit linearer Fetch/Extraktions/Handoff-
  Komposition (FR-15/MD-E1-T05) und **fail-closed Fehler-/Reason-Matrix**
  (FR-15/MD-E1-T06).
- **MD-E1-Paketabnahme** mit hermetischem End-to-End-Nachweis
  Inspect → Fetch → Extract → Handoff und Zero-Write-Garantie (FR-15/MD-E1-T07).
- **Standardmäßig aktive `draft`-Integration** mit fail-closed-Härtung, kanonischer
  Handoff-Revalidierung und deterministischer `already_fetched`-Idempotenz
  (FR-15/MD-E2-T01/T02).
- **Opt-in `inspect`-`manifest_proposal`** (FR-15/MD-E2-T03).
- **MD-E2-Paketabnahme** mit hermetischem Real-Pfad-Nachweis bis zum persistierten
  Projekt-`DraftManifest` (FR-15/MD-E2-T04).
- **Classifier-Entflechtung** mit kanonischen Matching-Ownern und kontrahierter
  Facade (FR-13/MD-M1-T01–T04).
- **Quarantäne-Paketierung** unter `core/quarantine/` mit identitätserhaltenden
  Legacy-Shims (FR-13/MD-M2, FR-13 geschlossen).
- **Katalogtreues Routing** mit `routing_priority`, Exaktcode-vor-Kontakt und
  `do_not_route_if` für Projekte und Topics inkl. Newsletter-Mapping (FR-17/MD-R1).
- **DNR-gegatete Sibling-Kohärenz** (FR-17/MD-R2).
- **Begrenzte Client-Deadlines** für mehrordrige Himalaya-Operationen (FR-17/MD-R6).
- **Teilmengenfähiger Verify-Scope** mit Runner-Provenienz und kanonischem
  Evidence-Fallback (FR-17/MD-R7).
- **Deterministische MIME-Inventarkette** mit Feldkonsistenz-Gate (FR-17/MD-R3).
- **Inline-Bild-Policy** für die automatische Auswertung (FR-17/MD-R4).
- **Vertragsdokumentation und Laufzeit-Hygiene** (FR-17/MD-R5).
- **Reply-Heuristik:** Abschluss-/Dankesmails ohne konkrete Anforderung werden
  auf `needs_reply: false` herabgestuft, in beiden Klassifikationspässen; zitierte
  Thread-Historie zählt dabei nicht als Anforderung; kanonischer Owner
  `core/matching/reply_heuristics.py` (FR-17/MD-R8).
- Himalaya-Adapter, Dossier-Synthese.

---

## 3. Navigationsmatrix der Paket-Map

| Dimension | Dokument | Inhalt auf Paket-Ebene |
| :--- | :--- | :--- |
| **Nomen** (Struktur & Zustand) | [`objects.md`](objects.md) | Konsumierende Datenzonen, globale Kataloge (`projects.json`, `topics.json`), Lock-Leases, CLI-Envelopes. |
| **Verben** (Ablauf & Transformation) | [`processes.md`](processes.md) | Desk-übergreifende Workflows, Handoffs (Mail → Task, Meeting → Katalog, Cloud → Mirror). |
| **Seiteneffekte** (Umwelt & Grenzen) | [`effects.md`](effects.md) | Bundle-weite Invarianten, Lock-Zwang, Data-Zone-Containment, Zero-Mutation im Bundle. |

### 3.1 Metrik-SSOT: Kanonische Stellen und Änderungscheckliste (DOC-M1, FR-16)

Die Git-Index-Metrik (getrackte Dateien je Sub-Skill, `scripts/`- bzw. `core`-Zerlegung,
Testmodule, Testanzahl) wird **nicht mehr an mehreren Stellen als Literal gepflegt**:

- **SSOT je Ebene:** Die L2-Subsystem-Map (`skills/<desk>/docs/system-map/README.md`,
  Kopfzeile) ist die kanonische Stelle für die Sub-Skill-Metrik. Die L1-Komplexitätsmatrix
  (§2) referenziert die L2-Karte oder nennt keine Zerlegung; historische Snapshots in den
  FR-Ledgern (`FEATURE-REQUESTS.md`, `FEATURE-REQUEST-PROGRESS.md`) sind **bewusst
  eingefrorene Abnahmewerte** und werden nicht fortgeschrieben (sie tragen den
  Zeitstempel ihrer Paketabnahme).
- **Frische-Erhebungsregel:** Vor jedem Dokumentations-Commit, der eine Metrik nennt,
  wird der Wert frisch per `git ls-files` bzw. Testlauf erhoben — nie aus einer anderen
  Dokumentstelle kopiert.

**Änderungscheckliste (alle Stellen je Metrik-Änderung, deterministisch abzuarbeiten):**

1. L2-Subsystem-Map, Kopfzeile (z. B. `skills/mail-desk/docs/system-map/README.md` §Kopf).
2. L2-Subsystem-Map, ASCII-Diagramm in §1 (Datei-/Mode-Zerlegung), falls die Zerlegung
   sich ändert.
3. L1 `docs/system-map/README.md` §2-Komplexitätsmatrix: nur die Zelle „Dateien / Tests"
   des betroffenen Sub-Skills (Zerlegung nur via Anker auf die L2-Karte).
4. L1 §1-Systemübersicht-Diagramm (Sub-Skill-Datei-Anzahl in Box-Labels).
5. `SKILL.md`-Stellen mit Feld-/Schemazählern (z. B. Pflichtfelder des Quarantäneindex).
6. FR-Ledger **nur für den neuen Paketabschluss** als neuer, mit Datum eingefrorener
   Snapshot; keine Altzeilen umbiegen.
7. Verifikation: `git grep` über die neuen Literale muss genau die Stellen der Checkliste
   treffen; historische Snapshots bleiben absichtlich unangetastet.

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

---

## 6. FR-13/MD-M1-T01–T04 — Matching-Foundation, Projekt-/Topic-Vertikale & Facade-Kontraktion (MD-M1 abgeschlossen)

Mit **FR-13/MD-M1-T01** beginnt die Classifier-Entflechtung im `mail-desk`-Subsystem:
`skills/mail-desk/scripts/core/matching/` ist ein eigenständiges Unterpaket mit den
kanonischen Ownern `date_parser.py` (`parse_date_to_year_month`) und `ambiguity.py`
(Ranking/Unique-Choice, Cross-Kind-Conflict, Fallback). Mit **FR-13/MD-M1-T02** kommt der
kanonische Owner `matching/project_matching.py` hinzu: er besitzt die Artefakt-Primitive
(`_artifact_text_matches`, `_artifact_code_matches`, `_artifact_candidate`),
`_select_project_artifacts`, `_catalog_artifact_title`, `_project_context_label`, den
geteilten `_evidence_read_escalation`, `_build_project_evidence` und den extrahierten
Root-Match-Owner `select_project_match`, der die zuvor inline in `classify_email` liegende
Projektkatalog-Schleife unverändert kapselt (Katalogreihenfolge, First-Match,
High/Medium-Konfidenz, Ergebnisform). Mit **FR-13/MD-M1-T03** kommt der kanonische Owner
`matching/topic_matching.py` hinzu: er besitzt die Topic-/Subtopic-/Operation-/Event-Signale,
-Auflösung, -Validierung, -Evidenz und die Safe-Target-Guards sowie die beiden extrahierten
Owner-Callables `select_topic_match` (geordnete Root-Topic-Katalog-Schleife plus
Unique-Subtopic-Fallback/Override; liefert das exakte Gewinner-Katalogobjekt unter `catalog`,
sodass keine zweite Katalog-Suche nötig ist) und `materialize_topic_details`
(Subtopic/Operation/Event-Anreicherung, Evidenz, Validierung, Cross-Kind-Conflict und sichere
Synthesis-Targets). `classifier.py` bleibt die kompatible Facade, re-exportiert jeden
verschobenen Callable per Objektidentität und leitet den Root-Projektentscheid über
`select_project_match`, den Root-Topic-Entscheid über `select_topic_match` und die
Topic-Detailanreicherung über `materialize_topic_details`. Keine verschobene
Matching-Implementierung bleibt als Kompatibilitätskopie in der Facade; Full-Body-Eskalation,
Thread-Vererbung, Sent-/Final-Index-Kontext, Anhangsbindung und Manifest-Drafting bleiben
dort. Verhalten, Katalogsemantik und Ergebnisform bleiben unverändert, und es entsteht kein
Importzyklus. Der `classifier_revision`-Fingerprint bindet die geordnete AST-Quelle-Menge
`classifier.py`, `matching/ambiguity.py`, `matching/date_parser.py`,
`matching/project_matching.py`, `matching/topic_matching.py` host-pfad- und
kommentarinvariant und fail-closed.
**MD-M1-T04 (abgeschlossen):** Mit **FR-13/MD-M1-T04** ist die Facade auf 810 physische
Zeilen kontrahiert (≤ 813; Baseline 2.035) und das MD-M1-Paket abgenommen. Die
verbleibende project/topic-Domänenauswertung (Full-Read-Evidenz-Rebuild und
Thread-Ordner-Inheritance) liegt jetzt bei den bestehenden Ownern
`matching/project_matching.py` (`match_thread_project_inheritance`,
`resolve_full_read_project_evidence`) und `matching/topic_matching.py`
(`match_thread_topic_inheritance`, `resolve_full_read_topic_evidence`); `classifier.py`
behält Katalog-I/O, Full-Reader-I/O, Zwei-Pass-Orchestrierung, Thread-Referenzparsing und
Final-Index-Parent-Lookup, Anhangsbindung und Manifest-Drafting und hält die Reihenfolge
Projekt-vor-Topic sowie exakte Katalogobjekte, Entscheidungs-, Evidenz-, Notiz- und
Zielsemantik. Der Kompatibilitätsvertrag
[`../../skills/mail-desk/tests/test_classifier_compatibility_contract.py`](../../skills/mail-desk/tests/test_classifier_compatibility_contract.py)
sichert die gesamte Basissymboloberfläche. Da sich die gebundenen Regel-ASTs in MD-M1
einmalig geändert haben, rotierten die vorhandenen `classifier_revision`-Werte genau
einmal (genehmigt); der Fünf-Quellen-AST-Fingerprint bleibt `classifier.py`,
`matching/ambiguity.py`, `matching/date_parser.py`, `matching/project_matching.py`,
`matching/topic_matching.py`. **Mit FR-13/MD-M2 ist die Quarantäne-Paketierung abgeschlossen
und FR-13 geschlossen:** die sechs Quarantäne-Owner (`quarantine_index.py` — umbenannt aus
`attachment_quarantine_index.py` —, `attachment_fetch.py`, `attachment_extract.py`,
`attachment_filing.py`, `attachment_policy.py`, `attachment_handoff.py`) liegen kanonisch
unter `scripts/core/quarantine/`; die alten `core/attachment_*.py`-Pfade sind dünne
`sys.modules`-aliasende Shims mit Objektidentität für Importe, Monkeypatch-Seams und
`core.__init__`-Re-Exports (804 Tests grün, `classifier_revision` unrotiert).
L2-Detail: [`../../skills/mail-desk/docs/system-map/README.md`](../../skills/mail-desk/docs/system-map/README.md) §6 und §7.
