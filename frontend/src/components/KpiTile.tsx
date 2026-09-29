import type { ReactNode } from "react";

export function KpiTile({ label, value, sub, className = "" }: { label: string; value: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2">
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`num text-xl font-semibold ${className}`}>{value}</div>
      {sub && <div className="num text-xs text-zinc-400">{sub}</div>}
    </div>
  );
}
