# Office Intelligence — Paket System Map: Seiteneffekte (Effects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Seiteneffekte  
> **Ziel**: Vollständige Dokumentation aller Systemgrenzen, Lock-Bedingungen, Dateisystem-Garantien, Fail-Closed-Verhalten und Umwelt-Interaktionen.  
> **Gültig für**: Repository Root & konsumierende Workspaces (relativ)

---

## 1. Zero-Mutation-Garantie für das Bundle

Das Repository `office-intelligence` (einschließlich aller Unterordner in `skills/`) ist ein **statisches Code- und Dokumentations-Paket**.

* **Invariant:** Kein Skript aus dem Bundle modifiziert zur Laufzeit Dateien innerhalb von `skills/office-intelligence/`.
* **Keine Bundle-Control-Plane:** Es werden keine `.agents/session.lock`-Dateien oder temporäre Cache-Dateien im Bundle-Root angelegt oder committed.
* **Zielort aller Mutationen:** Schreiboperationen finden ausnahmslos in den explizit deklarierten Verzeichnissen des Ziel-Workspaces statt (`memory/`, `data/`).

---

## 2. Workspace-Lock & Concurrency-Regeln

Um Race Conditions zwischen parallelen Agenten-Sitzungen (z. B. Codex, Antigravity, Claude Code) zu verhindern, unterliegen alle mutierenden Operationen dem Lock-Guard:

* **Lock-Erwerb:** Vor Beginn einer Mutation muss der ausführende Harness die Lock-Lease im Ziel-Workspace über den Nachbar-Skill `workspace-lock` besitzen.
* **Fail-Closed Guard:** Skripte rufen `require_workspace_lock()` auf:
  * Fehlt die Lease oder ist sie abgelaufen → Sofortiger Abbruch (`WorkspaceLockRequiredError`, Stopcode `WORKSPACE_LOCK_REQUIRED`).
  * Aktiver fremder Lock vorhanden → Sofortiger Abbruch; kein stilles Überschreiben.
* **Kein Legacy-Bypass:** `--allow-legacy` und Umgebungs-Overrides wie `WORKSPACE_LOCK_ALLOW_LEGACY` sind für neue schreibende Pfade strikt deaktiviert bzw. entfernt.

---

## 3. Dateisystem-Sicherheit & Integrität

### 3.1 Atomare Schreiboperationen (Replace-Semantik)
Keine schreibende Skriptoperation schreibt direkt in eine bestehende Zieldatei:
1. Schreiben der Daten in eine temporäre Datei (`tempfile.NamedTemporaryFile`) im selben Verzeichnis (selbes Dateisystem/Mount-Point).
2. Vollständiges Flush und Sync auf Disk.
3. Atomarer Austausch via `os.replace()`.
4. **Garantie:** Bei einem Prozessabbruch, Stromausfall oder Timeout bleibt die bestehende Datei entweder im Originalzustand erhalten oder wird vollständig ersetzt. Es entstehen keine unvollständigen oder korrupten JSON-/Markdown-Dateien.

### 3.2 Pfad-Disziplin & Relativität
* Generell **keine absoluten Host-Pfade** (`C:\Users\...`, `/home/...`) in generierte JSON-Dateien, Kataloge oder Evidenzprotokolle schreiben.
* Alle Pfadreferenzen innerhalb von Katalogen und Indizes sind **relativ zum Workspace-Root** zu halten (Portabilität und Vermeidung von Datenlecks bei Git-Push).

---

## 4. Externe Subprozesse & Adapter-Grenzen

Das Bundle steuert bei Bedarf externe CLI-Tools an, kapselt diese jedoch strikt:

| Werkzeug | Verwendung | Sicherheits- & Laufzeit-Grenze |
| :--- | :--- | :--- |
| **Himalaya** | E-Mail-Fetch & Search (`mail-desk`) | Strikter Prozess-Timeout; kein interaktiver Konfigurations-Wizard (`himalaya_config_missing` stoppt sofort); keine Shell-Expansion. |
| **Pandoc** | Markdown-Konvertierung (`cloud-atlas`) | Subprozess mit isolierten Argumenten und Timeout; keine Ausführung von eingebetteten Skripten. |
| **LibreOffice** | Headless-Konvertierung (`cloud-atlas`) | Ausführung im Headless-Modus (`--headless --convert-to`); Isolation vor GUI-Abhängigkeiten. |
| **Tesseract OCR** | PDF/Bild-Texterkennung (`cloud-atlas`) | Beschränkt auf lokale Bildanalyse; Fallback auf Originaldatei bei Nichterreichbarkeit. |

---

## 5. Sub-Skill-spezifische Effekte (Verweise)

Vertiefende systemspezifische Schutz- und Isolationseffekte sind in den L2-Effektkarten dokumentiert:

* **Mail-Desk Deep Dive:** [`../../skills/mail-desk/docs/system-map/effects.md`](../../skills/mail-desk/docs/system-map/effects.md)  
  *(Windows Reparse-Point-Blockade 0x400, Symlink-Bann, Filterung verbotener Inhalte wie `body` oder `credentials` in Metadaten, Fail-Closed Drift-Abbruch)*
* **Cloud-Atlas Deep Dive:** [`../../skills/cloud-atlas/docs/system-map/effects.md`](../../skills/cloud-atlas/docs/system-map/effects.md)  
  *(Schutz signierter PDFs vor In-Place-Mutation, Differenzierung `enrich_source` vs. `local_derivative`, Tool-Timeouts)*
