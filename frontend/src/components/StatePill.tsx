import { Led } from "./ui";

type Tone = "green" | "amber" | "red" | "blue" | "zinc";
const TONES: Record<string, [string, Tone, boolean]> = {
  RUNNING: ["border-emerald-500/70 bg-emerald-900/40 text-emerald-300", "green", true],
  PAUSED: ["border-amber-500/70 bg-amber-900/40 text-amber-300", "amber", false],
  HALTED: ["border-rose-500 bg-rose-900/60 text-rose-200", "red", true],
  FLATTENING: ["border-rose-500 bg-rose-700/70 text-white animate-pulse", "red", true],
  STARTING: ["border-sky-600 bg-sky-900/50 text-sky-300", "blue", true],
  STOPPED: ["border-zinc-600 bg-zinc-800 text-zinc-300", "zinc", false],
};

export function StatePill({ state, stale }: { state: string | undefined; stale?: boolean }) {
  const label = state ?? "UNKNOWN";
  const [cls, led, pulse] = stale ? ["border-zinc-600 bg-zinc-800 text-zinc-400 line-through", "zinc" as Tone, false] : TONES[label] ?? ["border-zinc-700 bg-zinc-800 text-zinc-300", "zinc" as Tone, false];
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 font-mono text-[0.6875rem] font-semibold tracking-wider ${cls}`} title={stale ? "no engine heartbeat for 30 s" : undefined}>
      <Led tone={led} pulse={pulse} />
      {label}
    </span>
  );
}

export function ModeBadge({ mode }: { mode: string | undefined }) {
  const tone = mode === "LIVE"
    ? "border-rose-500 bg-rose-600 text-white shadow-[0_0_14px_-2px_rgb(242_58_90/0.7)]"
    : mode === "DEMO" ? "border-sky-600 bg-sky-900/50 text-sky-300" : "border-zinc-700 bg-zinc-800 text-zinc-300";
  return <span className={`rounded-sm border px-2 py-0.5 font-mono text-[0.6875rem] font-semibold tracking-wider ${tone}`}>{mode ?? "?"}</span>;
}
