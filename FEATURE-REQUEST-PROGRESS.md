# Feature Request Progress & Code Review

> [!NOTE]
> **Ephemere Arbeitsdatei für den Implementierungsagenten und das Code-Review.**  
> Dokumentiert den aktuellen Durchführungsstand, Mini-Walkthroughs, Diffs und Verifikationsnachweise der aktiven Pakete. Diese Datei wird nach erfolgreicher Abnahme bereinigt bzw. auf das nächste Paket umgestellt.

---

## Übersicht FR-08: Paketstatus

| Paket | Status | Commit | Verifikation |
| --- | --- | --- | --- |
| `MD-A1` — Read-only MIME-Inventar | ✅ abgeschlossen | `6f86c67` | 46 fokussierte / 276 Gesamt-Tests |
| `MD-A2` — Reviewgebundener Quarantäne-Abruf | ✅ nachgebessert | Review (ungestaged) | 37 fokussierte / 351 Gesamt-Tests |
| `MD-A3` — Begrenzte Extraktion & OCR-Derivat | ✅ abgeschlossen | `8bb87b5` | 11 fokussierte / 301 Gesamt-Tests |
| `MD-A4` — Materialitäts-Gate & LLM-Handoff | ✅ abgeschlossen | `a50652f` | 14 fokussierte / 315 Gesamt-Tests |
| `MD-A5` — Ablagevorschlag | ✅ abgeschlossen | `286e238` | 13 fokussierte / 328 Gesamt-Tests |

---

## Paket: FR-08 / `MD-A2` — Reviewgebundener Abruf in Temp-Quarantäne

- **Status:** ✅ Nachgebessert & Verifiziert (Bereit für Abnahme)
- **Scope:** Nur Transport und lokale Quarantäne; keine Extraktion, kein LLM, kein Cloud-Vorschlag, keine Mailboxmutation.

### 1. Zusammenfassung der Umsetzung & Nachbesserungen

Nach dem Code-Review wurden alle identifizierten Vertragslücken vollständig behoben:

1. **Ausschluss von Workspace-Lock-Parametern aus dem Manifestvertrag:**
   - [`mail_desk_himalaya_client.py`](skills/mail-desk/scripts/mail_desk_himalaya_client.py): `lease_id`, `conversation_id` und `allow_legacy` wurden vollständig aus dem Manifestvertrag entfernt. Untrusted Manifest- oder Operationsdaten können die Mutationsautorisierung weder liefern noch übersteuern; `allow_legacy=False` ist im produktiven Manifestpfad zwingend verankert.
   - Autorisierung erfolgt ausschließlich über die vertrauenswürdige Harness-/Prozess-Control-Plane.
   - Adversariale Tests verifizieren, dass eingeschleuste Lock-IDs und `allow_legacy: true` im Manifest ignoriert werden und ohne tatsächlich gehaltenen Lock fail-closed mit strikten 0 Disk-I/O abbrechen.
2. **Gemeinsame Serialisierung von Quotenprüfung, Promotion und Inventarupdate:**
   - [`op_attachment_fetch()`](skills/mail-desk/scripts/core/attachment_fetch.py): Quotenprüfung (`check_quarantine_quotas`), Re-Check des Zielzustands, atomare Promotion (`_atomic_no_clobber_promote`) und Inventaraktualisierung (`_record_in_quarantine_inventory_unlocked`) bilden eine unteilbare gemeinsame kritische Sektion unter `_QuarantineInventoryLock(run_dir)`.
   - Nach Lock-Erwerb werden Zieldateizustand und per-Message-Quoten mit den aktuellen Werten erneut geprüft. Konkurrierende Threads können dadurch die Grenzwerte von 5 Dateien bzw. 25 MB kumulativ niemals überschreiten.
