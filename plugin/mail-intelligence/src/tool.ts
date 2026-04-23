import fs from "node:fs/promises";
import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import type { ClassificationCandidate, ClassificationInput, ClassificationResult, ClassificationWorkpackageCandidate } from "./contracts.js";
import type { OpenClawPluginApi } from "../types/openclaw-plugin-api.js";

type RawCandidate = {
  id?: unknown;
  confidence?: unknown;
  evidence?: unknown;
  project_id?: unknown;
};

type RawExtraction = {
  projectCandidates?: unknown;
  topicCandidates?: unknown;
  workpackageCandidates?: unknown;
  needsReply?: unknown;
  warnings?: unknown;
};

const PROMPT_VERSION = "mail-intelligence-classify-v1";
const FALLBACK_MODEL = "openai-codex/gpt-5.4-mini";

const SYSTEM_PROMPT = [
  "You classify one email for deterministic routing.",
  "Return STRICT JSON only.",
  "Select only ids from the provided candidate space.",
  "Never invent projects, topics, workpackages, labels, or warnings.",
  "Output schema:",
  '{"projectCandidates":[{"id":"string","confidence":0,"evidence":["subject_match"]}],"topicCandidates":[{"id":"string","confidence":0,"evidence":["keyword_match"]}],"workpackageCandidates":[{"id":"string","project_id":"string","confidence":0,"evidence":["workpackage_reference"]}],"needsReply":false,"warnings":["weak_project_signal"]}',
  "If unsure, return low confidence or empty arrays.",
  "Evidence values must come from the controlled vocabulary in the input.",
].join("\n");

const ALLOWED_EVIDENCE = new Set([
  "subject_match",
  "sender_domain",
  "sender_contact",
  "reply_chain",
  "current_message",
  "thread_context",
  "keyword_match",
  "alias_match",
  "workpackage_reference",
  "task_reference",
  "milestone_reference",
]);

const ALLOWED_WARNINGS = new Set([
  "ambiguous_project_overlap",
  "weak_project_signal",
  "topic_stronger_than_project",
  "workpackage_without_project",
  "thread_context_used",
  "insufficient_current_message_signal",
  "catalog_gap_suspected",
]);

const DEBUG_LOG_PATH = path.join(os.tmpdir(), "mail-intelligence-debug.log");

async function appendDebugLog(payload: unknown): Promise<void> {
  const line = `${JSON.stringify({ ts: new Date().toISOString(), ...((payload && typeof payload === "object") ? payload as Record<string, unknown> : { payload }) })}\n`;
  await fs.appendFile(DEBUG_LOG_PATH, line, "utf8").catch(() => undefined);
}

function clampConfidence(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  if (numeric < 0) return 0;
  if (numeric > 100) return 100;
  return numeric;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function safeJsonParse(text: string): RawExtraction {
  const trimmed = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/i, "");
  return JSON.parse(trimmed) as RawExtraction;
}

function collectText(payloads: Array<{ isError?: boolean; text?: string }> | undefined): string {
  return (payloads ?? [])
    .filter((payload) => !payload?.isError && typeof payload?.text === "string")
    .map((payload) => payload.text ?? "")
    .join("\n")
    .trim();
}

function normalizeCandidates(raw: unknown, allowedIds: Set<string>, maxCount: number): ClassificationCandidate[] {
  if (!Array.isArray(raw)) return [];
  const out: ClassificationCandidate[] = [];
  for (const entry of raw as RawCandidate[]) {
    if (out.length >= maxCount) break;
    const id = typeof entry?.id === "string" ? entry.id : undefined;
    if (!id || !allowedIds.has(id)) continue;
    out.push({
      id,
      confidence: clampConfidence(entry.confidence),
      evidence: asStringArray(entry.evidence).filter((item) => ALLOWED_EVIDENCE.has(item)) as ClassificationCandidate["evidence"],
    });
  }
  out.sort((a, b) => b.confidence - a.confidence);
  return out;
}

