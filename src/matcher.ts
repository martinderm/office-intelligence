import { Project } from "./types.js";

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
