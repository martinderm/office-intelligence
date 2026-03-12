#!/usr/bin/env node
import { spawnSync } from "node:child_process";

function parseFetchLimit(argv) {
  const arg = argv.find((a) => a.startsWith("--fetch-limit="));
  if (!arg) return undefined;
  const value = Number.parseInt(arg.split("=")[1] ?? "", 10);
  return Number.isFinite(value) && value > 0 ? String(value) : undefined;
}

function run(command, args, env) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    shell: process.platform === "win32",
    env,
  });
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

const fetchLimit = parseFetchLimit(process.argv.slice(2));
const env = {
  ...process.env,
  MAIL_ROUTING_ENABLED: "true",
  ...(fetchLimit ? { MAIL_FETCH_LIMIT: fetchLimit } : {}),
};

run("npm", ["run", "build"], env);
run("npm", ["run", "run"], env);
