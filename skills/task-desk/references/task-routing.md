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

## 2. Standard für Factored Attribution

Jeder erzeugte Task muss seine Quelle nachvollziehbar ausweisen:

### A) Mail-Task
```
Aktion: Antwortentwurf für Rektorat bezüglich Lehrplananpassung erstellen
Quelle: [EVID-2026-08-24-01] E-Mail von rektorat@boku.ac.at
Mail-ID: <480625239.67.1777027738570@boku-dbp...>
```

### B) Meeting-Task
```
Aktion: Moodle-Kurs duplizieren und Gastvortragende freischalten
Quelle: [EVID-2026-08-24-02] Meeting "Semesterplanung WS26/27"
Meeting-ID: fireflies_123456789
```

### C) Event-Task
```
Aktion: Abstract für Konferenztrack 2 einreichen (Deadline: 15.11.)
Quelle: [EVID-2026-08-24-03] Event "EUCEN 2026"
Event: eucen-2026
```
