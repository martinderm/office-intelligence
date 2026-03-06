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
    // Heuristic: first token is numeric message id in himalaya envelope list output
    const m = line.match(/^(\d+)\b(.*)$/);
    if (!m) continue;
    items.push({ id: m[1], rawLine: line, subjectHint: m[2]?.trim() || undefined });
  }
  return items;
}

export function listEnvelopes(command: string, sourceFolder: string, limit: number): Envelope[] {
  const args = ["envelope", "list", "-f", sourceFolder, "-s", String(limit)];
  const out = runCmd(command, args);
  return parseEnvelopeList(out);
}

export function readMessage(command: string, sourceFolder: string, id: string): MailMessage {
  const args = ["message", "read", "-f", sourceFolder, id];
  const raw = runCmd(command, args);
  return { id, raw };
}

export function copyMessage(command: string, targetFolder: string, id: string): void {
  const args = ["message", "copy", targetFolder, id];
  runCmd(command, args);
}
