#!/usr/bin/env node
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
    const key = line.slice(0, i).trim();
    const value = line.slice(i + 1).trim();
    out[key] = value;
  }
  return out;
}

function run(command, args, env, cwd) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    shell: process.platform === "win32",
    env,
    cwd,
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

const __filename = fileURLToPath(import.meta.url);
const scriptDir = path.dirname(__filename);
const workspaceRoot = path.resolve(scriptDir, "../../..");
const envFromFile = loadDotEnv(path.join(workspaceRoot, ".env"));

const env = {
  ...process.env,
  ...envFromFile,
  MAIL_ROUTING_ENABLED: "false",
};

const projectDir = env.MAIL_PROCESSOR_PROJECT_DIR || process.cwd();
if (!fs.existsSync(path.join(projectDir, "package.json"))) {
  throw new Error(`MAIL_PROCESSOR_PROJECT_DIR invalid or missing package.json: ${projectDir}`);
}

run("npm", ["run", "build"], env, projectDir);
run("npm", ["run", "discover-projects", "--", ...process.argv.slice(2)], env, projectDir);
