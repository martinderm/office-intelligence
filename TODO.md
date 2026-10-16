# TODO — office-intelligence (offene Altlasten)

## 2026-10-16 — Präexistent, diff-fremd: `test_event_dossier` referenziert fehlendes Skript

- **rule_id:** missing-module (tracked test without tracked script)
- **Pfad:** `skills/mail-desk/tests/test_event_dossier.py` (12 Tests)
- **Meldung:** `ModuleNotFoundError: No module named 'mail_desk_event_dossier'`
  — das Testmodul ist getrackt, das erwartete
  `skills/mail-desk/scripts/mail_desk_event_dossier.py` existiert nicht im
  Baum (`Test-Path` false, auch an HEAD).
- **Run-Referenz:** FR-28-DOC-P1-Run (`2026-10-16-office-intelligence-fr28-doc-batch-docs`);
  diff-fremd — FR-28-Diff ist rein additiv (neue Doku-Abschnitte,
  `mail_desk_sign_manifest.py`, neues Testmodul) und berührt kein Dossier-Pfad.
- **Disposition:** Durch Anlage des Skripts (Feature-Vollzug) auf expliziten
  User-Auftrag beheben oder das Testmodul entfernen; kein stiller
  Orchestrator-Fix.

## 2026-10-16 — Präexistent, diff-fremd: `test_attachment_basename` Vertragsbruch

- **rule_id:** broken-contract (`original_filename` KeyError + Win32-Pfadsemantik)
- **Pfad:** `skills/mail-desk/tests/test_attachment_basename.py` (11 Tests; 5 fail,
  6 error in Coder-/Orchestrator-Läufen am 2026-10-16)
- **Meldung:** `KeyError: 'original_filename'` in
  `test_embedded_dots_fetch_and_repeat_preserve_original_mime_name`,
  `Free candidate path or directory traversal rejected before I/O: 'agenda..pdf'`
  in `test_embedded_double_dot_basename_is_not_a_traversal_component` und
  Windows-Pfadsemantik (`agenda\t.pdf`, `agenda.pdf.`) in weiteren — der
  getestete Basename-Vertrag weicht vom implementierten Code ab.
- **Run-Referenz:** FR-28-DOC-P1-Run (`2026-10-16-office-intelligence-fr28-doc-batch-docs`);
  diff-fremd — kein Berührungspunkt zur FR-28-Diff; vermutlich hat eine frühere
  fremde Feature-Änderung den Basename-Vertrag geändert oder ein Fullzug eines
  Fremd-Runs fehlt.
- **Disposition:** Eigenes Bug-Ticket für die Triage durch den User; der
  Basename-Vertrag verdient einen eigenständigen Fremd-Record.
- **Beobachtung:** Variabilität zwischen Läufen (Zähler 5 fail/6 err vs. 2 err)
  deutet auf Last- bzw. Reihenfolge-Abhängigkeit hin; im Modul-Isolat
  reproduzierbar. Der Bruch selbst ist kreuzlauf-konsistent
  (KeyError/Traversal/Pfad-Semantik), keine Timing-Flake-Familie.

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