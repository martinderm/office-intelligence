import fs from "node:fs";
import path from "node:path";
import { EnvConfig } from "./types.js";

function parseBool(value: string | undefined, fallback: boolean): boolean {
  if (value == null || value === "") return fallback;
  return ["1", "true", "yes", "on"].includes(value.toLowerCase());
}

function parseIntSafe(value: string | undefined, fallback: number): number {
  const n = Number.parseInt(value ?? "", 10);
  return Number.isFinite(n) ? n : fallback;
}

export function loadDotEnv(cwd: string): void {
  const envPath = path.join(cwd, ".env");
  if (!fs.existsSync(envPath)) return;

  const raw = fs.readFileSync(envPath, "utf8");
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const idx = trimmed.indexOf("=");
    if (idx <= 0) continue;
    const key = trimmed.slice(0, idx).trim();
    const value = trimmed.slice(idx + 1).trim();
    if (!(key in process.env)) process.env[key] = value;
  }
}

function parseFloatSafe(value: string | undefined, fallback: number): number {
  const n = Number.parseFloat(value ?? "");
  return Number.isFinite(n) ? n : fallback;
}

function parseRetentionDays(value: string | undefined, fallback: number | null): number | null {
  const v = (value ?? "").trim().toLowerCase();
  if (!v) return fallback;
  if (["unlimited", "none", "off", "inf", "infinite", "unbestimmt"].includes(v)) {
    return null;
  }
  const n = Number.parseInt(v, 10);
  if (!Number.isFinite(n) || n < 0) return fallback;
  return n;
}

export function getConfig(cwd: string): EnvConfig {
  const dataDir = process.env.MAIL_PROCESSOR_DATA_DIR ?? "./data/mail-routing";

  return {
    MAIL_PROCESSOR_DATA_DIR: dataDir,
    MAIL_PROCESSOR_STATE_FILE:
      process.env.MAIL_PROCESSOR_STATE_FILE ?? `${dataDir}/state.jsonl`,
    MAIL_PROCESSOR_MSGS_DIR:
      process.env.MAIL_PROCESSOR_MSGS_DIR ?? `${dataDir}/msgs`,
    MAIL_PROCESSOR_SUGGESTIONS_FILE:
      process.env.MAIL_PROCESSOR_SUGGESTIONS_FILE ?? `${dataDir}/memory_suggestions.jsonl`,
    MAIL_PROCESSOR_LOCK_FILE:
      process.env.MAIL_PROCESSOR_LOCK_FILE ?? `${dataDir}/router.lock`,
    MAIL_PROCESSOR_LOCK_TTL_SECONDS: parseIntSafe(
      process.env.MAIL_PROCESSOR_LOCK_TTL_SECONDS,
      900,
    ),
    PROJECTS_JSON_PATH:
      process.env.PROJECTS_JSON_PATH ?? "./memory/references/projects/projects.json",
    MAIL_ROUTING_ENABLED: parseBool(process.env.MAIL_ROUTING_ENABLED, false),
    LOG_LEVEL: process.env.LOG_LEVEL ?? "info",
    HIMALAYA_COMMAND: process.env.HIMALAYA_COMMAND ?? "himalaya",
    MAIL_SOURCE_FOLDER: process.env.MAIL_SOURCE_FOLDER ?? "INBOX",
    MAIL_FETCH_LIMIT: parseIntSafe(process.env.MAIL_FETCH_LIMIT, 20),
    PROJECT_MATCH_THRESHOLD: parseFloatSafe(process.env.PROJECT_MATCH_THRESHOLD, 0.65),
    NEEDS_REPLY_THRESHOLD: parseFloatSafe(process.env.NEEDS_REPLY_THRESHOLD, 0.7),
    NEEDS_REPLY_NEGATIVE_HINTS: (process.env.NEEDS_REPLY_NEGATIVE_HINTS ?? "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    MAIL_DEBUG_RETENTION_DAYS: parseRetentionDays(process.env.MAIL_DEBUG_RETENTION_DAYS, 30),
  };
}
