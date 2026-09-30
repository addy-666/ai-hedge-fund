// Learning Lab (roadmap 7.9, docs/05 §4.5): the rules board, a rule's evidence and matches, audit runs, the
// operator's rule editor and the rulebook history. Every action is an engine command; the validator decides.
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError } from "../api/client";
import {
  useAudit, useAudits, useCreateRule, useFeatures, useRule, useRuleAction, useRulebook, useRulebookDiff, useRules,
  useRunAudit,
} from "../api/queries";
import type { RuleOut } from "../api/types";
import { Dialog } from "../components/Dialog";
import { ReauthDialog } from "../components/ReauthDialog";
import { Badge, Button, Card, Empty, ErrorNote, Loading, Table, Td } from "../components/ui";
import { r, utc } from "../lib/format";

const COLUMNS = ["CANDIDATE", "SHADOW", "ACTIVE", "RETIRED", "REJECTED"] as const;
const TABS = [["rules", "Rules"], ["audits", "Audit runs"], ["editor", "New rule"], ["rulebook", "Rulebook"]] as const;
type Action = "approve" | "reject" | "retire";

function actionText(a: Record<string, unknown> | undefined): string {
  if (!a) return "?";
  if (a.type === "block") return "block";
  if (a.type === "risk_scale") return `risk ×${String(a.factor)}`;
  if (a.type === "penalty") return `−${String(a.points)} conf`;
  return "?";
}

function num(e: Record<string, unknown> | null | undefined, key: string): number | undefined {
  const v = e?.[key];
  return typeof v === "number" ? v : undefined;
}

export function evidenceLine(e: Record<string, unknown> | null | undefined): string {
  const n = num(e, "n_matched");
  if (n === undefined) return "not validated yet";
  return `n=${n} · ${r(num(e, "mean_matched"))} vs ${r(num(e, "mean_unmatched"))} · holdout n=${num(e, "n_holdout") ?? "–"}`;
}

function RuleCard({ rule, selected, onOpen }: { rule: RuleOut; selected: boolean; onOpen: () => void }) {
  const shadow = rule.evidence?.shadow as { n?: number; days?: number } | undefined;
  return (
    <button onClick={onOpen} className={`w-full rounded border p-2 text-left text-xs hover:border-sky-600 ${selected ? "border-sky-500 bg-zinc-800/60" : "border-zinc-800 bg-zinc-950/40"}`}>
      <div className="mb-1 flex items-center justify-between gap-1">
        <span className="font-semibold text-zinc-200">{rule.rule_id} v{rule.version}</span>
        <Badge tone={rule.action?.type === "block" ? "red" : rule.action?.type === "risk_scale" ? "amber" : "zinc"}>{actionText(rule.action)}</Badge>
      </div>
      <div className="mb-1 text-zinc-300">{rule.text}</div>
      <div className="text-zinc-500">{evidenceLine(rule.evidence)}</div>
      {rule.status === "SHADOW" && shadow && <div className="text-zinc-500">shadow: {shadow.n ?? 0} matches, {shadow.days ?? 0} days</div>}
      {rule.status === "ACTIVE" && rule.review_at && <div className="text-zinc-500">review {utc(rule.review_at)}</div>}
      {rule.awaiting_approval && <div className="mt-1"><Badge tone="amber">needs approval</Badge></div>}
    </button>
  );
}

