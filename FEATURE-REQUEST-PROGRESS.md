# Feature Request Progress & Code Review

> [!NOTE]
> **Ephemere Arbeitsdatei für den Implementierungsagenten und das Code-Review.**  
> Dokumentiert den aktuellen Durchführungsstand, Mini-Walkthroughs, Diffs und Verifikationsnachweise der aktiven Pakete. Diese Datei wird nach erfolgreicher Abnahme bereinigt bzw. auf das nächste Paket umgestellt.

---

## Übersicht FR-08: Paketstatus

| Paket | Status | Commit | Verifikation |
| --- | --- | --- | --- |
| `MD-A1` — Read-only MIME-Inventar | ✅ abgeschlossen | `6f86c67` | 46 fokussierte / 276 Gesamt-Tests |
| `MD-A2` — Reviewgebundener Quarantäne-Abruf | ✅ abgeschlossen | `bc4d125` | 14 fokussierte / 290 Gesamt-Tests |
| `MD-A3` — Begrenzte Extraktion & OCR-Derivat | ✅ abgeschlossen | `8bb87b5` | 11 fokussierte / 301 Gesamt-Tests |
| `MD-A4` — Materialitäts-Gate & LLM-Handoff | ✅ abgeschlossen | `a50652f` | 14 fokussierte / 315 Gesamt-Tests |
| `MD-A5` — Ablagevorschlag | ✅ abgeschlossen | `286e238` | 13 fokussierte / 328 Gesamt-Tests |

---

## Paket: FR-08 / `MD-A2` — Reviewgebundener Abruf in Temp-Quarantäne

- **Status:** ✅ Abgeschlossen & Verifiziert
- **Scope:** Nur Transport und lokale Quarantäne; keine Extraktion, kein LLM, kein Cloud-Vorschlag, keine Mailboxmutation.

### 1. Zusammenfassung der Umsetzung

1. **Approval-Receipt & Deterministischer Review-Hash:**
   - [`core/attachment_fetch.py`](skills/mail-desk/scripts/core/attachment_fetch.py): `compute_review_hash()` berechnet einen deterministischen 64-stelligen SHA-256-Hash über die kanonischen Request-Parameter (`account|message_id|folder|envelope_id|part_locator|inventory_sha256`).
   - `verify_approval_receipt()` verifiziert die Existenz und Pflichtfelder (`receipt_id`, `request_hash`, `approved_at`) und stellt sicher, dass `request_hash` exakt dem `review_hash` entspricht. Abweichung löst fail-closed `ReceiptDriftError` aus, Fehlen löst `ApprovalReceiptMissingError` aus.
2. **Preflight-Drift-Guard vor jeglichem I/O:**
   - Vor jeglichem Dateisystem- oder Mailboxzugriff werden Account, Folder, Envelope-ID, Message-ID, Part-Locator und Inventar-Hash gegen den MD-A1-Kandidaten verifiziert.
   - Bei Mismatch bricht die Operation sofort mit spezifischen Exceptions (`AccountDriftError`, `LocationDriftError`, `MessageIdDriftError`, `PartLocatorDriftError`, `HashDriftError`) ab. Es werden weder Verzeichnisse angelegt noch externe Calls getätigt.
3. **Quarantäne-Isolation & Pfadsicherheit:**
   - Zielpfad ist strikt `data/mail-desk/attachments/<run-id>/<clean_filename>`.
   - `is_valid_run_id()` blockiert Path-Traversal (`../`, Slashes), überlange Strings und Win32-Gerätenamen (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).
   - Symlink-/Reparse-Point-Prüfung verhindert Link-Escape-Angriffe (`SymlinkEscapeError`).
