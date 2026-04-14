import { Project, Topic } from "./types.js";

export type MatchResult = {
  projectId?: string;
  score: number;
  reason: string;
  matchedTopicId?: string;
  topicScore?: number;
  topicReason?: string;
  matchedWorkpackageId?: string;
  workpackageScore?: number;
  workpackageReason?: string;
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

export function matchProject(textRaw: string, projects: Project[], topics: Topic[] = []): MatchResult {
  const text = norm(textRaw);
  let best = { projectId: undefined as string | undefined, score: 0, reason: "no match", project: undefined as Project | undefined };

  for (const p of projects) {
    let score = 0;
    score += includesAny(text, [p.title]) * 0.25;
    score += includesAny(text, p.aliases) * 0.2;
    score += includesAny(text, p.keywords) * 0.1;
    score += includesAny(text, p.domains) * 0.35;
    score += includesAny(text, p.typical_subject_patterns) * 0.2;
    if (p.contacts?.some((c) => c.email && text.includes(norm(c.email)))) score += 0.35;

    if (score > best.score) {
      best = { projectId: p.id, score, reason: `matched ${p.id}`, project: p };
    }
  }

  let matchedTopicId: string | undefined;
  let topicScore = 0;
  let topicReason = "no topic match";

  for (const t of topics) {
    let score = 0;
    score += includesAny(text, [t.title]) * 0.25;
    score += includesAny(text, t.aliases) * 0.2;
    score += includesAny(text, t.keywords) * 0.1;
    score += includesAny(text, t.domains) * 0.35;
    score += includesAny(text, t.typical_subject_patterns) * 0.2;
    if (t.contacts?.some((c) => c.email && text.includes(norm(c.email)))) score += 0.35;

    if (score > topicScore) {
      matchedTopicId = t.id;
      topicScore = score;
      topicReason = `matched topic ${t.id}`;
    }
  }

  let matchedWorkpackageId: string | undefined;
  let workpackageScore = 0;
  let workpackageReason = "no workpackage match";

  if (best.project && best.project.workpackages?.length) {
    for (const wp of best.project.workpackages) {
      let score = 0;
      score += includesAny(text, [wp.title]) * 0.3;
      score += includesAny(text, wp.aliases) * 0.25;
      score += includesAny(text, wp.keywords) * 0.2;
      if (wp.contacts?.some((c) => c.email && text.includes(norm(c.email)))) score += 0.35;

      if (score > workpackageScore) {
        matchedWorkpackageId = wp.id;
        workpackageScore = score;
        workpackageReason = `matched workpackage ${wp.id}`;
      }
    }
  }

  return {
    projectId: best.score > 0 ? best.projectId : undefined,
    score: Number(best.score.toFixed(3)),
    reason: best.reason,
    matchedTopicId: topicScore > 0 ? matchedTopicId : undefined,
    topicScore: Number(topicScore.toFixed(3)),
    topicReason,
    matchedWorkpackageId: workpackageScore > 0 ? matchedWorkpackageId : undefined,
    workpackageScore: Number(workpackageScore.toFixed(3)),
    workpackageReason,
    needsReply: false,
  };
}

