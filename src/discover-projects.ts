import fs from "node:fs";
import path from "node:path";
import { listEnvelopes, readMessage } from "./mail-source.js";
import { prepareMailText } from "./preprocess.js";
import { getConfig } from "./env.js";
import { loadProjects } from "./projects.js";
import { matchProject } from "./matcher.js";
import { Project } from "./types.js";

type DiscoverSource = "local" | "imap";

type DiscoverOptions = {
  enabled: boolean;
  last: number;
  outPath?: string;
  source: DiscoverSource;
};

type CandidateCluster = {
  key: string;
  idSeed: string;
  titleSeed: string;
  domain: string;
  messageCount: number;
  participants: Set<string>;
  samples: string[];
  lastSeen?: string;
};

const SUBJECT_STOPWORDS = new Set([
  "re",
  "fwd",
  "wg",
  "aw",
  "the",
  "and",
  "for",
  "und",
  "der",
  "die",
  "das",
  "von",
  "mit",
  "zur",
  "zum",
  "bitte",
  "danke",
  "update",
  "info",
]);

function parseBoolFlag(args: string[], flag: string): boolean {
  return args.includes(flag);
}

function parseIntFlag(args: string[], prefix: string, fallback: number): number {
  const raw = args.find((a) => a.startsWith(prefix));
  if (!raw) return fallback;
  const value = Number.parseInt(raw.slice(prefix.length), 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function parseStringFlag(args: string[], prefix: string): string | undefined {
  const raw = args.find((a) => a.startsWith(prefix));
  if (!raw) return undefined;
  const value = raw.slice(prefix.length).trim();
  return value || undefined;
}

export function parseDiscoverOptions(args: string[], defaultFetchLimit: number): DiscoverOptions {
  const enabled = parseBoolFlag(args, "--discover-projects");
  const last = parseIntFlag(args, "--discover-last=", Math.max(defaultFetchLimit, 50));
  const outPath = parseStringFlag(args, "--discover-output=");
  const rawSource = (parseStringFlag(args, "--discover-source=") || "local").toLowerCase();
  const source: DiscoverSource = rawSource === "imap" ? "imap" : "local";
  return { enabled, last, outPath, source };
}

function extractEmails(input?: string): string[] {
  if (!input) return [];
  const found = input.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g) || [];
  return [...new Set(found.map((e) => e.toLowerCase()))];
}

function firstDomain(emails: string[]): string | undefined {
  return emails[0]?.split("@")[1]?.toLowerCase();
}

function cleanSubject(subject?: string): string {
  if (!subject) return "";
  return subject
    .replace(/^(re|aw|wg|fwd)\s*:\s*/gi, "")
    .replace(/\[[^\]]+\]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function subjectTokens(subject?: string): string[] {
  return cleanSubject(subject)
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, " ")
    .split(/\s+/)
    .map((t) => t.trim())
    .filter((t) => t.length >= 4 && !SUBJECT_STOPWORDS.has(t));
}

function slugify(input: string): string {
  return input
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-")
    .slice(0, 48);
}

function looksLikeListMail(meta: ReturnType<typeof prepareMailText>["meta"]): boolean {
  const precedence = (meta.precedence || "").toLowerCase();
  const autoSubmitted = (meta.autoSubmitted || "").toLowerCase();
  return Boolean(meta.listId || precedence.includes("bulk") || precedence.includes("list") || autoSubmitted.startsWith("auto-"));
}

function projectHasContact(project: Project, email: string): boolean {
  return Boolean(project.contacts?.some((c) => (c.email || "").toLowerCase() === email.toLowerCase()));
}

function projectParticipantFromMessage(project: Project, emails: string[]): string[] {
  return [...new Set(emails.filter((e) => !projectHasContact(project, e)))];
}

function knownProjectDomains(projects: Project[]): Set<string> {
  const domains = new Set<string>();
  for (const p of projects) {
    for (const d of p.domains || []) {
      if (d) domains.add(d.toLowerCase());
    }
    for (const c of p.contacts || []) {
      if (c.email?.includes("@")) domains.add(c.email.split("@")[1].toLowerCase());
    }
  }
  return domains;
}

function listLocalEmlFiles(dataDir: string, limit: number): Array<{ id: string; rawLine: string; emlPath: string }> {
  const exportsDir = path.resolve(dataDir, "exports");
  if (!fs.existsSync(exportsDir)) return [];

  const stack = [exportsDir];
  const files: Array<{ path: string; mtimeMs: number }> = [];

  while (stack.length) {
    const dir = stack.pop();
    if (!dir || !fs.existsSync(dir)) continue;

    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
        continue;
      }
      if (entry.isFile() && entry.name.toLowerCase().endsWith(".eml")) {
        const stat = fs.statSync(full);
        files.push({ path: full, mtimeMs: stat.mtimeMs });
      }
    }
  }

  files.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return files.slice(0, limit).map((f) => ({
    id: path.basename(f.path, ".eml"),
    rawLine: `local:${f.path}`,
    emlPath: f.path,
  }));
}

