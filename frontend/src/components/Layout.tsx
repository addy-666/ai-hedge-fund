import { Suspense, useEffect, useState, useSyncExternalStore } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useSystem } from "../api/queries";
import { age } from "../lib/format";
import { link } from "../ws/socket";
import { KillSwitch } from "./KillSwitch";
import { ThemeToggle } from "./ThemeToggle";
import { ModeBadge, StatePill } from "./StatePill";
import { Led, Loading, Meter } from "./ui";

const NAV = [
  ["/", "Overview"], ["/positions", "Positions"], ["/decisions", "Decisions"], ["/journal", "Journal"], ["/learning", "Learning Lab"],
  ["/analytics", "Analytics"], ["/agents", "Agents & LLM"], ["/settings", "Settings"], ["/system", "System"],
] as const;

function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const iso = now.toISOString();
  return (
    <span className="num hidden items-baseline gap-2 text-xs sm:inline-flex" aria-label="UTC time">
      <span className="text-zinc-500">{iso.slice(0, 10)}</span>
      <span className="text-amber-300">{iso.slice(11, 19)}</span>
      <span className="label text-[0.625rem] text-zinc-500">UTC</span>
    </span>
  );
}

export function Header() {
  const { data } = useSystem();
  const engine = data?.engine;
  const beat = data?.heartbeats.find((h) => h.component === "engine");
  const beatTone = !beat ? "zinc" : beat.age_s > 30 ? "red" : beat.age_s > 20 ? "amber" : "green";
  return (
    <header className="border-b border-zinc-800 bg-zinc-950">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3 py-1.5">
        <span className="flex items-center gap-2 select-none">
          <span className="bg-amber-400 px-1.5 py-px font-mono text-sm font-semibold tracking-tight text-zinc-950 shadow-[var(--glow-amber)]">aifund</span>
          <span className="label hidden text-[0.625rem] text-zinc-500 md:inline">Quant ops terminal</span>
        </span>
        <span aria-hidden className="hidden h-5 w-px bg-zinc-800 md:block" />
        <StatePill state={engine?.state} stale={data?.engine_stale} />
        <ModeBadge mode={engine?.mode} />
        <span className="num inline-flex items-center gap-1.5 text-xs text-zinc-400">
          <Led tone={beatTone} pulse={beatTone === "green"} />heartbeat {beat ? age(beat.age_s) : "–"}
        </span>
        {engine?.halt_reason && (
          <span className="num rounded-sm border border-rose-700/70 bg-rose-950/70 px-2 py-0.5 text-xs text-rose-300">{engine.halt_reason}</span>
        )}
        <div className="ml-auto flex items-center gap-4">
          <Clock />
          <ThemeToggle />
          <KillSwitch />
        </div>
      </div>
    </header>
  );
}

/** Function-key style tabs; Alt+1…9 jumps to a page (ignored while typing in a field). */
function Nav() {
  const navigate = useNavigate();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const target = e.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable]")) return;
      const n = /^Digit([1-9])$/.exec(e.code)?.[1];
      const entry = n ? NAV[Number(n) - 1] : undefined;
      if (entry) {
        e.preventDefault();
        void navigate(entry[0]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);
  return (
    <nav className="flex overflow-x-auto border-b border-zinc-800 bg-zinc-950">
      {NAV.map(([to, label], i) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/"}
          title={`Alt+${i + 1}`}
          className={({ isActive }) =>
            `group relative flex items-center gap-2 whitespace-nowrap border-r border-zinc-800/80 px-3 py-1.5 font-mono text-xs tracking-wide transition-colors ${
              isActive
                ? "bg-gradient-to-b from-sky-900/40 to-transparent text-zinc-50 after:absolute after:inset-x-0 after:bottom-0 after:h-px after:bg-sky-400 after:shadow-[0_0_8px_rgb(47_224_245/0.9)]"
                : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
            }`
          }
        >
          {({ isActive }) => (
            <>
              <span aria-hidden className={`num rounded-[1px] px-1 text-[0.625rem] leading-4 ${isActive ? "bg-amber-400 text-zinc-950" : "bg-zinc-800 text-amber-400/80 group-hover:bg-zinc-700"}`}>
                {i + 1}
              </span>
              {label}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}

function StatusBar() {
  const { data } = useSystem();
  const live = useSyncExternalStore(link.subscribe, link.get);
  const spent = Number(data?.llm_spent_today_usd ?? 0);
  const budget = Number(data?.llm_budget_usd ?? 0);
  return (
    <footer className="num fixed inset-x-0 bottom-0 z-40 flex h-6 items-center gap-4 overflow-hidden border-t border-zinc-800 bg-zinc-950 px-3 text-[0.6875rem] text-zinc-500">
      <span className="inline-flex items-center gap-1.5" title="live event stream">
        <Led tone={live ? "green" : "red"} pulse={!live} />
        <span className={live ? "text-emerald-300" : "text-rose-300"}>{live ? "LINK" : "NO LINK"}</span>
      </span>
      {data && (
        <>
          <span className="inline-flex items-center gap-1.5">
            LLM <span className="text-zinc-300">${spent.toFixed(2)}</span>/${budget.toFixed(2)}
            <Meter fraction={budget ? spent / budget : 0} className="w-12" />
          </span>
          <span className="hidden md:inline">CFG <span className="text-zinc-300">{data.versions.config_sha256?.slice(0, 8) ?? "–"}</span></span>
          <span className="hidden md:inline">FS <span className="text-zinc-300">v{data.versions.feature_set}</span></span>
          <span className="hidden md:inline">RB <span className="text-zinc-300">v{data.versions.rulebook}</span></span>
        </>
      )}
      <span className="ml-auto hidden text-zinc-600 lg:inline">ALT+1…9 NAVIGATE · ALL TIMES UTC</span>
    </footer>
  );
}

export function Layout() {
  return (
    <div className="min-h-screen pb-8">
      <div className="sticky top-0 z-40">
        <Header />
        <Nav />
      </div>
      <main className="mx-auto max-w-[1600px] p-3 md:p-4"><Suspense fallback={<Loading />}><Outlet /></Suspense></main>
      <StatusBar />
    </div>
  );
}
