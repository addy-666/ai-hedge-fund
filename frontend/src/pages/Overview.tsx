import { useAccount, useDecisions, useEquity, useLlmUsage, usePositions, useSystem } from "../api/queries";
import { EquityChart } from "../components/Charts";
import { KpiTile } from "../components/KpiTile";
import { Card, Empty, Loading, Table, Td } from "../components/ui";
import { money, pct, signed, tone, utc } from "../lib/format";

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
  const funnel = (recent.data?.items ?? []).reduce<Record<string, number>>((acc, d) => ({ ...acc, [d.outcome]: (acc[d.outcome] ?? 0) + 1 }), {});
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-7">
        <KpiTile label="Equity" value={money(a?.equity)} sub={a?.as_of ? `as of ${utc(a.as_of)}` : "no snapshot yet"} />
        <KpiTile label="Day P&L" value={signed(a?.day_pnl)} className={tone(a?.day_pnl)} />
        <KpiTile label="Drawdown" value={pct(a?.drawdown_pct)} sub={`limit ${pct(limit("drawdown")?.limit_pct, 1)}`} />
        <KpiTile label="Daily loss" value={pct(limit("daily_loss")?.used_pct)} sub={`limit ${pct(limit("daily_loss")?.limit_pct, 1)}`} />
        <KpiTile label="Heat" value={pct(a?.heat_pct)} sub={`limit ${pct(limit("portfolio_heat")?.limit_pct, 1)}`} />
        <KpiTile label="Open positions" value={positions.data?.length ?? "–"} />
        <KpiTile label="LLM today" value={`$${spent.toFixed(2)}`} sub={system.data ? `budget $${system.data.llm_budget_usd}` : undefined} />
      </div>
      <Card title="Equity (30 days)">
        {equity.isLoading ? <Loading /> : equity.data?.length ? <EquityChart points={equity.data} /> : <Empty>No equity snapshots yet.</Empty>}
      </Card>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Latest decisions: funnel">
          {Object.keys(funnel).length ? (
            <Table head={["Outcome", "Count"]}>
              {Object.entries(funnel).sort((x, y) => y[1] - x[1]).map(([k, v]) => <tr key={k}><Td>{k}</Td><Td>{v}</Td></tr>)}
            </Table>
          ) : <Empty>No decisions yet.</Empty>}
        </Card>
        <Card title="Latest alerts & heartbeats">
          <Table head={["Component", "Status", "Age"]}>
            {(system.data?.heartbeats ?? []).map((h) => (
              <tr key={h.component}><Td>{h.component}</Td><Td className={h.status === "error" ? "text-rose-300" : ""}>{h.status}</Td><Td>{Math.round(h.age_s)}s</Td></tr>
            ))}
          </Table>
        </Card>
      </div>
    </div>
  );
}
