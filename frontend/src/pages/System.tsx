import { useState } from "react";
import { useLogs, useSystem } from "../api/queries";
import { EngineControls } from "../components/EngineControls";
import { CrossAssetCard } from "../components/CrossAsset";
import { RolloutCard } from "../components/Rollout";
import { Card, Loading, Select, Table, Td } from "../components/ui";
import { age, utc } from "../lib/format";

export function System() {
  const system = useSystem();
  const [level, setLevel] = useState("");
  const logs = useLogs(level || undefined);
  const s = system.data;
  if (system.isLoading || !s) return <Loading />;
  return (
    <div className="space-y-4">
      {s.evidence_override && (
        <p role="alert" className="rounded border border-amber-700 bg-amber-950/50 p-2 text-sm text-amber-200">
          Evidence override ON: DEMO orders run for detectors without E1 evidence and an analyst without a
          G-LLM sign-off (<code>engine.demo_orders_without_evidence</code>). Never valid in LIVE.
        </p>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Engine">
          <dl className="num grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-[0.8125rem]">
            <dt className="label pt-0.5 text-[0.625rem] text-amber-400/70">State</dt><dd>{s.engine?.state ?? "no engine row"}{s.engine_stale ? " (stale)" : ""}</dd>
            <dt className="label pt-0.5 text-[0.625rem] text-amber-400/70">Mode</dt><dd>{s.engine?.mode ?? "–"}</dd>
            <dt className="label pt-0.5 text-[0.625rem] text-amber-400/70">Halt reason</dt><dd>{s.engine?.halt_reason ?? "–"}</dd>
            <dt className="label pt-0.5 text-[0.625rem] text-amber-400/70">Updated</dt><dd>{utc(s.engine?.updated_at)}</dd>
            <dt className="label pt-0.5 text-[0.625rem] text-amber-400/70">LLM today</dt><dd>${s.llm_spent_today_usd} of ${s.llm_budget_usd}</dd>
            <dt className="label pt-0.5 text-[0.625rem] text-amber-400/70">Config</dt><dd className="truncate">{s.versions.config_sha256?.slice(0, 12) ?? "–"}</dd>
            <dt className="label pt-0.5 text-[0.625rem] text-amber-400/70">Feature set</dt><dd>v{s.versions.feature_set}</dd>
            <dt className="label pt-0.5 text-[0.625rem] text-amber-400/70">Rulebook</dt><dd>v{s.versions.rulebook}</dd>
          </dl>
          <div className="mt-3 border-t border-zinc-800 pt-3"><EngineControls /></div>
        </Card>
        <Card title="Heartbeats">
          <Table head={["Component", "Status", "Age", "Detail"]}>
            {s.heartbeats.map((h) => (
              <tr key={h.component}><Td>{h.component}</Td><Td className={["error", "diff", "failed"].includes(h.status) ? "text-rose-300" : ""}>{h.status}</Td>
                <Td>{age(h.age_s)}</Td><Td className="max-w-xs truncate text-xs text-zinc-400">{h.detail ? JSON.stringify(h.detail) : ""}</Td></tr>
            ))}
          </Table>
        </Card>
      </div>
      <RolloutCard />
      <CrossAssetCard />
      <Card title="Engine log (tail)" actions={<Select label="level" value={level} onChange={setLevel} options={["error", "warning", "info"]} />}>
        <div className="max-h-96 overflow-auto rounded-sm border border-zinc-800 bg-zinc-950/80 p-2 font-mono text-xs leading-5">
          {(logs.data ?? []).slice().reverse().map((l, i) => (
            <div key={i} className={l.level === "error" ? "text-rose-300" : l.level === "warning" ? "text-amber-300" : "text-zinc-300"}>
              <span className="text-zinc-600">{l.ts?.slice(0, 19)}</span> {l.level} <span className="text-sky-400/80">{l.component}</span> {l.event}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
