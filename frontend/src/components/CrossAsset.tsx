import { useCrossAsset } from "../api/queries";
import type { CrossAssetOut } from "../api/types";
import { Badge, Card, Empty, ErrorNote, Loading, Table, Td } from "./ui";
import { utc } from "../lib/format";

const num = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "–" : `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;

/** What each traded symbol's newest decision saw of the other markets (roadmap 10.6): correlation, 24-bar move. */
export function CrossAssetView({ data }: { data: CrossAssetOut }) {
  if (!data.enabled) return <Empty>Cross-asset features are off (cross_asset.enabled).</Empty>;
  return (
    <div className="space-y-2">
      <p className="text-sm text-zinc-400">
        {data.instruments.length} instruments on {data.timeframe} bars
        {data.references.length > 0 ? ` (data only: ${data.references.join(", ")})` : ""}; a market whose last bar is
        older than {data.max_age_minutes} min counts as shut. Each cell: correlation over 100 bars · 24-bar move (σ).
      </p>
      {data.rows.length === 0 ? (
        <Empty>No decision with cross-asset features yet.</Empty>
      ) : (
        <Table head={["Symbol", "Decided", ...data.instruments]}>
          {data.rows.map((row) => {
            const cells = new Map(row.cells.map((c) => [c.instrument, c]));
            return (
              <tr key={row.symbol}>
                <Td className="font-semibold">{row.symbol}</Td>
                <Td className="text-xs text-zinc-400">{utc(row.bar_time)}</Td>
                {data.instruments.map((inst) => {
                  const c = cells.get(inst);
                  if (!c) return <Td key={inst} className="text-zinc-600">self</Td>;
                  if (!c.open) return <Td key={inst}><Badge tone="zinc">shut</Badge></Td>;
                  return (
                    <Td key={inst} className="text-xs">
                      <span title="correlation of 1-bar returns over 100 bars">{num(c.corr100)}</span>
                      {" · "}
                      <span title="24-bar move in standard deviations" className={(c.ret24_z ?? 0) >= 0 ? "text-emerald-300" : "text-rose-300"}>
                        {num(c.ret24_z, 1)}σ
                      </span>
                      {c.ema_stack ? <span className="ml-1 text-zinc-500">{c.ema_stack}</span> : null}
                    </Td>
                  );
                })}
              </tr>
            );
          })}
        </Table>
      )}
    </div>
  );
}

export function CrossAssetCard() {
  const q = useCrossAsset();
  return (
    <Card title="Cross-asset">
      {q.isLoading ? <Loading /> : q.data ? <CrossAssetView data={q.data} /> : <ErrorNote error={q.error} />}
    </Card>
  );
}