function normalizeWorkpackages(raw: unknown, allowedWorkpackages: Map<string, string>, maxCount: number): ClassificationWorkpackageCandidate[] {
  if (!Array.isArray(raw)) return [];
  const out: ClassificationWorkpackageCandidate[] = [];
  for (const entry of raw as RawCandidate[]) {
    if (out.length >= maxCount) break;
    const id = typeof entry?.id === "string" ? entry.id : undefined;
    const projectId = typeof entry?.project_id === "string" ? entry.project_id : undefined;
    if (!id || !projectId) continue;
    if (allowedWorkpackages.get(id) !== projectId) continue;
    out.push({
      id,
      project_id: projectId,
      confidence: clampConfidence(entry.confidence),
      evidence: asStringArray(entry.evidence).filter((item) => ALLOWED_EVIDENCE.has(item)) as ClassificationWorkpackageCandidate["evidence"],
    });
  }
  out.sort((a, b) => b.confidence - a.confidence);
  return out;
}

function sortStrings(values: string[] | undefined): string[] | undefined {
  if (!Array.isArray(values) || values.length === 0) return undefined;
  return [...values].sort((a, b) => a.localeCompare(b));
}

function canonicalizeObject(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((entry) => canonicalizeObject(entry));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, entry]) => entry !== undefined)
    .sort(([left], [right]) => left.localeCompare(right));
  return Object.fromEntries(entries.map(([key, entry]) => [key, canonicalizeObject(entry)]));
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalizeObject(value));
}

function buildCanonicalCatalogHints(input: ClassificationInput): ClassificationInput["catalog_hints"] {
  const projects = [...input.catalog_hints.projects]
    .map((project) => ({
      id: project.id,
      title: project.title,
      aliases: sortStrings(project.aliases),
      keywords: sortStrings(project.keywords),
      domains: sortStrings(project.domains),
      contacts: project.contacts?.length
        ? [...project.contacts]
            .map((contact) => ({
              email: contact.email,
              name: contact.name,
              role: contact.role,
            }))
            .sort((left, right) => `${left.email}\u0000${left.name ?? ""}\u0000${left.role ?? ""}`.localeCompare(`${right.email}\u0000${right.name ?? ""}\u0000${right.role ?? ""}`))
        : undefined,
      workpackages: project.workpackages?.length
        ? [...project.workpackages]
            .map((workpackage) => ({
              id: workpackage.id,
              title: workpackage.title,
              aliases: sortStrings(workpackage.aliases),
              keywords: sortStrings(workpackage.keywords),
              hint_rank: workpackage.hint_rank,
            }))
            .sort((left, right) => left.id.localeCompare(right.id))
        : undefined,
      hint_rank: project.hint_rank,
    }))
    .sort((left, right) => left.id.localeCompare(right.id));

  const topics = [...input.catalog_hints.topics]
    .map((topic) => ({
      id: topic.id,
      title: topic.title,
      aliases: sortStrings(topic.aliases),
      keywords: sortStrings(topic.keywords),
      domains: sortStrings(topic.domains),
      contacts: topic.contacts?.length
        ? [...topic.contacts]
            .map((contact) => ({
              email: contact.email,
              name: contact.name,
              role: contact.role,
            }))
            .sort((left, right) => `${left.email}\u0000${left.name ?? ""}\u0000${left.role ?? ""}`.localeCompare(`${right.email}\u0000${right.name ?? ""}\u0000${right.role ?? ""}`))
        : undefined,
      description: topic.description,
      typical_subject_patterns: sortStrings(topic.typical_subject_patterns),
      subtopics: topic.subtopics?.length
        ? [...topic.subtopics]
            .map((subtopic) => ({
              id: subtopic.id,
              title: subtopic.title,
              aliases: sortStrings(subtopic.aliases),
              keywords: sortStrings(subtopic.keywords),
            }))
            .sort((left, right) => left.id.localeCompare(right.id))
        : undefined,
      hint_rank: topic.hint_rank,
    }))
    .sort((left, right) => left.id.localeCompare(right.id));

  return { projects, topics };
}