3. **Erzwungener Workspace-Lock vor jeglicher lokaler Mutation:**
   - [`verify_workspace_lock()`](skills/mail-desk/scripts/core/attachment_fetch.py): Bindet den kanonischen `workspace-lock/scripts/workspace_lock_guard.py` dynamisch über `Path.resolve().parents` ein (keine Code-Kopie).
   - Bricht bei fehlendem, inaktivem oder fremdem Lock fail-closed mit `WorkspaceLockError` ab, bevor ein Verzeichnis (`run_dir`) angelegt, ein Lockfile erstellt, eine Temp-Datei geschrieben oder das Inventar mutiert wird (strikte 0 Disk-I/O Garantie).
   - Auch [`cleanup_run_quarantine()`](skills/mail-desk/scripts/core/attachment_fetch.py) erzwingt die Workspace-Lock-Ownership vor dem Löschen.
4. **Kein überschreibender `os.replace`-Fallback in No-Clobber-Promotion:**
   - [`_atomic_no_clobber_promote()`](skills/mail-desk/scripts/core/attachment_fetch.py): Nutzt atomares `os.link()` (NTFS-Hardlink).
   - Wenn das Dateisystem keine atomaren Hardlinks unterstützt (`OSError`), bricht die Promotion fail-closed mit `RuntimeError` ab und löscht die Temp-Datei; es wird niemals `os.replace()` als Fallback ausgeführt, um Zieldateien unter Concurrent-Races unter keinen Umständen zu überschreiben.
5. **Quoteninventar fail-closed & geschützt vor Race-Conditions:**
   - [`_load_quarantine_inventory()`](skills/mail-desk/scripts/core/attachment_fetch.py): Beschädigtes JSON, Lesefehler oder schematisch ungültige Inventare werfen `QuarantineInventoryError`.
   - Fehlt `.quarantine-inventory.json` in einem bestehenden Run-Verzeichnis mit vorhandenen Dateien, bricht die Quotenprüfung fail-closed mit `QuarantineInventoryError` ab (kein stilles Zurücksetzen auf 0).
6. **Vollständige Sicherheitsvalidierung im `already_fetched`-Pfad:**
   - Auch bei einer bereits existierenden Datei mit identischem Hash werden Active-Content-Prüfung, gesperrte Endungen, MIME-/Endungsdrift und Quotenlimit vollständig durchlaufen.
   - Vorplatzierte Dateien werden ordnungsgemäß in `.quarantine-inventory.json` nachgetragen und verbleiben nicht außerhalb der Quotenüberwachung.
7. **Pfadprüfung bricht bei `lstat`-I/O-Fehlern fail-closed ab:**
   - [`check_quarantine_path_security()`](skills/mail-desk/scripts/core/attachment_fetch.py): Fehler von `os.lstat()` (z. B. `PermissionError`) werden nicht mehr ignoriert (`except OSError: pass`), sondern lösen `SymlinkEscapeError` aus.
8. **Freie Pfade & Windows-Gerätenamen vor I/O abgewiesen:**
   - [`validate_attachment_filename()`](skills/mail-desk/scripts/core/attachment_fetch.py): Freie Kandidatenpfade (`../../bericht.pdf`, `sub/folder.pdf`) und Windows-Gerätenamen einschließlich Erweiterung (`CON.txt`, `AUX.pdf`, `NUL.dat`, `COM1.doc`, etc.) werden vor jeglichem I/O mit `ValueError` abgewiesen (keine stille Bereinigung oder Ersetzung).
9. **Gesperrte Endungen (`transport.disallowed_extensions`) & OOXML-Inspektion:**
   - Auslesen aus Policy (`.docm`, `.xlsm`, `.pptm`, `.ps1`, `.bat`, etc.) blockiert fail-closed.
   - Echte OOXML-Paketanalyse (`[Content_Types].xml`, `word/document.xml`), Abweisung reiner ZIPs und Sperrung von Makro-Paketen (`vbaProject.bin`).
10. **Policy-Timeout (25s) & RFC-3339 Receipt:**
   - `download_timeout_seconds` an `himalaya.fetch_raw_message_eml` durchgereicht.
   - `"approved_at": "now"` abgewiesen; strikte RFC-3339 Zeitzonen-Prüfung und kanonisches versioniertes JSON-Hashing (`schema_version: 1`).

---

### 2. Geänderte Dateien

