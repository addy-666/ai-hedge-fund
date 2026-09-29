// Live events (docs/05 §3): reconnects with ?since=<last seq> so nothing is missed or repeated, and turns each
// event into query invalidations.
import type { QueryClient } from "@tanstack/react-query";

export type LiveEvent = { seq: number; ts: string; type: string; severity: string; payload: Record<string, unknown> | null };

const INVALIDATES: Record<string, string[][]> = {
  "engine.state": [["system"]],
  "command.updated": [["system"], ["positions"], ["rules"], ["audits"]],
  "risk.limit_breach": [["account"], ["system"]],
  "rule.status_changed": [["rules"], ["rule"], ["rulebook"], ["system"]],
};

export function invalidationsFor(type: string): string[][] {
  if (INVALIDATES[type]) return INVALIDATES[type];
  if (type.startsWith("trade.")) return [["positions"], ["trades"], ["account"]];
  if (type.startsWith("decision") || type.startsWith("intent")) return [["decisions"]];
  return [];
}

export function connect(qc: QueryClient, onEvent: (e: LiveEvent) => void = () => {}): () => void {
  let since = -1; // "from now": the page loads its data fresh; the server answers with stream.start
  let socket: WebSocket | null = null;
  let stopped = false;
  let delay = 1000;

  const open = () => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/api/ws?since=${since}`);
    socket.onopen = () => {
      delay = 1000;
    };
    socket.onmessage = (msg) => {
      const event = JSON.parse(String(msg.data)) as LiveEvent;
      since = Math.max(since, event.seq);
      if (event.type === "stream.start") return;
      for (const key of invalidationsFor(event.type)) void qc.invalidateQueries({ queryKey: key });
      onEvent(event);
    };
    socket.onclose = () => {
      if (stopped) return;
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 30_000);
    };
  };
  open();
  return () => {
    stopped = true;
    socket?.close();
  };
}