function renderPromptParts(input: ClassificationInput): { cachePrefix: string; mailTail: string; cacheFingerprint: string } {
  const stableCatalogHints = buildCanonicalCatalogHints(input);
  const stableOptions = {
    include_needs_reply: input.options.include_needs_reply,
    max_project_candidates: input.options.max_project_candidates,
    max_topic_candidates: input.options.max_topic_candidates,
    max_workpackage_candidates: input.options.max_workpackage_candidates,
  };

  const cachePrefixPayload = {
    task: "mail_classification",
    prompt_version: PROMPT_VERSION,
    allowed_evidence: [...ALLOWED_EVIDENCE].sort((a, b) => a.localeCompare(b)),
    allowed_warnings: [...ALLOWED_WARNINGS].sort((a, b) => a.localeCompare(b)),
    catalog_hints: stableCatalogHints,
    options: stableOptions,
  };
  const cachePrefixJson = canonicalJson(cachePrefixPayload);
  const cacheFingerprint = crypto.createHash("sha256").update(cachePrefixJson).digest("hex").slice(0, 16);
  const mailTailJson = canonicalJson({
    schema_version: input.schema_version,
    mail: input.mail,
  });

  return {
    cachePrefix: [
      "Stable cache prefix:",
      cachePrefixJson,
      "",
      "Classify the next mail using the stable catalog context above.",
    ].join("\n"),
    mailTail: [
      "Mail tail:",
      mailTailJson,
    ].join("\n"),
    cacheFingerprint,
  };
}

function normalizeModelRef(model: string | { primary?: string } | null | undefined): string | undefined {
  if (typeof model === "string" && model.trim()) return model.trim();
  if (model && typeof model === "object" && typeof model.primary === "string" && model.primary.trim()) {
    return model.primary.trim();
  }
  return undefined;
}

function resolveProviderRequestModel(model: string): string {
  if (!model.includes("/")) return model;
  const [, providerModel] = model.split(/\/(.+)/, 2);
  return providerModel || model;
}

function resolveAgentPrimaryModel(api: OpenClawPluginApi): string | undefined {
  const configuredDefault = normalizeModelRef(api.pluginConfig?.defaultModel);
  if (configuredDefault) return configuredDefault;

  const agentId = api.toolContext?.agentId;
  const resolvedIdentity = api.runtime.agent?.resolveAgentIdentity?.(api.config, agentId);
  const resolvedIdentityModel = normalizeModelRef(resolvedIdentity?.model);
  if (resolvedIdentityModel) return resolvedIdentityModel;

  const listedAgentModel = normalizeModelRef(
    api.config?.agents?.list?.find((entry) => entry.id === agentId)?.model,
  );
  if (listedAgentModel) return listedAgentModel;

  const defaultsModel = normalizeModelRef(api.config?.agents?.defaults?.model);
  if (defaultsModel) return defaultsModel;

  return undefined;
}