| Datei | Status | Verantwortung |
| --- | --- | --- |
| `skills/mail-desk/scripts/core/attachment_fetch.py` | Modifiziert | Quarantäne-Abruf, No-Clobber-Promotion, RFC-3339 Receipt, OOXML-Sniffing, Per-Message-Quoten, Lockfile, Fail-Closed Inventory, Dynamic Workspace-Lock Guard |
| `skills/mail-desk/scripts/mail_desk_himalaya_client.py` | Modifiziert | Manifest-Runner entfernt Lock-Parameter aus Manifestvertrag; erzwingt zwingend `allow_legacy=False` |
| `skills/mail-desk/tests/test_maildesk_attachments_mda2.py` | Modifiziert | 37 hermetische TDD-/Adversarial-Tests einschließlich Lock-Spoofing-/Bypass-Tests, Barrier-Races und Fail-Closed mit 0 I/O |

---

### 3. Verifikationsergebnisse & Nachweise

1. **Fokussierte Suite `MD-A2`:**
   ```powershell
   python -m unittest skills/mail-desk/tests/test_maildesk_attachments_mda2.py
   ```
   *Ergebnis:* **37 von 37 Tests OK (1.704s)**
   *Abdeckung:*
   - `test_compute_review_hash_is_deterministic`
   - `test_verify_approval_receipt_valid_and_drifts`
   - `test_attachment_fetch_success_hermetic`
   - `test_preflight_drift_fails_closed_before_io`
   - `test_path_traversal_and_win32_device_names_rejected`
   - `test_hash_drift_during_fetch_cleans_temp_and_aborts`
   - `test_active_content_and_extension_drift_blocked`
   - `test_single_and_cumulative_quota_enforced`
   - `test_idempotent_retry_and_collision_handling`
   - `test_execute_manifest_attachment_fetch`
   - `test_symlink_escape_rejected`
   - `test_fetch_timeout_fails_closed`
   - `test_manifest_partial_failure_handling`
   - `test_disallowed_extensions_loaded_and_blocked`
   - `test_mime_and_extension_drift_fails_closed`
   - `test_genuine_ooxml_verification`
   - `test_quarantine_parent_chain_security_and_reparse_point`
   - `test_atomic_no_clobber_promotion_race_and_collision`
   - `test_quotas_enforced_per_message_not_per_run`
   - `test_configured_25s_download_timeout_applied`
   - `test_approval_receipt_rfc3339_validation_and_versioned_json_hash`
   - `test_atomic_no_clobber_fails_closed_when_link_unsupported`
   - `test_quarantine_inventory_corrupted_and_missing_in_existing_run`
   - `test_concurrent_inventory_locking`
   - `test_already_fetched_executes_full_security_validation`
   - `test_lstat_inspection_os_error_fails_closed`
   - `test_free_candidate_paths_and_device_names_rejected_before_io`
   - `test_concurrent_fetches_serialized_count_quota_enforced`
   - `test_concurrent_fetches_serialized_size_quota_enforced`
   - `test_concurrent_fetches_same_file_idempotent`
   - `test_workspace_lock_missing_fails_closed_zero_io`
   - `test_workspace_lock_foreign_owner_fails_closed_zero_io`
   - `test_workspace_lock_owned_succeeds_and_cleanup_checks_lock`
   - `test_workspace_lock_dynamic_loader_locates_canonical_guard`
   - `test_manifest_injected_allow_legacy_and_lease_ignored_zero_io`
   - `test_manifest_spoofed_lease_rejected_when_foreign_lock_active_zero_io`

2. **Gesamte Mail-Desk-Testsuite:**
   ```powershell
   python -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
   ```
   *Ergebnis:* **351 von 351 Tests OK (11.257s)** — 0 Fehler, 0 Regressionen.

3. **Linter, Catalog-Validation & Git-Check:**
   - `python -m compileall -q skills/mail-desk` ➔ **0 Fehler (Exit 0)**
   - `python scripts/validate-skills-catalog.py` ➔ **Skills catalog validation passed (Exit 0)**
   - `git diff --check` ➔ **0 Whitespace-/Formatierungsfehler (Exit 0)**
   - `git status` ➔ **Ungestaged belassen (Review-Modus, kein Commit gemäß Vorgabe)**

---

