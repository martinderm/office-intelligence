# mail-desk Batch-Runner Referenz & JSON-Schema

Dokumentation und Spezifikation für [`scripts/mail_desk_batch_runner.py`](../scripts/mail_desk_batch_runner.py).

## Zweck & Architektur

Der Batch-Runner bündelt mehrstufige E-Mail-Verarbeitungsabläufe in **einem einzigen Python-Aufruf**, um:
1. **Token-Verbrauch zu minimieren:** Vermeidung redundanter Tool-Aufrufe und Terminal-Puffer pro Einzel-Mail.
2. **Rechte-/Freigabeprozesse im Agent-Harness zu optimieren:** Der Nutzer muss für einen gesamten Batchlauf genau **einen** Shell-Befehl freigeben.
3. **Idempotenz und atomare Konsistenz sicherzustellen:** Gekoppeltes Routing, Verifikation im Zielordner, atomarer Index-Upsert (`final-location-index.json`), Protokollierung (`action-log.jsonl`) und Evidence-Pflege (`evidence/YYYY-MM.md`) in einer geschlossenen Transaktionskette.
4. **Automatische Aufräumlogik:** Das als Eingabe dienende temporäre JSON-Manifest unter `data/mail-desk/` wird nach bestätigter, fehlerfreier Ausführung automatisch gelöscht (`delete_input_on_success: true`).
5. **Autonome Pipeline & Drafts:** Ermöglicht das automatisierte Nachladen unverarbeiteter E-Mails (`skip_known: true`), regelbasiertes Erstellen von Manifest-Entwürfen (`draft`) sowie autonome End-to-End-Verarbeitungsdurchläufe (`pipeline`).

### Implementierungsstruktur

Der Runner bleibt Eigentümer von CLI, Konfiguration, Dispatch und dem kanonischen Ergebnis-Envelope. Alle acht Handler liegen unter `scripts/core/modes/`: `search.py`, `resolve.py`, `inspect.py`, `draft.py`, `sync_sent.py`, `execute.py`, `verify.py` und `pipeline.py`. `search` und `resolve` werden direkt importiert und re-exportiert. Für `inspect`, `draft`, `sync_sent`, `execute`, `verify` und `pipeline` behält der Runner schlanke gleichnamige Kompatibilitäts-Fassaden, die seine bisherigen patchbaren Abhängigkeiten zur Laufzeit einspeisen. Damit bleiben bestehende Imports, Patches, Modus-Aliase sowie Cleanup-, Manifest-, Sent-Index-, Mutations- und Konsistenzprüf-Semantik stabil, ohne einen Importzyklus zu erzeugen. Im 952-zeiligen Runner verbleiben bewusst diese Fassaden, CLI-/Envelope-/Konfigurations- und Dispatch-Helfer sowie die gemeinsamen Fetch-Helper; `dossier.py` ist nicht Teil dieses abgeschlossenen FR-05.

---

## Einheitliche Standard-Dateinamen

Für temporäre Ein- und Ausgabedateien gelten unter `data/mail-desk/` folgende standardisierte Dateinamen:

| Dateityp | Standard-Pfad | Modus | Zweck & Lebenszyklus |
|---|---|---|---|
| **Inspektions-Anforderung (Input)** | `data/mail-desk/batch-inspect.json` | `inspect` | Temporäres Eingabemanifest zum Vorfiltern; wird nach erfolgreicher Ausführung automatisch gelöscht. |
| **Inspektions-Ergebnis (Output)** | `data/mail-desk/batch-inspected.json` | `inspect` | Standard-Ausgabedatei mit extrahierten Headern, Previews und Bekanntheitsstatus. |
| **Entwurf-Anforderung (Input)** | `data/mail-desk/batch-draft.json` | `draft` | Erzeugt einen vollständigen `batch-manifest.json`-Entwurf basierend auf Katalogen. |
| **Ausführungs-Manifest (Input)** | `data/mail-desk/batch-manifest.json` | `execute` | Temporäres Arbeitsmanifest mit Routing-, Logging- und Evidenzentscheidungen; wird nach erfolgreicher Ausführung automatisch gelöscht. |
| **Pipeline-Anforderung (Input)** | `data/mail-desk/batch-pipeline.json` | `pipeline` | Führt den gesamten Ablauf (Inspect -> Classify -> Execute -> Verify) autonom aus. |
| **Ausführungs-Ergebnis (Output)** | `data/mail-desk/batch-result.json` | `execute` | Optionales / standardisiertes Protokoll des ausgeführten Batch-Laufs. |
| **Verifikations-Anforderung (Input)** | `data/mail-desk/batch-verify.json` | `verify` | Temporäre Liste von Message-IDs / Batch-Files zur Konsistenzprüfung (Index, Log, Evidenz, Ordner). |
| **Such-Anforderung (Input)** | `data/mail-desk/batch-search.json` | `search` | Suchauftrag nach Text oder Message-IDs über mehrere Mailbox-Ordner hinweg. |
| **Falllösungs-Anforderung (Input)** | `data/mail-desk/batch-resolve.json` | `resolve` | Schließt und archiviert offene Fälle aus `replies-needed.jsonl` / `pending-review.jsonl`. |

