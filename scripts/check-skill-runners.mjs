#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const cwd = process.cwd();
const targets = [
  "skills/mail-processor/scripts/run-shadow.mjs",
  "skills/mail-processor/scripts/run-run.mjs",
];

const forbiddenPatterns = [
  { name: "hardcoded HIMALAYA_COMMAND", re: /HIMALAYA_COMMAND\s*[:=]\s*["'`]/ },
  { name: "hardcoded LLM_BASE_URL", re: /LLM_BASE_URL\s*[:=]\s*["'`]/ },
  { name: "hardcoded LLM_API_KEY", re: /LLM_API_KEY\s*[:=]\s*["'`]/ },
  { name: "hardcoded LLM_MODEL", re: /LLM_MODEL\s*[:=]\s*["'`]/ },
  { name: "hardcoded PROJECTS_JSON_PATH", re: /PROJECTS_JSON_PATH\s*[:=]\s*["'`]/ },
  { name: "hardcoded MAIL_PROCESSOR_DATA_DIR", re: /MAIL_PROCESSOR_DATA_DIR\s*[:=]\s*["'`]/ },
  { name: "hardcoded MAILBOX_KEY", re: /MAILBOX_KEY\s*[:=]\s*["'`]/ },
  { name: "absolute Windows path literal", re: /[A-Za-z]:\\|[A-Za-z]:\// },
];

let failed = false;
for (const rel of targets) {
  const fp = path.resolve(cwd, rel);
  if (!fs.existsSync(fp)) {
    console.error(`[check-skill-runners] missing file: ${rel}`);
    failed = true;
    continue;
  }
  const raw = fs.readFileSync(fp, "utf8");
  for (const p of forbiddenPatterns) {
    if (p.re.test(raw)) {
      console.error(`[check-skill-runners] ${rel}: ${p.name}`);
      failed = true;
    }
  }
}

if (failed) {
  process.exit(1);
}

console.log("[check-skill-runners] OK");
