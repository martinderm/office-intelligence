# Meeting-Desk - L2 System Map (ICM Form 6)

**Standalone-Guard (#38):** Der explizite Writer-/CLI-Installationspfad geht dem
gemeinsamen Operator-Environment `WORKSPACE_LOCK_ROOT` vor; danach folgt der
kompatible Alias `MEETING_DESK_WORKSPACE_LOCK_ROOT`, zuletzt der Installations-
Sibling des Bundles. Fehlende Installation liefert begrenzte Setup-Hilfe auf
`WORKSPACE_LOCK_ROOT` / `--workspace-lock-root` und die Bundle-README. Kein
CWD-/Payload-Fallback; Ownership, Guard und Recovery bleiben unverändert.

> **Metrik (DOC-M1):** 15 Dateien (`git ls-files --cached --others --exclude-standard`,
> 2026-10-21) · 99 Testmethoden in 2 Modulen (`test_ingest_meeting.py`,
> `test_build_clean_transcript.py`) plus Crash-Treiber und Fixture-Paket.
> Diese Kopfzeile ist die kanonische Metrik-SSOT; L1 nennt nur Dateizahl + Anker.
>
> **Typ:** ICM Form 6, L2 des Sub-Skills `meeting-desk`
> **Gültig für:** `skills/meeting-desk/` (Bundle-relativ)

---

## 1. Rolle und Grenze

`meeting-desk` besitzt den zustandsbehafteten Meeting-Ingest: Record-Validierung,
vertrauenswürdiges Routing, Objektregister, Artefaktpublikation, Journal,
Ownership und Recovery. **Eine** Schreibgrenze: `scripts/ingest_meeting.py`.

```
Adapter (fireflies/ticnote/zoom/manual/vimeo-webvtt)
   │  meeting-record/1 (UTF-8 JSON, stdin oder Datei)
   ▼
ingest_meeting.py ── validate/bound ── catalog routing (trusted flags)
   │
   ├── memory/evidence/{topics|projects|...}/meetings/<stem>.{summary,transcript,transcript.raw}.md
   ├── memory/evidence/meetings/meetings.json        (Objektregister, zuletzt)
   └── data/meeting-desk/{journal/,ownership.json}   (Staging/Recovery/Ownership)
```

Nicht im Besitz des Writers: API-/OAuth-Transport, Klassifikationsfreigabe,
Content-Approval, Task-Dispatch, Mail-/Cloud-Mutationen, Consumer-Migration.
Ad-hoc-Exporte bleiben stdout oder `.meeting-export.json` unter `data/`.

---

## 2. Nomen

| Objekt | Ort / Schema | Wesentliche Regeln |
| :--- | :--- | :--- |
| `meeting-record/1` | Transport, stdin/Datei/`--local` | exakte Root-/Blockfelder; Erweiterungen nur inert in `source_metadata`; strikt begrenztes JSON; beobachtete Start-Basis erlaubt |
| Register | `memory/evidence/meetings/meetings.json` | Objekt mit `meetings`-Liste; Root-Extras/untouched entries bleiben; Legacy-Array = Rejection, keine Migration; gematchte stabile ID/Pfade bleiben erhalten |
| Registereintrag | 25 kanonische Felder + erhaltene Legacy-/Unknown-Felder | stabile Identität (`meeting_id`/`id`/source pair), opake Unicode-IDs exakt (≤512, keine Steuerzeichen), Dateinamen nur per Hash; keine Re-ID; unsourced Legacy nur über belegten Quell-Präfix |
| Summary/Transkript | `*.summary.md`, `*.transcript.md` | generierte Frontmatter, byte-exakter Body, `status: provisional` bis menschliche Freigabe; `null` = nicht geliefert, nie Löschen |
| Raw | `*.transcript.raw.md` | byte-exakt, kein Frontmatter, nach Erstpublikation unveränderlich; auch ohne Clean-Transcript eigenständig registriert; `null` lässt Bestand unverändert |
| Local sources | `--local --summary/--transcript/--raw` in kanonischen Zonen | gleicher Record/Writer; Plan+Hash, byte-exakt; kindliche Links/Junctions/Hardlinks und Writer-State-/Katalogpfade vor Planung abgewiesen; Löschsteps journal-/hashgebunden nach Register |
| Ownership | `data/meeting-desk/ownership.json` (`meeting-desk-ownership/1`) | Pfad → Meeting-ID, SHA-256, Größe, Art, Writer-Version, Zeit |
| Journal | `data/meeting-desk/journal/<id>.journal.json` (`meeting-desk-journal/1`) | Integritätsdigest, Prestate/Poststate, Steps, Stage-Hashes, Workspace-Fingerprint |
| Staging | `data/meeting-desk/journal/<id>/stage/NNN.bin` | geplante Zielbytes, vor Publikation gehasht |

Details: [`references/data-model.md`](../../references/data-model.md).

---

## 3. Zustandsmaschine der Publikation

```
Preflight (read-only)
  record → identity match → routing/catalog → paths/hashes → no open journal
        │ Fehler
        ▼
     Rejected (Exit 2, keine Mutation)
        │ ok
        ▼
  Lease ownership (allow_legacy=False)
        ▼
  Stage bytes → journal(prepared, integrity+hashes) → journal(publishing)
        ▼
  step 1..n: re-check lease → publish → journal(step published)
        │   Dateien: no-clobber create | hash-bound replace | delete nur Cleanup
        │   Ownership vor Register
        ▼
  register publish (letzter Erstell-/Update-Schritt)
        ▼
  cleanup alter Pfade (nur Writer-eigen, hash-gebunden)
        ▼
  journal(completed) → journal identity/hash recheck → Journal/Stage entfernen → Applied (Exit 0)

Jeder Fehler nach Journalerstellung → journal(failed), Exit 3 recovery_required.
Unveränderter Reingest → Unchanged (Exit 0), kein Write.
```

---

## 4. Routing und Pfade

| Auswahl | Ziel |
| :--- | :--- |
| `--topic` + `--event` | `memory/evidence/topics/<topic>/events/<event>/meetings/` |
| `--project` + `--event` | `memory/evidence/projects/<project>/events/<event>/meetings/` |
| `--topic` | `memory/evidence/topics/<topic>/meetings/` |
| `--project` | `memory/evidence/projects/<project>/meetings/` |
| keine (neu) | `memory/evidence/meetings/unrouted/`, `classification_status: unmapped` |
| keine (bestehend klassifiziert) | vorhandene Pfade/Zuordnung bleiben unverändert |
| explizites Rerouting | nur vollständiger Artefakt-Snapshot; unvollständig = Rejection vor Mutation; vollständig = Move + Cleanup erst nach Register |

Topic gewinnt bei Doppelzuordnung; Event muss im Katalog des gewählten Owners
stehen. Slugs werden gegen `topics.json`/`projects.json` validiert. Record-Hints
sind untrusted. Dateiname: `<datum|undated>-<title-slug>-<sha256(meeting_id)[:8]>`.

---

## 5. Sicherheits- und Integritätsgates

- **Record/JSON:** Größe (1 MiB), Tiefe 32, 200 000 Nodes, Duplicate Keys,
  NaN/Infinity, Integer-Überlauf, Surrogates, NUL, unbekannte Felder.
- **Register:** 16 MiB, max. 20 000 Einträge, Objekt+Liste, keine stillen
  Reparaturen; mehrdeutige Identität oder Quellsystem-Widerspruch stoppt.
- **Pfade:** kanonische Zonen, kein Traversal, keine Symlinks/Junctions/
  Reparse-Points, keine Hardlink-Aliase, keine Windows-Reserved/ADS/
  Trailing-Dot/Space; Bundle ist kein Workspace.
- **Ownership:** generierte Updates nur gegen den exakt gebundenen Hash; nicht
  Writer-eigene Dateien werden nicht überschrieben; identische Bytes sind No-op.
- **Raw:** unveränderlich; abweichender Raw-Inhalt unter derselben Identität
  stoppt.
- **Lease:** kanonischer Shared-Guard in place (`allow_legacy=False`), Root
  explizit (`--workspace-lock-root`/Env/Sibling); Prüfung vor und zwischen
  Publikationsschritten; kein Erwerb/Release durch den Writer.

---

## 6. Recovery-Vertrag

- `--inspect`: read-only; Journal-/Step-Zustand (`pre`/`post`/`foreign`/
  `missing`), Register-/Ownership-Zustand, `recovery_required`; keine Rohinhalte.
- `--recover --lease-id`: explizit, lease-gebunden, forward-only. Voraussetzungen:
  genau ein Journal, Integritätsdigest gültig, Stage-Hashes unverändert,
  Workspace-Fingerprint passend, jedes Ziel in Vor- oder Nachzustand.
  Danach werden offene Steps publiziert, der Journalzustand finalisiert und
  Journal/Stage entfernt.
- Fremde Bytes, fehlende Ziele, korrupte/mehrere Journale oder falsche Workspace-
  Bindung → Exit 3 ohne Mutation. Unbekannte Journal-Verzeichnis-Einträge
  (Dateien, Stage-Verzeichnisse ohne Journal, Junctions/Reparse-Points) sind
  fremder Zustand: Exit 3, bytegenau erhalten, keine Rekursion/Löschung. Kein
  Rollback/Löschen fremder Bestände und kein Rückbau bereits publizierter Bytes;
  Recovery verifiziert/finalisiert nur. Nur journalgebundene, hash-/größen- und
  pfadgeprüfte eigene Stage-Dateien (einfache Ebene) werden nach Erfolg entfernt.
- Lease-Ownership wird vor jedem mkdir/stage/write/rename/delete erneut geprüft;
  Verlust stoppt vor der nächsten Mutation. stdout ist UTF-8-bytesicher
  (cp1252/CJK/Emoji) ohne Traceback.
- Grenzen: keine Multi-File-ACID-/Power-Loss-Garantie, keine Race-Freiheit gegen
  bösartige gleichzeitige Dateisystemakteure, keine globale Snapshot-Isolation
  für nicht journalbewusste Leser. Manuelle Meeting-IDs werden vom Aufrufer als
  Datum+Slug gebildet; der Writer erzwingt Identität/Kollision/Quelle ohne
  automatisches Suffix.

---

## 7. Schnittstellen

| Einstieg | Vertrag |
| :--- | :--- |
| `ingest_meeting.py --capabilities --json` | read-only, ohne Workspace/Lease; `data.record_schema`, `data.writer_version`, Limits/Actions |
| `ingest_meeting.py --record -\|PATH --workspace … --lease-id …` | genau ein Sechs-Feld-Envelope; Exit 0/2/3; `data.entry/meeting_id/paths/changed` |
| `ingest_meeting.py --local --summary … [--transcript … [--raw …]]` | zweiter Eingang, gleicher Record/Writer; Default Plan, Apply mit Plan-Hash; Quellen erst nach Register verschoben |
| `ingest_meeting.py --inspect/--recover` | read-only bzw. explizite Recovery |
| `ingest_meeting.ingest_record(...)` | Python-API, gibt Envelope-kompatibles Dict zurück |
| `ingest_meeting.publish_registered_transcript(...)` | einzige Builder-Publikationsgrenze |
| `build_clean_transcript.py` | fachliche Ableitung; `--check` read-only; Publikation delegiert |
| `verify_meeting_fixtures.py` | read-only Fixture-Digest + Record-Konformität |

Envelope: `action/success/state/message/data/error`; stderr nur begrenzte
Diagnostik; stdout genau eine Zeile.

---

## 8. Tests und belegte Grenzen

- `tests/test_ingest_meeting.py`: Capabilities/CLI/Envelopes, UTF-8 unter cp1252
  inkl. erfolgreichem Apply, Fixture-Konformität, gebundenes JSON,
  Register/Legacy/Aliase (stabile ID/Pfade), Routing/Trusted Choices, Pfad-/
  Link-/Alias-Gates, Human-File- und Raw-Schutz, Journal-Korruption,
  fremde/Orphan-Journal-Einträge inkl. echter NTFS-Junction, Crash nach jedem
  Publikationsschritt mit echter Recovery, Lease-Verlust vor Staging,
  Local-Datei-Eingang mit Move/Crash, Builder→Writer→Datei.
- `tests/test_build_clean_transcript.py`: Korrektur-Engine und
  Delegations-/Fehler-/Exit-Vertrag des Builders.
- `tests/fixtures/meeting-record-v1.json`: gemeinsames SSOT-Paket mit
  Digest-Manifest; `scripts/verify_meeting_fixtures.py` prüft es read-only.
- Crash-Tests starten einen echten Kindprozess (`tests/_crash_ingest.py`) und
  belegen Journal, Blockade, Inspect und explizite Resume-Semantik.
- Keine Live-APIs, keine echten Transkripte, keine Consumer-Workspaces. Die
  Testsuite belegt die implementierten Grenzen, nicht adressierbare
  Dateisystem-Races oder Power-Loss-Verhalten.
