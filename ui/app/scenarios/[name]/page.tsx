"use client";
import { use, useEffect, useState } from "react";
import Link from "next/link";
import { Alert, CHANGE_TYPES, Job, ScenarioDetail, Status, Topology, api, hhmm, tail, useApi } from "@/lib/api";
import { Chip, ErrBox, Spin, Stat } from "@/components/Bits";

export default function ScenarioPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = use(params);
  const { data, error, loading } = useApi<ScenarioDetail>(`/scenarios/${name}`);
  const [tab, setTab] = useState<"chains" | "changes" | "alerts" | "topology" | "rca">("chains");
  return (
    <>
      <h1>{name}</h1>
      <div className="sub">Read from the as-of store through the same graph tools the agent uses.</div>
      {error && <ErrBox msg={error} />}
      {loading && !data && <Spin what="Correlating" />}
      {data && <Header d={data} />}
      <div className="tabs">
        {(["chains", "changes", "alerts", "topology", "rca"] as const).map((t) => (
          <button key={t} className={`tab ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>
            {{ chains: "Causal chains", changes: "Changes near the symptom", alerts: "Alerts", topology: "Topology", rca: "AutoRCA" }[t]}
          </button>
        ))}
      </div>
      {tab === "chains" && data && <Chains d={data} />}
      {tab === "changes" && data && <Changes d={data} />}
      {tab === "alerts" && <Alerts name={name} />}
      {tab === "topology" && <TopologyView name={name} closure={data?.closure ?? []} />}
      {tab === "rca" && <Rca name={name} defaultAlert={data?.live?.alert ?? ""} />}
    </>
  );
}

function Header({ d }: { d: ScenarioDetail }) {
  const caseLink = <Link href={`/scenarios/${d.name}/case`}>Open case view →</Link>;
  if (!d.symptom) return <div className="card">No live incident in this scenario. {caseLink}</div>;
  const s = d.symptom;
  return (
    <div className="grid g4">
      <div style={{ gridColumn: "1 / -1" }}>{caseLink} <span className="mut">the graph argument for the AutoRCA verdict: timeline, suspects and raw lineage</span></div>
      <Stat v={<span className="mono">{s.alert.id}</span>} l={`alert on ${s.alert.app} at ${hhmm(s.alert.at)}`} />
      <Stat v={<span className="mono">{s.incident?.id ?? "—"}</span>} l={s.incident ? `incident ${s.incident.severity}, affects ${s.incident.affects.join(", ")}` : "no incident opened before the cutoff"} />
      <Stat v={d.chains.length} l="ranked causal chains (topology gated)" />
      <Stat v={<span className="mono" style={{ fontSize: 16 }}>{hhmm(s.as_of)}</span>} l="as-of cutoff (alert + 30 min)" />
      {s.alert.summary && <div className="card" style={{ gridColumn: "1 / -1" }}><h3>Alert</h3>{s.alert.summary} {s.alert.metric && <Chip>{s.alert.metric}</Chip>} {s.alert.severity && <Chip tone="warn">{s.alert.severity}</Chip>}</div>}
    </div>
  );
}

function ChainView({ links }: { links: { type: string; app: string; at: string; uri: string }[] }) {
  return (
    <div className="chain">
      {links.map((l, i) => (
        <span key={l.uri} className="row" style={{ gap: 6 }}>
          {i > 0 && <span className="arrow">→</span>}
          <span className={`link ${CHANGE_TYPES.has(l.type) ? "change" : ""}`} title={l.uri}>
            <b>{l.type} · {hhmm(l.at).slice(11)}</b>{l.app}
          </span>
        </span>
      ))}
    </div>
  );
}

function Chains({ d }: { d: ScenarioDetail }) {
  if (!d.chains.length) return <div className="card mut">The correlation engine found no chain above the confidence floor (expected for a control scenario).</div>;
  return (
    <div className="grid">
      {d.chains.map((c, i) => (
        <div className="card" key={i}>
          <div className="row" style={{ marginBottom: 8 }}><h2 style={{ margin: 0 }}>#{i + 1}</h2><Chip tone={i === 0 ? "acc" : undefined}>confidence {c.confidence.toFixed(3)}</Chip>
            <span className="mut">root: <span className="mono">{tail(c.links[0]?.uri)}</span></span></div>
          <ChainView links={c.links} />
        </div>
      ))}
    </div>
  );
}

function Changes({ d }: { d: ScenarioDetail }) {
  if (!d.changes.length) return <div className="card mut">No changes in the window.</div>;
  return (
    <div className="card">
      <div className="mut" style={{ marginBottom: 8 }}>Changes on the symptom&apos;s downstream closure from 6 h before the alert to the cutoff.</div>
      <table>
        <thead><tr><th>at</th><th>kind</th><th>app</th><th>what</th><th>id</th></tr></thead>
        <tbody>{d.changes.map((c) => (
          <tr key={c.uri}><td className="mono">{hhmm(c.at)}</td><td><Chip>{c.kind}</Chip></td><td>{c.app}</td><td className="mono">{c.what}</td><td className="mono" title={c.uri}>{c.id.slice(0, 16)}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Alerts({ name }: { name: string }) {
  const { data, error } = useApi<Alert[]>(`/scenarios/${name}/alerts?limit=200`);
  if (error) return <ErrBox msg={error} />;
  if (!data) return <Spin />;
  return (
    <div className="card">
      <table>
        <thead><tr><th>at</th><th>alert</th><th>app</th></tr></thead>
        <tbody>{data.map((a) => <tr key={a.uri}><td className="mono">{hhmm(a.at)}</td><td className="mono">{a.id}</td><td>{a.app}</td></tr>)}</tbody>
      </table>
    </div>
  );
}

function TopologyView({ name, closure }: { name: string; closure: string[] }) {
  const { data, error } = useApi<Topology>(`/scenarios/${name}/topology`);
  const [q, setQ] = useState("");
  if (error) return <ErrBox msg={error} />;
  if (!data) return <Spin />;
  const deps = new Map<string, string[]>();
  data.edges.forEach((e) => deps.set(e.source, [...(deps.get(e.source) ?? []), e.target]));
  const inClosure = new Set(closure);
  const nodes = data.nodes.filter((n) => !q || n.id.includes(q)).sort((a, b) => Number(inClosure.has(b.id)) - Number(inClosure.has(a.id)) || a.id.localeCompare(b.id));
  return (
    <div className="card">
      <div className="row" style={{ marginBottom: 10 }}>
        <span>{data.nodes.length} applications, {data.edges.length} dependsOn edges.</span>
        <span className="sp" />
        <input type="text" placeholder="filter apps" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 260 }} />
      </div>
      <table>
        <thead><tr><th>app</th><th>kind</th><th>tier</th><th>depends on</th></tr></thead>
        <tbody>{nodes.map((n) => (
          <tr key={n.id}>
            <td>{n.id} {inClosure.has(n.id) && <Chip tone="acc">symptom closure</Chip>}</td>
            <td>{n.kind}</td><td>{n.tier}</td>
            <td className="mut">{(deps.get(n.id) ?? []).sort().join(", ")}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Rca({ name, defaultAlert }: { name: string; defaultAlert: string }) {
  const status = useApi<Status>("/status");
  const [alert, setAlert] = useState(defaultAlert);
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (!alert) setAlert(defaultAlert); }, [defaultAlert, alert]);
  useEffect(() => {
    if (!job || job.state === "done" || job.state === "failed") return;
    const t = setTimeout(() => api<Job>(`/jobs/${job.id}`).then(setJob).catch((e: Error) => setErr(e.message)), 3000);
    return () => clearTimeout(t);
  }, [job]);
  const start = () => { setErr(null); api<Job>(`/scenarios/${name}/rca`, { alert }).then(setJob).catch((e: Error) => setErr(e.message)); };
  const busy = job && (job.state === "queued" || job.state === "running");
  const elapsed = job?.started ? Math.round(((job.finished ?? Date.now() / 1000) - job.started)) : 0;
  return (
    <div className="grid">
      <div className="card">
        <div className="mut" style={{ marginBottom: 10 }}>AutoRCA: a DeepAgents commander with seven specialists over the as-of graph (GLM on Fireworks). A run usually takes 2–5 minutes.</div>
        {status.data && !status.data.fireworks_key && <div className="err">FIREWORKS_API_KEY is not set for the backend. Add it to <code>.env</code> and restart <code>scripts/start-backend.sh</code>.</div>}
        <div className="row">
          <input type="text" value={alert} onChange={(e) => setAlert(e.target.value)} placeholder="radar-09000 or INC-1234" style={{ maxWidth: 260 }} />
          <button onClick={start} disabled={!!busy || !alert}>{busy ? "Running…" : "Run AutoRCA"}</button>
          {job && <span className="mut">job {job.id} · {job.state}{elapsed ? ` · ${elapsed}s` : ""}</span>}
        </div>
        {err && <ErrBox msg={err} />}
        {job?.error && <ErrBox msg={job.error} />}
      </div>
      {job?.report && (
        <div className="card">
          <div className="row" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>Report</h2>
            <Chip tone={job.report.verdict === "root_cause_found" ? "ok" : "warn"}>{job.report.verdict}</Chip>
          </div>
          <p>{job.report.summary}</p>
          {job.report.hypotheses.map((h, i) => (
            <div key={i} className="card" style={{ background: "var(--panel2)", marginTop: 10 }}>
              <div className="row"><b>#{i + 1}</b><span className="mono" title={h.root_cause_uri}>{tail(h.root_cause_uri)}</span><Chip tone="acc">confidence {h.confidence.toFixed(2)}</Chip></div>
              <div className="mono mut" style={{ margin: "6px 0" }}>{h.chain.map(tail).join(" → ")}</div>
              <details><summary>Evidence ({h.evidence.length})</summary><ul>{h.evidence.map((e, j) => <li key={j} className="mono">{e}</li>)}</ul></details>
              {h.refuted_alternatives.length > 0 && <details><summary>Refuted alternatives ({h.refuted_alternatives.length})</summary><ul>{h.refuted_alternatives.map((e, j) => <li key={j}>{e}</li>)}</ul></details>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
