import type { Project, Topic } from "../types.js";
import { matchProject } from "../matcher.js";
import type { MailClassifier, ClassifierRequest, ClassifierResponse } from "./classifier.js";
import type { ClassificationResult, ThreadContextEntry } from "./contracts.js";

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

function buildCombinedMailText(input: ClassifierRequest): string {
  return [input.mail.sanitized_text, renderThreadContext(input.mail.thread_context)]
    .filter(Boolean)
    .join("\n\n");
}

function toClassificationResult(match: ReturnType<typeof matchProject>): ClassificationResult {
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
    needsReply: false,
  };
}

export class HeuristicClassifier implements MailClassifier {
  readonly kind = "heuristic" as const;

  constructor(
    private readonly params: {
      projects: Project[];
      topics: Topic[];
    },
  ) {}

  async classify(input: ClassifierRequest): Promise<ClassifierResponse> {
    const combinedMailText = buildCombinedMailText(input);
    const heuristicMatch = matchProject(combinedMailText, this.params.projects, this.params.topics);

    return {
      ok: true,
      backend: this.kind,
      result: toClassificationResult(heuristicMatch),
    };
  }
}
