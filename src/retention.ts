import fs from "node:fs";
import path from "node:path";

export function cleanupDebugMessages(msgsDirRaw: string, retentionDays: number | null): number {
  if (retentionDays === null) return 0;

  const msgsDir = path.resolve(msgsDirRaw);
  if (!fs.existsSync(msgsDir)) return 0;

  const now = Date.now();
  const maxAgeMs = Math.max(0, retentionDays) * 24 * 60 * 60 * 1000;
  let deleted = 0;

  for (const entry of fs.readdirSync(msgsDir, { withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.endsWith(".json")) continue;
    const full = path.join(msgsDir, entry.name);
    const st = fs.statSync(full);
    const age = now - st.mtimeMs;
    if (age > maxAgeMs) {
      fs.rmSync(full, { force: true });
      deleted += 1;
    }
  }

  return deleted;
}
