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
| `MD-A4` — Materialitäts-Gate & LLM-Handoff | ✅ nachgebessert | Review (ungestaged) | 59 fokussierte / 421 Gesamt-Tests |
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

## Paket: FR-08 / `MD-A3` — Begrenzte Extraktion und lokales OCR-Derivat (Gehärtet)

- **Status:** ✅ Abgeschlossen & Verifiziert
- **Scope:** Nur verifizierte Quarantänedateien; keine Mailboxmutation, kein Cloud-Write, kein LLM.

### 1. Zusammenfassung der Umsetzung & Härtung

1. **Strikte MD-A2-Envelope- und Hash-Validierung (`validate_mda2_fetch_result`):**
   - Akzeptiert ausschließlich verifizierte MD-A2-Ergebnis-Envelopes mit Status `fetched` oder `already_fetched`.
   - `expected_sha256: str` ist ein **zwingender Pflichtparameter** von `extract_attachment_content` und `validate_mda2_fetch_result`.
   - `run_id` wird strikt gegen Path-Traversal, illegale Zeichen und Windows-Gerätenamen (`CON`, `PRN`, `AUX`, `NUL`, etc.) validiert (`is_valid_run_id`).
   - `relative_path` und `effective_mime_type` sind Pflichtfelder.
   - `fetch_sha256` und `inventory_sha256` müssen zwingend vorliegen, gültige 64-stellige Hex-Strings sein und exakt mit `expected_sha256` übereinstimmen.
   - **Pre-Extraction-Prüfung:** Der Hash der Quelldatei auf der Festplatte wird vor der Verarbeitung berechnet und gegen `fetch_sha256` geprüft; bei Abweichung bricht die Extraktion sofort fail-closed mit `HashDriftError` ab.

2. **Pfadcontainment, Symlink- und Windows-Reparse-Point-Schutz:**
   - Quelle und Derivate werden ausschließlich innerhalb des aktiven Run-Verzeichnisses `data/mail-desk/attachments/<run-id>/` aufgelöst.
   - Absolute Pfade, Windows-Laufwerksbuchstaben, `..`-Traversal-Sequenzen und Unterverzeichnisse innerhalb der Run-Quarantäne werden fail-closed mit `ValueError` abgewiesen.
   - **Unresolved Root Guard:** Symlink- und Junction-Checks (`check_quarantine_path_security`) prüfen die Pfadhierarchie bereits auf dem **unaufgelösten** `raw_attachments_root`, um Junction-Escapes vor der Auflösung zuverlässig zu erkennen.

3. **MIME- und Dateiendungs-Drift-Schutz (Fail-Closed vor Parseraufruf):**
   - Re-Sniffing der Quelldatei via `detect_mime_and_active_content()` und Re-Validierung gegen das MD-A2-Envelope (`validate_mime_and_extension`).
   - Abweichungen zwischen MD-A2 `effective_mime_type` und dem tatsächlichen Dateiinhalt lösen `MimeDriftError` aus.
   - Nicht zur Dateiendung passende MIME-Typen lösen `ExtensionMimeDriftError` aus.
   - `.docm`, `.xlsm`, `.pptm`, `.ps1`, `.bat`, `.cmd`, `.exe` und alle weiteren in der Policy konfigurierten `disallowed_extensions` werden vor jedem Parseraufruf abgewiesen (`DisallowedExtensionError`).
   - OOXML-Archive werden vorab auf `vbaProject.bin` und `macroEnabled` in `[Content_Types].xml` geprüft (`ActiveContentBlockedError`).

