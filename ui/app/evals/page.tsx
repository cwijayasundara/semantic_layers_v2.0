"use client";
import { useEffect, useState } from "react";
import { EvalDetail, EvalRun, useApi } from "@/lib/api";
import { ErrBox, Spin } from "@/components/Bits";

/** Just enough Markdown for summary.md: headings, bullets, pipe tables, paragraphs. */
function Md({ text }: { text: string }) {
  const out: React.ReactNode[] = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith("|")) {
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        if (!/^\|[\s|:-]+\|$/.test(lines[i])) rows.push(lines[i].slice(1, -1).split("|").map((c) => c.trim()));
        i++;
      }
      i--;
      out.push(<div key={i} style={{ overflowX: "auto" }}><table><thead><tr>{rows[0].map((c, j) => <th key={j}>{c}</th>)}</tr></thead>
        <tbody>{rows.slice(1).map((r, k) => <tr key={k}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody></table></div>);
    } else if (l.startsWith("# ")) out.push(<h1 key={i}>{l.slice(2)}</h1>);
    else if (l.startsWith("## ")) out.push(<h2 key={i}>{l.slice(3)}</h2>);
    else if (l.startsWith("- ")) out.push(<li key={i}>{l.slice(2)}</li>);
    else if (l.trim()) out.push(<p key={i}>{l}</p>);
  }
  return <div className="md">{out}</div>;
}

export default function Evals() {
  const { data: runs, error } = useApi<EvalRun[]>("/evals");
  const [run, setRun] = useState<string | null>(null);
  useEffect(() => { if (runs?.length && !run) setRun(runs[0].name); }, [runs, run]);
  const detail = useApi<EvalDetail>(run ? `/evals/${run}` : null);
  return (
    <>
      <h1>Evals</h1>
      <div className="sub">Eval runs under <code>data/evals</code> (create one with <code>e2ekg eval</code>). The scorer is the only reader of the answer key.</div>
      {error && <ErrBox msg={error} />}
      {runs && !runs.length && <div className="card">No eval runs yet.</div>}
      {runs && runs.length > 0 && (
        <div className="row" style={{ marginBottom: 14 }}>
          {runs.map((r) => <button key={r.name} className={r.name === run ? "" : "ghost"} onClick={() => setRun(r.name)}>{r.name}</button>)}
        </div>
      )}
      {detail.error && <ErrBox msg={detail.error} />}
      {run && !detail.data && !detail.error && <Spin />}
      {detail.data && <div className="card"><Md text={detail.data.summary_md} /></div>}
    </>
  );
}
