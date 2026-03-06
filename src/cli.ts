#!/usr/bin/env node
import path from "node:path";
import { loadDotEnv, getConfig } from "./env.js";
import { loadProjects } from "./projects.js";
import { acquireLock } from "./lock.js";
import { appendJsonl, ensureRuntimeDirs } from "./state.js";

function parseMode(args: string[]): "shadow" | "run" {
  const modeArg = args.find((a) => a.startsWith("--mode="));
  if (!modeArg) return "shadow";
  const value = modeArg.split("=")[1];
  if (value !== "shadow" && value !== "run") {
    throw new Error("--mode must be shadow or run");
  }
  return value;
}

async function main(): Promise<void> {
  const cwd = process.cwd();
  loadDotEnv(cwd);
  const mode = parseMode(process.argv.slice(2));
  const cfg = getConfig(cwd);

  ensureRuntimeDirs([
    cfg.MAIL_PROCESSOR_DATA_DIR,
    cfg.MAIL_PROCESSOR_MSGS_DIR,
    path.dirname(cfg.MAIL_PROCESSOR_STATE_FILE),
  ]);

  const lock = acquireLock(cfg.MAIL_PROCESSOR_LOCK_FILE, cfg.MAIL_PROCESSOR_LOCK_TTL_SECONDS);
  const runId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const startedAt = new Date().toISOString();

  try {
    const projects = loadProjects(cwd, cfg.PROJECTS_JSON_PATH);

    appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
      type: "run_started",
      runId,
      mode,
      startedAt,
      projectCount: projects.length,
    });

    if (mode === "run" && !cfg.MAIL_ROUTING_ENABLED) {
      throw new Error(
        "Run mode requested, but MAIL_ROUTING_ENABLED is false. Use shadow mode or enable explicitly.",
      );
    }

    const summary = {
      inspected: 0,
      copied: 0,
      replyCopied: 0,
      skipped: 0,
      errors: 0,
      projectsLoaded: projects.length,
    };

    console.log(JSON.stringify({ ok: true, runId, mode, summary }, null, 2));

    appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
      type: "run_finished",
      runId,
      mode,
      finishedAt: new Date().toISOString(),
      summary,
    });
  } finally {
    lock.release();
  }
}

main().catch((err) => {
  console.error(`[mail-processor] ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