4. **Echter Prozess-Worker mit Win32 Job Object, Win64 ctypes-Signaturen, portablem POSIX-Handshake und Process-Tree-Terminierung:**
   - `run_with_timeout` führt Extraktions- und OCR-Workflows standardmäßig in einem isolierten `multiprocessing.Process`-Worker aus.
   - **Vollständige Win64-kompatible `ctypes`-Signaturen & Fail-Closed Guard (`_init_win32_signatures`, `_get_verified_kernel32`):**
     - Unter Windows besitzen alle Win32-APIs (`CreateJobObjectW`, `SetInformationJobObject`, `AssignProcessToJobObject`, `TerminateJobObject`, `OpenProcess`, `CloseHandle`, `GetExitCodeProcess`) explizit deklarierte `argtypes` und `restype`.
     - HANDLE-Rückgaben und -Parameter sind strikt als pointerbreite Typen (`wintypes.HANDLE` / `c_void_p`, 8 Bytes auf Win64) typisiert, wodurch 32-Bit-Truncation, Stack-Corruption oder Sign-Extension auf 64-Bit-Windows ausgeschlossen sind.
     - **Kein unsicherer Win32-Fallback:** Fehlgeschlagene oder fehlende Signaturinitialisierung speichert den Fehler in `_win32_init_error`. `_get_verified_kernel32()` bricht fail-closed mit `RuntimeError` ab; ein Rückgriff auf unkonfiguriertes `ctypes.windll.kernel32` ist in allen Funktionen (`WindowsJobObject`, `is_process_alive`) vollständig eliminiert.
   - **Strikter 2-Phasen-Handshake (Windows & POSIX):**
     - Der Worker startet, etabliert seine Confinement-Grenze und meldet `"READY"` über eine Duplex-Pipe; er blockiert zwingend, bis der übergeordnete Prozess die Isolation verifiziert und autorisiert hat.
     - **Windows Confinement:** Das Job Object wird mit `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000)` konfiguriert und der Worker-PID zugewiesen. Schlägt `CreateJobObjectW`, `SetInformationJobObject`, `AssignProcessToJobObject` oder die Signaturverifikation fehl, terminiert der Parent den Worker sofort via `terminate_process_tree`, schließt die Pipe und bricht fail-closed mit `RuntimeError` ab — **bevor der Worker jemals die Nutzfunktion ausführt**.
     - **POSIX Confinement:** Der Worker ruft `os.setpgid(0, 0)` auf, um seine eigene Prozessgruppe zu etablieren. Schlägt `setpgid` fehl, bricht der Worker strukturiert ab, meldet einen Fehler und sendet niemals `"READY"`.
     - **Parent PGID-Verifikation:** Unter POSIX verifiziert der Parent vor dem Senden von `"START"` zwingend `os.getpgid(proc.pid) == proc.pid`. Bei Abweichung oder Fehler terminiert der Parent den Worker **strikt einzeln** (`_terminate_single_process(proc)`), tötet **niemals eine unbestätigte oder fremde Prozessgruppe** und bricht fail-closed mit `RuntimeError` ab.
     - **Tree-Kill mit Leader-Guard (`terminate_process_tree`):** Auch bei Timeouts prüft `terminate_process_tree` unter POSIX vor jedem `os.killpg(pid, SIGKILL)` nachweislich `pgid == pid`. Ist der Prozess nicht Gruppenführer, wird nur der Einzelprozess beendet.
   - Erst nach nachweislich erfolgreicher Confinement-Verifikation sendet der Parent das `"START"`-Signal. Erst danach führt der Worker die eigentliche Nutzfunktion aus.
   - **Kein stiller Thread-Fallback im Produktionspfad:** `allow_thread_fallback=False` ist im Produktionspfad strikt erzwungen. Unpicklbare Objekte lösen fail-closed eine `RuntimeError`-Exception aus.
   - **Hermetische Dependency Injection für Tests:** `extract_attachment_content` akzeptiert optionale module-level picklbare Runner (`_ocr_runner`, `_lock_verifier`), um Tests unter 100% echter Prozessisolation auszuführen.

5. **Parent-Owned Temp-Derivate & Sibling-Sicheres Zero-Leakage-Cleanup:**
   - Der übergeordnete Prozess (`extract_attachment_content`) bestimmt den invocationsspezifischen temporären Derivatpfad deterministisch vorab (`.{target_file.stem}.ocr.{invocation_id}.tmp`).
   - Bei Timeout, Abbruch oder Nichterreichen des `extracted`-Status räumt der übergeordnete Prozess **ausschließlich** den parent-owned, invocationsspezifischen Pfad ab (`_cleanup_temp_artifacts(known_temp_deriv_path)`).
   - **Kein Globbing von Geschwisterdateien:** Das Cleanup entfernt niemals Sibling-Temp-Dateien (`.{file_stem}.ocr.*.tmp`) desselben Dateistamms; aktive Temp-Dateien paralleler Aufrufe unter derselben Lock-Ownership bleiben vollständig intakt und geschützt.
   - Keine vorzeitige Verzeichniserstellung: Das `derivatives`-Verzeichnis wird erst nach erfolgreicher Workspace-Lock-Prüfung bei tatsächlichem OCR-Bedarf angelegt.

