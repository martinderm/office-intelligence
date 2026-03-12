#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { loadDotEnv, getConfig } from "./env.js";
import { loadProjects } from "./projects.js";
import { acquireLock } from "./lock.js";
import { appendJsonl, ensureRuntimeDirs } from "./state.js";
import { copyMessage, copyMessageWithUidPlus, listEnvelopes, moveMessage, readMessage } from "./mail-source.js";
import { getProcessedIds } from "./idempotency.js";
import { matchProject, mergeHeuristicAndLlm, needsReplyHeuristic } from "./matcher.js";
import { cleanupDebugMessages } from "./retention.js";
import { prepareMailText } from "./preprocess.js";
import { extractWithLlm } from "./llm.js";
import { loadOrFetchCapabilities } from "./capabilities.js";
import { parseDiscoverOptions, runDiscoverProjects } from "./discover-projects.js";

function parseMode(args: string[]): "shadow" | "run" {
  const modeArg = args.find((a) => a.startsWith("--mode="));
  if (!modeArg) return "shadow";
  const value = modeArg.split("=")[1];
  if (value !== "shadow" && value !== "run") {
    throw new Error("--mode must be shadow or run");
  }
  return value;
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

function moveExportArtifact(dataDir: string, envelopeId: string, stableId: string, folder: string): string {
  const exportsBaseDir = path.resolve(dataDir, "exports");
  const sourcePath = path.join(exportsBaseDir, `${envelopeId}.eml`);
  const targetDir = path.join(exportsBaseDir, folderToSlug(folder));
  const targetPath = path.join(targetDir, `${stableId}.eml`);

  fs.mkdirSync(targetDir, { recursive: true });

  if (fs.existsSync(sourcePath)) {
    if (fs.existsSync(targetPath)) {
      fs.unlinkSync(sourcePath);
    } else {
      fs.renameSync(sourcePath, targetPath);
    }
  }

  return targetPath;
}

function findMessageArtifactByStableId(msgsDir: string, stableId: string): string | undefined {
  const wanted = `${stableId}.json`;
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
      if (entry.isFile() && entry.name === wanted) {
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
    runDiscoverProjects(cwd, cfg, discover);
    return;
  }

  ensureRuntimeDirs([
    cfg.MAIL_PROCESSOR_DATA_DIR,
    cfg.MAIL_PROCESSOR_MSGS_DIR,
    cfg.MAIL_PROCESSOR_CAPABILITIES_DIR,
    path.dirname(cfg.MAIL_PROCESSOR_STATE_FILE),
  ]);

  const lock = acquireLock(cfg.MAIL_PROCESSOR_LOCK_FILE, cfg.MAIL_PROCESSOR_LOCK_TTL_SECONDS);
  const runId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const startedAt = new Date().toISOString();

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

    const summary = {
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
    const envelopes =
      cfg.HIMALAYA_COMMAND === "mock"
        ? [{ id: "mock-1", rawLine: "mock-1 Example subject" }]
        : listEnvelopes(cfg.HIMALAYA_COMMAND, cfg.MAIL_SOURCE_FOLDER, cfg.MAIL_FETCH_LIMIT);

    for (const env of envelopes) {
      summary.inspected += 1;
      try {
        const msg =
          cfg.HIMALAYA_COMMAND === "mock"
            ? {
                id: env.id,
                raw: "Subject: [EXAMPLE] Bitte um Rückmeldung\nFrom: contact@example.org\nBody: Kannst du bis morgen antworten?",
              }
            : readMessage(cfg.HIMALAYA_COMMAND, cfg.MAIL_SOURCE_FOLDER, env.id, cfg.MAIL_PROCESSOR_DATA_DIR);
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
        const debugPath = path.join(msgFolderDir, `${stableId}.json`);

        const localExportPath = moveExportArtifact(cfg.MAIL_PROCESSOR_DATA_DIR, env.id, stableId, finalFolder);

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
    }

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
