# Mail-Desk — Subsystem System Map

> **Typ**: ICM Form 6 (`system-map`), Sub-Skill-Ebene (L2)
> **Subsystem**: [`skills/mail-desk`](../SKILL.md)
> **Ziel**: Kompakte, zitierbare Architekturkarte der Mail-Desk-Engine (reproduzierbare Git-Index-Metrik via `git ls-files` (Kanonik-Ebene, Stand MD-BC-Fix): 155 getrackte Dateien; 69 getrackte Dateien unter `scripts/`, davon 56 unter `scripts/core` inkl. `scripts/core/quarantine/` (6 kanonische Quarantäne-Owner) und `matching/reply_heuristics.py`, `catalog_validator.py` sowie `mail_desk_batch_cli.py`/`catalog_inspect.py` (Harness-Ausgabe-Boundaries); 67 Testmodule; 1032 Tests) zur Vermeidung von Context-Bloat und Attention Drift bei Refactorings, Quarantäne-Erweiterungen und Bugfixes.
> **Gültig für**: `skills/mail-desk/` relativ zum Repository-Root

---

## 1. Systemübersicht

`mail-desk` ist das größte und sicherheitskritischste Subsystem im `office-intelligence`-Bundle. Es verarbeitet eingehende E-Mails agentisch, ordnet sie Projekt- und Topic-Katalogen zu, steuert Antwort- und Aufgabenbedarfe und isoliert Dateianhänge über eine deterministische Quarantäne-Engine.

