import { Link01Icon, Refresh01Icon } from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import type { ChangeEvent, SubmitEvent } from "react";
import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { TooltipHint } from "@/components/ui/tooltip.tsx";
import { cn } from "@/lib/cn.ts";
import {
  connectionStore,
  connectRuntime,
  disconnectRuntime,
} from "@/store/connection.ts";

export function ConnectionPopover() {
  const status = useSelector(connectionStore, (s) => s.status);
  const error = useSelector(connectionStore, (s) => s.error);
  const modelName = useSelector(connectionStore, (s) => s.modelName);
  const savedBaseUrl = useSelector(connectionStore, (s) => s.baseUrl);
  const savedToken = useSelector(connectionStore, (s) => s.token);

  const [open, setOpen] = useState(false);
  const [baseUrl, setBaseUrl] = useState(savedBaseUrl);
  const [token, setToken] = useState(savedToken);

  const handleToggle = useCallback(() => {
    setOpen((prev) => {
      const next = !prev;
      if (next) {
        setBaseUrl(connectionStore.state.baseUrl);
        setToken(connectionStore.state.token);
      }
      return next;
    });
  }, []);

  const handleBaseUrlChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      setBaseUrl(event.target.value);
    },
    []
  );

  const handleTokenChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      setToken(event.target.value);
    },
    []
  );

  const handleConnect = useCallback(
    (event: SubmitEvent<HTMLFormElement>) => {
      event.preventDefault();
      void connectRuntime({
        baseUrl: baseUrl.trim(),
        token: token.trim(),
      }).then((ok) => {
        if (ok) {
          setOpen(false);
        }
      });
    },
    [baseUrl, token]
  );

  const handleDisconnect = useCallback(() => {
    disconnectRuntime();
    setOpen(false);
  }, []);

  let dotClass = "bg-muted-foreground/50";
  let statusText = "离线";
  if (status === "connected") {
    dotClass = "bg-emerald-500";
    statusText = "已连";
  } else if (status === "connecting") {
    dotClass = "bg-amber-500 animate-pulse";
    statusText = "连接中";
  } else if (status === "error") {
    dotClass = "bg-destructive";
    statusText = "异常";
  }

  return (
    <div className="relative">
      <TooltipHint label="服务连接" side="bottom">
        <button
          aria-expanded={open}
          aria-label="服务连接"
          className="border-border-subtle bg-surface-elevated hover:bg-surface-hover flex h-7 items-center gap-1.5 rounded-lg border px-2 text-xs font-medium transition-colors"
          onClick={handleToggle}
          type="button"
        >
          <span className={cn("size-1.5 rounded-full", dotClass)} />
          <span>{statusText}</span>
        </button>
      </TooltipHint>

      {open ? (
        <div className="border-border bg-surface-elevated absolute top-9 left-0 z-50 w-72 rounded-xl border p-3 shadow-lg">
          <div className="mb-2.5 flex items-center justify-between">
            <div className="flex items-center gap-1.5 text-xs font-semibold">
              <Icon icon={Link01Icon} size="xs" />
              <span>后端服务</span>
            </div>
            {modelName ? (
              <span className="text-muted-foreground max-w-32 truncate text-[11px]">
                {modelName}
              </span>
            ) : null}
          </div>

          <form className="flex flex-col gap-2" onSubmit={handleConnect}>
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground text-[11px]">
                服务地址
              </span>
              <input
                className="border-border-subtle bg-surface focus:border-accent h-8 rounded-lg border px-2.5 text-xs outline-none"
                onChange={handleBaseUrlChange}
                placeholder="默认走同源 /v1 代理"
                type="text"
                value={baseUrl}
              />
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground text-[11px]">
                访问令牌
              </span>
              <input
                className="border-border-subtle bg-surface focus:border-accent h-8 rounded-lg border px-2.5 text-xs outline-none"
                onChange={handleTokenChange}
                placeholder="输入 Bearer Token"
                type="password"
                value={token}
              />
            </label>

            {error ? (
              <p className="text-destructive text-[11px] leading-snug">
                {error}
              </p>
            ) : null}

            <div className="mt-1 flex items-center justify-end gap-1.5">
              {status === "connected" ? (
                <Button
                  onClick={handleDisconnect}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  断开
                </Button>
              ) : null}
              <Button
                disabled={status === "connecting"}
                size="sm"
                type="submit"
                variant="primary"
              >
                <Icon icon={Refresh01Icon} size="xs" />
                <span>{status === "connected" ? "重连" : "连接"}</span>
              </Button>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  );
}
