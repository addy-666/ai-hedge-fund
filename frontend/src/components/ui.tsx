// Small shadcn-style primitives on Tailwind (dense, dark "operations terminal").
import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Card({ title, children, className = "", actions }: { title?: string; children: ReactNode; className?: string; actions?: ReactNode }) {
  return (
    <section className={`rounded-lg border border-zinc-800 bg-zinc-900/60 ${className}`}>
      {title && (
        <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-3">{children}</div>
    </section>
  );
}

type Variant = "default" | "danger" | "warn" | "ghost";
const VARIANTS: Record<Variant, string> = {
  default: "bg-zinc-800 hover:bg-zinc-700 text-zinc-100",
  danger: "bg-rose-700 hover:bg-rose-600 text-white",
  warn: "bg-amber-600 hover:bg-amber-500 text-black",
  ghost: "hover:bg-zinc-800 text-zinc-300",
};

export function Button({ variant = "default", className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      className={`rounded px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
      {...props}
    />
  );
}

export function Badge({ children, tone = "zinc" }: { children: ReactNode; tone?: "zinc" | "green" | "amber" | "red" | "blue" }) {
  const tones = {
    zinc: "bg-zinc-800 text-zinc-300",
    green: "bg-emerald-900/60 text-emerald-300",
    amber: "bg-amber-900/60 text-amber-300",
    red: "bg-rose-900/70 text-rose-200",
    blue: "bg-sky-900/60 text-sky-300",
  };
  return <span className={`inline-block rounded px-2 py-0.5 text-xs font-semibold ${tones[tone]}`}>{children}</span>;
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs uppercase text-zinc-500">
          <tr>{head.map((h) => <th key={h} className="px-2 py-1 font-medium">{h}</th>)}</tr>
        </thead>
        <tbody className="num divide-y divide-zinc-800/80">{children}</tbody>
      </table>
    </div>
  );
}

export const Td = ({ children, className = "" }: { children: ReactNode; className?: string }) => (
  <td className={`px-2 py-1 ${className}`}>{children}</td>
);

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-zinc-500">{children}</p>;
}

export function Loading() {
  return <p className="py-6 text-center text-sm text-zinc-500">Loading…</p>;
}

export function ErrorNote({ error }: { error: unknown }) {
  return <p className="rounded bg-rose-950 px-3 py-2 text-sm text-rose-200">{error instanceof Error ? error.message : "Something went wrong"}</p>;
}

export function Select({ value, onChange, options, label }: { value: string; onChange: (v: string) => void; options: string[]; label: string }) {
  return (
    <label className="text-xs text-zinc-400">
      {label}{" "}
      <select className="ml-1 rounded bg-zinc-800 px-2 py-1 text-sm text-zinc-100" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">all</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  );
}
