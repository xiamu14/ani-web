import { Wrench01Icon } from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import { Popover, PopoverContent, PopoverTrigger } from "@ui-sdk/react/popover";
import { useCallback, useState } from "react";
import { Badge } from "@/components/ui/badge.tsx";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import type {
  McpServerView,
  SkillScopeLiteral,
  SkillView,
} from "@/protocol/types.ts";
import { connectionStore, refreshCapabilities } from "@/store/connection.ts";
import { workspaceStore } from "@/store/workspace.ts";

const QUICK_COMMANDS = [
  { command: "工作区切到 ", label: "切换目录", hint: "修改工作路径" },
  { command: "请检查当前工作区代码", label: "检查代码", hint: "只读审查" },
  { command: "请运行测试并汇报结果", label: "运行测试", hint: "执行验证" },
] as const;

const SKILL_SCOPE_LABEL: Record<SkillScopeLiteral, string> = {
  workspace: "工作区",
  global: "全局",
  builtin: "内置",
};

function getMcpStatusMeta(server: McpServerView): {
  label: string;
  tone: "emerald" | "amber" | "neutral";
} {
  if (server.blocked) {
    return { label: "已阻断", tone: "amber" };
  }
  if (server.compile_error) {
    return { label: "异常", tone: "amber" };
  }
  if (server.defer_loading) {
    return { label: "按需", tone: "neutral" };
  }
  return { label: "就绪", tone: "emerald" };
}

function CommandOption({
  command,
  hint,
  label,
  onSelect,
}: {
  readonly command: string;
  readonly hint: string;
  readonly label: string;
  readonly onSelect: (command: string) => void;
}) {
  const handleClick = useCallback(() => onSelect(command), [command, onSelect]);

  return (
    <button
      className="hover:bg-surface-hover flex w-full cursor-pointer items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors"
      onClick={handleClick}
      type="button"
    >
      <span className="text-text font-medium">{label}</span>
      <span className="text-text-muted text-[11px]">{hint}</span>
    </button>
  );
}

function SkillRow({
  skill,
  onSelect,
}: {
  readonly skill: SkillView;
  readonly onSelect: (token: string) => void;
}) {
  const handleClick = useCallback(
    () => onSelect(`@skill:${skill.name} `),
    [onSelect, skill.name]
  );

  return (
    <li>
      <button
        className="hover:bg-surface-hover flex w-full cursor-pointer items-center justify-between gap-2 rounded-md px-1.5 py-1 text-left text-[11px] transition-colors"
        onClick={handleClick}
        title={skill.description}
        type="button"
      >
        <span className="text-text truncate font-medium">{skill.name}</span>
        <span className="text-text-muted shrink-0">
          {SKILL_SCOPE_LABEL[skill.scope]}
        </span>
      </button>
    </li>
  );
}

