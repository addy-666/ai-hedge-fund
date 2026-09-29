import { useState } from "react";
import { api } from "../api/client";
import { Dialog } from "./Dialog";
import { Button } from "./ui";

/** Asks for the password again before a gated action (LIVE, raising risk, REARM after a drawdown halt). */
export function ReauthDialog({ onDone, onClose }: { onDone: () => void; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    try {
      await api.post("/api/auth/reauth", { password });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    }
  };
  return (
    <Dialog title="Confirm with your password" onClose={onClose} actions={<Button onClick={submit} disabled={!password}>Confirm</Button>}>
      <p className="mb-2">This change needs a fresh password (valid 5 minutes).</p>
      <input
        type="password"
        aria-label="password"
        className="w-full rounded bg-zinc-800 px-2 py-1.5"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && void submit()}
        autoFocus
      />
      {error && <p className="mt-2 text-rose-300">{error}</p>}
    </Dialog>
  );
}
