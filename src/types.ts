export type Project = {
  id: string;
  title: string;
  mailbox_folder: string;
  reference_md?: string;
  aliases?: string[];
  keywords?: string[];
  domains?: string[];
  contacts?: Array<{ name?: string; email?: string }>;
  description?: string;
  typical_subject_patterns?: string[];
  routing_priority?: number;
  do_not_route_if?: string[];
  updated_at?: string;
  schema_version?: number;
};

export type EnvConfig = {
  MAIL_PROCESSOR_DATA_DIR: string;
  MAIL_PROCESSOR_STATE_FILE: string;
  MAIL_PROCESSOR_MSGS_DIR: string;
  MAIL_PROCESSOR_SUGGESTIONS_FILE: string;
  MAIL_PROCESSOR_LOCK_FILE: string;
  MAIL_PROCESSOR_LOCK_TTL_SECONDS: number;
  PROJECTS_JSON_PATH: string;
  MAIL_ROUTING_ENABLED: boolean;
  LOG_LEVEL: string;
};
