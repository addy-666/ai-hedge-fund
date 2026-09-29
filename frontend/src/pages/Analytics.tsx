import { useState } from "react";
import { useBreakdown, useCosts, useSummary } from "../api/queries";
import { KpiTile } from "../components/KpiTile";
import { Card, Empty, Loading, Select, Table, Td } from "../components/ui";
import { money, r, signed, tone } from "../lib/format";

const DIMS = ["symbol", "setup_tag", "session", "regime", "direction", "confidence_bucket", "rulebook_version"];
const ratio = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "–" : v.toFixed(digits));

export function Analytics() {
  const [days, setDays] = useState(90);
  const [dim, setDim] = useState("symbol");
  const summary = useSummary(days);
  const rows = useBreakdown(dim, days);
  const costs = useCosts(days);
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
