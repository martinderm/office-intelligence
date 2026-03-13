#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { loadDotEnv, getConfig } from "./env.js";
import { loadProjects } from "./projects.js";
import { acquireLock } from "./lock.js";
import { appendJsonl, ensureRuntimeDirs } from "./state.js";
import { copyMessage, copyMessageWithUidPlus, listEnvelopesPage, moveMessage, readMessage } from "./mail-source.js";
import { computeNextAttemptAtMs, loadDueRetryItems, makeRetryKey } from "./retry-queue.js";
import { getProcessedIds } from "./idempotency.js";
import { matchProject, mergeHeuristicAndLlm, needsReplyHeuristic } from "./matcher.js";
import { cleanupDebugMessages } from "./retention.js";
import { prepareMailText } from "./preprocess.js";
import { extractWithLlm } from "./llm.js";
import { loadOrFetchCapabilities } from "./capabilities.js";
import { parseDiscoverOptions, runDiscoverProjects } from "./discover-projects.js";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isTransientReadError(err: unknown): boolean {
  const msg = (err instanceof Error ? err.message : String(err)).toLowerCase();
  return (
    msg.includes("os error 10054") ||
    msg.includes("cannot connect to tls stream") ||
    msg.includes("cannot connect to imap server") ||
    msg.includes("command timeout") ||
    msg.includes("operation was aborted")
  );
}

function parseMode(args: string[]): "shadow" | "run" {
  const modeArg = args.find((a) => a.startsWith("--mode="));
  if (!modeArg) return "shadow";
  const value = modeArg.split("=")[1];
  if (value !== "shadow" && value !== "run") {
    throw new Error("--mode must be shadow or run");
  }
  return value;
}

type CursorState = {
  version: 1;
  mailbox: string;
  folder: string;
  backfill: {
    nextPage: number;
    nextIndex: number;
    completed: boolean;
  };
  updatedAt: string;
};

function loadCursorState(cursorFile: string, mailbox: string, folder: string): CursorState {
  try {
    const raw = fs.readFileSync(cursorFile, "utf8");
    const parsed = JSON.parse(raw) as CursorState;
    if (parsed.mailbox === mailbox && parsed.folder === folder && parsed.backfill?.nextPage) {
      return {
        ...parsed,
        backfill: {
          nextPage: parsed.backfill.nextPage,
          nextIndex: Number.isFinite((parsed.backfill as any).nextIndex) ? (parsed.backfill as any).nextIndex : 0,
          completed: Boolean(parsed.backfill.completed),
        },
      };
    }
  } catch {
    // ignore and recreate
  }
  return {
    version: 1,
    mailbox,
    folder,
    backfill: {
      nextPage: 1,
      nextIndex: 0,
      completed: false,
    },
    updatedAt: new Date().toISOString(),
  };
}

function saveCursorState(cursorFile: string, state: CursorState): void {
  state.updatedAt = new Date().toISOString();
  fs.mkdirSync(path.dirname(cursorFile), { recursive: true });
  fs.writeFileSync(cursorFile, JSON.stringify(state, null, 2), "utf8");
}

function parseExplicitEnvelopeIds(args: string[]): string[] {
  const arg = args.find((a) => a.startsWith("--ids="));
  if (!arg) return [];
  return arg
    .slice("--ids=".length)
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

function normalizeMessageId(value?: string): string | undefined {
  if (!value) return undefined;
  const v = value.trim().toLowerCase().replace(/^<+|>+$/g, "");
  return v || undefined;
}

function fallbackStableId(meta: { from?: string; date?: string; subject?: string }, bodyText: string): string {
  const basis = `${meta.from || ""}|${meta.date || ""}|${meta.subject || ""}|${bodyText.slice(0, 1200)}`;
  const hash = crypto.createHash("sha256").update(basis, "utf8").digest("hex").slice(0, 20);
  return `fallback:${hash}`;
}

type EffectiveRouting = {
  requestedAction: "auto" | "copy" | "move";
  effectiveAction: "copy" | "move";
  copySemantics: "normal" | "acts_like_move";
  effectiveMove: boolean;
  supportsMove: boolean;
  supportsUidPlus: boolean;
  useUidPlus: boolean;
  forcedSingleTarget: boolean;
};

type RoutingHistoryEntry = {
  at: string;
  event: "observed" | "routed_copy" | "routed_move";
  envelopeId: string;
  fromFolder?: string;
  toFolders?: string[];
  folder?: string;
};

function folderToSlug(folder: string): string {
  const parts = folder
    .split(/[\\/]+/)
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => p.toLowerCase().replace(/[^a-z0-9._-]+/g, "-"))
    .filter(Boolean);
  return parts.length ? parts.join("__") : "unknown-folder";
}

