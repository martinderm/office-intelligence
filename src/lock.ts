import fs from "node:fs";
import path from "node:path";

type LockHandle = { release: () => void };

export function acquireLock(lockPathRaw: string, ttlSeconds: number): LockHandle {
  const lockPath = path.resolve(lockPathRaw);
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });

  const now = Date.now();
  const ttlMs = Math.max(1, ttlSeconds) * 1000;

  if (fs.existsSync(lockPath)) {
    const stat = fs.statSync(lockPath);
    const age = now - stat.mtimeMs;
    if (age > ttlMs) {
      fs.rmSync(lockPath, { force: true });
    } else {
      throw new Error(`lock exists: ${lockPath} (age ${Math.round(age / 1000)}s)`);
    }
  }

  const fd = fs.openSync(lockPath, "wx");
  fs.writeFileSync(
    fd,
    JSON.stringify({ pid: process.pid, createdAt: new Date(now).toISOString() }),
    "utf8",
  );

  return {
    release: () => {
      try {
        fs.closeSync(fd);
      } catch {
        // noop
      }
      fs.rmSync(lockPath, { force: true });
    },
  };
}
