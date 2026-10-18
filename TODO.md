# TODO — office-intelligence (offene Altlasten)

## 2026-10-18 — Issue #14: Writer-Implementierung zur User-Abnahme

Die historische Writer-Lücke unten wird durch den source-bound Writer für
Projekt- und katalogisierte Topic/Subtopic-Events bearbeitet. Echte lokale
Fassaden-Integration ergänzt die bisherigen synthetischen Boundary-Pins; keine
Live-Mailbox-Abnahme. Normative Entwurfs-/Bootstrap-Änderung ist High Risk,
unabhängiges finales Review und User-Merge bleiben verpflichtend. Aktueller
Nachweis: `FEATURE-REQUEST-PROGRESS.md`; historische Angaben bleiben erhalten.

## 2026-10-16 — GELÖST → Issue #8: `test_event_dossier` referenziert fehlendes Skript

- **Auflösung:** Die beiden diff-fremden Altlasten-Module
  (`test_event_dossier.py` — Spec-first committet in `0896334`, Produktionsmodul
  `mail_desk_event_dossier.py` nie implementiert — und `test_attachment_basename.py`)
  wurden am 2026-10-16 aus der Suite entfernt (User-Auftrag „Altlasten entfernen").
- **Nachverfolgung:** **GitHub issue #8** implementiert die read-only Fassade;
  die 12 historischen Pins sind wiederhergestellt und um Negativtests ergänzt.
  Dokumentierte Testkorrektur: Der E2E-Pin prüft die externe Writer-Grenze mit
  synthetischen Artefakten und Source-/Output-Receipt. Der vorab nachweislich
  fehlende Event-Writer ist separat **GitHub issue #14**; keine Implementierung
  oder End-to-End-Writer-Abnahme wird mit der Fassade behauptet.
- **PR-Governance 2026-10-17:** Lane-Branch mit `main` (#9) reconciliert;
  vier Copilot-Merge-Findings am Fassaden-Code fixiert (Syntheseziel-Validator,
  `needs_reply`-Boolean-Step, kanonische Ziel-Normalisierung im Handoff,
  `inspect_requests` mit Client-`action`) samt Regression-Pins; Merge-Abnahme
  und Attestation laufen nach PR-Merge-Governance
  (`agent-architecture/governance/pr-merge-governance.md`, unabhängiges
  Maschinen-Review in anderem Modell, Squash, Cleanup).

## 2026-10-16 — GELÖST → Issue #9: `test_attachment_basename` Vertragsbruch

- **Auflösung:** Testmodul am 2026-10-16 aus der Suite entfernt (User-Auftrag),
  durch **GitHub issue #9** wiederhergestellt (6 Pins unverändert, genuine Rot
  5 Failures + 6 Errors). Reconcile abgeschlossen: Leserichtung je Pin-Gruppe
  mit dokumentierter Begründung in L2-Map §30; Produktionshärtung an
  `validate_attachment_filename` und `original_filename`-Provenienz, keine
  stillen Sicherheits-Rotationen.

## 2026-09-27 — Präexistenter, diff-fremder Timing-Flake in MD-A3-Tests

- **rule_id:** flaky-timing (assertion window)
- **Pfad:** `skills/mail-desk/tests/test_maildesk_attachments_mda3.py:1068`
  (`test_ocr_timeout_enforced_and_cleans_temporary_artifacts`, Familie auch
  :982/:1024/:1566)
- **Meldung:** `AssertionError: 2.61324143409729 not less than 2.5` — die
  elapsed-Assertion (< 2.5 s) für den OCR-Timeout-Abbruch (OCR-Mock 1,0 s,
  Timeout 0,05 s, Windows-Prozess-Spawn/Shutdown) riss unter Maschinenlast
  (2,6–3,0 s); nach Ruhe wieder 37/37 OK. Betroffen sind ausschließlich
  Timeout-Tests mit engen elapsed-Fenstern; die Suite war im selben Tag 3×
  vollgrün (1073/1073).
- **Run-Referenz:** Doku-Run „Jev-Integration-Konzept“ (2026-09-27, Commit folgt
  im selben Repo); diff-fremd — Working Tree enthielt nur
  `docs/analysis/jev-integration.md` + L1-Map-Diagrammzeile.
- **Vorschlag:** Slack der elapsed-Assertions in den vier Timeout-Tests
  lasttolerant anheben (z. B. 2,5→4,0 s) oder Messung über
  monotonic/perf_counter mit größerem Budget; Korrektur als eigene
  `test-correction`-Runde, nicht als stiller Orchestrator-Fix.
- **Nachverfolgung:** Zusammen mit der Multiprocessing-READY-Handshake-Familie
  (`core/quarantine/attachment_extract.py:529`) als **GitHub issue #10** erfasst
  (Vorab-Prüfung Pflicht).
