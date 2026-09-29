import { useState } from "react";
import { api, setCsrf } from "../api/client";
import type { SessionInfo } from "../api/types";
import { Button } from "../components/ui";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const session = await api.post<SessionInfo>("/api/auth/login", { password });
      setCsrf(session.csrf_token);
      onLogin();
    } catch (err) {
      setError(err instanceof Error ? err.message : "login failed");
    }
  };
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-lg border border-zinc-800 bg-zinc-900 p-6">
        <h1 className="mb-4 text-lg font-semibold">aifund</h1>
        <label className="mb-1 block text-sm text-zinc-400" htmlFor="password">Password</label>
        <input id="password" type="password" autoFocus className="mb-3 w-full rounded bg-zinc-800 px-2 py-1.5"
               value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="mb-3 text-sm text-rose-300" role="alert">{error}</p>}
        <Button type="submit" className="w-full" disabled={!password}>Log in</Button>
      </form>
    </div>
  );
}
