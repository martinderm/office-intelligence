# Installation Paths (local tracking template)

Diese Datei ist eine Vorlage. Lege eine lokale Kopie an als:
- `docs/INSTALL_PATHS.local.md` (gitignored)

## Instances

### <instance-name>
- Agent workspace: `<abs path>`
- Skill target: `<abs path>/skills/mail-processor`
- Project repo: `<abs path>/projects/office-intelligence`
- Env file: `<abs path>/.env`
- Notes: `<optional>`

## Beispiel Sync-Checks

```bash
npm run check:sync -- --pair skills/mail-processor <skill-target>
npm run check:sync -- --strict --pair skills/mail-processor <skill-target>
```