6. **Zweistufiger Workspace-Lock-Check & Derivat-Isolation:**
   - Workspace-Lock wird **zweistufig** erzwungen: unmittelbar vor der Erstellung temporärer Derivatdateien (`.tmp`) UND unmittelbar vor der atomaren Promotion (`_atomic_no_clobber_promote`). Ein Lock-Verlust während der OCR-Phase bricht den Vorgang fail-closed ab und bereinigt die Temp-Dateien.
   - OCR-Derivate werden ausschließlich in `derivatives/<filename>.ocr.pdf` über temporäre Geschwisterdateien (`.tmp`) geschrieben und via `_atomic_no_clobber_promote` atomar verlinkt. Bei abweichendem Hash wird `QuarantineCollisionError` ausgelöst.
   - Bei Post-OCR Source-Hash-Drift wird ein bestehendes Derivat nur dann gelöscht, wenn es in diesem Aufruf neu erzeugt wurde (`was_newly_created`), um idempotente bestehende Zieldateien nicht zu zerstören.

7. **Gemischte PDFs mit korrekter Seitenadressierung:**
   - Digitale Seiten werden nativ extrahiert.
   - Bildbasierte Seiten werden bis zum OCR-Budget (`max_ocr_pages`, Default 3) OCR-verarbeitet.
   - **0-basierte Adressierung:** OCR-Seiten im Volltext-Derivat werden mit `p_num - 1` adressiert, sodass Bildseiten an beliebigen Positionen (z. B. Seite 3) exakt aus dem OCR-Dokument gelesen werden.
   - Bei Überschreiten des Budgets wird `[Page X: image page skipped - OCR page limit of 3 exceeded]` eingefügt und `truncation_reason: "ocr_page_limit_exceeded"` gesetzt.
   - Bei Ausfall des OCR-Tools bleiben native Textseiten erhalten (`quality: "partial"`, `truncation_reason: "ocr_unavailable"`).

8. **In-Flight Memory- und Format-Budgets:**
   - Text- und Zeichenbegrenzungen (15.000 Zeichen `max_chars_per_attachment`) werden in Office-Parsern (`python-docx`, `openpyxl`, `python-pptx`) und `MarkItDown` bereits während der zeilen-/absatzweisen Verarbeitung erzwungen.
   - DOCX: max. 40 Absätze (`max_docx_paragraphs`).
   - XLSX: max. 2 Sheets (`max_xlsx_sheets`), max. 50 Zeilen (`max_xlsx_rows`), max. 10 Spalten (`max_xlsx_cols`).
   - PPTX: max. 15 Folien (`max_pptx_slides`).
   - PDF: max. 10 Seiten (`max_pdf_pages`), max. 3 OCR-Seiten (`max_ocr_pages`).

9. **Echte Tool-Versionen & Ausgabe-Transparenz:**
   - Echte Versionsausgabe via Modul-Inspektion (`pymupdf`, `python-docx`, `openpyxl`, `python-pptx`, `ocrmypdf`, `markitdown`).
   - Vollständiger Output-Kontrakt mit `source_sha256`, `derivative_sha256`, `derivative_relative_path`, `method`, `tool`, `tool_version`, `quality`, `scope`, `truncation_reason`, `character_count`, `text` und `error`.

---

### 2. Geänderte und neue Dateien

