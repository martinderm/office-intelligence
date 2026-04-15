import type { MailClassifier, ClassifierRequest, ClassifierResponse } from "./classifier.js";
import type {
  ClassificationCandidate,
  ClassificationEvidence,
  ClassificationInput,
  ClassificationResult,
  ClassificationWarning,
  ClassificationWorkpackageCandidate,
} from "./contracts.js";

type OpenClawToolClassifierParams = {
  baseUrl: string;
  apiKey: string;
  model: string;
  timeoutMs?: number;
  toolName?: string;
};

type RawToolCandidate = {
  id?: unknown;
  confidence?: unknown;
  evidence?: unknown;
  project_id?: unknown;
};

type RawToolResult = {
  schema_version?: unknown;
  projectCandidates?: unknown;
  topicCandidates?: unknown;
  workpackageCandidates?: unknown;
  needsReply?: unknown;
  warnings?: unknown;
};

const DEFAULT_TOOL_NAME = "mail_intelligence.classify";

const ALLOWED_EVIDENCE = new Set<ClassificationEvidence>([
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

const ALLOWED_WARNINGS = new Set<ClassificationWarning>([
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

function allowedProjectIds(input: ClassificationInput): Set<string> {
  return new Set(input.catalog_hints.projects.map((project) => project.id));
}

function allowedTopicIds(input: ClassificationInput): Set<string> {
  return new Set(input.catalog_hints.topics.map((topic) => topic.id));
}

function allowedWorkpackageIds(input: ClassificationInput): Map<string, string> {
  const map = new Map<string, string>();
  for (const project of input.catalog_hints.projects) {
    for (const workpackage of project.workpackages ?? []) {
      map.set(workpackage.id, project.id);
    }
  }
  return map;
}

function normalizeEvidence(value: unknown): ClassificationEvidence[] {
  return asStringArray(value).filter((item): item is ClassificationEvidence => ALLOWED_EVIDENCE.has(item as ClassificationEvidence));
}

function normalizeWarnings(value: unknown): ClassificationWarning[] | undefined {
  const warnings = asStringArray(value).filter((item): item is ClassificationWarning => ALLOWED_WARNINGS.has(item as ClassificationWarning));
  return warnings.length ? warnings : undefined;
}

function normalizeCandidateList(params: {
  raw: unknown;
  allowedIds: Set<string>;
  maxCandidates: number;
}): ClassificationCandidate[] {
  if (!Array.isArray(params.raw)) return [];

  const normalized: ClassificationCandidate[] = [];
  for (const entry of params.raw as RawToolCandidate[]) {
    if (normalized.length >= params.maxCandidates) break;
    if (!entry || typeof entry !== "object") continue;
    const id = typeof entry.id === "string" ? entry.id : undefined;
    if (!id || !params.allowedIds.has(id)) continue;
    normalized.push({
      id,
      confidence: clampConfidence(entry.confidence),
      evidence: normalizeEvidence(entry.evidence),
    });
  }

  normalized.sort((a, b) => b.confidence - a.confidence);
  return normalized;
}

function normalizeWorkpackageList(params: {
  raw: unknown;
  allowedWorkpackages: Map<string, string>;
  maxCandidates: number;
}): ClassificationWorkpackageCandidate[] {
  if (!Array.isArray(params.raw)) return [];

  const normalized: ClassificationWorkpackageCandidate[] = [];
  for (const entry of params.raw as RawToolCandidate[]) {
    if (normalized.length >= params.maxCandidates) break;
    if (!entry || typeof entry !== "object") continue;
    const id = typeof entry.id === "string" ? entry.id : undefined;
    const projectId = typeof entry.project_id === "string" ? entry.project_id : undefined;
    const allowedProjectId = id ? params.allowedWorkpackages.get(id) : undefined;
    if (!id || !projectId || !allowedProjectId || allowedProjectId !== projectId) continue;
    normalized.push({
      id,
      project_id: projectId,
      confidence: clampConfidence(entry.confidence),
      evidence: normalizeEvidence(entry.evidence),
    });
  }

  normalized.sort((a, b) => b.confidence - a.confidence);
  return normalized;
}

function safeJsonParse(text: string): RawToolResult {
  const trimmed = text.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/i, "");
  return JSON.parse(trimmed) as RawToolResult;
}

async function callOpenAiCompatibleEndpoint(params: {
  endpoint: string;
  apiKey: string;
  model: string;
  toolName: string;
  input: ClassificationInput;
  timeoutMs: number;
}): Promise<string> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), params.timeoutMs);

  try {
    const response = await fetch(params.endpoint, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${params.apiKey}`,
      },
      body: JSON.stringify({
        model: params.model,
        temperature: 0,
        response_format: { type: "json_object" },
        messages: [
          {
            role: "user",
            content: JSON.stringify({
              tool: params.toolName,
              input: params.input,
            }),
          },
        ],
      }),
      signal: controller.signal,
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`OpenClaw tool backend request failed (${response.status}): ${text}`);
    }

    const data = (await response.json()) as any;
    const content = data?.choices?.[0]?.message?.content;
    if (typeof content !== "string" || !content.trim()) {
      throw new Error("OpenClaw tool backend response missing message content");
    }
    return content;
  } finally {
    clearTimeout(timeout);
  }
}

export class OpenClawToolClassifier implements MailClassifier {
  readonly kind = "openclaw_tool" as const;

  constructor(private readonly params: OpenClawToolClassifierParams) {}

  async classify(input: ClassifierRequest): Promise<ClassifierResponse> {
    const timeoutMs = this.params.timeoutMs ?? 60_000;
    const base = this.params.baseUrl.replace(/\/$/, "");
    const endpoints = [`${base}/chat/completions`, `${base}/v1/chat/completions`];
    const toolName = this.params.toolName ?? DEFAULT_TOOL_NAME;

    try {
      let rawText = "";
      let lastError: string | null = null;

      for (const endpoint of endpoints) {
        try {
          rawText = await callOpenAiCompatibleEndpoint({
            endpoint,
            apiKey: this.params.apiKey,
            model: this.params.model,
            toolName,
            input,
            timeoutMs,
          });
          lastError = null;
          break;
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          lastError = message;
          if (!message.includes("404")) {
            throw error;
          }
        }
      }

      if (!rawText) {
        throw new Error(lastError || "OpenClaw tool backend request failed: no endpoint succeeded");
      }

      const parsed = safeJsonParse(rawText);
      const projects = normalizeCandidateList({
        raw: parsed.projectCandidates,
        allowedIds: allowedProjectIds(input),
        maxCandidates: input.options.max_project_candidates,
      });
      const topics = normalizeCandidateList({
        raw: parsed.topicCandidates,
        allowedIds: allowedTopicIds(input),
        maxCandidates: input.options.max_topic_candidates,
      });
      const workpackages = normalizeWorkpackageList({
        raw: parsed.workpackageCandidates,
        allowedWorkpackages: allowedWorkpackageIds(input),
        maxCandidates: input.options.max_workpackage_candidates,
      });

      const result: ClassificationResult = {
        schema_version: 1,
        projectCandidates: projects,
        topicCandidates: topics,
        workpackageCandidates: workpackages,
        needsReply: Boolean(parsed.needsReply),
        warnings: normalizeWarnings(parsed.warnings),
      };

      return {
        ok: true,
        backend: this.kind,
        result,
      };
    } catch (error) {
      return {
        ok: false,
        backend: this.kind,
        error: error instanceof Error ? error.message : String(error),
        retryable: true,
      };
    }
  }
}
