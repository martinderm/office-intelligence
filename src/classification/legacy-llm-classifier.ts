import type { Project, Topic } from "../types.js";
import { extractWithLlm } from "../llm.js";
import { matchProject } from "../matcher.js";
import { mergeHeuristicAndLegacyLlm } from "./legacy-llm-merge.js";
import type { MailClassifier, ClassifierResponse, ClassifierRequest } from "./classifier.js";
import type {
  ClassificationInput,
  ClassificationProjectHint,
  ClassificationResult,
  ClassificationTopicHint,
  ThreadContextEntry,
} from "./contracts.js";

type LegacyLlmClassifierParams = {
  cwd: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  timeoutMs?: number;
  promptPath?: string;
  projects: Project[];
  topics: Topic[];
};

function renderThreadContext(entries?: ThreadContextEntry[]): string {
  if (!entries?.length) return "";
  return entries
    .map((entry) => {
      if (entry.source === "artifact") {
        return [entry.subject, entry.current_message, entry.older_context, entry.effective_text]
          .filter(Boolean)
          .join("\n");
      }
      return entry.raw_text;
    })
    .filter(Boolean)
    .join("\n\n");
}

function buildCombinedMailText(input: ClassificationInput): string {
  return [input.mail.sanitized_text, renderThreadContext(input.mail.thread_context)]
    .filter(Boolean)
    .join("\n\n");
}

function projectHintsToText(projects: ClassificationProjectHint[]): string {
  return projects
    .map((p) => `${p.id} | ${p.title}${p.aliases?.length ? ` | aliases: ${p.aliases.join(", ")}` : ""}${p.workpackages?.length ? ` | workpackages: ${p.workpackages.map((wp) => `${wp.id}:${wp.title}`).join(", ")}` : ""}${p.hint_rank ? ` | hint_rank: ${p.hint_rank}` : ""}`)
    .join("\n");
}

function topicHintsToText(topics: ClassificationTopicHint[]): string {
  return topics
    .map((t) => `${t.id} | ${t.title}${t.aliases?.length ? ` | aliases: ${t.aliases.join(", ")}` : ""}${t.hint_rank ? ` | hint_rank: ${t.hint_rank}` : ""}`)
    .join("\n");
}

function toClassificationResult(match: ReturnType<typeof matchProject>, needsReply: boolean): ClassificationResult {
  return {
    schema_version: 1,
    projectCandidates: match.projectId
      ? [{ id: match.projectId, confidence: match.score, evidence: ["current_message"] }]
      : [],
    topicCandidates: match.matchedTopicId
      ? [{ id: match.matchedTopicId, confidence: match.topicScore || 0, evidence: ["current_message"] }]
      : [],
    workpackageCandidates: match.matchedWorkpackageId && match.projectId
      ? [{ id: match.matchedWorkpackageId, project_id: match.projectId, confidence: match.workpackageScore || 0, evidence: ["current_message"] }]
      : [],
    needsReply,
  };
}

export class LegacyLlmClassifier implements MailClassifier {
  readonly kind = "legacy_llm" as const;

  constructor(private readonly params: LegacyLlmClassifierParams) {}

  async classify(input: ClassifierRequest): Promise<ClassifierResponse> {
    try {
      const combinedMailText = buildCombinedMailText(input);
      const heuristicMatch = matchProject(combinedMailText, this.params.projects, this.params.topics);

      const llm = await extractWithLlm({
        cwd: this.params.cwd,
        baseUrl: this.params.baseUrl,
        apiKey: this.params.apiKey,
        model: this.params.model,
        mailText: combinedMailText,
        projectHints: projectHintsToText(input.catalog_hints.projects),
        topicHints: topicHintsToText(input.catalog_hints.topics),
        promptPath: this.params.promptPath,
        timeoutMs: this.params.timeoutMs,
      });

      const merged = mergeHeuristicAndLegacyLlm(heuristicMatch, llm, this.params.projects, this.params.topics);
      const llmNeedsReplyScore = Number(llm?.needsReply?.score || 0);
      const needsReply = input.options.include_needs_reply ? llmNeedsReplyScore > 0 : false;

      return {
        ok: true,
        backend: this.kind,
        result: toClassificationResult(merged, needsReply),
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