## Paket: FR-08 / `MD-A3` — Begrenzte Extraktion und lokales OCR-Derivat

- **Status:** ✅ Abgeschlossen & Verifiziert
- **Scope:** Nur verifizierte Quarantänedateien; keine Mailboxmutation, kein Cloud-Write, kein LLM.

### 1. Zusammenfassung der Umsetzung

1. **Begrenzte Format-Extraktoren:**
   - [`core/attachment_extract.py`](skills/mail-desk/scripts/core/attachment_extract.py): `extract_attachment_text()` extrahiert strukturierten Text aus Quarantänedateien mit strikten Formatbudgets:
     - Plain Text / CSV / Markdown: bis 15.000 Zeichen.
     - PDF (`pymupdf`): maximal 10 Seiten (`MAX_PDF_PAGES`), Zeichenbudget 15.000 Zeichen.
     - DOCX (`python-docx`): maximal 40 Absätze (`MAX_DOCX_PARAGRAPHS`), Zeichenbudget 15.000 Zeichen.
     - XLSX (`openpyxl`): maximal 2 Sheets (`MAX_XLSX_SHEETS`), maximal 50 Zeilen (`MAX_XLSX_ROWS`), maximal 10 Spalten (`MAX_XLSX_COLS`), Zeichenbudget 15.000 Zeichen.
     - PPTX (`python-pptx`): maximal 15 Slides (`MAX_PPTX_SLIDES`), Zeichenbudget 15.000 Zeichen.
2. **Globales Zeichenlimit & Truncation:**
   - Jede Extraktion wird bei Erreichen von 15.000 Zeichen (`MAX_CHARS_PER_ATTACHMENT`) hart abgeschnitten (`[... Truncated at 15000 characters ...]`) und setzt das Flag `truncated: True`.
3. **OCR-Derivat-Isolation & Budget:**
   - Bei reinen Bild-PDFs (0 extrahierter Text) und aktiviertem `ocr_enabled=True` wird `_run_ocr_derivative()` aufgerufen.
   - **Original bleibt unberührt:** Das OCR-Ergebnis wird ausschließlich in ein isoliertes Derivat-Verzeichnis geschrieben (`derivatives/<filename>.ocr.pdf`). Die Quarantäne-Quelldatei bleibt byte-identisch unverändert (geprüft via SHA-256).
   - Budgets: maximal 3 Seiten (`MAX_OCR_PAGES`), OCR-Timeout von maximal 30 Sekunden (`OCR_TIMEOUT_SECONDS`).
4. **Prozess-Timeout & Fail-Closed-Fehlerbehandlung:**
   - Gesamt-Prozess-Timeout: 20 Sekunden (`PROCESS_TIMEOUT_SECONDS`).
   - Fehlende Bibliotheken, defekte Dateien oder nicht unterstützte Formate liefern strukturiert `status: "attachment_conversion_unavailable"` mit Fehlermeldung — niemals scheinbaren Erfolg oder leere Ausgaben ohne Fehlerkennzeichnung.

---

### 2. Geänderte und neue Dateien

| Datei | Status | Verantwortung |
| --- | --- | --- |
| `skills/mail-desk/scripts/core/attachment_extract.py` | Neu | Text-Extraktion, Formatbudgets (PDF/DOCX/XLSX/PPTX/Text), OCR-Derivat-Isolation, Timeouts |
| `skills/mail-desk/tests/test_maildesk_attachments_mda3.py` | Neu | 11 hermetische Tests für Formatbudgets, OCR-Derivat, Timeouts, Fail-Closed |

---

### 3. Verifikationsergebnisse & Nachweise

1. **Fokussierte Suite `MD-A3`:**
   ```powershell
   python -m unittest skills/mail-desk/tests/test_maildesk_attachments_mda3.py
   ```
   *Ergebnis:* **11 von 11 Tests OK (2.3s)**
   *Abdeckung:* Text/CSV, PDF-Seitenlimit (10 Seiten), OCR-Derivat-Isolation & -Budget (Original unberührt), DOCX-Absatzlimit (40), XLSX-Sheets/Zeilen/Spalten-Limits, PPTX-Slide-Limits (15), 15k-Zeichenlimit-Truncation, Gesamt-Timeout, Converter-Ausfall (fail-closed), defekte Dateien (fail-closed).

