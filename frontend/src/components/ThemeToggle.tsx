import { useSyncExternalStore } from "react";
import { setTheme, theme } from "../lib/theme";

/** Sun / moon switch between the dark terminal and the light theme. */
export function ThemeToggle() {
  const current = useSyncExternalStore(theme.subscribe, theme.get);
  const next = current === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
      className="grid h-7 w-7 place-items-center rounded-sm border border-zinc-700 text-zinc-400 transition-colors hover:border-sky-500 hover:text-sky-300"
    >
      {current === "dark" ? (
        <svg aria-hidden viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <circle cx="8" cy="8" r="3" />
          <path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1" />
        </svg>
      ) : (
        <svg aria-hidden viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round">
          <path d="M13.5 9.5A5.5 5.5 0 0 1 6.5 2.5a5.5 5.5 0 1 0 7 7Z" />
        </svg>
      )}
    </button>
  );
}
