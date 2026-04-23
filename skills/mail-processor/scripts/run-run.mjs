#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

function parseFetchLimit(argv) {
  const arg = argv.find((a) => a.startsWith("--fetch-limit="));
  if (!arg) return undefined;
  const value = Number.parseInt(arg.split("=")[1] ?? "", 10);
  return Number.isFinite(value) && value > 0 ? String(value) : undefined;
}

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

function resolveNpmInvocation() {
  const candidates = [
    process.env.npm_execpath,
    path.join(path.dirname(process.execPath), "node_modules", "npm", "bin", "npm-cli.js"),
    path.join(path.dirname(process.execPath), "..", "node_modules", "npm", "bin", "npm-cli.js"),
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) {
      return { command: process.execPath, args: [candidate] };
    }
  }

  if (process.platform === "win32") {
    return { command: "npm.cmd", args: [] };
  }
  return { command: "npm", args: [] };
}

function run(command, args, env, cwd) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    shell: false,
    windowsHide: true,
    env,
    cwd,
  });
  if (result.error) {
    throw result.error;
  }
  if (result.signal) {
    process.kill(process.pid, result.signal);
    return;
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

const __filename = fileURLToPath(import.meta.url);
const scriptDir = path.dirname(__filename);
const workspaceRoot = path.resolve(scriptDir, "../../..");
const envFromFile = loadDotEnv(path.join(workspaceRoot, ".env"));

const fetchLimit = parseFetchLimit(process.argv.slice(2));
const env = {
  ...process.env,
  ...envFromFile,
  MAIL_ROUTING_ENABLED: "true",
  ...(fetchLimit ? { MAIL_FETCH_LIMIT: fetchLimit } : {}),
};

const repoRootFromScript = path.resolve(scriptDir, "../../..");
const projectDir =
  env.MAIL_PROCESSOR_PROJECT_DIR ||
  (fs.existsSync(path.join(repoRootFromScript, "package.json")) ? repoRootFromScript : process.cwd());
if (!fs.existsSync(path.join(projectDir, "package.json"))) {
  throw new Error(`MAIL_PROCESSOR_PROJECT_DIR invalid or missing package.json: ${projectDir}`);
}

const npm = resolveNpmInvocation();
run(npm.command, [...npm.args, "run", "build"], env, projectDir);
run(process.execPath, ["dist/cli.js", "--mode=run"], env, projectDir);