function fileIdFromStableId(stableId: string): string {
  return crypto.createHash("sha256").update(stableId, "utf8").digest("base64url").slice(0, 16);
}

function moveExportArtifact(dataDir: string, envelopeId: string, fileId: string, folder: string): string {
  const exportsBaseDir = path.resolve(dataDir, "exports");
  const sourcePath = path.join(exportsBaseDir, `${envelopeId}.eml`);
  const targetDir = path.join(exportsBaseDir, folderToSlug(folder));
  const targetPath = path.join(targetDir, `${fileId}.eml`);

  fs.mkdirSync(targetDir, { recursive: true });

  if (!fs.existsSync(sourcePath)) {
    return targetPath;
  }

  if (fs.existsSync(targetPath)) {
    fs.unlinkSync(sourcePath);
    return targetPath;
  }

  const tempPath = path.join(targetDir, `${fileId}.tmp-${process.pid}-${Date.now()}`);
  fs.copyFileSync(sourcePath, tempPath);
  fs.renameSync(tempPath, targetPath);
  fs.unlinkSync(sourcePath);

  return targetPath;
}

function findMessageArtifactByStableId(msgsDir: string, stableId: string): string | undefined {
  const wantedHashed = `${fileIdFromStableId(stableId)}.json`;
  const wantedLegacy = `${stableId}.json`;
  const stack = [path.resolve(msgsDir)];

  while (stack.length) {
    const dir = stack.pop();
    if (!dir || !fs.existsSync(dir)) continue;

    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
        continue;
      }
      if (entry.isFile() && (entry.name === wantedHashed || entry.name === wantedLegacy)) {
        return full;
      }
    }
  }

  return undefined;
}

function isCompleteMessageArtifact(raw: string, stableId: string, llmEnabled: boolean): boolean {
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (parsed.stableId !== stableId) return false;
    if (!("match" in parsed)) return false;
    if (typeof parsed.needsReply !== "boolean") return false;
    if (!("mailMeta" in parsed)) return false;
    if (llmEnabled && !("llm" in parsed)) return false;
    return true;
  } catch {
    return false;
  }
}

