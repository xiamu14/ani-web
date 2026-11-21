import { scope, type } from "arktype";

export const roleNameSchema = type(
  "'coder' | 'reviewer' | 'tester' | 'researcher'"
);
export type RoleName = typeof roleNameSchema.infer;

export const taskStatusSchema = type(
  "'running' | 'queued' | 'completed' | 'failed' | 'interrupted' | 'cancelled'"
);
export type TaskStatus = typeof taskStatusSchema.infer;

export const lockModeSchema = type("'exclusive' | 'shared' | 'none'");
export type LockMode = typeof lockModeSchema.infer;

export const dashboardTabSchema = type("'tree' | 'context' | 'files'");
export type DashboardTab = typeof dashboardTabSchema.infer;

export const attachmentKindSchema = type(
  "'image' | 'audio' | 'video' | 'document' | 'binary'"
);
export type AttachmentKind = typeof attachmentKindSchema.infer;

export const artifactFormatSchema = type(
  "'json' | 'markdown' | 'code' | 'diff'"
);
export type ArtifactFormat = typeof artifactFormatSchema.infer;

export const artifactItemSchema = type({
  id: "string",
  routeId: "string",
  artifactCode: "string",
  title: "string",
  format: artifactFormatSchema,
  sizeLabel: "string",
  summary: "string",
  producedBy: "string",
  createdAt: "string",
});
export type ArtifactItem = typeof artifactItemSchema.infer;

export const attachmentReferenceSchema = type({
  id: "string",
  kind: "'attachment'",
  attachmentKind: attachmentKindSchema,
  title: "string",
  mediaType: "string",
  sizeLabel: "string",
});
export type AttachmentReference = typeof attachmentReferenceSchema.infer;

export const fileReferenceSchema = type({
  id: "string",
  kind: "'file'",
  path: "string",
  name: "string",
  isDir: "boolean",
  "sizeLabel?": "string",
});
export type FileReference = typeof fileReferenceSchema.infer;

export const artifactReferenceSchema = type({
  id: "string",
  kind: "'artifact'",
  artifactId: "string",
  artifactCode: "string",
  title: "string",
  format: artifactFormatSchema,
  sizeLabel: "string",
});
export type ArtifactReference = typeof artifactReferenceSchema.infer;

export const taskReferenceSchema = type({
  id: "string",
  kind: "'task'",
  taskId: "string",
  taskCode: "string",
  role: roleNameSchema,
  title: "string",
});
export type TaskReference = typeof taskReferenceSchema.infer;

export const composerReferenceSchema = attachmentReferenceSchema
  .or(fileReferenceSchema)
  .or(artifactReferenceSchema)
  .or(taskReferenceSchema);
export type ComposerReference = typeof composerReferenceSchema.infer;

export const routeBranchSchema = type({
  id: "string",
  title: "string",
  workPath: "string",
  status: "'active' | 'idle' | 'waiting'",
  headEntryId: "string",
  revision: "string",
  updatedAt: "string",
  messageCount: "number",
});
export type RouteBranch = typeof routeBranchSchema.infer;

export const roleTaskSchema = type({
  id: "string",
  routeId: "string",
  taskCode: "string",
  role: roleNameSchema,
  title: "string",
  status: taskStatusSchema,
  lockMode: lockModeSchema,
  dependsOn: "string[]",
  workPath: "string",
  updatedAt: "string",
  "livePhase?": "string",
  "roleConversationId?": "string",
  "currentStep?": "number",
  "maxRequests?": "number",
  "toolCallsCount?": "number",
  "maxToolCalls?": "number",
  "activeToolName?": "string",
  "summary?": "string",
});
export type RoleTask = typeof roleTaskSchema.infer;

export const toolTraceSchema = type({
  id: "string",
  name: "string",
  argsSummary: "string",
  status: "'running' | 'done' | 'error'",
  durationMs: "number",
});
export type ToolTrace = typeof toolTraceSchema.infer;

export const turnStepSchema = type({
  id: "string",
  kind: "'thinking' | 'tool' | 'text'",
  "text?": "string",
  "toolCallId?": "string",
  "toolName?": "string",
  "status?": "'running' | 'done' | 'error'",
  "argsText?": "string",
  "resultText?": "string",
  "durationMs?": "number",
});
export type TurnStep = typeof turnStepSchema.infer;

export const verifiedCitationSchema = type({
  id: "string",
  sourceType: "string",
  label: "string",
  summary: "string",
});
export type VerifiedCitation = typeof verifiedCitationSchema.infer;

export const suggestedActionSchema = type({
  id: "string",
  label: "string",
  action: "string",
  "payload?": "unknown",
});
export type SuggestedActionItem = typeof suggestedActionSchema.infer;

export const chatMessageSchema = type({
  id: "string",
  threadId: "string",
  sender: "'user' | 'router' | 'role' | 'system'",
  senderLabel: "string",
  "role?": roleNameSchema,
  content: "string",
  createdAt: "string",
  "thinking?": "string",
  "runId?": "string",
  "streaming?": "boolean",
  "proactive?": "boolean",
  "error?": "string",
  "dispatchedTaskIds?": "string[]",
  "toolTraces?": toolTraceSchema.array(),
  "steps?": turnStepSchema.array(),
  "references?": composerReferenceSchema.array(),
  "artifactIds?": "string[]",
  "citations?": verifiedCitationSchema.array(),
  "suggestedActions?": suggestedActionSchema.array(),
});
export type ChatMessage = typeof chatMessageSchema.infer;

const treeScope = scope({
  conversationNode: {
    id: "string",
    label: "string",
    kind: "'root' | 'route' | 'task'",
    refId: "string",
    entryId: "string",
    status: "'active' | 'running' | 'queued' | 'completed' | 'idle'",
    tokens: "number",
    isHead: "boolean",
    "children?": "conversationNode[]",
  },
  fileNode: {
    path: "string",
    name: "string",
    kind: "'dir' | 'file'",
    "change?": "'modified' | 'added' | 'read'",
    "touchedBy?": "string",
    "sizeLabel?": "string",
    "children?": "fileNode[]",
  },
});

export const conversationTreeNodeSchema = treeScope.export().conversationNode;
export type ConversationTreeNode = typeof conversationTreeNodeSchema.infer;

export const workspaceFileNodeSchema = treeScope.export().fileNode;
export type WorkspaceFileNode = typeof workspaceFileNodeSchema.infer;

export const contextSegmentSchema = type({
  key: "'system' | 'history' | 'tools' | 'memory'",
  label: "string",
  tokens: "number",
});
export type ContextSegment = typeof contextSegmentSchema.infer;

export const contextWindowSchema = type({
  threadId: "string",
  label: "string",
  maxTokens: "number",
  usedTokens: "number",
  requests: "number",
  inputTokens: "number",
  outputTokens: "number",
  toolCalls: "number",
  segments: contextSegmentSchema.array(),
});
export type ContextWindowSnapshot = typeof contextWindowSchema.infer;

export const workspaceSnapshotSchema = type({
  routes: routeBranchSchema.array(),
  tasks: roleTaskSchema.array(),
  artifacts: artifactItemSchema.array(),
  messagesByThread: { "[string]": chatMessageSchema.array() },
  treeNodes: conversationTreeNodeSchema.array(),
  contextByThread: { "[string]": contextWindowSchema },
  filesByRoute: { "[string]": workspaceFileNodeSchema.array() },
});
export type WorkspaceSnapshot = typeof workspaceSnapshotSchema.infer;