| Datei | Status | Verantwortung |
| --- | --- | --- |
| `skills/mail-desk/scripts/core/attachment_extract.py` | Gehärtet | Bounded Extraction, Win64 pointerbreite `ctypes`-Signaturen (`_init_win32_signatures`), strikt fail-closed `_get_verified_kernel32()` ohne unsicheren `ctypes.windll.kernel32`-Fallback, Win32 Job Object Confinement, portabler POSIX `setpgid`/PGID-Handshake mit Leader-Guard, Process-Tree-Terminierung ohne Tötung fremder Gruppen (`_terminate_single_process`), Parent-Owned Temp-Derivate (`_cleanup_temp_artifacts`), Fail-Closed Prozessisolation ohne silenten Thread-Fallback, strikte MD-A2-Envelope-Prüfung (`expected_sha256` Pflicht), MIME-/Extension-Drift-Guard, Pfadcontainment (unresolved root), zweistufiger Workspace-Lock, Immutabilitätsprüfungen, Mixed-PDF-Page-Mapping (`p_num - 1`), In-Flight-Budgets, echte Toolversionen |
| `skills/mail-desk/scripts/core/attachment_fetch.py` | Angepasst | `detect_mime_and_active_content`: Erkennung von `text/csv` und `application/msword` (CFBF magic) zur Vermeidung falscher MIME-Drifts |
| `skills/mail-desk/tests/test_maildesk_attachments_mda3.py` | Erweitert | 36 hermetische TDD- und Adversarial-Tests (inkl. Fail-Closed-Verhalten bei fehlgeschlagener Win32-Signaturinitialisierung ohne Payload-Ausführung und ohne Zugriff auf unkonfiguriertes `windll.kernel32`, POSIX `setpgid`-Fehler und PGID-Mismatch ohne Payload-Ausführung, Win64 pointer-wide ctypes-Signaturen, Handshake-Fail-Closed-Confinement bei Win32 Job Object Fehlern, Sibling-Temp-Erhalt ohne Globbing, Grandchild-Prozessbaum-Kill, Late-Write-Prevention, Zero-Leakage-Temp-Cleanup, Pfadescape, Traversal, Root-Junction, Envelope-Gaps, Hash-Drift, MIME-Drift, Makroformate, 20/30s-Timeouts, Digital/Image/Mixed-PDFs, zweistufiger Lockverlust vor Promotion, Derivat-Race, bestehendes Derivat-Preservation, partielles OCR-Cleanup, unberührtes Original, Missing Tools) |

---

### 3. Verifikationsergebnisse & Nachweise

1. **Fokussierte Suite `MD-A3`:**
   ```powershell
   python -m unittest -v skills/mail-desk/tests/test_maildesk_attachments_mda3.py
   ```
   *Ergebnis:* **36 von 36 Tests OK (30.567s)**
   *Abdeckung (alle 36 Tests grün):*
   - `test_extract_digital_pdf_within_limit`
   - `test_extract_digital_pdf_exceeding_10_pages_truncated`
   - `test_character_budget_15000_chars_truncated`
   - `test_extract_plain_text_and_csv`
   - `test_extract_docx_within_and_exceeding_paragraph_limit`
   - `test_extract_xlsx_sheet_and_grid_limit`
   - `test_extract_pptx_slide_limit`
   - `test_extract_image_pdf_ocr_local_derivative_preserves_original`
   - `test_extract_mixed_pdf_extracts_digital_and_ocrs_image_pages`
   - `test_extract_mixed_pdf_exceeding_ocr_budget_marked_truncated`
   - `test_path_escapes_and_traversal_rejected_fail_closed`
   - `test_unsafe_run_id_rejected_fail_closed`
   - `test_mda2_envelope_integrity_and_hash_drift_fail_closed`
   - `test_macro_formats_and_active_content_rejected_before_parsers`
   - `test_process_worker_terminates_and_prevents_late_write`
   - `test_process_timeout_enforced_and_aborts_cleanly`
   - `test_ocr_timeout_enforced_and_cleans_temporary_artifacts`
   - `test_workspace_lock_missing_fails_closed_zero_mutation`
   - `test_workspace_lock_loss_immediately_before_promotion_aborts`
   - `test_partial_ocr_cleanup_on_error_and_source_immutability`
   - `test_derivative_collision_race_fails_closed_no_clobber`
   - `test_derivative_already_exists_same_hash_idempotent`
   - `test_existing_idempotent_derivative_preserved_on_source_drift`
   - `test_mime_drift_against_mda2_envelope_fails_closed`
   - `test_attachments_root_junction_fails_closed_before_resolve`
   - `test_mixed_pdf_with_image_page_not_page_one`
   - `test_mixed_pdf_ocr_failure_preserves_native_text`
   - `test_corrupt_file_handled_gracefully`
   - `test_missing_tool_returns_attachment_conversion_unavailable`
   - `test_process_worker_tree_kill_terminates_grandchild_process_and_prevents_late_write`
   - `test_timeout_cleans_partially_written_temp_derivative_zero_leakage`
   - `test_job_confinement_failure_aborts_fail_closed_without_executing_payload`
   - `test_cleanup_removes_only_own_invocation_temp_path_preserving_sibling`
   - `test_posix_confinement_setpgid_failure_and_pgid_mismatch_without_executing_payload`
   - `test_win32_ctypes_signatures_64bit_pointer_width`
   - `test_failed_win32_signature_init_fails_closed_without_payload_or_untyped_api`

