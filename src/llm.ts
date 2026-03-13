import fs from "node:fs";
import path from "node:path";

export type LlmExtraction = {
  projectCandidates: Array<{ label: string; confidence: number; evidence?: string[] }>;
  workpackageCandidates?: Array<{ label: string; confidence: number; evidence?: string[] }>;
  needsReply: { score: number; reasons?: string[] };
  keywords?: string[];
  entities?: string[];
  notes?: string;
};

export type DiscoveryLlmExtraction = {
  project_name: string;
  project_title: string;
  topics: string[];
  confidence: number;
};

const DEFAULT_PROMPT = `You classify emails for project routing.
Return STRICT JSON only.
Focus primarily on [CURRENT_MESSAGE]. Treat [OLDER_CONTEXT_LOWER_WEIGHT] as weaker evidence.

Output schema:
{
  "projectCandidates": [{"label":"string","confidence":0.0,"evidence":["string"]}],
  "workpackageCandidates": [{"label":"string","confidence":0.0,"evidence":["string"]}],
  "needsReply": {"score":0.0,"reasons":["string"]},
  "keywords": ["string"],
  "entities": ["string"],
  "notes": "string"
}

Rules:
- confidence and score in [0,1]
- Prefer precision over recall
- If unsure, return low confidence
- When matching to projects, prefer labels from PROJECT_CATALOG_HINTS (project id/title)
- Also use PROJECT_CATALOG_HINTS to infer plausible workpackage suggestions for the selected project
- workpackageCandidates labels must refer to workpackage id or title within the most likely project candidate
- If no workpackage signal exists, return an empty workpackageCandidates array
- Do not include markdown or code fences`;

const DEFAULT_DISCOVERY_PROMPT = `You extract project identity and topics from ONE email for project discovery.

Return STRICT JSON only. No markdown, no code fences, no extra keys, no commentary.

Input sections you will receive:
- [MAIL_META]
- [CURRENT_MESSAGE]
- [OLDER_CONTEXT_LOWER_WEIGHT]

Task:
From this email, infer:
1) a stable project identifier ("project_name")
2) a human-readable project title ("project_title")
3) 1 to 3 concise topics ("topics")

Output JSON schema (exact keys, exact types):
{
  "project_name": "string",
  "project_title": "string",
  "topics": ["string"],
  "confidence": 0.0
}

Rules:
- "project_name":
  - lowercase
  - kebab-case (a-z, 0-9, hyphen)
  - 3..48 chars
  - must start with a letter
  - no spaces, no underscores
- "project_title":
  - readable title-case text
  - 3..120 chars
  - no trailing punctuation
- "topics":
  - array length: 1..3
  - each topic: 2..40 chars
  - specific, concrete nouns/phrases
  - no duplicates
  - no generic filler like: "email", "communication", "project", "update", "general"
- "confidence":
  - float in [0,1]
  - reflects certainty of project identification

Unknown policy (mandatory):
- If no clear project can be inferred, use:
  - "project_name": "unknown"
  - "project_title": "Unknown"
  - "topics": still provide 1..3 meaningful content topics from the email
  - "confidence": <= 0.35

Evidence weighting:
- Prefer [CURRENT_MESSAGE].
- Use [MAIL_META] as strong support (subject, sender domain, reply-to, list headers).
- Treat [OLDER_CONTEXT_LOWER_WEIGHT] as weak context.

Disambiguation:
- If multiple projects are plausible, choose the most likely one and lower confidence.
- Do not invent organization names or acronyms not grounded in the email.

Validation before final output:
- Ensure output is valid JSON.
- Ensure all constraints above are satisfied.
- If a constraint would be violated, return unknown-policy output instead.`;

function getPrompt(cwd: string, promptPath?: string): string {
  if (!promptPath) return DEFAULT_PROMPT;
  const p = path.resolve(cwd, promptPath);
  if (!fs.existsSync(p)) return DEFAULT_PROMPT;
  return fs.readFileSync(p, "utf8");
}

