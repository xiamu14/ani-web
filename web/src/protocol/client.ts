import { ProtocolApiError } from "./error.ts";
import type {
  ArtifactView,
  AttachmentView,
  BranchView,
  ContextWindowSnapshotView,
  EntryPageView,
  ErrorEnvelope,
  HealthView,
  InteractionRespondRequest,
  InteractionView,
  InterruptResponse,
  McpsListView,
  RuntimeSnapshotView,
  SendMessageReceipt,
  SendMessageRequest,
  SkillsListView,
  TaskView,
  TreeView,
  TurnPageView,
  WorkspaceLockView,
} from "./types.ts";

export { ProtocolApiError } from "./error.ts";

export interface ClientConfig {
  readonly authToken: string;
  readonly baseUrl: string;
}

const TRAILING_SLASH_REGEX = /\/+$/u;

function normalizeBaseUrl(baseUrl: string): string {
  return baseUrl.trim().replace(TRAILING_SLASH_REGEX, "");
}

export class GuiProtocolClient {
  private readonly authToken: string;
  private readonly baseUrl: string;

  constructor(config: ClientConfig) {
    this.baseUrl = normalizeBaseUrl(config.baseUrl);
    this.authToken = config.authToken.trim();
  }

  private url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  private headers(extra?: Record<string, string>): Record<string, string> {
    const result: Record<string, string> = { ...extra };
    if (this.authToken) {
      result.Authorization = `Bearer ${this.authToken}`;
    }
    return result;
  }

  private async requestJson<T>(
    path: string,
    init: RequestInit = {}
  ): Promise<T> {
    const response = await fetch(this.url(path), {
      ...init,
      headers: this.headers(init.headers as Record<string, string> | undefined),
    });
    if (!response.ok) {
      let envelope: ErrorEnvelope | null = null;
      try {
        envelope = (await response.json()) as ErrorEnvelope;
      } catch {
        envelope = null;
      }
      throw new ProtocolApiError(
        response.status,
        envelope?.code ?? `HTTP_${response.status}`,
        envelope?.message ?? response.statusText,
        envelope?.retryable ?? false
      );
    }
    return (await response.json()) as T;
  }

  health(): Promise<HealthView> {
    return this.requestJson<HealthView>("/v1/health");
  }

  getRuntimeSnapshot(): Promise<RuntimeSnapshotView> {
    return this.requestJson<RuntimeSnapshotView>("/v1/runtime/snapshot");
  }

