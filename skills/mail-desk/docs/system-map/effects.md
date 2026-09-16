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

Um zu verhindern, dass vertrauliche Mail-Inhalte, Tokens oder Prompts unbemerkt in maschinelle Indizes sickern:

* **Rekursiver Filter:** `_find_forbidden_content_keys()` in [`scripts/core/attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py#L154-L165).
* **Verbotene Schlüssel:**
  `text`, `extracted_text`, `content`, `body`, `prompt`, `llm_prompt`, `response`, `model_response`, `credentials`, `password`, `token`, `tokens`, `api_key`, `envelope_id`, `himalaya_id`.
* **Effekt:** Taucht einer dieser Schlüssel in Metadaten oder Dispositions-Referenzen auf, bricht die Indizierung sofort mit `ForbiddenContentError` ab.

---

## 3. Concurrency & Lock-Ownership

* **Voraussetzung:** Jede schreibende Skriptausführung (`execute`, `save_quarantine_index_atomic`) verlangt eine verifizierte Lease via `require_workspace_lock()`.
* **Kein Legacy-Bypass:** `--allow-legacy` wurde in den MD-Q2-Modulen vollständig entfernt; die Umgebungsvariable `WORKSPACE_LOCK_ALLOW_LEGACY` wird ignoriert.
* **Effekt:** Ohne gültige Lease bricht der Prozess mit `WorkspaceLockRequiredError` ab.

---

## 4. Fail-Closed Drift-Erkennung

Der Mail-Desk repariert Diskrepanzen **niemals still oder automatisch**:

* **Geltungsbereich:** Alle 16 Pflichtfelder im `attachment-quarantine-index.json`.
* **Widersprüchliche Alias-Felder:** Weichen `folder` vs. `original_folder` oder `clean_filename` vs. `filename` voneinander ab, wird dies als Manipulation gewertet.
* **Effekt:** Es wird sofort `AttachmentIndexDriftError` geworfen. Der Lauf stoppt ohne Schreiboperationen; der menschliche Operator muss den Zustand manuell auditieren.

---

## 5. Subprozess-Isolation & Netzwerk-Grenzen

* **Kein interaktiver Setup-Wizard:** Fehlt `config.toml`, wirft `resolve_himalaya_invocation()` sofort `HimalayaInvocationError("himalaya_config_missing")`. Ein interaktives Blockieren des Prozesses ist ausgeschlossen.
* **Timeout-Abbruch:** Hängt die IMAP-Verbindung, greift der Subprozess-Timeout. Ein Timeout führt zu `himalaya_timeout` und wird **nicht** wiederholt (kein Retry-Loop bei hängenden Prozessen).
