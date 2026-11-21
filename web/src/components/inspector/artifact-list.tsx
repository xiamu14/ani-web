import { PackageIcon } from "@hugeicons/core-free-icons";
import { useSelector } from "@tanstack/react-store";
import { ArtifactCard } from "@/components/chat/artifact-card.tsx";
import { Icon } from "@/components/ui/icon.tsx";
import { workspaceStore } from "@/store/workspace.ts";

export function ArtifactList() {
  const activeRouteId = useSelector(workspaceStore, (s) => s.activeRouteId);
  const allArtifacts = useSelector(workspaceStore, (s) => s.artifacts);

  const artifacts = allArtifacts.filter(
    (item) => item.routeId === activeRouteId
  );

  return (
    <section className="border-border-subtle bg-rail/60 flex shrink-0 flex-col gap-2 border-t p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-text-secondary inline-flex items-center gap-1.5 text-xs font-medium">
          <Icon className="text-accent" icon={PackageIcon} size="xs" />
          <span>产物</span>
          <span className="text-text-muted font-mono text-[11px]">
            {artifacts.length}
          </span>
        </span>
      </div>

      {artifacts.length === 0 ? (
        <div className="border-border-subtle text-text-muted rounded-lg border border-dashed py-3 text-center text-xs">
          暂无产物
        </div>
      ) : (
        <div className="flex max-h-40 flex-col gap-1.5 overflow-y-auto">
          {artifacts.map((artifact) => (
            <ArtifactCard artifact={artifact} compact key={artifact.id} />
          ))}
        </div>
      )}
    </section>
  );
}
