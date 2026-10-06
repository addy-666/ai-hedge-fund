// The pipeline trace of one decision (docs/05 §4 Decisions): gates -> setups -> proposal -> rules ->
// confidence math -> risk worksheet -> execution. Every number shown is the one the engine recorded.
import { Fragment, type ReactNode } from "react";
import type { DecisionDossier } from "../api/types";
import { Badge } from "./ui";

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[8rem_1fr] gap-3 border-b border-zinc-800/60 py-1.5 text-sm last:border-0">
      <div className="label pt-0.5 text-[0.625rem] text-amber-400/70">{label}</div>
      <div className="min-w-0 break-words">{children}</div>
    </div>
  );
}

export function confidenceMath(d: Pick<DecisionDossier, "llm_confidence" | "calibrated_confidence" | "penalty_points" | "final_confidence">): string {
  if (d.final_confidence === null || d.final_confidence === undefined) return "–";
  const parts = [`raw ${d.llm_confidence ?? "–"}`, `calibrated ${d.calibrated_confidence ?? "–"}`];
  if (d.penalty_points) parts.push(`− ${d.penalty_points} rule penalty`);
  return `${parts.join(" → ")} = ${d.final_confidence}`;
}

export function DecisionTrace({ d }: { d: DecisionDossier }) {
  const proposal = d.proposal ?? {};
  const analyst = (proposal["shadow_analyst"] as Record<string, unknown> | undefined) ?? null;
  const intents = d.intents ?? [];
  const llmCalls = d.llm_calls ?? [];
  return (
    <div>
      <Row label="Outcome">
        <Badge tone={d.outcome === "ORDERED" ? "green" : d.outcome === "ERROR" ? "red" : "zinc"}>{d.outcome}</Badge>{" "}
        {d.reason_code && <Badge tone="amber">{d.reason_code}</Badge>} <span className="text-zinc-400">{d.reason_detail}</span>
      </Row>
      <Row label="Stage reached">{d.stage_reached}</Row>
      <Row label="Setups">{(d.setups ?? []).map((s, i) => <code key={i} className="mr-2 text-xs">{JSON.stringify(s)}</code>)}</Row>
      <Row label="Proposal"><code className="text-xs">{JSON.stringify(proposal)}</code></Row>
      {analyst && <Row label="Analyst (shadow)"><code className="text-xs">{JSON.stringify(analyst)}</code></Row>}
      <Row label="Rules matched">{(d.rules_matched ?? []).join(", ") || "none"}</Row>
      <Row label="Confidence">{confidenceMath(d)}{d.risk_factor ? ` · risk × ${d.risk_factor}` : ""}</Row>
      <Row label="Risk worksheet">
        {d.risk_calc ? (
          <dl className="grid grid-cols-2 gap-x-4 text-xs">
            {Object.entries(d.risk_calc).map(([k, v]) => (
              <Fragment key={k}><dt className="text-zinc-500">{k}</dt><dd className="num">{String(v)}</dd></Fragment>
            ))}
          </dl>
        ) : "–"}
      </Row>
      <Row label="Execution">
        {intents.length ? intents.map((i) => <div key={i.id} className="text-xs">{i.kind} {i.side} {i.volume} → {i.status}{i.fill_price ? ` @ ${i.fill_price}` : ""}</div>) : "no order"}
      </Row>
      <Row label="LLM">
        {llmCalls.length ? llmCalls.map((c) => (
          <details key={c.id} className="text-xs">
            <summary>{c.agent} · {c.model} · {c.latency_ms ?? "–"} ms · ${c.cost_usd ?? "0"} {c.valid ? "" : "· INVALID"}</summary>
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap bg-zinc-950 p-2">{JSON.stringify(c.messages, null, 2)}</pre>
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap bg-zinc-950 p-2">{c.response_text}</pre>
          </details>
        )) : "no LLM call"}
      </Row>
    </div>
  );
}
