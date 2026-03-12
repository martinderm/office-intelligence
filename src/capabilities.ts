import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

export type CapabilityPolicy = {
  supportsImap4Rev1: boolean;
  supportsUidPlus: boolean;
  supportsMove: boolean;
  supportsIdle: boolean;
  supportsCondstore: boolean;
  supportsQresync: boolean;
  supportsSpecialUse: boolean;
  supportsNamespace: boolean;
  supportsUtf8Accept: boolean;
  recommendedRoutingMode: "normal" | "single-target";
  rationale: string[];
};

export type MailboxCapabilities = {
  mailboxKey: string;
  sourceFolder: string;
  fetchedAt: string;
  capabilities: string[];
  rawCapabilityLine?: string;
  rawGreetingLine?: string;
  host?: string;
  policy: CapabilityPolicy;
};

function sanitizeKey(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, "_");
}

function parseCapabilityLine(output: string): { caps: string[]; raw?: string } {
  const lines = output.split(/\r?\n/);

  // Prefer explicit CAPABILITY response, fallback to greeting capability.
  const explicit = lines.find((l) => l.includes("* CAPABILITY "));
  if (explicit) {
    const raw = explicit.slice(explicit.indexOf("* CAPABILITY ") + 13).trim();
    return { caps: raw.split(/\s+/).filter(Boolean), raw };
  }

  const greeting = lines.find((l) => l.includes("OK [CAPABILITY "));
  if (greeting) {
    const m = greeting.match(/OK \[CAPABILITY\s+([^\]]+)\]/);
    const raw = m?.[1]?.trim();
    return { caps: (raw ?? "").split(/\s+/).filter(Boolean), raw };
  }

  return { caps: [] };
}

function parseHost(output: string): string | undefined {
  const m = output.match(/DnsName\("([^"]+)"\)/);
  return m?.[1];
}

function hasCap(caps: string[], token: string): boolean {
  const want = token.toUpperCase();
  return caps.some((c) => c.toUpperCase() === want);
}

export function buildCapabilityPolicy(caps: string[]): CapabilityPolicy {
  const supportsImap4Rev1 = hasCap(caps, "IMAP4REV1");
  const supportsUidPlus = hasCap(caps, "UIDPLUS");
  const supportsMove = hasCap(caps, "MOVE");
  const supportsIdle = hasCap(caps, "IDLE");
  const supportsCondstore = hasCap(caps, "CONDSTORE");
  const supportsQresync = hasCap(caps, "QRESYNC");
  const supportsSpecialUse = hasCap(caps, "SPECIAL-USE") || hasCap(caps, "CREATE-SPECIAL-USE");
  const supportsNamespace = hasCap(caps, "NAMESPACE");
  const supportsUtf8Accept = hasCap(caps, "UTF8=ACCEPT");

  const rationale: string[] = [];
  if (!supportsMove) {
    rationale.push("MOVE nicht verfügbar: Multi-Target-Copy kann backend-abhängig riskant sein.");
  }
  if (supportsUidPlus) {
    rationale.push("UIDPLUS verfügbar: UID-Mapping bei COPY/APPEND kann robuster verfolgt werden.");
  } else {
    rationale.push("UIDPLUS fehlt: weniger robuste UID-Nachverfolgung nach COPY/APPEND.");
  }

  return {
    supportsImap4Rev1,
    supportsUidPlus,
    supportsMove,
    supportsIdle,
    supportsCondstore,
    supportsQresync,
    supportsSpecialUse,
    supportsNamespace,
    supportsUtf8Accept,
    recommendedRoutingMode: supportsMove ? "normal" : "single-target",
    rationale,
  };
}

function fetchCapabilitiesViaTrace(command: string, sourceFolder: string): {
  capabilities: string[];
  rawCapabilityLine?: string;
  rawGreetingLine?: string;
  host?: string;
} {
  const args = ["--trace", "envelope", "list", "-s", "1", "-f", sourceFolder];
  const result = spawnSync(command, args, { encoding: "utf8" });
  const output = `${result.stdout ?? ""}\n${result.stderr ?? ""}`;

  if (result.status !== 0) {
    // Some account gates do not allow --trace; degrade gracefully.
    if (output.includes("command not allowed") || output.includes("--trace")) {
      return { capabilities: [] };
    }
    throw new Error(`capability fetch failed: ${command} ${args.join(" ")}\n${output}`);
  }

  const parsed = parseCapabilityLine(output);
  const host = parseHost(output);
  const greetingMatch = output.match(/OK \[CAPABILITY\s+([^\]]+)\]/);

  return {
    capabilities: parsed.caps,
    rawCapabilityLine: parsed.raw,
    rawGreetingLine: greetingMatch?.[1]?.trim(),
    host,
  };
}

export function loadOrFetchCapabilities(opts: {
  command: string;
  sourceFolder: string;
  capabilitiesDir: string;
  mailboxKey?: string;
}): MailboxCapabilities {
  const mailboxKey = opts.mailboxKey || opts.command;
  const safe = sanitizeKey(mailboxKey);
  const filePath = path.resolve(opts.capabilitiesDir, `${safe}.json`);

  if (fs.existsSync(filePath)) {
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8")) as Partial<MailboxCapabilities>;
    const capabilities = parsed.capabilities ?? [];
    const policy = parsed.policy ?? buildCapabilityPolicy(capabilities);
    return {
      mailboxKey: parsed.mailboxKey ?? mailboxKey,
      sourceFolder: parsed.sourceFolder ?? opts.sourceFolder,
      fetchedAt: parsed.fetchedAt ?? new Date().toISOString(),
      capabilities,
      rawCapabilityLine: parsed.rawCapabilityLine,
      rawGreetingLine: parsed.rawGreetingLine,
      host: parsed.host,
      policy,
    };
  }

  const fetched = fetchCapabilitiesViaTrace(opts.command, opts.sourceFolder);
  const record: MailboxCapabilities = {
    mailboxKey,
    sourceFolder: opts.sourceFolder,
    fetchedAt: new Date().toISOString(),
    capabilities: fetched.capabilities,
    rawCapabilityLine: fetched.rawCapabilityLine,
    rawGreetingLine: fetched.rawGreetingLine,
    host: fetched.host,
    policy: buildCapabilityPolicy(fetched.capabilities),
  };

  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(record, null, 2)}\n`, "utf8");
  return record;
}
