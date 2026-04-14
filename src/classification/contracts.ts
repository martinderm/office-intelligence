import type { MailMeta } from "../preprocess.js";
import type { Project, Topic } from "../types.js";

export const CLASSIFICATION_SCHEMA_VERSION = 1;

export type ClassificationEvidence =
  | "subject_match"
  | "sender_domain"
  | "sender_contact"
  | "reply_chain"
  | "current_message"
  | "thread_context"
  | "keyword_match"
  | "alias_match"
  | "workpackage_reference"
  | "task_reference"
  | "milestone_reference";

export type ClassificationWarning =
  | "ambiguous_project_overlap"
  | "weak_project_signal"
  | "topic_stronger_than_project"
  | "workpackage_without_project"
  | "thread_context_used"
  | "insufficient_current_message_signal"
  | "catalog_gap_suspected";

export type ThreadContextRelation = "ancestor";

export type ArtifactThreadContextEntry = {
  source: "artifact";
  message_id: string;
  date: string;
  from: string;
  subject: string;
  relation: ThreadContextRelation;
  current_message: string | null;
  older_context: string | null;
  effective_text: string | null;
};

export type RawReferenceThreadContextEntry = {
  source: "raw_reference";
  message_id: string | null;
  date: string | null;
  from: string | null;
  subject: string | null;
  relation: ThreadContextRelation;
  raw_text: string;
};

export type ThreadContextEntry = ArtifactThreadContextEntry | RawReferenceThreadContextEntry;

export type ClassificationContactHint = {
  email: string;
  name?: string;
  role?: string;
};

export type ClassificationWorkpackageHint = {
  id: string;
  title: string;
  aliases?: string[];
  keywords?: string[];
  hint_rank?: number;
};

export type ClassificationProjectHint = {
  id: string;
  title: string;
  aliases?: string[];
  keywords?: string[];
  domains?: string[];
  contacts?: ClassificationContactHint[];
  workpackages?: ClassificationWorkpackageHint[];
  hint_rank?: number;
};

export type ClassificationTopicHint = {
  id: string;
  title: string;
  aliases?: string[];
  keywords?: string[];
  domains?: string[];
  contacts?: ClassificationContactHint[];
  hint_rank?: number;
};

export type ClassificationMailHeaders = {
  reply_to: string | null;
  return_path: string | null;
  list_id: string | null;
  in_reply_to: string | null;
  references: string[];
};

export type ClassificationInput = {
  schema_version: 1;
  mail: {
    message_id: string;
    subject: string;
    from: string;
    date: string;
    current_message: string;
    sanitized_text: string;
    headers: ClassificationMailHeaders;
    thread_context?: ThreadContextEntry[];
  };
  catalog_hints: {
    projects: ClassificationProjectHint[];
    topics: ClassificationTopicHint[];
  };
  options: {
    include_needs_reply: boolean;
    max_project_candidates: number;
    max_topic_candidates: number;
    max_workpackage_candidates: number;
  };
};

export type ClassificationCandidate = {
  id: string;
  confidence: number;
  evidence: ClassificationEvidence[];
};

export type ClassificationWorkpackageCandidate = ClassificationCandidate & {
  project_id: string;
};

export type ClassificationResult = {
  schema_version: 1;
  projectCandidates: ClassificationCandidate[];
  topicCandidates: ClassificationCandidate[];
  workpackageCandidates: ClassificationWorkpackageCandidate[];
  needsReply: boolean;
  warnings?: ClassificationWarning[];
};

export type RoutingDecisionState =
  | "route"
  | "shadow_only"
  | "review"
  | "keep_in_inbox"
  | "classification_failed";

export type ClassifierBackendKind = "heuristic" | "openclaw_tool" | "legacy_llm";

export type MailArtifactThreadInfo = {
  messageIdNormalized: string;
  inReplyToNormalized: string | null;
  referencesNormalized: string[];
};

export type MailArtifactContextInfo = {
  currentMessageText: string | null;
  olderContextText: string | null;
  effectiveText: string;
  previewText: string | null;
};

export type MailArtifactExtensions = {
  thread?: MailArtifactThreadInfo;
  context?: MailArtifactContextInfo;
};

export type MailArtifactRecord = MailArtifactExtensions & {
  id: string;
  stableId: string;
  mailMeta: MailMeta & {
    referencesNormalized?: string[];
  };
  preview?: string | null;
};

export function toClassificationProjectHint(project: Project, hint_rank?: number): ClassificationProjectHint {
  return {
    id: project.id,
    title: project.title,
    aliases: project.aliases,
    keywords: project.keywords,
    domains: project.domains,
    contacts: project.contacts?.flatMap((contact) =>
      contact.email
        ? [{ email: contact.email, name: contact.name }]
        : [],
    ),
    workpackages: project.workpackages?.map((workpackage) => ({
      id: workpackage.id,
      title: workpackage.title,
      aliases: workpackage.aliases,
      keywords: workpackage.keywords,
    })),
    hint_rank,
  };
}

export function toClassificationTopicHint(topic: Topic, hint_rank?: number): ClassificationTopicHint {
  return {
    id: topic.id,
    title: topic.title,
    aliases: topic.aliases,
    keywords: topic.keywords,
    domains: topic.domains,
    contacts: topic.contacts?.flatMap((contact) =>
      contact.email
        ? [{ email: contact.email, name: contact.name, role: contact.role }]
        : [],
    ),
    hint_rank,
  };
}
