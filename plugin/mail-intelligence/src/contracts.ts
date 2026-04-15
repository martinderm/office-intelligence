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

export type ThreadContextEntry =
  | {
      source: "artifact";
      message_id: string;
      date: string;
      from: string;
      subject: string;
      relation: ThreadContextRelation;
      current_message: string | null;
      older_context: string | null;
      effective_text: string | null;
    }
  | {
      source: "raw_reference";
      message_id: string | null;
      date: string | null;
      from: string | null;
      subject: string | null;
      relation: ThreadContextRelation;
      raw_text: string;
    };

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

export type ClassificationInput = {
  schema_version: 1;
  mail: {
    message_id: string;
    subject: string;
    from: string;
    date: string;
    current_message: string;
    sanitized_text: string;
    headers: {
      reply_to: string | null;
      return_path: string | null;
      list_id: string | null;
      in_reply_to: string | null;
      references: string[];
    };
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
