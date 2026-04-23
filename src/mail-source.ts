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

export type UidPlusCopyInfo = {
  mailboxUidValidity?: string;
  sourceUids?: string;
  targetUids?: string;
};

export type RouteCommandResult = {
  output: string;
  uidPlus?: UidPlusCopyInfo;
};

export type MailFolder = {
  name: string;
  desc?: string;
};

function shouldUseShell(command: string): boolean {
  return process.platform === "win32" && /\.(cmd|bat)$/i.test(command.trim());
}

function runCmd(command: string, args: string[], timeoutMs?: number): string {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    shell: shouldUseShell(command),
    ...(timeoutMs && timeoutMs > 0 ? { timeout: timeoutMs } : {}),
  });
  if (result.error && (result.error as NodeJS.ErrnoException).code === "ETIMEDOUT") {
    throw new Error(`command timeout after ${timeoutMs}ms: ${command} ${args.join(" ")}`);
  }
  if (result.status !== 0) {
    throw new Error(`command failed: ${command} ${args.join(" ")}\n${result.stderr || result.stdout}`);
  }
  return result.stdout ?? "";
}

function runCmdDetailed(command: string, args: string[], timeoutMs?: number): { output: string; status: number } {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    shell: shouldUseShell(command),
    ...(timeoutMs && timeoutMs > 0 ? { timeout: timeoutMs } : {}),
  });
  const output = `${result.stdout ?? ""}\n${result.stderr ?? ""}`;
  if (result.error && (result.error as NodeJS.ErrnoException).code === "ETIMEDOUT") {
    throw new Error(`command timeout after ${timeoutMs}ms: ${command} ${args.join(" ")}`);
  }
  if (result.status !== 0) {
    throw new Error(`command failed: ${command} ${args.join(" ")}\n${output}`);
  }
  return { output, status: result.status ?? 0 };
}

function parseUidPlusCopy(output: string): UidPlusCopyInfo | undefined {
  const m = output.match(/COPYUID\s+(\d+)\s+([\d:,]+)\s+([\d:,]+)/i);
  if (!m) return undefined;
  return {
    mailboxUidValidity: m[1],
    sourceUids: m[2],
    targetUids: m[3],
  };
}

function extractJsonArray(output: string): string {
  const ansiStripped = output.replace(/\u001b\[[0-9;]*m/g, "");
  const match = ansiStripped.match(/(\[\s*\{[\s\S]*\])\s*$/);
  if (!match) {
    throw new Error(`json array not found in command output: ${ansiStripped.slice(0, 300)}`);
  }
  return match[1];
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

export function listFolders(command: string, account?: string): MailFolder[] {
  const args = ["folder", "list", "-o", "json"];
  if (account?.trim()) {
    args.push("-a", account.trim());
  }
  const out = runCmd(command, args);
  const parsed = JSON.parse(extractJsonArray(out)) as Array<{ name?: string; desc?: string }>;
  return parsed
    .filter((item) => typeof item?.name === "string" && item.name.trim())
    .map((item) => ({
      name: item.name!.trim(),
      ...(typeof item.desc === "string" && item.desc.trim() ? { desc: item.desc.trim() } : {}),
    }));
}

export function listEnvelopes(command: string, sourceFolder: string, limit: number): Envelope[] {
  return listEnvelopesPage(command, sourceFolder, 1, limit);
}

export function listEnvelopesInFolder(command: string, sourceFolder: string, limit: number): Envelope[] {
  return listEnvelopesPage(command, sourceFolder, 1, limit);
}

export function listEnvelopesPage(
  command: string,
  sourceFolder: string,
  page: number,
  pageSize: number,
  queryTerms: string[] = [],
): Envelope[] {
  const args = ["envelope", "list", "-f", sourceFolder, "-p", String(page), "-s", String(pageSize), ...queryTerms];
  try {
    const out = runCmd(command, args);
    return parseEnvelopeList(out);
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    if (msg.toLowerCase().includes("out of bound")) {
      return [];
    }
    throw error;
  }
}

export function readMessage(
  command: string,
  sourceFolder: string,
  id: string,
  exportBaseDir?: string,
  timeoutMs?: number,
): MailMessage {
  // Prefer raw MIME export to preserve HTML parts for sanitizing.
  if (exportBaseDir) {
    try {
      const exportDir = path.resolve(exportBaseDir, "exports");
      fs.mkdirSync(exportDir, { recursive: true });

      const exportArgs = ["message", "export", "-f", sourceFolder, "--full", "-d", exportDir, id];
      runCmd(command, exportArgs, timeoutMs);

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
  const raw = runCmd(command, args, timeoutMs);
  return { id, raw };
}

export function copyMessage(command: string, targetFolder: string, id: string): void {
  const args = ["message", "copy", targetFolder, id];
  runCmd(command, args);
}

export function copyMessageWithUidPlus(command: string, targetFolder: string, id: string): RouteCommandResult {
  const args = ["--trace", "message", "copy", targetFolder, id];
  try {
    const result = runCmdDetailed(command, args);
    return { output: result.output, uidPlus: parseUidPlusCopy(result.output) };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    // Some account gates disallow --trace. Fallback to normal copy without UIDPLUS mapping.
    if (msg.includes("command not allowed") || msg.includes("--trace")) {
      const fallback = runCmdDetailed(command, ["message", "copy", targetFolder, id]);
      return { output: fallback.output };
    }
    throw err;
  }
}

export function moveMessage(command: string, targetFolder: string, id: string): void {
  const args = ["message", "move", targetFolder, id];
  runCmd(command, args);
}
