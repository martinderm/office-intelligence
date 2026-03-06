import fs from "node:fs";
import path from "node:path";

export type LlmExtraction = {
  projectCandidates: Array<{ label: string; confidence: number; evidence?: string[] }>;
  needsReply: { score: number; reasons?: string[] };
  keywords?: string[];
  entities?: string[];
  notes?: string;
};

const DEFAULT_PROMPT = `You classify emails for project routing.
Return STRICT JSON only.
Focus primarily on [CURRENT_MESSAGE]. Treat [OLDER_CONTEXT_LOWER_WEIGHT] as weaker evidence.

Output schema:
{
  "projectCandidates": [{"label":"string","confidence":0.0,"evidence":["string"]}],
  "needsReply": {"score":0.0,"reasons":["string"]},
  "keywords": ["string"],
  "entities": ["string"],
  "notes": "string"
}

Rules:
- confidence and score in [0,1]
- Prefer precision over recall
- If unsure, return low confidence
- Do not include markdown or code fences`; 

function getPrompt(cwd: string, promptPath?: string): string {
  if (!promptPath) return DEFAULT_PROMPT;
  const p = path.resolve(cwd, promptPath);
  if (!fs.existsSync(p)) return DEFAULT_PROMPT;
  return fs.readFileSync(p, "utf8");
}

function safeJsonParse(text: string): any {
  const trimmed = text.trim();
  try {
    return JSON.parse(trimmed);
  } catch {
    const m = trimmed.match(/\{[\s\S]*\}$/);
    if (!m) throw new Error("LLM did not return valid JSON");
    return JSON.parse(m[0]);
  }
}

export async function extractWithLlm(params: {
  cwd: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  mailText: string;
  promptPath?: string;
  timeoutMs?: number;
}): Promise<LlmExtraction> {
  const prompt = getPrompt(params.cwd, params.promptPath);
  const timeoutMs = params.timeoutMs ?? 60000;

  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(`${params.baseUrl.replace(/\/$/, "")}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${params.apiKey}`,
      },
      body: JSON.stringify({
        model: params.model,
        temperature: 0,
        messages: [
          { role: "system", content: prompt },
          { role: "user", content: params.mailText },
        ],
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      throw new Error(`LLM request failed: ${res.status} ${await res.text()}`);
    }

    const data = await res.json() as any;
    const content = data?.choices?.[0]?.message?.content;
    if (typeof content !== "string" || !content.trim()) {
      throw new Error("LLM response missing message content");
    }

    const parsed = safeJsonParse(content);
    return {
      projectCandidates: Array.isArray(parsed.projectCandidates) ? parsed.projectCandidates : [],
      needsReply: parsed.needsReply && typeof parsed.needsReply === "object" ? parsed.needsReply : { score: 0 },
      keywords: Array.isArray(parsed.keywords) ? parsed.keywords : [],
      entities: Array.isArray(parsed.entities) ? parsed.entities : [],
      notes: typeof parsed.notes === "string" ? parsed.notes : "",
    };
  } finally {
    clearTimeout(t);
  }
}
