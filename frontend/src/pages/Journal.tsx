import { useState } from "react";
import { useTrade, useTrades, useVirtualTrades } from "../api/queries";
import { PriceChart } from "../components/Charts";
import { DecisionTrace } from "../components/DecisionTrace";
import { Badge, Button, Card, Empty, ErrorNote, Loading, Select, Table, Td, ROW_CLICK, ROW_SELECTED } from "../components/ui";
import { money, r, signed, tone, utc } from "../lib/format";

export function Journal() {
  const [outcome, setOutcome] = useState("");
  const [cursors, setCursors] = useState<string[]>([]);
  const [open, setOpen] = useState<string | undefined>();
  const trades = useTrades({ outcome }, cursors.at(-1));
  const shadows = useVirtualTrades();
  const items = trades.data?.items ?? [];
  return (
    <div className="space-y-4">
      <Card title="Closed trades" actions={<Select label="outcome" value={outcome} onChange={(v) => { setOutcome(v); setCursors([]); }} options={["WIN", "LOSS", "BREAKEVEN"]} />}>
        {trades.isLoading ? <Loading /> : trades.error ? <ErrorNote error={trades.error} /> : items.length === 0 ? <Empty>No closed trades yet.</Empty> : (
          <Table head={["Closed (UTC)", "Symbol", "Side", "Setup", "Net", "R", "MAE", "MFE", "Reason"]}>
            {items.map((t) => (
              <tr key={t.id} onClick={() => setOpen(t.id)} className={`${ROW_CLICK} ${open === t.id ? ROW_SELECTED : ""}`}>
                <Td>{utc(t.close_time)}</Td><Td className="font-medium text-zinc-100">{t.symbol}</Td><Td className={t.side === "BUY" ? "text-emerald-400" : "text-rose-400"}>{t.side}</Td><Td>{t.setup_tag ?? "–"}</Td>
                <Td className={tone(t.net_pnl)}>{signed(t.net_pnl)}</Td><Td className={tone(t.r_multiple)}>{r(t.r_multiple)}</Td>
                <Td>{r(t.mae_r)}</Td><Td>{r(t.mfe_r)}</Td><Td>{t.close_reason ?? "–"}</Td>
              </tr>
            ))}
          </Table>
        )}
        <div className="mt-2 flex gap-2">
          <Button variant="ghost" disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0, -1))}>Newer</Button>
          <Button variant="ghost" disabled={!trades.data?.next_cursor} onClick={() => trades.data?.next_cursor && setCursors([...cursors, trades.data.next_cursor])}>Older</Button>
        </div>
      </Card>
      {open && <TradeDossierView id={open} />}
      <Card title="Virtual trades (blocked signals and G-LLM shadows)">
        {(shadows.data?.items ?? []).length === 0 ? <Empty>None yet.</Empty> : (
          <Table head={["Entry (UTC)", "Arm", "Symbol", "Side", "Status", "Exit", "R", "Blocked by"]}>
            {(shadows.data?.items ?? []).map((v) => (
              <tr key={v.id}><Td>{utc(v.entry_time)}</Td><Td>{v.arm}</Td><Td>{v.symbol}</Td><Td>{v.side}</Td><Td>{v.status}</Td>
                <Td>{v.exit_reason ?? "–"}</Td><Td className={tone(v.r_multiple)}>{r(v.r_multiple)}</Td><Td>{v.blocked_by ?? "–"}</Td></tr>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}

function TradeDossierView({ id }: { id: string }) {
  const { data, isLoading, error } = useTrade(id);
  if (isLoading) return <Loading />;
  if (!data) return <ErrorNote error={error} />;
  const bars = data.bars ?? [];
  const deals = data.deals ?? [];
  const costs = [["commission", data.commission], ["swap", data.swap], ["fee", data.fee]] as const;
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title={`${data.symbol} ${data.side} · ${utc(data.open_time)} → ${utc(data.close_time)}`}>
        {bars.length ? (
          <PriceChart bars={bars} markers={data.markers ?? []}
                      levels={[{ price: data.initial_sl, label: "SL", color: "down" }, { price: data.initial_tp, label: "TP", color: "up" }]} />
        ) : <Empty>No cached bars for this trade.</Empty>}
        <dl className="num my-3 grid grid-cols-3 gap-x-3 gap-y-2 border-y border-zinc-800 py-3 text-sm">
          <div><dt className="label text-[0.625rem] text-amber-400/70">Net</dt><dd className={tone(data.net_pnl)}>{signed(data.net_pnl)}</dd></div>
          <div><dt className="label text-[0.625rem] text-amber-400/70">R</dt><dd>{r(data.r_multiple)}</dd></div>
          <div><dt className="label text-[0.625rem] text-amber-400/70">Held</dt><dd>{data.holding_minutes ?? "–"} min</dd></div>
          {costs.map(([k, v]) => <div key={k}><dt className="label text-[0.625rem] text-amber-400/70">{k}</dt><dd>{money(v)}</dd></div>)}
          <div><dt className="label text-[0.625rem] text-amber-400/70">Slippage in/out</dt><dd>{data.entry_slippage_points ?? "–"} / {data.exit_slippage_points ?? "–"} pts</dd></div>
        </dl>
        <Table head={["Deal", "Time", "Entry", "Volume", "Price", "Profit"]}>
          {deals.map((d) => <tr key={d.ticket}><Td>{d.ticket}</Td><Td>{utc(d.time_utc)}</Td><Td>{d.entry}</Td><Td>{d.volume}</Td><Td>{money(d.price)}</Td><Td>{money(d.profit)}</Td></tr>)}
        </Table>
      </Card>
      <div className="space-y-4">
        <Card title="Review (thesis vs outcome)">
          {data.review ? (
            <div className="space-y-1 text-sm">
              <p>
                <Badge tone={data.review.thesis_verdict === "CORRECT" ? "green" : data.review.thesis_verdict === "WRONG" ? "red" : "zinc"}>{data.review.thesis_verdict}</Badge>{" "}
                execution {data.review.execution_quality}/5
              </p>
              <p>{data.review.tags.map((t) => <Badge key={t}>{t}</Badge>)}</p>
              <p className="text-zinc-300">{data.review.lesson}</p>
            </div>
          ) : <Empty>Review {data.review_status.toLowerCase()}.</Empty>}
        </Card>
        <Card title="Decision">{data.decision ? <DecisionTrace d={data.decision} /> : <Empty>Orphan: no decision.</Empty>}</Card>
      </div>
    </div>
  );
}
