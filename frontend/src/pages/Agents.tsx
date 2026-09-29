import { useState } from "react";
import { useLlmUsage, useSystem } from "../api/queries";
import { Card, Empty, Loading, Select, Table, Td } from "../components/ui";

export function Agents() {
  const [days, setDays] = useState(1);
  const usage = useLlmUsage(days);
  const system = useSystem();
  const rows = usage.data ?? [];
  return (
    <div className="space-y-4">
      <Card title="LLM usage" actions={<Select label="days" value={String(days)} onChange={(v) => setDays(Number(v) || 1)} options={["1", "7", "30"]} />}>
        {usage.isLoading ? <Loading /> : rows.length === 0 ? <Empty>No LLM calls in the window.</Empty> : (
          <Table head={["Agent", "Model", "Calls", "Errors", "Tokens in / out", "Cache hit", "Cost", "p50 / p95 ms"]}>
            {rows.map((u) => (
              <tr key={`${u.agent}-${u.model}`}><Td>{u.agent}</Td><Td>{u.model}</Td><Td>{u.calls}</Td>
                <Td className={u.errors ? "text-rose-300" : ""}>{u.errors}</Td><Td>{u.prompt_tokens} / {u.completion_tokens}</Td>
                <Td>{(u.cache_hit_rate * 100).toFixed(0)}%</Td><Td>${u.cost_usd}</Td><Td>{u.latency_p50_ms ?? "–"} / {u.latency_p95_ms ?? "–"}</Td></tr>
            ))}
          </Table>
        )}
      </Card>
      <Card title="Prompt versions (released, hash-pinned)">
        <ul className="text-sm">{(system.data?.versions.prompts ?? []).map((p) => <li key={p}><code>{p}</code></li>)}</ul>
      </Card>
    </div>
  );
}
