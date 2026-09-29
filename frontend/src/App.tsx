import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { lazy, useEffect } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { setCsrf } from "./api/client";
import { useMe } from "./api/queries";
import { Layout } from "./components/Layout";
import { Loading } from "./components/ui";
import { Login } from "./pages/Login";
import { connect } from "./ws/socket";

// One chunk per page: the charts library loads only with the pages that draw charts.
const page = <K extends string>(load: () => Promise<Record<K, () => React.JSX.Element>>, name: K) =>
  lazy(async () => ({ default: (await load())[name] }));
const Agents = page(() => import("./pages/Agents"), "Agents");
const Analytics = page(() => import("./pages/Analytics"), "Analytics");
const Decisions = page(() => import("./pages/Decisions"), "Decisions");
const Journal = page(() => import("./pages/Journal"), "Journal");
const Overview = page(() => import("./pages/Overview"), "Overview");
const Positions = page(() => import("./pages/Positions"), "Positions");
const Settings = page(() => import("./pages/Settings"), "Settings");
const System = page(() => import("./pages/System"), "System");
const defaultClient = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000 } } });

function Authed() {
  const qc = useQueryClient();
  useEffect(() => connect(qc), [qc]);
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Overview />} />
        <Route path="positions" element={<Positions />} />
        <Route path="decisions" element={<Decisions />} />
        <Route path="journal" element={<Journal />} />
        <Route path="analytics" element={<Analytics />} />
        <Route path="agents" element={<Agents />} />
        <Route path="settings" element={<Settings />} />
        <Route path="system" element={<System />} />
        <Route path="*" element={<Overview />} />
      </Route>
    </Routes>
  );
}

function Gate() {
  const me = useMe();
  const qc = useQueryClient();
  useEffect(() => {
    if (me.data) setCsrf(me.data.csrf_token);
  }, [me.data]);
  if (me.isLoading) return <Loading />;
  if (me.error || !me.data) return <Login onLogin={() => void qc.invalidateQueries({ queryKey: ["me"] })} />;
  return <Authed />;
}

export function App({ client = defaultClient }: { client?: QueryClient }) {
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter><Gate /></BrowserRouter>
    </QueryClientProvider>
  );
}
