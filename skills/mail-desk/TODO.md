# Mail-Desk — Anstehende Aufgaben & Optimierungs-Backlog

Dieses Dokument dient als zentrales Backlog für alle noch offenen technischen Optimierungen und operativen Aufgaben im Bereich `mail-desk`.

---

## 1. Technische Performance-Optimierungen (Engine & Runner)

- [ ] **[P0] Bulk-Copy & Multi-ID Move (Gruppierung nach Zielordner)**
  - *Beschreibung:* E-Mails einer Charge beim Ausführen nach Zielordner bündeln und mit Multi-ID-Befehlen kopieren (`himalaya message copy <id1> <id2> ... -f <folder>`).
  - *Sammel-Löschung:* Alle erfolgreich verschobenen Mails in einem einzigen Befehl aus der `INBOX` löschen (`himalaya message delete <id1> <id2> ...`).
  - *Bulk-Verifikation:* 1 `envelope list` pro Zielordner verifiziert alle neu transferierten Mails gleichzeitig.
  - *Ziel-Metrik:* Reduktion von ~150 IMAP-Verbindungen auf ~10 Verbindungen pro 25er-Charge; Verkürzung der Ausführungsdauer von ~12–15 Min. auf **ca. 30–45 Sekunden**.
  - *Dateien:* [`scripts/core/himalaya.py`](scripts/core/himalaya.py), [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [ ] **[P0] Thread-Vererbung via `In-Reply-To` & `References` ($O(1)$-Klassifikation)**
  - *Beschreibung:* Im Klassifikator prüfen, ob die `in_reply_to`- oder `references`-Header einer Mail auf eine bereits im [`final-location-index.json`](../../../../boku-user/data/mail-desk/final-location-index.json) registrierte Eltern-`message_id` verweisen.
  - *Wirkung:* Sofortige und deterministische Übernahme des Zielordners und Projekts/Topics in < 0,001 ms ohne Volltextsuche. 100 % Thread-Konsistenz.
  - *Dateien:* [`scripts/core/classifier.py`](scripts/core/classifier.py)

- [ ] **[P1] Lokaler Index-Vorab-Check ($O(1)$ Hash-Lookup)**
  - *Beschreibung:* Vor dem Kopieren prüfen, ob die `message_id` bereits im lokalen Master-Index vorliegt.
  - *Wirkung:* Vermeidet unnötige IMAP-Netzwerkabfragen auf Zielordner vor dem Kopieren; überspringt bei Runner-Neustarts bereits transferierte Mails sofort.
  - *Dateien:* [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [ ] **[P1] Batch Evidence Flush (Atomares Projekt-Schreiben)**
  - *Beschreibung:* Neue Evidenzeinträge einer Charge nach Projekt gruppieren und gesammelt in einem einzigen Schreibvorgang in [`evidence/YYYY-MM.md`](../../../../boku-user/memory/references/projects/) anhängen.
  - *Wirkung:* Schont I/O, verhindert Dateisperren und minimiert Git-Diff-Fragmentierung.
  - *Dateien:* [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [ ] **[P2] Multi-Batch Pipelining (`batch-pipeline.json`)**
  - *Beschreibung:* Unterstützung für Pipeline-Läufe mit z. B. `total_count: 100` und `chunk_size: 25`, die mehrere Chunks nacheinander abarbeiten, nach jedem Chunk den Index sichern und Fortschritt melden.
  - *Wirkung:* Aufarbeitung größerer historischer Zeitfenster ohne wiederholte manuelle Anstöße.
  - *Dateien:* [`scripts/mail_desk_batch_runner.py`](scripts/mail_desk_batch_runner.py)

- [ ] **[P2] Persistenter IMAP-Session-Pool (`imaplib` / Keep-Alive)**
  - *Beschreibung:* Optionaler nativer Python-IMAP-Worker, der eine offene TLS-Verbindung über den gesamten Lauf hinweg hält, statt für jede Operation den `himalaya`-Prozess neu zu spawnen.
  - *Wirkung:* IMAP-Befehle antworten in < 50 ms statt 3–5 s pro TLS-Handshake.
  - *Dateien:* [`scripts/core/himalaya.py`](scripts/core/himalaya.py)

---

## 2. Operative E-Mail-Verarbeitung (Historische Bestände)

- [ ] **Nächste historische Charge verarbeiten (ab 11. Februar 2026)**
  - *Umfang:* Nächste 25 E-Mails aus der `INBOX`.
  - *Ablauf:* Standardisierter Ablauf via JSON-Manifest (`batch-manifest.json` Draft $\rightarrow$ Reclassify/Audit $\rightarrow$ Execute $\rightarrow$ Verify).
  - *Ziel:* Fortlaufende Reduktion der INBOX und Erweiterung des Master Location Index (> 635 Mails).