  updateSubscriptions(
    subscriptionId: string,
    taskIds: string[]
  ): Promise<{ ok: boolean; task_ids: string[] }> {
    return this.requestJson("/v1/events/subscriptions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription_id: subscriptionId,
        task_ids: taskIds,
      }),
    });
  }

  getTree(includeRoleBranches = true): Promise<TreeView> {
    return this.requestJson<TreeView>(
      `/v1/tree?include_role_branches=${String(includeRoleBranches)}`
    );
  }

  createConversation(payload: {
    target_title?: string;
    work_path?: string | null;
    switch_to?: boolean;
  }): Promise<BranchView> {
    return this.requestJson<BranchView>("/v1/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  switchConversation(
    cid: string
  ): Promise<{ conversation_id: string; active: boolean }> {
    return this.requestJson(
      `/v1/conversations/${encodeURIComponent(cid)}/switch`,
      {
        method: "POST",
      }
    );
  }

  forkConversation(
    cid: string,
    payload: {
      mode: "full_fork" | "handoff";
      at_entry_id?: string | null;
      summary?: string;
      target_title?: string;
      work_path?: string | null;
      switch_to?: boolean;
    }
  ): Promise<BranchView> {
    return this.requestJson<BranchView>(
      `/v1/conversations/${encodeURIComponent(cid)}/fork`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
  }

  patchConversation(
    cid: string,
    payload: { target_title?: string | null; work_path?: string | null }
  ): Promise<BranchView> {
    return this.requestJson<BranchView>(
      `/v1/conversations/${encodeURIComponent(cid)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
  }

  getEntries(
    cid: string,
    limit = 50,
    beforeEntryId?: string
  ): Promise<EntryPageView> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (beforeEntryId) {
      query.set("before_entry_id", beforeEntryId);
    }
    return this.requestJson<EntryPageView>(
      `/v1/conversations/${encodeURIComponent(cid)}/entries?${query.toString()}`
    );
  }

  getTurns(
    cid: string,
    limit = 50,
    beforeEntryId?: string
  ): Promise<TurnPageView> {
    const query = new URLSearchParams({
      view: "turns",
      limit: String(limit),
    });
    if (beforeEntryId) {
      query.set("before_entry_id", beforeEntryId);
    }
    return this.requestJson<TurnPageView>(
      `/v1/conversations/${encodeURIComponent(cid)}/entries?${query.toString()}`
    );
  }

  sendMessage(
    cid: string,
    payload: SendMessageRequest
  ): Promise<SendMessageReceipt> {
    return this.requestJson<SendMessageReceipt>(
      `/v1/conversations/${encodeURIComponent(cid)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
  }

  interruptConversation(cid: string): Promise<InterruptResponse> {
    return this.requestJson<InterruptResponse>(
      `/v1/conversations/${encodeURIComponent(cid)}/interrupt`,
      { method: "POST" }
    );
  }

  async uploadAttachments(files: readonly File[]): Promise<AttachmentView[]> {
    const form = new FormData();
    for (const file of files) {
      form.append("file", file, file.name);
    }
    const res = await this.requestJson<{ attachments: AttachmentView[] }>(
      "/v1/attachments",
      {
        method: "POST",
        body: form,
      }
    );
    return res.attachments;
  }

  async listArtifacts(scope = "global"): Promise<ArtifactView[]> {
    const query = new URLSearchParams({ scope });
    const res = await this.requestJson<{ artifacts: ArtifactView[] }>(
      `/v1/artifacts?${query.toString()}`
    );
    return res.artifacts;
  }

  respondInteraction(
    interactionId: string,
    payload: InteractionRespondRequest
  ): Promise<InteractionView> {
    return this.requestJson<InteractionView>(
      `/v1/interactions/${encodeURIComponent(interactionId)}/respond`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
  }

  async listTasks(cid: string): Promise<TaskView[]> {
    const res = await this.requestJson<{ tasks: TaskView[] }>(
      `/v1/conversations/${encodeURIComponent(cid)}/tasks`
    );
    return res.tasks;
  }

  steerTask(
    cid: string,
    tid: string,
    instruction: string
  ): Promise<{ message: string; task: TaskView | null }> {
    return this.requestJson(
      `/v1/conversations/${encodeURIComponent(cid)}/tasks/${encodeURIComponent(tid)}/steer`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction }),
      }
    );
  }

  cancelTask(
    cid: string,
    tid: string,
    reason = ""
  ): Promise<{ message: string; task: TaskView | null }> {
    return this.requestJson(
      `/v1/conversations/${encodeURIComponent(cid)}/tasks/${encodeURIComponent(tid)}/cancel`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }),
      }
    );
  }

  async listWorkspaceLocks(): Promise<WorkspaceLockView[]> {
    const res = await this.requestJson<{ locks: WorkspaceLockView[] }>(
      "/v1/workspace-locks"
    );
    return res.locks;
  }

  getContext(cid: string): Promise<ContextWindowSnapshotView> {
    return this.requestJson<ContextWindowSnapshotView>(
      `/v1/conversations/${encodeURIComponent(cid)}/context`
    );
  }

  listSkills(params?: {
    work_path?: string;
    conversation_id?: string;
  }): Promise<SkillsListView> {
    const query = new URLSearchParams();
    if (params?.work_path) {
      query.set("work_path", params.work_path);
    }
    if (params?.conversation_id) {
      query.set("conversation_id", params.conversation_id);
    }
    const qs = query.toString();
    return this.requestJson<SkillsListView>(`/v1/skills${qs ? `?${qs}` : ""}`);
  }

  listMcps(): Promise<McpsListView> {
    return this.requestJson<McpsListView>("/v1/mcps");
  }
}
