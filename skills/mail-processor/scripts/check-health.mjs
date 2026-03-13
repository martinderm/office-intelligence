#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
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

function tryParseJson(line) {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function tailLines(filePath, maxLines = 500) {
  if (!fs.existsSync(filePath)) return [];
  const text = fs.readFileSync(filePath, "utf8");
  const lines = text.split(/\r?\n/).filter(Boolean);
  return lines.slice(-maxLines);
}

function toMs(ts) {
  const n = Date.parse(ts || "");
  return Number.isFinite(n) ? n : null;
}

const args = new Set(process.argv.slice(2));
const fixStaleLock = args.has("--fix-stale-lock");
const cleanupOrphanedRuns = args.has("--cleanup-orphaned-runs");

const __filename = fileURLToPath(import.meta.url);
const scriptDir = path.dirname(__filename);
const workspaceRoot = path.resolve(scriptDir, "../../..");
const env = {
  ...loadDotEnv(path.join(workspaceRoot, ".env")),
  ...process.env,
};

const dataDir = path.resolve(workspaceRoot, env.MAIL_PROCESSOR_DATA_DIR || "data/mail-processor");
const statePath = path.resolve(workspaceRoot, env.MAIL_PROCESSOR_STATE_FILE || path.join(dataDir, "state.jsonl"));
const lockPath = path.resolve(workspaceRoot, env.MAIL_PROCESSOR_LOCK_FILE || path.join(dataDir, "router.lock"));

const staleLockMaxSeconds = Number.parseInt(env.HEALTH_STALE_LOCK_MAX_SECONDS || "300", 10);
const recentWindowMinutes = Number.parseInt(env.HEALTH_RECENT_WINDOW_MINUTES || "60", 10);
const maxTlsErrors = Number.parseInt(env.HEALTH_MAX_TLS_ERRORS || "3", 10);
const orphanRunMaxAgeSeconds = Number.parseInt(env.HEALTH_ORPHAN_RUN_MAX_AGE_SECONDS || "900", 10);
const now = Date.now();

const findings = [];
let status = "ok";

const lock = {
  exists: fs.existsSync(lockPath),
  path: lockPath,
  stale: false,
  pid: null,
  pidAlive: null,
  ageSeconds: null,
  fixed: false,
};

if (lock.exists) {
  const raw = fs.readFileSync(lockPath, "utf8");
  const parsed = tryParseJson(raw) || {};
  lock.pid = Number.isInteger(parsed.pid) ? parsed.pid : null;
  lock.pidAlive = lock.pid == null ? false : isPidAlive(lock.pid);
  const createdAtMs = toMs(parsed.createdAt);
  lock.ageSeconds = createdAtMs == null ? null : Math.floor((now - createdAtMs) / 1000);
  const ageExceeded = (lock.ageSeconds ?? Infinity) > staleLockMaxSeconds;
  lock.stale = !lock.pidAlive && ageExceeded;

  if (lock.stale && fixStaleLock) {
    fs.unlinkSync(lockPath);
    lock.fixed = true;
    lock.exists = false;
    findings.push(`stale lock removed: ${lockPath}`);
  } else if (lock.stale) {
    findings.push(`stale lock detected: pid=${lock.pid ?? "n/a"}, age=${lock.ageSeconds ?? "n/a"}s`);
    status = "warn";
  }
}

const lines = tailLines(statePath, 1200);
const events = lines.map(tryParseJson).filter(Boolean);

const runState = new Map();
let recentTlsErrors = 0;
const recentCutoff = now - recentWindowMinutes * 60 * 1000;

for (const ev of events) {
  if (ev.runId && ev.type === "run_started") runState.set(ev.runId, { started: ev, finished: null });
  if (ev.runId && ev.type === "run_finished") {
    const current = runState.get(ev.runId) || { started: null, finished: null };
    current.finished = ev;
    runState.set(ev.runId, current);
  }
  if (ev.type === "message_error" || ev.type === "run_finished") {
    const t = toMs(ev.timestamp || ev.finishedAt);
    const msg = String(ev.error || "");
    if (t != null && t >= recentCutoff && (msg.includes("os error 10054") || msg.includes("cannot connect to IMAP server"))) {
      recentTlsErrors += 1;
    }
  }
}

const unfinished = [];
for (const [runId, pair] of runState.entries()) {
  if (!pair.started || pair.finished) continue;
  const startedAt = toMs(pair.started.startedAt);
  const ageSec = startedAt == null ? null : Math.floor((now - startedAt) / 1000);
  unfinished.push({ runId, ageSec, mode: pair.started.mode || "unknown", startedAt: pair.started.startedAt || null });
}

const orphaned = unfinished.filter((r) => (r.ageSec ?? Infinity) >= orphanRunMaxAgeSeconds);
let orphanedClosed = 0;

if (cleanupOrphanedRuns && orphaned.length > 0) {
  const append = orphaned.map((r) => JSON.stringify({
    type: "run_finished",
    runId: r.runId,
    mode: r.mode,
    status: "failed",
    error: `health cleanup: orphaned run (no run_finished after ${r.ageSec}s)`,
    finishedAt: new Date(now).toISOString(),
    summary: { inspected: 0, copied: 0, replyCopied: 0, skipped: 0, errors: 1, projectsLoaded: 0, retentionDeleted: 0 },
  })).join("\n") + "\n";
  fs.appendFileSync(statePath, append, "utf8");
  orphanedClosed = orphaned.length;
  findings.push(`orphaned runs closed: ${orphanedClosed}`);
}

if (unfinished.length > 0 && orphanedClosed === 0) {
  findings.push(`unfinished runs: ${unfinished.length}`);
  status = status === "ok" ? "warn" : status;
}

if (recentTlsErrors >= maxTlsErrors) {
  findings.push(`high TLS/IMAP disconnect rate: ${recentTlsErrors} in last ${recentWindowMinutes}m`);
  status = "crit";
}

const report = {
  ok: status === "ok",
  status,
  workspaceRoot,
  checkedAt: new Date(now).toISOString(),
  actions: { fixStaleLock, cleanupOrphanedRuns },
  lock,
  state: {
    path: statePath,
    eventsScanned: events.length,
    unfinishedRuns: unfinished,
    orphanedRuns: orphaned,
    orphanedClosed,
    recentTlsErrors,
    recentWindowMinutes,
  },
  thresholds: {
    staleLockMaxSeconds,
    maxTlsErrors,
    orphanRunMaxAgeSeconds,
  },
  findings,
};

console.log(JSON.stringify(report, null, 2));
process.exit(status === "crit" ? 2 : 0);
