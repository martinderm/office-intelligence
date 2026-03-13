import fs from "node:fs";
import path from "node:path";
import { listEnvelopes, readMessage } from "./mail-source.js";
import { prepareMailText } from "./preprocess.js";
import { getConfig } from "./env.js";
import { loadProjects } from "./projects.js";
import { Project } from "./types.js";
import { extractDiscoveryWithLlm } from "./llm.js";

type DiscoverSource = "local" | "imap";

type DiscoverOptions = {
  enabled: boolean;
  last: number;
  outPath?: string;
  source: DiscoverSource;
};

type CandidateCluster = {
  key: string;
  projectName: string;
  projectTitle: string;
  messageCount: number;
  participants: Set<string>;
  topics: Map<string, number>;
  samples: string[];
  lastSeen?: string;
  confidenceSum: number;
};

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

function resolveExistingProjectByLlmName(projects: Project[], projectName: string, projectTitle: string): Project | undefined {
  const name = (projectName || "").trim().toLowerCase();
  const title = (projectTitle || "").trim().toLowerCase();
  if (!name && !title) return undefined;

  return projects.find((p) => {
    const id = (p.id || "").trim().toLowerCase();
    const pTitle = (p.title || "").trim().toLowerCase();
    const aliases = (p.aliases || []).map((a) => a.trim().toLowerCase());
    return id === name || pTitle === title || aliases.includes(name) || aliases.includes(title);
  });
}

function buildNewProjectCandidates(clusters: Map<string, CandidateCluster>) {
  return [...clusters.values()]
    .sort((a, b) => b.messageCount - a.messageCount)
    .map((c) => {
      const topics = [...c.topics.entries()]
        .sort((a, b) => b[1] - a[1])
        .slice(0, 3)
        .map(([topic]) => topic);

      return {
        id: c.projectName,
        title: c.projectTitle,
        mailbox_folder: "Archive",
        domains: [],
        contacts: [...c.participants].slice(0, 10).map((email) => ({ email })),
        topics,
        candidate_score: Number((c.confidenceSum / Math.max(1, c.messageCount)).toFixed(3)),
        evidence: {
          message_count: c.messageCount,
          participant_count: c.participants.size,
          sample_subjects: c.samples,
          last_seen: c.lastSeen || null,
        },
      };
    });
}

function buildProjectParticipantSuggestions(participantUpdates: Map<string, Set<string>>) {
  return [...participantUpdates.entries()].map(([projectId, emails]) => ({
    project_id: projectId,
    contacts_to_add: [...emails].slice(0, 20).map((email) => ({ email })),
  }));
}

