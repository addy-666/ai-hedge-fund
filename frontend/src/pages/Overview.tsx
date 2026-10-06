import { Link } from "react-router-dom";
import { useAccount, useDecisions, useEquity, useLlmUsage, usePositions, useSystem } from "../api/queries";
import type { EquityPoint } from "../api/types";
import { EquityChart } from "../components/Charts";
import { KpiTile } from "../components/KpiTile";
import { Card, Empty, Led, Loading, Meter, Table, Td } from "../components/ui";
import { age, money, pct, r, signed, tone, utc } from "../lib/format";

/** used / limit as a fraction for a meter; undefined when either side is missing. */
const usage = (used: string | number | null | undefined, limit: string | number | null | undefined) => {
  const u = Number(used);
  const l = Number(limit);
  return Number.isFinite(u) && Number.isFinite(l) && l > 0 ? Math.abs(u) / l : undefined;
};

function EquityStats({ points }: { points: EquityPoint[] }) {
  if (points.length < 2) return null;
  const values = points.map((p) => Number(p.equity));
  const first = values[0] ?? 0;
  const last = values.at(-1) ?? 0;
  const change = last - first;
  const items: [string, string, string][] = [
    ["chg", signed(change), tone(change)],
    ["chg %", first ? signed((change / first) * 100, 2, "%") : "–", tone(change)],
    ["hi", money(Math.max(...values)), "text-zinc-300"],
    ["lo", money(Math.min(...values)), "text-zinc-300"],
  ];
  return (
    <dl className="num flex flex-wrap gap-x-4 text-xs">
      {items.map(([k, v, cls]) => (
        <div key={k} className="flex gap-1.5"><dt className="text-zinc-500 uppercase">{k}</dt><dd className={cls}>{v}</dd></div>
      ))}
    </dl>
  );
}

export function Overview() {
  const account = useAccount();
  const equity = useEquity(30);
  const positions = usePositions();
  const system = useSystem();
  const llm = useLlmUsage(1);
  const recent = useDecisions({});
  const a = account.data;
  const limit = (name: string) => a?.limits.find((l) => l.name === name);
  const spent = llm.data?.reduce((sum, row) => sum + Number(row.cost_usd), 0) ?? 0;
  const budget = Number(system.data?.llm_budget_usd ?? 0);
  const funnel = (recent.data?.items ?? []).reduce<Record<string, number>>((acc, d) => ({ ...acc, [d.outcome]: (acc[d.outcome] ?? 0) + 1 }), {});
  const funnelRows = Object.entries(funnel).sort((x, y) => y[1] - x[1]);
  const funnelMax = Math.max(1, ...funnelRows.map(([, v]) => v));
  const open = positions.data ?? [];
  return (
    <div className="space-y-3 md:space-y-4">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 md:gap-3 xl:grid-cols-7">
        <KpiTile label="Equity" value={money(a?.equity)} sub={a?.as_of ? `as of ${utc(a.as_of)}` : "no snapshot yet"} />
        <KpiTile label="Day P&L" value={signed(a?.day_pnl)} className={tone(a?.day_pnl)} sub={a?.balance ? `balance ${money(a.balance)}` : undefined} />
        <KpiTile label="Drawdown" value={pct(a?.drawdown_pct)} meter={usage(a?.drawdown_pct, limit("drawdown")?.limit_pct)} sub={`limit ${pct(limit("drawdown")?.limit_pct, 1)}`} />
        <KpiTile label="Daily loss" value={pct(limit("daily_loss")?.used_pct)} meter={usage(limit("daily_loss")?.used_pct, limit("daily_loss")?.limit_pct)} sub={`limit ${pct(limit("daily_loss")?.limit_pct, 1)}`} />
        <KpiTile label="Heat" value={pct(a?.heat_pct)} meter={usage(a?.heat_pct, limit("portfolio_heat")?.limit_pct)} sub={`limit ${pct(limit("portfolio_heat")?.limit_pct, 1)}`} />
        <KpiTile label="Open positions" value={positions.data?.length ?? "–"} sub={a?.open_risk_money ? `at risk ${money(a.open_risk_money)}` : undefined} />
        <KpiTile label="LLM today" value={`$${spent.toFixed(2)}`} meter={budget ? spent / budget : undefined} sub={system.data ? `budget $${system.data.llm_budget_usd}` : undefined} />
      </div>

      <div className="grid gap-3 md:gap-4 xl:grid-cols-3">
        <Card title="Equity (30 days)" className="xl:col-span-2" actions={equity.data ? <EquityStats points={equity.data} /> : undefined}>
          {equity.isLoading ? <Loading /> : equity.data?.length ? <EquityChart points={equity.data} /> : <Empty>No equity snapshots yet.</Empty>}
        </Card>
        <Card title="Open book" actions={<Link to="/positions" className="font-mono text-[0.6875rem] text-sky-400 hover:text-sky-200">all →</Link>}>
          {positions.isLoading ? <Loading /> : open.length === 0 ? <Empty>Flat: no open engine positions.</Empty> : (
            <Table head={["Symbol", "Side", "Vol", "R now", "Age"]}>
              {open.map((p) => (
                <tr key={p.trade_id}>
                  <Td className="font-medium text-zinc-100">{p.symbol}</Td>
                  <Td className={p.side === "BUY" ? "text-emerald-400" : "text-rose-400"}>{p.side}</Td>
                  <Td>{p.volume}</Td>
                  <Td className={tone(p.r_now)}>{r(p.r_now)}</Td>
                  <Td className="text-zinc-500">{p.age_minutes}m</Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </div>

      <div className="grid gap-3 md:grid-cols-2 md:gap-4">
        <Card title="Latest decisions: funnel">
          {funnelRows.length ? (
            <ul className="space-y-1.5" aria-label="decision outcomes">
              {funnelRows.map(([k, v]) => (
                <li key={k} className="num grid grid-cols-[9rem_1fr_2.5rem] items-center gap-3 text-xs">
                  <span className={k === "ORDERED" ? "text-emerald-300" : k === "ERROR" ? "text-rose-300" : "text-zinc-300"}>{k}</span>
                  <span className="h-2 bg-zinc-800/80">
                    <span className={`block h-full ${k === "ORDERED" ? "bg-emerald-400/80" : k === "ERROR" ? "bg-rose-500/80" : "bg-sky-500/50"}`} style={{ width: `${(v / funnelMax) * 100}%` }} />
                  </span>
                  <span className="text-right text-zinc-100">{v}</span>
                </li>
              ))}
            </ul>
          ) : <Empty>No decisions yet.</Empty>}
        </Card>
        <Card title="Latest alerts & heartbeats">
          <Table head={["Component", "Status", "Age"]}>
            {(system.data?.heartbeats ?? []).map((h) => {
              const bad = ["error", "diff", "failed"].includes(h.status);
              return (
                <tr key={h.component}>
                  <Td>{h.component}</Td>
                  <Td className={bad ? "text-rose-300" : "text-zinc-300"}>
                    <span className="inline-flex items-center gap-1.5"><Led tone={bad ? "red" : h.age_s > 120 ? "amber" : "green"} />{h.status}</span>
                  </Td>
                  <Td className="text-zinc-400">{age(h.age_s)}</Td>
                </tr>
              );
            })}
          </Table>
          {budget > 0 && (
            <div className="mt-3 flex items-center gap-2 border-t border-zinc-800 pt-2 font-mono text-[0.6875rem] text-zinc-500">
              LLM budget <Meter fraction={spent / budget} className="flex-1" /> {Math.round((spent / budget) * 100)}%
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
