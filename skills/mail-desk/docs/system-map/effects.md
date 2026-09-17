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
* **Effekt:** Lokale Laufwerkspfade (z. B. `C:\Users\...` oder `D:/...`) werden deterministisch in lokale UNC-Admin-Share-Pfade umgewandelt (`\\localhost\C$\...` bzw. `\\localhost\D$\...`).

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

Der Schutz vor Race Conditions und parallelen Mutationen erfolgt zweistufig:

* **Workspace-Lease (Ebene 1):** Jede schreibende Skriptausführung (`execute`, `save_quarantine_index_atomic`, `apply_attachment_disposition`) verlangt eine verifizierte Workspace-Lease via `require_workspace_lock()`.
  * **Kein Legacy-Bypass:** `--allow-legacy` wurde in allen Quarantäne- und Dispositions-Modulen vollständig entfernt; `WORKSPACE_LOCK_ALLOW_LEGACY` wird ignoriert.
  * **Effekt:** Ohne gültige Lease bricht der Prozess mit `WorkspaceLockRequiredError` ab.
* **Inventar-File-Lock (Ebene 2):** Zur Absicherung gleichzeitiger Zugriffe auf Quarantäne-Dateien und `.quarantine-inventory.json` verwendet der Mail-Desk den `_QuarantineInventoryLock` (`.quarantine-inventory.json.lock`).
  * **Mechanismus:** Lock-Directory mit PID-Binding, Timeout (30s) und Prüfung veralteter Locks (Stale-Detection >300s).
  * **Effekt:** Verhindert parallele Teilmutationen von Ingest, Reconcile und Discard-Cleanup.

---

## 4. Fail-Closed Drift- & Integritäts-Erkennung

Der Mail-Desk repariert Diskrepanzen **niemals still oder automatisch**:

* **Quarantine-Index-Drift:** Alle 16 Pflichtfelder im `attachment-quarantine-index.json` werden streng typisiert. Widersprüchliche Alias-Felder (z. B. `folder` vs. `original_folder` oder `clean_filename` vs. `filename`) werden als Manipulation gewertet → `AttachmentIndexDriftError`.
* **Bereinigungs-Scope-Drift:** Beim Ausführen einer Discard-Bereinigung (`apply_attachment_disposition`) muss der tatsächliche Kandidaten-Scope deterministisch mit dem `apply_request_hash` des `ApplyRequest` und `ApprovalReceipt` übereinstimmen. Jede Abweichung im Scope bricht die Bereinigung vor dem Unlink ab → `AttachmentDispositionError`.
* **Recovery-Journal-Integrität:**
  * Das `attachment-discard-journal.json` verlangt eine lückenlos monotone State Machine (`prepared → file_deleted → inventory_updated → index_updated → completed`).
  * Die History muss zwingend mit `prepared` beginnen.
  * Top-Level-`status` (`in_progress`, `completed`, `failed`), `last_successful_state`, `failure_stage` und `error` müssen exakt mit dem letzten History-Eintrag (`history[-1]`) übereinstimmen. Verkürzte oder manipulierte Journale werden strikt abgelehnt.
* **Physische Integrität:** `size_bytes <= 0` in `.quarantine-inventory.json` oder im Index wird als korrupte Datei/Manifest gewertet und abgelehnt.
* **Effekt:** Tritt eine Integritätsverletzung auf, bricht die Engine sofort fail-closed ab (`AttachmentIndexDriftError` bzw. `AttachmentDispositionError`). Es finden keine Schreib- oder Löschoperationen statt; der Zustand verharrt zur menschlichen Begutachtung.

---

## 5. Subprozess-Isolation & Netzwerk-Grenzen

* **Kein interaktiver Setup-Wizard:** Fehlt `config.toml`, wirft `resolve_himalaya_invocation()` sofort `HimalayaInvocationError("himalaya_config_missing")`. Ein interaktives Blockieren des Prozesses ist ausgeschlossen.
* **Timeout-Abbruch:** Hängt die IMAP-Verbindung, greift der Subprozess-Timeout. Ein Timeout führt zu `himalaya_timeout` und wird **nicht** wiederholt (kein Retry-Loop bei hängenden Prozessen).
