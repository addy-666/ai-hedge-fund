const TONES: Record<string, string> = {
  RUNNING: "bg-emerald-600 text-white",
  PAUSED: "bg-amber-500 text-black",
  HALTED: "bg-rose-700 text-white",
  FLATTENING: "bg-rose-600 text-white animate-pulse",
  STARTING: "bg-sky-700 text-white",
  STOPPED: "bg-zinc-700 text-zinc-200",
};

export function StatePill({ state, stale }: { state: string | undefined; stale?: boolean }) {
  const label = state ?? "UNKNOWN";
  return (
    <span className={`rounded-full px-3 py-1 text-xs font-bold ${stale ? "bg-zinc-700 text-zinc-300 line-through" : TONES[label] ?? "bg-zinc-700"}`} title={stale ? "no engine heartbeat for 30 s" : undefined}>
      {label}
    </span>
  );
}

export function ModeBadge({ mode }: { mode: string | undefined }) {
  const tone = mode === "LIVE" ? "bg-rose-600 text-white" : mode === "DEMO" ? "bg-sky-800 text-sky-100" : "bg-zinc-800 text-zinc-300";
  return <span className={`rounded px-2 py-1 text-xs font-bold ${tone}`}>{mode ?? "?"}</span>;
}
