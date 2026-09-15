# Feature Requests — aktiver Backlog

Diese Datei enthält nur laufende oder geplante Feature Requests sowie deren
verbindliche Paketkarten.

### Zusammenspiel der Dokumente

- [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md): Aktiver Backlog mit Status, Paketkarten,
  Spezifikationen und Ausführungsprofilen (SSOT für laufende/geplante Arbeit).
- [`FEATURE-REQUEST-PROGRESS.md`](FEATURE-REQUEST-PROGRESS.md): Ephemere Arbeitsdatei für den
  Implementierungsagenten und das Code-Review. Dokumentiert aktuellen Durchführungsstand,
  Mini-Walkthroughs, Diffs und Verifikationsnachweise des aktiven Pakets; wird nach
  Abnahme bereinigt.
- [`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md): Langzeit-Archiv für vollständig
  abgeschlossene und abgenommene Feature Requests.
- [`COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md`](COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md):
  Bewertet normative Konformität zur Agent-Architektur; kein Feature-Backlog.

## Status und Reihenfolge

| ID | Status | Erledigter Teil | Nächstes Paket |
| --- | --- | --- | --- |
| `FR-09` | ⬜ geplant; Human Gate offen | FR-08 und `attachment_filing_candidate` Schema 1 abgeschlossen | Nach ausdrücklicher Freigabe: `MD-P1` |

```text
Human Gate → MD-P1 → MD-P2 → MD-P3
```

## Ausführungsprofil für Coding Agents

Jedes Paket erhält eine frische Coding-Session. Innerhalb des Pakets wird linear
gearbeitet: Verträge lesen, Tests zuerst, kleinste Implementierung, fokussierte
Tests, Gesamtsuite, unabhängiges Review. Keine parallelen Agents auf denselben
Mail-Desk-Dateien. Der Implementierungsagent commitet nicht; nach grünem Review
wird genau ein Paket samt Statusupdate committed.

Vor jedem Paket vollständig lesen:

1. `skills/mail-desk/SKILL.md`
2. `skills/mail-desk/references/backends/himalaya.md`
3. die Paketkarte unten
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

## FR-09: Human-gated Cloud-Promotion

**Status:** ⬜ Technische Vorbedingungen erfüllt; ausdrückliches Human Gate für
`MD-P1` noch offen. FR-08 ist abgeschlossen und der
`attachment_filing_candidate` als Schema 1 eingefroren. Jede Promotion benötigt
Workspace-Lock, frische Storage-/Filemap-Preconditions und eine hashgebundene
Human-Receipt. Archiv- und Read-only-Storages werden abgewiesen.

### MD-P1 — Approval und read-only Preflight

**Agent:** Terra-high, frische Session. Neu: `scripts/core/attachment_promotion.py`
und `tests/test_maildesk_attachment_promotion_mdp1.py`.

Receipt bindet Schema, Candidate-/Quellhash, Storage, Zielpfad/-name,
Filemap-Snapshot und Ablaufzeit. Preflight prüft Katalog, Schreibbarkeit, Lock,
Quarantäne-Hash, Zielgrenze, Parent, Dublette und Kollision. Ausgabe nur `ready`,
`already_present` oder Stopcode. Tests decken Manipulation, Ablauf, Drift, Lock,
Archiv/Read-only, Traversal, Parent, Dublette, Kollision und Nullmutation ab.

### MD-P2 — Atomarer Storage-Writer

**Agent:** Terra-high, eigene Session. Genau einen MD-P1-freigegebenen Transfer
ausführen; keine Zielwahl, keine Konvertierung. Preflight unmittelbar wiederholen;
Sibling-Temp, Flush/Close, Zielhash, atomare Promotion ohne Überschreiben. Quelle
erst nach Verify entfernen. Journal macht Interrupt und Retry idempotent. Tests:
Ziel-Race, Disk-/Hashfehler, Interrupt jeder Phase, Retry, Pfad-/Lockverlust.

### MD-P3 — Cloud-Atlas-Handoff und Mirror

**Agent:** Terra-high, eigene Session. Mail-Desk schreibt `filemap.json` nicht
selbst. Ein hashgebundener `cloud_atlas_refresh_handoff` referenziert Storage,
Zielpfad/-hash und Promotion-Journal. Cloud-Atlas validiert eigene Grenzen und
aktualisiert differenziell. Refresh-Fehler ergeben
`promotion_completed_refresh_pending` ohne zweite Promotion. Tests: inkrementeller
Refresh, kein unnötiger Full-Scan, Drift, Tool fehlt, Abbruch/Retry und
`untrusted_external`-Metadaten.
