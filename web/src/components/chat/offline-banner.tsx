import { AlertCircleIcon, Refresh01Icon } from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import { useCallback } from "react";
import { Button } from "@/components/ui/button.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import {
  connectionStore,
  discoverAndConnectRuntime,
} from "@/store/connection.ts";

export function OfflineBanner() {
  const status = useSelector(connectionStore, (s) => s.status);
  const error = useSelector(connectionStore, (s) => s.error);

  const handleRetry = useCallback(() => {
    void discoverAndConnectRuntime();
  }, []);

  if (status === "connected") {
    return null;
  }

  const isConnecting = status === "connecting";
  let title = "服务离线";
  let description = "";
  if (isConnecting) {
    title = "正在连接";
  } else if (status === "error" && error) {
    title = "连接异常";
    description = error;
  }

  return (
    <div
      className="border-border-subtle bg-surface-subtle/90 flex items-center justify-between gap-3 border-b px-4 py-2 text-xs"
      role="status"
    >
      <div className="flex min-w-0 items-center gap-2">
        <Icon
          className={isConnecting ? "text-amber-500" : "text-text-muted"}
          icon={AlertCircleIcon}
          size="xs"
        />
        <span className="font-medium">{title}</span>
        {description ? (
          <span className="text-text-muted truncate">{description}</span>
        ) : null}
      </div>

      <Button
        disabled={isConnecting}
        onClick={handleRetry}
        size="xs"
        variant="subtle"
      >
        <Icon icon={Refresh01Icon} size="xs" />
        <span>{isConnecting ? "连接中" : "重新连接"}</span>
      </Button>
    </div>
  );
}
