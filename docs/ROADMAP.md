# ROADMAP

Kuratierte, noch sinnvolle nächste Schritte für `mail-processor`.

## Priorität Hoch

1) **Ambiguität robuster behandeln (Tie-Break-Regel)**
- Problem: ähnliche Scores können zu unsicherem Routing führen.
- Vorschlag:
  - zusätzlicher Abstandswert zwischen Top-1 und Top-2 Kandidat (`delta`, z. B. `>= 0.15`)
  - plus Mindest-Evidenz (z. B. Domain/Kontakt/Betreffsignal)
- Ziel: weniger False Positives bei ähnlichen Projekten.

2) **Crash-Fenster COPY → State absichern**
- Problem: COPY kann erfolgreich sein, bevor `state.jsonl` geschrieben wird.
- Vorschlag:
  - Nachverifikation im Zielordner (Message-ID/Hash), bevor erneut geroutet wird
  - oder explizites Duplikat-Handling mit Markierung im State
- Ziel: idempotentes Verhalten auch bei Prozessabbruch.

3) **Thread-Kontext für Klassifizierung nutzen**
- Problem: Einzelmail ohne Verlauf ist oft semantisch dünn.
- Vorschlag:
  - `In-Reply-To`/`References` auswerten
  - optional letzten Thread-Kontext (N Mails) beim Match berücksichtigen
  - optional `thread_id` im State persistieren
- Ziel: bessere Zuordnung bei Reply-Ketten.

## Priorität Mittel

4) **Reviewbarer Suggestion-Flow für Projektkatalog**
- Problem: Wissen über Projekte altert, manuelle Pflege ist aufwändig.
- Vorschlag:
  - Vorschläge (Aliases/Keywords/Contacts) als Review-Artefakt sammeln
  - expliziter Human-Review vor Änderungen an `projects.json`
- Ziel: bessere Datenqualität ohne unkontrollierte Auto-Edits.

5) **Testplan für neue Auswahl/Retry-Features (C + D)**
- Kontext: In boku-martin Shadow-Run mit `fetch-limit=20` triggerten die neuen Pfade nicht, weil
  - genug Envelopes schon auf Page 1 gefunden wurden (kein dynamisches Hochdrehen sichtbar), und
  - keine transienten Read-Fehler auftraten (Retry-Queue blieb leer).
- Zusätzliches Detail: Runner überschreibt Shell-Env-Overrides, weil `run-shadow.mjs` `envFromFile` **nach** `process.env` merged.
- ToDo (später):
  - **C demonstrieren:** `MAIL_ENVELOPE_PAGE_SIZE` klein setzen und `MAIL_SELECT_MAX_SCAN_PAGES` absichtlich klein halten, sodass `effectiveMaxScanPages` > requested werden muss.
  - **D demonstrieren:** transienten Read-Fehler provozieren (z. B. via sehr kurzem Timeout, künstlichem Fehler-Switch oder gezieltem Netzwerk-Glitch), dann prüfen:
    - `message_deferred_transient` im `state.jsonl`
    - `retry-queue.jsonl` erstellt + Items due beim nächsten Run priorisiert
    - nach Erfolg `retry_succeeded`
    - nach 2 Versuchen Eintrag in `retry-dead-letter.jsonl`

## Nicht im Scope (aktuell)

- Kein blindes Auto-Routing ohne Guardrails.
- Inbox bleibt Safe Default bei Unsicherheit.
