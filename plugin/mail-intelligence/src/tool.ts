import type { ClassificationCandidate, ClassificationInput, ClassificationResult, ClassificationWorkpackageCandidate } from "./contracts.js";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/plugin-entry";

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

function clampConfidence(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0;
  if (numeric < 0) return 0;
  if (numeric > 1) return 1;
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

function renderPrompt(input: ClassificationInput): string {
  return JSON.stringify({
    task: "mail_classification",
    prompt_version: PROMPT_VERSION,
    allowed_evidence: [...ALLOWED_EVIDENCE],
    allowed_warnings: [...ALLOWED_WARNINGS],
    input,
  });
}

export async function classifyMailWithModel(params: {
  api: OpenClawPluginApi;
  input: ClassificationInput;
  defaultModel?: string;
}): Promise<ClassificationResult> {
  const model = params.defaultModel || "academicai/gpt-5";
  const auth = await params.api.runtime.modelAuth.getApiKeyForModel({ model, cfg: params.api.config });
  const providerBaseUrl = params.api.config.models?.providers?.academicai?.baseUrl
    || params.api.config.models?.providers?.openai?.baseUrl
    || "http://127.0.0.1:11435/v1";

  const response = await fetch(`${String(providerBaseUrl).replace(/\/$/, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${auth}`,
    },
    body: JSON.stringify({
      model,
      temperature: 0,
      response_format: { type: "json_object" },
      messages: [
        { role: "user", content: `${SYSTEM_PROMPT}\n\n${renderPrompt(params.input)}` },
      ],
    }),
  });

  if (!response.ok) {
    throw new Error(`mail_intelligence.classify model request failed (${response.status})`);
  }

  const data = await response.json() as any;
  const content = data?.choices?.[0]?.message?.content;
  if (typeof content !== "string" || !content.trim()) {
    throw new Error("mail_intelligence.classify model response missing content");
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
}
