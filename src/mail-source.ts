import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

export type Envelope = {
  id: string;
  rawLine: string;
  subjectHint?: string;
};

export type MailMessage = {
  id: string;
  raw: string;
};

function runCmd(command: string, args: string[]): string {
  const result = spawnSync(command, args, { encoding: "utf8" });
  if (result.status !== 0) {
    throw new Error(`command failed: ${command} ${args.join(" ")}\n${result.stderr || result.stdout}`);
  }
  return result.stdout ?? "";
}

function parseEnvelopeList(output: string): Envelope[] {
  const lines = output.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const items: Envelope[] = [];
  for (const line of lines) {
    // Plain format: "7581 ..."
    const plain = line.match(/^(\d+)\b(.*)$/);
    if (plain) {
      items.push({ id: plain[1], rawLine: line, subjectHint: plain[2]?.trim() || undefined });
      continue;
    }

    // Table format: "| 7581 | * | Subject | From | Date |"
    const table = line.match(/^\|\s*(\d+)\s*\|/);
    if (table) {
      items.push({ id: table[1], rawLine: line });
    }
  }
  return items;
}

export function listEnvelopes(command: string, sourceFolder: string, limit: number): Envelope[] {
  const args = ["envelope", "list", "-f", sourceFolder, "-s", String(limit)];
  const out = runCmd(command, args);
  return parseEnvelopeList(out);
}

export function readMessage(
  command: string,
  sourceFolder: string,
  id: string,
  exportBaseDir?: string,
): MailMessage {
  // Prefer raw MIME export to preserve HTML parts for sanitizing.
  if (exportBaseDir) {
    try {
      const exportDir = path.resolve(exportBaseDir, "exports");
      fs.mkdirSync(exportDir, { recursive: true });

      const exportArgs = ["message", "export", "-f", sourceFolder, "--full", "-d", exportDir, id];
      runCmd(command, exportArgs);

      const emlPath = path.join(exportDir, `${id}.eml`);
      if (fs.existsSync(emlPath)) {
        const raw = fs.readFileSync(emlPath, "utf8");
        return { id, raw };
      }
    } catch {
      // fallback to human-friendly read below
    }
  }

  const args = ["message", "read", "-f", sourceFolder, id];
  const raw = runCmd(command, args);
  return { id, raw };
}

export function copyMessage(command: string, targetFolder: string, id: string): void {
  const args = ["message", "copy", targetFolder, id];
  runCmd(command, args);
}
