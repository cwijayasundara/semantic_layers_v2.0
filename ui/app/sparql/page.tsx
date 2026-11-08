"use client";
import { useEffect, useState } from "react";
import { Rows, Status, api, useApi } from "@/lib/api";
import { ErrBox, ResultTable } from "@/components/Bits";

const EXAMPLES: [string, string][] = [
  ["Changes by kind", "SELECT ?kind (COUNT(?c) AS ?n) WHERE {\n  ?c ops:changeKind/skos:notation ?kind\n} GROUP BY ?kind ORDER BY DESC(?n)"],
  ["Alerts, newest first", "SELECT ?alert ?app ?t WHERE {\n  ?alert a ops:Alert ; ops:atTime ?t .\n  OPTIONAL { ?alert ops:onApplication ?app }\n} ORDER BY DESC(?t) LIMIT 25"],
  ["Incidents and what they affect", "SELECT ?id ?t ?sev ?aff WHERE {\n  ?i a ops:Incident ; inc:incidentId ?id ; ops:atTime ?t .\n  OPTIONAL { ?i ops:severity ?sev } OPTIONAL { ?i ops:affects ?aff }\n} ORDER BY DESC(?t) LIMIT 50"],
  ["Who depends on title-metadata-svc", "SELECT ?app WHERE {\n  ?app ops:dependsOn+ <https://kg.local/app/application/title-metadata-svc>\n}"],
  ["Triples per named graph", "SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } GROUP BY ?g ORDER BY DESC(?n)"],
];

export default function Sparql() {
  const { data: status } = useApi<Status>("/status");
  const [scenario, setScenario] = useState("hero");
  const [store, setStore] = useState<"as_of" | "full">("as_of");
  const [query, setQuery] = useState(EXAMPLES[0][1]);
  const [res, setRes] = useState<Rows | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (status && !status.scenarios.includes(scenario) && status.scenarios[0]) setScenario(status.scenarios[0]); }, [status, scenario]);
  const run = () => {
    setBusy(true); setErr(null);
    api<Rows>(`/scenarios/${scenario}/sparql`, { query, store }).then(setRes).catch((e: Error) => { setErr(e.message); setRes(null); }).finally(() => setBusy(false));
  };
  return (
    <>
      <h1>SPARQL</h1>
      <div className="sub">Read-only SELECT over the union of named graphs plus the ontology. The project prefixes (ops, app, chg, inc, skos, …) are filled in, and LIMIT is capped at 200.</div>
      <div className="card">
        <div className="row" style={{ marginBottom: 10 }}>
          <select value={scenario} onChange={(e) => setScenario(e.target.value)} className="ghost">
            {(status?.scenarios ?? [scenario]).map((s) => <option key={s}>{s}</option>)}
          </select>
          <select value={store} onChange={(e) => setStore(e.target.value as "as_of" | "full")}>
            <option value="as_of">as-of store (what the agent sees)</option>
            <option value="full">full history store</option>
          </select>
          <span className="sp" />
          {EXAMPLES.map(([label, q]) => <button key={label} className="ghost" onClick={() => setQuery(q)}>{label}</button>)}
        </div>
        <textarea value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") run(); }} />
        <div className="row" style={{ marginTop: 10 }}>
          <button onClick={run} disabled={busy}>{busy ? "Running…" : "Run (⌘↵)"}</button>
          {res && <span className="mut">{res.count} rows in {res.ms} ms{res.truncated ? `, ${res.truncated} more truncated` : ""}{res.capped ? `, capped at ${res.capped}` : ""}</span>}
        </div>
      </div>
      {err && <ErrBox msg={err} />}
      {res && <div className="card" style={{ marginTop: 14 }}><ResultTable rows={res.rows as Record<string, string>[]} /></div>}
    </>
  );
}
