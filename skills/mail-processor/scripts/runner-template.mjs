#!/usr/bin/env node
// Template for agent-local skill runners.
// Rules:
// - load <agent-workspace>/.env
// - do not hardcode mailbox/proxy/path settings in this file
// - only set mode toggles (e.g. MAIL_ROUTING_ENABLED) and optional runtime flags

import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

function loadDotEnv(filePath) {
  if (!fs.existsSync(filePath)) return {};
  const out = {};
  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const i = line.indexOf("=");
    if (i <= 0) continue;
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

const __filename = fileURLToPath(import.meta.url);
const scriptDir = path.dirname(__filename);
const workspaceRoot = path.resolve(scriptDir, "../../..");
const envFromFile = loadDotEnv(path.join(workspaceRoot, ".env"));

const env = {
  ...process.env,
  ...envFromFile,
  // only minimal runtime toggle here:
  MAIL_ROUTING_ENABLED: "false",
};

const projectDir = env.MAIL_PROCESSOR_PROJECT_DIR || process.cwd();
if (!fs.existsSync(path.join(projectDir, "package.json"))) {
  throw new Error(`MAIL_PROCESSOR_PROJECT_DIR invalid or missing package.json: ${projectDir}`);
}

const result = spawnSync("npm", ["run", "shadow"], {
  stdio: "inherit",
  shell: process.platform === "win32",
  env,
  cwd: projectDir,
});

if (result.status !== 0) process.exit(result.status ?? 1);