function stripCodeFences(text: string): string {
  return text
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```\s*$/i, "")
    .trim();
}

function removeTrailingCommas(text: string): string {
  return text.replace(/,\s*([}\]])/g, "$1");
}

function safeJsonParse(text: string): any {
  const trimmed = stripCodeFences(text.trim());
  const candidates: string[] = [trimmed];

  const firstObj = trimmed.indexOf("{");
  const lastObj = trimmed.lastIndexOf("}");
  if (firstObj >= 0 && lastObj > firstObj) {
    candidates.push(trimmed.slice(firstObj, lastObj + 1));
  }

  for (const rawCandidate of candidates) {
    const candidate = removeTrailingCommas(rawCandidate);
    try {
      return JSON.parse(candidate);
    } catch {
      // try next candidate
    }
  }

  throw new Error("LLM did not return valid JSON");
}

export async function extractWithLlm(params: {
  cwd: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  mailText: string;
  projectHints?: string;
  promptPath?: string;
  timeoutMs?: number;
}): Promise<LlmExtraction> {
  const prompt = getPrompt(params.cwd, params.promptPath);
  const timeoutMs = params.timeoutMs ?? 60000;

  const base = params.baseUrl.replace(/\/$/, "");
  const endpoints = [`${base}/chat/completions`, `${base}/v1/chat/completions`];

  let lastErr = "";
  for (const endpoint of endpoints) {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${params.apiKey}`,
        },
        body: JSON.stringify({
          model: params.model,
          temperature: 0,
          messages: [
            {
              role: "user",
              content: `${prompt}\n\nPROJECT_CATALOG_HINTS:\n${params.projectHints ?? ""}\n\nEMAIL_INPUT:\n${params.mailText}`,
            },
          ],
        }),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text();
        lastErr = `LLM request failed (${endpoint}): ${res.status} ${text}`;
        if (res.status === 404) {
          continue;
        }
        throw new Error(lastErr);
      }

      const data = (await res.json()) as any;
      const content = data?.choices?.[0]?.message?.content;
      if (typeof content !== "string" || !content.trim()) {
        throw new Error("LLM response missing message content");
      }

      const parsed = safeJsonParse(content);
      return {
        projectCandidates: Array.isArray(parsed.projectCandidates) ? parsed.projectCandidates : [],
        workpackageCandidates: Array.isArray(parsed.workpackageCandidates) ? parsed.workpackageCandidates : [],
        needsReply: parsed.needsReply && typeof parsed.needsReply === "object" ? parsed.needsReply : { score: 0 },
        keywords: Array.isArray(parsed.keywords) ? parsed.keywords : [],
        entities: Array.isArray(parsed.entities) ? parsed.entities : [],
        notes: typeof parsed.notes === "string" ? parsed.notes : "",
      };
    } finally {
      clearTimeout(t);
    }
  }

  throw new Error(lastErr || "LLM request failed: no endpoint succeeded");
}

export async function extractDiscoveryWithLlm(params: {
  baseUrl: string;
  apiKey: string;
  model: string;
  mailText: string;
  timeoutMs?: number;
}): Promise<DiscoveryLlmExtraction> {
  const timeoutMs = params.timeoutMs ?? 60000;
  const base = params.baseUrl.replace(/\/$/, "");
  const endpoints = [`${base}/chat/completions`, `${base}/v1/chat/completions`];

  let lastErr = "";
  for (const endpoint of endpoints) {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${params.apiKey}`,
        },
        body: JSON.stringify({
          model: params.model,
          temperature: 0,
          messages: [
            {
              role: "user",
              content: `${DEFAULT_DISCOVERY_PROMPT}\n\nEMAIL_INPUT:\n${params.mailText}`,
            },
          ],
        }),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text();
        lastErr = `Discovery LLM request failed (${endpoint}): ${res.status} ${text}`;
        if (res.status === 404) continue;
        throw new Error(lastErr);
      }

      const data = (await res.json()) as any;
      const content = data?.choices?.[0]?.message?.content;
      if (typeof content !== "string" || !content.trim()) {
        throw new Error("Discovery LLM response missing message content");
      }

      const parsed = safeJsonParse(content);
      if (typeof parsed.project_name !== "string" || typeof parsed.project_title !== "string") {
        throw new Error("Discovery LLM response missing project_name/project_title");
      }

      return {
        project_name: parsed.project_name,
        project_title: parsed.project_title,
        topics: Array.isArray(parsed.topics) ? parsed.topics.filter((x: unknown) => typeof x === "string") : [],
        confidence: Number.isFinite(Number(parsed.confidence)) ? Number(parsed.confidence) : 0,
      };
    } finally {
      clearTimeout(t);
    }
  }

  throw new Error(lastErr || "Discovery LLM request failed: no endpoint succeeded");
}