---

## CLI-Aufrufe & Parameter

```bash
# Standard 1: Batch-Ausführung mit Standard-Manifest (Input wird automatisch gefunden)
python3 scripts/mail_desk_batch_runner.py

# Standard 2: Expliziter Pfad für Batch-Ausführung
python3 scripts/mail_desk_batch_runner.py --input data/mail-desk/batch-manifest.json

# Standard 3: Batch-Inspektion (JSON-gesteuert)
python3 scripts/mail_desk_batch_runner.py --input data/mail-desk/batch-inspect.json

# Standard 4: Autonome Pipeline direkt per CLI (Standard: 20 älteste Mails)
python3 scripts/mail_desk_batch_runner.py --pipeline 50 --order oldest

# Standard 5: Manifest-Entwurf direkt per CLI
python3 scripts/mail_desk_batch_runner.py --draft 50 --order oldest

# Standard 6: Direkte Inspektion per CLI
python3 scripts/mail_desk_batch_runner.py --inspect 50 --order oldest

# Standard 7: Einen gefilterten Pipeline-Lauf starten
python3 scripts/mail_desk_batch_runner.py --pipeline 50 --query 'from partner@example.org'
```

### Argumente

| Argument | Kurzform | Beschreibung |
|---|---|---|
| `--input <PFAD>` | `-i` | Pfad zur temporären JSON-Eingabedatei (Standard: `batch-manifest.json`, `batch-inspect.json`, etc.). |
| `--pipeline [N]` | `-p` | Führt die End-to-End-Pipeline für N Mails aus (Inspect, Classify, Execute, Verify). |
| `--draft [N]` | `-d` | Inspiziert N unverarbeitete Mails und schreibt einen `batch-manifest.json`-Entwurf. |
| `--inspect [N]` | | Inspiziert N Mails und schreibt `batch-inspected.json`. |
| `--order <oldest\|newest>` | | Verarbeitungsreihenfolge nach Alter (Standard: `oldest`). |
| `--folder <ORDNER>` | `-f` | Quellordner im Postfach (Standard: `INBOX`). |
| `--skip-known` / `--no-skip-known` | | Überspringt bereits verarbeitete E-Mails aus `final-location-index.json` (Standard: `True`). |
| `--query <AUSDRUCK>` | `-q` | Himalaya-Suchausdruck für `inspect`, `draft` und `pipeline`; wird bis zum Envelope-Abruf weitergereicht. Nicht zusammen mit `--date` verwenden. |
| `--date <YYYY-MM-DD>` | | Exakter Himalaya-Datumsfilter für `inspect`, `draft` und `pipeline`; wird bis zum Envelope-Abruf weitergereicht. Nicht zusammen mit `--query` verwenden. |
| `--min-confidence <high\|medium\|low>` | | Minimale Konfidenz für automatische Ausführung im Pipeline-Modus (Standard: `high`). |
| `--stdin` | | Liest das JSON-Manifest direkt aus der Standardeingabe. |
| `--account <NAME>` | `-a` | Optionaler Backend-/Himalaya-Account-Override. |
| `--data-dir <PFAD>` | | Pfad zum Datenverzeichnis (Standard: `data/mail-desk/`). |
| `--index <PFAD>` | | Pfad zur `final-location-index.json`. |
| `--keep-input` | | Verhindert das automatische Löschen des Eingabe-Files bei Erfolg. |

---

### Filter, Reihenfolge und bekannte Nachrichten

