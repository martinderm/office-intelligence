# Mail-Desk — Aufgaben, Backlog & Changelog

Zentrales Backlog für alle offenen Aufgaben, technischen Optimierungen und Meilensteine im Bereich `mail-desk`.

---

## 1. Offene operative Aufgaben (Mail-Verarbeitung)

- [ ] **Nächste historische Charge verarbeiten (März 2026)**
  - *Umfang:* E-Mails aus März 2026 (`date: "2026-03-01"` bzw. `query: "since 2026-03-01 before 2026-04-01"`).
  - *Ablauf:* Standardisierter Ablauf via JSON-Manifest (`batch-draft.json` $\rightarrow$ Audit $\rightarrow$ `batch-manifest.json` Execute).
  - *Ziel:* Fortlaufende Abarbeitung Monat für Monat bis zum aktuellen Tagesbestand.

---

## 2. Offene technische Optimierungen (Engine & Runner)

- [ ] **[P0] Bulk-Copy & Multi-ID Move (Gruppierung nach Zielordner)**
  - *Beschreibung:* E-Mails einer Charge beim Ausführen nach Zielordner bündeln und mit Multi-ID-Befehlen kopieren (`himalaya message copy <id1> <id2> ... -f <folder>`).
  - *Sammel-Löschung:* Alle erfolgreich verschobenen Mails in einem einzigen Befehl aus der `INBOX` löschen (`himalaya message delete <id1> <id2> ...`).
  - *Bulk-Verifikation:* 1 `envelope list` pro Zielordner verifiziert alle neu transferierten Mails gleichzeitig.
  - *Ziel-Metrik:* Reduktion von ~150 IMAP-Verbindungen auf ~10 Verbindungen pro 25er-Charge; Verkürzung der Ausführungsdauer von ~12–15 Min. auf **ca. 30–45 Sekunden**.
  - *Dateien:* [`scripts/core/himalaya.py`](scripts/core/himalaya.py), [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [ ] **[P2] Multi-Batch Pipelining (`batch-pipeline.json`)**
  - *Beschreibung:* Unterstützung für Pipeline-Läufe mit z. B. `total_count: 100` und `chunk_size: 25`, die mehrere Chunks nacheinander abarbeiten, nach jedem Chunk den Index sichern und Fortschritt melden.
  - *Wirkung:* Aufarbeitung größerer historischer Zeitfenster ohne wiederholte manuelle Anstöße.
  - *Dateien:* [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [ ] **[P2] Persistenter IMAP-Session-Pool (`imaplib` / Keep-Alive)**
  - *Beschreibung:* Optionaler nativer Python-IMAP-Worker, der eine offene TLS-Verbindung über den gesamten Lauf hinweg hält, statt für jede Operation den `himalaya`-Prozess neu zu spawnen.
  - *Wirkung:* IMAP-Befehle antworten in < 50 ms statt 3–5 s pro TLS-Handshake.
  - *Dateien:* [`scripts/core/himalaya.py`](scripts/core/himalaya.py)

---

## 3. Architektur & Modularisierungs-Backlog (Batch Runner Refactoring)

### Ist-Zustand & Bewertung
- **Code-Umfang:** `mail_desk_batch_runner.py` umfasst derzeit ca. **1.225 Zeilen**.
- **Bereits ausgelagert:** Sämtliche datenzugriffs- und protokollrelevanten Module liegen sauber isoliert in `scripts/core/`:
  - `core/himalaya.py` (IMAP-Kapselung & Verifikation)
  - `core/index.py` (Master-Index, Signaturen, Atomares I/O)
  - `core/classifier.py` (Kataloge, Thread-Vererbung, Heuristiken)
  - `core/evidence.py` (Projekt-Evidenzen & Batch-Flush)
  - `core/sent_indexer.py` (Sent-Items Synchronisation & Auto-Reply Resolution)
  - `core/action_log.py` (Action Logging & Case Resolution)
- **Architektonisches Urteil:** Aktuell vollkommen handhabbar und stabil. Der Runner fungiert als linearer, gut gegliederter Modus-Dispatcher ohne Spaghetti-Abhängigkeiten. Ein Refactoring hat derzeit **keine Dringlichkeit**, da das System performant und fehlerfrei läuft.

### Ziel-Architektur für zukünftige Modularisierung
Sollten weitere umfangreiche Workflows (z. B. LLM-basierte Antwortgenerierung, Moodle-Integration oder Multi-Account-Routing) hinzukommen, wird die Modi-Logik modularisiert:

```text
scripts/
├── mail_desk_batch_runner.py       <-- Schlanker CLI-Dispatcher (~150 Zeilen)
└── core/
    ├── __init__.py
    ├── common.py
    ├── himalaya.py
    ├── index.py
    ├── classifier.py
    ├── evidence.py
    ├── sent_indexer.py
    ├── action_log.py
    └── modes/                      <-- Ausgelagerte Modus-Handler
        ├── __init__.py
        ├── inspect.py              (run_inspect_mode)
        ├── draft.py                (run_draft_mode)
        ├── execute.py              (run_execute_mode)
        ├── verify.py               (run_verify_mode)
        ├── pipeline.py             (run_pipeline_mode)
        ├── search.py               (run_search_mode)
        └── resolve.py              (run_resolve_mode)
```

### Trigger-Kriterien für die Umsetzung
- [ ] Überschreitung von 1.500 Zeilen im Root-Runner.
- [ ] Hinzufügen neuer komplexer Betriebsmodi (z. B. AI-Drafting / Tutor-Bridge).
- [ ] Geplante Wartungs-Session ohne parallele operative Mail-Verarbeitung.

---

## 4. Abgeschlossene Aufgaben & Meilensteine (Changelog)

- [x] **Januar und Februar 2026 vollständig abgeschlossen** *(2026-09-01)*
  - Alle E-Mails bis 2026-03-01 transferiert, verifiziert, aus `INBOX` entfernt.
  - Master Location Index auf **696 Mails** erweitert.
  - Kontrollabfrage `before 2026-03-01` liefert 0 verbleibende Mails in `INBOX`.

- [x] **$O(1)$ In-Memory-Signatur-Filter ($0\text{ ms}$ Bekannt-Prüfung)** *(2026-09-01)*
  - Nutzung der im Envelope-Listing nativ vorhandenen Header (`subject`, `from`, `date`) zum sofortigen In-Memory-Abgleich gegen `final-location-index.json` und `action-log.jsonl`.
  - Scan-Dauer für 50–80 bekannte Mails von 150 s auf **unter 0,001 s** reduziert. Vollkommen unabhängig von wandernden Envelope-IDs.
  - Dateien: [`scripts/core/index.py`](scripts/core/index.py), [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [x] **Natives IMAP-Datums-Windowing (`date:` / `query:` statt Server-Side `SORT`)** *(2026-09-01)*
  - Vermeidung von `SORT (DATE) UTF-8 ALL` auf 9.500 Mails; stattdessen native IMAP `SEARCH`-B-Tree-Filter (`date 2026-02-12`, `before 2026-03-01`).
  - Serverantwort in < 0,3 Sekunden statt 60–90 Sekunden Datenbank-Sortierüberlastung auf dem GroupWise POA.
  - Dateien: [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [x] **GroupWise-Transaktionsresilienz & Schnellverifikation** *(Commits `75921c8`, `98f7a19`)*
  - Schnellverifikation im Zielordner über fokussierte Header-Prüfung (< 2 s). Socket-Schonung durch 0.15 s Transaktionspause zwischen IMAP-Befehlen. Dynamischer Konsolen-Timer korrigiert (bis 360 s).
  - Erkenntnis: GroupWise POA sperrt bei Multi-ID-Bulk-Befehlen leicht den Socket (WinError 10054); sequentielle, verifizierte Einzeltransaktionen mit Mini-Pausen laufen mit 6,3 s / Mail stabil und 22x schneller.
  - Dateien: [`scripts/core/himalaya.py`](scripts/core/himalaya.py), [`scripts/core/progress.py`](scripts/core/progress.py), [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [x] **Thread-Vererbung via `In-Reply-To` & `References` ($O(1)$-Klassifikation)** *(Commit `4de208c`)*
  - Im Klassifikator prüfen, ob die `in_reply_to`- oder `references`-Header einer Mail auf eine bereits im [`final-location-index.json`](../../../../boku-user/data/mail-desk/final-location-index.json) registrierte Eltern-`message_id` verweisen.
  - Sofortige und deterministische Übernahme des Zielordners und Projekts/Topics in < 0,001 ms ohne Volltextsuche. 100 % Thread-Konsistenz.
  - Dateien: [`scripts/core/classifier.py`](scripts/core/classifier.py)

- [x] **Lokaler Index-Vorab-Check ($O(1)$ Hash-Lookup)** *(Commit `9b4f31c`)*
  - Vor dem Kopieren prüfen, ob die `message_id` bereits im lokalen Master-Index vorliegt.
  - Vermeidet unnötige IMAP-Netzwerkabfragen auf Zielordner vor dem Kopieren; überspringt bei Runner-Neustarts bereits transferierte Mails sofort.
  - Dateien: [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [x] **Batch Evidence Flush (Atomares Projekt-Schreiben)** *(Commit `f0333b9`)*
  - Neue Evidenzeinträge einer Charge nach Projekt gruppieren und gesammelt in einem einzigen Schreibvorgang in [`evidence/YYYY-MM.md`](../../../../boku-user/memory/references/projects/) anhängen.
  - Schont I/O, verhindert Dateisperren und minimiert Git-Diff-Fragmentierung. Deduplizierung in $O(1)$ über Message-ID.
  - Dateien: [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py), [`scripts/core/evidence.py`](scripts/core/evidence.py)
