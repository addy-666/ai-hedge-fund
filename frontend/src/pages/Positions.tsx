import { Fragment, useState } from "react";
import { useClosePosition, usePositions } from "../api/queries";
import type { PositionOut } from "../api/types";
import { Dialog } from "../components/Dialog";
import { Button, Card, Empty, ErrorNote, Loading, ROW_CLICK, ROW_SELECTED, Table, Td } from "../components/ui";
import { money, r, tone, utc } from "../lib/format";

export function Positions() {
  const positions = usePositions();
  const close = useClosePosition();
  const [confirm, setConfirm] = useState<PositionOut | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  if (positions.isLoading) return <Loading />;
  if (positions.error) return <ErrorNote error={positions.error} />;
  const rows = positions.data ?? [];
  return (
    <Card title={`Open positions (${rows.length})`}>
      {rows.length === 0 ? <Empty>No open engine positions.</Empty> : (
        <Table head={["Symbol", "Side", "Volume", "Entry", "SL", "TP", "Last", "R now", "Age", "Setup", ""]}>
          {rows.map((p) => (
            <Fragment key={p.trade_id}>
              <tr className={`${ROW_CLICK} ${expanded === p.trade_id ? ROW_SELECTED : ""}`} onClick={() => setExpanded(expanded === p.trade_id ? null : p.trade_id)}>
                <Td className="font-medium text-zinc-100">{p.symbol}{p.status === "ORPHAN_OPEN" && <span className="ml-1 text-xs text-amber-300">orphan</span>}</Td>
                <Td className={p.side === "BUY" ? "text-emerald-400" : "text-rose-400"}>{p.side}</Td>
                <Td>{p.volume}</Td><Td>{money(p.open_price)}</Td><Td>{money(p.sl)}</Td><Td>{money(p.tp)}</Td>
                <Td>{money(p.last_price)}</Td><Td className={tone(p.r_now)}>{r(p.r_now)}</Td>
                <Td>{p.age_minutes}m</Td><Td>{p.setup_tag ?? "–"}</Td>
                <Td><Button variant="danger" onClick={(e) => { e.stopPropagation(); setConfirm(p); }}>Close</Button></Td>
              </tr>
              {expanded === p.trade_id && (
                <tr className="bg-zinc-950/60"><Td colSpan={11} className="py-2 text-xs text-zinc-400">
                  opened {utc(p.open_time)} · position #{p.position_id}{p.thesis ? ` · ${p.thesis}` : ""}
                </Td></tr>
              )}
            </Fragment>
          ))}
        </Table>
      )}
      {close.data && <p className="mt-2 text-xs text-zinc-400" role="status">close sent (command {close.data.command_id})</p>}
      {confirm && (
        <Dialog title={`Close ${confirm.symbol} #${confirm.position_id}?`} onClose={() => setConfirm(null)}
                actions={<Button variant="danger" onClick={() => { close.mutate(confirm.position_id); setConfirm(null); }}>Close at market</Button>}>
          {confirm.side} {confirm.volume} opened at {confirm.open_price}; closes at the market now.
        </Dialog>
      )}
    </Card>
  );
}
