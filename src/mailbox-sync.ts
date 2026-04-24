import fs from "node:fs";
import path from "node:path";
import { listFolders, type MailFolder } from "./mail-source.js";
import type { EnvConfig, Project, Topic } from "./types.js";

export type MailboxFolderSnapshot = {
  schema_version: 1;
  mailbox: string;
  fetched_at: string;
  source: {
    command: string;
    account?: string;
  };
  folders: Array<{
    path: string;
    normalized_path: string;
    attributes: string[];
    raw_desc?: string;
    sync?: {
      last_synced_at: string;
      mode: string;
      fetch_limit: number;
      inspected: number;
      skipped: number;
      errors: number;
      msg_dir: string;
      export_dir: string;
    };
  }>;
};

export type PendingDecisionStatus = "open" | "notified" | "snoozed" | "resolved" | "ignored";
export type PendingDecisionKind = "missing_folder";
export type PendingDecisionEntityKind = "project" | "topic" | "special";

export type PendingDecisionItem = {
  id: string;
  status: PendingDecisionStatus;
  kind: PendingDecisionKind;
  entity_kind: PendingDecisionEntityKind;
  entity_id: string;
  expected_folder: string;
  detected_at: string;
  source: "mailbox-sync";
  summary: string;
  proposed_actions: string[];
  last_notified_at: string | null;
  resolved_at: string | null;
  resolution: string | null;
  meta?: Record<string, unknown>;
};

export type PendingDecisionQueue = {
  schema_version: 1;
  updated_at: string;
  items: PendingDecisionItem[];
};

export type MailboxSyncResult = {
  snapshot: MailboxFolderSnapshot;
  pendingDecisions: PendingDecisionQueue;
  stats: {
    totalFolders: number;
    referencedFoldersChecked: number;
    openDecisions: number;
    changedDecisions: number;
  };
  syncMode: "fetched" | "cached";
};

function parseFolderAttributes(value: string | undefined): string[] {
  if (!value) return [];
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function normalizeFolderPath(folderPath: string): string {
  return folderPath
    .normalize("NFC")
    .replace(/\\/g, "/")
    .replace(/\/+/, "/")
    .replace(/\/+/g, "/")
    .replace(/^\/+|\/+$/g, "")
    .split("/")
    .map((segment) => segment.trim())
    .filter(Boolean)
    .join("/");
}

function mailboxName(cfg: EnvConfig): string {
  return cfg.HIMALAYA_ACCOUNT?.trim() || cfg.HIMALAYA_COMMAND;
}

function makeSnapshot(cfg: EnvConfig, folders: MailFolder[]): MailboxFolderSnapshot {
  return {
    schema_version: 1,
    mailbox: mailboxName(cfg),
    fetched_at: new Date().toISOString(),
    source: {
      command: cfg.HIMALAYA_COMMAND,
      ...(cfg.HIMALAYA_ACCOUNT ? { account: cfg.HIMALAYA_ACCOUNT } : {}),
    },
    folders: folders.map((folder) => ({
      path: folder.name,
      normalized_path: normalizeFolderPath(folder.name),
      attributes: parseFolderAttributes(folder.desc),
      ...(folder.desc ? { raw_desc: folder.desc } : {}),
    })),
  };
}

export function loadMailboxFolderSnapshot(filePath: string): MailboxFolderSnapshot | null {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw) as MailboxFolderSnapshot;
    if (parsed && parsed.schema_version === 1 && Array.isArray(parsed.folders) && typeof parsed.fetched_at === "string") {
      return parsed;
    }
  } catch {
    // ignore
  }
  return null;
}

export function isMailboxFolderSnapshotStale(snapshot: MailboxFolderSnapshot | null, maxAgeHours: number): boolean {
  if (!snapshot) return true;
  const fetchedAtMs = Date.parse(snapshot.fetched_at);
  if (!Number.isFinite(fetchedAtMs)) return true;
  const maxAgeMs = Math.max(0, maxAgeHours) * 60 * 60 * 1000;
  return (Date.now() - fetchedAtMs) > maxAgeMs;
}

export function loadPendingDecisionQueue(filePath: string): PendingDecisionQueue {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw) as PendingDecisionQueue;
    if (Array.isArray(parsed.items)) {
      return {
        schema_version: 1,
        updated_at: parsed.updated_at || new Date().toISOString(),
        items: parsed.items,
      };
    }
  } catch {
    // ignore
  }
  return {
    schema_version: 1,
    updated_at: new Date().toISOString(),
    items: [],
  };
}

