import type { ReactNode } from "react";
import { Meter } from "./ui";

/** A headline number. ``meter`` (0..1) draws how much of its limit is used under the value. */
export function KpiTile({ label, value, sub, className = "", meter }: {
  label: string; value: ReactNode; sub?: ReactNode; className?: string; meter?: number | null;
}) {
  const glow = className.includes("emerald") ? "glow-up" : className.includes("rose") ? "glow-down" : "";
  const color = /\btext-/.test(className) ? className : `text-zinc-50 ${className}`;
  return (
    <div className="brackets group flex min-w-0 flex-col justify-between rounded-sm border border-zinc-800 bg-zinc-900/70 px-3 py-2 transition-colors hover:border-zinc-700">
      <div className="label truncate text-[0.625rem] text-amber-400/80">{label}</div>
      <div className={`num mt-1 truncate text-[1.375rem] font-medium leading-tight ${glow} ${color}`}>{value}</div>
      {meter !== undefined && <Meter fraction={meter} className="mt-1.5" />}
      {sub && <div className="num mt-1 truncate text-[0.6875rem] text-zinc-500">{sub}</div>}
    </div>
  );
}
