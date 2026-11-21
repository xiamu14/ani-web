import { useEffect, useSyncExternalStore } from "react";
import { initializeTheme } from "@/store/theme.ts";

const darkQuery = "(prefers-color-scheme: dark)";

function subscribeToSystemTheme(onChange: () => void): () => void {
  const media = window.matchMedia(darkQuery);
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

function getSystemDark(): boolean {
  return window.matchMedia(darkQuery).matches;
}

export function useSystemDark(): boolean {
  return useSyncExternalStore(
    subscribeToSystemTheme,
    getSystemDark,
    () => false
  );
}

export function useThemeSync(): void {
  useEffect(initializeTheme, []);
}
