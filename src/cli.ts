#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { loadDotEnv, getConfig } from "./env.js";
import { loadProjects } from "./projects.js";
import { acquireLock } from "./lock.js";
import { appendJsonl, ensureRuntimeDirs } from "./state.js";
import { copyMessage, listEnvelopes, readMessage } from "./mail-source.js";
import { getProcessedIds } from "./idempotency.js";
import { matchProject, mergeHeuristicAndLlm, needsReplyHeuristic } from "./matcher.js";
import { cleanupDebugMessages } from "./retention.js";
import { prepareMailText } from "./preprocess.js";
import { extractWithLlm } from "./llm.js";

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

        const debugPath = path.resolve(cfg.MAIL_PROCESSOR_MSGS_DIR, `${env.id}.json`);
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
              preview: prepared.effectiveText.slice(0, 2000),
            },
            null,
            2,
          ),
          "utf8",
        );

        const copyTargets: string[] = [];

        if (mode === "run" && match.projectId && match.score >= cfg.PROJECT_MATCH_THRESHOLD) {
          const project = projects.find((p) => p.id === match.projectId);
          if (project) {
            if (cfg.HIMALAYA_COMMAND !== "mock") {
              copyMessage(cfg.HIMALAYA_COMMAND, project.mailbox_folder, env.id);
            }
            copyTargets.push(project.mailbox_folder);
            summary.copied += 1;
            if (needsReply) {
              const replyFolder = "Projekte/_Needs-Reply";
              if (cfg.HIMALAYA_COMMAND !== "mock") {
                copyMessage(cfg.HIMALAYA_COMMAND, replyFolder, env.id);
              }
              copyTargets.push(replyFolder);
              summary.replyCopied += 1;
            }
          }
        }

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
          lastKnownEnvelopeId: env.id,
          lastKnownFolder: copyTargets.length ? copyTargets[copyTargets.length - 1] : cfg.MAIL_SOURCE_FOLDER,
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
