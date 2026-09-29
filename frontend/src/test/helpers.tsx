// Test helpers: a fresh QueryClient per render and a fetch stub that answers by "METHOD path".
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

export function renderWithClient(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

export type Reply = { status?: number; body?: unknown };
export type Call = { method: string; path: string; headers: Record<string, string>; body: unknown };

/** Stubs ``fetch``; ``routes["PUT /api/config"]`` may be a reply or a function of the call (for sequences). */
export function stubFetch(routes: Record<string, Reply | ((call: Call) => Reply)>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (input: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const path = input.split("?")[0] ?? input;
    const call: Call = {
      method,
      path,
      headers: (init.headers ?? {}) as Record<string, string>,
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(call);
    const route = routes[`${method} ${path}`];
    if (!route) return new Response(JSON.stringify({ detail: "not stubbed" }), { status: 404 });
    const reply = typeof route === "function" ? route(call) : route;
    return new Response(JSON.stringify(reply.body ?? null), { status: reply.status ?? 200 });
  });
  return calls;
}
