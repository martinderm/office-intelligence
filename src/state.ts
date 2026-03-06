import fs from "node:fs";
import path from "node:path";

export function ensureRuntimeDirs(paths: string[]): void {
  for (const p of paths) {
    fs.mkdirSync(path.resolve(p), { recursive: true });
  }
}

export function appendJsonl(filePathRaw: string, data: Record<string, unknown>): void {
  const filePath = path.resolve(filePathRaw);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.appendFileSync(filePath, `${JSON.stringify(data)}\n`, "utf8");
}
