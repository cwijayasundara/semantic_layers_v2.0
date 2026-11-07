"use client";
import Link from "next/link";
import { ScenarioRow, hhmm, useApi } from "@/lib/api";
import { Chip, ErrBox, Spin } from "@/components/Bits";

export default function Home() {
  const { data, error, loading } = useApi<ScenarioRow[]>("/scenarios");
  return (
    <>
      <h1>Scenarios</h1>
      <div className="sub">Each scenario is a harvested knowledge graph: a full history store and the as-of store (what was observable at alert + 30 min) that AutoRCA reads.</div>
      {error && <ErrBox msg={error} />}
      {loading && !data && <Spin />}
      {data && !data.length && <div className="card">No harvested scenarios. Run <code>scripts/ingest.sh</code>.</div>}
      {data && data.length > 0 && (
        <div className="card">
          <table>
            <thead><tr><th>scenario</th><th>live alert</th><th>symptom</th><th>as of</th><th>quads (full / as-of)</th><th>SHACL</th><th>enrichers</th></tr></thead>
            <tbody>
              {data.map((s) => (
                <tr key={s.name}>
                  <td><Link href={`/scenarios/${s.name}`}><b>{s.name}</b></Link></td>
                  <td className="mono">{s.live?.alert ?? <span className="mut">none</span>}</td>
                  <td className="mono">{s.live ? `${s.live.symptom_kind} ${s.live.symptom}` : ""}</td>
                  <td className="mono">{s.as_of ? hhmm(s.as_of) : ""}</td>
                  <td className="mono">{s.quads?.toLocaleString() ?? "?"} / {s.as_of_quads?.toLocaleString() ?? "?"}</td>
                  <td>{s.shacl_ok === true ? <Chip tone="ok">clean</Chip> : s.shacl_ok === false ? <Chip tone="bad">violations</Chip> : <Chip>skipped</Chip>}</td>
                  <td>{s.failed_enrichers.length ? <Chip tone="bad">{s.failed_enrichers.join(", ")} failed</Chip> : <Chip tone="ok">all ok</Chip>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
