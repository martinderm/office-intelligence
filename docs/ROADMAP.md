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

## Nicht im Scope (aktuell)

- Kein blindes Auto-Routing ohne Guardrails.
- Inbox bleibt Safe Default bei Unsicherheit.