`--query` und `--date` gelten für alle drei lesenden Direktmodi (`inspect`,
`draft`, `pipeline`) und für die entsprechenden Manifestfelder `query` bzw.
`date`. Die Filter sind gegenseitig exklusiv; der Runner bricht bei einer
Kombination ab, statt einen Filter still zu ignorieren. Gefilterte Himalaya-
Ergebnisse werden anhand geparster RFC-5322-Daten chronologisch geordnet;
nicht parsebare Daten folgen am Ende. Bei `skip_known: true` erweitert der
Runner seine Abruffenster eindeutig und aufsteigend bis 2.500 Envelopes,
beginnend beim Doppelten des angeforderten Targets (mindestens 25, dabei bei
2.500 gedeckelt). Targets über 2.500 erhalten einen abschließenden Abruf in Zielgröße,
statt still begrenzt zu werden. Viele bereits indizierte Mails verdecken so
den ersten neuen Fall nicht. `--query`/`--date` ohne einen dieser drei
Direktmodi sind ein Argumentfehler; für Manifestläufe stehen die gleichnamigen
Manifestfelder bereit. Himalaya-Prozessfehler oder ungültiges Envelope-JSON
sind Fehlerzustände, nie ein leeres Ergebnis.

Temporäre Manifest-Lese- und Löschoperationen behandeln transiente Windows-
Dateisperren mit maximal drei Versuchen und kurzem exponentiellem Backoff
(0,1 s, 0,2 s). Danach bleibt das Manifest erhalten und der Lauf meldet den
Fehler.

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
    "query": {
      "type": "string",
      "description": "Optionaler Himalaya-Suchausdruck; gegenseitig exklusiv mit date."
    },
    "date": {
      "type": "string",
      "description": "Optionaler exakter Datumsfilter (YYYY-MM-DD); gegenseitig exklusiv mit query."
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
  "action": "batch_runner",
  "success": true,
  "state": "Completed",
  "message": "Inspected 1 message(s) from INBOX.",
  "data": {
    "operation": "inspect",
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
  },
  "error": null
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
          },
          "synthesis_targets": {
            "type": "array",
            "description": "Optionale, vor Execute menschlich/LLM-reviewbar angereicherte Synthese-Ziele. Autonome Drafts liefern immer [].",
            "items": {
              "type": "object",
              "required": ["file", "type"],
              "additionalProperties": false,
              "properties": {
                "file": {
                  "type": "string",
                  "minLength": 1,
                  "description": "Sicherer relativer POSIX-Pfad auf .md unter memory/references/projects/<decision.id>/... bzw. memory/references/topics/<decision.id>/... ."
                },
                "type": {
                  "type": "string",
                  "minLength": 1,
                  "pattern": "^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$",
                  "description": "Stabiles maschinenlesbares Label im sicheren Label-Raum; bewusst kein enger Enum."
                },
                "recommended_action": { "type": "string", "minLength": 1 },
                "task_anchor": { "type": "string", "minLength": 1 },
                "section": { "type": "string", "minLength": 1 }
              }
            }
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
      },
      "synthesis_targets": [
        {
          "file": "memory/references/projects/project-alpha/statusampel.md",
          "type": "statusampel",
          "recommended_action": "review_update",
          "task_anchor": "WP4"
        }
      ]
    }
  ]
}
```

### Beispiel Output
```json
{
  "action": "batch_runner",
  "success": true,
  "state": "Completed",
  "message": "Processed 1 message(s), all succeeded.",
  "data": {
    "operation": "execute",
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
        "success": true,
        "synthesis_targets": [
          {
            "file": "memory/references/projects/project-alpha/statusampel.md",
            "type": "statusampel",
            "recommended_action": "review_update",
            "task_anchor": "WP4"
          }
        ]
      }
    ],
    "telemetry": {
      "affected_projects": ["project-alpha"],
      "affected_topics": [],
      "synthesis_required": true
    },
    "input_file_deleted": true
  },
  "error": null
}
```

### FR-06a-Telemetrie

Jeder `execute`- und `pipeline`-Envelope enthält unter `data.telemetry` exakt
`affected_projects`, `affected_topics` und `synthesis_required`. Gezählt werden
nur Resultate mit `success: true`, deren `decision.kind` exakt `project` oder
`topic` und deren `decision.id` ein nichtleerer String ist. IDs werden nur an den
Rändern getrimmt; die erste Vorkommensreihenfolge des Input-Batches bleibt erhalten
und Duplikate entfallen. Fehlgeschlagene Items sowie Pipeline-`review_needed_items`
zählen nicht. `synthesis_required` entspricht exakt dem Wahrheitswert mindestens
einer betroffenen Projekt- oder Topic-ID.

`pipeline` übernimmt die Telemetrie seines Execute-Schritts. Bei keinem
Execute-Schritt oder einem Legacy-Execute-Handler ohne Telemetrie ist sie immer:

```json
{
  "affected_projects": [],
  "affected_topics": [],
  "synthesis_required": false
}
```

Die Telemetrie startet keine Synthese und verändert keine Wissensdateien.

### FR-06b-Syntheseziele

`synthesis_targets` ist ein optionales Feld jedes Execute-Manifest-Items. Ein
Target ist ausschließlich ein Objekt mit den nichtleeren String-Feldern `file`
und `type` sowie optional `recommended_action`, `task_anchor` und `section`
(jeweils ebenfalls nichtleere Strings); weitere Keys sind nicht zulässig. `type`
ist bewusst kein enger Enum, muss aber den stabilen Label-Raum
`[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*` erfüllen. `file` muss ein sicherer relativer POSIX-Pfad auf
eine `.md`-Datei sein, ohne Backslashes, absolute/Drive-/URL-Pfade oder `.`/`..`
Segmente. Jeder Pfadabschnitt darf keine Windows-reservierten Zeichen
`< > : " | ? *`, ASCII-Control-Zeichen oder abschließende Punkte/Leerzeichen
enthalten. Für `decision.kind: project` liegt er unter
`memory/references/projects/<decision.id>/...`, für `topic` entsprechend unter
`memory/references/topics/<decision.id>/...`; die zugehörige `decision.id` ist
ein nichtleerer, einzelner sicherer Slug. Andere Decision-Kinds dürfen nur
fehlende oder leere Targets haben.

Die autonome Klassifikation leitet keine Ziele aus Mailinhalten ab und erzeugt
kanonisch `synthesis_targets: []`. Ein Mensch oder LLM kann den Draft zwischen
`draft` und `execute` reviewbar anreichern. Execute prüft alle Items vor der
ersten Mailbox-, Log-, Evidence-, Index- oder Progress-Mutation. Erfolgreiche
Resultate enthalten die validierten Targets in Eingabereihenfolge; fehlgeschlagene
Items enthalten immer `[]`. Die Targets starten keine Synthese und verändern
keine Wissensdateien. `pipeline` übernimmt sie ausschließlich innerhalb seines
bestehenden `execute_summary`; FR-06b fügt keine neue Pipeline-Top-Level-Struktur
hinzu.

---

## Modus 3: `verify` (Integritäts- & Konsistenzprüfung)

### Beschreibung
Prüft für eine gegebene Liste von Message-IDs (oder ein zuvor ausgeführtes Manifest/Ergebnis) die Konsistenz über alle Speicher- und Protokollebenen:
1. Ist der Eintrag in `final-location-index.json` vorhanden und stimmt der finale Ordner?
2. Ist die Aktion in `action-log.jsonl` dokumentiert?
3. Ist der Nachweis in der entsprechenden `evidence/*.md` Datei festgehalten?
4. *(Optional via `check_folders: true`)*: Befindet sich die Mail tatsächlich mit einer gültigen Envelope-ID im Zielordner der Mailbox?

### Schema: Input Manifest (`batch-verify.json`)
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MailDeskBatchVerifyRequest",
  "type": "object",
  "properties": {
    "mode": { "type": "string", "enum": ["verify", "validate", "check"] },
    "message_ids": {
      "type": "array",
      "items": { "type": "string" }
    },
    "batch_file": { "type": "string" },
    "check_folders": { "type": "boolean", "default": false },
    "delete_input_on_success": { "type": "boolean", "default": true },
    "output_file": { "type": "string" }
  },
  "required": ["mode"]
}
```

### Beispiel Aufruf & Input
```json
{
  "mode": "verify",
  "message_ids": [
    "msg-2026-001@partner.example.org"
  ],
  "check_folders": false
}
```

### Beispiel Output
```json
{
  "action": "batch_runner",
  "success": true,
  "state": "Completed",
  "message": "Verified 1 message(s), all consistent.",
  "data": {
    "operation": "verify",
    "total_checked": 1,
    "all_consistent": true,
    "results": [
      {
        "message_id": "msg-2026-001@partner.example.org",
        "subject": "Statusbericht Arbeitspaket 4",
        "in_index": true,
        "indexed_folder": "Projekte/Project-Alpha",
        "indexed_envelope_id": "205",
        "in_action_log": true,
        "logged_folder": "Projekte/Project-Alpha",
        "in_evidence": true,
        "folder_verified": null,
        "current_envelope_id": null,
        "consistent": true
      }
    ]
  },
  "error": null
}
```

---

## Modus 4: `search` (Globales Finden & Lokalisieren)

### Beschreibung
Durchsucht parallel mehrere (oder alle) Mailbox-Ordner nach bestimmten Suchbegriffen (Betreff/Absender) oder gezielt nach einer Liste von `Message-ID`s. Ermöglicht schnelles Wiederfinden verschobener Nachrichten und Auslesen der aktuellen `envelope_id`.

### Schema: Input Manifest (`batch-search.json`)
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MailDeskBatchSearchRequest",
  "type": "object",
  "properties": {
    "mode": { "type": "string", "enum": ["search", "locate", "find"] },
    "query": { "type": "string" },
    "message_ids": {
      "type": "array",
      "items": { "type": "string" }
    },
    "folders": {
      "type": "array",
      "items": { "type": "string" }
    },
    "page_size": { "type": "integer", "default": 50 },
    "threads": { "type": "integer", "default": 4 },
    "output_file": { "type": "string" },
    "delete_input_on_success": { "type": "boolean", "default": true }
  },
  "required": ["mode"]
}
```