function RuleDetailPanel({ id }: { id: string }) {
  const detail = useRule(id);
  const act = useRuleAction();
  const [confirm, setConfirm] = useState<{ action: Action; force?: boolean } | null>(null);
  const [reauth, setReauth] = useState<{ action: Action; force?: boolean } | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  if (detail.isLoading) return <Loading />;
  if (!detail.data) return <ErrorNote error={detail.error} />;
  const { rule, versions, matches } = detail.data;
  const send = (a: { action: Action; force?: boolean }) => {
    setConfirm(null);
    act.mutate({ id, ...a }, {
      onSuccess: () => setSent(`${a.action} sent to the engine`),
      onError: (e) => { if (e instanceof ApiError && e.needsReauth) setReauth(a); },
    });
  };
  const e = rule.evidence ?? {};
  const failures = (e.failures as string[] | undefined) ?? [];
  return (
    <Card title={`${rule.rule_id} v${rule.version} · ${rule.status}`} actions={
      <div className="flex gap-2">
        {rule.status === "SHADOW" && <Button onClick={() => setConfirm({ action: "approve" })}>Approve</Button>}
        {rule.status === "CANDIDATE" && <Button variant="warn" onClick={() => setConfirm({ action: "approve", force: true })}>Force activate</Button>}
        {(rule.status === "CANDIDATE" || rule.status === "SHADOW") && <Button variant="ghost" onClick={() => setConfirm({ action: "reject" })}>Reject</Button>}
        {(rule.status === "ACTIVE" || rule.status === "SHADOW") && <Button variant="danger" onClick={() => setConfirm({ action: "retire" })}>Retire</Button>}
      </div>}>
      <p className="mb-2 text-sm">{rule.text} → <b>{actionText(rule.action)}</b></p>
      {rule.hypothesis && <p className="mb-2 text-sm text-zinc-400">Hypothesis: {rule.hypothesis}</p>}
      <p className="mb-2 text-xs text-zinc-500">{evidenceLine(rule.evidence)} · origin {rule.origin}{rule.retire_reason ? ` · ${rule.retire_reason}` : ""}</p>
      {failures.length > 0 && <ul className="mb-2 list-disc pl-5 text-xs text-rose-300">{failures.map((f) => <li key={f}>{f}</li>)}</ul>}
      {sent && <p role="status" className="mb-2 text-sm text-emerald-300">{sent}</p>}
      {act.error && !(act.error instanceof ApiError && act.error.needsReauth) && <ErrorNote error={act.error} />}
      <h3 className="mb-1 mt-3 text-xs uppercase text-zinc-500">Evidence</h3>
      <pre className="max-h-48 overflow-auto rounded bg-zinc-950 p-2 text-xs">{JSON.stringify(e, null, 1)}</pre>
      <h3 className="mb-1 mt-3 text-xs uppercase text-zinc-500">Matched decisions (newest first)</h3>
      {matches.length === 0 ? <Empty>No matches recorded yet.</Empty> : (
        <Table head={["Bar (UTC)", "Symbol", "Outcome", "Mode", "R"]}>
          {matches.map((m) => (
            <tr key={`${m.decision_id}-${m.mode}`}><Td>{utc(m.bar_time)}</Td><Td>{m.symbol}</Td><Td>{m.outcome}</Td>
              <Td>{m.matched ? m.mode : `${m.mode} (missing feature)`}</Td><Td>{r(m.r)}</Td></tr>
          ))}
        </Table>
      )}
      {versions.length > 1 && (
        <>
          <h3 className="mb-1 mt-3 text-xs uppercase text-zinc-500">Versions</h3>
          <Table head={["Version", "Status", "Rule"]}>
            {versions.map((v) => <tr key={v.version}><Td>v{v.version}</Td><Td>{v.status}</Td><Td>{v.text}</Td></tr>)}
          </Table>
        </>
      )}
      {confirm && (
        <Dialog title={`${confirm.force ? "Force-activate" : confirm.action} ${rule.rule_id}?`} onClose={() => setConfirm(null)}
                actions={<Button onClick={() => send(confirm)}>Yes, {confirm.force ? "force-activate" : confirm.action}</Button>}>
          {confirm.force ? "Activates without validation (audit-logged). Rules only ever reduce risk." : `The engine will ${confirm.action} this rule.`}
        </Dialog>
      )}
      {reauth && <ReauthDialog onClose={() => setReauth(null)} onDone={() => { const a = reauth; setReauth(null); send(a); }} />}
    </Card>
  );
}

function RulesBoard() {
  const rules = useRules();
  const [open, setOpen] = useState<string | undefined>();
  const run = useRunAudit();
  if (rules.isLoading) return <Loading />;
  if (rules.error) return <ErrorNote error={rules.error} />;
  const all = rules.data ?? [];
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Button onClick={() => run.mutate()} disabled={run.isPending}>Run audit now</Button>
        {run.isSuccess && <span role="status" className="text-xs text-zinc-400">audit queued</span>}
      </div>
      <div className="grid gap-3 md:grid-cols-5">
        {COLUMNS.map((col) => {
          const inCol = all.filter((x) => x.status === col);
          return (
            <div key={col} aria-label={`${col} rules`} className="space-y-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">{col} ({inCol.length})</h3>
              {inCol.map((x) => <RuleCard key={`${x.rule_id}v${x.version}`} rule={x} selected={open === x.rule_id} onOpen={() => setOpen(x.rule_id)} />)}
            </div>
          );
        })}
      </div>
      {open && <RuleDetailPanel id={open} />}
    </div>
  );
}

