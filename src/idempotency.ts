import fs from "node:fs";
import path from "node:path";

export function getProcessedIds(statePathRaw: string): Set<string> {
  const statePath = path.resolve(statePathRaw);
  if (!fs.existsSync(statePath)) return new Set<string>();
  const out = new Set<string>();
  const raw = fs.readFileSync(statePath, "utf8");
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const obj = JSON.parse(line);
      if (obj?.type !== "message_processed") continue;

      // Preferred modern key.
      if (typeof obj.stableId === "string" && obj.stableId.trim()) {
        out.add(obj.stableId.trim());
      }

      // Backward compatibility with older runs.
      if (typeof obj.messageId === "string" && obj.messageId.trim()) {
        out.add(obj.messageId.trim());
      }
    } catch {
      // ignore malformed line
    }
  }
  return out;
}