function selectEnvelopesForRun(params: {
  args: string[];
  cfg: ReturnType<typeof getConfig>;
  cursor: CursorState;
  priorityEnvelopeIds?: string[];
}): {
  envelopes: Array<{ id: string; rawLine: string }>;
  strategy: string;
  scan: {
    requestedMaxScanPages: number;
    effectiveMaxScanPages: number;
    scannedPages: number;
    pageSize: number;
    fetchLimit: number;
  };
} {
  const { args, cfg, cursor, priorityEnvelopeIds } = params;
  const explicitIds = parseExplicitEnvelopeIds(args);
  const cap = Math.max(1, cfg.MAIL_FETCH_LIMIT);
  const pageSize = Math.max(1, cfg.MAIL_ENVELOPE_PAGE_SIZE);

  if (explicitIds.length) {
    return {
      envelopes: explicitIds.map((id) => ({ id, rawLine: `[explicit] ${id}` })),
      strategy: "explicit_ids",
      scan: {
        requestedMaxScanPages: cfg.MAIL_SELECT_MAX_SCAN_PAGES,
        effectiveMaxScanPages: 0,
        scannedPages: 0,
        pageSize,
        fetchLimit: cap,
      },
    };
  }

  if (cfg.HIMALAYA_COMMAND === "mock") {
    return {
      envelopes: [{ id: "mock-1", rawLine: "mock-1 Example subject" }],
      strategy: "mock",
      scan: {
        requestedMaxScanPages: cfg.MAIL_SELECT_MAX_SCAN_PAGES,
        effectiveMaxScanPages: 0,
        scannedPages: 0,
        pageSize,
        fetchLimit: cap,
      },
    };
  }

  const unique = new Map<string, { id: string; rawLine: string }>();

  // Priority: retry queue items (due) are always processed first.
  if (priorityEnvelopeIds?.length) {
    for (const id of priorityEnvelopeIds) {
      if (!unique.has(id)) unique.set(id, { id, rawLine: `[retry] ${id}` });
      if (unique.size >= cap) break;
    }
  }

  const pushTailPage = (page: number) => {
    const rows = listEnvelopesPage(cfg.HIMALAYA_COMMAND, cfg.MAIL_SOURCE_FOLDER, page, pageSize);
    for (const e of rows) {
      if (!unique.has(e.id)) unique.set(e.id, e);
      if (unique.size >= cap) break;
    }
  };

  let scannedPages = 0;
  let effectiveMaxScanPages = 0;

  const consumeBackfillFromCursor = () => {
    let page = Math.max(1, cursor.backfill.nextPage || 1);
    let index = Math.max(0, cursor.backfill.nextIndex || 0);

    // cfg.MAIL_SELECT_MAX_SCAN_PAGES is treated as a *baseline* safety limit.
    // If MAIL_FETCH_LIMIT requires more pages (MAIL_ENVELOPE_PAGE_SIZE * pages),
    // we automatically raise the effective limit so we can actually reach the fetch-limit.
    // Example: fetchLimit=200, pageSize=20 => need at least 10 pages.
    const pagesNeededForFetchLimit = Math.max(1, Math.ceil(cap / pageSize));
    let dynamicMaxScanPages = Math.max(cfg.MAIL_SELECT_MAX_SCAN_PAGES, pagesNeededForFetchLimit);

    // Hard stop to avoid runaway scans on very large mailboxes.
    const HARD_MAX_SCAN_PAGES = Math.max(dynamicMaxScanPages, 500);

    while (unique.size < cap) {
      if (scannedPages >= dynamicMaxScanPages) {
        // Still not enough unique envelopes to satisfy fetchLimit → keep extending,
        // but never beyond the hard stop.
        if (dynamicMaxScanPages >= HARD_MAX_SCAN_PAGES) break;
        dynamicMaxScanPages = Math.min(HARD_MAX_SCAN_PAGES, dynamicMaxScanPages + 1);
      }

      const rows = listEnvelopesPage(cfg.HIMALAYA_COMMAND, cfg.MAIL_SOURCE_FOLDER, page, pageSize);
      scannedPages += 1;

      if (rows.length === 0) {
        cursor.backfill.completed = true;
        break;
      }

      let i = index;
      while (i < rows.length && unique.size < cap) {
        const e = rows[i];
        if (!unique.has(e.id)) unique.set(e.id, e);
        i += 1;
      }

      if (i >= rows.length) {
        page += 1;
        index = 0;
      } else {
        index = i;
      }
    }

    cursor.backfill.nextPage = page;
    cursor.backfill.nextIndex = index;
    effectiveMaxScanPages = dynamicMaxScanPages;
  };

  if (cfg.MAIL_SCAN_MODE === "tail") {
    pushTailPage(1);
    return {
      envelopes: [...unique.values()].slice(0, cap),
      strategy: "tail_page_1",
      scan: {
        requestedMaxScanPages: cfg.MAIL_SELECT_MAX_SCAN_PAGES,
        effectiveMaxScanPages: 0,
        scannedPages: 0,
        pageSize,
        fetchLimit: cap,
      },
    };
  }

  if (cfg.MAIL_SCAN_MODE === "backfill") {
    consumeBackfillFromCursor();
    return {
      envelopes: [...unique.values()].slice(0, cap),
      strategy: "backfill_cursor",
      scan: {
        requestedMaxScanPages: cfg.MAIL_SELECT_MAX_SCAN_PAGES,
        effectiveMaxScanPages,
        scannedPages,
        pageSize,
        fetchLimit: cap,
      },
    };
  }

  // auto: first newest page, then continue from backfill cursor
  pushTailPage(1);
  if (unique.size < cap) {
    consumeBackfillFromCursor();
  }
  return {
    envelopes: [...unique.values()].slice(0, cap),
    strategy: "auto_tail_plus_backfill",
    scan: {
      requestedMaxScanPages: cfg.MAIL_SELECT_MAX_SCAN_PAGES,
      effectiveMaxScanPages,
      scannedPages,
      pageSize,
      fetchLimit: cap,
    },
  };
}

function resolveEffectiveRouting(cfg: ReturnType<typeof getConfig>, supportsMove: boolean, supportsUidPlus: boolean): EffectiveRouting {
  const requestedAction = cfg.MAIL_ROUTE_ACTION;
  let effectiveAction: "copy" | "move" = "copy";

  if (requestedAction === "move") {
    if (supportsMove) {
      effectiveAction = "move";
    } else if (cfg.MAIL_ROUTE_STRICT) {
      throw new Error("MAIL_ROUTE_ACTION=move requested, but server does not advertise MOVE capability");
    } else {
      effectiveAction = "copy";
    }
  } else if (requestedAction === "copy") {
    effectiveAction = "copy";
  } else {
    // auto
    effectiveAction = supportsMove ? "move" : "copy";
  }

  const effectiveMove = effectiveAction === "move" || cfg.MAIL_COPY_SEMANTICS === "acts_like_move";

  return {
    requestedAction,
    effectiveAction,
    copySemantics: cfg.MAIL_COPY_SEMANTICS,
    effectiveMove,
    supportsMove,
    supportsUidPlus,
    useUidPlus: cfg.MAIL_USE_UIDPLUS && supportsUidPlus,
    forcedSingleTarget: effectiveMove,
  };
}

