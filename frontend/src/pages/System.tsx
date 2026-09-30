import { useState } from "react";
import { useLogs, useSystem } from "../api/queries";
import { EngineControls } from "../components/EngineControls";
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
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Engine">
          <dl className="grid grid-cols-2 gap-1 text-sm">
            <dt className="text-zinc-500">State</dt><dd>{s.engine?.state ?? "no engine row"}{s.engine_stale ? " (stale)" : ""}</dd>
            <dt className="text-zinc-500">Mode</dt><dd>{s.engine?.mode ?? "–"}</dd>
            <dt className="text-zinc-500">Halt reason</dt><dd>{s.engine?.halt_reason ?? "–"}</dd>
            <dt className="text-zinc-500">Updated</dt><dd>{utc(s.engine?.updated_at)}</dd>
            <dt className="text-zinc-500">LLM today</dt><dd>${s.llm_spent_today_usd} of ${s.llm_budget_usd}</dd>
            <dt className="text-zinc-500">Config</dt><dd className="truncate">{s.versions.config_sha256?.slice(0, 12) ?? "–"}</dd>
            <dt className="text-zinc-500">Feature set</dt><dd>v{s.versions.feature_set}</dd>
            <dt className="text-zinc-500">Rulebook</dt><dd>v{s.versions.rulebook}</dd>
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
      <Card title="Engine log (tail)" actions={<Select label="level" value={level} onChange={setLevel} options={["error", "warning", "info"]} />}>
        <div className="max-h-96 overflow-auto font-mono text-xs">
          {(logs.data ?? []).slice().reverse().map((l, i) => (
            <div key={i} className={l.level === "error" ? "text-rose-300" : l.level === "warning" ? "text-amber-300" : "text-zinc-300"}>
              {l.ts?.slice(0, 19)} {l.level} {l.component} {l.event}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