2. **Gesamte Mail-Desk-Testsuite:**
   ```powershell
   python -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
   ```
   *Ergebnis:* **301 von 301 Tests OK (25.8s)** — 0 Fehler, 0 Regressionen.

3. **Linter & Formatierungsprüfung:**
   - `python -m compileall -q skills/mail-desk` ➔ **0 Fehler (Exit 0)**
   - `python quick_validate.py skills/mail-desk` ➔ **Skill is valid! (Exit 0)**
   - `git diff --check` ➔ **0 Whitespace-/Formatierungsfehler (Exit 0)**

---

## Paket: FR-08 / `MD-A4` — Materialitäts-Gate und LLM-Handoff

- **Status:** ✅ Abgeschlossen & Verifiziert
- **Scope:** Nur Prompt- und Manifest-Vorbereitung; rein deklaratives Modul, kein LLM-Call, keine Cloud-Ablage, keine Mailbox-Mutation.

### 1. Zusammenfassung der Umsetzung

1. **Deklaratives Handoff-Modul (`core/attachment_handoff.py`):**
   - `build_attachment_analysis_handoff()` erstellt ein hashgebundenes Übergabe-Artefakt für nachgelagerte LLM-Prompts oder Manifest-Reviews.
   - Enforce strikte Budgets: 15.000 Zeichen je Anhang (`MAX_CHARS_PER_ATTACHMENT`) und 30.000 Zeichen je E-Mail kumulativ (`MAX_CHARS_PER_MAIL`).
   - Stabile deterministische Sortierung der MIME-Parts anhand des Part-Locators.
   - Truncation wird deterministisch durchgeführt, mit klaren Markern versehen (`[... Truncated at ...]`) und mit `truncated: true` gekennzeichnet.
2. **Prompt-Injection-Schutz & Kapselung:**
   - Textinhalte werden in `<untrusted_attachment_content part_locator="..." filename="..." sha256="..." mime_type="..." materiality="..." status="..." truncated="...">` gekapselt.
   - `escape_untrusted_content()` neutralisiert Breakout-Versuche (z. B. schließende XML-Tags wie `</untrusted_attachment_content>` oder gefälschte Tags) und entfernt Null-Bytes.
3. **Materialitäts-Matrix & Item-lokales Blocking:**
   - Zulässige Werte strikt: `supplementary` und `required_for_decision`; alle anderen Werte lösen fail-closed `InvalidMaterialityError` aus.
   - `supplementary`: Fehler bei der Konvertierung/Extraktion blockieren das Routing nicht. Das Item behält seine Zielordner-Zuweisung und der Fehler wird rein informativ dokumentiert.
   - `required_for_decision`: Schlägt die Extraktion eines als erforderlich markierten Anhangs fehl, wird **ausschließlich das betroffene Item in INBOX** gehalten (`action: {"type": "keep_in_folder", "target_folder": "INBOX"}`, `decision.review_required: true`, `decision.confidence: "low"`). Andere Items des Batches bleiben unbeeinflusst.
   - `needs_reply` wird vorab unabhängig ermittelt und bleibt durch das Handoff unter allen Bedingungen strikt unberührt.
4. **Deterministische Hash-Bindung:**
   - `compute_handoff_hash()` bindet Mailidentität (`account`, `message_id`, `folder`, `envelope_id`) und Anhangsmetadaten samt Inhalts-Hashes an einen 64-stelligen SHA-256 (`handoff_hash`).
5. **Classifier- & Batch-Runner-Integration:**
   - [`core/classifier.py`](skills/mail-desk/scripts/core/classifier.py): Wendet Handoff-Ergebnisse via `apply_attachment_handoff_to_item()` an.
   - [`references/batch-runner.md`](skills/mail-desk/references/batch-runner.md): Dokumentation der Materialitätsmatrix, Limits und Kapselungsregeln.

---

### 2. Geänderte und neue Dateien

