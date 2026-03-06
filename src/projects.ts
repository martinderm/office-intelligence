import fs from "node:fs";
import path from "node:path";
import { Project } from "./types.js";

function isSlug(value: string): boolean {
  return /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(value);
}

function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object" && Array.isArray((value as any).projects)) {
    return (value as any).projects;
  }
  throw new Error("projects.json must be an array or { projects: [...] }");
}

export function loadProjects(cwd: string, projectPathRaw: string): Project[] {
  const projectPath = path.resolve(cwd, projectPathRaw);
  if (!fs.existsSync(projectPath)) {
    throw new Error(`projects.json not found: ${projectPath}`);
  }

  const raw = fs.readFileSync(projectPath, "utf8");
  const parsed = JSON.parse(raw);
  const items = asArray(parsed);

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
      const refPath = path.resolve(cwd, p.reference_md);
      if (!fs.existsSync(refPath)) {
        console.warn(`[warn] reference_md missing for ${p.id}: ${p.reference_md}`);
      }
    }

    return p;
  });

  return projects;
}
