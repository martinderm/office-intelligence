# Feature Requests — aktiver Backlog

> **Neue Feature Requests ab sofort als GitHub Issues** im Repo
> `martinderm/office-intelligence` anlegen (nicht mehr als FR-Record-File in
> `docs/features/`). Diese Datei bleibt als Katalog der historischen
> FR-Records (FR-01 …) bestehen; neue Fälle werden nur noch als Issue erfasst
> und verfolgt.

Issue-Nachweis (kein neuer FR-Record): [#2 — Cloud-Atlas-Verzeichnisausschlüsse](https://github.com/martinderm/office-intelligence/issues/2).
Vertrag und Runtime-Grenzen: `skills/cloud-atlas/SKILL.md` und L2-System-Map.

Issue-Nachweis: [#8 — Event-Dossier-Fassade](https://github.com/martinderm/office-intelligence/issues/8),
Vertrag `skills/mail-desk/references/event-dossier.md`;
separate, vorab bestätigte Writer-Lücke
[#14](https://github.com/martinderm/office-intelligence/issues/14).
Keine neuen FR-Record-Files.

Issue-Nachweis (kein neuer FR-Record): [#9 — Attachment-Basename Contract-Reconcile](https://github.com/martinderm/office-intelligence/issues/9).
Vertrag und Runtime-Grenzen: Mail-Desk L2-System-Map §30 (Leserichtung je
Pin-Gruppe) und `skills/mail-desk/docs/system-map/effects.md` §4.

Issue-Nachweis (kein neuer FR-Record): [#20 — PDF-AcroForm-Absicherung](https://github.com/martinderm/office-intelligence/issues/20).
Fix der Review-Findings der isolierten Feature-Erweiterung (Branch
`feat/cloud-atlas-pdf-form-fields`) in zwei Runden (Fix-Runde 1 + unabhängiges
GLM-Go-Review A1–A7). Vertrag und Runtime-Grenzen:
`skills/cloud-atlas/SKILL.md` (AcroForm-Capability-Vertrag),
`skills/cloud-atlas/docs/system-map/` (README/effects/objects/processes) und das
additive Filemap-Objekt `files[<quelle>].form_fields` (bewusst **außerhalb** des
kanonischen Mirror-Frontmatters, als **reserviertes generated Feld** gegen
Curation-Forging geschützt und nur bei unverändertem Quell-Hash weitergereicht).
Finale Cloud-Atlas-Suite: 241 Tests / 134 Subtests bestanden, 0 Skips; unabhängiges
GLM-Go-Re-Review ohne blockierende Findings. Übergabe für PR-Prüfung, kein Merge.

Katalog über laufende und geplante Feature Requests. Routing und Status-Tabelle
liegen hier; die verbindliche Spezifikation (Paketkarte, SSOT) jedes FR liegt in
seinem Record-File unter [`docs/features/`](docs/features/).

**ICM-Form:** Record library (Form 3). Jedes FR ist ein Record mit YAML-Frontmatter
(`id`, `type: feature-request`, `status`, `packages`, `next_package`, `blocker`).
Der Record ist die eine Heimat jedes FR-Fakts; diese Datei ist der Katalog und
trägt keinen Content.

### Zusammenspiel der Dokumente

- [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md) (diese Datei): Reiner Katalog mit
  Status-Tabelle und Reihenfolge — kein Spec-Inhalt.
- [`docs/features/FR-<n>.md`](docs/features/): Record-Files der aktiven FRs
  (SSOT je FR: Paketkarte, Ziel & Invarianten, Abnahme, Umsetzungsnachweis).
- [`FEATURE-REQUEST-PROGRESS.md`](FEATURE-REQUEST-PROGRESS.md): Ephemeres
  Arbeitsdokument (L4-Produkt) für Implementierungsagent und Code-Review; wird
  nach Abnahme bereinigt.
- [`docs/features/_archive.md`](docs/features/_archive.md) (Root-Stub:
  `FEATURE-REQUEST-ARCHIVE.md`): Langzeit-Archiv für
  vollständig abgeschlossene und abgenommene Feature Requests (Archivstatus-Tabelle
  + FR-Sektionen).
- [`COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md`](COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md):
  Bewertet normative Konformität zur Agent-Architektur; kein Feature-Backlog.
- Registriert in der ICM System Map (Form 6): [`docs/system-map/objects.md`](docs/system-map/objects.md)
  § 3.4 Feature-Request-System.

## Status und Reihenfolge

| ID | Status | Erledigter Teil | Nächstes Paket | Record |
| --- | --- | --- | --- | --- |
| `FR-10` | ⬜ geplant | Temporäre manifestgebundene Host-Ausführung dokumentiert | `MD-G1` | [`docs/features/FR-10.md`](docs/features/FR-10.md) |
| `FR-12` | ⬜ geplant | Identifikation des 2.200-Zeilen-Monolithen `convert_cloud_docs.py` in System Map | `CA-M1` | [`docs/features/FR-12.md`](docs/features/FR-12.md) |
| `FR-25` | ⬜ geplant (low) | Aus TODO-Historik disponiert: Chunk-Modus für historische Großaufarbeitung (total_count/chunk_size, Index-Sicherung je Chunk) | `MD-P1`/`MD-P2` | [`docs/features/FR-25.md`](docs/features/FR-25.md) |
| `FR-28` | 🔄 in Umsetzung (DOC-P1 läuft) | GitHub issue #4: Batch-Doku-Lücken A–E aus realem Batch + stdlib-Signatur-Helfer `mail_desk_sign_manifest.py` | Review nach DOC-P1 | [`docs/features/FR-28.md`](docs/features/FR-28.md) |

Vollständig abgeschlossene FRs (FR-01–09, 11, 13–20, 21–26) stehen im
Archiv [`docs/features/_archive.md`](docs/features/_archive.md); ihre Paketkarten sind
dort Teil der Sektionen.

```text
Human Gate → MD-P1 → MD-P2 → MD-P3
```

FR-10 ist unabhängig von FR-09 und sollte vor einem breiteren produktiven
Mailbox-Betrieb umgesetzt werden.

## Ausführungsprofil für Coding Agents

Jedes Paket erhält eine frische Coding-Session. Innerhalb des Pakets wird linear
gearbeitet: Verträge lesen, Tests zuerst, kleinste Implementierung, fokussierte
Tests, Gesamtsuite, unabhängiges Review. Keine parallelen Agents auf denselben
Mail-Desk-Dateien. Der Implementierungsagent commitet nicht; nach grünem Review
wird genau ein Paket samt Statusupdate committed.

Vor jedem Paket vollständig lesen:

1. `skills/mail-desk/SKILL.md`
2. `skills/mail-desk/references/backends/himalaya.md`
3. den Record des FR unter `docs/features/` (Paketkarte, SSOT)
4. die genannten Code- und Testdateien

Vor Mutationen ist der Workspace-Lock erforderlich. Mailboxzugriffe bleiben
JSON-manifestgebunden. Tests blockieren reale Himalaya-, Cloud- und Office-Prozesse.
Mail- und Anhangsinhalte bleiben `untrusted_external`.

Gemeinsame Abnahme:

```powershell
python -B skills/mail-desk/tests/<paket_test>.py
python -B -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
python -B -m compileall -q skills/mail-desk
python <quick_validate.py> skills/mail-desk
git diff --check
```

Berichtsort nach jedem Paket in `FEATURE-REQUEST-PROGRESS.md`: geänderte Dateien,
öffentliche Verträge, Verifikationsergebnisse.
