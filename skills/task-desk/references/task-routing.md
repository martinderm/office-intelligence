# Task Routing & Attribution Rules

> **Skill:** `office-intelligence/task-desk`

---

## 1. Prioritäts- und Fälligkeitsregeln

| Priorität | Todoist-Level | Verwendung |
| :--- | :---: | :--- |
| **Kritisch** | `p1` (Rot) | Harte Deadlines < 24h, wichtige Einreichungen, dringende Kundenmails. |
| **Hoch** | `p2` (Orange) | Wichtige Meilensteine in der aktuellen Woche, Rechnungsfreigaben. |
| **Normal** | `p3` (Blau) | Standardaufgaben, reguläre Folgeaktivitäten aus Meetings. |
| **Niedrig** | `p4` (Grau) | "Someday/Maybe", Hintergrundrecherchen, langfristige Ideen. |

---

## 2. Standard für Factored Attribution (einheitlich)

Jeder dispatchte Task weist seine Quelle in **fünf** Zeilen aus — quellenunabhängig
**eine** Form statt früherer `Mail-ID:`/`Meeting-ID:`/`Event:`-Labels:

```
Aktion: <title in eigenen Worten>
Quelle: <vorhandener EVID-Beleg> <neutraler Kontext-Titel>
Trace-ID: <stabile quellengebundene ID>
action_id: <UUID4>
source.type: <email|meeting|event|chat>
```

- **`Quelle`** ist ein **bereits vorhandener** Beleg (Beispiel `[EVID-2026-08-24-01]`
  oder `mail_message_id:a1b2c3d4e5@example.org`) — **nicht** stets ein Handoff-Feld.
  Es wird **kein** Producer-EVID erfunden: der `synthesis_handoff` trägt **kein**
  `evidence_anchor`. Der Beleg wird aus dem reviewed Evidence bzw. einem vorhandenen
  echten EVID gelesen; fehlen echte Artefakt-/EVID-Referenzen ⇒ **Review**.
- **`Trace-ID` (email):** die **normalisierte** Message-ID nach
  `skills/mail-desk/scripts/core/common.py:normalize_message_id` — `.strip()`,
  wiederholtes Entfernen äußerer `<>`, Lowercase (also ohne spitze Klammern).
  **Nicht** die Roh-Message-ID.
- **`Trace-ID` (meeting/event/chat):** die jeweilige **stabile** Quell-ID; sie wird
  **nicht** mail-normalisiert.
- **`action_id`/`source.type`** sind die **stabile `action_id`** und der **`source.type`**,
  konsistent mit dem `action_ref` des Zielvertrags
  ([`task-schemas.md`](task-schemas.md) §3) — ein **neuer** Zielvertrag und **nicht**
  die bestehende API-Attribution.

Die Attribution **materialisiert** aus `source` (`type`/`id`) plus dem persistierten
`evidence_ref={file,anchor}` (beide geprüft, nie erfunden; siehe
[`task-schemas.md`](task-schemas.md) §2/§4).

### Beispiele (nur Form, keine personenbezogenen Werte)

```
Aktion: Antwortentwurf für das Rektorat bezüglich Lehrplananpassung erstellen
Quelle: [EVID-2026-08-24-01] E-Mail Projekt-Alpha
Trace-ID: a1b2c3d4e5@example.org
action_id: 0f5a2c1e-7b3d-4a9c-8e21-3b6f0d4c9a12
source.type: email
```

```
Aktion: Kursvorlage duplizieren und Gastvortragende freischalten
Quelle: [EVID-2026-08-24-02] Meeting "Semesterplanung"
Trace-ID: fireflies_123456789
action_id: 5c9e7d21-0a4b-4f6e-9d33-77b1e2c8a004
source.type: meeting
```

```
Aktion: Abstract für Konferenztrack einreichen
Quelle: [EVID-2026-08-24-03] Event "EUCEN"
Trace-ID: eucen-2026
action_id: c4d8a1f0-3b6e-4c2a-8f17-2e9d5b0a71f3
source.type: event
```

### Grenzen

- Der `Quelle`-Beleg und die Human-Freigabe werden **nie erfunden**; `Quelle` ist ein
  bereits vorhandener EVID-/`mail_message_id`-Beleg und **nicht** stets ein
  Handoff-Feld.
- Routing/`project_id`/`section_id`/Priorität/Fälligkeit sind **optional**; eine
  bewusste Abwesenheit ist zulässig. Fehlen Belege für einen von der Workspace-Regel
  **erforderlichen** Wert ⇒ **Review**; **keine** Pflicht-Terminierung/-Section.
- `synthesis_targets` sind **kein** Todoist-Ziel; `action_ref` ist die einzige
  Rückreferenz (Details: [`task-schemas.md`](task-schemas.md)).
