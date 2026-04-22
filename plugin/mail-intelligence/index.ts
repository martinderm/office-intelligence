import { Type } from "@sinclair/typebox";
import { definePluginEntry } from "./vendor/plugin-entry.js";
import type { OpenClawPluginApi } from "./types/openclaw-plugin-api.js";
import { classifyMailWithModel } from "./src/tool.js";

const CONTACT = Type.Object({
  email: Type.String(),
  name: Type.Optional(Type.String()),
  role: Type.Optional(Type.String()),
});

const WORKPACKAGE = Type.Object({
  id: Type.String(),
  title: Type.String(),
  aliases: Type.Optional(Type.Array(Type.String())),
  keywords: Type.Optional(Type.Array(Type.String())),
  hint_rank: Type.Optional(Type.Number()),
});

const PROJECT = Type.Object({
  id: Type.String(),
  title: Type.String(),
  aliases: Type.Optional(Type.Array(Type.String())),
  keywords: Type.Optional(Type.Array(Type.String())),
  domains: Type.Optional(Type.Array(Type.String())),
  contacts: Type.Optional(Type.Array(CONTACT)),
  workpackages: Type.Optional(Type.Array(WORKPACKAGE)),
  hint_rank: Type.Optional(Type.Number()),
});

const TOPIC = Type.Object({
  id: Type.String(),
  title: Type.String(),
  aliases: Type.Optional(Type.Array(Type.String())),
  keywords: Type.Optional(Type.Array(Type.String())),
  domains: Type.Optional(Type.Array(Type.String())),
  contacts: Type.Optional(Type.Array(CONTACT)),
  hint_rank: Type.Optional(Type.Number()),
});

const THREAD_CONTEXT_ENTRY = Type.Union([
  Type.Object({
    source: Type.Literal("artifact"),
    message_id: Type.String(),
    date: Type.String(),
    from: Type.String(),
    subject: Type.String(),
    relation: Type.Literal("ancestor"),
    current_message: Type.Union([Type.String(), Type.Null()]),
    older_context: Type.Union([Type.String(), Type.Null()]),
    effective_text: Type.Union([Type.String(), Type.Null()]),
  }),
  Type.Object({
    source: Type.Literal("raw_reference"),
    message_id: Type.Union([Type.String(), Type.Null()]),
    date: Type.Union([Type.String(), Type.Null()]),
    from: Type.Union([Type.String(), Type.Null()]),
    subject: Type.Union([Type.String(), Type.Null()]),
    relation: Type.Literal("ancestor"),
    raw_text: Type.String(),
  }),
]);

const TOOL_INPUT = Type.Object({
  schema_version: Type.Literal(1),
  mail: Type.Object({
    message_id: Type.String(),
    subject: Type.String(),
    from: Type.String(),
    date: Type.String(),
    current_message: Type.String(),
    sanitized_text: Type.String(),
    headers: Type.Object({
      reply_to: Type.Union([Type.String(), Type.Null()]),
      return_path: Type.Union([Type.String(), Type.Null()]),
      list_id: Type.Union([Type.String(), Type.Null()]),
      in_reply_to: Type.Union([Type.String(), Type.Null()]),
      references: Type.Array(Type.String()),
    }),
    thread_context: Type.Optional(Type.Array(THREAD_CONTEXT_ENTRY)),
  }),
  catalog_hints: Type.Object({
    projects: Type.Array(PROJECT),
    topics: Type.Array(TOPIC),
  }),
  options: Type.Object({
    include_needs_reply: Type.Boolean(),
    max_project_candidates: Type.Number(),
    max_topic_candidates: Type.Number(),
    max_workpackage_candidates: Type.Number(),
  }),
});

function configuredDefaultModel(api: OpenClawPluginApi): string | undefined {
  const value = api.pluginConfig?.defaultModel;
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

export default definePluginEntry({
  id: "mail-intelligence",
  name: "Mail Intelligence",
  description: "Structured mail classification tool for mail-processor.",
  register(api: OpenClawPluginApi) {
    api.registerTool(
      {
        name: "mail-classify",
        description: "Classify one prepared email into allowed project/topic/workpackage ids and needsReply.",
        parameters: TOOL_INPUT,
        async execute(_toolCallId: string, params: unknown) {
          const result = await classifyMailWithModel({
            api,
            input: params as any,
            defaultModel: configuredDefaultModel(api),
          });
          return { content: [{ type: "text", text: JSON.stringify(result) }] };
        },
      },
      { optional: true },
    );
  },
});