| Datei | Status | Verantwortung |
| --- | --- | --- |
| `skills/mail-desk/scripts/core/attachment_handoff.py` | Neu | Materialitäts-Gate, Budgets (15k/30k), Injection-Escaping, Kapselung, Hash-Bindung |
| `skills/mail-desk/tests/test_maildesk_attachments_mda4.py` | Neu | 14 hermetische Tests für Materialitätswerte, Budgets, Truncation, Injection, Blocking |
| `skills/mail-desk/scripts/core/classifier.py` | Modifiziert | Handoff-Anwendung und Item-lokales Blocking im Klassifikations- und Draft-Pfad |
| `skills/mail-desk/references/batch-runner.md` | Modifiziert | Dokumentation von MD-A4 im Batch-Runner-Referenzdokument |

---

### 3. Verifikationsergebnisse & Nachweise

1. **Fokussierte Suite `MD-A4`:**
   ```powershell
   python -m unittest skills/mail-desk/tests/test_maildesk_attachments_mda4.py
   ```
   *Ergebnis:* **14 von 14 Tests OK (0.022s)**
   *Abdeckung:* Beide Materialitätswerte, ungültige Werte (`InvalidMaterialityError`), 15k-Einzelbudget, 30k-Mailbudget (kumulativ), stabile Part-Sortierung, Prompt-Injection-Schutz, `supplementary`-Toleranz, `required_for_decision`-Blocking in INBOX, Item-lokales Blocking im Batch, unverändertes `needs_reply`, deterministische Hash-Bindung, zero LLM / Zero-Subprocess.

2. **Gesamte Mail-Desk-Testsuite:**
   ```powershell
   python -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
   ```
   *Ergebnis:* **315 von 315 Tests OK (21.1s)** — 0 Fehler, 0 Regressionen.

3. **Linter & Formatierungsprüfung:**
   - `python -m compileall -q skills/mail-desk` ➔ **0 Fehler (Exit 0)**
   - `python quick_validate.py skills/mail-desk` ➔ **Skill is valid! (Exit 0)**
   - `git diff --check` ➔ **0 Whitespace-/Formatierungsfehler (Exit 0)**

---

## Paket: FR-08 / `MD-A5` — Katalog-/Filemap-gestützter Ablagevorschlag

- **Status:** ✅ Abgeschlossen & Verifiziert
- **Scope:** Nur read-only `attachment_filing_candidate`; kein Upload, kein Ordneranlegen, kein Filemap-Write, keine Katalogmutation.

### 1. Zusammenfassung der Umsetzung

1. **Reine Read-Only-Architektur (`core/attachment_filing.py`):**
   - `propose_attachment_filing()` erzeugt einen deklarativen Ablagekandidaten (`attachment_filing_candidate`) mit `promotion_status: "pending_human_review"`.
   - Schreibt niemals auf Dateisystem, Filemaps oder externe Cloud-Storages (verifiziert durch strikte Read-Only-Trap-Tests).
2. **Katalog- und Storage-Auflösung:**
   - Eindeutige Auflösung von Projekten, Topics, Subtopics und Events anhand von `projects.json` und `topics.json`.
   - **Event-Vererbungsregel:** Events erben Cloud-Storage ausschließlich von explizit katalogisierten Parent-Topics oder Subtopics. Fehlt eine solche explizite Deklaration, schlägt die Zuordnung mit `status: "not_configured"` fehl.
   - Mehrere Storages oder als Archiv/Read-only deklarierte Storages erfordern manuelle Freigabe (`status: "storage_review_required"`).
3. **Strikte Entscheidungs-Matrix:**
   - `not_configured`: Kein Cloud-Storage im Katalog deklariert oder kein expliziter Parent-Storage für Events.
   - `storage_review_required`: Mehrere Storages konfiguriert, Archiv-/Read-only-Storage deklariert, oder `filemap.json` fehlt/ist korrupt/ist stale (> 24 Stunden).
   - `directory_review_required`: Das vorgeschlagene Zielverzeichnis ist im Filemap-Inventar nicht als belegt/etabliert nachgewiesen.
   - `already_present`: Eine Datei mit identischem SHA-256 existiert bereits im Filemap-Inventar des Ziel-Storages (Deduplizierung).
   - `collision_detected`: Eine Datei mit identischem Namen aber abweichendem SHA-256 existiert bereits am Zielort.
   - `proposed`: Eindeutiger, kollisionsfreier und katalogbelegter Zielpfad ermittelt.
