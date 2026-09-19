# Mail-Desk — Subsystem System Map: Seiteneffekte (Effects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Seiteneffekte  
> **Subsystem**: [`skills/mail-desk`](../SKILL.md)  
> **Ziel**: Vollständige Dokumentation aller Sicherheitsgrenzen, Dateisystem-Barrieren, Fail-Closed-Bedingungen und Plattformspezifika.  
> **Gültig für**: `skills/mail-desk/` relativ zum Repository-Root

---

## 1. Plattform-Spezifika & Windows-Barrieren

### 1.1 Reparse-Point- & Symlink-Erkennung (NTFS 0x400)
Zur Abwehr von Junction-Escapes und Symlink-Angriffen in Quarantäneverzeichnissen implementiert [`scripts/core/attachment_fetch.py`](../scripts/core/attachment_fetch.py) und [`attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py) eine strikte Attributprüfung:

```python
# Bit 0x400 = FILE_ATTRIBUTE_REPARSE_POINT unter Windows
if hasattr(stat_res, "st_file_attributes") and (stat_res.st_file_attributes & 0x400):
    raise SymlinkEscapeError("Reparse point or directory junction detected")
```
* **Effekt:** Dateipfade, die auf Reparse-Points, Directory Junctions oder Windows-Symlinks verweisen, brechen fail-closed mit `SymlinkEscapeError` ab. Quarantänedateien müssen echte, reguläre Dateien sein.

### 1.2 Windows Drive Colon Bug Workaround (Himalaya 1.2.0)
Himalaya interpretiert Doppelpunkte (`:`) in `-c <config>` auf Windows fälschlich als Pfadlistentrenner.
* **Funktion:** `normalize_himalaya_config_path()` in [`scripts/core/himalaya.py`](../scripts/core/himalaya.py#L33-L51)
* **Geltungsbereich:** `resolve_himalaya_invocation()` normalisiert **beide** Pfadquellen vor jeder Existenzprüfung (`is_file`) und vor der `-c`-Tokenerzeugung identisch: einen expliziten `HIMALAYA_CONFIG`-Override ebenso wie den plattformspezifischen Default aus `_default_himalaya_config_path()` (`%APPDATA%\himalaya\config.toml` unter Windows, sonst `~/.config/himalaya/config.toml`).
* **Effekt:** Lokale Laufwerkspfade (z. B. `C:\Users\...` oder `D:/...`) werden deterministisch in lokale UNC-Admin-Share-Pfade umgewandelt (`\\localhost\C$\...` bzw. `\\localhost\D$\...`). Relative Overrides bleiben unverändert und stoppen fail-closed als `himalaya_config_invalid`.
* **Betriebliche Voraussetzung (Caveat):** Die Konvertierung setzt voraus, dass die lokale administrative Freigabe `\\localhost\<drive>$` erreichbar ist. Ist sie deaktiviert oder nicht erreichbar, liefert `is_file()` auf dem konvertierten Pfad `False`; der Bootstrap stoppt dann vor jedem Prozessstart fail-closed als nicht verfügbare Konfiguration (`himalaya_config_missing`). Eine Ende-zu-Ende-Unterstützung ohne erreichbare Admin-Freigabe wird nicht behauptet.

---

## 2. Geheimnisschutz & Filterung verbotener Inhalte

Um zu verhindern, dass vertrauliche Mail-Inhalte, Tokens oder Prompts unbemerkt in maschinelle Indizes oder Logs sickern:

* **Rekursiver Filter:** `_find_forbidden_content_keys()` in [`scripts/core/attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py#L154-L165) und [`scripts/core/attachment_disposition_log.py`](../scripts/core/attachment_disposition_log.py).
* **Verbotene Schlüssel:**
  `text`, `extracted_text`, `content`, `body`, `prompt`, `llm_prompt`, `response`, `model_response`, `credentials`, `password`, `token`, `tokens`, `api_key`, `envelope_id`, `himalaya_id`.
* **Geltungsbereich:** Gilt für Metadaten, `disposition_ref`, `ApprovalReceipt`, `DispositionRequest`, `ApplyRequest`, Disposition-Logs und Discard-Journaleinträge.
* **Effekt:** Taucht einer dieser Schlüssel auf (auch tief geschachtelt), bricht die Operation sofort mit `ForbiddenContentError` ab.

---

## 3. Concurrency & Lock-Ownership

Der Schutz vor Race Conditions und parallelen Mutationen erfolgt mehrstufig:

* **Workspace-Lease (Ebene 1):** Jede schreibende Skriptausführung (`execute`, `save_quarantine_index_atomic`, `apply_attachment_disposition`, `op_attachment_fetch`, `cleanup_run_quarantine` sowie die OCR-Derivat-Promotion in `extract_attachment_content`) verlangt eine verifizierte Workspace-Lease via `require_workspace_lock()`.
  * **Kein Legacy-Bypass (normative Invariante, geschlossen):** Für keinen Attachment-/Quarantäne-Schreibpfad darf `--allow-legacy` (`allow_legacy=True`) oder `WORKSPACE_LOCK_ALLOW_LEGACY` eine Lease ersetzen. Der shared Guard wird ausnahmslos mit `allow_legacy=False` aufgerufen; kein Env-, Parameter- oder Manifest-Wert kann Legacy reaktivieren.
  * **Geschlossener Drift (FR-15/MD-E1-T01):** `verify_workspace_lock()` in [`scripts/core/attachment_fetch.py`](../scripts/core/attachment_fetch.py) wertet `WORKSPACE_LOCK_ALLOW_LEGACY` nicht mehr aus und führt keinen `allow_legacy`-Parameter mehr; `op_attachment_fetch`, `cleanup_run_quarantine` sowie [`attachment_extract.py`](../scripts/core/attachment_extract.py) (`extract_attachment_content`, `_extract_content_internal`) haben den Parameter ebenfalls verloren. Die Quarantäne-Index-Mutation ([`attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py#L755-L800)) erzwingt weiterhin hardcodiert `allow_legacy=False`.
  * **Zulässige Ownership:** Ausschließlich die vertrauenswürdige Lease-/Conversation-ID aus der Harness-Control-Plane (explizite `lease_id`/`conversation_id`-Argumente oder `WORKSPACE_LOCK_LEASE_ID`/`WORKSPACE_LOCK_CONVERSATION_ID`) wird an den Guard durchgereicht.
  * **Effekt:** Ohne gültige, eigene Lease bricht der Prozess vor jedem Quarantäne-Write mit `WorkspaceLockRequiredError` ab; Tests belegen, dass Env-, Parameter- und Manifest-Werte keinen Schreibpfad öffnen.
* **Inventar-File-Lock (Ebene 2):** Zur Absicherung gleichzeitiger Zugriffe auf Quarantäne-Dateien und `.quarantine-inventory.json` verwendet der Mail-Desk den `_QuarantineInventoryLock` (`.quarantine-inventory.json.lock`).
  * **Mechanismus:** Lock-Directory mit PID-Binding, Timeout (30s) und Prüfung veralteter Locks (Stale-Detection >300s).
  * **Effekt:** Verhindert parallele Teilmutationen von Ingest, Reconcile und Discard-Cleanup.
* **Tracked-Quarantäne-Preflight (Ebene 3, FR-15/MD-E1-T02):** Nach der Lock-Ownership-Prüfung und vor dem ersten Quarantäne-Write (`op_attachment_fetch`) bzw. vor dem ersten mutierenden OCR-Derivat-Write (`extract_attachment_content`/`_extract_content_internal`) prüft [`scripts/core/quarantine_preflight.py`](../scripts/core/quarantine_preflight.py) den Git-Index des vertrauenswürdigen `workspace_root` (`git ls-files`, ohne Shell, mit Timeout).
  * **Getrackte Quarantäne:** Jede getrackte Datei unter `data/mail-desk/attachments/` sowie jede getrackte `**/.quarantine-inventory.json`/`.quarantine-inventory.lock` stoppt fail-closed mit `TrackedQuarantineError`.
  * **Bounded & fail-closed:** Non-Zero-Exit, Timeout oder unlesbarer Git-Output stoppen fail-closed mit `QuarantinePreflightError` — niemals stiller Pass.
  * **Read-only:** Der Preflight verändert weder `.gitignore` noch staged/committet/entfernt er Dateien oder Repository-Konfiguration; die Detection-Semantik ist mit dem MD-Q1-Test-Helper single-sourced.
* **Receipt-Klassen-Grenze (Ebene 4, FR-15/MD-E1-T03):** [`scripts/core/attachment_authorization.py`](../scripts/core/attachment_authorization.py) erzwingt die kontextgebundene Receipt-Klassen-Grenze. Die interne Fabrik `create_machine_authorization()` erzeugt ausschließlich aus der vertrauenswürdigen Draft-Control-Plane eine Maschinen-Autorisierung (`receipt_class: "machine"`, `receipt_type: "attachment_auto_evaluation"`, Issuer `mail_desk_auto_evaluator`) mit gebundenem `request_hash` gleich dem kanonischen MD-A2-`review_hash` sowie Policy-, Account-, Message-ID-, Folder-, Envelope-ID-, Part-Locator- und Inventar-Bindungen.
  * **Prozessinterne Identitäts-Provenienz:** Die Fabrik liefert ein opakes Mapping-Objekt; Autorität ist an die exakte Objekt-Identität gebunden, nicht an einen vom Objekt exponierten Wert. Eine modul-private Weak-Identity-Registry (`weakref.WeakKeyDictionary`) ordnet das ausgestellte Objekt dem beim Mint festgehaltenen kanonischen Content-Hash zu; der Guard prüft zuerst, dass der Registry-Eintrag per `is` auf das exakte Objekt verweist, und danach den Content-Hash. Die Registry räumt sich mit der Garbage Collection selbst auf (kein Memory-Leak, keine Single-Use-Anforderung); es wird kein Token/Secret auf dem Capability-Objekt gespeichert, serialisiert oder geloggt. Ein strukturell identisches Caller-/Mail-/Manifest-Dictionary, ein nachgebautes Look-alike-Objekt, `copy`/`copy.deepcopy` und Pickling sind damit keine Autorität; eine Manipulation einzelner Felder (Issuer, Policy, Request-Hash, Bindungen) wird über den Content-Hash erkannt. Die Grenze richtet sich gegen externe Daten; beliebiger, bereits im Prozess laufender Code mit Zugriff auf modul-private Interna liegt außerhalb der Grenze (keine Python-Access-Control-Sandbox).
  * **Kontextbewusster Guard:** `guard_context_authorization()` akzeptiert im `evaluation`-Kontext ausschließlich die exakt intern ausgestellte Maschinen-Autorisierung und verlangt dort zusätzlich expliziten, nicht-leeren `expected_request_hash` und `expected_policy_revision` (fehlt einer, wird auch ein genuines Capability nicht akzeptiert). Jeder Human-Approval-Kontext (Filing, Promotion, Export, Disposition, Apply-Discard und der manifest-getriebene direkte Fetch) weist die Maschinen-Klasse/-Typ/-Issuer fail-closed ab (`ReceiptClassRejectedError`), während die bestehende typenlose menschliche FR-08-MD-A2-Receipt-Form unverändert gültig bleibt (keine Schema-Migration). Der Guard ist an den Callsites [`attachment_filing.validate_mda2_attachment`](../scripts/core/attachment_filing.py), [`attachment_disposition_log.record_disposition_entry`/`verify_apply_receipt`](../scripts/core/attachment_disposition_log.py) und dem direkten Fetch in [`mail_desk_himalaya_client.py`](../scripts/mail_desk_himalaya_client.py) verdrahtet. Promotion/Export besitzen weiterhin **keine** Laufzeitpfade; nur die Guard-Semantik und Tests existieren, damit künftige Aufrufer die Grenze erben.
  * **Kein Duplikat:** Der Guard selbst ruft keinen zweiten Hash-/Request-Validator auf, sondern verwendet für den `evaluation`-Kontext [`attachment_fetch.verify_approval_receipt`](../scripts/core/attachment_fetch.py#L236) weiter. Der pre-existierende Struktur-Validator `attachment_disposition_log.validate_receipt_structure` bleibt der nachgelagerte Validator der Disposition-/Apply-Callsites und wird vom Guard **nicht** aufgerufen. Der MD-E1-Evaluierungs-Orchestrator (`attachment_evaluate`) ist **nicht** Teil von T03 und hier nicht als T03-Arbeit dokumentiert.
* **Evaluierungs-Orchestrator (Ebene 5, FR-15/MD-E1-T04 + T05):** [`scripts/core/attachment_evaluation.py`](../scripts/core/attachment_evaluation.py) stellt den öffentlichen `attachment_evaluate`-Seam bereit. Er akzeptiert nur rohes MIME plus vertrauenswürdigen Message-/Binding-Kontext, Decision-/Read-Escalation-Metadaten, die effektive Policy und minimale vertrauenswürdige Fetch-/Extract-Control-Plane-Bindungen (`run_id`, `data_dir`, `workspace_root`, `lease_id`, `conversation_id`); Kandidaten-, Policy-, Fetch-Status-, Receipt-, Materiality- oder Handoff-Werte des Callers sind keine Autorität. Die echte MIME-Struktur wird über `inspect_mime_tree`, `canonicalize_and_bind_attachments` (inkl. `check_attachment_policy` mit kumulativen Quoten) und `verify_attachment_drift` revalidiert. Für jeden zulässigen Part (`fetch_status: "available"`, `policy_status: "allowed"`) wird der kanonische `review_hash` berechnet und die interne Maschinen-Autorisierung gemint und sofort im `evaluation`-Kontext geprüft; die effektive `policy_revision` stammt ausschließlich aus dem `version`-Feld der effektiven Policy (malformt/fehlend → `AttachmentEvaluationError`).
  * **T05 lineare Komposition:** Vor der Fetch-Schleife ruft der Orchestrator explizit `attachment_fetch.verify_workspace_lock` mit den vertrauenswürdigen Control-Plane-Bindungen auf; kein I/O geschieht vor diesem Guard. Danach läuft pro zulässigem Part das unveränderte `op_attachment_fetch` (mit eigenem Lock-/Preflight-/Drift-Check), dem erst nach bestandenem `evaluation`-Guard der nicht-autoritative `capability.to_dict()`-Snapshot als struktureller `approval_receipt` übergeben wird (die opake Capability wird nie exponiert). Alle Anhänge einer Mail teilen **eine** `run_id`, damit kumulative Count-/Size-Quoten nicht umgangen werden; ohne Vorgabe wird sie vom ersten kanonischen Fetch allokiert und wiederverwendet. `fetched` und `already_fetched` laufen identisch durch `extract_attachment_content`; Office-/PDF-Formate nutzen ausschließlich diesen Extraktor. Aus den angereicherten Envelopes und den kanonisch gebundenen Parts entsteht **ein** `build_attachment_analysis_handoff(default_materiality="required_for_decision")`, das vor der Rückgabe mit `validate_attachment_handoff` validiert wird; `apply_attachment_handoff_to_item` wird nie aufgerufen. Die 15.000-/30.000-Zeichen-Budgets samt sichtbaren Truncation-Markern stammen unverändert aus dem Builder und werden nicht dupliziert.
  * **Bounded Rückgabe:** `{"attachment_evaluation": {…}, "attachment_analysis_handoff": {…}}`. Erfolgreich validierter `ready`-Handoff → `status: "completed"`, `reason: "handoff_ready"`, `authorization: "auto_evaluated"`, befüllte sichere `files[]` (genau `{filename, sha256, mime_type, chars, coverage, run_id}`, `coverage ∈ {full, truncated}`), `used_for_classification` immer `false`, `classifier_revision` immer `null`. Ein validierter `blocked_on_required_attachment`-Handoff bleibt `completed`/`still_ambiguous` und `required_for_decision` (nie `supplementary`). Absolute Pfade oder Rohinhalte erscheinen nie im staged Objekt; nur der gekapselte Handoff trägt begrenzten Inhalt.
  * **Abgrenzung:** Die negative Fehler-/Reason-Matrix (`lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`, `handoff_invalid`) bleibt **MD-E1-T06**; jede `DraftManifest`-Installation bleibt **MD-E2**. Der Orchestrator ruft keine Mailbox-, Promotions-, Export-, Filing-, Dispositions-, Evidence-, Katalog-, Cloud-, Classifier- oder Cleanup-/GC-Seam auf und bewahrt Quarantäne-Artefakte/Inventar für MD-E2 auf.

---

## 4. Fail-Closed Drift- & Integritäts-Erkennung

Der Mail-Desk repariert Diskrepanzen **niemals still oder automatisch**:

* **Quarantine-Index-Drift:** Die 17 kanonischen Basisfelder sowie der optionale konsistente Block aus 6 Coverage-Feldern im `attachment-quarantine-index.json` werden streng typisiert. `CANONICAL_ENTRY_FIELDS` bindet alle 23 Felder bei der Idempotenzprüfung. Widersprüchliche Alias-Felder (z. B. `folder` vs. `original_folder` oder `clean_filename` vs. `filename`) werden als Manipulation gewertet → `AttachmentIndexDriftError`.
* **Bereinigungs-Scope-Drift:** Beim Ausführen einer Discard-Bereinigung (`apply_attachment_disposition`) muss der tatsächliche Kandidaten-Scope deterministisch mit dem `apply_request_hash` des `ApplyRequest` und `ApprovalReceipt` übereinstimmen. Jede Abweichung im Scope bricht die Bereinigung vor dem Unlink ab → `AttachmentDispositionError`.
* **Recovery-Journal-Integrität:**
  * Das `attachment-discard-journal.json` verlangt eine lückenlos monotone State Machine (`prepared → file_deleted → inventory_updated → index_updated → completed`).
  * Die History muss zwingend mit `prepared` beginnen.
  * Top-Level-`status` (`in_progress`, `completed`, `failed`), `last_successful_state`, `failure_stage` und `error` müssen exakt mit dem letzten History-Eintrag (`history[-1]`) übereinstimmen. Verkürzte oder manipulierte Journale werden strikt abgelehnt.
* **Physische Integrität:** `size_bytes <= 0` in `.quarantine-inventory.json` oder im Index wird als korrupte Datei/Manifest gewertet und abgelehnt.
* **Tracked-Quarantäne-Preflight:** Ein versehentlich im Git-Index getrackter Quarantänepfad ist ein begrenzter fail-closed Stop vor dem ersten Write (§3). Ebenso stoppen Non-Zero-Exit, Timeout oder unlesbarer Git-Output fail-closed; der Preflight repariert nichts und entfernt keine getrackten Dateien.
* **Effekt:** Tritt eine Integritätsverletzung auf, bricht die Engine sofort fail-closed ab (`AttachmentIndexDriftError` bzw. `AttachmentDispositionError`). Es finden keine Schreib- oder Löschoperationen statt; der Zustand verharrt zur menschlichen Begutachtung.

---

## 5. Subprozess-Isolation & Netzwerk-Grenzen

* **Kein interaktiver Setup-Wizard:** Fehlt `config.toml`, wirft `resolve_himalaya_invocation()` sofort `HimalayaInvocationError("himalaya_config_missing")`. Ein interaktives Blockieren des Prozesses ist ausgeschlossen.
* **Timeout-Abbruch:** Hängt die IMAP-Verbindung, greift der Subprozess-Timeout. Ein Timeout führt zu `himalaya_timeout` und wird **nicht** wiederholt (kein Retry-Loop bei hängenden Prozessen).