### Beispiel Aufruf & Input
```json
{
  "mode": "search",
  "query": "Statusbericht",
  "folders": ["INBOX", "Projekte/Project-Alpha", "Newsletter"]
}
```

### Beispiel Output
```json
{
  "action": "batch_runner",
  "success": true,
  "state": "Completed",
  "message": "Found 1 match(es).",
  "data": {
    "operation": "search",
    "total_found": 1,
    "matches": [
      {
        "folder": "Projekte/Project-Alpha",
        "envelope_id": "205",
        "message_id": "msg-2026-001@partner.example.org",
        "subject": "Statusbericht Arbeitspaket 4",
        "from": "Dr. Alex Beispiel alex@partner.example.org",
        "date": "2026-01-06 14:20+01:00"
      }
    ]
  },
  "error": null
}
```

---

## Modus 5: `resolve` (Batch-Fallauflösung & Archivierung)

### Beschreibung
Schließt und archiviert offene Einträge aus `replies-needed.jsonl` oder `pending-review.jsonl` im Batch. Aktualisierte Einträge werden mit Timestamp, Status und Begründung in das kalenderwochenbasierte Archiv (`data/mail-desk/archive/YYYY-Www/`) verschoben und aus den aktiven Trackingdateien entfernt.

### Schema: Input Manifest (`batch-resolve.json`)
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "MailDeskBatchResolveRequest",
  "type": "object",
  "properties": {
    "mode": { "type": "string", "enum": ["resolve", "archive"] },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "message_id": { "type": "string" },
          "status": { "type": "string", "default": "resolved" },
          "resolution": { "type": "string" },
          "resolved_by_message_id": { "type": "string" }
        },
        "required": ["message_id", "resolution"]
      }
    },
    "delete_input_on_success": { "type": "boolean", "default": true }
  },
  "required": ["mode", "items"]
}
```

### Beispiel Aufruf & Input
```json
{
  "mode": "resolve",
  "items": [
    {
      "message_id": "msg-2026-001@partner.example.org",
      "status": "resolved",
      "resolution": "Abstimmung telefonisch am 14.01. erfolgt, keine weitere Aktion nötig.",
      "resolved_by_message_id": null
    }
  ]
}
```

### Beispiel Output
```json
{
  "action": "batch_runner",
  "success": true,
  "state": "Completed",
  "message": "Resolved 1 case(s), all succeeded.",
  "data": {
    "operation": "resolve",
    "total_processed": 1,
    "all_resolved": true,
    "results": [
      {
        "resolved": true,
        "message_id": "msg-2026-001@partner.example.org",
        "source_file": "replies-needed.jsonl",
        "archived_to": "data/mail-desk/archive/2026-W03/replies-needed.jsonl",
        "item": {
          "timestamp": "2026-01-12T10:00:00Z",
          "envelope_id": "205",
          "message_id": "msg-2026-001@partner.example.org",
          "subject": "Statusbericht Arbeitspaket 4",
          "status": "resolved",
          "resolution": "Abstimmung telefonisch am 14.01. erfolgt, keine weitere Aktion nötig.",
          "closed_at": "2026-01-14T15:30:00Z"
        }
      }
    ],
    "input_file_deleted": true
  },
  "error": null
}
```

---

## Live-Fortschritts-Monitoring & Zeitschätzung (`core/progress.py`)

- Bei allen Batch-Läufen (`--draft`, `--execute`, `--pipeline`, `--inspect`) führt der Runner eine atomare Statusdatei `data/mail-desk/runner-progress.json` (Schema: [`log-schema.md`](log-schema.md)) mit Zählern, Prozentwert, aktuellen Arbeitsschritten und deterministischer Restzeitschätzung (ETA).
- Ungepuffertes Live-Streaming in stdout/`task.log`: Jeder Schritt wird sofort sichtbar geloggt (`[11/20 - 55.0%] Env 7081: 'Antw: Re: ATAEL...' (16.7s | ETA: 183s)`).
- **Verbindliche Timer-Regel (60s $\rightarrow$ 75%-ETA-Formel):**
  1. Batch im Hintergrund starten mit initialem Timer von **60 Sekunden** (Warmup-Phase für realistische $\bar{T}_{\text{item}}$-Messung über mehrere IMAP-Operationen hinweg).
  2. Beim Aufwachen `runner-progress.json` lesen:
     - Wenn `status == "completed"` $\rightarrow$ Batch abgeschlossen, Vollzugsmeldung.
     - Wenn `status == "running"` $\rightarrow$ nächsten Timer auf $\Delta t = \max(30, \min(0.75 \times \text{estimated\_remaining\_seconds}, 360))$ Sekunden setzen.
     - Wiederholen bis zum Abschluss.
  3. Reduziert unnötiges Polling drastisch und schont Context Window und Systemressourcen bei maximaler Termintreue.

---

## Final-Index- und Batch-Importregeln

- Die Backend-Location ist immer die nach Routing verifizierte finale Location; niemals eine Quell- oder Zwischenlocation speichern.
- Ohne verifizierte finale Backend-Location kein `upsert-final`; spätere Korrekturen nur über `patch`.
- JSONL-Batches sind temporäre Input-Artefakte und nie die Source of Truth. Jede Zeile enthält genau einen bereits verifizierten finalen Eintrag.
- Nach erfolgreichem Import verwendete `final-index-batch-*.jsonl` löschen.
- Backend-spezifische Verifikation und Felder stehen im jeweiligen Adapter ([`backends/gmail.md`](backends/gmail.md) bzw. [`backends/himalaya.md`](backends/himalaya.md)); Scriptzugriffsregeln in [`cli-operations.md`](cli-operations.md).

---

## Fehlerbehandlung & Sicherheit

1. **Kein Datenverlust:** Schlägt auch nur ein Einzelschritt (z. B. Routing oder Index-Write) fehl, gibt das Skript `success: false` (mit kanonischem Fehler-Envelope) zurück und das Eingabemanifest **bleibt zur Fehleranalyse erhalten** (wird nicht gelöscht).
2. **Atomare Index-Transaktion:** `final-location-index.json` wird über eine temporäre Zwischendatei (`.tmp`) geschrieben und anschließend atomar ersetzt, um Korruption bei Prozessabbrüchen zu verhindern.
3. **Plattformunabhängiges UTF-8:** Standard-Streams (`stdout`/`stderr`) und Dateilese-/schreiboperationen sind strikt auf UTF-8 konfiguriert (verhindert Windows `charmap`-Codierungsfehler bei Umlauten oder Sonderzeichen).
4. **Fehlertolerante Subprozess-Ausführung:** `subprocess.run(..., errors="replace")` und Timeouts auf Einzelebene stellen sicher, dass langsame IMAP-Verbindungen oder fehlerhafte Zeichensätze nicht den gesamten Batch-Lauf blockieren.
