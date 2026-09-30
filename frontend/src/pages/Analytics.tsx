import { useState } from "react";
import { useBreakdown, useCommittee, useCosts, useSummary } from "../api/queries";
import type { CommitteeComparison } from "../api/types";
import { KpiTile } from "../components/KpiTile";
import { Card, Empty, Loading, Select, Table, Td } from "../components/ui";
import { money, r, signed, tone } from "../lib/format";

const DIMS = ["symbol", "setup_tag", "session", "regime", "direction", "confidence_bucket", "rulebook_version"];
const ratio = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "–" : v.toFixed(digits));
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(0)}%`);
const uplift = (u: CommitteeComparison["vs_analyst"]) =>
  !u ? "–" : `${u.mean_r >= 0 ? "+" : ""}${u.mean_r.toFixed(3)}R [${ratio(u.ci_low, 3)}, ${ratio(u.ci_high, 3)}]`;

export function CommitteeCard({ data }: { data: CommitteeComparison | undefined }) {
  if (!data) return <Loading />;
  if (data.bars === 0)
    return <Empty>No committee shadow yet{data.mode === "off" ? " (committee.mode is off)" : ""}.</Empty>;
  return (
    <div className="space-y-2">
      <p className="text-sm text-slate-400">
        {data.bars} paired bars over {data.days.toFixed(1)} days (mode {data.mode}); an arm that stayed out earned 0R.
        Agreement {pct(data.agreement)}.
      </p>
      <Table head={["Arm", "Trades", "Total", "R / trade", "Win rate", "LLM cost"]}>
        {data.arms.map((a) => (
          <tr key={a.arm}><Td>{a.arm}</Td><Td>{a.trades}</Td><Td className={tone(a.total_r)}>{r(a.total_r)}</Td>
            <Td>{ratio(a.mean_r_per_trade, 3)}</Td><Td>{pct(a.win_rate)}</Td><Td>{money(a.cost_usd)}</Td></tr>
        ))}
      </Table>
      <p className="text-sm">
        Committee uplift per bar, net of LLM cost (90% CI): vs analyst <span data-testid="vs-analyst">{uplift(data.vs_analyst)}</span>,
        vs baseline {uplift(data.vs_baseline)}{data.risk_usd ? ` (1R = ${money(data.risk_usd)})` : " (no equity yet: cost not in R)"}.
      </p>
    </div>
  );
}

export function Analytics() {
  const [days, setDays] = useState(90);
  const [dim, setDim] = useState("symbol");
  const summary = useSummary(days);
  const rows = useBreakdown(dim, days);
  const costs = useCosts(days);
  const committee = useCommittee(days);
  const s = summary.data;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Select label="window (days)" value={String(days)} onChange={(v) => setDays(Number(v) || 90)} options={["7", "30", "90", "365"]} />
      </div>
      {summary.isLoading ? <Loading /> : s && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <KpiTile label="Trades" value={s.trades} sub={`${ratio(s.trades_per_day)} / day`} />
          <KpiTile label="Net" value={signed(s.net_pnl)} className={tone(s.net_pnl)} sub={`${r(s.r_total)} total`} />
          <KpiTile label="Expectancy" value={r(s.expectancy_r)} sub={`win rate ${s.win_rate === null || s.win_rate === undefined ? "–" : `${(s.win_rate * 100).toFixed(0)}%`}`} />
          <KpiTile label="Profit factor" value={ratio(s.profit_factor)} sub={`Sharpe (daily) ${ratio(s.sharpe_daily)}`} />
          <KpiTile label="Max drawdown" value={money(s.max_drawdown)} sub={`avg cost ${money(s.avg_cost)}`} />
        </div>
      )}
      <Card title="Breakdown" actions={<Select label="by" value={dim} onChange={(v) => setDim(v || "symbol")} options={DIMS} />}>
        {rows.isLoading ? <Loading /> : (rows.data ?? []).length === 0 ? <Empty>No closed trades in the window.</Empty> : (
          <Table head={[dim, "Trades", "Net", "Expectancy", "Win rate"]}>
            {(rows.data ?? []).map((b) => (
              <tr key={b.key}><Td>{b.key}</Td><Td>{b.trades}</Td><Td className={tone(b.net_pnl)}>{signed(b.net_pnl)}</Td>
                <Td className={tone(b.expectancy_r)}>{r(b.expectancy_r)}</Td><Td>{b.win_rate === null || b.win_rate === undefined ? "–" : `${(b.win_rate * 100).toFixed(0)}%`}</Td></tr>
            ))}
          </Table>
        )}
      </Card>
      <Card title="Committee vs analyst (shadow)">
        <CommitteeCard data={committee.data} />
      </Card>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Costs">
          {costs.data && (
            <Table head={["Commission", "Swap", "Fee", "Per trade", "Slippage in / out (pts)"]}>
              <tr><Td>{money(costs.data.commission)}</Td><Td>{money(costs.data.swap)}</Td><Td>{money(costs.data.fee)}</Td>
                <Td>{money(costs.data.per_trade)}</Td><Td>{ratio(costs.data.avg_entry_slippage_points, 1)} / {ratio(costs.data.avg_exit_slippage_points, 1)}</Td></tr>
            </Table>
          )}
        </Card>
        <Card title="Calibration"><Empty>Confidence calibration arrives with Phase 8.</Empty></Card>
      </div>
    </div>
  );
}
