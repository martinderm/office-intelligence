# mail-desk Batch-Runner Referenz & JSON-Schema

Dokumentation und Spezifikation für [`scripts/mail_desk_batch_runner.py`](../scripts/mail_desk_batch_runner.py).

## Zweck & Architektur

Der Batch-Runner bündelt mehrstufige E-Mail-Verarbeitungsabläufe in **einem einzigen Python-Aufruf**, um:
1. **Token-Verbrauch zu minimieren:** Vermeidung redundanter Tool-Aufrufe und Terminal-Puffer pro Einzel-Mail.
2. **Rechte-/Freigabeprozesse im Agent-Harness zu optimieren:** Der Nutzer muss für einen gesamten Batchlauf genau **einen** Shell-Befehl freigeben.
3. **Idempotenz und atomare Konsistenz sicherzustellen:** Gekoppeltes Routing, Verifikation im Zielordner, atomarer Index-Upsert (`final-location-index.json`), Protokollierung (`action-log.jsonl`) und Evidence-Pflege (`evidence/YYYY-MM.md`) in einer geschlossenen Transaktionskette.
4. **Automatische Aufräumlogik:** Das als Eingabe dienende temporäre JSON-Manifest unter `data/mail-desk/` wird nach bestätigter, fehlerfreier Ausführung automatisch gelöscht (`delete_input_on_success: true`).

---

## CLI-Aufrufe & Parameter

```bash
# Modus 1: Datei-basiertes Manifest (empfohlen)
python3 scripts/mail_desk_batch_runner.py --input data/mail-desk/batch-manifest.json

# Modus 2: STDIN-Übergabe
cat data/mail-desk/batch-manifest.json | python3 scripts/mail_desk_batch_runner.py --stdin

# Zusätzliche Optionen
python3 scripts/mail_desk_batch_runner.py --input data/mail-desk/batch-manifest.json \
  --account "primary" \
  --data-dir data/mail-desk \
  --index data/mail-desk/final-location-index.json \
  --keep-input \
  --threads 5
```

### Argumente

| Argument | Kurzform | Beschreibung |
|---|---|---|
| `--input <PFAD>` | `-i` | Pfad zur temporären JSON-Eingabedatei (z. B. in `data/mail-desk/`). |
| `--stdin` | | Liest das JSON-Manifest direkt aus der Standardeingabe. |
| `--account <NAME>` | `-a` | Optionaler Backend-/Himalaya-Account-Override. |
| `--data-dir <PFAD>` | | Pfad zum Datenverzeichnis (Standard: `data/mail-desk/`). |
| `--index <PFAD>` | | Pfad zur `final-location-index.json`. |
| `--keep-input` | | Verhindert das automatische Löschen des Eingabe-Files bei Erfolg. |
| `--threads <N>` | | Anzahl paralleler Worker für den `inspect`-Modus (Standard: 5). |

---

## Modus 1: `inspect` (Paralleles Einlesen & Vorfiltern)

### Beschreibung
Liest Metadaten, Header (`Message-Id`, `In-Reply-To`, `References`, `From`, `To`, `Date`, `Subject`) sowie Textvorschauen für mehrere E-Mails parallel ein. Gleicht die ermittelten Message-IDs automatisch mit `final-location-index.json` und `action-log.jsonl` ab, um den Bekanntheitsgrad (`is_new`) zu bestimmen.

