# Changelog

## Unreleased

- Neues Discovery-Feature (`--discover-projects`):
  - analysiert die letzten X Mails (`--discover-last`)
  - schlägt potenzielle neue Projekte vor (`new_project_candidates`)
  - erzeugt Kontakt-/Teilnehmer-Vorschläge für bestehende Projekte (`project_participant_suggestions`)
  - schreibt Ergebnis als Review-Artefakt (Default: `data/mail-routing/project-candidates.json`)
- Schutz gegen Rauschen: list-/bulk-/auto-submitted Mails werden im Discovery-Mode übersprungen.
- Himalaya-Proxy-Wrapper korrigiert: Argumente werden jetzt robust mit `%*` weitergereicht (Fix für fehlerhafte Tokens wie `--trace0`/`envelope0`).
- Neues Script `scripts/create-himalaya-account-proxy.mjs` zum reproduzierbaren Erzeugen eines plattformübergreifenden Proxy-Wrappers mit fixer Account-Bindung.

## 0.1.1 - 2026-03-07

- Sanitizing massiv erweitert für HTML-/Newsletter-Mails:
  - aktive Inhalte entfernt
  - Tracking-Parameter entfernt
  - Footer-/Boilerplate-Trim
  - Layout-Noise-Cleanup
  - Dedupe wiederholter Links
- Himalaya-Read-Pfad umgestellt:
  - bevorzugt `message export --full` (raw MIME)
  - `message read` als Fallback
- MIME-aware Body-Extraktion ergänzt:
  - multipart parsing
  - bevorzugt `text/html`, fallback `text/plain`
  - quoted-printable/base64-Dekodierung
- Header-Extraktion in strukturiertes `mailMeta` ergänzt (u. a. From/Subject/Date/Message-ID/List/Auth-Signale).
- Idempotenz auf stabile IDs umgestellt:
  - primär normalisierte `Message-ID`
  - fallback deterministischer Content-Hash
  - envelope-lokale IDs nur noch für Live-Operationen
- State-Logging erweitert:
  - neues Event `message_skipped`
  - `sourceFolder`, `copyTargets`, `lastKnownEnvelopeId`, `lastKnownFolder`
- README auf neuen Stand gebracht.

## 0.1.0 - 2026-03-06

- Projekt initialisiert (`mail-processor`) inkl. Git-Repository.
- Konzept übernommen: `concepts/MAIL-PIPELINE.md`.
- Dokumentation aufgebaut:
  - Root `README.md`
  - `memory/references/projects/README.md`
  - `_TEMPLATE-project.md`
  - Skill-Skizze `skills/mail-processor/SKILL.md`
- MIT-Lizenz hinzugefügt.
- TypeScript-Scaffold erstellt (`src/*`, Build-Skripte, CLI).
- Implementiert:
  - `.env`-Config Loader
  - Lockfile/Single-Runner
  - `projects.json`-Validation
  - JSONL-State-Logging
  - Himalaya-Adapter (`envelope list`, `message read`, `message copy`)
  - Deterministischer Matcher + needsReply-Heuristik
  - Idempotenz (`message_processed`)
  - Mock-Mode (`HIMALAYA_COMMAND=mock`)
- README/SKILL/Konzept auf aktuellen Implementierungsstand aktualisiert.
- LLM-Stufe ergänzt (OpenAI-kompatibel, modellwahlbar, Prompt-Datei konfigurierbar).
- Preprocessing ergänzt: aktuelle Nachricht priorisiert, ältere Kontexte niedriger gewichtet.
- Dokumentiert: Projektkatalog-Qualität (`projects.json`) ist kritischer Faktor für Routing-Qualität.
