# Mail-Desk — Subsystem System Map: Verben (Processes)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Verben  
> **Subsystem**: [`skills/mail-desk`](../SKILL.md)  
> **Ziel**: Detaillierte Darstellung aller Transformations-, Batch- und Quarantäne-Pipelines mit Code-Referenzen.  
> **Gültig für**: `skills/mail-desk/` relativ zum Repository-Root

---

## 1. Übersicht der Lifecycles im Mail-Desk

```
[Mailbox / Himalaya]
         │
         ▼
[1. Preflight & Search] ──────► scripts/core/himalaya.py
         │
         ▼
[2. Draft-Phase] ─────────────► scripts/core/classifier.py (Katalog-Matching)
         │                       └─► Erzeugt Review-Hash & Manifest
         ▼
[3. Human / Agent Gate] ──────► Freigabe erzeugt Review-Receipt
         │
         ▼
[4. Execute-Phase] ───────────► scripts/core/modes/execute.py
         │                       ├─► Optional: Quarantäne-Pipeline (MD-Q1/MD-Q2)
         │                       └─► final-location-index.json Update
         ▼
[5. Verify-Phase] ────────────► scripts/core/modes/verify.py (Zielordner-Check)
         │
         ▼
[6. Dossier & Handoff] ───────► scripts/core/modes/dossier_synthesis.py
                                 └─► Action Candidates an task-desk
```

---

## 2. Der reguläre Batch-Runner Lifecycle

Definiert in [`references/batch-runner.md`](../references/batch-runner.md) und implementiert in [`scripts/core/modes/`](../scripts/core/modes/):

### Phase 1: `draft` ([`scripts/core/modes/draft.py`](../scripts/core/modes/draft.py))
1. Lädt verarbeitete Mails aus `final-location-index.json` und `sent-index.json`.
2. Ruft unprozessierte E-Mails aus dem Zielordner ab (`get_unprocessed_emails()`).
3. Führt konservative Klassifikation durch:
   - Extrahiert Betreff, Sender und Text-Snippets.
   - Prüft Projektcodes (`WP...`, Projekt-IDs) und Topic-Schlagworte gegen `memory/references/`.
   - Bei Mehrdeutigkeit verbleibt das Item ohne automatische Verschiebung in `INBOX`.
4. Berechnet den `review_hash` über alle vorgeschlagenen Aktionen und emittiert das `DraftManifest`.

### Phase 2: Review-Gate
* Ohne einen gültigen, kryptographisch bindenden Receipt mit identischem `review_hash` weigert sich die Folgestufe strikt zu starten.

### Phase 3: `execute` ([`scripts/core/modes/execute.py`](../scripts/core/modes/execute.py))
1. Validiert den Workspace-Lock (`require_workspace_lock()`).
2. Prüft die Gültigkeit des übergebenen Receipts gegen das Manifest.
3. Führt IMAP-Operationen (Verschieben, Flaggen) atomar aus.
4. Schreibt neue Einträge in `data/mail-desk/final-location-index.json` via atomarem Tempfile-Replace.

### Phase 4: `verify` ([`scripts/core/modes/verify.py`](../scripts/core/modes/verify.py))
1. Fragt die Zielordner via Himalaya ab.
2. Bestätigt das physische Vorhandensein der verschobenen Mails am Zielort.

---

## 3. Die Attachment-Quarantäne-Pipeline (MD-Q1 / MD-Q2)

Implementiert in [`scripts/core/attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py) und [`scripts/core/attachment_fetch.py`](../scripts/core/attachment_fetch.py):

```
[MIME-Part Erkennung] ──► [Symlink / Reparse Check] ──► [Download in attachments/<run_id>/]
                                                                   │
                                                                   ▼
[Atomarer Index-Write] ◄── [SHA-256 Re-Verifikation] ◄── [Inventar .quarantine-inventory.json]
```

1. **MIME-Inspektion (`inspect_mime_tree`):**
   - Traversiert die MIME-Struktur der Mail und identifiziert Binäranhänge.
2. **Containment- & Sicherheitsprüfung:**
   - Quarantänepfad wird workspace-relativ aufgelöst: `data/mail-desk/attachments/<run_id>/<clean_filename>`.
   - Prüfung auf Symlinks und Windows Reparse-Points (Attribut-Bit `0x400`).
3. **Physischer Download & Inventar-Erzeugung:**
   - Binärdatei wird geschrieben.
   - `.quarantine-inventory.json` wird mit dem tatsächlichen SHA-256 Hash der Disk-Bytes erstellt.
4. **Deterministische ID-Ableitung (`compute_attachment_id`):**
   - SHA-256 Hash über `{"inventory_sha256", "message_id", "part_locator"}`.
5. **Re-Verifikation:**
   - `verify_quarantine_attachment_artifact()` verifiziert die Datei unmittelbar vor dem Index-Write erneut gegen Disk-Bytes.
6. **Atomarer Index-Eintrag:**
   - `save_quarantine_index_atomic()` lädt den bestehenden Index, validiert Schema 1, prüft auf Drift (`AttachmentIndexDriftError`), fügt das Item ein und speichert atomar via `tempfile` + `os.replace`.

---

## 4. Der Read-Only Reconcile Flow

Implementiert in [`scripts/core/modes/reconcile.py`](../scripts/core/modes/reconcile.py):

* **Ziel:** Vollständige Konsistenzprüfung zwischen Dateisystem, Inventaren und Index ohne jede Schreiboperation.
* **Ergebnisse:**
  * `consistent`: Alle Dateien auf Disk stimmen exakt mit Inventaren und Index überein.
  * `missing_review`: Quarantäne-Dateien existieren, wurden aber noch nicht indiziert oder freigegeben.
  * `drift`: Ein Feld (Hash, Größe, Pfad) weicht ab → Sofortiger Alarmstop (`AttachmentIndexDriftError`).

---

## 5. Himalaya Invocation Lifecycle

Implementiert in [`scripts/core/himalaya.py`](../scripts/core/himalaya.py):

1. **Fail-Fast Bootstrap (`resolve_himalaya_invocation`):**
   - Liest `HIMALAYA_CONFIG` aus der Umgebung (oder Default).
   - Wandelt Windows-Laufwerkspfade zu UNC um (`normalize_himalaya_config_path`: `C:\...` → `\\localhost\C$\...`).
   - Fehlt die Konfigurationsdatei → Sofortiger Abbruch mit Stopcode `himalaya_config_missing` (verhindert den interaktiven Setup-Wizard).
   - Fehlt die Binärdatei → Abbruch mit `himalaya_unavailable`.
2. **Kommando-Konstruktion (`build_himalaya_command`):**
   - Erzeugt tokenisierte Argumentliste ohne Shell (`shell=False`).
   - Fügt Account-Parameter `-a <account>` an vordefinierter Position ein.
3. **Ausführung mit Timeout:**
   - Führt den Subprozess aus; bei Überschreiten des Timeouts erfolgt sofortiger harter Abbruch ohne Retries (`himalaya_timeout`).