export function saveJson(filePath: string, value: unknown): void {
  const resolved = path.resolve(filePath);
  fs.mkdirSync(path.dirname(resolved), { recursive: true });
  fs.writeFileSync(resolved, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function decisionId(kind: PendingDecisionKind, entityKind: PendingDecisionEntityKind, entityId: string, folder: string): string {
  return `${kind}:${entityKind}:${entityId}:${normalizeFolderPath(folder).toLowerCase()}`;
}

function upsertDecision(
  existing: PendingDecisionItem | undefined,
  draft: Omit<PendingDecisionItem, "status" | "last_notified_at" | "resolved_at" | "resolution">,
): PendingDecisionItem {
  if (!existing) {
    return {
      ...draft,
      status: "open",
      last_notified_at: null,
      resolved_at: null,
      resolution: null,
    };
  }
  return {
    ...existing,
    ...draft,
    status: existing.status === "resolved" || existing.status === "ignored" ? "open" : existing.status,
    resolved_at: existing.status === "resolved" || existing.status === "ignored" ? null : existing.resolved_at,
    resolution: existing.status === "resolved" || existing.status === "ignored" ? null : existing.resolution,
  };
}

function plannedFolderRefs(cfg: EnvConfig, projects: Project[], topics: Topic[]): Array<{
  entityKind: PendingDecisionEntityKind;
  entityId: string;
  expectedFolder: string;
  summary: string;
  proposedActions: string[];
  meta?: Record<string, unknown>;
}> {
  const refs: Array<{
    entityKind: PendingDecisionEntityKind;
    entityId: string;
    expectedFolder: string;
    summary: string;
    proposedActions: string[];
    meta?: Record<string, unknown>;
  }> = [];

  for (const project of projects) {
    refs.push({
      entityKind: "project",
      entityId: project.id,
      expectedFolder: project.mailbox_folder,
      summary: `Projektordner fehlt in der Mailbox: ${project.title}`,
      proposedActions: ["mailbox_create_folder", "catalog_change_folder", "ignore"],
      meta: { title: project.title },
    });
    refs.push({
      entityKind: "project",
      entityId: `${project.id}:needs-reply`,
      expectedFolder: normalizeFolderPath(`${project.mailbox_folder}/_Needs-Reply`),
      summary: `Needs-Reply-Unterordner fehlt im Projektordner: ${project.title}`,
      proposedActions: ["mailbox_create_folder", "ignore_feature", "ignore"],
      meta: { title: project.title, parent_entity_id: project.id, feature: "needs-reply", scope: "project" },
    });
  }

  for (const topic of topics) {
    refs.push({
      entityKind: "topic",
      entityId: topic.id,
      expectedFolder: topic.mailbox_folder,
      summary: `Topic-Ordner fehlt in der Mailbox: ${topic.title}`,
      proposedActions: ["mailbox_create_folder", "catalog_change_folder", "ignore"],
      meta: { title: topic.title },
    });
    refs.push({
      entityKind: "topic",
      entityId: `${topic.id}:needs-reply`,
      expectedFolder: normalizeFolderPath(`${topic.mailbox_folder}/_Needs-Reply`),
      summary: `Needs-Reply-Unterordner fehlt im Topic-Ordner: ${topic.title}`,
      proposedActions: ["mailbox_create_folder", "ignore_feature", "ignore"],
      meta: { title: topic.title, parent_entity_id: topic.id, feature: "needs-reply", scope: "topic" },
    });
  }

  refs.push({
    entityKind: "special",
    entityId: "inbox-needs-reply",
    expectedFolder: "Inbox/_Needs-Reply",
    summary: "Sonderordner für nicht zugeordnete Needs-Reply-Mails fehlt in der Mailbox",
    proposedActions: ["mailbox_create_folder", "ignore_feature", "ignore"],
    meta: { feature: "needs-reply", scope: "inbox" },
  });

  return refs;
}

function reconcilePendingDecisions(
  cfg: EnvConfig,
  existing: PendingDecisionQueue,
  snapshot: MailboxFolderSnapshot,
  projects: Project[],
  topics: Topic[],
): PendingDecisionQueue {
  const now = new Date().toISOString();
  const folderSet = new Set(snapshot.folders.map((folder) => folder.normalized_path.toLowerCase()));
  const nextItems = [...existing.items];
  const seenIds = new Set<string>();

  for (const ref of plannedFolderRefs(cfg, projects, topics)) {
    const normalizedFolder = normalizeFolderPath(ref.expectedFolder);
    const id = decisionId("missing_folder", ref.entityKind, ref.entityId, normalizedFolder);
    seenIds.add(id);
    const exists = folderSet.has(normalizedFolder.toLowerCase());
    const index = nextItems.findIndex((item) => item.id === id);
    const current = index >= 0 ? nextItems[index] : undefined;

    if (!exists) {
      const updated = upsertDecision(current, {
        id,
        kind: "missing_folder",
        entity_kind: ref.entityKind,
        entity_id: ref.entityId,
        expected_folder: normalizedFolder,
        detected_at: current?.detected_at || now,
        source: "mailbox-sync",
        summary: ref.summary,
        proposed_actions: ref.proposedActions,
        ...(ref.meta ? { meta: ref.meta } : {}),
      });
      if (index >= 0) nextItems[index] = updated;
      else nextItems.push(updated);
      continue;
    }

    if (current && current.status !== "resolved") {
      nextItems[index] = {
        ...current,
        status: "resolved",
        resolved_at: now,
        resolution: "folder_present",
      };
    }
  }

  for (let i = 0; i < nextItems.length; i += 1) {
    const item = nextItems[i];
    if (item.kind === "missing_folder" && !seenIds.has(item.id) && item.status !== "resolved" && item.status !== "ignored") {
      nextItems[i] = {
        ...item,
        status: "resolved",
        resolved_at: now,
        resolution: "reference_removed",
      };
    }
  }

  return {
    schema_version: 1,
    updated_at: now,
    items: nextItems,
  };
}

export function getOpenPendingDecisions(queue: PendingDecisionQueue): PendingDecisionItem[] {
  return queue.items.filter((item) => item.status === "open" || item.status === "notified" || item.status === "snoozed");
}

export function summarizePendingDecisionsForChat(items: PendingDecisionItem[], limit = 3): string[] {
  return items.slice(0, Math.max(0, limit)).map((item) => {
    const actions = item.proposed_actions.join(" / ");
    return `${item.summary} (${item.expected_folder}) — Optionen: ${actions}`;
  });
}

export function updateFolderSyncMetadata(
  snapshot: MailboxFolderSnapshot,
  folderPath: string,
  sync: {
    last_synced_at: string;
    mode: string;
    fetch_limit: number;
    inspected: number;
    skipped: number;
    errors: number;
    msg_dir: string;
    export_dir: string;
  },
): MailboxFolderSnapshot {
  const normalized = normalizeFolderPath(folderPath);
  const folders = snapshot.folders.map((folder) => {
    if (folder.normalized_path !== normalized) return folder;
    return {
      ...folder,
      sync,
    };
  });
  return {
    ...snapshot,
    folders,
  };
}

export function syncMailboxFolders(cfg: EnvConfig, projects: Project[], topics: Topic[], opts?: { force?: boolean }): MailboxSyncResult {
  const existingSnapshot = loadMailboxFolderSnapshot(cfg.MAILBOX_FOLDERS_FILE);
  const shouldFetch = opts?.force === true || isMailboxFolderSnapshotStale(existingSnapshot, cfg.MAILBOX_FOLDERS_MAX_AGE_HOURS);
  const snapshot = shouldFetch
    ? makeSnapshot(cfg, listFolders(cfg.HIMALAYA_COMMAND, cfg.HIMALAYA_ACCOUNT))
    : existingSnapshot!;

  if (shouldFetch) {
    saveJson(cfg.MAILBOX_FOLDERS_FILE, snapshot);
  }

  const existingQueue = loadPendingDecisionQueue(cfg.PENDING_DECISIONS_FILE);
  const nextQueue = reconcilePendingDecisions(cfg, existingQueue, snapshot, projects, topics);
  saveJson(cfg.PENDING_DECISIONS_FILE, nextQueue);

  const openDecisions = getOpenPendingDecisions(nextQueue).length;
  const previousActive = new Set(existingQueue.items.filter((item) => item.status !== "resolved" && item.status !== "ignored").map((item) => `${item.id}:${item.status}`));
  const nextActive = new Set(nextQueue.items.filter((item) => item.status !== "resolved" && item.status !== "ignored").map((item) => `${item.id}:${item.status}`));
  let changedDecisions = 0;
  for (const key of nextActive) if (!previousActive.has(key)) changedDecisions += 1;
  for (const key of previousActive) if (!nextActive.has(key)) changedDecisions += 1;

  return {
    snapshot,
    pendingDecisions: nextQueue,
    stats: {
      totalFolders: snapshot.folders.length,
      referencedFoldersChecked: plannedFolderRefs(cfg, projects, topics).length,
      openDecisions,
      changedDecisions,
    },
    syncMode: shouldFetch ? "fetched" : "cached",
  };
}
