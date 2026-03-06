import { Project } from "./types.js";
import { LlmExtraction } from "./llm.js";

export type MatchResult = {
  projectId?: string;
  score: number;
  reason: string;
  needsReply: boolean;
};

function norm(s: string): string {
  return s.toLowerCase();
}

function includesAny(text: string, arr?: string[]): number {
  if (!arr || arr.length === 0) return 0;
  let score = 0;
  for (const x of arr) {
    if (x && text.includes(norm(x))) score += 1;
  }
  return score;
}

export function needsReplyHeuristic(text: string, negatives: string[]): boolean {
  const t = norm(text);
  if (negatives.some((n) => n && t.includes(norm(n)))) return false;
  return t.includes("?") || t.includes("deadline") || t.includes("bitte") || t.includes("kannst du");
}

export function matchProject(textRaw: string, projects: Project[]): MatchResult {
  const text = norm(textRaw);
  let best = { projectId: undefined as string | undefined, score: 0, reason: "no match" };

  for (const p of projects) {
    let score = 0;
    score += includesAny(text, [p.title]) * 0.25;
    score += includesAny(text, p.aliases) * 0.2;
    score += includesAny(text, p.keywords) * 0.1;
    score += includesAny(text, p.domains) * 0.35;
    score += includesAny(text, p.typical_subject_patterns) * 0.2;
    if (p.contacts?.some((c) => c.email && text.includes(norm(c.email)))) score += 0.35;

    if (score > best.score) {
      best = { projectId: p.id, score, reason: `matched ${p.id}` };
    }
  }

  return {
    projectId: best.score > 0 ? best.projectId : undefined,
    score: Number(best.score.toFixed(3)),
    reason: best.reason,
    needsReply: false,
  };
}

function resolveProjectByLabel(label: string, projects: Project[]): string | undefined {
  const l = norm(label);
  for (const p of projects) {
    if (norm(p.id) === l || norm(p.title) === l) return p.id;
    if (p.aliases?.some((a) => norm(a) === l)) return p.id;
  }
  return undefined;
}

export function mergeHeuristicAndLlm(
  heuristic: MatchResult,
  llm: LlmExtraction | undefined,
  projects: Project[],
): MatchResult {
  if (!llm || llm.projectCandidates.length === 0) return heuristic;

  const top = llm.projectCandidates
    .map((c) => ({ projectId: resolveProjectByLabel(c.label, projects), confidence: Number(c.confidence) || 0 }))
    .filter((c) => !!c.projectId)
    .sort((a, b) => b.confidence - a.confidence)[0];

  if (!top?.projectId) return heuristic;

  const blended = Math.max(heuristic.score * 0.45 + top.confidence * 0.65, heuristic.score);
  return {
    projectId: top.projectId,
    score: Number(blended.toFixed(3)),
    reason: `llm+heuristic ${top.projectId}`,
    needsReply: heuristic.needsReply,
  };
}
