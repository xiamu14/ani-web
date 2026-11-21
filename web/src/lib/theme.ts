export type ThemePreference = "system" | "light" | "dark";

export const themeStorageKey = "ani-web-theme";

export function readThemePreference(): ThemePreference {
  try {
    const saved = window.localStorage.getItem(themeStorageKey);
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

export function applyThemePreference(preference: ThemePreference): void {
  const root = document.documentElement;
  root.classList.toggle("light", preference === "light");
  root.classList.toggle("dark", preference === "dark");
}

export function persistThemePreference(preference: ThemePreference): void {
  try {
    if (preference === "system") {
      window.localStorage.removeItem(themeStorageKey);
    } else {
      window.localStorage.setItem(themeStorageKey, preference);
    }
  } catch {
    // Ignore storage errors in restricted browser environments.
  }
}

export const themeBootstrapScript = `(() => {
  let preference = "system";
  try {
    const saved = localStorage.getItem(${JSON.stringify(themeStorageKey)});
    if (saved === "light" || saved === "dark") preference = saved;
  } catch {}
  const root = document.documentElement;
  root.classList.toggle("light", preference === "light");
  root.classList.toggle("dark", preference === "dark");
})();`;