export function runDiscoverProjects(cwd: string, cfg: ReturnType<typeof getConfig>, options: DiscoverOptions): void {
  const projectsPathAbs = path.resolve(cwd, cfg.PROJECTS_JSON_PATH);
  const hasProjects = fs.existsSync(projectsPathAbs);
  const projects = hasProjects ? loadProjects(cwd, cfg.PROJECTS_JSON_PATH) : [];
  const projectDomains = knownProjectDomains(projects);

  const envelopes =
    options.source === "local"
      ? listLocalEmlFiles(cfg.MAIL_PROCESSOR_DATA_DIR, options.last)
      : cfg.HIMALAYA_COMMAND === "mock"
        ? [{ id: "mock-1", rawLine: "mock-1 Example subject" }]
        : listEnvelopes(cfg.HIMALAYA_COMMAND, cfg.MAIL_SOURCE_FOLDER, options.last);

  const clusters = new Map<string, CandidateCluster>();
  const participantUpdates = new Map<string, Set<string>>();

  let inspected = 0;
  let skippedListLike = 0;

  for (const env of envelopes) {
    inspected += 1;

    const msg =
      options.source === "local" && "emlPath" in env
        ? {
            id: env.id,
            raw: fs.readFileSync(env.emlPath, "utf8"),
          }
        : cfg.HIMALAYA_COMMAND === "mock"
          ? {
              id: env.id,
              raw: "Subject: [EXAMPLE] Project kickoff\nFrom: alice@example.org\nTo: bob@example.org\nBody: We should plan this project.",
            }
          : readMessage(cfg.HIMALAYA_COMMAND, cfg.MAIL_SOURCE_FOLDER, env.id, cfg.MAIL_PROCESSOR_DATA_DIR);

    const prepared = prepareMailText(
      msg.raw,
      cfg.MAIL_HTML_MAX_CURRENT,
      cfg.MAIL_HTML_MAX_QUOTED,
      {
        enabled: cfg.MAIL_SANITIZE_ENABLED,
        mode: cfg.MAIL_SANITIZE_MODE,
        stripTrackingParams: cfg.MAIL_STRIP_TRACKING_PARAMS,
        trimNewsletterFooter: cfg.MAIL_NEWSLETTER_FOOTER_TRIM,
      },
    );

    if (looksLikeListMail(prepared.meta)) {
      skippedListLike += 1;
      continue;
    }

    const emails = [
      ...extractEmails(prepared.meta.from),
      ...extractEmails(prepared.meta.replyTo),
      ...extractEmails(prepared.meta.returnPath),
      ...extractEmails(prepared.currentMessage.slice(0, 1500)),
    ];
    const uniqEmails = [...new Set(emails)];

    if (projects.length > 0) {
      const m = matchProject(prepared.effectiveText, projects);
      if (m.projectId && m.score >= Math.max(cfg.PROJECT_MATCH_THRESHOLD, 0.75)) {
        const project = projects.find((p) => p.id === m.projectId);
        if (project) {
          const missing = projectParticipantFromMessage(project, uniqEmails);
          if (missing.length) {
            const set = participantUpdates.get(project.id) || new Set<string>();
            for (const e of missing) set.add(e);
            participantUpdates.set(project.id, set);
          }
        }
        continue;
      }
    }

    const domain = firstDomain(uniqEmails);
    const tokens = subjectTokens(prepared.meta.subject);
    const tokenKey = tokens.slice(0, 3).join("-");
    const key = `${domain || "unknown"}|${tokenKey || "no-subject-signal"}`;

    const titleSeed = cleanSubject(prepared.meta.subject) || tokenKey || domain || "Untitled Project Candidate";
    const idSeed = slugify(`${tokenKey || "project"}-${domain || "mail"}`) || `project-${env.id}`;

    const c = clusters.get(key) || {
      key,
      idSeed,
      titleSeed,
      domain: domain || "",
      messageCount: 0,
      participants: new Set<string>(),
      samples: [],
      lastSeen: undefined,
    };

    c.messageCount += 1;
    for (const e of uniqEmails) c.participants.add(e);
    if (prepared.meta.subject && c.samples.length < 4) c.samples.push(prepared.meta.subject);
    if (prepared.meta.date) c.lastSeen = prepared.meta.date;
    clusters.set(key, c);
  }

  const newProjectCandidates = [...clusters.values()]
    .filter((c) => c.messageCount >= 2 || c.participants.size >= 3)
    .filter((c) => !projectDomains.has(c.domain))
    .sort((a, b) => b.messageCount - a.messageCount)
    .map((c) => ({
      id: c.idSeed,
      title: c.titleSeed,
      mailbox_folder: "Archive",
      domains: c.domain ? [c.domain] : [],
      contacts: [...c.participants].slice(0, 10).map((email) => ({ email })),
      candidate_score: Number((Math.min(1, c.messageCount / 5) * 0.7 + Math.min(1, c.participants.size / 5) * 0.3).toFixed(3)),
      evidence: {
        message_count: c.messageCount,
        participant_count: c.participants.size,
        sample_subjects: c.samples,
        last_seen: c.lastSeen || null,
      },
    }));

  const projectParticipantSuggestions = [...participantUpdates.entries()].map(([projectId, emails]) => ({
    project_id: projectId,
    contacts_to_add: [...emails].slice(0, 20).map((email) => ({ email })),
  }));

  const out = {
    generated_at: new Date().toISOString(),
    source_mode: options.source,
    source_folder: cfg.MAIL_SOURCE_FOLDER,
    inspected,
    skipped_list_like: skippedListLike,
    existing_projects: projects.length,
    new_project_candidates: newProjectCandidates,
    project_participant_suggestions: projectParticipantSuggestions,
    note: "Review required before merging into projects.json",
  };

  const ts = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  const defaultOut = path.join("memory", "references", "projects", "inbox", `project-candidates-${ts}.json`);
  const outputPath = path.resolve(cwd, options.outPath || defaultOut);
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, JSON.stringify(out, null, 2), "utf8");

  console.log(JSON.stringify({ ok: true, mode: "discover-projects", source: options.source, outputPath, summary: {
    inspected,
    skippedListLike,
    newCandidates: newProjectCandidates.length,
    participantSuggestions: projectParticipantSuggestions.length,
  } }, null, 2));
}
