import fs from "node:fs";

export type RetryQueueEvent =
  | {
      type: "retry_enqueued";
      key: string;
      sourceFolder: string;
      envelopeId: string;
      attempts: number;
      nextAttemptAtMs: number;
      error: string;
      timestamp: string;
    }
  | {
      type: "retry_succeeded";
      key: string;
      sourceFolder: string;
      envelopeId: string;
      timestamp: string;
    };

export function makeRetryKey(sourceFolder: string, envelopeId: string): string {
  return `${sourceFolder}::${envelopeId}`;
}

function safeParseJsonLine(line: string): unknown | null {
  const trimmed = line.trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

export function loadRetryQueueState(queuePath: string): Map<string, RetryQueueEvent> {
  const state = new Map<string, RetryQueueEvent>();
  if (!fs.existsSync(queuePath)) return state;

  const raw = fs.readFileSync(queuePath, "utf8");
  const lines = raw.split(/\r?\n/);
  for (const line of lines) {
    const parsed = safeParseJsonLine(line);
    if (!parsed || typeof parsed !== "object") continue;
    const obj = parsed as any;
    if (obj.type !== "retry_enqueued" && obj.type !== "retry_succeeded") continue;
    if (typeof obj.key !== "string") continue;
    state.set(obj.key, obj as RetryQueueEvent);
  }
  return state;
}

export function loadDueRetryItems(params: {
  queuePath: string;
  nowMs: number;
  limit: number;
}): Array<{ key: string; sourceFolder: string; envelopeId: string; attempts: number }> {
  const { queuePath, nowMs, limit } = params;
  const state = loadRetryQueueState(queuePath);

  const due: Array<{ key: string; sourceFolder: string; envelopeId: string; attempts: number; nextAttemptAtMs: number }> = [];
  for (const [key, evt] of state.entries()) {
    if (evt.type !== "retry_enqueued") continue;
    if (evt.nextAttemptAtMs > nowMs) continue;
    due.push({
      key,
      sourceFolder: evt.sourceFolder,
      envelopeId: evt.envelopeId,
      attempts: evt.attempts,
      nextAttemptAtMs: evt.nextAttemptAtMs,
    });
  }

  due.sort((a, b) => a.nextAttemptAtMs - b.nextAttemptAtMs || a.attempts - b.attempts);
  return due.slice(0, Math.max(0, limit)).map(({ key, sourceFolder, envelopeId, attempts }) => ({
    key,
    sourceFolder,
    envelopeId,
    attempts,
  }));
}

export function computeNextAttemptAtMs(params: { nowMs: number; attempts: number; baseBackoffMs: number }): number {
  // Exponential backoff with base. Attempts starts at 1.
  const { nowMs, attempts, baseBackoffMs } = params;
  const factor = Math.max(1, Math.pow(2, Math.max(0, attempts - 1)));
  return nowMs + baseBackoffMs * factor;
}
