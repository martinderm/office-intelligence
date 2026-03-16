#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const cwd = process.cwd();
const args = process.argv.slice(2);

function arg(name, fallback = "") {
  const p = `--${name}=`;
  const found = args.find((a) => a.startsWith(p));
  return found ? found.slice(p.length) : fallback;
}

function has(flag) {
  return args.includes(`--${flag}`);
}

const dryRun = has("dry-run");
const projectsPath = path.resolve(cwd, process.env.PROJECTS_JSON_PATH || "./memory/references/projects/projects.json");
const inboxDir = path.resolve(cwd, "memory/references/projects/inbox");
const changelogPath = path.resolve(cwd, "memory/references/projects/changelog.md");

function latestInboxFile() {
  if (!fs.existsSync(inboxDir)) return undefined;
  const files = fs
    .readdirSync(inboxDir)
    .filter((f) => f.endsWith(".json"))
    .map((f) => ({ f, t: fs.statSync(path.join(inboxDir, f)).mtimeMs }))
    .sort((a, b) => b.t - a.t);
  return files[0]?.f ? path.join(inboxDir, files[0].f) : undefined;
}

const inputPath = path.resolve(cwd, arg("input", latestInboxFile() || ""));
if (!inputPath || !fs.existsSync(inputPath)) {
  console.error("No suggestion file found. Use --input=<path> or run discover first.");
  process.exit(1);
}

const projects = JSON.parse(fs.readFileSync(projectsPath, "utf8"));
if (!Array.isArray(projects)) {
  console.error("projects.json must be an array");
  process.exit(1);
}

const suggestions = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const newCandidates = suggestions.new_project_candidates || [];
const participantSuggestions = suggestions.project_participant_suggestions || [];

const byId = new Map(projects.map((p) => [p.id, p]));
const touched = new Set();
let created = 0;
let contactsAdded = 0;
let domainsAdded = 0;

for (const c of newCandidates) {
  if (!c?.id || !c?.title) continue;
  if (byId.has(c.id)) continue;

  const project = {
    id: c.id,
    title: c.title,
    mailbox_folder: c.mailbox_folder || "Archive",
    reference_md: `memory/references/projects/${c.id}/index.md`,
    aliases: [],
    keywords: [],
    domains: Array.isArray(c.domains) ? c.domains.slice(0, 5) : [],
    contacts: Array.isArray(c.contacts) ? c.contacts.slice(0, 10) : [],
    description: "Auto-created from reviewed mail discovery suggestions.",
    typical_subject_patterns: [],
    routing_priority: 10,
    do_not_route_if: ["newsletter", "no-reply", "autoreply"],
    updated_at: new Date().toISOString().slice(0, 10),
    schema_version: 1,
  };

  projects.push(project);
  byId.set(project.id, project);
  touched.add(project.id);
  created += 1;
}

for (const s of participantSuggestions) {
  const project = byId.get(s?.project_id);
  if (!project) continue;

  project.contacts = Array.isArray(project.contacts) ? project.contacts : [];
  const known = new Set(project.contacts.map((c) => (c.email || "").toLowerCase()).filter(Boolean));

  for (const c of s.contacts_to_add || []) {
    const email = (c?.email || "").toLowerCase().trim();
    if (!email || known.has(email)) continue;
    project.contacts.push({ email });
    known.add(email);
    contactsAdded += 1;
    touched.add(project.id);
  }
}

for (const c of newCandidates) {
  const project = byId.get(c?.id);
  if (!project || !Array.isArray(c?.domains)) continue;
  project.domains = Array.isArray(project.domains) ? project.domains : [];
  const known = new Set(project.domains.map((d) => d.toLowerCase()));
  for (const d of c.domains) {
    const dn = String(d || "").toLowerCase().trim();
    if (!dn || known.has(dn)) continue;
    project.domains.push(dn);
    known.add(dn);
    domainsAdded += 1;
    touched.add(project.id);
  }
}

projects.sort((a, b) => a.id.localeCompare(b.id));