4. **Sibling-Temp & Atomare Promotion:**
   - Downloads werden zuerst in temporäre Geschwisterdateien `.<filename>.<uuid>.tmp` geschrieben.
   - Nach dem Download werden die Bytes neu gehasht und mit dem Inventar-Hash abgeglichen (`HashDriftError` bei Mismatch).
   - `detect_mime_and_active_content()` snifft Magic Bytes und blockiert aktive/ausführbare Inhalte (Windows PE `MZ`, Linux ELF, Scripts, verbotene Dateiendungen) fail-closed via `ActiveContentBlockedError`.
   - Promotion erfolgt atomar via `os.replace()`.
5. **Quoten & Limits:**
   - `check_quarantine_quotas()` erzwingt:
     - Einzeldateilimit: 15 MB
     - Kumulatives Run-Limit: 25 MB
     - Maximale Dateianzahl pro Run: 5 Dateien
     - Bei Überschreitung bricht die Operation mit `QuotaExceededError` ab; Temp-Dateien werden bereinigt.
6. **Idempotenter Retry & Kollisionsschutz:**
   - Existiert die Zieldatei bereits mit identischem SHA-256, wird sie nicht überschrieben oder dupliziert (`status: "already_fetched"` / idempotent).
   - Existiert eine Datei mit gleichem Namen aber abweichendem Hash, bricht die Operation fail-closed mit `QuarantineCollisionError` ab.
7. **Manifest-Integration & CLI-Doku:**
   - [`mail_desk_himalaya_client.py`](skills/mail-desk/scripts/mail_desk_himalaya_client.py): `attachment_fetch` / `fetch_attachment` in `execute_manifest()` eingebunden; erzwingt Manifest-Account-Bindung.
   - [`references/cli-operations.md`](skills/mail-desk/references/cli-operations.md): Dokumentation der Operation und ihrer Sicherheitsvoraussetzungen.

---

### 2. Geänderte und neue Dateien

| Datei | Status | Verantwortung |
| --- | --- | --- |
| `skills/mail-desk/scripts/core/attachment_fetch.py` | Neu | Quarantäne-Abruf, Receipt-Prüfung, Quoten, Re-Hashing, Re-Typing, Sibling-Temp |
| `skills/mail-desk/tests/test_maildesk_attachments_mda2.py` | Neu | 14 hermetische Tests für Quarantäne, Limits, Drift, Quoten, Kollision |
| `skills/mail-desk/scripts/mail_desk_himalaya_client.py` | Modifiziert | Manifest-Handler für `attachment_fetch` mit Account-Drift-Guard |
| `skills/mail-desk/references/cli-operations.md` | Modifiziert | Dokumentation der `attachment_fetch`-Manifest-Operation |

---

### 3. Verifikationsergebnisse & Nachweise

1. **Fokussierte Suite `MD-A2`:**
   ```powershell
   python -m unittest skills/mail-desk/tests/test_maildesk_attachments_mda2.py
   ```
   *Ergebnis:* **14 von 14 Tests OK (0.139s)**  
   *Abdeckung:* Erfolg, Receipt-Drift, Identitäts-/Location-Drift, Traversal, Win32-Gerätename, Symlink/Reparse, Timeout, Limits, MIME-/Endungsdrift, aktiver Inhalt, Kollision, idempotenter Retry, Teilfehler, Cleanup-Grenze, Preflight-No-op.

2. **Fokussierte Suite `MD-A1`:**
   ```powershell
   python -m unittest skills/mail-desk/tests/test_maildesk_attachments_mda1.py
   ```
   *Ergebnis:* **46 von 46 Tests OK (0.460s)**

3. **Gesamte Mail-Desk-Testsuite:**
   ```powershell
   python -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
   ```
   *Ergebnis:* **290 von 290 Tests OK (20.061s)** — 0 Fehler, 0 Regressionen.

4. **Linter & Formatierungsprüfung:**
   - `python -m compileall -q skills/mail-desk` ➔ **0 Fehler (Exit 0)**
   - `python quick_validate.py skills/mail-desk` ➔ **Skill is valid! (Exit 0)**
   - `git diff --check` ➔ **0 Whitespace-/Formatierungsfehler (Exit 0)**

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
