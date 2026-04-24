#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

function wantsBuild(argv, env) {
  return argv.includes("--build") || env.MAIL_PROCESSOR_BUILD_BEFORE_RUN === "true";
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
const envPath = path.join(workspaceRoot, ".env");
if (!fs.existsSync(envPath)) {
  throw new Error(`Missing .env in agent workspace: ${envPath}`);
}
const envFromFile = loadDotEnv(envPath);

if (!envFromFile.AGENT_WORKSPACE_ROOT) {
  throw new Error("AGENT_WORKSPACE_ROOT must be set in <agent-workspace>/.env");
}
const configuredWorkspaceRoot = path.resolve(envFromFile.AGENT_WORKSPACE_ROOT);
if (configuredWorkspaceRoot !== workspaceRoot) {
  throw new Error(
    `AGENT_WORKSPACE_ROOT mismatch. .env=${configuredWorkspaceRoot}, inferred=${workspaceRoot}`
  );
}

const env = {
  ...process.env,
  ...envFromFile,
  AGENT_WORKSPACE_ROOT: workspaceRoot,
  MAIL_ROUTING_ENABLED: "false",
};

const repoRootFromScript = path.resolve(scriptDir, "../../..");
const projectDir =
  env.MAIL_PROCESSOR_PROJECT_DIR ||
  (fs.existsSync(path.join(repoRootFromScript, "package.json")) ? repoRootFromScript : process.cwd());
if (!fs.existsSync(path.join(projectDir, "package.json"))) {
  throw new Error(`MAIL_PROCESSOR_PROJECT_DIR invalid or missing package.json: ${projectDir}`);
}

const rawCliArgs = process.argv.slice(2);
const cliArgs = rawCliArgs.filter((arg) => arg !== "--build");
const hasDiscoverOut = cliArgs.some((a) => a === "--discover-output" || a.startsWith("--discover-output="));
if (!hasDiscoverOut) {
  const ts = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  const outDir = path.join(workspaceRoot, "memory", "references", "projects", "inbox");
  fs.mkdirSync(outDir, { recursive: true });
  cliArgs.push(`--discover-output=${path.join(outDir, `project-candidates-${ts}.json`)}`);
}

const cliPath = path.join(projectDir, "dist", "cli.js");
if (!fs.existsSync(cliPath)) {
  throw new Error(`Missing built CLI: ${cliPath}. Run npm run build in MAIL_PROCESSOR_PROJECT_DIR once.`);
}

if (wantsBuild(rawCliArgs, env)) {
  run("npm", ["run", "build"], env, projectDir);
}
run("npm", ["run", "discover-projects", "--", ...cliArgs], env, projectDir);
