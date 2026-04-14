import type { ClassificationResult, RoutingDecisionState } from "./contracts.js";

export type FusionResult = {
  state: RoutingDecisionState;
  projectId?: string;
  topicId?: string;
  workpackageId?: string;
  score: number;
  reason: string;
  needsReply: boolean;
};

export function fuseClassificationResult(params: {
  result: ClassificationResult | undefined;
  projectThreshold: number;
}): FusionResult {
  const result = params.result;

  if (!result) {
    return {
      state: "keep_in_inbox",
      score: 0,
      reason: "no classification result",
      needsReply: false,
    };
  }

  const topProject = result.projectCandidates[0];
  const secondProject = result.projectCandidates[1];
  const topTopic = result.topicCandidates[0];
  const topWorkpackage = result.workpackageCandidates[0];

  if (!topProject) {
    return {
      state: topTopic ? "review" : "keep_in_inbox",
      projectId: undefined,
      topicId: topTopic?.id,
      workpackageId: undefined,
      score: Number(topTopic?.confidence || 0),
      reason: topTopic ? "topic signal without project" : "no project candidate",
      needsReply: result.needsReply,
    };
  }

  const topScore = Number(topProject.confidence || 0);
  const secondScore = Number(secondProject?.confidence || 0);
  const delta = topScore - secondScore;
  const topicScore = Number(topTopic?.confidence || 0);

  if (topScore < params.projectThreshold) {
    return {
      state: topScore >= 0.4 ? "review" : "keep_in_inbox",
      projectId: topProject.id,
      topicId: topTopic?.id,
      workpackageId: undefined,
      score: topScore,
      reason: "project confidence below routing threshold",
      needsReply: result.needsReply,
    };
  }

  if (topicScore > topScore) {
    return {
      state: "review",
      projectId: topProject.id,
      topicId: topTopic?.id,
      workpackageId: undefined,
      score: topScore,
      reason: "topic stronger than project",
      needsReply: result.needsReply,
    };
  }

  if (delta < 0.15) {
    return {
      state: "review",
      projectId: topProject.id,
      topicId: topTopic?.id,
      workpackageId: undefined,
      score: topScore,
      reason: "ambiguous project overlap",
      needsReply: result.needsReply,
    };
  }

  return {
    state: "shadow_only",
    projectId: topProject.id,
    topicId: topTopic?.id,
    workpackageId: topWorkpackage?.project_id === topProject.id ? topWorkpackage.id : undefined,
    score: topScore,
    reason: "strong project candidate",
    needsReply: result.needsReply,
  };
}
