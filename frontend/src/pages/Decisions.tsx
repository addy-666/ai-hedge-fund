import { useState } from "react";
import { useDecision, useDecisions } from "../api/queries";
import { DecisionTrace } from "../components/DecisionTrace";
import { Badge, Button, Card, Empty, ErrorNote, Loading, Select, Table, Td, ROW_CLICK, ROW_SELECTED } from "../components/ui";
import { utc } from "../lib/format";

const OUTCOMES = ["ORDERED", "RISK_REJECTED", "BELOW_THRESHOLD", "RULE_BLOCKED", "HOLD", "INVALID", "SHADOW", "DRY_RUN", "NO_SETUP", "SKIPPED", "ERROR"];

export function Decisions() {
  const [outcome, setOutcome] = useState("");
  const [symbol, setSymbol] = useState("");
  const [cursors, setCursors] = useState<string[]>([]);
  const [open, setOpen] = useState<string | undefined>();
  const page = useDecisions({ outcome, symbol }, cursors.at(-1));
  const dossier = useDecision(open);
  const items = page.data?.items ?? [];
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_1.1fr]">
      <Card title="Decisions" actions={
        <div className="flex gap-3">
          <Select label="outcome" value={outcome} onChange={(v) => { setOutcome(v); setCursors([]); }} options={OUTCOMES} />
          <label className="label flex items-center gap-1.5 text-[0.625rem] text-zinc-500">symbol <input className="w-24 bg-zinc-950 px-1.5 py-0.5 normal-case tracking-normal" value={symbol} onChange={(e) => { setSymbol(e.target.value); setCursors([]); }} /></label>
        </div>}>
        {page.isLoading ? <Loading /> : page.error ? <ErrorNote error={page.error} /> : items.length === 0 ? <Empty>No decisions.</Empty> : (
          <Table head={["Bar (UTC)", "Symbol", "Outcome", "Reason", "Conf"]}>
            {items.map((d) => (
              <tr key={d.id} onClick={() => setOpen(d.id)} className={`${ROW_CLICK} ${open === d.id ? ROW_SELECTED : ""}`}>
                <Td>{utc(d.bar_time)}</Td><Td>{d.symbol}</Td>
                <Td><Badge tone={d.outcome === "ORDERED" ? "green" : d.outcome === "ERROR" ? "red" : "zinc"}>{d.outcome}</Badge></Td>
                <Td className="text-xs text-zinc-400">{d.reason_code ?? ""}</Td><Td>{d.final_confidence ?? "–"}</Td>
              </tr>
            ))}
          </Table>
        )}
        <div className="mt-2 flex gap-2">
          <Button variant="ghost" disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0, -1))}>Newer</Button>
          <Button variant="ghost" disabled={!page.data?.next_cursor} onClick={() => page.data?.next_cursor && setCursors([...cursors, page.data.next_cursor])}>Older</Button>
        </div>
      </Card>
      <Card title="Pipeline trace" className="self-start lg:sticky lg:top-24">
        {!open ? <Empty>Select a decision.</Empty> : dossier.isLoading ? <Loading /> : dossier.data ? <DecisionTrace d={dossier.data} /> : <ErrorNote error={dossier.error} />}
      </Card>
    </div>
  );
}
