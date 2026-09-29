import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { useConfig, useConfigVersions, useSaveConfig } from "../api/queries";
import type { FieldError } from "../api/types";
import { ReauthDialog } from "../components/ReauthDialog";
import { Button, Card, Loading, Table, Td } from "../components/ui";
import { utc } from "../lib/format";

export function Settings() {
  const config = useConfig();
  const versions = useConfigVersions();
  const save = useSaveConfig();
  const [text, setText] = useState("");
  const [comment, setComment] = useState("");
  const [errors, setErrors] = useState<FieldError[]>([]);
  const [reauth, setReauth] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  useEffect(() => {
    if (config.data) setText(config.data.yaml);
  }, [config.data]);

  const submit = () => {
    setErrors([]);
    setSaved(null);
    save.mutate({ yaml: text, comment: comment || undefined }, {
      onSuccess: (r) => setSaved(r.changed ? `saved as version ${r.version_id}; the engine applies it at its next restart` : "unchanged"),
      onError: (e) => {
        if (e instanceof ApiError && e.needsReauth) setReauth(true);
        else if (e instanceof ApiError && e.status === 422 && Array.isArray(e.body)) setErrors(e.body as FieldError[]);
        else setErrors([{ field: "(request)", message: e.message }]);
      },
    });
  };

  if (config.isLoading) return <Loading />;
  const dirty = text !== config.data?.yaml;
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
      <Card title="Trading config (config/trading.yaml)">
        <textarea aria-label="config" spellCheck={false} className="h-[32rem] w-full rounded bg-zinc-950 p-2 font-mono text-xs" value={text} onChange={(e) => setText(e.target.value)} />
        <div className="mt-2 flex items-center gap-2">
          <input aria-label="comment" placeholder="comment (optional)" className="flex-1 rounded bg-zinc-800 px-2 py-1 text-sm" value={comment} onChange={(e) => setComment(e.target.value)} />
          <Button onClick={submit} disabled={!dirty || save.isPending}>Validate & save</Button>
        </div>
        {errors.length > 0 && (
          <ul className="mt-2 space-y-1 text-sm text-rose-300" role="alert">{errors.map((e, i) => <li key={i}><code>{e.field}</code>: {e.message}</li>)}</ul>
        )}
        {saved && <p className="mt-2 text-sm text-emerald-300" role="status">{saved}</p>}
      </Card>
      <Card title="History">
        <Table head={["When", "By", "Comment"]}>
          {(versions.data ?? []).map((v) => <tr key={v.id}><Td>{utc(v.created_at)}</Td><Td>{v.created_by}</Td><Td>{v.comment ?? ""}</Td></tr>)}
        </Table>
      </Card>
      {reauth && <ReauthDialog onClose={() => setReauth(false)} onDone={() => { setReauth(false); submit(); }} />}
    </div>
  );
}
