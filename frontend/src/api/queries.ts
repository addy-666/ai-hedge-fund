// TanStack Query hooks, one per read endpoint. Live events (ws/socket.ts) invalidate them.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  AccountOut, BreakdownRow, CommandOut, ConfigOut, ConfigSaved, ConfigVersionOut, Costs, DecisionDossier,
  DecisionOut, EquityPoint, LLMUsageRow, LogLine, Me, Page, PositionOut, Summary, SystemOut, TradeDossier,
  TradeOut, VirtualTradeOut, RuleOut, RuleDetail, RuleIn, RuleCreated, RulebookVersionOut, RulebookDiff,
  AuditRunOut, AuditRunDetail, FeatureOut,
} from "./types";

export const useMe = () => useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/me"), retry: false });
export const useSystem = () =>
  useQuery({ queryKey: ["system"], queryFn: () => api.get<SystemOut>("/api/system"), refetchInterval: 10_000 });
export const useAccount = () => useQuery({ queryKey: ["account"], queryFn: () => api.get<AccountOut>("/api/account") });
export const useEquity = (days = 30) =>
  useQuery({
    queryKey: ["equity", days],
    queryFn: () =>
      api.get<EquityPoint[]>("/api/equity", {
        from: new Date(Date.now() - days * 86_400_000).toISOString(),
        granularity: days > 7 ? 60 : 5,
      }),
  });
export const usePositions = () =>
  useQuery({ queryKey: ["positions"], queryFn: () => api.get<PositionOut[]>("/api/positions"), refetchInterval: 15_000 });

export type DecisionFilter = { symbol?: string; outcome?: string; reason?: string };
export const useDecisions = (f: DecisionFilter, cursor?: string) =>
  useQuery({
    queryKey: ["decisions", f, cursor],
    queryFn: () => api.get<Page<DecisionOut>>("/api/decisions", { ...f, cursor, limit: 50 }),
  });
export const useDecision = (id: string | undefined) =>
  useQuery({
    queryKey: ["decision", id],
    queryFn: () => api.get<DecisionDossier>(`/api/decisions/${id}`),
    enabled: !!id,
  });
export const useTrades = (f: { symbol?: string; setup?: string; outcome?: string }, cursor?: string) =>
  useQuery({
    queryKey: ["trades", f, cursor],
    queryFn: () => api.get<Page<TradeOut>>("/api/trades", { ...f, cursor, limit: 50 }),
  });
export const useTrade = (id: string | undefined) =>
  useQuery({ queryKey: ["trade", id], queryFn: () => api.get<TradeDossier>(`/api/trades/${id}`), enabled: !!id });
export const useVirtualTrades = (arm?: string) =>
  useQuery({
    queryKey: ["virtual", arm],
    queryFn: () => api.get<Page<VirtualTradeOut>>("/api/virtual-trades", { arm, limit: 100 }),
  });
export const useLlmUsage = (days = 1) =>
  useQuery({
    queryKey: ["llm", days],
    queryFn: () =>
      api.get<LLMUsageRow[]>("/api/llm/usage", { from: new Date(Date.now() - days * 86_400_000).toISOString() }),
  });
export const useLogs = (level?: string) =>
  useQuery({
    queryKey: ["logs", level],
    queryFn: () => api.get<LogLine[]>("/api/logs", { level, limit: 200 }),
    refetchInterval: 10_000,
  });
export const useSummary = (days: number) =>
  useQuery({
    queryKey: ["summary", days],
    queryFn: () =>
      api.get<Summary>("/api/analytics/summary", { from: new Date(Date.now() - days * 86_400_000).toISOString() }),
  });
export const useBreakdown = (dim: string, days: number) =>
  useQuery({
    queryKey: ["breakdown", dim, days],
    queryFn: () =>
      api.get<BreakdownRow[]>("/api/analytics/breakdown", {
        dim,
        from: new Date(Date.now() - days * 86_400_000).toISOString(),
      }),
  });
export const useCosts = (days: number) =>
  useQuery({
    queryKey: ["costs", days],
    queryFn: () =>
      api.get<Costs>("/api/analytics/costs", { from: new Date(Date.now() - days * 86_400_000).toISOString() }),
  });
export const useConfig = () => useQuery({ queryKey: ["config"], queryFn: () => api.get<ConfigOut>("/api/config") });
export const useConfigVersions = () =>
  useQuery({ queryKey: ["config-versions"], queryFn: () => api.get<ConfigVersionOut[]>("/api/config/versions") });

export function useCommand() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { type: string; payload?: Record<string, unknown> }) =>
      api.post<{ command_id: string }>("/api/engine/commands", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["system"] }),
  });
}

export function useClosePosition() {
  return useMutation({
    mutationFn: (positionId: number) => api.post<{ command_id: string }>(`/api/positions/${positionId}/close`),
  });
}

export function useSaveConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { yaml: string; comment?: string }) => api.put<ConfigSaved>("/api/config", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["config"] });
      void qc.invalidateQueries({ queryKey: ["config-versions"] });
    },
  });
}

/** A queued command until the engine finishes it (the engine polls commands about once a second). */
export const useCommandStatus = (id: string | null) =>
  useQuery({
    queryKey: ["command", id],
    queryFn: () => api.get<CommandOut>(`/api/commands/${id}`),
    enabled: id !== null,
    refetchInterval: (q) => (q.state.data && ["DONE", "FAILED"].includes(q.state.data.status) ? false : 1000),
  });

// ---------------------------------------------------------------- learning lab (Phase 7)
export const useRules = () =>
  useQuery({ queryKey: ["rules"], queryFn: () => api.get<RuleOut[]>("/api/rules"), refetchInterval: 15_000 });
export const useRule = (id: string | undefined) =>
  useQuery({ queryKey: ["rule", id], queryFn: () => api.get<RuleDetail>(`/api/rules/${id}`), enabled: !!id });
export const useRulebook = () =>
  useQuery({ queryKey: ["rulebook"], queryFn: () => api.get<RulebookVersionOut[]>("/api/rulebook/versions") });
export const useRulebookDiff = (v: number | undefined) =>
  useQuery({
    queryKey: ["rulebook-diff", v],
    queryFn: () => api.get<RulebookDiff>(`/api/rulebook/versions/${v}/diff`),
    enabled: v !== undefined,
  });
export const useAudits = () => useQuery({ queryKey: ["audits"], queryFn: () => api.get<AuditRunOut[]>("/api/audits") });
export const useAudit = (id: string | undefined) =>
  useQuery({ queryKey: ["audit", id], queryFn: () => api.get<AuditRunDetail>(`/api/audits/${id}`), enabled: !!id });
export const useFeatures = () =>
  useQuery({ queryKey: ["features"], queryFn: () => api.get<FeatureOut[]>("/api/features"), staleTime: Infinity });

export function useRuleAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action, force }: { id: string; action: "approve" | "reject" | "retire"; force?: boolean }) =>
      api.post<{ command_id: string }>(`/api/rules/${id}/${action}${force ? "?force=true" : ""}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
  });
}
export function useRunAudit() {
  return useMutation({ mutationFn: () => api.post<{ command_id: string }>("/api/audits/run") });
}
export function useCreateRule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: RuleIn) => api.post<RuleCreated>("/api/rules", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["rules"] }),
  });
}