### JSON-Schema (`inspect`)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MailDeskInspectRequest",
  "type": "object",
  "required": ["mode"],
  "properties": {
    "mode": {
      "type": "string",
      "enum": ["inspect", "fetch"]
    },
    "folder": {
      "type": "string",
      "default": "INBOX",
      "description": "Quellordner im Postfach."
    },
    "count": {
      "type": "integer",
      "default": 20,
      "description": "Anzahl der abzurufenden Nachrichten."
    },
    "order": {
      "type": "string",
      "enum": ["newest", "oldest"],
      "default": "newest",
      "description": "Sortierreihenfolge nach E-Mail-Alter."
    },
    "envelope_ids": {
      "type": "array",
      "items": { "type": ["string", "integer"] },
      "description": "Optionale explizite Liste von Envelope-IDs statt Sortierabruf."
    },
    "preview_lines": {
      "type": "integer",
      "default": 30,
      "description": "Maximale Anzahl an Textzeilen für den Body-Vorschautext."
    },
    "check_known": {
      "type": "boolean",
      "default": true,
      "description": "Gleicht Message-IDs gegen Index und Action-Log ab."
    },
    "output_file": {
      "type": "string",
      "description": "Optionaler Ausgabepfad (z. B. data/mail-desk/inspected.json). Wenn nicht angegeben, erfolgt die Ausgabe auf stdout."
    },
    "delete_input_on_success": {
      "type": "boolean",
      "default": true,
      "description": "Löscht das Eingabemanifest nach erfolgreicher Ausführung."
    }
  }
}
```

### Beispiel Input (`data/mail-desk/inspect-request.json`)
```json
{
  "mode": "inspect",
  "folder": "INBOX",
  "count": 20,
  "order": "oldest",
  "delete_input_on_success": true
}
```

### Beispiel Output
```json
{
  "ok": true,
  "mode": "inspect",
  "folder": "INBOX",
  "total_fetched": 1,
  "items": [
    {
      "envelope_id": "101",
      "folder": "INBOX",
      "message_id": "msg-2026-001@partner.example.org",
      "raw_message_id": "MSG-2026-001@partner.example.org",
      "subject": "Statusbericht Arbeitspaket 4",
      "from": "Dr. Alex Beispiel <alex@partner.example.org>",
      "to": "Empfänger <user@example.org>",
      "date": "Tue, 6 Jan 2026 14:15:20 +0000",
      "in_reply_to": "",
      "references": "",
      "preview": "Hallo zusammen,\n\nanbei der aktuelle Berichtsentwurf...",
      "error": null,
      "known_status": {
        "in_index": false,
        "in_action_log": false,
        "final_folder": null,
        "is_new": true
      }
    }
  ],
  "input_file_deleted": true
}
```

---

## Modus 2: `execute` (Gekoppelte Batch-Verarbeitung)

### Beschreibung
Führt für eine Liste von Nachrichten alle nötigen Einzelschritte aus:
1. **Mailbox-Routing:** Ausführen des Kopiervorgangs in den Zielordner.
2. **Zielverifikation:** Schnelle Ermittlung der neuen `envelope_id` im Zielordner.
3. **Index-Aktualisierung:** Atomarer Upsert in `final-location-index.json`.
4. **Aktionsprotokoll:** Anhängen des Eintrags an `data/mail-desk/action-log.jsonl`.
5. **Antwortbedarf:** Protokollierung in `replies-needed.jsonl` (wenn `needs_reply: true`).
6. **Wissens- & Evidenzpflege:** Automatische Aktualisierung / Anlage der Markdown-Datei (`evidence/YYYY-MM.md`) unter strikter Vermeidung von Duplikaten anhand der `message_id`.

### JSON-Schema (`execute`)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MailDeskExecuteRequest",
  "type": "object",
  "required": ["mode", "items"],
  "properties": {
    "mode": {
      "type": "string",
      "enum": ["execute", "process"]
    },
    "mailbox": {
      "type": "string",
      "default": "primary",
      "description": "Logischer Bezeichner des Postfachs."
    },
    "backend": {
      "type": "string",
      "default": "himalaya",
      "description": "Verwendetes Mail-Backend."
    },
    "delete_input_on_success": {
      "type": "boolean",
      "default": true,
      "description": "Löscht das Eingabemanifest nach erfolgreicher Ausführung."
    },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["envelope_id", "message_id", "action", "decision"],
        "properties": {
          "envelope_id": {
            "type": ["string", "integer"],
            "description": "Aktuelle Envelope-ID im Quellordner."
          },
          "source_folder": {
            "type": "string",
            "default": "INBOX",
            "description": "Quellordner der Nachricht."
          },
          "message_id": {
            "type": "string",
            "description": "RFC Message-ID (mit oder ohne spitze Klammern)."
          },
          "raw_message_id": {
            "type": "string",
            "description": "Optionale originale RFC Message-ID mit Original-Groß-/Kleinschreibung."
          },
          "subject": {
            "type": "string",
            "description": "Betreffzeile (für Zielverifikation und Logging)."
          },
          "from": {
            "type": "string",
            "description": "Absenderangabe."
          },
          "date": {
            "type": "string",
            "description": "Datumsangabe."
          },
          "action": {
            "type": "object",
            "required": ["type"],
            "properties": {
              "type": {
                "type": "string",
                "enum": ["copy_as_move", "move", "copy", "none", "archive"]
              },
              "target_folder": {
                "type": "string",
                "description": "Zielordner im Postfach (z. B. 'Projekte/Project-Alpha')."
              }
            }
          },
          "decision": {
            "type": "object",
            "required": ["kind", "id", "confidence", "needs_reply"],
            "properties": {
              "kind": {
                "type": "string",
                "enum": ["project", "topic", "archive", "ignore", "newsletter"]
              },
              "id": {
                "type": "string",
                "description": "ID aus projects.json oder topics.json (z. B. 'project-alpha')."
              },
              "subtopic": {
                "type": "string",
                "description": "Optionales Subtopic."
              },
              "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"]
              },
              "needs_reply": {
                "type": "boolean"
              }
            }
          },
          "notes": {
            "type": "string",
            "description": "Operative Begründung / Zusammenfassung für das Action-Log."
          },
          "evidence": {
            "oneOf": [
              {
                "type": "object",
                "required": ["file", "entry"],
                "properties": {
                  "file": { "type": "string", "description": "Relativer Pfad zur Evidence-Datei." },
                  "entry": { "type": "string", "description": "Markdown-Zeile(n) für das Evidence-Log." }
                }
              },
              {
                "type": "array",
                "items": {
                  "type": "object",
                  "required": ["file", "entry"],
                  "properties": {
                    "file": { "type": "string" },
                    "entry": { "type": "string" }
                  }
                }
              }
            ]
          }
        }
      }
    }
  }
}
```

