import { useState } from "react";
import { ApiError } from "../api/client";
import { useBreakdown, useCalibration, useCalibrationAction, useChallenger, useCommittee, useCosts, useSummary } from "../api/queries";
import type { CalibrationOut, ChallengerComparison, CommitteeComparison } from "../api/types";
import { KpiTile } from "../components/KpiTile";
import { ReauthDialog } from "../components/ReauthDialog";
import { Reliability } from "../components/Reliability";
import { Badge, Button, Card, Empty, ErrorNote, Loading, Select, Table, Td } from "../components/ui";
import { money, r, signed, tone, utc } from "../lib/format";

const DIMS = ["symbol", "setup_tag", "session", "regime", "direction", "confidence_bucket", "rulebook_version"];
const ratio = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "–" : v.toFixed(digits));
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(0)}%`);
const uplift = (u: CommitteeComparison["vs_analyst"]) =>
  !u ? "–" : `${u.mean_r >= 0 ? "+" : ""}${u.mean_r.toFixed(3)}R [${ratio(u.ci_low, 3)}, ${ratio(u.ci_high, 3)}]`;

/** A contender in shadow beside the analyst and the baseline: the committee (8.4) or the challenger prompt (10.4). */
export function ContenderCard({ data, name, off, intro }: {
  data: CommitteeComparison | undefined; name: string; off: string; intro?: string;
}) {
  if (!data) return <Loading />;
  if (data.bars === 0)
    return <Empty>No {name.toLowerCase()} shadow yet{data.mode === "off" ? ` (${off})` : ""}.</Empty>;
  return (
    <div className="space-y-2">
      <p className="text-sm text-slate-400">
        {intro ? `${intro} ` : ""}{data.bars} paired bars over {data.days.toFixed(1)} days (mode {data.mode}); an arm that
        stayed out earned 0R. Agreement {pct(data.agreement)}.
      </p>
      <Table head={["Arm", "Trades", "Total", "R / trade", "Win rate", "LLM cost"]}>
        {data.arms.map((a) => (
          <tr key={a.arm}><Td>{a.arm}</Td><Td>{a.trades}</Td><Td className={tone(a.total_r)}>{r(a.total_r)}</Td>
            <Td>{ratio(a.mean_r_per_trade, 3)}</Td><Td>{pct(a.win_rate)}</Td><Td>{money(a.cost_usd)}</Td></tr>
        ))}
      </Table>
      <p className="text-sm">
        {name} uplift per bar, net of LLM cost (90% CI): vs analyst <span data-testid="vs-analyst">{uplift(data.vs_analyst)}</span>,
        vs baseline {uplift(data.vs_baseline)}{data.risk_usd ? ` (1R = ${money(data.risk_usd)})` : " (no equity yet: cost not in R)"}.
      </p>
    </div>
  );
}

export function CommitteeCard({ data }: { data: CommitteeComparison | undefined }) {
  return <ContenderCard data={data} name="Committee" off="committee.mode is off" />;
}

export function ChallengerCard({ data }: { data: ChallengerComparison | undefined }) {
  const intro = data?.challenger_prompt ? `${data.challenger_prompt} vs ${data.analyst_prompt}:` : undefined;
  return <ContenderCard data={data} name="Challenger" off="strategy.challenger_prompt_version is not set" intro={intro} />;
}

type CalAction = { version?: number; action: "approve" | "reject" | "fit" };
const brierLine = (m: { brier_before?: number | null; brier_after?: number | null; improvement?: number | null }) =>
  `${ratio(m.brier_before, 4)} → ${ratio(m.brier_after, 4)}${m.improvement === null || m.improvement === undefined ? "" : ` (${(m.improvement * 100).toFixed(1)}% better)`}`;

export function CalibrationView({ data, onAction }: { data: CalibrationOut; onAction: (a: CalAction) => void }) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-400">
        Activation: <b>{data.activation}</b> · a model needs {data.min_samples} finished trades and a ≥5% better held-out
        Brier score. Calibration can raise a confidence over the threshold, so approving needs your password.
      </p>
      <div className="grid gap-3 md:grid-cols-2">
        {data.sources.map((src) => (
          <div key={src.source} className="rounded border border-zinc-800 p-2">
            <div className="mb-1 flex items-center justify-between text-sm">
              <span className="font-semibold capitalize">{src.source}</span>
              <span className="text-zinc-400">{src.n} trades · raw Brier {ratio(src.brier_raw, 4)}</span>
            </div>
            {src.n === 0 ? <Empty>No finished trades yet.</Empty> : <Reliability source={src} />}
            <p className="text-xs text-zinc-400">
              {src.active ? `Active v${src.active.version}: Brier ${brierLine(src.active)}` : "Identity (no active model)."}
            </p>
            {src.candidate && (
              <div className="mt-2 flex items-center justify-between gap-2 rounded bg-zinc-900 p-2 text-xs">
                <span>Candidate v{src.candidate.version}: {src.candidate.n_samples} trades, Brier {brierLine(src.candidate)}</span>
                <span className="flex gap-1">
                  <Button onClick={() => onAction({ version: src.candidate?.version, action: "approve" })}>Approve</Button>
                  <Button variant="ghost" onClick={() => onAction({ version: src.candidate?.version, action: "reject" })}>Reject</Button>
                </span>
              </div>
            )}
          </div>
        ))}
      </div>
      {data.models.length > 0 && (
        <Table head={["Version", "Source", "Status", "Trades", "Held-out Brier", "Fitted", "Decided"]}>
          {data.models.map((m) => (
            <tr key={m.version}><Td>v{m.version}</Td><Td>{m.source}</Td>
              <Td><Badge tone={m.status === "ACTIVE" ? "green" : m.status === "CANDIDATE" ? "amber" : "zinc"}>{m.status}</Badge></Td>
              <Td>{m.n_samples}</Td><Td>{brierLine(m)}</Td><Td>{utc(m.created_at)}</Td>
              <Td>{m.decided_by ? `${m.decided_by} ${utc(m.decided_at)}` : "–"}</Td></tr>
          ))}
        </Table>
      )}
    </div>
  );
}

function CalibrationCard() {
  const cal = useCalibration();
  const act = useCalibrationAction();
  const [reauth, setReauth] = useState<CalAction | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const send = (a: CalAction) =>
    act.mutate(a, {
      onSuccess: () => setSent(`${a.action}${a.version ? ` v${a.version}` : ""} sent to the engine`),
      onError: (e) => { if (e instanceof ApiError && e.needsReauth) setReauth(a); },
    });
  return (
    <Card title="Calibration" actions={<Button variant="ghost" onClick={() => send({ action: "fit" })}>Fit now</Button>}>
      {sent && <p role="status" className="mb-2 text-sm text-emerald-300">{sent}</p>}
      {act.error && !(act.error instanceof ApiError && act.error.needsReauth) && <ErrorNote error={act.error} />}
      {cal.isLoading ? <Loading /> : cal.data ? <CalibrationView data={cal.data} onAction={send} /> : <ErrorNote error={cal.error} />}
      {reauth && <ReauthDialog onClose={() => setReauth(null)} onDone={() => { const a = reauth; setReauth(null); send(a); }} />}
    </Card>
  );
}

export function Analytics() {
  const [days, setDays] = useState(90);
  const [dim, setDim] = useState("symbol");
  const summary = useSummary(days);
  const rows = useBreakdown(dim, days);
  const costs = useCosts(days);
  const committee = useCommittee(days);
  const challenger = useChallenger(days);
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
      <Card title="Challenger prompt vs analyst (shadow A/B)">
        <ChallengerCard data={challenger.data} />
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
      </div>
      <CalibrationCard />
    </div>
  );
}
