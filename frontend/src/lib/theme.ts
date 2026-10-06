// Light / dark theme: a data-theme attribute on <html> switches the palette variables in index.css.
// index.html sets it before the first paint (no flash); the choice is remembered per browser.
export type Theme = "dark" | "light";
const KEY = "aifund.theme";
const listeners = new Set<() => void>();

function read(): Theme {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export function setTheme(next: Theme) {
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    // private window or blocked storage: the theme still applies for this visit
  }
  for (const l of listeners) l();
}

export const theme = {
  get: read,
  subscribe: (l: () => void) => {
    listeners.add(l);
    return () => listeners.delete(l);
  },
};

/** A CSS custom property's current value (chart colours follow the theme). */
export const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
