# Changelog

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