function SkillsSection({
  skills,
  statusLabel,
  tone,
  onSelect,
}: {
  readonly skills: readonly SkillView[];
  readonly statusLabel: string;
  readonly tone: "emerald" | "amber" | "neutral";
  readonly onSelect: (token: string) => void;
}) {
  return (
    <div className="border-border-subtle border-b pb-2">
      <div className="mb-1 flex items-center justify-between px-1">
        <span className="text-text-secondary text-[11px] font-semibold">
          技能 Skill
        </span>
        <Badge tone={tone}>{statusLabel}</Badge>
      </div>
      {skills.length === 0 ? (
        <p className="text-text-muted px-1 text-[11px]">暂无</p>
      ) : (
        <ul className="flex max-h-32 flex-col gap-0.5 overflow-y-auto">
          {skills.map((skill) => (
            <SkillRow
              key={`${skill.scope}:${skill.name}`}
              onSelect={onSelect}
              skill={skill}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function McpRow({
  server,
  onSelect,
}: {
  readonly server: McpServerView;
  readonly onSelect: (token: string) => void;
}) {
  const meta = getMcpStatusMeta(server);
  const handleClick = useCallback(
    () => onSelect(`@mcp:${server.name} `),
    [onSelect, server.name]
  );

  return (
    <li>
      <button
        className="hover:bg-surface-hover flex w-full cursor-pointer items-center justify-between gap-2 rounded-md px-1.5 py-1 text-left text-[11px] transition-colors"
        onClick={handleClick}
        title={server.compile_error ?? server.description ?? server.name}
        type="button"
      >
        <span className="text-text truncate font-medium">{server.name}</span>
        <div className="flex shrink-0 items-center gap-1.5">
          <span className="text-text-muted font-mono">
            {server.tools.length}
          </span>
          <Badge tone={meta.tone}>{meta.label}</Badge>
        </div>
      </button>
    </li>
  );
}

function McpSection({
  servers,
  statusLabel,
  tone,
  onSelect,
}: {
  readonly servers: readonly McpServerView[];
  readonly statusLabel: string;
  readonly tone: "emerald" | "amber" | "neutral";
  readonly onSelect: (token: string) => void;
}) {
  return (
    <div className="border-border-subtle border-b pb-2">
      <div className="mb-1 flex items-center justify-between px-1">
        <span className="text-text-secondary text-[11px] font-semibold">
          扩展 MCP
        </span>
        <Badge tone={tone}>{statusLabel}</Badge>
      </div>
      {servers.length === 0 ? (
        <p className="text-text-muted px-1 text-[11px]">暂无</p>
      ) : (
        <ul className="flex max-h-32 flex-col gap-0.5 overflow-y-auto">
          {servers.map((server) => (
            <McpRow key={server.name} onSelect={onSelect} server={server} />
          ))}
        </ul>
      )}
    </div>
  );
}

export function ToolsPopover({
  onInsertCommand,
}: {
  readonly onInsertCommand: (command: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const status = useSelector(connectionStore, (s) => s.status);
  const skills = useSelector(connectionStore, (s) => s.skills);
  const mcpServers = useSelector(connectionStore, (s) => s.mcpServers);
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);

  let skillStatus = "未连接";
  let skillTone: "emerald" | "amber" | "neutral" = "neutral";
  if (status === "connected") {
    skillStatus = skills.length > 0 ? `可用 ${skills.length}` : "暂无";
    skillTone = skills.length > 0 ? "emerald" : "neutral";
  }

  let mcpStatus = "未连接";
  let mcpTone: "emerald" | "amber" | "neutral" = "neutral";
  if (status === "connected") {
    if (mcpServers.length === 0) {
      mcpStatus = "未启用";
    } else {
      const hasIssue = mcpServers.some(
        (server) => server.blocked || Boolean(server.compile_error)
      );
      mcpStatus = `已配 ${mcpServers.length}`;
      mcpTone = hasIssue ? "amber" : "emerald";
    }
  }

  const handleOpenChange = useCallback(
    (nextOpen: boolean) => {
      setOpen(nextOpen);
      if (nextOpen && status === "connected") {
        void refreshCapabilities(activeRouteId || undefined);
      }
    },
    [activeRouteId, status]
  );

  const handleSelectCommand = useCallback(
    (command: string) => {
      onInsertCommand(command);
      setOpen(false);
    },
    [onInsertCommand]
  );

  return (
    <Popover onOpenChange={handleOpenChange} open={open}>
      <PopoverTrigger
        render={
          <Button size="xs" variant="subtle">
            <Icon icon={Wrench01Icon} size="xs" />
            <span>工具</span>
          </Button>
        }
      />
      <PopoverContent
        align="start"
        className="border-border bg-surface w-72 rounded-xl border p-2.5 shadow-lg"
        side="top"
        sideOffset={8}
      >
        <div className="flex flex-col gap-2.5">
          <SkillsSection
            onSelect={handleSelectCommand}
            skills={skills}
            statusLabel={skillStatus}
            tone={skillTone}
          />
          <McpSection
            onSelect={handleSelectCommand}
            servers={mcpServers}
            statusLabel={mcpStatus}
            tone={mcpTone}
          />
          <div>
            <div className="text-text-secondary mb-1 px-1 text-[11px] font-semibold">
              快捷指令
            </div>
            <div className="flex flex-col gap-0.5">
              {QUICK_COMMANDS.map((item) => (
                <CommandOption
                  command={item.command}
                  hint={item.hint}
                  key={item.label}
                  label={item.label}
                  onSelect={handleSelectCommand}
                />
              ))}
            </div>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
