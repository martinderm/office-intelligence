import fs from "node:fs";
import path from "node:path";
import { Project, Topic } from "./types.js";

function isSlug(value: string): boolean {
  return /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(value);
}

function parseCatalog(value: unknown): { projects: unknown[]; topics: unknown[] } {
  if (Array.isArray(value)) {
    return { projects: value, topics: [] };
  }
  if (value && typeof value === "object") {
    const obj = value as any;
    return {
      projects: Array.isArray(obj.projects) ? obj.projects : [],
      topics: Array.isArray(obj.topics) ? obj.topics : [],
    };
  }
  throw new Error("projects.json must be an array or { projects: [...], topics?: [...] }");
}

function loadCatalogRaw(cwd: string, projectPathRaw: string): { projects: unknown[]; topics: unknown[] } {
  const projectPath = path.resolve(cwd, projectPathRaw);
  if (!fs.existsSync(projectPath)) {
    throw new Error(`projects.json not found: ${projectPath}`);
  }

  const raw = fs.readFileSync(projectPath, "utf8");
  const parsed = JSON.parse(raw);
  return parseCatalog(parsed);
}

function resolveReferencePath(cwd: string, referencePathRaw: string): string {
  const direct = path.resolve(cwd, referencePathRaw);
  if (fs.existsSync(direct)) return direct;

  const workspaceRoot = process.env.AGENT_WORKSPACE_ROOT;
  if (workspaceRoot) {
    const fromWorkspace = path.resolve(workspaceRoot, referencePathRaw);
    if (fs.existsSync(fromWorkspace)) return fromWorkspace;
  }

  return direct;
}

export function loadProjects(cwd: string, projectPathRaw: string): Project[] {
  const { projects: items } = loadCatalogRaw(cwd, projectPathRaw);

  const ids = new Set<string>();
  const projects: Project[] = items.map((item, idx) => {
    if (!item || typeof item !== "object") {
      throw new Error(`projects[${idx}] must be an object`);
    }
    const p = item as Project;
    if (!p.id || !p.title || !p.mailbox_folder) {
      throw new Error(`projects[${idx}] requires id, title, mailbox_folder`);
    }
    if (!isSlug(p.id)) {
      throw new Error(`projects[${idx}].id must be slug-like (a-z0-9-)`);
    }
    if (ids.has(p.id)) {
      throw new Error(`duplicate project id: ${p.id}`);
    }
    ids.add(p.id);

    if (p.reference_md) {
      const refPath = resolveReferencePath(cwd, p.reference_md);
      if (!fs.existsSync(refPath)) {
        console.warn(`[warn] reference_md missing for ${p.id}: ${p.reference_md}`);
      }
    }

    return p;
  });

  return projects;
}

export function loadTopics(cwd: string, topicsPathRaw: string): Topic[] {
  const topicsPath = path.resolve(cwd, topicsPathRaw);
  if (!fs.existsSync(topicsPath)) {
    throw new Error(`topics.json not found: ${topicsPath}`);
  }

  const raw = fs.readFileSync(topicsPath, "utf8");
  const parsed = JSON.parse(raw);
  const items = Array.isArray(parsed)
    ? parsed
    : (parsed && typeof parsed === "object" && Array.isArray((parsed as any).topics)
      ? (parsed as any).topics
      : []);

  const ids = new Set<string>();
  const topics: Topic[] = (items as unknown[]).map((item: unknown, idx: number) => {
    if (!item || typeof item !== "object") {
      throw new Error(`topics[${idx}] must be an object`);
    }

    const t = item as Topic;
    if (!t.id || !t.title || !t.mailbox_folder) {
      throw new Error(`topics[${idx}] requires id, title, mailbox_folder`);
    }
    if (!isSlug(t.id)) {
      throw new Error(`topics[${idx}].id must be slug-like (a-z0-9-)`);
    }
    if (ids.has(t.id)) {
      throw new Error(`duplicate topic id: ${t.id}`);
    }
    ids.add(t.id);

    return t;
  });

  return topics;
}