### Beispiel Input (`data/mail-desk/batch-manifest.json`)
```json
{
  "mode": "execute",
  "mailbox": "primary",
  "backend": "himalaya",
  "delete_input_on_success": true,
  "items": [
    {
      "envelope_id": "101",
      "source_folder": "INBOX",
      "message_id": "msg-2026-001@partner.example.org",
      "raw_message_id": "MSG-2026-001@partner.example.org",
      "subject": "Statusbericht Arbeitspaket 4",
      "from": "Dr. Alex Beispiel <alex@partner.example.org>",
      "action": {
        "type": "copy_as_move",
        "target_folder": "Projekte/Project-Alpha"
      },
      "decision": {
        "kind": "project",
        "id": "project-alpha",
        "confidence": "high",
        "needs_reply": false
      },
      "notes": "Alex Beispiel übermittelt WP4-Berichtsentwurf zu Project-Alpha.",
      "evidence": {
        "file": "memory/references/projects/project-alpha/evidence/2026-01.md",
        "entry": "- 2026-01-06 — Übermittlung des Entwurfs zum WP4-Bericht durch Partner.\n  - Message-ID: `msg-2026-001@partner.example.org` (Dr. Alex Beispiel)\n  - Aussagekern: Übermittlung des Entwurfs zur Vorabstimmung..."
      }
    }
  ]
}
```

### Beispiel Output
```json
{
  "ok": true,
  "mode": "execute",
  "total_processed": 1,
  "all_succeeded": true,
  "results": [
    {
      "envelope_id": "101",
      "message_id": "msg-2026-001@partner.example.org",
      "subject": "Statusbericht Arbeitspaket 4",
      "final_folder": "Projekte/Project-Alpha",
      "new_envelope_id": "205",
      "routing": "ok",
      "metadata": "ok",
      "final-index-script": "ok",
      "reference-source-id": "ok",
      "success": true
    }
  ],
  "input_file_deleted": true
}
```

---

## Fehlerbehandlung & Sicherheit

1. **Kein Datenverlust:** Schlägt auch nur ein Einzelschritt (z. B. Routing oder Index-Write) fehl, gibt das Skript `ok: false` zurück und das Eingabemanifest **bleibt zur Fehleranalyse erhalten** (wird nicht gelöscht).
2. **Atomare Index-Transaktion:** `final-location-index.json` wird über eine temporäre Zwischendatei (`.tmp`) geschrieben und anschließend atomar ersetzt, um Korruption bei Prozessabbrüchen zu verhindern.
3. **Plattformunabhängiges UTF-8:** Standard-Streams (`stdout`/`stderr`) und Dateilese-/schreiboperationen sind strikt auf UTF-8 konfiguriert (verhindert Windows `charmap`-Codierungsfehler bei Umlauten oder Sonderzeichen).