function ensureProjectDocs(project) {
  const baseRel = `memory/references/projects/${project.id}`;
  const baseAbs = path.resolve(cwd, baseRel);
  const indexAbs = path.resolve(baseAbs, "index.md");
  const signalsAbs = path.resolve(baseAbs, "signals.md");
  const evidenceDirAbs = path.resolve(baseAbs, "evidence");
  const workpackagesDirAbs = path.resolve(baseAbs, "workpackages");

  fs.mkdirSync(baseAbs, { recursive: true });
  fs.mkdirSync(evidenceDirAbs, { recursive: true });
  fs.mkdirSync(workpackagesDirAbs, { recursive: true });

  if (!fs.existsSync(indexAbs)) {
    const indexContent = `# ${project.id} — ${project.title}\n\nKurzbeschreibung: auto-created from mail discovery suggestions.\n\n## Überblick\n\n- Status: aktiv\n- Owner: n/a\n- Mailbox-Ordner: ${project.mailbox_folder || "Archive"}\n- Letzte Aktivität: ${project.updated_at || new Date().toISOString().slice(0, 10)}\n- Aktualisiert am: ${project.updated_at || new Date().toISOString().slice(0, 10)}\n\n## Aktuelle Lage\n\n<!-- BEGIN:managed-summary -->\n- Zusammenfassung folgt nach den ersten klassifizierten Mails.\n<!-- END:managed-summary -->\n\n## Referenzen\n\n- Signale: ./signals.md\n- Evidenz-Log: ./evidence/\n- Workpackages: ./workpackages/\n`;
    fs.writeFileSync(indexAbs, indexContent, "utf8");
  }

  if (!fs.existsSync(signalsAbs)) {
    const signalsContent = `# Signals — ${project.id}\n\n## Routing-Signale\n\n- Primäre Domains:\n${(project.domains || []).map((d) => `  - ${d}`).join("\n") || "  - "}\n- Schlüssel-Kontakte (Name <mail>):\n${(project.contacts || []).map((c) => `  - ${c.name ? `${c.name} <${c.email || ""}>` : c.email || ""}`).join("\n") || "  - "}\n- Typische Betreffmuster:\n  - \n- Typische Begriffe / Abkürzungen:\n  - \n\n## Do-not-route / Ausschlüsse\n\n- newsletter\n- no-reply\n- autoreply\n\n## Konsolidierte Signale (Managed)\n\n<!-- BEGIN:managed-signals -->\n- Letzter relevanter Mailkontakt: \n- Relevante Teilnehmer:innen:\n  - \n- Häufige Themen/Cluster:\n  - \n- Aktuelle nächste Schritte:\n  - \n- Risiken/Blocker:\n  - \n<!-- END:managed-signals -->\n`;
    fs.writeFileSync(signalsAbs, signalsContent, "utf8");
  }

  const readmeKeepAbs = path.resolve(evidenceDirAbs, ".gitkeep");
  if (!fs.existsSync(readmeKeepAbs)) fs.writeFileSync(readmeKeepAbs, "\n", "utf8");
  const workpackagesKeepAbs = path.resolve(workpackagesDirAbs, ".gitkeep");
  if (!fs.existsSync(workpackagesKeepAbs)) fs.writeFileSync(workpackagesKeepAbs, "\n", "utf8");
}

if (!dryRun) {
  fs.writeFileSync(projectsPath, JSON.stringify(projects, null, 2), "utf8");
  for (const id of touched) ensureProjectDocs(byId.get(id));

  fs.mkdirSync(path.dirname(changelogPath), { recursive: true });
  if (!fs.existsSync(changelogPath)) {
    fs.writeFileSync(changelogPath, "# Project Catalog Changelog\n\n", "utf8");
  }
  const line = `- ${new Date().toISOString()} | source: ${path.relative(cwd, inputPath)} | created: ${created} | contacts_added: ${contactsAdded} | domains_added: ${domainsAdded}\n`;
  fs.appendFileSync(changelogPath, line, "utf8");
}

console.log(JSON.stringify({
  ok: true,
  dryRun,
  input: path.relative(cwd, inputPath),
  projectsPath: path.relative(cwd, projectsPath),
  created,
  contactsAdded,
  domainsAdded,
  touched: [...touched],
}, null, 2));