4. **Path-Traversal- & Injektionsschutz:**
   - Dateinamen und Zielverzeichnisse werden gegen Path-Traversal (`../`, `..\`, absolute Pfade, Null-Bytes) bereinigt und validiert.
5. **Deterministische Hash-Bindung:**
   - `compute_candidate_hash()` bindet alle Felder (Quelle, Destination, Filemap-Zeitstand, Dedupe-Status, Promotion-Status) an einen deterministischen 64-stelligen SHA-256 (`candidate_hash`).
6. **Batch-Runner-Dokumentation:**
   - [`references/batch-runner.md`](skills/mail-desk/references/batch-runner.md) um Abschnitt zu `core/attachment_filing.py` und der Matrix ergänzt.

---

### 2. Geänderte und neue Dateien

| Datei | Status | Verantwortung |
| --- | --- | --- |
| `skills/mail-desk/scripts/core/attachment_filing.py` | Neu | Read-Only Ablagevorschlag, Storage-/Filemap-Auflösung, Matrix-Evaluation, Hash-Bindung |
| `skills/mail-desk/tests/test_maildesk_attachments_mda5.py` | Neu | 13 hermetische Tests für Matrix, Vererbung, Stale Filemap, Kollision, Traversal, Schreibfallen |
| `skills/mail-desk/references/batch-runner.md` | Modifiziert | Dokumentation von MD-A5 im Batch-Runner-Referenzdokument |

---

### 3. Verifikationsergebnisse & Nachweise

1. **Fokussierte Suite `MD-A5`:**
   ```powershell
   python -m unittest skills/mail-desk/tests/test_maildesk_attachments_mda5.py
   ```
   *Ergebnis:* **13 von 13 Tests OK (0.030s)**
   *Abdeckung:* Project, Topic, Subtopic (explizit & vererbt), Event-Vererbungseinschränkung, EUCEN-Katalogauflösung, Mehrfach-/Archiv-Storages (`storage_review_required`), fehlende/stale Filemap (`storage_review_required`), unbelegtes Verzeichnis (`directory_review_required`), Traversal-/Injektionsabwehr, Deduplizierung (`already_present`), Kollision (`collision_detected`), deterministischer `candidate_hash`, Schreibfallen für Cloud/Katalog/Filemap (Zero-Mutation).

2. **Gesamte Mail-Desk-Testsuite:**
   ```powershell
   python -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
   ```
   *Ergebnis:* **328 von 328 Tests OK (19.4s)** — 0 Fehler, 0 Regressionen.

3. **Linter & Formatierungsprüfung:**
   - `python -m compileall -q skills/mail-desk` ➔ **0 Fehler (Exit 0)**
   - `python quick_validate.py skills/mail-desk` ➔ **Skill is valid! (Exit 0)**
   - `git diff --check` ➔ **0 Whitespace-/Formatierungsfehler (Exit 0)**

---

## Abschluss FR-08 & Vorbereitung FR-09: Human-gated Cloud-Promotion

> [!IMPORTANT]
> **FR-08 ist vollständig umgesetzt und verifiziert:**
> - `MD-A1` (MIME-Inventar) ✅
> - `MD-A2` (Quarantäne-Abruf) ✅
> - `MD-A3` (Begrenzte Extraktion & OCR-Derivat) ✅
> - `MD-A4` (Materialitäts-Gate & Handoff) ✅
> - `MD-A5` (Katalog-/Filemap-Ablagevorschlag) ✅
>
> **Human Gate vor FR-09:** Gemäß Spezifikation in `FEATURE-REQUESTS.md` startet `FR-09` (`MD-P1` bis `MD-P3`) erst nach eingefrorenem `attachment_filing_candidate`-Schema und ausdrücklicher Freigabe. Jede Mutation benötigt Workspace-Lock, frische Preconditions und eine hashgebundene Human-Receipt.
