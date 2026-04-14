import type { Project, Topic } from "../types.js";
import type { LlmExtraction } from "../llm.js";
import type { MatchResult } from "../matcher.js";

function norm(s: string): string {
  return s.toLowerCase();
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

export function mergeHeuristicAndLegacyLlm(
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
