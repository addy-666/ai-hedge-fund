import { useState } from "react";
import { api, setCsrf } from "../api/client";
import type { SessionInfo } from "../api/types";
import { ThemeToggle } from "../components/ThemeToggle";
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
    <div className="relative flex min-h-screen items-center justify-center p-4">
      <div className="absolute right-4 top-4"><ThemeToggle /></div>
      <form onSubmit={submit} className="brackets w-full max-w-sm rounded-sm border border-zinc-800 bg-zinc-900/80 shadow-[0_0_80px_-20px_rgb(47_224_245/0.25)]">
        <header className="flex items-center justify-between border-b border-zinc-800 bg-zinc-950/70 px-5 py-3">
          <h1 className="bg-amber-400 px-1.5 py-px font-mono text-sm font-semibold text-zinc-950 shadow-[var(--glow-amber)]">aifund</h1>
          <span className="label text-[0.625rem] text-zinc-500">Quant ops terminal</span>
        </header>
        <div className="space-y-4 px-5 py-5">
          <p className="num text-xs leading-relaxed text-zinc-500">
            <span className="text-emerald-400">●</span> secure session · operator access only
            <br />
            <span className="text-sky-400">&gt;</span> authenticate to continue
            <span aria-hidden className="ml-1 inline-block h-3 w-1.5 translate-y-0.5 animate-blink bg-sky-400" />
          </p>
          <div>
            <label className="label mb-1.5 block text-[0.625rem] text-amber-400/80" htmlFor="password">Password</label>
            <input id="password" type="password" autoFocus className="w-full bg-zinc-950 px-2.5 py-2"
                   value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          {error && (
            <p className="rounded-sm border border-rose-700/70 bg-rose-950/80 px-3 py-2 font-mono text-xs text-rose-200" role="alert">{error}</p>
          )}
          <Button type="submit" className="w-full py-2" disabled={!password}>Log in</Button>
        </div>
      </form>
    </div>
  );
}
