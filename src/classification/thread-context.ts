import fs from "node:fs";
import path from "node:path";
import type { MailMeta } from "../preprocess.js";
import type { MailArtifactContextInfo, MailArtifactThreadInfo } from "../types.js";
import type { ThreadContextEntry } from "./contracts.js";

type ArtifactMailMeta = MailMeta & {
  referencesNormalized?: string[];
};

type MailArtifactRecord = {
  id: string;
  stableId: string;
  mailMeta?: ArtifactMailMeta;
  thread?: MailArtifactThreadInfo;
  context?: MailArtifactContextInfo;
  preview?: string | null;
};

function normalizeMessageId(value?: string | null): string | undefined {
  if (!value) return undefined;
  const v = value.trim().toLowerCase().replace(/^<+|>+$/g, "");
  return v || undefined;
}

function normalizeMessageIdList(value?: string | string[] | null): string[] {
  if (!value) return [];
  if (Array.isArray(value)) {
    return value
      .map((item) => normalizeMessageId(item))
      .filter((item): item is string => Boolean(item));
  }

  const matches = value.match(/<[^>]+>/g);
  if (matches?.length) {
    return matches
      .map((item) => normalizeMessageId(item))
      .filter((item): item is string => Boolean(item));
  }

  return value
    .split(/\s+/)
    .map((item) => normalizeMessageId(item))
    .filter((item): item is string => Boolean(item));
}

function readArtifact(filePath: string): MailArtifactRecord | undefined {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    return JSON.parse(raw) as MailArtifactRecord;
  } catch {
    return undefined;
  }
}

function collectArtifactFiles(rootDir: string): string[] {
  const out: string[] = [];
  const stack = [path.resolve(rootDir)];

  while (stack.length) {
    const dir = stack.pop();
    if (!dir || !fs.existsSync(dir)) continue;

    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
      } else if (entry.isFile() && entry.name.endsWith(".json")) {
        out.push(full);
      }
    }
  }

  return out;
}

function artifactMessageId(record: MailArtifactRecord): string | undefined {
  return (
    record.thread?.messageIdNormalized ||
    normalizeMessageId(record.mailMeta?.messageId) ||
    normalizeMessageId(record.stableId)
  );
}

function artifactToThreadContext(record: MailArtifactRecord): ThreadContextEntry | undefined {
  const messageId = artifactMessageId(record);
  if (!messageId) return undefined;

  const context = record.context;
  const preview = typeof record.preview === "string" ? record.preview : null;

  return {
    source: "artifact",
    message_id: messageId,
    date: record.mailMeta?.date || "",
    from: record.mailMeta?.from || "",
    subject: record.mailMeta?.subject || "",
    relation: "ancestor",
    current_message: context?.currentMessageText ?? preview,
    older_context: context?.olderContextText ?? null,
    effective_text: context?.effectiveText ?? preview,
  };
}

export function findMessageArtifactByNormalizedId(msgsDir: string, normalizedId: string): string | undefined {
  const wanted = normalizeMessageId(normalizedId);
  if (!wanted) return undefined;

  for (const filePath of collectArtifactFiles(msgsDir)) {
    const record = readArtifact(filePath);
    if (!record) continue;

    if (artifactMessageId(record) === wanted) {
      return filePath;
    }
  }

  return undefined;
}

export function buildThreadContextFromMailArtifact(params: {
  msgsDir: string;
  meta: Pick<MailMeta, "inReplyTo" | "references">;
  maxEntries?: number;
}): ThreadContextEntry[] {
  const referenceIds = [
    normalizeMessageId(params.meta.inReplyTo),
    ...normalizeMessageIdList(params.meta.references),
  ].filter((item, index, arr): item is string => Boolean(item) && arr.indexOf(item) === index);

  const maxEntries = Math.max(1, params.maxEntries ?? 3);
  const entries: ThreadContextEntry[] = [];

  for (const referenceId of referenceIds) {
    if (entries.length >= maxEntries) break;

    const artifactPath = findMessageArtifactByNormalizedId(params.msgsDir, referenceId);
    if (!artifactPath) continue;

    const record = readArtifact(artifactPath);
    if (!record) continue;

    const entry = artifactToThreadContext(record);
    if (!entry) continue;

    entries.push(entry);
  }

  return entries;
}
