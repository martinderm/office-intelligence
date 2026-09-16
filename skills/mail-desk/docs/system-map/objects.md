# Mail-Desk — Subsystem System Map: Nomen (Objects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Nomen  
> **Subsystem**: [`skills/mail-desk`](../SKILL.md)  
> **Ziel**: Vollständige Dokumentation aller Datenstrukturen, Indizes, Schemas und DTOs des Mail-Desks mit exakten Felddefinitionen und Quellcode-Zitaten.  
> **Gültig für**: `skills/mail-desk/` und `data/mail-desk/` im Ziel-Workspace

---

## 1. Versionierter Quarantäneindex (`attachment-quarantine-index.json`)

Der Quarantäneindex ist die zentrale Buchführung über alle isolierten Dateianhänge.

* **Dateipfad:** `data/mail-desk/attachment-quarantine-index.json`
* **Implementierungsdatei:** [`scripts/core/attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py#L70-L135)
* **CLI-Fassade:** [`scripts/mail_desk_attachment_quarantine_index.py`](../scripts/mail_desk_attachment_quarantine_index.py)
* **Aktuelle Schema-Version:** `1`

### 1.1 Root-Struktur
Das Wurzelelement erlaubt ausschließlich drei Felder (`ALLOWED_ROOT_FIELDS`):
```json
{
  "schema_version": 1,
  "updated_at": "2026-09-16T14:00:00Z",
  "items": {
    "<attachment_id>": { "..." : "..." }
  }
}
```

### 1.2 Die 16 kanonischen Eintragsfelder (`CANONICAL_ENTRY_FIELDS`)
Jeder Eintrag unter `items` erzwingt die exakte Einhaltung dieser 16 Schlüssel:

| Feldname | Typ / Regex | Beschreibung |
| :--- | :--- | :--- |
| `attachment_id` | `^[0-9a-fA-F]{64}$` | Deterministischer 64-Hex SHA-256 Hash (siehe unten). |
| `message_id` | String | Normalisierte Message-ID (RFC 822). |
| `account` | String | Mailbox-Account-Bezeichnung. |
| `folder` | String | Ursprünglicher Mailbox-Ordner (z. B. `INBOX`). |
| `part_locator` | `^\d+(?:\.\d+)*$` | MIME-Part Locator (z. B. `1`, `2.1`). |
| `clean_filename` | String | Bereinigter Dateiname ohne Sonderzeichen/Steuerzeichen. |
| `mime_type` | `^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$` | Normalisierter MIME-Typ. |
| `sha256` | `^[0-9a-fA-F]{64}$` | Physischer SHA-256 Hash der Binärdatei auf Disk. |
| `size_bytes` | Integer (`>= 0`) | Exakte Dateigröße in Bytes. |
| `run_id` | String | Eindeutige Kennung des Extraktionslaufs. |
| `quarantine_path` | String | Workspace-relativer Pfad: `data/mail-desk/attachments/<run_id>/<file>`. |
| `analysis_status` | Enum | Fest auf `"completed"` (`ALLOWED_ANALYSIS_STATUSES`). |
| `analyzed_at` | RFC 3339 String | Zeitstempel der Extraktion und Analyse. |
| `contract_version` | String | Version des Extraktionsvertrags (z. B. `"1.0"`). |
| `contract_hash` | String / Hex | Hash des bindenden Prüfvertrags. |
| `lifecycle_state` | Enum | Fest auf `"quarantined"` (`ALLOWED_LIFECYCLE_STATES`). |
| `disposition_ref` | String / `null` | Max. 128 Zeichen (`DISPOSITION_REF_REGEX`) oder `null` (MD-Q3 Vorbereitung). |

### 1.3 Ableitung der deterministischen `attachment_id`
Definiert in [`scripts/core/attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py#L172-L196):
```python
canonical_dict = {
    "inventory_sha256": norm_sha,
    "message_id": norm_mid,
    "part_locator": norm_loc,
}
canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
attachment_id = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
```

### 1.4 Verbotene Inhaltsfelder (`FORBIDDEN_ENTRY_FIELDS`)
Die Anwesenheit folgender Felder (auch verschachtelt) führt über `_find_forbidden_content_keys()` zum sofortigen `ForbiddenContentError`:
`text`, `extracted_text`, `content`, `body`, `prompt`, `llm_prompt`, `response`, `model_response`, `credentials`, `password`, `token`, `tokens`, `api_key`, `envelope_id`, `himalaya_id`.

---

## 2. Quarantäne-Inventar (`.quarantine-inventory.json`)

* **Speicherort:** `data/mail-desk/attachments/<run_id>/.quarantine-inventory.json`
* **Implementierungsdatei:** [`scripts/core/attachment_fetch.py`](../scripts/core/attachment_fetch.py)
* **Zweck:** Dient als physischer Bindungsnachweis zwischen extrahierter Datei auf Disk und Index. Bevor ein Eintrag in den Quarantäneindex geschrieben wird, prüft `verify_quarantine_attachment_artifact()` physisch, ob:
  1. Die Datei auf Disk existiert.
  2. Sie weder Symlink noch Windows-Reparse-Point ist (`os.lstat().st_file_attributes & 0x400`).
  3. Ihr physischer SHA-256 Hash exakt mit dem Inventar übereinstimmt.

---

## 3. Begleitende Indizes & Control-Plane-Objekte

### 3.1 Final Location Index (`final-location-index.json`)
* **Dateipfad:** `data/mail-desk/final-location-index.json`
* **Implementierungsdatei:** [`scripts/core/index.py`](../scripts/core/index.py#L12-L28)
* **Schema-Version:** `1`
* **Zweck:** Verhindert Doppelverarbeitung. Mappt normalisierte `message_id` auf Ablageort, Verarbeitungsstatus und Timestamp.
* **Erlaubte Eintragsfelder (`ALLOWED_FIELDS`):**
  `message_id`, `mailbox`, `backend`, `final_folder`, `final_label`, `envelope_id`, `gmail_message_id`, `gmail_thread_id`, `in_reply_to`, `references`, `subject`, `from`, `date`, `updated_at`.

### 3.2 Sent Items Index (`sent-index.jsonl`)
* **Dateipfad:** `data/mail-desk/sent-index.jsonl` (JSON-Lines-Format)
* **Implementierungsdatei:** [`scripts/core/sent_indexer.py`](../scripts/core/sent_indexer.py#L25-L43)
* **Zweck:** Hält gesendete E-Mails nach, um Antwortzustände (`check_if_replied()`) für eingehende Mails präzise zu ermitteln.
* **In-Memory-Lookup-Maps:**
  * `by_message_id`: Direkte Normalisierte Message-ID-Zuordnung
  * `by_in_reply_to`: Zuordnung über Header `In-Reply-To`
  * `by_reference`: Zuordnung über `References`-Ketten
  * `by_subject_clean`: Zuordnung über normalisierten Betreff (`clean_subject()`, befreit von Re/Aw/Wg/Fwd)

### 3.3 Backend-Konfiguration (`mail-desk-backend.json`)
* **Dateipfad:** `.agents/mail-desk-backend.json` (im Ziel-Workspace)
* **Zweck:** Credentials-freie Deklaration des Mailbox-Backends (`"himalaya"` / `"gmail"`), des Standard-Accounts und Quellordners.

---

## 4. DTOs, Verträge und In-Memory-Strukturen

* **`DraftManifest` & Review Contract** ([`scripts/core/batch_contract.py`](../scripts/core/batch_contract.py)):
  * Bindende Parameter: `expected_count`, `allow_fewer`, `candidate_count`, `source_folder`, `account`, `skip_known`, `review`.
  * `canonical_execute_request_sha256()`: Berechnet Hash über das Request-Objekt unter bewusstem Ausschluss des Feldes `review`.
* **Attachment Filing Candidate (MD-A5)** ([`scripts/core/attachment_filing.py`](../scripts/core/attachment_filing.py)):
  * Vorschlag für Cloud-Ablage mit `promotion_status: "pending_human_review"`. Rein lesend; führt keine unautorisierten Cloud-Mutationen aus.
* **`Dossier`** ([`scripts/core/modes/dossier.py`](../scripts/core/modes/dossier.py)): Strukturierte Fallakte mit klassifizierten Workpackages (`WP...`), Aufgaben und Signalstärken.
* **`SynthesisHandoff`** ([`scripts/core/synthesis_handoff.py`](../scripts/core/synthesis_handoff.py)): Bereinigtes Übergabepaket für identifizierte Action Items zur Weitergabe an `task-desk`.

