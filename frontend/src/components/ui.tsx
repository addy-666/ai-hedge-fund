// Small primitives on Tailwind: square panels, amber mono labels, cyan for anything you can act on.
import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Card({ title, children, className = "", actions, flush = false }: {
  title?: string; children: ReactNode; className?: string; actions?: ReactNode; flush?: boolean;
}) {
  return (
    <section className={`brackets min-w-0 rounded-sm border border-zinc-800 bg-zinc-900/85 shadow-[inset_0_1px_0_rgb(255_255_255/0.03)] ${className}`}>
      {title && (
        <header className="flex min-h-9 flex-wrap items-center justify-between gap-2 border-b border-zinc-800 bg-gradient-to-r from-zinc-950/80 to-zinc-900/40 px-3 py-1.5">
          <h2 className="label flex items-center gap-2 text-amber-400">
            <span aria-hidden className="inline-block h-2.5 w-[3px] bg-amber-400 shadow-[var(--glow-amber)]" />
            {title}
          </h2>
          {actions}
        </header>
      )}
      <div className={flush ? "" : "p-3"}>{children}</div>
    </section>
  );
}

type Variant = "default" | "danger" | "warn" | "ghost";
const VARIANTS: Record<Variant, string> = {
  default: "border-sky-700/70 bg-sky-900/30 text-sky-300 hover:border-sky-400 hover:bg-sky-900/60 hover:text-sky-100 hover:shadow-[var(--glow-cyan)]",
  danger: "border-rose-600 bg-rose-900/50 text-rose-200 hover:bg-rose-600 hover:text-white hover:shadow-[0_0_14px_-2px_rgb(242_58_90/0.6)]",
  warn: "border-amber-500/80 bg-amber-900/40 text-amber-300 hover:bg-amber-400 hover:text-zinc-950 hover:shadow-[var(--glow-amber)]",
  ghost: "border-transparent text-zinc-400 hover:border-zinc-700 hover:bg-zinc-800/60 hover:text-zinc-100",
};

export function Button({ variant = "default", className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      className={`rounded-sm border px-3 py-1 font-mono text-xs font-medium tracking-wide transition-[background-color,color,box-shadow,border-color] duration-100 disabled:pointer-events-none disabled:opacity-40 ${VARIANTS[variant]} ${className}`}
      {...props}
    />
  );
}

type Tone = "zinc" | "green" | "amber" | "red" | "blue";
const TONES: Record<Tone, string> = {
  zinc: "border-zinc-700 bg-zinc-800/60 text-zinc-300",
  green: "border-emerald-600/60 bg-emerald-900/50 text-emerald-300",
  amber: "border-amber-600/60 bg-amber-900/50 text-amber-300",
  red: "border-rose-600/70 bg-rose-900/60 text-rose-200",
  blue: "border-sky-700 bg-sky-900/50 text-sky-300",
};

export function Badge({ children, tone = "zinc" }: { children: ReactNode; tone?: Tone }) {
  return <span className={`inline-block rounded-sm border px-1.5 py-px font-mono text-[0.6875rem] font-medium tracking-wide ${TONES[tone]}`}>{children}</span>;
}

/** A status LED: green steady, amber/red pulse, grey off. */
export function Led({ tone = "zinc", pulse = false }: { tone?: Tone; pulse?: boolean }) {
  const color = {
    zinc: "bg-zinc-600",
    green: "bg-emerald-400 shadow-[0_0_8px_rgb(47_227_154/0.8)]",
    amber: "bg-amber-400 shadow-[0_0_8px_rgb(255_178_36/0.8)]",
    red: "bg-rose-500 shadow-[0_0_8px_rgb(242_58_90/0.9)]",
    blue: "bg-sky-400 shadow-[0_0_8px_rgb(47_224_245/0.8)]",
  }[tone];
  return <span aria-hidden className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${color} ${pulse ? "animate-pulse-dot" : ""}`} />;
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="-mx-3 overflow-x-auto px-3">
      <table className="w-full border-collapse text-left text-[0.8125rem]">
        <thead>
          <tr className="border-b border-zinc-700/80">
            {head.map((h, i) => <th key={`${h}-${i}`} className="label whitespace-nowrap px-2 py-1.5 text-[0.625rem] text-zinc-500">{h}</th>)}
          </tr>
        </thead>
        <tbody className="num divide-y divide-zinc-800/70 [&>tr]:transition-colors [:where(&>tr:nth-child(even))]:bg-zinc-950/30 [:where(&>tr:hover)]:bg-sky-900/15">
          {children}
        </tbody>
      </table>
    </div>
  );
}

/** Row classes for a clickable table row and the selected one (a cyan rail on the left). */
export const ROW_CLICK = "cursor-pointer hover:bg-sky-900/20";
export const ROW_SELECTED = "bg-sky-900/30 shadow-[inset_2px_0_0_var(--color-sky-400)]";

export const Td = ({ children, className = "", colSpan }: { children: ReactNode; className?: string; colSpan?: number }) => (
  <td colSpan={colSpan} className={`px-2 py-1 ${className}`}>{children}</td>
);

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="py-6 text-center font-mono text-xs text-zinc-500">
      <span aria-hidden className="mr-2 text-zinc-600">∅</span>{children}
    </p>
  );
}

export function Loading() {
  return (
    <p className="py-6 text-center font-mono text-xs text-sky-400/80">
      Loading…<span aria-hidden className="ml-0.5 inline-block h-3 w-1.5 translate-y-0.5 animate-blink bg-sky-400/80" />
    </p>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  return (
    <p className="rounded-sm border border-rose-700/70 border-l-2 border-l-rose-500 bg-rose-950/80 px-3 py-2 font-mono text-xs text-rose-200">
      <span className="mr-2 font-semibold text-rose-400">ERR</span>{error instanceof Error ? error.message : "Something went wrong"}
    </p>
  );
}

export function Select({ value, onChange, options, label }: { value: string; onChange: (v: string) => void; options: string[]; label: string }) {
  return (
    <label className="label flex items-center gap-1.5 text-[0.625rem] text-zinc-500">
      {label}
      <select className="bg-zinc-950 px-1.5 py-0.5 normal-case tracking-normal" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">all</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  );
}

/** Horizontal bar: how much of a limit (or a share of a total) is used; amber past 60%, red past 85%. */
export function Meter({ fraction, tone, className = "" }: { fraction: number | null | undefined; tone?: "auto" | "blue"; className?: string }) {
  const f = Math.max(0, Math.min(1, fraction ?? 0));
  const color = tone === "blue" ? "bg-sky-400/80" : f >= 0.85 ? "bg-rose-500" : f >= 0.6 ? "bg-amber-400" : "bg-emerald-400";
  return (
    <div className={`h-1 w-full overflow-hidden bg-zinc-800 ${className}`} role="presentation">
      <div className={`h-full ${color} transition-[width] duration-500`} style={{ width: `${f * 100}%` }} />
    </div>
  );
}