2. **Gesamte Mail-Desk-Testsuite:**
   ```powershell
   python -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
   ```
   *Ergebnis:* **376 von 376 Tests OK (43.092s)** — 0 Fehler, 0 Regressionen.

3. **Linter, Catalog-Validation & Git-Check:**
   - `python -m compileall -q skills/mail-desk` ➔ **0 Fehler (Exit 0)**
   - `python scripts/validate-skills-catalog.py` (aus Skills-Root) ➔ **Skills catalog validation passed (Exit 0)**
   - `git diff --check` ➔ **0 Whitespace-/Formatierungsfehler (Exit 0)**
   - `git status` ➔ **Ungestaged belassen (Review-Modus, kein Commit gemäß Vorgabe)**

---

## Paket: FR-08 / `MD-A4` — Materialitäts-Gate und LLM-Handoff (Gehärtet)

- **Status:** ✅ Abgeschlossen & Verifiziert
- **Scope:** Nur Prompt- und Manifest-Vorbereitung; rein deklaratives Modul, kein LLM-Call, keine Cloud-Ablage, keine Mailbox-Mutation.

#### 1. Zusammenfassung der Umsetzung & Härtung

1. **Deklaratives Handoff-Modul (`core/attachment_handoff.py`):**
   - `build_attachment_analysis_handoff()` erstellt ein hashgebundenes Übergabe-Artefakt für nachgelagerte LLM-Prompts oder Manifest-Reviews.
   - **Exakte Zeichenbudgets inklusive Marker:** 15.000 Zeichen je Anhang (`MAX_CHARS_PER_ATTACHMENT`) und 30.000 Zeichen je E-Mail kumulativ (`MAX_CHARS_PER_MAIL`) — **strikt inklusive** des sichtbaren Truncation-Markers `[... Truncated at ... chars ...]`. Ist das kumulative Budget erschöpft, erhalten weitere Anhänge `char_count = 0` und leeren Text.
   - Stabile deterministische Sortierung der MIME-Parts anhand des Part-Locators.
   - Truncation wird deterministisch durchgeführt, mit klaren Markern versehen und mit `truncated: true` gekennzeichnet.

2. **Kanonische MD-A3-Envelope-Validierung (`validate_mda3_extraction_envelope`):**
   - **Vollständige MD-A3-Vertragskompatibilität:** Direkte Ausrichtung an den tatsächlich von MD-A3 emittierten Werten. Qualitäts-Taxonomie umfasst `{"high", "medium", "mixed", "partial", "low"}` (inkl. `medium` für lokale OCR-Derivate gescannter PDFs). Truncation-Taxonomie umfasst `{"max_pages_exceeded", "ocr_page_limit_exceeded", "ocr_unavailable", "max_paragraphs_exceeded", "grid_limit_exceeded", "max_slides_exceeded", "max_chars_exceeded", "timeout_exceeded"}`. Parallele Taxonomien wurden vollständig eliminiert.
   - **Strikte Part-Locator-, Dateinamen- und MIME-Validierung:** Part-Locator muss zwingend ein gültiger RFC-822 Part-Locator sein (`^\d+(?:\.\d+)*$`, z. B. `1`, `2`, `1.1`). Dateinamen dürfen weder leer noch `unknown_attachment` sein und dürfen keine Null-Bytes enthalten. MIME-Types müssen zwingend syntaktisch valide und normalisiert sein.
    - **Kanonische MD-A1/A2-Bestandsbindung & Trust Boundary (`canonical_parts`):**
      - Für jedes nichtleere Attachment-Handoff müssen `canonical_parts` zwingend als separat vertrauenswürdig gebundener Parameter vom Aufrufer kommen. `att.canonical_part` und `handoff.canonical_parts` dürfen niemals als Validierungsanker dienen.
      - Eingebettete `canonical_parts` in vorgebauten Handoffs dienen rein als gehashte Evidenz und werden 1-zu-1 gegen das externe Aufrufer-Inventar verifiziert (`HandoffDriftError` bei Mismatch, zusätzlichen oder fehlenden Teilen).
      - Jeder externe Part erfordert eindeutige Locators (`^\d+(?:\.\d+)*$`, Duplikate werden fail-closed mit `AttachmentHandoffError` abgewiesen), nicht-leere Dateinamen (keine Null-Bytes), 64-Hex SHA-256, normalisierte MIME-Types und exakte Provenienz `rfc822_mime_inspection`.
    - Erzwingt kanonisches `source_sha256` (64-stelliges Hex) und prüft Konsistenz mit eventuellem `sha256`.

