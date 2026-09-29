// Number and time formatting. Money and prices arrive as decimal strings; they are only ever displayed.

export function money(v: string | number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === "") return "–";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "–";
}

export function signed(v: string | number | null | undefined, digits = 2, suffix = ""): string {
  if (v === null || v === undefined || v === "") return "–";
  const n = Number(v);
  if (!Number.isFinite(n)) return "–";
  return `${n > 0 ? "+" : ""}${n.toFixed(digits)}${suffix}`;
}

export const r = (v: string | number | null | undefined) => signed(v, 2, "R");
export const pct = (v: string | number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "–" : `${Number(v).toFixed(digits)}%`;

export function tone(v: string | number | null | undefined): string {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return "text-zinc-300";
  return n > 0 ? "text-emerald-400" : "text-rose-400";
}

export function utc(iso: string | null | undefined): string {
  if (!iso) return "–";
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
}

export function age(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}
