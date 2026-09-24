# Feature Requests — aktiver Backlog

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
| `FR-09` | ⬜ geplant; Human Gate offen | FR-08 und `attachment_filing_candidate` Schema 1 abgeschlossen | Nach ausdrücklicher Freigabe: `MD-P1` | [`docs/features/FR-09.md`](docs/features/FR-09.md) |
| `FR-10` | ⬜ geplant | Temporäre manifestgebundene Host-Ausführung dokumentiert | `MD-G1` | [`docs/features/FR-10.md`](docs/features/FR-10.md) |
| `FR-12` | ⬜ geplant | Identifikation des 2.200-Zeilen-Monolithen `convert_cloud_docs.py` in System Map | `CA-M1` | [`docs/features/FR-12.md`](docs/features/FR-12.md) |
| `FR-19` | ⬜ geplant | Befund Batch 2026-W39/3 (Env 9428) dokumentiert | `MD-RC1` (stale Records nachverifizieren) | [`docs/features/FR-19.md`](docs/features/FR-19.md) |
| `FR-20` | ⬜ geplant | Befund Batch 2026-W39/3 (Env 9438) dokumentiert; Inline-Quota-Festlegung präzisiert | `MD-A3` (Inline vs. Datei) | [`docs/features/FR-20.md`](docs/features/FR-20.md) |
| `FR-23` | ⬜ geplant | Befund Batch 2026-W39/4 (Env 9451) dokumentiert | `MD-L1` (Lease-Delegation) | [`docs/features/FR-23.md`](docs/features/FR-23.md) |
| `FR-24` | ⬜ geplant | Befund Batch 2026-W39/4: Batch-Lauf ohne Fachvertrags-Ladung — drei Werkzeugverletzungen (Final-Index ad-hoc, Himalaya ad-hoc, Katalogpflege inline); Pipeline-SOP verweist nicht auf SKILL.md/Adapter/cli-operations.md; Skill-Description routet Batch-Work weg | `MD-R9` (Pflicht-Skill-Routing: Description-Schärfung + Pflicht-Ladeblock + Consumer-Migration) | [`docs/features/FR-24.md`](docs/features/FR-24.md) |

Vollständig abgeschlossene FRs (FR-01–08, 11, 13–18, 21, 22) stehen im
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