3. **Nutzbarkeitskriterium & Item-lokales Blocking:**
   - Als nutzbar (`is_usable_extraction`) gilt ausschließlich `status == "extracted"`, ohne Fehler, mit nicht-leerem Text und ohne partielle Qualität (`quality != "partial"`).
   - `supplementary`: Fehler bei der Konvertierung/Extraktion blockieren das Routing nicht. Das Item behält seine Zielordner-Zuweisung und der Fehler wird rein informativ dokumentiert.
   - `required_for_decision`: Schlägt die Extraktion eines als erforderlich markierten Anhangs fehl, liegt eine Teil-Extraktion vor (`quality == "partial"`) oder ist der Status nicht `extracted`, wird **ausschließlich das betroffene Item in INBOX** gehalten (`action: {"type": "keep_in_folder", "target_folder": "INBOX"}`, `decision.review_required: true`, `decision.confidence: "low"`), **unabhängig von eventuellem Resttext**. Andere Items des Batches bleiben unbeeinflusst.
   - `needs_reply` wird vorab unabhängig ermittelt und bleibt durch das Handoff unter allen Bedingungen strikt unberührt.

4. **Prompt-Injection-Schutz & Kapselung:**
   - Textinhalte werden in `<untrusted_attachment_content part_locator="..." filename="..." source_sha256="..." mime_type="..." materiality="..." status="..." truncated="...">` gekapselt.
   - `escape_untrusted_content()` neutralisiert Breakout-Versuche (z. B. schließende XML-Tags wie `</untrusted_attachment_content>` oder gefälschte Tags) und entfernt Null-Bytes.

5. **Deterministische Decision- & Mail-Hash-Bindung:**
   - `compute_handoff_hash()` bindet die vollständige normalisierte Mail-Identität (`account`, `message_id`, `folder`, `envelope_id`), den normalisierten `decision_snapshot` (inklusive aller kataloggestützten Unterentscheidungen wie `workpackage`, `task`, `deliverable`, `milestone`, `subtopic`, `operation` und `event`) und alle sicherheitsrelevanten Anhangsdaten hashgebunden an einen 64-stelligen SHA-256 (`handoff_hash`), um nachträgliche Decision- oder Routing-Drifts zuverlässig zu erkennen und zu verhindern.
   - **Vollständige Mailidentitäts-Erzwingung & Bypass-Schutz:** Bei nicht-leeren Anhängen müssen sowohl beim Builder als auch beim Validator alle 4 Identitätsfelder (`account`, `message_id`, `folder`, `envelope_id`) vollständig und nicht-leer übergeben werden. Die Driftprüfung vergleicht alle 4 Felder bedingungslos; ein Auslassen von Feldern führt fail-closed zum Abbruch (`AttachmentHandoffError` bzw. `HandoffDriftError`) und kann Drift-Prüfungen niemals umgehen.

6. **Classifier-Re-Validierung & Fail-Closed Durchsetzung (`validate_attachment_handoff`, `classify_email`):**
   - Ein im Input bereits vorliegendes `attachment_analysis_handoff` wird in `classifier.py` (`classify_email`) vor der Verwendung kanonisch re-validiert.
   - **Kryptografischer Hash-Integritätscheck:** Der gekapselte Textpayload wird direkt aus dem `xml_block` extrahiert; sein SHA-256 wird berechnet und strikt gegen `content_hash` validiert. Zudem wird geprüft, dass `char_count` exakt der Länge des extrahierten Texts entspricht und keine unescapeten Breakout-Tags vorliegen.
   - **Byte-für-Byte Rekonstruktion:** `xml_block` wird aus den validierten Feldern des Items kanonisch rekonstruiert und muss exakt mit dem übergebenen `xml_block` übereinstimmen. Ebenso wird `prompt_content` aus allen `xml_block`-Bestandteilen rekonstruiert und muss Byte für Byte mit `prompt_content` übereinstimmen.
   - **Classifier Fail-Closed Durchsetzung:**
     - Liegen `attachment_extractions` ohne verifizierte `bound_attachments` aus dem Mail-Inventar vor, fällt die E-Mail fail-closed in Review in INBOX (`review_reason: "untrusted_attachment_extractions_without_inventory"`), ohne Handoff-Anwendung. `needs_reply` bleibt unberührt.
     - Liegt ein vorgebautes `attachment_analysis_handoff` mit Items ohne verifizierte `bound_attachments` vor, wird fail-closed eine Vertragsverletzung aufgeworfen (`HandoffDriftError`).
   - Bei manipulierten Hashes, verfälschten Items, manipuliertem XML-/Prompt-Inhalt, Mail-Identity-, Decision- oder MD-A1/A2-Bestands-Drift bricht die Klassifikation fail-closed mit `HandoffDriftError` ab.

