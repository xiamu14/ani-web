import { createStore } from "@tanstack/react-store";
import type { ThemePreference } from "@/lib/theme.ts";
import {
  applyThemePreference,
  persistThemePreference,
  readThemePreference,
  themeStorageKey,
} from "@/lib/theme.ts";

interface ThemeState {
  preference: ThemePreference;
}

export const themeStore = createStore<ThemeState>({ preference: "system" });

function syncTheme(preference: ThemePreference): void {
  applyThemePreference(preference);
  themeStore.setState((state) =>
    state.preference === preference ? state : { preference }
  );
}

export function setThemePreference(preference: ThemePreference): void {
  persistThemePreference(preference);
  syncTheme(preference);
}

const NEXT_THEME: Record<ThemePreference, ThemePreference> = {
  system: "dark",
  dark: "light",
  light: "system",
};

export function cycleThemePreference(): void {
  const current = themeStore.state.preference;
  setThemePreference(NEXT_THEME[current]);
}

export function initializeTheme(): () => void {
  syncTheme(readThemePreference());
  const onStorage = (event: StorageEvent) => {
    if (event.key === themeStorageKey || event.key === null) {
      syncTheme(readThemePreference());
    }
  };
  window.addEventListener("storage", onStorage);
  return () => window.removeEventListener("storage", onStorage);
}
