# TODO — office-intelligence (offene Altlasten)

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