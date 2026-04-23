#!/usr/bin/env node
import { spawnSync } from "node:child_process";

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

const env = {
  ...process.env,
};

run("npm", ["run", "build"], env);
run("node", ["dist/cli.js", "--mode=shadow", "--sync-mailbox-folders-force"], env);
