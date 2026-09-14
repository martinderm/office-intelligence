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
| `MD-A3` — Begrenzte Extraktion & OCR-Derivat | ⬜ nächstes Paket | — | ausstehend |
| `MD-A4` — Materialitäts-Gate & LLM-Handoff | ⬜ geplant | — | ausstehend |
| `MD-A5` — Ablagevorschlag | ⬜ geplant | — | ausstehend |

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

## Nächstes Paket: `MD-A3` — Begrenzte Extraktion und lokales OCR-Derivat

- **Scope:** Nur verifizierte Quarantänedateien; keine Mailbox, kein Cloud-Write, kein LLM.
- **Dateien:** Neu `scripts/core/attachment_extract.py`, `tests/test_maildesk_attachments_mda3.py`.
- **Limits:** PDF 10 Seiten; OCR 3 Seiten/30 s; DOCX 40 Absätze; PPTX 15 Slides; XLSX 2 Sheets x 50 Zeilen x 10 Spalten; Prozess 20 s; 15.000 Zeichen je Datei.
- **OCR-Regel:** Nur Quarantäne-Derivat wird verändert; Original bleibt unverändert.
