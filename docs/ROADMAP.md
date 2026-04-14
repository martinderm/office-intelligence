# ROADMAP

Kuratierte, noch sinnvolle nächste Schritte für `mail-processor`.

## Priorität Hoch

1) **OpenClaw-Tool-basierte Klassifikation als neuer Zielpfad einführen**
- Entscheidung: Für operative Mail-Klassifikation künftig **Variante A** verfolgen.
- Das bedeutet:
  - `mail-processor` bleibt deterministischer Orchestrator
  - modellgestützte Auswertung läuft über ein dediziertes OpenClaw-Plugin-Tool
  - **kein** `sessions_spawn` pro Mail, **kein** frei formulierender Agent im Routing-Pfad
- Umsetzungspakete:
  - **Paket 1:** Contract & Entscheidungsmodell
  - **Paket 2:** Klassifikations-Abstraktion im `mail-processor`
  - **Paket 3:** OpenClaw-Plugin-Tool `mail_intelligence.classify`
  - **Paket 4:** Shadow-Integration & Beobachtbarkeit
  - **Paket 5:** Routing-Fusion aktivieren
  - **Paket 6:** Altpfad und Discovery neu ordnen
- Referenz: `docs/architecture/agent-classification.md`

2) **Ambiguität robuster behandeln (Tie-Break-Regel)**
- Problem: ähnliche Scores können zu unsicherem Routing führen.
- Vorschlag:
  - zusätzlicher Abstandswert zwischen Top-1 und Top-2 Kandidat (`delta`, z. B. `>= 0.15`)
  - plus Mindest-Evidenz (z. B. Domain/Kontakt/Betreffsignal)
- Ziel: weniger False Positives bei ähnlichen Projekten.
- Hinweis: Diese Logik muss mit der künftigen Fusion aus Heuristik + Tool-Resultat zusammengedacht werden.

3) **Crash-Fenster COPY → State absichern**
- Problem: COPY kann erfolgreich sein, bevor `state.jsonl` geschrieben wird.
- Vorschlag:
  - Nachverifikation im Zielordner (Message-ID/Hash), bevor erneut geroutet wird
  - oder explizites Duplikat-Handling mit Markierung im State
- Ziel: idempotentes Verhalten auch bei Prozessabbruch.

4) **Thread-Kontext für Klassifizierung nutzen**
- Problem: Einzelmail ohne Verlauf ist oft semantisch dünn.
- Vorschlag:
  - `In-Reply-To`/`References` auswerten
  - optional letzten Thread-Kontext (N Mails) beim Match berücksichtigen
  - optional `thread_id` im State persistieren
- Ziel: bessere Zuordnung bei Reply-Ketten.
- Hinweis: Thread-Kontext soll sowohl der Heuristik als auch dem OpenClaw-Klassifikationstool zugeliefert werden.

## Priorität Mittel

5) **Umsetzungspakete sequentiell abarbeiten**
- Reihenfolge:
  - zuerst Paket 1, dann Paket 2, dann Paket 3
  - produktive Wirkung erst nach Paket 4 und Paket 5
  - Paket 6 als Bereinigung am Schluss
- Ziel: kein Big-Bang-Umbau, sondern kontrollierte Migration mit früher Verifikation
- Status:
  - Paket 1 begonnen
  - erster Contract-Entwurf in `docs/architecture/agent-classification.md`
  - Schema-Nachschärfung für Mail-JSON-Artefakte ergänzt (`thread.*`, `context.*`, normalisierte IDs)
  - Paket 2 in konkrete Refactoring-Bausteine zerlegt
  - Paket 2A umgesetzt (Contracts, Classifier-Interface, Artefakt-Typen)
  - Paket 2B begonnen (`matcher.ts` als reine Heuristik, Legacy-Merge separat ausgelagert)
  - Paket 2D teilweise umgesetzt (Artefakt-Writer ergänzt um `thread.*`, `context.*`, `referencesNormalized`)
  - Paket 2C teilweise umgesetzt (`thread-context.ts`, Lookup bekannter Referenzmails aus Artefakten, lokale Einhängung in bestehenden Pfad)

6) **Reviewbarer Suggestion-Flow für Projektkatalog**
- Problem: Wissen über Projekte altert, manuelle Pflege ist aufwändig.
- Vorschlag:
  - Vorschläge (Aliases/Keywords/Contacts) als Review-Artefakt sammeln
  - expliziter Human-Review vor Änderungen an `projects.json`
- Ziel: bessere Datenqualität ohne unkontrollierte Auto-Edits.
- Perspektive: später als getrennte Tool-Funktion denkbar, aber nicht mit der Routing-Klassifikation vermischen.

7) **Testplan für neue Auswahl/Retry-Features (C + D)**
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