async function main(): Promise<void> {
  const cwd = process.cwd();
  loadDotEnv(cwd);
  const args = process.argv.slice(2);
  const mode = parseMode(args);
  const cfg = getConfig(cwd);
  const discover = parseDiscoverOptions(args, cfg.MAIL_FETCH_LIMIT);

  if (discover.enabled) {
    await runDiscoverProjects(cwd, cfg, discover);
    return;
  }

  ensureRuntimeDirs([
    cfg.MAIL_PROCESSOR_DATA_DIR,
    cfg.MAIL_PROCESSOR_MSGS_DIR,
    cfg.MAIL_PROCESSOR_CAPABILITIES_DIR,
    path.dirname(cfg.MAIL_PROCESSOR_STATE_FILE),
    path.dirname(cfg.MAIL_CURSOR_FILE),
  ]);

  const lock = acquireLock(cfg.MAIL_PROCESSOR_LOCK_FILE, cfg.MAIL_PROCESSOR_LOCK_TTL_SECONDS);
  const runId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const startedAt = new Date().toISOString();
  const mailboxKey = process.env.MAILBOX_KEY || process.env.HIMALAYA_MAILBOX_KEY || "default";
  const cursor = loadCursorState(cfg.MAIL_CURSOR_FILE, mailboxKey, cfg.MAIL_SOURCE_FOLDER);

  let summary = {
    inspected: 0,
    copied: 0,
    replyCopied: 0,
    skipped: 0,
    errors: 0,
    projectsLoaded: 0,
    retentionDeleted: 0,
  };
  let runStatus: "ok" | "failed" = "failed";
  let fatalError: string | null = null;

  try {
    const projects = loadProjects(cwd, cfg.PROJECTS_JSON_PATH);
    const projectHints = projects
      .map((p) => `${p.id} | ${p.title}${p.aliases?.length ? ` | aliases: ${p.aliases.join(", ")}` : ""}`)
      .join("\n");

    if (mode === "run" && !cfg.MAIL_ROUTING_ENABLED) {
      throw new Error(
        "Run mode requested, but MAIL_ROUTING_ENABLED is false. Use shadow mode or enable explicitly.",
      );
    }

    summary = {
      inspected: 0,
      copied: 0,
      replyCopied: 0,
      skipped: 0,
      errors: 0,
      projectsLoaded: projects.length,
      retentionDeleted: cleanupDebugMessages(cfg.MAIL_PROCESSOR_MSGS_DIR, cfg.MAIL_DEBUG_RETENTION_DAYS),
    };

    appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
      type: "run_started",
      runId,
      mode,
      startedAt,
      sourceFolder: cfg.MAIL_SOURCE_FOLDER,
      fetchLimit: cfg.MAIL_FETCH_LIMIT,
      projectCount: projects.length,
      llmEnabled: cfg.LLM_ENABLED,
      llmModel: cfg.LLM_MODEL || null,
    });

    let supportsMove = false;
    let supportsUidPlus = false;

    if (cfg.HIMALAYA_COMMAND !== "mock") {
      const capabilityRecord = loadOrFetchCapabilities({
        command: cfg.HIMALAYA_COMMAND,
        sourceFolder: cfg.MAIL_SOURCE_FOLDER,
        capabilitiesDir: cfg.MAIL_PROCESSOR_CAPABILITIES_DIR,
        mailboxKey: process.env.MAILBOX_KEY || process.env.HIMALAYA_MAILBOX_KEY || undefined,
      });
      supportsMove = capabilityRecord.policy.supportsMove;
      supportsUidPlus = capabilityRecord.policy.supportsUidPlus;

      appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
        type: "mailbox_capabilities_loaded",
        runId,
        mailboxKey: capabilityRecord.mailboxKey,
        host: capabilityRecord.host || null,
        fetchedAt: capabilityRecord.fetchedAt,
        capabilityCount: capabilityRecord.capabilities.length,
        capabilities: capabilityRecord.capabilities,
        policy: capabilityRecord.policy,
        sourceFolder: cfg.MAIL_SOURCE_FOLDER,
        timestamp: new Date().toISOString(),
      });
    }

    const routing = resolveEffectiveRouting(cfg, supportsMove, supportsUidPlus);

    appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
      type: "routing_policy_resolved",
      runId,
      requestedAction: routing.requestedAction,
      effectiveAction: routing.effectiveAction,
      copySemantics: routing.copySemantics,
      supportsMove: routing.supportsMove,
      supportsUidPlus: routing.supportsUidPlus,
      useUidPlus: routing.useUidPlus,
      effectiveMove: routing.effectiveMove,
      forcedSingleTarget: routing.forcedSingleTarget,
      strictMode: cfg.MAIL_ROUTE_STRICT,
      timestamp: new Date().toISOString(),
    });

    const processed = getProcessedIds(cfg.MAIL_PROCESSOR_STATE_FILE);

    // Transient-read retry queue (global): items are re-attempted across runs without blocking progress.
    // Spec per request: max-attempts=2, base backoff=30s.
    const retryQueuePath = path.join(cfg.MAIL_PROCESSOR_DATA_DIR, "retry-queue.jsonl");
    const retryDeadLetterPath = path.join(cfg.MAIL_PROCESSOR_DATA_DIR, "retry-dead-letter.jsonl");
    const retryMaxAttempts = 2;
    const retryBaseBackoffMs = 30_000;

    const dueRetry = loadDueRetryItems({ queuePath: retryQueuePath, nowMs: Date.now(), limit: cfg.MAIL_FETCH_LIMIT });
    const dueRetryIds = dueRetry
      .filter((x) => x.sourceFolder === cfg.MAIL_SOURCE_FOLDER)
      .map((x) => x.envelopeId);
    const dueRetryKeys = new Set(dueRetry.map((x) => x.key));

    const selection = selectEnvelopesForRun({ args, cfg, cursor, priorityEnvelopeIds: dueRetryIds });
    const envelopes = selection.envelopes;

    appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
      type: "selection_resolved",
      runId,
      mode,
      scanMode: cfg.MAIL_SCAN_MODE,
      strategy: selection.strategy,
      selectedCount: envelopes.length,
      fetchLimit: selection.scan.fetchLimit,
      envelopePageSize: selection.scan.pageSize,
      requestedMaxScanPages: selection.scan.requestedMaxScanPages,
      effectiveMaxScanPages: selection.scan.effectiveMaxScanPages,
      scannedPages: selection.scan.scannedPages,
      cursorFile: cfg.MAIL_CURSOR_FILE,
      cursor: cursor.backfill,
      timestamp: new Date().toISOString(),
    });

    for (let i = 0; i < envelopes.length; i += 1) {
      const env = envelopes[i];
      summary.inspected += 1;
      try {
        let msg:
          | {
              id: string;
              raw: string;
            }
          | undefined;

        if (cfg.HIMALAYA_COMMAND === "mock") {
          msg = {
            id: env.id,
            raw: "Subject: [EXAMPLE] Bitte um Rückmeldung\nFrom: contact@example.org\nBody: Kannst du bis morgen antworten?",
          };
        } else {
          try {
            const maxAttempts = Math.max(1, cfg.MAIL_MESSAGE_READ_RETRIES + 1);
            for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
              const started = Date.now();
              try {
                msg = readMessage(
                  cfg.HIMALAYA_COMMAND,
                  cfg.MAIL_SOURCE_FOLDER,
                  env.id,
                  cfg.MAIL_PROCESSOR_DATA_DIR,
                  cfg.MAIL_MESSAGE_READ_TIMEOUT_MS,
                );
                appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
                  type: "message_read_attempt",
                  runId,
                  sourceFolder: cfg.MAIL_SOURCE_FOLDER,
                  envelopeId: env.id,
                  attempt,
                  maxAttempts,
                  transient: false,
                  ok: true,
                  durationMs: Date.now() - started,
                  timeoutMs: cfg.MAIL_MESSAGE_READ_TIMEOUT_MS,
                  timestamp: new Date().toISOString(),
                });
                break;
              } catch (readErr) {
                const transient = isTransientReadError(readErr);
                const canRetry = transient && attempt < maxAttempts;
                appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
                  type: "message_read_attempt",
                  runId,
                  sourceFolder: cfg.MAIL_SOURCE_FOLDER,
                  envelopeId: env.id,
                  attempt,
                  maxAttempts,
                  transient,
                  ok: false,
                  durationMs: Date.now() - started,
                  timeoutMs: cfg.MAIL_MESSAGE_READ_TIMEOUT_MS,
                  willRetry: canRetry,
                  error: readErr instanceof Error ? readErr.message : String(readErr),
                  timestamp: new Date().toISOString(),
                });
                if (!canRetry) throw readErr;
                const backoff = Math.max(0, cfg.MAIL_MESSAGE_READ_RETRY_BACKOFF_MS * attempt);
                if (backoff > 0) {
                  await sleep(backoff);
                }
              }
            }
            if (!msg) {
              throw new Error(`message read failed without result for envelope ${env.id}`);
            }
          } catch (readErr) {
            // Transient read errors should not block progress: enqueue into persistent retry queue.
            if (isTransientReadError(readErr)) {
              const key = makeRetryKey(cfg.MAIL_SOURCE_FOLDER, env.id);
              const nowMs = Date.now();
              // Determine prior attempts from the due list (if this run is processing a queued item).
              const prev = dueRetry.find((x) => x.key === key);
              const attempts = (prev?.attempts || 0) + 1;
              const errMsg = readErr instanceof Error ? readErr.message : String(readErr);

              if (attempts >= retryMaxAttempts) {
                fs.appendFileSync(
                  retryDeadLetterPath,
                  `${JSON.stringify({
                    type: "retry_dead_letter",
                    key,
                    sourceFolder: cfg.MAIL_SOURCE_FOLDER,
                    envelopeId: env.id,
                    attempts,
                    error: errMsg,
                    timestamp: new Date().toISOString(),
                  })}\n`,
                  "utf8",
                );
                appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
                  type: "message_deferred_transient",
                  runId,
                  sourceFolder: cfg.MAIL_SOURCE_FOLDER,
                  envelopeId: env.id,
                  action: "dead_letter",
                  attempts,
                  maxAttempts: retryMaxAttempts,
                  error: errMsg,
                  timestamp: new Date().toISOString(),
                });
              } else {
                const nextAttemptAtMs = computeNextAttemptAtMs({ nowMs, attempts, baseBackoffMs: retryBaseBackoffMs });
                fs.appendFileSync(
                  retryQueuePath,
                  `${JSON.stringify({
                    type: "retry_enqueued",
                    key,
                    sourceFolder: cfg.MAIL_SOURCE_FOLDER,
                    envelopeId: env.id,
                    attempts,
                    nextAttemptAtMs,
                    error: errMsg,
                    timestamp: new Date().toISOString(),
                  })}\n`,
                  "utf8",
                );
                appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
                  type: "message_deferred_transient",
                  runId,
                  sourceFolder: cfg.MAIL_SOURCE_FOLDER,
                  envelopeId: env.id,
                  action: "enqueued",
                  attempts,
                  maxAttempts: retryMaxAttempts,
                  nextAttemptAt: new Date(nextAttemptAtMs).toISOString(),
                  error: errMsg,
                  timestamp: new Date().toISOString(),
                });
              }

              summary.skipped += 1;
              continue;
            }
            throw readErr;
          }
        }
        // If this envelope came from the persistent retry queue, mark it as succeeded.
        const retryKey = makeRetryKey(cfg.MAIL_SOURCE_FOLDER, env.id);
        if (dueRetryKeys.has(retryKey)) {
          fs.appendFileSync(
            retryQueuePath,
            `${JSON.stringify({
              type: "retry_succeeded",
              key: retryKey,
              sourceFolder: cfg.MAIL_SOURCE_FOLDER,
              envelopeId: env.id,
              timestamp: new Date().toISOString(),
            })}\n`,
            "utf8",
          );
          appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
            type: "retry_succeeded",
            runId,
            sourceFolder: cfg.MAIL_SOURCE_FOLDER,
            envelopeId: env.id,
            timestamp: new Date().toISOString(),
          });
        }

        const prepared = prepareMailText(
          msg.raw,
          cfg.MAIL_HTML_MAX_CURRENT,
          cfg.MAIL_HTML_MAX_QUOTED,
          {
            enabled: cfg.MAIL_SANITIZE_ENABLED,
            mode: cfg.MAIL_SANITIZE_MODE,
            stripTrackingParams: cfg.MAIL_STRIP_TRACKING_PARAMS,
            trimNewsletterFooter: cfg.MAIL_NEWSLETTER_FOOTER_TRIM,
          },
        );
        const normalizedMessageId = normalizeMessageId(prepared.meta.messageId);
        const stableId = normalizedMessageId || fallbackStableId(prepared.meta, prepared.effectiveText);
        const fileId = fileIdFromStableId(stableId);

        if (processed.has(stableId)) {
          summary.skipped += 1;
          appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
            type: "message_skipped",
            runId,
            sourceFolder: cfg.MAIL_SOURCE_FOLDER,
            envelopeId: env.id,
            stableId,
            reason: "already_processed",
            timestamp: new Date().toISOString(),
          });
          continue;
        }

        const existingArtifactPath = findMessageArtifactByStableId(cfg.MAIL_PROCESSOR_MSGS_DIR, stableId);
        if (existingArtifactPath) {
          const existingRaw = fs.readFileSync(existingArtifactPath, "utf8");
          if (isCompleteMessageArtifact(existingRaw, stableId, cfg.LLM_ENABLED)) {
            summary.skipped += 1;
            processed.add(stableId);
            appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
              type: "message_skipped",
              runId,
              sourceFolder: cfg.MAIL_SOURCE_FOLDER,
              envelopeId: env.id,
              stableId,
              reason: "existing_complete_artifact",
              artifactPath: existingArtifactPath,
              timestamp: new Date().toISOString(),
            });
            continue;
          }
        }

        const heuristicMatch = matchProject(prepared.effectiveText, projects);

        let llm: any = undefined;
        if (
          cfg.LLM_ENABLED &&
          cfg.HIMALAYA_COMMAND !== "mock" &&
          cfg.LLM_BASE_URL &&
          cfg.LLM_API_KEY &&
          cfg.LLM_MODEL
        ) {
          try {
            llm = await extractWithLlm({
              cwd,
              baseUrl: cfg.LLM_BASE_URL,
              apiKey: cfg.LLM_API_KEY,
              model: cfg.LLM_MODEL,
              mailText: prepared.effectiveText,
              projectHints,
              promptPath: cfg.LLM_PROMPT_PATH,
              timeoutMs: cfg.LLM_TIMEOUT_MS,
            });
          } catch (llmErr) {
            appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
              type: "llm_parse_error",
              runId,
              sourceFolder: cfg.MAIL_SOURCE_FOLDER,
              envelopeId: env.id,
              error: llmErr instanceof Error ? llmErr.message : String(llmErr),
              fallback: "heuristic_only",
              timestamp: new Date().toISOString(),
            });
            llm = undefined;
          }
        }

        const match = mergeHeuristicAndLlm(heuristicMatch, llm, projects);
        const needsReplyHeuristicFlag = needsReplyHeuristic(
          prepared.effectiveText,
          cfg.NEEDS_REPLY_NEGATIVE_HINTS,
        );
        const llmNeedsReplyScore = Number(llm?.needsReply?.score || 0);
        const needsReply = Math.max(needsReplyHeuristicFlag ? 0.65 : 0, llmNeedsReplyScore) >= cfg.NEEDS_REPLY_THRESHOLD;

        const copyTargets: string[] = [];

        if (mode === "run" && match.projectId && match.score >= cfg.PROJECT_MATCH_THRESHOLD) {
          const project = projects.find((p) => p.id === match.projectId);
          if (project) {
            const plannedTargets: string[] = [project.mailbox_folder];
            if (needsReply) {
              plannedTargets.push("Projekte/_Needs-Reply");
            }

            const executionTargets = routing.forcedSingleTarget ? plannedTargets.slice(0, 1) : plannedTargets;
            const skippedTargets = plannedTargets.slice(executionTargets.length);

            if (cfg.HIMALAYA_COMMAND !== "mock") {
              for (const target of executionTargets) {
                let uidPlusCopy: Record<string, unknown> | undefined;
                if (routing.effectiveAction === "move") {
                  moveMessage(cfg.HIMALAYA_COMMAND, target, env.id);
                } else if (routing.useUidPlus) {
                  const routeResult = copyMessageWithUidPlus(cfg.HIMALAYA_COMMAND, target, env.id);
                  if (routeResult.uidPlus) {
                    uidPlusCopy = routeResult.uidPlus as unknown as Record<string, unknown>;
                  }
                } else {
                  copyMessage(cfg.HIMALAYA_COMMAND, target, env.id);
                }
                copyTargets.push(target);

                if (uidPlusCopy) {
                  appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
                    type: "uidplus_copy_mapping",
                    runId,
                    envelopeId: env.id,
                    stableId,
                    targetFolder: target,
                    mapping: uidPlusCopy,
                    timestamp: new Date().toISOString(),
                  });
                }

                if (target === project.mailbox_folder) {
                  summary.copied += 1;
                }
                if (target === "Projekte/_Needs-Reply") {
                  summary.replyCopied += 1;
                }
              }
            } else {
              copyTargets.push(...executionTargets);
            }

            if (skippedTargets.length) {
              appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
                type: "routing_targets_skipped",
                runId,
                envelopeId: env.id,
                stableId,
                skippedTargets,
                reason: "effective_move_single_target",
                timestamp: new Date().toISOString(),
              });
            }
          }
        }

        const finalFolder = routing.effectiveMove && copyTargets.length
          ? copyTargets[copyTargets.length - 1]
          : cfg.MAIL_SOURCE_FOLDER;

        const history: RoutingHistoryEntry[] = [
          {
            at: new Date().toISOString(),
            event: "observed",
            envelopeId: env.id,
            folder: cfg.MAIL_SOURCE_FOLDER,
          },
        ];

        if (copyTargets.length) {
          history.push({
            at: new Date().toISOString(),
            event: routing.effectiveMove ? "routed_move" : "routed_copy",
            envelopeId: env.id,
            fromFolder: cfg.MAIL_SOURCE_FOLDER,
            toFolders: [...copyTargets],
          });
        }

        const msgFolderDir = path.resolve(cfg.MAIL_PROCESSOR_MSGS_DIR, folderToSlug(finalFolder));
        fs.mkdirSync(msgFolderDir, { recursive: true });
        const debugPath = path.join(msgFolderDir, `${fileId}.json`);

        const localExportPath = moveExportArtifact(cfg.MAIL_PROCESSOR_DATA_DIR, env.id, fileId, finalFolder);

        fs.writeFileSync(
          debugPath,
          JSON.stringify(
            {
              id: env.id,
              stableId,
              envelope: env.rawLine,
              match,
              llm,
              needsReply,
              llmNeedsReplyScore,
              preprocessing: {
                truncated: prepared.truncated,
                originalChars: prepared.originalChars,
                keptChars: prepared.keptChars,
              },
              mailMeta: prepared.meta,
              sanitizing: prepared.sanitizing,
              local: {
                fileId,
                msgPath: debugPath,
                exportPath: localExportPath,
                folder: finalFolder,
              },
              history,
              preview: prepared.effectiveText.slice(0, 2000),
            },
            null,
            2,
          ),
          "utf8",
        );

        processed.add(stableId);

        appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
          type: "message_processed",
          runId,
          sourceFolder: cfg.MAIL_SOURCE_FOLDER,
          envelopeId: env.id,
          messageId: normalizedMessageId || prepared.meta.messageId || env.id,
          stableId,
          mode,
          matchedProjectId: match.projectId,
          score: match.score,
          needsReply,
          copied: mode === "run" ? summary.copied : 0,
          copyTargets,
          routeActionRequested: routing.requestedAction,
          routeActionEffective: routing.effectiveAction,
          copySemantics: routing.copySemantics,
          supportsMove: routing.supportsMove,
          supportsUidPlus: routing.supportsUidPlus,
          useUidPlus: routing.useUidPlus,
          effectiveMove: routing.effectiveMove,
          forcedSingleTarget: routing.forcedSingleTarget,
          lastKnownEnvelopeId: env.id,
          lastKnownFolder: finalFolder,
          timestamp: new Date().toISOString(),
        });
      } catch (error) {
        summary.errors += 1;
        appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
          type: "message_error",
          runId,
          sourceFolder: cfg.MAIL_SOURCE_FOLDER,
          envelopeId: env.id,
          messageId: env.id,
          error: error instanceof Error ? error.message : String(error),
          timestamp: new Date().toISOString(),
        });
      }

      if (i < envelopes.length - 1 && cfg.MAIL_INTER_MESSAGE_DELAY_MS > 0) {
        const jitterMax = Math.max(0, cfg.MAIL_INTER_MESSAGE_JITTER_MS);
        const jitter = jitterMax > 0 ? Math.floor(Math.random() * (jitterMax * 2 + 1)) - jitterMax : 0;
        const delayMs = Math.max(0, cfg.MAIL_INTER_MESSAGE_DELAY_MS + jitter);
        if (delayMs > 0) {
          await sleep(delayMs);
        }
      }
    }

    console.log(JSON.stringify({ ok: true, runId, mode, summary }, null, 2));
    runStatus = "ok";
  } catch (error) {
    fatalError = error instanceof Error ? error.message : String(error);
    throw error;
  } finally {
    saveCursorState(cfg.MAIL_CURSOR_FILE, cursor);
    appendJsonl(cfg.MAIL_PROCESSOR_STATE_FILE, {
      type: "run_finished",
      runId,
      mode,
      status: runStatus,
      error: fatalError,
      finishedAt: new Date().toISOString(),
      summary,
      cursor: cursor.backfill,
    });
    lock.release();
  }
}

main().catch((err) => {
  console.error(`[mail-processor] ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
