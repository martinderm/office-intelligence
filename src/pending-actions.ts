import fs from "node:fs";
import path from "node:path";
import { saveJson } from "./mailbox-sync.js";

export type PendingActionStatus = "pending";
export type PendingActionType = "mail_postprocess";
export type PendingActionTargetKind = "project" | "topic" | "none";
export type PendingActionClassificationSource = "openclaw_tool" | "legacy_llm" | "heuristic_only";

export type PendingActionTarget = {
  kind: PendingActionTargetKind;
  id: string | null;
  workpackage_id?: string | null;
  confidence?: number | null;
};

export type PendingActionItem = {
  id: string;
  type: PendingActionType;
  status: PendingActionStatus;
  created_at: string;
  updated_at: string;
  source: {
    mailbox: string;
    source_folder: string;
    envelope_id: string;
    stable_id: string;
    file_id: string;
    artifact_path: string;
  };
  target: PendingActionTarget;
  needs_reply: boolean;
  classification_source: PendingActionClassificationSource;
};

export type PendingActionsQueue = {
  schema_version: 1;
  updated_at: string;
  items: PendingActionItem[];
};

export type ActionLogStatus = "completed" | "failed" | "dismissed" | "superseded";

export type ActionLogEntry = {
  schema_version: 1;
  id: string;
  action_id: string;
  type: PendingActionType;
  status: ActionLogStatus;
  processed_at: string;
  source: PendingActionItem["source"];
  target: PendingActionTarget;
  needs_reply: boolean;
  result?: Record<string, unknown>;
};

export type ActionLogFile = {
  schema_version: 1;
  week: string;
  updated_at: string;
  entries: ActionLogEntry[];
};

function emptyQueue(): PendingActionsQueue {
  return {
    schema_version: 1,
    updated_at: new Date().toISOString(),
    items: [],
  };
}

export function loadPendingActions(filePath: string): PendingActionsQueue {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw) as PendingActionsQueue;
    if (parsed && Array.isArray(parsed.items)) {
      return {
        schema_version: 1,
        updated_at: parsed.updated_at || new Date().toISOString(),
        items: parsed.items.filter((item) => item?.status === "pending"),
      };
    }
  } catch {
    // ignore and recreate lazily
  }
  return emptyQueue();
}

export function savePendingActions(filePath: string, queue: PendingActionsQueue): void {
  saveJson(filePath, {
    schema_version: 1,
    updated_at: queue.updated_at,
    items: queue.items,
  });
}

export function pendingActionId(stableId: string, fileId: string): string {
  return `mail_postprocess:${stableId}:${fileId}`;
}

export function upsertPendingAction(filePath: string, draft: Omit<PendingActionItem, "status" | "created_at" | "updated_at">): PendingActionItem {
  const queue = loadPendingActions(filePath);
  const now = new Date().toISOString();
  const existingIndex = queue.items.findIndex((item) => item.id === draft.id);
  const existing = existingIndex >= 0 ? queue.items[existingIndex] : undefined;
  const item: PendingActionItem = {
    ...draft,
    status: "pending",
    created_at: existing?.created_at || now,
    updated_at: now,
  };

  if (existingIndex >= 0) queue.items[existingIndex] = item;
  else queue.items.push(item);

  queue.updated_at = now;
  savePendingActions(filePath, queue);
  return item;
}

export function removePendingAction(filePath: string, actionId: string): PendingActionItem | null {
  const queue = loadPendingActions(filePath);
  const index = queue.items.findIndex((item) => item.id === actionId);
  if (index < 0) return null;
  const [item] = queue.items.splice(index, 1);
  queue.updated_at = new Date().toISOString();
  savePendingActions(filePath, queue);
  return item ?? null;
}

function isoWeek(date: Date): { year: number; week: number } {
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const dayNum = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil((((d.getTime() - yearStart.getTime()) / 86400000) + 1) / 7);
  return { year: d.getUTCFullYear(), week };
}

export function actionLogWeekId(date = new Date()): string {
  const { year, week } = isoWeek(date);
  return `${year}-W${String(week).padStart(2, "0")}`;
}

export function actionLogPath(logDir: string, date = new Date()): string {
  return path.join(logDir, `${actionLogWeekId(date)}.json`);
}

function loadActionLog(filePath: string, week: string): ActionLogFile {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    const parsed = JSON.parse(raw) as ActionLogFile;
    if (parsed && Array.isArray(parsed.entries)) {
      return {
        schema_version: 1,
        week: parsed.week || week,
        updated_at: parsed.updated_at || new Date().toISOString(),
        entries: parsed.entries,
      };
    }
  } catch {
    // ignore and recreate lazily
  }
  return {
    schema_version: 1,
    week,
    updated_at: new Date().toISOString(),
    entries: [],
  };
}

export function appendActionLog(logDir: string, entry: Omit<ActionLogEntry, "schema_version" | "processed_at" | "id"> & { id?: string; processed_at?: string }): ActionLogEntry {
  const processedAt = entry.processed_at || new Date().toISOString();
  const date = new Date(processedAt);
  const week = actionLogWeekId(date);
  const filePath = actionLogPath(logDir, date);
  const log = loadActionLog(filePath, week);
  const fullEntry: ActionLogEntry = {
    schema_version: 1,
    id: entry.id || `log:${entry.action_id}:${processedAt}`,
    action_id: entry.action_id,
    type: entry.type,
    status: entry.status,
    processed_at: processedAt,
    source: entry.source,
    target: entry.target,
    needs_reply: entry.needs_reply,
    ...(entry.result ? { result: entry.result } : {}),
  };
  log.entries.push(fullEntry);
  log.updated_at = new Date().toISOString();
  saveJson(filePath, log);
  return fullEntry;
}

export function completePendingAction(params: {
  pendingActionsFile: string;
  actionLogDir: string;
  actionId: string;
  status: ActionLogStatus;
  result?: Record<string, unknown>;
}): ActionLogEntry | null {
  const item = removePendingAction(params.pendingActionsFile, params.actionId);
  if (!item) return null;
  return appendActionLog(params.actionLogDir, {
    action_id: item.id,
    type: item.type,
    status: params.status,
    source: item.source,
    target: item.target,
    needs_reply: item.needs_reply,
    ...(params.result ? { result: params.result } : {}),
  });
}