export async function classifyMailWithModel(params: {
  api: OpenClawPluginApi;
  input: ClassificationInput;
  defaultModel?: string;
}): Promise<ClassificationResult> {
  try {
    const model = params.defaultModel || resolveAgentPrimaryModel(params.api) || FALLBACK_MODEL;
    if (!model) {
      throw new Error("mail-classify could not resolve a default model for the calling agent");
    }

    const providerId = model.includes("/") ? model.split("/")[0] : "";
    const providerModel = resolveProviderRequestModel(model);
    if (!providerId || !providerModel) {
      throw new Error(`mail-classify could not split provider/model from ${model}`);
    }

    const workspaceDir = params.api.runtime.agent?.resolveAgentWorkspaceDir?.(params.api.config, params.api.toolContext?.agentId)
      || params.api.config?.agents?.defaults?.workspace
      || process.cwd();
    const runEmbeddedPiAgent = params.api.runtime.agent?.runEmbeddedPiAgent;
    await appendDebugLog({
      event: "start",
      model,
      providerId,
      providerModel,
      workspaceDir,
      agentId: params.api.toolContext?.agentId ?? null,
      sessionKey: params.api.toolContext?.sessionKey ?? null,
      hasRunEmbeddedPiAgent: Boolean(runEmbeddedPiAgent),
      runtimeAgentKeys: params.api.runtime.agent ? Object.keys(params.api.runtime.agent) : [],
    });
    if (!runEmbeddedPiAgent) {
      throw new Error("mail-classify runtime is missing runEmbeddedPiAgent");
    }

    let tempDir: string | null = null;
    let content = "";
    try {
      tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "mail-intelligence-"));
      const promptParts = renderPromptParts(params.input);
      const sessionId = `mail-intel-cache-${promptParts.cacheFingerprint}`;
      const sessionKey = `mail-intelligence:${params.api.toolContext?.agentId ?? "default"}:${promptParts.cacheFingerprint}`;
      const sessionFile = path.join(tempDir, "session.json");
      const result = await runEmbeddedPiAgent({
        sessionId,
        sessionKey,
        sessionFile,
        workspaceDir,
        config: params.api.config,
        prompt: `${SYSTEM_PROMPT}\n\n${promptParts.cachePrefix}\n\n${promptParts.mailTail}`,
        timeoutMs: 120000,
        runId: `mail-intelligence-${Date.now()}`,
        provider: providerId,
        model: providerModel,
        authProfileIdSource: "auto",
        disableTools: true,
      });
      content = collectText((result as any)?.payloads);
      await appendDebugLog({
        event: "after_run",
        cacheFingerprint: promptParts.cacheFingerprint,
        embeddedSessionId: sessionId,
        embeddedSessionKey: sessionKey,
        contentPreview: content.slice(0, 300),
        payloadCount: Array.isArray((result as any)?.payloads) ? (result as any).payloads.length : null,
        payloads: Array.isArray((result as any)?.payloads)
          ? (result as any).payloads.map((payload: any) => ({
              text: typeof payload?.text === "string" ? payload.text.slice(0, 300) : payload?.text,
              isError: payload?.isError ?? false,
              isReasoning: payload?.isReasoning ?? false,
              mediaUrl: payload?.mediaUrl ?? null,
              mediaUrls: Array.isArray(payload?.mediaUrls) ? payload.mediaUrls : null,
            }))
          : null,
        meta: (result as any)?.meta ?? null,
      });
    } finally {
      if (tempDir) {
        await fs.rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
      }
    }

    if (!content.trim()) {
      throw new Error("mail-classify model response missing content");
    }

    const parsed = safeJsonParse(content);
    const allowedProjects = new Set(params.input.catalog_hints.projects.map((item) => item.id));
    const allowedTopics = new Set(params.input.catalog_hints.topics.map((item) => item.id));
    const allowedWorkpackages = new Map<string, string>();
    for (const project of params.input.catalog_hints.projects) {
      for (const workpackage of project.workpackages ?? []) {
        allowedWorkpackages.set(workpackage.id, project.id);
      }
    }

    return {
      schema_version: 1,
      projectCandidates: normalizeCandidates(parsed.projectCandidates, allowedProjects, params.input.options.max_project_candidates),
      topicCandidates: normalizeCandidates(parsed.topicCandidates, allowedTopics, params.input.options.max_topic_candidates),
      workpackageCandidates: normalizeWorkpackages(parsed.workpackageCandidates, allowedWorkpackages, params.input.options.max_workpackage_candidates),
      needsReply: Boolean(parsed.needsReply),
      warnings: asStringArray(parsed.warnings).filter((item) => ALLOWED_WARNINGS.has(item)) as ClassificationResult["warnings"],
    };
  } catch (error) {
    await appendDebugLog({
      event: "error",
      error: error instanceof Error ? error.message : String(error),
      stack: error instanceof Error ? error.stack : undefined,
    });
    throw error;
  }
}
