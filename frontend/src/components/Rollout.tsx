import { useState } from "react";
import { ApiError } from "../api/client";
import { useRollout, useSignoff } from "../api/queries";
import type { RolloutOut } from "../api/types";
import { ReauthDialog } from "./ReauthDialog";
import { Badge, Button, Card, ErrorNote, Loading, Table, Td } from "./ui";
import { utc } from "../lib/format";

/** The rollout ladder (docs/06 §10): the level, its measured exit gates, the operator's sign-off. */
export function RolloutView({ data, onSignoff }: { data: RolloutOut; onSignoff: (note: string) => void }) {
  const [note, setNote] = useState("");
  return (
    <div className="space-y-3">
      <p className="text-sm">
        Level <b>{data.level} {data.level_name}</b>
        {data.period_start ? ` since ${utc(data.period_start)}` : ""}
        {data.next_level ? ` · exit gates to ${data.next_level}` : ""}
      </p>
      <Table head={["Gate", "Status", "Measured", "Needs"]}>
        {data.gates.map((g) => (
          <tr key={g.name}><Td>{g.name.replace(/_/g, " ")}</Td>
            <Td><Badge tone={g.status === "PASS" ? "green" : g.status === "FAIL" ? "red" : "zinc"}>{g.status}</Badge></Td>
            <Td className="text-xs">{g.value}</Td><Td className="text-xs text-zinc-400">{g.need}</Td></tr>
        ))}
      </Table>
      {data.next_level && (
        <div className="flex items-center gap-2">
          <input aria-label="sign-off note" className="flex-1 rounded bg-zinc-800 px-2 py-1.5 text-sm" value={note}
            placeholder={data.ready ? "why this is ready (recorded in the audit log)" : "not ready: every measured gate must pass"}
            onChange={(e) => setNote(e.target.value)} disabled={!data.ready} />
          <Button disabled={!data.ready || note.trim().length < 3} onClick={() => onSignoff(note.trim())}>
            Sign off {data.level} → {data.next_level}
          </Button>
        </div>
      )}
      <p className="text-xs text-zinc-500">
        Signing off records your decision; switch <code>engine.mode</code> / the risk in Settings yourself afterwards.
        Moving down a level never needs this.
      </p>
      {data.signoffs.length > 0 && (
        <Table head={["When", "From", "To", "Note"]}>
          {data.signoffs.map((s) => (
            <tr key={s.ts}><Td>{utc(s.ts)}</Td><Td>{s.level_from}</Td><Td>{s.level_to}</Td><Td>{s.note}</Td></tr>
          ))}
        </Table>
      )}
    </div>
  );
}

export function RolloutCard() {
  const rollout = useRollout();
  const signoff = useSignoff();
  const [reauth, setReauth] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const send = (note: string) =>
    signoff.mutate(note, {
      onSuccess: (r) => setSent(`signed off: now tracking ${r.level}`),
      onError: (e) => { if (e instanceof ApiError && e.needsReauth) setReauth(note); },
    });
  return (
    <Card title="Rollout gates">
      {sent && <p role="status" className="mb-2 text-sm text-emerald-300">{sent}</p>}
      {signoff.error && !(signoff.error instanceof ApiError && signoff.error.needsReauth) && <ErrorNote error={signoff.error} />}
      {rollout.isLoading ? <Loading /> : rollout.data ? <RolloutView data={rollout.data} onSignoff={send} /> : <ErrorNote error={rollout.error} />}
      {reauth && <ReauthDialog onClose={() => setReauth(null)} onDone={() => { const n = reauth; setReauth(null); send(n); }} />}
    </Card>
  );
}
