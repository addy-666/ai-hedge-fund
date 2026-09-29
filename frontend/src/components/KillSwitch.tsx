import { useState } from "react";
import { useCommand } from "../api/queries";
import { Dialog } from "./Dialog";
import { Button } from "./ui";

type Action = "PAUSE" | "FLATTEN_ALL";
const TEXT: Record<Action, string> = {
  PAUSE: "Stop opening new positions. Open positions keep their stops and are still managed.",
  FLATTEN_ALL: "Close EVERY engine position at the market now, then halt the engine. This cannot be undone.",
};

/** PAUSE and FLATTEN ALL: always one confirmation, never a password (safety actions must be fast). */
export function KillSwitch() {
  const [confirm, setConfirm] = useState<Action | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const command = useCommand();
  const send = (type: Action) => {
    command.mutate({ type }, { onSuccess: () => setSent(type) });
    setConfirm(null);
  };
  return (
    <div className="flex items-center gap-2">
      <Button variant="warn" onClick={() => setConfirm("PAUSE")}>Pause</Button>
      <Button variant="danger" onClick={() => setConfirm("FLATTEN_ALL")}>Flatten all</Button>
      {sent && <span className="text-xs text-zinc-400" role="status">{sent} sent</span>}
      {command.error && <span className="text-xs text-rose-300">{command.error.message}</span>}
      {confirm && (
        <Dialog
          title={confirm === "PAUSE" ? "Pause the engine?" : "Flatten all positions?"}
          onClose={() => setConfirm(null)}
          actions={<Button variant={confirm === "PAUSE" ? "warn" : "danger"} onClick={() => send(confirm)}>Yes, {confirm === "PAUSE" ? "pause" : "flatten all"}</Button>}
        >
          {TEXT[confirm]}
        </Dialog>
      )}
    </div>
  );
}
