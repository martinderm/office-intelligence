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

function normalizeModelName(value: string | undefined): string {
  const v = (value ?? "").trim();
  if (!v) return "";
  if (v.includes("/")) return v.split("/").pop() ?? v;
  return v;
}

function parseSanitizeMode(value: string | undefined): "off" | "balanced" | "strict" {
  const v = (value ?? "balanced").trim().toLowerCase();
  if (v === "off" || v === "strict" || v === "balanced") return v;
  return "balanced";
}

function parseRouteAction(value: string | undefined): "auto" | "copy" | "move" {
  const v = (value ?? "auto").trim().toLowerCase();
  if (v === "auto" || v === "copy" || v === "move") return v;
  return "auto";
}

function parseCopySemantics(value: string | undefined): "normal" | "acts_like_move" {
  const v = (value ?? "normal").trim().toLowerCase();
  if (v === "normal" || v === "acts_like_move") return v;
  return "normal";
}

function parseScanMode(value: string | undefined): "auto" | "tail" | "backfill" {
  const v = (value ?? "auto").trim().toLowerCase();
  if (v === "auto" || v === "tail" || v === "backfill") return v;
  return "auto";
}

export function getConfig(cwd: string): EnvConfig {
  const dataDir = process.env.MAIL_PROCESSOR_DATA_DIR ?? "./data/mail-processor";

  return {
    MAIL_PROCESSOR_DATA_DIR: dataDir,
    MAIL_PROCESSOR_STATE_FILE:
      process.env.MAIL_PROCESSOR_STATE_FILE ?? `${dataDir}/state.jsonl`,
    MAIL_PROCESSOR_MSGS_DIR:
      process.env.MAIL_PROCESSOR_MSGS_DIR ?? `${dataDir}/msgs`,
    MAIL_PROCESSOR_SUGGESTIONS_FILE:
      process.env.MAIL_PROCESSOR_SUGGESTIONS_FILE ?? `${dataDir}/memory_suggestions.jsonl`,
    MAIL_PROCESSOR_CAPABILITIES_DIR:
      process.env.MAIL_PROCESSOR_CAPABILITIES_DIR ?? `${dataDir}/capabilities`,
    MAIL_PROCESSOR_LOCK_FILE:
      process.env.MAIL_PROCESSOR_LOCK_FILE ?? `${dataDir}/router.lock`,
    MAIL_PROCESSOR_LOCK_TTL_SECONDS: parseIntSafe(
      process.env.MAIL_PROCESSOR_LOCK_TTL_SECONDS,
      900,
    ),
    MAILBOX_FOLDERS_FILE:
      process.env.MAILBOX_FOLDERS_FILE ?? `${dataDir}/mailbox-folders.json`,
    PENDING_DECISIONS_FILE:
      process.env.PENDING_DECISIONS_FILE ?? `${dataDir}/pending-decisions.json`,
    PENDING_ACTIONS_FILE:
      process.env.PENDING_ACTIONS_FILE ?? `${dataDir}/pending-actions.json`,
    ACTION_LOG_DIR:
      process.env.ACTION_LOG_DIR ?? `${dataDir}/logs/actions`,
    MAILBOX_FOLDERS_MAX_AGE_HOURS: parseIntSafe(
      process.env.MAILBOX_FOLDERS_MAX_AGE_HOURS,
      12,
    ),
    PROJECTS_JSON_PATH:
      process.env.PROJECTS_JSON_PATH ?? "./memory/references/projects/projects.json",
    TOPICS_JSON_PATH:
      process.env.TOPICS_JSON_PATH ?? "./memory/references/topics/topics.json",
    MAIL_ROUTING_ENABLED: parseBool(process.env.MAIL_ROUTING_ENABLED, false),
    LOG_LEVEL: process.env.LOG_LEVEL ?? "info",
    HIMALAYA_COMMAND: process.env.HIMALAYA_COMMAND ?? "himalaya",
    HIMALAYA_ACCOUNT: process.env.HIMALAYA_ACCOUNT ?? "",
    MAIL_SOURCE_FOLDER: process.env.MAIL_SOURCE_FOLDER ?? "INBOX",
    MAIL_FETCH_LIMIT: parseIntSafe(process.env.MAIL_FETCH_LIMIT, 20),
    MAIL_SCAN_MODE: parseScanMode(process.env.MAIL_SCAN_MODE),
    MAIL_ENVELOPE_PAGE_SIZE: parseIntSafe(process.env.MAIL_ENVELOPE_PAGE_SIZE, parseIntSafe(process.env.MAIL_FETCH_LIMIT, 20)),
    MAIL_SELECT_MAX_SCAN_PAGES: parseIntSafe(process.env.MAIL_SELECT_MAX_SCAN_PAGES, 10),
    MAIL_CURSOR_FILE: process.env.MAIL_CURSOR_FILE ?? `${dataDir}/cursor.json`,
    MAIL_INTER_MESSAGE_DELAY_MS: parseIntSafe(process.env.MAIL_INTER_MESSAGE_DELAY_MS, 0),
    MAIL_INTER_MESSAGE_JITTER_MS: parseIntSafe(process.env.MAIL_INTER_MESSAGE_JITTER_MS, 0),
    MAIL_MESSAGE_READ_TIMEOUT_MS: parseIntSafe(process.env.MAIL_MESSAGE_READ_TIMEOUT_MS, 30000),
    MAIL_MESSAGE_READ_RETRIES: parseIntSafe(process.env.MAIL_MESSAGE_READ_RETRIES, 1),
    MAIL_MESSAGE_READ_RETRY_BACKOFF_MS: parseIntSafe(process.env.MAIL_MESSAGE_READ_RETRY_BACKOFF_MS, 1500),
    MAIL_ROUTE_ACTION: parseRouteAction(process.env.MAIL_ROUTE_ACTION),
    MAIL_COPY_SEMANTICS: parseCopySemantics(process.env.MAIL_COPY_SEMANTICS),
    MAIL_ROUTE_STRICT: parseBool(process.env.MAIL_ROUTE_STRICT, false),
    MAIL_USE_UIDPLUS: parseBool(process.env.MAIL_USE_UIDPLUS, true),
    PROJECT_MATCH_THRESHOLD: parseFloatSafe(process.env.PROJECT_MATCH_THRESHOLD, 0.65),
    NEEDS_REPLY_THRESHOLD: parseFloatSafe(process.env.NEEDS_REPLY_THRESHOLD, 0.7),
    NEEDS_REPLY_NEGATIVE_HINTS: (process.env.NEEDS_REPLY_NEGATIVE_HINTS ?? "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    MAIL_DEBUG_RETENTION_DAYS: parseRetentionDays(process.env.MAIL_DEBUG_RETENTION_DAYS, 30),
    OPENCLAW_BASE_URL: process.env.OPENCLAW_BASE_URL ?? "http://127.0.0.1:18789",
    OPENCLAW_GATEWAY_TOKEN: process.env.OPENCLAW_GATEWAY_TOKEN ?? "",
    OPENCLAW_SESSION_KEY: process.env.OPENCLAW_SESSION_KEY ?? "",
    LLM_BASE_URL: process.env.LLM_BASE_URL ?? "",
    LLM_API_KEY: process.env.LLM_API_KEY ?? "",
    LLM_MODEL: normalizeModelName(process.env.LLM_MODEL),
    LLM_ENABLED: parseBool(process.env.LLM_ENABLED, true),
    LLM_TIMEOUT_MS: parseIntSafe(process.env.LLM_TIMEOUT_MS, 60000),
    LLM_PROMPT_PATH: process.env.LLM_PROMPT_PATH || undefined,
    MAIL_SANITIZE_ENABLED: parseBool(process.env.MAIL_SANITIZE_ENABLED, true),
    MAIL_SANITIZE_MODE: parseSanitizeMode(process.env.MAIL_SANITIZE_MODE),
    MAIL_STRIP_TRACKING_PARAMS: parseBool(process.env.MAIL_STRIP_TRACKING_PARAMS, true),
    MAIL_NEWSLETTER_FOOTER_TRIM: parseBool(process.env.MAIL_NEWSLETTER_FOOTER_TRIM, true),
    MAIL_HTML_MAX_CURRENT: parseIntSafe(process.env.MAIL_HTML_MAX_CURRENT, 5000),
    MAIL_HTML_MAX_QUOTED: parseIntSafe(process.env.MAIL_HTML_MAX_QUOTED, 1200),
  };
}