---

### 2. Geänderte und neue Dateien

| Datei | Status | Verantwortung |
| --- | --- | --- |
| `skills/mail-desk/scripts/core/attachment_handoff.py` | Gehärtet | Kanonische MD-A3 Envelope-Validierung (Qualitäten inkl. `medium`, reale Truncation-Reasons), strikte RFC-822 Part-Locator-, Filename- und MIME-Typ-Validierung, MD-A1/A2-Bestandsbindung (`verify_item_against_canonical_parts`), Schließen der Trust-Boundary (`canonical_parts` Pflicht bei nichtleeren Handoffs, kein Vertrauen in `att.canonical_part`/`handoff.canonical_parts`), Nutzbarkeitsgate (`is_usable_extraction`), Decision-Snapshot-Bindung, exakte Marker-Budgetierung, Prompt-Injection-Schutz, kryptografische Re-Validierung mit Byte-für-Byte `xml_block`- und `prompt_content`-Rekonstruktion |
| `skills/mail-desk/scripts/core/classifier.py` | Gehärtet | Bindung von `bound_attachments` als `canonical_parts` in `classify_email`, Fail-Closed in Review in INBOX bei Extraktionen ohne Inventar (`untrusted_attachment_extractions_without_inventory`), Fail-Closed `HandoffDriftError` bei vorgebauten Handoffs ohne Inventar, Re-Validierung vorab gelieferter Handoff-Artefakte gegen Mail-Identität, Decision-Snapshot und kanonische Anhänge (`validate_attachment_handoff`) |
| `skills/mail-desk/tests/test_maildesk_attachments_mda4.py` | Erweitert | 59 hermetische TDD- und Adversarial-Tests (inkl. aller MD-A3 Qualitäten und Truncation-Gründe, repräsentativer Envelopes für PDF, DOCX, PPTX, XLSX, ungültiger Locators/Dateinamen/MIME-Types, fehlendem externem Inventar, selbst mitgebrachtem `canonical_part`, gefälschtem eingebettetem Handoff-Inventar, doppelten Locators im externen Inventar, erfolgreichem extern gebundenem Pfad, Classifier-Extraktionen ohne Inventar, Classifier-Handoff ohne Inventar, manipuliertem `content_hash`, manipuliertem `xml_block`-Text und -Attributen, strukturell defektem XML, manipuliertem `prompt_content`, `char_count`-Drift, unberührtem `needs_reply`, exakter Marker-Budgetgrenzen, Classifier-Integration, aller 7 Katalog-Unterentscheidungs-Skalare und strikter Mailidentitätsprüfung ohne Auslassungs-Bypass) |
| `skills/mail-desk/references/batch-runner.md` | Aktualisiert | Dokumentation des gehärteten MD-A4-Vertrags, der geschlossenen Trust-Boundary, der MD-A1/A2-Bestandsbindung, der kryptografischen Integritätsprüfung, der Classifier Fail-Closed-Regeln und des Nutzbarkeitskriteriums |

---

### 3. Verifikationsergebnisse & Nachweise