```
┌────────────────────────────────────────────────────────────────────────┐
│                   Himalaya CLI / IMAP Mailbox                          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ IMAP/MIME Transport
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                           mail-desk Engine                             │
│  ├─ CLI Facades: scripts/mail_desk_*.py + catalog_validator.py         │
│  │   + mail_desk_batch_cli.py + catalog_inspect.py                     │
│  ├─ Core Domain: scripts/core/ (56 getrackte .py-Dateien)              │
│  │   ├─ himalaya.py (CLI-Adapter, UNC-Normalisierung, Fail-Fast)       │
│  │   ├─ classifier.py (Facade + matching/ project, topic, date & amb.) │
│  │   ├─ quarantine/ (6 kanonische Quarantäne-Owner + Legacy-Shims)      │
│  │   └─ sent_indexer.py (Sent-Mails & Reply-Erkennung)                 │
│  └─ Modes: scripts/core/modes/ (13 Workflow-Treiber)                   │
│      ├─ pipeline.py / execute.py / verify.py                           │
│      ├─ dossier.py / dossier_apply.py / dossier_synthesis.py           │
│      └─ reconcile.py (Read-Only Drift-Erkennung)                       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Atomare Disk-Writes (Lock-geschützt)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                Ziel-Workspace Laufzeitdaten (data/mail-desk/)          │
│  ├─ attachment-quarantine-index.json (Schema 1, additive Coverage)     │
│  ├─ attachment-disposition-log.jsonl (Append-only Audit-Trail)         │
│  ├─ attachment-discard-journal.json (Apply-/Recovery-Journal)          │
│  ├─ final-location-index.json                                          │
│  └─ attachments/<run_id>/ (.quarantine-inventory.json + Binärdateien)  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Modul-Topographie (`scripts/core/`)

| Komponente | Dateipfade | Primäre Verantwortlichkeit |
| :--- | :--- | :--- |
| **Quarantäne-, Coverage- & Recovery-Engine** | [`scripts/core/quarantine/quarantine_index.py`](../scripts/core/quarantine/quarantine_index.py)<br>[`scripts/core/quarantine/attachment_handoff.py`](../scripts/core/quarantine/attachment_handoff.py)<br>[`scripts/core/quarantine/attachment_filing.py`](../scripts/core/quarantine/attachment_filing.py)<br>[`scripts/core/attachment_disposition_log.py`](../scripts/core/attachment_disposition_log.py)<br>[`scripts/mail_desk_attachment_quarantine_index.py`](../scripts/mail_desk_attachment_quarantine_index.py)<br>[`scripts/mail_desk_attachment_disposition.py`](../scripts/mail_desk_attachment_disposition.py)<br>[`scripts/core/quarantine/attachment_fetch.py`](../scripts/core/quarantine/attachment_fetch.py)<br>[`scripts/core/quarantine/attachment_extract.py`](../scripts/core/quarantine/attachment_extract.py)<br>[`scripts/core/quarantine/attachment_policy.py`](../scripts/core/quarantine/attachment_policy.py)<br>[`scripts/core/quarantine_preflight.py`](../scripts/core/quarantine_preflight.py)<br>[`scripts/core/attachment_authorization.py`](../scripts/core/attachment_authorization.py)<br>[`scripts/core/attachment_evaluation.py`](../scripts/core/attachment_evaluation.py) | Kurzstatus: Schema-1-Quarantäneindex (17 Pflichtfelder), Coverage-Vertrag, Receipts & Recovery-Journal, MD-E1/MD-E2-Grenzen, Quarantäne-Paketierung — Langfassung: [§2.1](#21-quarantine-coverage--recovery-engine--verantwortlichkeits-detail) |


#### 2.1 Quarantäne-, Coverage- & Recovery-Engine – Verantwortlichkeits-Detail

Kanonische Langfassung der Quarantäne-Zelle der Modul-Topographie (§2). Die Zelle
in §2 nennt nur Kurzstatus + Anker; Änderungen an Verantwortlichkeiten erfolgen
ausschließlich hier (Null-Informationsverlust, zitierfähige Anker).

- **Schema 1 (17 Pflichtfelder + bis zu 6 additive optionale Coverage-Felder)**
  Quarantäneindex; Trennung von technischem Status (`analysis_status: "completed"`)
  und inhaltlicher Deckung (`analysis_completeness`, `truncation_stage`).
- **Append-only Dispositionslog, verifizierbare Receipt-Contracts**, persistiertes
  Apply-/Recovery-Journal mit monotoner Zustandsmaschine (`prepared` bis
  `completed`), Fehler-Resumability via `last_successful_state`.
- **Atomare Inventar-Mutation** via `_QuarantineInventoryLock`, SHA-256
  Disk-Verifikation, Symlink- & 0x400-Reparse-Point-Blockade, 10-Vorbedingungen
  Discard-Apply.
- **Bounded read-only Tracked-Quarantäne-Preflight** vor jedem
  Quarantäne-/Derivat-Write (FR-15/MD-E1-T02).
- **Kontextgebundene Receipt-Klassen-Grenze** mit interner Maschinen-Autorisierung
  (FR-15/MD-E1-T03).
- **Policygebundener Anhang-Evaluierungs-Orchestrator `attachment_evaluate`** mit
  staged `attachment_evaluation`, linearer Fetch/Extraktions/Handoff-Komposition
  und validiertem `attachment_analysis_handoff` (FR-15/MD-E1-T05) sowie geschlossener
  Fail-closed-Fehler-/Reason-Matrix (FR-15/MD-E1-T06).
- **MD-E1 ist mit FR-15/MD-E1-T07 als Paket abgenommen:** ein hermetischer
  End-to-End-Test belegt Inspect → policygebundenen Fetch → Extraktion → validierten
  Handoff bei null Mailbox-/Promotion-/Export-/Dispositions-/Cleanup-/Classifier-Writes;
  Promotion und Export besitzen weiterhin keinen MD-E1-Laufzeitpfad.
- **MD-E1 selbst endet vor der Reklassifikation und der `DraftManifest`-Installation;**
  mit FR-15/MD-E2-T01 sind die `draft`-Happy-Path-Verdrahtung,
  `--evaluate-attachments`/`--no-evaluate-attachments` und die einmalige
  Neuklassifikation samt additiver `DraftManifest`-Installation implementiert, und mit
  FR-15/MD-E2-T02 ist diese Grenze fail-closed gehärtet (bounded Outcome-Matrix,
  kanonische Handoff-Revalidierung, AST-/Katalog-/Hash-gebundene
  `classifier_revision`, deterministische `already_fetched`-Idempotenz) sowie der
  opt-in `inspect`-`manifest_proposal` mit wiederverwendetem Item-Flow und
  `manifest_file`-Grenze (FR-15/MD-E2-T03) und die MD-E2-Paketabnahme mit hermetischem
  Real-Pfad-Nachweis bis zum persistierten Projekt-`DraftManifest`
  (FR-15/MD-E2-T04) implementiert; **FR-15 geschlossen**.
- **Seit FR-13/MD-M2** sind die sechs Quarantäne-Owner kanonisch unter
  `scripts/core/quarantine/` paketiert; die alten `scripts/core/attachment_*.py`-Pfade
  sind dünne `sys.modules`-aliasende Shims (Objektidentität, Monkeypatch-Seams und
  `core.__init__`-Re-Exports unverändert, `classifier_revision` unrotiert).

---

## 7. FR-13/MD-M2 — Quarantäne-Paketierung (MD-M2 abgeschlossen; FR-13 geschlossen)

Mit **FR-13/MD-M2-T01** sind die sechs zusammengehörigen Quarantäne-/Anhangsmodule kanonisch
unter [`scripts/core/quarantine/`](../scripts/core/quarantine/) paketiert:
`quarantine_index.py` (umbenannt aus `attachment_quarantine_index.py`), `attachment_fetch.py`,
`attachment_extract.py`, `attachment_filing.py`, `attachment_policy.py` und
`attachment_handoff.py` sind die kanonischen Owner (Inhalt byte-erhalten bis auf zwei
Relative-Import-Korrekturen in `attachment_filing.py`: `..attachment_authorization`,
`..attachments`). Die sechs alten `scripts/core/attachment_*.py`-Pfade sind dünne Shims, die
`sys.modules[__name__]` auf das Owner-Modulobjekt aliasieren — Legacy-Importe,
`mock.patch`-Strings (`core.attachment_quarantine_index.*`), `patch.object`-Seams auf den
Legacy-Aliases und die 33 `core.__init__`-Re-Exports bleiben objektident und wirksam; der
`classifier_revision`-Fingerprint bindet weiterhin ausschließlich `classifier.py` +
`matching/*` (keine Rotation). Der Kompatibilitätsvertrag
[`tests/test_quarantine_compatibility_contract.py`](../tests/test_quarantine_compatibility_contract.py)
sichert Modulidentität, Symbolidentität, Seam-Wirksamkeit, Fingerprint-Quellenmenge und das
eingefrorene `core.__all__`;
[`tests/test_quarantine_package_structure.py`](../tests/test_quarantine_package_structure.py)
sichert Paketstruktur, Owner-Vollständigkeit und die Cloud-Atlas-Discovery vom tieferen
Verzeichnislevel. Vollständige Suite 804/804 grün. **MD-M2 ist abgenommen; FR-13 ist
geschlossen.**
| **MD-E2 Draft-Integration (FR-15/MD-E2-T01/T02)** | [`scripts/core/attachment_reclassification.py`](../scripts/core/attachment_reclassification.py) | Standardmäßig aktive `draft`-Auswertung: nur unklare Items rufen `attachment_evaluate`, der validierte `ready`-Handoff wird genau einmal als getrenntes `untrusted_external` reklassifiziert, und jedes Draft-Item erhält genau ein additives finales `attachment_evaluation`; `classifier_revision` bindet den Classifier-Regel-AST, Kataloge und konsumierte Hashes. Fail-closed-Härtung: bounded Outcome-Matrix in Review/`INBOX`, kanonische Handoff-Revalidierung, fail-loud `AttachmentReclassificationContractError`, deterministische `already_fetched`-Idempotenz. Keine zweite Fetch-/MIME-/Hash-Validierung. |
| **Mailbox-Adapter** | [`scripts/core/himalaya.py`](../scripts/core/himalaya.py)<br>[`scripts/mail_desk_himalaya_client.py`](../scripts/mail_desk_himalaya_client.py) | Subprozess-Isolation, Windows-UNC-Drive-Workaround, Preflight, Fail-Fast ohne interaktiven Wizard. |
| **Klassifikation & Triage** | [`scripts/core/classifier.py`](../scripts/core/classifier.py)<br>[`scripts/core/matching/date_parser.py`](../scripts/core/matching/date_parser.py)<br>[`scripts/core/matching/ambiguity.py`](../scripts/core/matching/ambiguity.py)<br>[`scripts/core/matching/project_matching.py`](../scripts/core/matching/project_matching.py)<br>[`scripts/core/matching/topic_matching.py`](../scripts/core/matching/topic_matching.py)<br>[`scripts/core/quarantine/attachment_policy.py`](../scripts/core/quarantine/attachment_policy.py) | `classifier.py` bleibt die kompatible Facade für Katalog-Matching und Signalanalyse; die kanonischen Owner `matching/date_parser.py` (Datums-Parsing), `matching/ambiguity.py` (Ranking/Unique-Choice, Cross-Kind-Conflict, Fallback), `matching/project_matching.py` (Artefakt-Primitive, Projekt-/Workpackage-/Task-/Deliverable-/Milestone-Auflösung, Projekt-Evidenz und der Root-Match-Owner `select_project_match`) und `matching/topic_matching.py` (Topic-/Subtopic-/Operation-/Event-Auflösung, -Validierung, -Evidenz, Safe-Targets sowie die Owner `select_topic_match` und `materialize_topic_details`) sind per Objektidentität an die Facade gebunden (FR-13/MD-M1-T01–T04). Seit **MD-M1-T04** liegen zusätzlich die Thread-Ordner-Inheritance (`match_thread_project_inheritance`, `match_thread_topic_inheritance`) und der Full-Read-Evidenz-Rebuild (`resolve_full_read_project_evidence`, `resolve_full_read_topic_evidence`) bei diesen Ownern; `classifier.py` behält Referenzparsing, Final-Index-Parent-Lookup, Full-Reader-I/O und Zwei-Pass-Orchestrierung. |
| **Dossier & Synthese** | [`scripts/core/modes/dossier*.py`](../scripts/core/modes/)<br>[`scripts/core/synthesis_handoff.py`](../scripts/core/synthesis_handoff.py) | Strukturierte Fallakten, Übergabe von Action Candidates an den Task-Desk. |
| **Batch & Orchestrierung** | [`scripts/core/modes/pipeline.py`](../scripts/core/modes/pipeline.py)<br>[`scripts/core/modes/execute.py`](../scripts/core/modes/execute.py)<br>[`scripts/core/modes/verify.py`](../scripts/core/modes/verify.py) | Hash-gebundene Review-Receipts, Ausführung, Verifikation. |
| **Integrität & Reconcile** | [`scripts/core/modes/reconcile.py`](../scripts/core/modes/reconcile.py)<br>[`scripts/core/readiness.py`](../scripts/core/readiness.py) | Read-only Drift-Erkennung zwischen Disk, Inventar und Quarantäne-Index. |

---

## 3. Navigationsmatrix

| Dimension | Dokument | Inhalt |
| :--- | :--- | :--- |
| **Nomen** (Struktur & Zustand) | [`objects.md`](objects.md) | `attachment-quarantine-index.json` (Schema 1 mit optionalen Coverage-Feldern), `attachment-disposition-log.jsonl`, `attachment-discard-journal.json`, `.quarantine-inventory.json`, `final-location-index.json`, Dossiers, DTOs. |
| **Verben** (Ablauf & Transformation) | [`processes.md`](processes.md) | Batch-Lifecycles (`draft` → `execute` → `verify`), Quarantäne-, Coverage- & Dispositions-Ablauf, Recovery-Workflow, Reconcile, Himalaya-Intake. |
| **Seiteneffekte** (Umwelt & Grenzen) | [`effects.md`](effects.md) | Windows UNC-Drive Normalisierung, 0x400 Reparse-Point Bann, Tempfile-Replace, Secret-Filter, Recovery-Scope-Drift, Coverage-Fail-Closed-Integrität. |

---

## 4. Die 5 unverhandelbaren Mail-Desk-Invarianten

1. **Untrusted Content:** E-Mail-Inhalte (Body, Header, Anhänge) sind unvertrauenswürdige Daten. Sie dürfen niemals direkt als Instruktionen ausgeführt werden.
2. **Review-Receipt-Bindung:** Ein Batch-Lauf (`execute`) sowie Dispositions- und Löschoperationen (`apply-discard`) erfordern verbindlich hash-gebundene Review- und Freigabe-Receipts. Kein unautorisiertes Mutieren von Mailbox, Dateisystem oder Index. Ergänzend erzwingt die kontextgebundene Receipt-Klassen-Grenze (FR-15/MD-E1-T03), dass die interne Maschinen-Autorisierung (`receipt_class: "machine"`, `receipt_type: "attachment_auto_evaluation"`) ausschließlich den begrenzten MD-E1-Fetch/Evaluate-Flow autorisiert und von Filing, Promotion, Export, Disposition, Apply-Discard und direkten Fetch-Pfaden fail-closed abgewiesen wird; die bestehenden typenlosen menschlichen Receipts bleiben unverändert gültig. Der policygebundene Anhang-Evaluierungs-Orchestrator (`attachment_evaluate`) revalidiert echte MIME-Kandidaten, erzeugt die interne Maschinen-Autorisierung intern, prüft vorab den Workspace-Lock und komponiert mit **FR-15/MD-E1-T05** die bestehenden kanonischen Seams linear (`op_attachment_fetch` → `extract_attachment_content` → `build_attachment_analysis_handoff` → `validate_attachment_handoff`) unter einer `run_id`; er gibt das staged `attachment_evaluation` (`used_for_classification: false`, `classifier_revision: null`) plus den validierten `attachment_analysis_handoff` zurück. Der Erfolgspfad ist `completed`/`handoff_ready`, ein validierter blockierter Handoff `completed`/`still_ambiguous` (niemals `supplementary`); mit **FR-15/MD-E1-T06** ist die negative Fehler-/Reason-Matrix fail-closed geschlossen (bounded `failed`-Envelopes mit `lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`, `handoff_invalid`, ohne Handoff-Geschwister und ohne Exception-Text/Rohinhalt/absoluten Pfad), während die `DraftManifest`-Installation MD-E2 bleibt (Details: [`effects.md`](effects.md) §3).
3. **Workspace-Lock Ownership:** Schreibende Skripte erfordern eine verifizierte Lease via `require_workspace_lock()`. Legacy-Bypässe (`allow_legacy=True`/`WORKSPACE_LOCK_ALLOW_LEGACY`) sind normativ strikt verboten und im Attachment-Fetch-/Extract-/Quarantäne-Pfad **geschlossen** (FR-15/MD-E1-T01): der shared Guard wird ausnahmslos mit `allow_legacy=False` aufgerufen, und weder Env-, Parameter- noch Manifest-Werte können Legacy reaktivieren. Nur die vertrauenswürdige Lease-/Conversation-ID der Harness-Control-Plane bleibt zulässig. Ergänzend prüft der bounded read-only Tracked-Quarantäne-Preflight (FR-15/MD-E1-T02) nach der Ownership-Prüfung den Git-Index am `workspace_root`; ein getrackter Quarantänepfad stoppt fail-closed vor dem ersten Quarantäne- bzw. Derivat-Write (Details: [`effects.md`](effects.md) §3).
4. **Schema 1 Quarantäne-, Dispositions- & Journal-Integrität:** Der Quarantäne-Index erzwingt in Schema 1 exakt 17 kanonische Basisfelder und erlaubt bis zu 6 optionale additive Coverage-Felder (`analysis_completeness`, `truncation_reason`, `truncation_stage`, `handoff_character_count`, `analysis_character_budget`, `source_character_count`), strikt positive `size_bytes > 0` und deterministische 64-Hex `attachment_id`s. `analysis_status: "completed"` bezeichnet ausschließlich den technischen Abschluss, niemals die inhaltliche Vollständigkeit. Bei Altdaten ohne Coverage-Felder liefert `lookup_quarantine_entry()` in-memory `analysis_completeness: "unknown"`, während die Datei auf Disk unverändert und byte-identisch bleibt. Das Dispositionslog erzwingt deterministische `decision_id`s und Hash-Bindung an den Index. Das Discard-Journal erzwingt monotone Zustandsübergänge (`prepared` bis `completed`) und exakte Übereinstimmung von `history[-1]` mit Top-Level-Status. Unbekannte oder widersprüchliche Felder führen zum sofortigen Abbruch (`AttachmentIndexSchemaError`, `DispositionSchemaError`, `RecoveryJournalCorruptedError`).
5. **Fail-Closed Drift-Abbruch:** Jede Diskrepanz zwischen physischer Datei, Inventar-Hash, Index, Dispositionslog und Recovery-Journal stoppt sofort als Drift-Fehler.

---

## 5. FR-15/MD-E2-T01 + T02 + T03 + T04 — Draft-Integration, Neuklassifikation und opt-in inspect-Vorschlag

Mit **FR-15/MD-E2-T01** ist die standardmäßig aktive `draft`-Auswertung implementiert
([`scripts/core/attachment_reclassification.py`](../scripts/core/attachment_reclassification.py),
Orchestrierung; [`scripts/core/modes/draft.py`](../scripts/core/modes/draft.py), Aufruf nach der
bestehenden zweistufigen Klassifikation und vor `add_draft_contract`). Der Seam hält die
MD-E1-/FR-15-Grenzen unverändert: keine zweite Fetch-/MIME-/Policy-/Hash-Validierung, keine
Mailbox-, Promotion-, Export-, Filing-, Dispositions-, Katalog-, Cloud- oder Evidence-Mutation.
Mit **FR-15/MD-E2-T02** ist die Grenze fail-closed gehärtet: die vollständige bounded
MD-E1-Outcome-Matrix bleibt item-lokal in Review/`INBOX`, Identitäts-/Quellen-Pairing-Fehler
sind Bindungsfehler (`handoff_invalid`), ein `ready`-Handoff wird vor der Klassifikation
kanonisch revalidiert, fortbestehende Mehrdeutigkeit erhält `completed`/`still_ambiguous` mit
`auto_evaluated` und sicheren `files[]`, `classifier_revision` bindet den normalisierten AST
des Classifier-Regelmoduls plus Kataloge und konsumierte Hashes, unerwartete Backend-/
Programmiervertragsfehler schlagen über `AttachmentReclassificationContractError` fail-loud
fehl, und ein deterministischer, PII-freier Run-ID je Nachricht erreicht im zweiten
Default-Lauf MD-E1 `already_fetched` ohne Doppel-Fetch. Mit **FR-15/MD-E2-T03** ist der
opt-in `inspect`-Vorschlag verdrahtet
([`scripts/core/modes/inspect.py`](../scripts/core/modes/inspect.py)): `inspect` bleibt
ohne `evaluate_attachments`/`propose_manifest` rein lesend; nur `evaluate_attachments: true`
ergänzt einen top-level, **nicht ausführbaren** `manifest_proposal` über denselben
`draft_manifest`- + `install_draft_attachment_evaluations`-Flow wie `draft`, und eine
ausführbare Batch-Manifest-Datei entsteht weiterhin nur bei explizitem `manifest_file`.
Ein gemischter Batch (klar/geklärt/weiterhin mehrdeutig/bounded failed) bewahrt die
Reihenfolge und bleibt item-lokal. Mit **FR-15/MD-E2-T04** ist die Paketabnahme über
[`tests/test_batch_runner_mde2_acceptance.py`](../tests/test_batch_runner_mde2_acceptance.py)
abgeschlossen: der hermetische Akzeptanztest beweist in einem einzigen realen Pfad
Body/Full-Read → mehrdeutig → realer `text/plain`-Anhang → genau eine
`untrusted_external`-Neuklassifikation → persistiertes Projekt-`DraftManifest` mit bounded
`attachment_evaluation` (`used_for_classification: true`, 64-Hex-`classifier_revision`
gebunden an Classifier-Regeln plus konsumierten Anhangs-Hash) bei null Mailbox-/Netzwerk-
und null Execute-/Promote-/Export-/Filing-/Dispositions-/Katalog-/Cloud-Seiteneffekten.
**FR-15 ist geschlossen.** Details:
[`processes.md`](processes.md) §3.2, [`objects.md`](objects.md) §6, [`effects.md`](effects.md) §6.

---

## 6. FR-13/MD-M1-T01–T04 — Matching-Foundation, Projekt-/Topic-Vertikale & Facade-Kontraktion (MD-M1 abgeschlossen)

Mit **FR-13/MD-M1-T01** ist die erste Stufe der Classifier-Entflechtung implementiert.
[`scripts/core/matching/`](../scripts/core/matching/) ist ein eigenständiges Unterpaket mit
[`matching/date_parser.py`](../scripts/core/matching/date_parser.py) als kanonischem Owner von
`parse_date_to_year_month` und
[`matching/ambiguity.py`](../scripts/core/matching/ambiguity.py) als kanonischem Owner der
wiederverwendbaren Policy (`resolve_scored_candidates`, `select_unique_fallback`,
`cross_kind_conflict`, `mark_cross_kind_conflict`). Mit **FR-13/MD-M1-T02** kommt
[`matching/project_matching.py`](../scripts/core/matching/project_matching.py) als kanonischer
Owner der vollständigen Projekt-Vertikale hinzu: die Artefakt-Primitive
(`_artifact_text_matches`, `_artifact_code_matches`, `_artifact_candidate`),
`_select_project_artifacts`, `_catalog_artifact_title`, `_project_context_label`, der
geteilte `_evidence_read_escalation`, `_build_project_evidence` und der extrahierte
Root-Match-Owner `select_project_match`. Dieser kapselt die zuvor inline in `classify_email`
liegende Projektkatalog-Schleife unverändert (Katalogreihenfolge, First-Match,
High/Medium-Konfidenz, Ergebnisform).

Mit **FR-13/MD-M1-T03** kommt
[`matching/topic_matching.py`](../scripts/core/matching/topic_matching.py) als kanonischer
Owner der vollständigen Topic-Vertikale hinzu: die Topic-/Subtopic-Signale
(`_topic_parent_subject_signal`, `_subject_signal_matches`), die Subtopic-/Operation-/Event-
Auflösung samt Kontext-Labels (`_select_topic_subtopic`, `_topic_context_label`,
`_select_subtopic_operation`, `_operation_context_label`, `_select_subtopic_event`,
`_event_context_label`), die Event-Validierung (`_event_validation_reasons`), die neutralen
Evidenz-Builder (`_build_topic_evidence`, `_build_operation_evidence`, `_build_event_evidence`)
und die Safe-Target-Guards (`_safe_subtopic_reference_target`,
`_safe_operation_reference_target`, `_safe_event_dossier_target`,
`_has_canonical_operation_reference`). Zusätzlich extrahiert der Owner zwei konkrete
Callables: `select_topic_match` kapselt die zuvor inline in `classify_email` liegende
geordnete Root-Topic-Katalog-Schleife plus Unique-Subtopic-Fallback/Override und liefert das
exakte Gewinner-Katalogobjekt unter `catalog`, sodass die Detailstufe ohne zweiten
Katalog-Lookup auskommt; `materialize_topic_details` besitzt Subtopic/Operation/Event-
Anreicherung, Evidenz, Event-Validierung, Cross-Kind-Conflict und Safe-Synthesis-Targets.

[`scripts/core/classifier.py`](../scripts/core/classifier.py) bleibt die kompatible Facade,
re-exportiert jeden verschobenen Callable per Objektidentität und leitet den Root-Topic-
Entscheid über `select_topic_match` sowie die Detailanreicherung über
`materialize_topic_details`. Keine Topic-Implementierung bleibt als Kompatibilitätskopie in
der Facade; Full-Body-Eskalation, Thread-Vererbung, Sent-/Final-Index-Kontext,
Anhangsbindung und Manifest-Drafting bleiben in der Facade. Die Matching-Module importieren
`classifier.py` nie (kein Zyklus, keine Importzeit-Nebenwirkung); Verhalten, Katalogsemantik
und Ergebnisform bleiben unverändert.

Der `classifier_revision`-Fingerprint bindet den normalisierten AST der geordneten
Fünf-Modul-Quelle-Menge `classifier.py`, `matching/ambiguity.py`, `matching/date_parser.py`,
`matching/project_matching.py`, `matching/topic_matching.py` (host-pfad-invariant,
Kommentar-/Formatierungs-invariant, fail-closed ohne Partial-Digest).

**MD-M1-T04 (abgeschlossen):** Mit **FR-13/MD-M1-T04** ist die Facade auf 810 physische
Zeilen kontrahiert (≤ 813; Baseline 2.035) und das MD-M1-Paket abgenommen. Die
verbleibende project/topic-Domänenauswertung (Full-Read-Evidenz-Rebuild und
Thread-Ordner-Inheritance) liegt jetzt bei den bestehenden Ownern
`matching/project_matching.py` (`match_thread_project_inheritance`,
`resolve_full_read_project_evidence`) und `matching/topic_matching.py`
(`match_thread_topic_inheritance`, `resolve_full_read_topic_evidence`); `classifier.py`
behält Katalog-I/O, Full-Reader-I/O, Zwei-Pass-Orchestrierung, Thread-Referenzparsing und
Final-Index-Parent-Lookup, Anhangsbindung und Manifest-Drafting und wahrt die Reihenfolge
Projekt-vor-Topic sowie exakte Katalogobjekte, Entscheidungs-, Evidenz-, Notiz- und
Zielsemantik. Der Kompatibilitätsvertrag
[`tests/test_classifier_compatibility_contract.py`](../tests/test_classifier_compatibility_contract.py)
sichert die gesamte Basissymboloberfläche sowie die neue Owner-Identität/-Routing. Da sich
die gebundenen Regel-ASTs in MD-M1 einmalig geändert haben, rotierten die vorhandenen
`classifier_revision`-Werte genau einmal (genehmigt); der Fünf-Quellen-Fingerprint bleibt
unverändert. **FR-13 ist damit teilweise umgesetzt (MD-M1 abgeschlossen); MD-M2 und die
Quarantäne-Paketierung bleiben offen.**

---

## 8. FR-17/MD-R1 — Katalogtreues Routing: Priorität, Exaktcode, Ausschlüsse und Newsletter-Mapping (MD-R1 abgeschlossen)

Mit **FR-17/MD-R1** ist die Routing-Ordnung katalogtreu und deterministisch
([matching/project_matching.py](../scripts/core/matching/project_matching.py),
[matching/topic_matching.py](../scripts/core/matching/topic_matching.py)):
select_project_match bewertet alle nicht-unterdrückten Kandidaten, gewichtet sie
(subject-exact = 3, pattern = 2, body-contact = 1) und wählt nach
(strength desc, routing_priority desc, catalog_index asc) —
routing_priority ist damit
erstmals wirksam (Befund B-1 behoben), fehlende oder ungültige Werte (string/bool) gelten
als neutral-niedrigste und fallen auf die stabile Katalogreihenfolge zurück. Exaktcode-/
Betrefftreffer schlagen reine Kontakt-/Domänentreffer anderer Projekte unabhängig von der
Priorität (Stärke dominiert). Der do_not_route_if-Prädikatsblock ist als
`evaluate_do_not_route_signal` kanonischer Owner in `project_matching.py` und wird von
topic_matching.py importiert: do_not_route_if gilt nun für **Projekte und Topics** mit
identischer Semantik — einmal vor der Root-Schleife (2a) und einmal vor der
Subtopic-Fallback-Schleife. Das
Newsletter-Prädikat ist strikt Betreff-/Header-scoped
(subject, from, to, cc; niemals Body-/Preview-Text; der list.-Token bleibt from/to-only);

no-reply bleibt from-only. Unterdrückte Kandidaten werden als decision.suppressed_candidates[]
(Katalogdaten: kind, id, suppression_reason) sichtbar; Newsletter-unterdrückte Mails
werden deterministisch auf classifier.NEWSLETTER_TARGET_FOLDER (Newsletter,
copy_as_move,
review_required: false) abgebildet, außer ein starker nicht-unterdrückter
Kandidat gewinnt; nicht-Newsletter-Unterdrückung endet mit Review-Grund
(`do_not_route_suppressed`) in INBOX (keep_in_folder). Thread-Vererbung und die
`ambiguity`-Policy bleiben unverändert. Struktureller Nachweis:
[tests/test_classifier_routing_priority.py](../tests/test_classifier_routing_priority.py)
(18 Tests) und [tests/test_classifier_do_not_route.py](../tests/test_classifier_do_not_route.py)
(14 Tests) mit hermetischen BOKU-Analog-Fixtures
([tests/routing_fixtures.py](../tests/routing_fixtures.py)); die Red-Gate-Sequenz
(MD-R1-red-001: 7+6 Assertion-Failures) ist im Run
daedalus/runs/2026-09-22-office-intelligence-fr17-routing-determinism dokumentiert.
Die classifier_revision-Rotation (ASTs von drei der fünf gebundenen Regelmodule geändert)
ist genau einmal und genehmigt. Mail-Desk-Suite: 836/836 grün.

## 9. FR-17/MD-R2 — Thread-/Sibling-Kohärenz (MD-R2 abgeschlossen)

Mit **FR-17/MD-R2** ist die Thread-Kohärenz DNR-gegate: Direkte
`in_reply_to`-Elternvererbung bleibt unverändert DNR-frei; nur die

`references`-Ketten-Auflösung eines Siblings (Final-Location-Elternordner →
exakter, eindeutiger Katalogcode) wird über
[`evaluate_thread_sibling_do_not_route`](../scripts/core/matching/project_matching.py)
(DNR-Owner `project_matching.py`) geprüft. Ein unterdrückter Sibling bleibt
unbekannt/`INBOX`/`keep_in_folder` mit `review_reason`
(`do_not_route_suppressed`), genau einer katalogdaten-only `suppressed_candidates`-Zeile und ohne Thread-Vererbungsanspruch — kein Reroute.
Saubere Siblings erben unverändert; widersprüchliche Ketten, unbekannte/
INBOX-/unowned Eltern und body-only-Tokens verhalten sich wie zuvor; aus
Mailinhalt wird nie ein Ziel abgeleitet. Struktureller Nachweis:
[`tests/test_classifier_thread_sibling_coherence.py](../tests/test_classifier_thread_sibling_coherence.py)
(12 Tests, Red-Gate `MD-R2-red-001`: 4 Assertion-Failures).
Mail-Desk-Suite: 848/848 grün.

## 10. FR-17/MD-R6 — Client-Deadlines und Hänger-Freiheit (MD-R6 abgeschlossen)

Mit **FR-17/MD-R6** läuft jede mehrordrige Himalaya-Client-Operation unter einer
begrenzten Gesamt-Deadline: [`search_mailbox`](../scripts/core/himalaya.py) akzeptiert
`overall_deadline_seconds` (Modul-Default `DEFAULT_SEARCH_OVERALL_DEADLINE_SECONDS = 120.0`, `None` deaktiviert dokumentiert); die Validierung `_validate_overall_deadline_seconds` weist `None`/`bool`/nicht-numerische/nicht-endliche/nicht-positive Werte fail-closed
vor jedem Mailbox-Kommando ab (bounded `ValueError`; auch über die
Manifest-Oberfläche von [`mail_desk_himalaya_client.py`](../scripts/mail_desk_himalaya_client.py)).
Ein per-call `himalaya_timeout` bricht den Ordner-Sweep sofort
fail-closed ab statt stillschweigend übernommen zu werden; die Deadline-Exhaustion ist
terminal (kein Ordner zweimal, kein zweiter Sweep, keine Teil-Ergebnisse als
vollständig). Deadline-Fehler sind begrenzte `himalaya_timeout`-Errors
mit deadline-unterscheidbarem Reason; `run_himalaya` bleibt unverändert
(per-call `timeout=35`, `max_retries=5`, bounded
Transport-Retries). Struktureller Nachweis: [`tests/test_himalaya_client_deadlines.py`](../tests/test_himalaya_client_deadlines.py)
(16 Tests, Red-Gate `MD-R6-red-001`: 5 Assertion-Failures).
Mail-Desk-Suite: 864/864 grün.

## 11. FR-17/MD-R7 — Standalone-Verify-Scope, Provenienz und Evidence-Prüfung (MD-R7 abgeschlossen)

Mit **FR-17/MD-R7** ist der Standalone-Verify-Scope teilmengenfähig:
[`_verified_execute_candidate`](../scripts/core/modes/verify.py) gibt den
Synthesis-Handoff frei, wenn `candidate_ids ⊆ verify_scope_ids == result_ids`
gilt (gemischte Batches mit Archiv-/Newsletter-Item funktionieren; Befund B-9 behoben);
exakte Batches verhalten sich unverändert, Kandidaten außerhalb des Scopes bleiben
`not_required`, `candidate_ids: null` wird fail-closed abgewiesen.
Der Runner-Envelope bewahrt `mode`/`ok` unter `data`
([`mail_desk_batch_runner.py`](../scripts/mail_desk_batch_runner.py)) und der Verify-Reader
nimmt kanonische Sechs-Key-Envelopes als Execute-Provenienz an (Forging bleibt
abgewiesen). Der Evidence-Fallback liest kanonisch `memory/evidence/**`; fehlende
kanonische Evidenz ergibt `in_evidence: false` und markiert das Item
inkonsistent (Befund B-10 behoben), fehlender Root bleibt der H5-None-Fall. Nachweis:
[`tests/test_verify_scope_and_evidence.py`](../tests/test_verify_scope_and_evidence.py)
(7 Tests, Red-Gate `MD-R7-red-001`: 4 Assertion-Failures).
Mail-Desk-Suite: 871/871 grün.

## 12. FR-17/MD-R3 — Deterministische MIME-Inventarkette und Feldkonsistenz (MD-R3 abgeschlossen)

Mit **FR-17/MD-R3** gilt je Item und Lauf genau ein kanonischer MIME-Export:
der MD-E2-Trigger-Gate ([`_evaluate_item`](../scripts/core/attachment_reclassification.py),
nach dem Deaktiviert-Guard) überspringt Items mit
`attachment_status == attachment_inventory_unavailable` oder gesetztem
`attachment_error` und liefert ein begrenztes
`failed/fetch_failed/not_applicable`-Terminal **vor** Source-Pairing und
`read_raw_mime` — ein transienter Export-Timeout erzeugt damit ein
definiertes, wiederholbares Review-Ergebnis ohne Same-Run-Nachlagern (Befund B-3
behoben). Der Konsistenz-Gate
[`discard_inventory_contradicting_evaluations`](../scripts/core/attachment_reclassification.py)
läuft nach der Installation und vor `add_draft_contract` (draft) bzw.
`manifest_proposal` (inspect) und ersetzt eine widersprüchliche
`completed`+nicht-leere `files[]`-Evaluation neben unverfügbarer
Inventur durch denselben begrenzten Terminal (Befund B-4-Fläche behoben; kein
`AttachmentReclassificationContractError` für Policy-Inkonsistenz). Notes
entstehen ausschließlich aus der finalen Entscheidung: unbekannte Items tragen nur
die Review-Wortwahl, keine Zuordnungsformulierung. `derive_evaluation_run_id`
und die `already_fetched`-Idempotenz sind unverändert. Nachweis:
[`tests/test_draft_mime_inventory_determinism.py`](../tests/test_draft_mime_inventory_determinism.py)
(8 Tests, Red-Gate `MD-R3-red-001`: 5 Assertion-Failures).
Mail-Desk-Suite: 879/879 grün.

## 13. FR-17/MD-R4 — Inline-Bild-Policy für die automatische Auswertung (MD-R4 abgeschlossen)

Mit **FR-17/MD-R4** sind Bild-MIME-Parts ohne extrahierbaren Text kein
`required_for_decision`-Trigger der automatischen Auswertung mehr:
[`_is_image_without_extractable_text`](../scripts/core/attachment_evaluation.py)
schließt vor der Eligibility-Entscheidung alle `image/*`-Parts aus —
inline Signatur-Parts (`content_disposition: inline` mit `content_id`)
und angehängte Bilder gleichermaßen (Befund B-5 behoben). Es entsteht kein automatischer
Quarantäne-Fetch, kein `files[]`-Eintrag und keine Materialitätsbindung
für sie; sie bleiben Inventar-Metadaten. Der Ausschluss ist bewusst eng: texttragende
Anhänge (z. B. `text/plain` mit Extrakt) lösen weiterhin aus, inline
`text/plain`-Parts mit Extrakt bleiben gültig, der null-byte-Textfall bleibt
MD-A1-policy-abgewiesen, der menschliche MD-A2-Fetch-Pfad und die MD-A1-Gates
(aktiver Inhalt, disallowed Extensions, Quoten) sind unverändert. Struktureller Nachweis:
[`tests/test_attachment_inline_policy.py`](../tests/test_attachment_inline_policy.py)
(8 Tests, Red-Gate `MD-R4-red-001`: 3 Assertion-Failures).
Mail-Desk-Suite: 887/887 grün.

## 14. FR-17/MD-R5 — Vertragsdokumentation und Laufzeit-Hygiene (MD-R5 abgeschlossen)

Mit **FR-17/MD-R5** ist `keep_in_folder` als kanonischer
`action.type`-Wert in [`references/batch-runner.md`](../references/batch-runner.md)
dokumentiert (Verbleib im Quellordner, kein Mailbox-Write; Befund B-6 behoben), mit
Retention-Semantik in [`references/folder-rules.md`](../references/folder-rules.md); ein
Contract-Test
([`tests/test_action_type_contract.py`](../tests/test_action_type_contract.py)) pinnt, dass
die dokumentierte Enum-Menge exakt der produktionsemittierten Menge plus der Legacy-
Aliase entspricht. [`core/progress.py`](../scripts/core/progress.py) räumt
`progress_*.tmp` in jedem In-Process-Endzustand (success/failed/aborted)
auf — die Atomarität des Ersatzes bleibt erhalten (Befund B-7 behoben;
[`tests/test_progress_hygiene.py`](../tests/test_progress_hygiene.py)). Die
Newsletter-Regel ist konsistent mit der MD-R1-Abnahme. Nachweis: beide neuen
Testmodule grün (6+3 Tests, Charakterisierung ohne hergestelltes Red).
Mail-Desk-Suite: 896/896 grün. **FR-17 ist mit MD-R5 abgeschlossen.**

---

## 15. FR-17/MD-R8 — Reply-Heuristik: Abschluss-/Dankesmails sind nie reply-pflichtig (MD-R8 abgeschlossen; Quote-Härtung 2026-09-23)

**Befund (B-11):** Abschluss-/Dankesmails wurden als `needs_reply: true` klassifiziert
(Env 9412: „vielen Dank für das Zusammenstellen… Liebe Grüße") und landeten fälschlich
im `_Needs-Reply`-Ordner.

**Umsetzung:**
- Kanonischer Owner [`scripts/core/matching/reply_heuristics.py`](../scripts/core/matching/reply_heuristics.py):
  Closing-/Dankesmarker („vielen Dank", „passt für mich", Grüße-Floskeln),
  Request-Signal-Gate (Frage, Bitte, Frist, Entscheidung, Freigabe, Beitrag),
  `_strip_quoted_history` (zitierte `>`/`>>>`-Blöcke, Header-Seperatoren und
  `Am … schrieb …:`-Zeilen zählen nicht als Anforderung der aktuellen Mail),
  `is_closing_or_thanks`, `needs_reply_review` und `downgrade_if_closing`
  (Prüftext explizit als `subject`/`body`) mit `reply_downgrade`-Provenienz
  (`rule_revision: "md-r8"`).
- Facade-Verdrahtung im einzigen `_finish`-Punkt von `classify_email_two_pass`
  ([`scripts/core/classifier.py`](../scripts/core/classifier.py)): der Downgrade wirkt
  in **beiden** Pässen — ein aus dem Preview abgeleiteter Reply-Bedarf wird durch den
  Full-Read korrigiert und umgekehrt; nur ein aktuell assertierter `needs_reply`-Wert
  wird herabgestuft, `false`-Werte und Review-Semantik bleiben unberührt.
- `FULL_BODY_ACTION_REQUEST` bleibt unverändert der Escalation-Trigger; MD-R8 ändert
  ausschließlich die Bedarfsbewertung, nicht die Volltext-Eskalation.

**Pflichttests (alle erfüllt, `tests/test_reply_heuristics.py`):** Dankes-/Abschlussmails
(„vielen Dank", „passt für mich", „Liebe Grüße" ohne Rückfrage) → `needs_reply: false`;
echte Bitten/Fragen/Fristen werden nie herabgestuft; Thread, in dem nur die erste Mail
eine Bitte enthielt und die letzte abschließt → `false` (auch wenn der zitierte
Vorgänger eine Anforderung enthält); Quote-Grenz-Varianten (`>`, `>>>`,
`-----Ursprüngliche Nachricht-----`, `____…`, `Am … schrieb …:`) werden erkannt;
Review-Fall bleibt erhalten; Decisions enthalten keine `_closing_check_*`-Debugfelder.
**Live-Nachweis 2026-09-23 (Env 9412):** Re-Klassifikation ergibt `needs_reply: false`
mit `reply_downgrade` (`rule_revision: md-r8`). **Suite 908/908 grün.**

---

## 16. FR-18 — Workspace-Agnostizismus: Desk-Signals-Katalog und Identitäts-Bindung (MD-S1–MD-S3 abgeschlossen)

**Problem:** Der Basis-Klassifikator und `sent_indexer` trugen hartcodierte
Identitäts-/Routing-Annahmen ohne Workspace-/Account-Bezug: die `martin`-Reply-Trigger,
die `no_reply_sender_tokens`, das `"mailbox": "BOKU-MARTIN"`-Literal je Sent-Index-Eintrag
und der Zoom-Recording-Hardcode (`Themen/BOKU-Organisation`). Ein konsumierender
Workspace mit anderem Desk-Owner erhielt aus dieser Heuristik niemals einen Reply-Bedarf
(stille Unter-Klassifikation), und Sent-Indexe/Zoom-Routings waren an einen konkreten
Workspace gebunden.

**Umsetzung:**
- **MD-S1 — Desk-Signals-Katalog:** Kanonischer Loader
  [`load_reply_heuristics`](../scripts/core/matching/reply_heuristics.py) liest
  `memory/references/mail-desk/mail-desk.json` (Schema 1: `reply_heuristics` mit
  pflichtigem `reply_triggers`, optionalem `no_reply_sender_tokens`, optionalem
  `owner_address`). Fehlende Datei → dokumentierte Kompatibilitäts-Defaults (die
  bisherige Trigger-Liste); invalides JSON oder Schema-Drift (inkl. `schema_version`
  als bool/float/str) → fail-loud, nie stiller Fallback. Trigger-Matching mit
  Wortgrenzen (`matches_reply_trigger`); `classify_email` konsumiert die geladene
  Konfiguration statt der hartcodierten Liste, inklusive für `unknown`-Items.
- **MD-S2 — Sent-Index-Account-Bindung:**
  [`sync_sent_items_by_date`](../scripts/core/sent_indexer.py) verlangt einen
  gebundenen Account: ohne (`None`/blank) → strukturiertes fail-loud `ValueError`
  (Account-Bindung, konsistent zur Attachment-Bindung), bevor irgendein Write oder
  Himalaya-Aufruf erfolgt; jeder Eintrag trägt `"mailbox": <verifizierter Account>`.
  `draft_manifest` reicht seinen `account`-Parameter durch (eine Zeile).
- **MD-S3 — Zoom-Recording-Routing im Katalog:** Der hardcoded Recording-Pfad
  (`Themen/BOKU-Organisation`/`boku-organisation`) ist aus dem Bundle entfernt;
  das Routing gehört in den Topic-Katalog des konsumierenden Workspace
  (`typical_subject_patterns`/`keywords` am Topic-Entry). Ohne Katalog-Eintrag ergibt
  sich `unknown`/Review statt stiller Fehlroute. Die generische
  `zoom-join-ping` → `Trash`-Heuristik bleibt (workspace-unabhängig). Der Such-Fallback-
  Ordnerliste in [`himalaya.py`](../scripts/core/himalaya.py) wurde der workspace-
  spezifische Eintrag entzogen (neutral: INBOX, Junk, Trash, Newsletter).

**Pflichttests (alle erfüllt):** `tests/test_reply_trigger_catalog.py` (15,
inkl. Schema-Drift bool/'1'/1.0), `tests/test_sent_indexer_account_binding.py` (4,
inkl. Fail-loud vor Write und Entry-Struktur-Regression),
`tests/test_zoom_recording_catalog_routing.py` (5, inkl. Katalog-Charakterisierung,
Fehlroute-frei ohne Entry und Join-Ping-Regression). **Suite 932/932 grün.**

## 17. FR-21 — Desk-Signals-Doku + Workspace-Katalog-Validator (MD-S4/MD-S5 abgeschlossen)

**Problem:** Der FR-18-Desk-Signals-Katalog war implementiert, aber undokumentiert
(SKILL.md/batch-runner.md); die Literal-/Lookaround-Semantik von
`typical_subject_patterns` war nirgends dokumentiert (Migrationsprompts nutzten
regex-artige Patterns, die nie matchen); Workspace-Kataloge hatten keinen
ausführbaren Validator (`load_catalogs` schluckt Drift still).

**Umsetzung:**
- **MD-S4 — Dokumentation:** SKILL.md-Abschnitt „Desk-Signals-Katalog
  (mail-desk.json)" (Pflegevertrag: Trigger nur im Workspace-Katalog, Bundle-Default
  = Kompatibilitäts-Fallback, Schema-1-Gate, `owner_address` als reiner
  Katalog-Contract — die Auswertung im Sent-Reply-Check ist FR-18-Target-Verhalten,
  es existiert heute kein Consumer); batch-runner.md-Verweis;
  topic-catalog-entry-Abschnitt „Betreffmuster-Semantik" (Literal + Lookaround
  `(?<!\w)`/`(?!\w)`, IGNORECASE, Mindestlänge 3, `[-_]+`-Normalisierung,
  Gegenbeispiel; Root-Topic-Unterschied: plain Substring/``, zusätzlich
  Parent-Signal-Gate `_topic_parent_subject_signal`).
- **MD-S5 — Katalog-Validator:** [`catalog_validator.py`](../../scripts/catalog_validator.py)
  (read-only CLI, kanonischer Envelope `catalog_validator`, Exit 0/1/2) validiert
  `topics.json`, `projects.json` und das optionale `mail-desk.json` (fehlend =
  dokumentierter Fallback). Root-Patterns brauchen nur Nicht-Leer-String
  (Root-Routing per plain Substring ohne Längen-Gate, `select_topic_match`);
  nested Patterns (`subtopics[]`/`operations[]`/`events[]`) behalten
  Nicht-Leer-String + Mindestlänge 3 (`_subject_signal_matches`). Live-Lauf gegen
  `boku-user`: valid (der anfängliche `QC`-Treffer war ein False Positive der
  Root-Ebene).

**Pflichttests (alle erfüllt):** `tests/test_catalog_docs_contract.py` (13,
liest die realen Dokumentdateien), `tests/test_catalog_validator.py` (35, je
Drift-Klasse, Root-vs-Nested, CLI 0/1/2, boku-user-Form). **Suite 980/980 grün.**

## 19. MD-BC — Harness-Ausgabe-Boundaries: Batch-CLI-Wrapper und Katalog-Inspector (abgeschlossen)

**Problem:** Der Batch-Runner emittiert seinen vollständigen Envelope auf stdout;
bei `draft`-Läufen mit Anhängen sprengt das Harness-Output-Fenster (opencode
schneidet ab und leitet in eine Datei um). Der Katalog-Feld-Inspekt per rohem
`jq`/JSON-Lesen erzeugte dasselbe Problem auf der Read-Seite.

**Umsetzung:**
- **`mail_desk_batch_cli.py` (Wrapper):** Persistiert Runner-stdout/-stderr nach
  `<workspace>/tmp/mail-batch/` und emittiert einen kompakten kanonischen
  Summary-Envelope (Exit-Code, Zustand, Review-Hash, draft-/execute-Zähler).
  `--reconcile` wird korrekt ohne `--input` durchgereicht (reconcile liest das
  Recovery-Journal); `--account` optional (Default: Workspace-Backend-Binding,
  kein Workspace-Literal); `--keep N` prunt die ältesten Envelope-/Progress-Paare.
- **`catalog_inspect.py` (read-only):** Feld-Inspektion der drei
  Workspace-Kataloge als kanonischer Envelope; `--id` respektiert `--fields`
  (bounded Output); Fehler (fehlende Datei, defektes JSON, unbekannte Payload)
  enden kanonisch (`CatalogError`, Exit 2), nie bare `SystemExit`.
- Beide Skripte nutzen `core.envelope` (kanonischer Vertrag) und wurden aus dem
  Consumer-Betrieb adoptiert und gehärtet (Fix-Runde MD-BC-fix-001).

**Pflichttests (alle erfüllt):** `tests/test_batch_cli_wrapper.py` (11,
Argument-Threading, Account-Neutralität, Prune-Hygiene, Envelope-Fehlerpfad),
`tests/test_catalog_inspect.py` (6, Payload-Klassen, Field-Bounds, Envelope).
**Suite 1032/1032 grün.**

## 18. FR-22 — Identity-freier Desk-Signals-Fallback und Katalogisierung der BOKU-Restbestände (MD-ID1–MD-ID4 abgeschlossen)

**Problem:** Der FR-18-Trigger-Fallback trug 9 hardcodierte „martin"-Trigger
(Identity-Leak im Bundle, toter Code für Katalog-Besitzer); die
sent_indexer-Domain-Whitelist und das „boku"-Stopwort, das internal-domain-Matching
(`@boku.ac.at`) und die Freemail-Spam-Gegenindikatoren waren weitere
Workspace-Spezifika im Bundle-Code; die FR-18-Zielinvariante „sent_indexer-Domain-Liste
über Desk-Katalog konfigurierbar" war nicht implementiert.

**Umsetzung:**
- **MD-ID1 — Schema 2 + neutraler Fallback:** Fehlende `mail-desk.json` →
  **leere** Trigger-Liste (keine Anrede-Trigger ohne Katalog; `needs_reply` bleibt
  trigger-false). Schema 2 (striktes Gate, bool/float/str abgewiesen): `reply_triggers`
  optional, wenn `owner_address` gesetzt ist — dann leitet der Loader über
  `derive_greeting_triggers` die generische Anrede-Klasse aus dem Vorname-Segment des
  Owner-Local-Parts ab (Wortgrenzen-Predicate byte-identisch); explizite Trigger werden
  nie gemischt. Schema-1-Dateien bleiben valide (Legacy; der reale `boku-user`-Katalog
  bleibt unverändert verwendbar). Vier neue optionale Katalogfelder:
  `sent_sender_domain_whitelist`, `sent_subject_stopwords`, `internal_domains`,
  `spam_sender_allowlist` (je Liste nicht-leerer Strings, dokumentierte Defaults).
  Kein „martin"-Literal bleibt als erwarteter Fallback im Bundle.
- **MD-ID2 — sent_indexer-Katalogisierung:** `check_if_replied` erhält optional
  `identity_config` (None → Dataclass-Defaults ohne Datei-I/O); die Stopwort- und
  Domain-Whitelist-Mengen kommen aus dem Katalog (Defaults via Re-Export aus
  `reply_heuristics` — identische Objekte, `assertIs`-gepinnt); kein „boku"-Literal
  in `sent_indexer.py`; Standardverhalten byte-identisch.
- **MD-ID3 — internal-domain-Matching:** `select_project_match`/`select_topic_match`
  erhalten optional `internal_domains` (Default = Katalog-Default `boku.ac.at`);
  die generische Freemail-Ausschlussmenge (`gmail.com`, `outlook.com`, `yahoo.com`)
  ist Bundle-Konstante (`GENERIC_FREEMAIL_DOMAINS`); classifier threadt die
  Katalogliste in beide Selektoren. Default-Verhalten byte-identisch.
- **MD-ID4 — Spam-Gegenindikatoren entfernt + Allowlist:** Die 4
  BOKU-Content-Gegenindikatoren sind aus dem `junk-freemailer`-Zweig entfernt
  (Zweig = Phishing-Betreffmuster UND Freemail-Domain); optionaler
  `spam_sender_allowlist`-Gate (Exaktadresse ODER Domain, case-insensitive) als
  erste Bedingung — kataloggetreue Freemail-Kontakte fallen durch zur normalen
  Klassifikation. Branch-Reihenfolge und Freemail-Domain-Liste unverändert.

**Pflichttests (alle erfüllt):** `tests/test_reply_trigger_catalog.py` (26,
Schema-2-Contract), `tests/test_sent_indexer_identity_catalog.py` (8, Identity-Config),
`tests/test_internal_domain_catalog.py` (9, Domain-Gating),
`tests/test_classifier_hardening.py` (12, Allowlist + Gegenindikator-Entfernung),
`tests/test_catalog_validator.py` (38, Schema-1+2-Gate),
`tests/test_catalog_docs_contract.py` (13, neutraler-Fallback-Wording),
`tests/test_reply_heuristics.py` (12, MD-R8-Intent mit hermetischen Katalog-Fixtures).
**Suite 1015/1015 grün.** Fix-Runde MD-ID-fix-001: Validator-Gate für unbenutzbare
Owner-Local-Parts (reuse `derive_greeting_triggers`, keine duplizierte Logik).
