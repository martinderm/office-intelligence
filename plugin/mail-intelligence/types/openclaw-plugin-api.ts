export type OpenClawPluginApi = {
  pluginConfig?: {
    defaultModel?: string;
    promptVersion?: string;
  };
  config: {
    agents?: {
      defaults?: {
        workspace?: string;
        model?: string | { primary?: string };
      };
      list?: Array<{
        id?: string;
        model?: string | { primary?: string };
      }>;
    };
    models?: {
      providers?: Record<string, {
        baseUrl?: string;
      }>;
    };
  };
  runtime: {
    modelAuth: {
      getApiKeyForModel(params: { model: string; cfg: unknown }): Promise<string>;
    };
    agent?: {
      resolveAgentIdentity?(cfg: unknown, agentId?: string): {
        id?: string;
        model?: string | { primary?: string };
      } | null | undefined;
      resolveAgentWorkspaceDir?(cfg: unknown, agentId?: string): string | undefined;
      runEmbeddedPiAgent?(params: {
        sessionId: string;
        sessionFile: string;
        workspaceDir: string;
        config: unknown;
        prompt: string;
        timeoutMs?: number;
        runId?: string;
        provider?: string;
        model?: string;
        authProfileId?: string;
        authProfileIdSource?: "auto" | "user";
        streamParams?: Record<string, unknown>;
        disableTools?: boolean;
      }): Promise<{ payloads?: Array<{ isError?: boolean; text?: string }> }>;
    };
  };
  toolContext?: {
    agentId?: string;
    sessionKey?: string;
  };
  registerTool(tool: unknown, opts?: { optional?: boolean }): void;
};