1. **Fokussierte Suite `MD-A4`:**
   ```powershell
   python -m unittest -v skills/mail-desk/tests/test_maildesk_attachments_mda4.py
   ```
   *Ergebnis:* **59 von 59 Tests OK (0.046s)**
   *Abdeckung (alle 59 Tests grün):*
   - `test_both_valid_materiality_values`
   - `test_invalid_materiality_rejected`
   - `test_budget_per_file_15k_characters`
   - `test_exact_budget_limits_including_marker_per_file`
   - `test_budget_cumulative_per_mail_30k_characters`
   - `test_exact_budget_limits_including_marker_cumulative_mail`
   - `test_exact_budget_exhausted_cumulative_mail_produces_empty_content`
   - `test_stable_sorting_by_part_locator`
   - `test_prompt_injection_protection`
   - `test_missing_extraction_supplementary_does_not_block`
   - `test_missing_extraction_required_blocks_item_in_inbox`
   - `test_required_for_decision_blocks_on_partial_extraction_despite_residual_text`
   - `test_required_for_decision_blocks_on_non_extracted_status_despite_residual_text`
   - `test_item_local_blocking_in_batch`
   - `test_unaltered_needs_reply_preservation`
   - `test_no_llm_or_external_tool_execution`
   - `test_mda3_envelope_accepts_all_canonical_qualities`
   - `test_mda3_envelope_accepts_all_canonical_truncation_reasons`
   - `test_representative_mda3_envelopes_for_all_document_types`
   - `test_mda3_envelope_rejects_missing_and_invented_hashes`
   - `test_mda3_envelope_detects_hash_drift`
   - `test_mda3_envelope_rejects_arbitrary_invented_status_values`
   - `test_mda3_envelope_rejects_invalid_quality_or_truncation_reason`
   - `test_mda3_envelope_rejects_invalid_part_locators`
   - `test_mda3_envelope_rejects_missing_empty_or_unknown_attachment_filenames`
   - `test_mda3_envelope_rejects_invalid_mime_types`
   - `test_canonical_parts_binding_success`
   - `test_canonical_parts_binding_detects_locator_drift`
   - `test_canonical_parts_binding_detects_filename_drift`
   - `test_canonical_parts_binding_detects_hash_drift`
   - `test_canonical_parts_binding_detects_mime_drift`
   - `test_canonical_parts_binding_detects_invalid_provenance`
   - `test_validate_handoff_rejects_manipulated_content_hash`
   - `test_validate_handoff_rejects_tampered_text_inside_xml_block`
   - `test_validate_handoff_rejects_tampered_xml_block_attributes`
   - `test_validate_handoff_rejects_malformed_xml_block_structure`
   - `test_validate_handoff_rejects_tampered_prompt_content`
   - `test_validate_handoff_rejects_char_count_mismatch`
   - `test_hash_binding_determinism`
   - `test_decision_snapshot_binding_in_handoff_hash`
   - `test_validate_attachment_handoff_detects_mail_identity_drift`
   - `test_validate_attachment_handoff_detects_decision_drift`
   - `test_validate_attachment_handoff_detects_hash_tampering_and_item_forgery`
   - `test_classifier_integration_required_attachment_failure_blocks_in_inbox`
   - `test_classifier_integration_supplementary_failure_does_not_block`
   - `test_classifier_rejects_manipulated_presubmitted_handoff`
   - `test_classifier_accepts_valid_matching_presubmitted_handoff`
   - `test_classifier_rejects_presubmitted_handoff_with_tampered_xml_block`
   - `test_classifier_enforces_canonical_parts_binding`
   - `test_missing_external_inventory_rejected`
   - `test_self_supplied_canonical_part_on_item_does_not_protect`
   - `test_forged_embedded_handoff_inventory_detected`
   - `test_duplicate_locators_in_external_inventory_rejected`
   - `test_successful_externally_bound_path`
   - `test_classifier_extractions_without_inventory_fails_closed_to_review`
   - `test_classifier_prebuilt_handoff_with_items_without_inventory_raises_drift_error`
   - `test_catalog_decision_scalars_bound_in_hash_and_drift`
   - `test_missing_individual_mail_identity_fields_fail_closed`
   - `test_empty_validator_input_cannot_bypass_identity_drift`

2. **Gesamte Mail-Desk-Testsuite:**
   ```powershell
   python -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
   ```
   *Ergebnis:* **421 von 421 Tests OK (48.170s)** — 0 Fehler, 0 Regressionen.

3. **Linter, Catalog-Validation & Git-Check:**
   - `python -m compileall -q skills/mail-desk` ➔ **0 Fehler (Exit 0)**
   - `python scripts/validate-skills-catalog.py` (aus Skills-Root) ➔ **Skills catalog validation passed (Exit 0)**
   - `git diff --check` ➔ **0 Whitespace-/Formatierungsfehler (Exit 0)**
   - `git status` ➔ **Ungestaged belassen (Review-Modus, kein Commit gemäß Vorgabe)**

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
