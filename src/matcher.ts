import { Project, Topic } from "./types.js";
import { LlmExtraction } from "./llm.js";

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

function resolveProjectByLabel(label: string, projects: Project[]): string | undefined {
  const l = norm(label);
  for (const p of projects) {
    if (norm(p.id) === l || norm(p.title) === l) return p.id;
    if (p.aliases?.some((a) => norm(a) === l)) return p.id;
  }
  return undefined;
}

function resolveTopicByLabel(label: string, topics: Topic[]): string | undefined {
  const l = norm(label);
  for (const t of topics) {
    if (norm(t.id) === l || norm(t.title) === l) return t.id;
    if (t.aliases?.some((a) => norm(a) === l)) return t.id;
  }
  return undefined;
}

function resolveWorkpackageByLabel(label: string, project: Project): string | undefined {
  const l = norm(label);
  for (const wp of project.workpackages || []) {
    if (norm(wp.id) === l || norm(wp.title) === l) return wp.id;
    if (wp.aliases?.some((a) => norm(a) === l)) return wp.id;
  }
  return undefined;
}

export function mergeHeuristicAndLlm(
  heuristic: MatchResult,
  llm: LlmExtraction | undefined,
  projects: Project[],
  topics: Topic[] = [],
): MatchResult {
  if (!llm || llm.projectCandidates.length === 0) return heuristic;

  const top = llm.projectCandidates
    .map((c) => ({ projectId: resolveProjectByLabel(c.label, projects), confidence: Number(c.confidence) || 0 }))
    .filter((c) => !!c.projectId)
    .sort((a, b) => b.confidence - a.confidence)[0];

  if (!top?.projectId) return heuristic;

  const blended = Math.max(heuristic.score * 0.45 + top.confidence * 0.65, heuristic.score);

  let matchedTopicId = heuristic.matchedTopicId;
  let topicScore = heuristic.topicScore || 0;
  let topicReason = heuristic.topicReason || "no topic match";

  if (llm.topicCandidates?.length) {
    const topicTop = llm.topicCandidates
      .map((c) => ({ id: resolveTopicByLabel(c.label, topics), confidence: Number(c.confidence) || 0 }))
      .filter((c) => !!c.id)
      .sort((a, b) => b.confidence - a.confidence)[0];

    if (topicTop?.id) {
      const blendedTopic = Math.max(topicScore * 0.45 + topicTop.confidence * 0.65, topicScore);
      matchedTopicId = topicTop.id;
      topicScore = Number(blendedTopic.toFixed(3));
      topicReason = `llm+heuristic topic ${topicTop.id}`;
    }
  }

  let matchedWorkpackageId = heuristic.matchedWorkpackageId;
  let workpackageScore = heuristic.workpackageScore || 0;
  let workpackageReason = heuristic.workpackageReason || "no workpackage match";

  const selectedProject = projects.find((p) => p.id === top.projectId);
  if (selectedProject && llm.workpackageCandidates?.length) {
    const wpTop = llm.workpackageCandidates
      .map((c) => ({ id: resolveWorkpackageByLabel(c.label, selectedProject), confidence: Number(c.confidence) || 0 }))
      .filter((c) => !!c.id)
      .sort((a, b) => b.confidence - a.confidence)[0];

    if (wpTop?.id) {
      const blendedWp = Math.max(workpackageScore * 0.45 + wpTop.confidence * 0.65, workpackageScore);
      matchedWorkpackageId = wpTop.id;
      workpackageScore = Number(blendedWp.toFixed(3));
      workpackageReason = `llm+heuristic workpackage ${wpTop.id}`;
    }
  }

  return {
    projectId: top.projectId,
    score: Number(blended.toFixed(3)),
    reason: `llm+heuristic ${top.projectId}`,
    matchedTopicId,
    topicScore,
    topicReason,
    matchedWorkpackageId,
    workpackageScore,
    workpackageReason,
    needsReply: heuristic.needsReply,
  };
}
