# Event-Dossier-Fassade (GitHub #8)

## Grenze und Aufruf

`scripts/mail_desk_event_dossier.py` exportiert
`process_request(request, workspace_root=...)`. Die fünf Stages sind **read-only**:
Sie komponieren Requests oder verifizieren vorhandene Outputs; sie führen keine
Mailbox-, Attachment-, Event-, Cloud- oder Task-Writes aus. Sie erzeugen keine
Freigaben. Der bestehende Batch-Runner bleibt der operative Owner; der externe
Event-Writer ist noch nicht implementiert und wird separat in
[#14](https://github.com/martinderm/office-intelligence/issues/14) verfolgt.

```text
python -B <bundle>/skills/mail-desk/scripts/mail_desk_event_dossier.py \
  --request data/mail-desk/event-request.json --workspace . --json
```

`--request` ist ein expliziter Dateipfad (relative Pfade beziehen sich auf das
Prozess-CWD), `--workspace` bezeichnet den Consumer-Workspace. stdout enthält
einen kanonischen Sechs-Feld-Envelope; Erfolg Exit 0, Fehler Exit 2. Kein
Traceback und kein Echo externer Inhalte/Pfade im Fehlerfall. API-Fehler sind
`{"ok": false, "error": {"code": "invalid_event_dossier_request", ...}}`.
Keine Persistenz in diesem Bundle. Der Consumer speichert Requests/Receipts bei
Bedarf unter `data/`; operative Writer benötigen dessen Workspace-Lock.

Alle Requests enthalten `schema_version: 1` (kein Boolean) und `stage`.
Unbekannte Stages/Felder, doppelte JSON-Keys, NaN/Infinity und Eingaben über
2 MiB scheitern. JSON-Hashes sind Integritätsbindungen, **kein Beweis der
Echtheit von Mailbox-Snapshots, Writer-Receipts oder menschlicher Autorisierung**.
Caller müssen Quellen/Account live revalidieren und Freigaben extern einholen.

## `plan`

Pflichtfeld `scope` (auch für review/apply/handoff):

```json
{
  "account": "synthetic",
  "folders": ["INBOX", "Archive"],
  "terms": ["workshop"],
  "scan_limit_per_folder": 20,
  "inspect_limit": 5
}
```

Explizit, nicht leer, keine Duplikate: maximal 20 Ordner und 20 Begriffe,
Account maximal 100 Zeichen, Ordner/Begriffe maximal 200 Zeichen.
Positive Integer-Limits: Scan bis 500, Inspection bis 100 und nie über dem
gesamten Envelope-Budget. `search_requests` enthält je Ordner/Begriff einen
kanonischen Batch-`search`-Request (`folders`, `query`, `page_size`); `folder`
ist zusätzlich ein Anzeige-Locator. Jeder Begriff ist eine separate literale
Query, keine erfundene Boolean-Suchsyntax. Suchtreffer müssen insgesamt auf
`inspect_limit` begrenzt werden. Optionale `inspect_ids` sind eindeutige
Envelope-ID-Strings und benötigen genau einen expliziten Ordner; IDs ohne
Ordnerbindung werden nie geraten. `inspect_requests` sind read-Operationen
für den Client, keine neuen Batch-Modi.

`completeness.exhaustive` ist immer `false`, `scan_basis` immer
`first_page_envelopes`; Account-Bindung bleibt
`pending_workspace_revalidation`. Keine Behauptung einer vollständigen Suche.

## `review` und `apply`

Pflichtfelder: `scope`, `sources`, `operative_request`; optional
`attachment_requests` (Default `[]`). Jede Quelle enthält genau:

- `search`: `account`, `folder`, `envelope_id`, `message_id` (Strings).
- `read`: dieselben Locator-Felder plus `content_sha256`, `references`
  (Message-ID-Liste), `in_reply_to` (Message-ID oder null).
- `attachments`: inventarisierte MIME-Anhänge mit den Locator-Feldern plus
  `filename`, `original_filename` (literale Basenames), `mime_type`,
  `size_bytes`, `sha256`, `part_locator`, `fetch_status: "available"`,
  `provenance: "rfc822_mime"`, `policy_status: "allowed"`.
- `calendar_objects`: `uid`, `sequence` (nichtnegativer Integer),
  `recurrence_id` (String oder null), `content_sha256`.

Hashes sind lowercase SHA-256. Account/Ordner/Envelope/normalisierte Message-ID
müssen zwischen Search, Read und Anhängen exakt übereinstimmen. Konfliktierende
Locatoren/MIME-Parts oder dieselbe Message-ID mit anderem Inhalt scheitern.
Messages werden nach normalisierter Identität **und konsistentem Content-Hash**
dedupliziert, nicht allein nach gleichem Body-Hash unterschiedlicher Messages.
Alle Mail-Locations bleiben erhalten; Anhänge nach Hash mit allen MIME-Provenienzen.
Kalenderdaten gehen nicht verloren. Unterschiedliche Kalenderstände sowie
fehlende Thread-Parents liefern `review_required: true`; apply/handoff blockieren.
Nach fachlicher Klärung ist ein neuer konsistenter Source-Snapshot erforderlich;
es gibt keinen bypassenden `reviewed: true`-Schalter.

`operative_request` ist ein unverändert zurückgegebener, enger `dossier_apply`-
Request für **ein Projekt**, mit `account`, `project`, `execute_request`,
`review` und `delete_input_on_success: false`. Die ausgewählten INBOX-Items
müssen aus den inspizierten Locatoren stammen, zum selben Projekt gehören und
per `copy_as_move` in einen einheitlichen Zielordner routen. Die Fassade erfindet
keinen Katalogeintrag. Der Batch-Owner prüft vor Ausführung den tatsächlichen
Projektkatalog, Zielordner, Account und Workspace-Lock erneut.

`review_binding.inner_sha256` verwendet `core.batch_contract` (ohne inneres
`review`). Zuerst wird dieses innere Review extern freigegeben. Erst danach
bindet `outer_sha256` über `core.modes.dossier_apply` den **gesamten finalen
inneren Execute-Request einschließlich innerem Receipt**. Pending Reviews
bleiben pending; vorhandene approved Receipts werden exakt geprüft. Die Fassade
fügt kein Approval hinzu und verändert den operativen Request nie.

`apply` setzt beide passenden approved Receipts voraus und gibt den unveränderten
`batch_request` mit `dispatch_required: true`, **`applied: false`** zurück. Diesen
Request separat an den kanonischen Batch-Runner übergeben. Weder erfolgreiche
Komposition noch Hash-Validität behaupten eine erfolgte Mailbox-Ausführung.
Optionale Attachment-Requests müssen exakt zu inventarisierten Anhängen passen;
die Fassade lädt oder promotet keine Anhänge.

## `handoff`

Pflichtfelder: `scope`, `sources`, `apply_request`, `apply_result`,
`apply_result_sha256`, `event`. Nur vollständig erfolgreicher Execute **und**
Verify mit exakt allen genehmigten Quellen in gleicher Reihenfolge, unverändertem
Approval-Receipt und genehmigten Synthesezielen erlauben einen Handoff.
`core.modes.dossier_synthesis._require_completed_apply` bleibt der gemeinsame
Validator für die Ergebnis-Kette. Nicht angewendete Suchkandidaten werden nicht
in den Work-Order aufgenommen.

`event`: `owner_kind: "project"`, `owner_id` gleich operativem Projekt,
`id` (Slug), `title`, ISO-`start`, optional `end` nicht vor `start`.
Nur der Projekt-Dossierpfad wird hier komponiert; vorhandenes Topic-/Subtopic-
Event-Matching bleibt unverändert. Der Event-Writer muss Owner und Event gemäß
seinem Katalogvertrag erneut prüfen.

Ausgabe `work_order`: `schema_version: 1`, `owner: "event-documentation"`,
`status: "pending_content_review"`, `event`, `sources`, `attachments`,
`completeness` und `source_snapshot_sha256` über exakt
`{"sources": ..., "attachments": ...}`. Hinzu kommen auf Fassade-Ebene
`work_order_sha256` und `apply_result_sha256`. Keine Inhaltsfreigabe.

## `report` und externe Writer-Grenze

Pflichtfelder: `artifacts` (explizite vollständige Pfadliste), der unveränderte
`work_order` und ein externes `output_receipt`:

```json
{
  "schema_version": 1,
  "work_order_sha256": "<SHA256 des exakten Work-Orders>",
  "source_snapshot_sha256": "<SHA256 seines exakten Source-Snapshots>",
  "outputs": [{"path": "memory/references/projects/demo/events/workshop/index.md", "sha256": "<SHA256 der Datei-Bytes>"}]
}
```

Dies ist die explizite Integrationsgrenze für #14; kein existierender Writer
wird behauptet. Der historische E2E-Pin aus `0896334` wurde nachvollziehbar
auf synthetische Writer-Bytes mit echtem Receipt umgestellt. Alle übrigen
historischen Verhaltenspins bleiben, zusätzliche Negativtests härten die Grenze.

Report prüft Source-Snapshot, Work-Order-Bindung, exakte Output-Menge und jeden
Dateihash. `index.md` ist erforderlich; weitere Outputs sind ausschließlich
im exakten Projekt-/Event-Scope unter `memory/references/` oder
`memory/evidence/` zulässig. Absolute/Traversal-/ADS-Pfade, fehlende Dateien,
Symlinks/Reparse-Points und Hardlinks scheitern. Der Report ist eine lokale
Integritätsprüfung, keine Race-freie oder kryptographisch authentifizierte
Writer-Attestation. `content_approved: false` und
`content_review: "human_content_review_required"` bleiben immer erhalten.
