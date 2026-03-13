# Changelog

## Unreleased

- Envelope-Selection transparenter gemacht: `selection_resolved` loggt jetzt zusätzlich
  - `requestedMaxScanPages`, `effectiveMaxScanPages`, `scannedPages`
  - damit ist `fetch-limit` vs. tatsächliche Auswahl nachvollziehbar.
- Envelope-Selection gehärtet: `MAIL_SELECT_MAX_SCAN_PAGES` wird dynamisch angehoben, bis `MAIL_FETCH_LIMIT` erreicht ist (oder Postfach „aus“ ist / Hard-Stop greift).
- Transient-Fehler-Queue für Message-Reads: persistente `retry-queue.jsonl` mit exponential backoff + `max-attempts` + dead-letter (verhindert, dass wackelige Reads den Fortschritt blockieren).
- Topic-Matching ergänzt (gleichrangig zu Projekten; heuristisch + LLM-Schema):
  - neue Match-Felder `matchedTopicId`, `topicScore`, `topicReason`
  - LLM-Prompt erweitert um `topicCandidates` + `TOPIC_CATALOG_HINTS`
  - Topic-Katalog getrennt in `memory/references/topics/topics.json` (`TOPICS_JSON_PATH`)
- Workpackage-Matching ergänzt (projektuntergeordnet; heuristisch + LLM-Schema):
  - neue Match-Felder `matchedWorkpackageId`, `workpackageScore`, `workpackageReason`
  - LLM-Prompt erweitert um `workpackageCandidates`

- Discovery auf LLM-only umgestellt (`--discover-projects`):
  - pro Mail ein LLM-Call für `project_name`, `project_title`, `topics`, `confidence`
  - Ausgabe enthält `per_message_extractions` + aggregierte `new_project_candidates`
  - Ergebnisdatei wird während des Runs inkrementell nach jeder verarbeiteten Mail aktualisiert
- Schutz gegen Rauschen: list-/bulk-/auto-submitted Mails werden im Discovery-Mode übersprungen.
- Qualitätsstatus dokumentiert: aktuelle Discovery-Ergebnisse sind für direkte Katalogpflege noch nicht zuverlässig genug (nur Review-Hinweise).
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