function Audits() {
  const audits = useAudits();
  const [open, setOpen] = useState<string | undefined>();
  const audit = useAudit(open);
  const clusters = (audit.data?.miner_output?.clusters as { id: string; text: string; n: number; mean_r: number; p_value: number }[] | undefined) ?? [];
  const results = (audit.data?.validation?.results as { rule_id: string; version: number; text: string; passed: boolean; failures: string[] }[] | undefined) ?? [];
  return (
    <div className="grid gap-4 lg:grid-cols-[22rem_1fr]">
      <Card title="Audit runs">
        {(audits.data ?? []).length === 0 ? <Empty>No audits yet.</Empty> : (
          <Table head={["When", "Trigger", "Status", "n"]}>
            {(audits.data ?? []).map((a) => (
              <tr key={a.id} onClick={() => setOpen(a.id)} className={`cursor-pointer hover:bg-zinc-800/40 ${open === a.id ? "bg-zinc-800/60" : ""}`}>
                <Td>{utc(a.created_at)}</Td><Td>{a.trigger}</Td><Td>{a.status}</Td><Td>{a.n_trades}+{a.n_virtual}v</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      {open && audit.data && (
        <Card title="Report">
          <pre className="mb-3 whitespace-pre-wrap rounded bg-zinc-950 p-2 text-xs">{audit.data.lessons_md ?? ""}</pre>
          <h3 className="mb-1 text-xs uppercase text-zinc-500">Miner clusters</h3>
          {clusters.length === 0 ? <Empty>None survived.</Empty> : (
            <Table head={["Id", "Cluster", "n", "Mean R", "p"]}>
              {clusters.map((c) => <tr key={c.id}><Td>{c.id}</Td><Td>{c.text}</Td><Td>{c.n}</Td><Td>{r(c.mean_r)}</Td><Td>{c.p_value.toFixed(4)}</Td></tr>)}
            </Table>
          )}
          <h3 className="mb-1 mt-3 text-xs uppercase text-zinc-500">Candidates and the validator</h3>
          {results.length === 0 ? <Empty>No candidates.</Empty> : (
            <ul className="space-y-1 text-sm">
              {results.map((v) => (
                <li key={`${v.rule_id}v${v.version}`}>
                  <Badge tone={v.passed ? "green" : "zinc"}>{v.passed ? "SHADOW" : "rejected"}</Badge> {v.rule_id} v{v.version} {v.text}
                  {!v.passed && <span className="text-xs text-zinc-500"> — {v.failures.join("; ")}</span>}
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
    </div>
  );
}

type Pred = { feature: string; op: string; value: string };
const OPS = ["<", "<=", ">", ">=", "==", "!=", "in", "not_in", "between"];

export function parseValue(op: string, raw: string): unknown {
  const one = (x: string): unknown => {
    const t = x.trim();
    if (t === "true" || t === "false") return t === "true";
    const n = Number(t);
    return t !== "" && Number.isFinite(n) ? n : t;
  };
  return ["in", "not_in", "between"].includes(op) ? raw.split(",").map(one) : one(raw);
}

function RuleEditor() {
  const features = useFeatures();
  const create = useCreateRule();
  const [symbols, setSymbols] = useState("");
  const [direction, setDirection] = useState("");
  const [setups, setSetups] = useState("");
  const [preds, setPreds] = useState<Pred[]>([{ feature: "", op: ">", value: "" }]);
  const [action, setAction] = useState("penalty");
  const [param, setParam] = useState("10");
  const [hypothesis, setHypothesis] = useState("");
  const list = (x: string) => x.split(",").map((s) => s.trim()).filter(Boolean);
  const submit = () =>
    create.mutate({
      scope: { symbols: list(symbols), directions: direction ? [direction] : [], setup_tags: list(setups) },
      conditions: { all: preds.map((p) => ({ feature: p.feature, op: p.op, value: parseValue(p.op, p.value) })) },
      action: action === "block" ? { type: "block" } : action === "risk_scale" ? { type: "risk_scale", factor: Number(param) } : { type: "penalty", points: Number(param) },
      hypothesis,
    });
  const set = (i: number, patch: Partial<Pred>) => setPreds(preds.map((p, j) => (j === i ? { ...p, ...patch } : p)));
  return (
    <Card title="New rule (goes through the validator, then shadow)">
      <datalist id="features">{(features.data ?? []).map((f) => <option key={f.name} value={f.name}>{f.description}</option>)}</datalist>
      <div className="grid gap-2 text-sm md:grid-cols-3">
        <label>Symbols <input aria-label="symbols" className="w-full rounded bg-zinc-800 px-2 py-1" placeholder="XAUUSD, NAS100 (empty = all)" value={symbols} onChange={(e) => setSymbols(e.target.value)} /></label>
        <label>Direction <select aria-label="direction" className="w-full rounded bg-zinc-800 px-2 py-1" value={direction} onChange={(e) => setDirection(e.target.value)}><option value="">both</option><option>LONG</option><option>SHORT</option></select></label>
        <label>Setups <input aria-label="setups" className="w-full rounded bg-zinc-800 px-2 py-1" placeholder="empty = all" value={setups} onChange={(e) => setSetups(e.target.value)} /></label>
      </div>
      <h3 className="mb-1 mt-3 text-xs uppercase text-zinc-500">Conditions (all must hold)</h3>
      {preds.map((p, i) => (
        <div key={i} className="mb-1 flex gap-2">
          <input aria-label={`feature ${i + 1}`} list="features" className="flex-1 rounded bg-zinc-800 px-2 py-1 text-sm" placeholder="feature, e.g. h1.rsi14" value={p.feature} onChange={(e) => set(i, { feature: e.target.value })} />
          <select aria-label={`op ${i + 1}`} className="rounded bg-zinc-800 px-2 py-1 text-sm" value={p.op} onChange={(e) => set(i, { op: e.target.value })}>{OPS.map((o) => <option key={o}>{o}</option>)}</select>
          <input aria-label={`value ${i + 1}`} className="w-40 rounded bg-zinc-800 px-2 py-1 text-sm" placeholder="70 · NY · lo,hi" value={p.value} onChange={(e) => set(i, { value: e.target.value })} />
          {preds.length > 1 && <Button variant="ghost" onClick={() => setPreds(preds.filter((_, j) => j !== i))}>×</Button>}
        </div>
      ))}
      {preds.length < 3 && <Button variant="ghost" onClick={() => setPreds([...preds, { feature: "", op: ">", value: "" }])}>+ condition</Button>}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
        <select aria-label="action" className="rounded bg-zinc-800 px-2 py-1" value={action} onChange={(e) => { setAction(e.target.value); setParam(e.target.value === "risk_scale" ? "0.5" : "10"); }}>
          <option value="penalty">penalty (points)</option><option value="risk_scale">risk_scale (factor)</option><option value="block">block</option>
        </select>
        {action !== "block" && <input aria-label="action value" className="w-20 rounded bg-zinc-800 px-2 py-1" value={param} onChange={(e) => setParam(e.target.value)} />}
        <input aria-label="hypothesis" className="flex-1 rounded bg-zinc-800 px-2 py-1" placeholder="Hypothesis: why would these trades lose?" value={hypothesis} onChange={(e) => setHypothesis(e.target.value)} />
        <Button onClick={submit} disabled={create.isPending || hypothesis.length < 10}>Submit</Button>
      </div>
      {create.error && <div className="mt-2" role="alert"><ErrorNote error={create.error} /></div>}
      {create.data && <p role="status" className="mt-2 text-sm text-emerald-300">{create.data.rule_id} created as {create.data.status}: the validator runs within the hour</p>}
    </Card>
  );
}

function Rulebook() {
  const versions = useRulebook();
  const [open, setOpen] = useState<number | undefined>();
  const diff = useRulebookDiff(open);
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
      <Card title="Rulebook versions">
        {(versions.data ?? []).length === 0 ? <Empty>No rulebook yet.</Empty> : (
          <Table head={["Version", "When", "Active", "Shadow", "Why"]}>
            {(versions.data ?? []).map((v) => (
              <tr key={v.version} onClick={() => setOpen(v.version)} className={`cursor-pointer hover:bg-zinc-800/40 ${open === v.version ? "bg-zinc-800/60" : ""}`}>
                <Td>v{v.version}</Td><Td>{utc(v.created_at)}</Td><Td>{v.active_rules.length}</Td><Td>{v.shadow_rules.length}</Td>
                <Td className="text-xs text-zinc-400">{v.reason}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      {diff.data && (
        <Card title={`v${diff.data.version} vs ${diff.data.previous ? `v${diff.data.previous}` : "nothing"}`}>
          {([["activated", "green"], ["deactivated", "red"], ["shadowed", "blue"], ["unshadowed", "zinc"]] as const).map(([k, tone]) => (
            <p key={k} className="mb-1 text-sm"><Badge tone={tone}>{k}</Badge> {diff.data[k].join(", ") || "–"}</p>
          ))}
          <p className="mt-2 text-xs text-zinc-500">{diff.data.reason}</p>
        </Card>
      )}
    </div>
  );
}

export function LearningLab() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "rules";
  return (
    <div className="space-y-4">
      <nav className="flex gap-2" aria-label="learning lab">
        {TABS.map(([key, label]) => (
          <Button key={key} variant={tab === key ? "default" : "ghost"} onClick={() => setParams({ tab: key })}>{label}</Button>
        ))}
      </nav>
      {tab === "rules" && <RulesBoard />}
      {tab === "audits" && <Audits />}
      {tab === "editor" && <RuleEditor />}
      {tab === "rulebook" && <Rulebook />}
    </div>
  );
}
