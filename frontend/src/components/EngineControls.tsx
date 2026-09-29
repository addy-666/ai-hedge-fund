import { useState } from "react";
import { ApiError } from "../api/client";
import { useCommand, useCommandStatus } from "../api/queries";
import { Dialog } from "./Dialog";
import { ReauthDialog } from "./ReauthDialog";
import { Button } from "./ui";

type Action = "START" | "RESUME" | "STOP" | "REARM";
const TEXT: Record<Action, string> = {
  START: "Start the engine from STOPPED (it comes up PAUSED or RUNNING per its restart rules).",
  RESUME: "Resume trading: the engine checks the broker, limits and heartbeats before it goes RUNNING.",
  STOP: "Stop the engine: no new decisions; open positions keep their broker-side stops.",
  REARM: "Clear a HALT (a new trading day or an operator decision). After a drawdown halt this needs your password.",
};

/** START / RESUME / STOP / REARM with one confirmation; the API asks for the password where docs/05 §2 says so. */
export function EngineControls() {
  const [confirm, setConfirm] = useState<Action | null>(null);
  const [reauth, setReauth] = useState<Action | null>(null);
  const [commandId, setCommandId] = useState<string | null>(null);
  const command = useCommand();
  const status = useCommandStatus(commandId);
  const send = (type: Action) => {
    setConfirm(null);
    command.mutate(
      { type },
      {
        onSuccess: (r) => setCommandId(r.command_id),
        onError: (e) => e instanceof ApiError && e.needsReauth && setReauth(type),
      },
    );
  };
  const result = status.data;
  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {(Object.keys(TEXT) as Action[]).map((a) => (
          <Button key={a} variant={a === "STOP" ? "warn" : "default"} onClick={() => setConfirm(a)}>{a[0] + a.slice(1).toLowerCase()}</Button>
        ))}
      </div>
      {result && (
        <p role="status" className={`mt-2 text-sm ${result.status === "FAILED" ? "text-rose-300" : "text-zinc-400"}`}>
          {result.type}: {result.status}{result.result ? ` · ${JSON.stringify(result.result)}` : ""}
        </p>
      )}
      {command.error && !(command.error instanceof ApiError && command.error.needsReauth) && (
        <p className="mt-2 text-sm text-rose-300">{command.error.message}</p>
      )}
      {confirm && (
        <Dialog title={`${confirm} the engine?`} onClose={() => setConfirm(null)}
                actions={<Button onClick={() => send(confirm)}>Yes, {confirm.toLowerCase()}</Button>}>
          {TEXT[confirm]}
        </Dialog>
      )}
      {reauth && <ReauthDialog onClose={() => setReauth(null)} onDone={() => { const a = reauth; setReauth(null); send(a); }} />}
    </div>
  );
}
