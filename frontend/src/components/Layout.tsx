import { Suspense } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useSystem } from "../api/queries";
import { age } from "../lib/format";
import { KillSwitch } from "./KillSwitch";
import { ModeBadge, StatePill } from "./StatePill";
import { Loading } from "./ui";

const NAV = [
  ["/", "Overview"], ["/positions", "Positions"], ["/decisions", "Decisions"], ["/journal", "Journal"],
  ["/analytics", "Analytics"], ["/agents", "Agents & LLM"], ["/settings", "Settings"], ["/system", "System"],
] as const;

export function Header() {
  const { data } = useSystem();
  const engine = data?.engine;
  const beat = data?.heartbeats.find((h) => h.component === "engine");
  return (
    <header className="sticky top-0 z-40 flex flex-wrap items-center gap-3 border-b border-zinc-800 bg-zinc-950/95 px-4 py-2 backdrop-blur">
      <span className="font-bold tracking-tight">aifund</span>
      <StatePill state={engine?.state} stale={data?.engine_stale} />
      <ModeBadge mode={engine?.mode} />
      <span className="text-xs text-zinc-400">heartbeat {beat ? age(beat.age_s) : "–"}</span>
      {engine?.halt_reason && <span className="text-xs text-rose-300">{engine.halt_reason}</span>}
      <div className="ml-auto"><KillSwitch /></div>
    </header>
  );
}

export function Layout() {
  return (
    <div className="min-h-screen">
      <Header />
      <nav className="flex gap-1 overflow-x-auto border-b border-zinc-800 px-3 text-sm">
        {NAV.map(([to, label]) => (
          <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => `whitespace-nowrap px-3 py-2 ${isActive ? "border-b-2 border-sky-400 text-white" : "text-zinc-400 hover:text-zinc-200"}`}>
            {label}
          </NavLink>
        ))}
      </nav>
      <main className="mx-auto max-w-7xl p-4"><Suspense fallback={<Loading />}><Outlet /></Suspense></main>
    </div>
  );
}