export async function runDiscoverProjects(cwd: string, cfg: ReturnType<typeof getConfig>, options: DiscoverOptions): Promise<void> {
  if (!cfg.LLM_BASE_URL || !cfg.LLM_API_KEY || !cfg.LLM_MODEL) {
    throw new Error("Discovery requires LLM configuration: LLM_BASE_URL, LLM_API_KEY, LLM_MODEL");
  }

  const projectsPathAbs = path.resolve(cwd, cfg.PROJECTS_JSON_PATH);
  const hasProjects = fs.existsSync(projectsPathAbs);
  const projects = hasProjects ? loadProjects(cwd, cfg.PROJECTS_JSON_PATH) : [];

  const envelopes =
    options.source === "local"
      ? listLocalEmlFiles(cfg.MAIL_PROCESSOR_DATA_DIR, options.last)
      : cfg.HIMALAYA_COMMAND === "mock"
        ? [{ id: "mock-1", rawLine: "mock-1 Example subject" }]
        : listEnvelopes(cfg.HIMALAYA_COMMAND, cfg.MAIL_SOURCE_FOLDER, options.last);

  const clusters = new Map<string, CandidateCluster>();
  const participantUpdates = new Map<string, Set<string>>();
  const perMessageExtractions: Array<{
    envelope_id: string;
    project_name: string;
    project_title: string;
    topics: string[];
    confidence: number;
    llm_error?: string;
  }> = [];

  const ts = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
  const defaultOut = path.join("memory", "references", "projects", "inbox", `project-candidates-${ts}.json`);
  const outputPath = path.resolve(cwd, options.outPath || defaultOut);
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });

  const writeSnapshot = () => {
    const out = {
      generated_at: new Date().toISOString(),
      source_mode: options.source,
      source_folder: cfg.MAIL_SOURCE_FOLDER,
      inspected,
      skipped_list_like: skippedListLike,
      existing_projects: projects.length,
      new_project_candidates: buildNewProjectCandidates(clusters),
      project_participant_suggestions: buildProjectParticipantSuggestions(participantUpdates),
      per_message_extractions: perMessageExtractions,
      note: "Review required before merging into projects.json",
    };
    fs.writeFileSync(outputPath, JSON.stringify(out, null, 2), "utf8");
  };

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
      writeSnapshot();
      continue;
    }

    const emails = [
      ...extractEmails(prepared.meta.from),
      ...extractEmails(prepared.meta.replyTo),
      ...extractEmails(prepared.meta.returnPath),
      ...extractEmails(prepared.currentMessage.slice(0, 1500)),
    ];
    const uniqEmails = [...new Set(emails)];

    let extracted;
    try {
      extracted = await extractDiscoveryWithLlm({
        baseUrl: cfg.LLM_BASE_URL,
        apiKey: cfg.LLM_API_KEY,
        model: cfg.LLM_MODEL,
        mailText: prepared.effectiveText,
        timeoutMs: cfg.LLM_TIMEOUT_MS,
      });
    } catch (error) {
      const llmError = error instanceof Error ? error.message : String(error);
      perMessageExtractions.push({
        envelope_id: env.id,
        project_name: "unknown",
        project_title: "Unknown",
        topics: [],
        confidence: 0,
        llm_error: llmError,
      });
      writeSnapshot();
      continue;
    }

    perMessageExtractions.push({
      envelope_id: env.id,
      project_name: extracted.project_name,
      project_title: extracted.project_title,
      topics: extracted.topics,
      confidence: extracted.confidence,
    });

    const existing = resolveExistingProjectByLlmName(projects, extracted.project_name, extracted.project_title);
    if (existing) {
      const missing = projectParticipantFromMessage(existing, uniqEmails);
      if (missing.length) {
        const set = participantUpdates.get(existing.id) || new Set<string>();
        for (const e of missing) set.add(e);
        participantUpdates.set(existing.id, set);
      }
      writeSnapshot();
      continue;
    }

    const key = extracted.project_name || "unknown";
    const c = clusters.get(key) || {
      key,
      projectName: extracted.project_name || "unknown",
      projectTitle: extracted.project_title || "Unknown",
      messageCount: 0,
      participants: new Set<string>(),
      topics: new Map<string, number>(),
      samples: [],
      lastSeen: undefined,
      confidenceSum: 0,
    };

    c.messageCount += 1;
    c.confidenceSum += Number(extracted.confidence) || 0;

    for (const e of uniqEmails) c.participants.add(e);
    for (const t of extracted.topics || []) {
      c.topics.set(t, (c.topics.get(t) || 0) + 1);
    }
    if (prepared.meta.subject && c.samples.length < 4) c.samples.push(prepared.meta.subject);
    if (prepared.meta.date) c.lastSeen = prepared.meta.date;

    clusters.set(key, c);
    writeSnapshot();
  }

  if (!fs.existsSync(outputPath)) {
    writeSnapshot();
  }

  const newProjectCandidates = buildNewProjectCandidates(clusters);
  const projectParticipantSuggestions = buildProjectParticipantSuggestions(participantUpdates);

  console.log(JSON.stringify({
    ok: true,
    mode: "discover-projects",
    source: options.source,
    outputPath,
    summary: {
      inspected,
      skippedListLike,
      newCandidates: newProjectCandidates.length,
      participantSuggestions: projectParticipantSuggestions.length,
    },
  }, null, 2));
}
