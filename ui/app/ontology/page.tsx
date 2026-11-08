"use client";
import { useState } from "react";
import { Ontology, tail, useApi } from "@/lib/api";
import { Chip, ErrBox, Spin } from "@/components/Bits";

export default function OntologyPage() {
  const { data, error } = useApi<Ontology>("/ontology");
  const [tab, setTab] = useState<"taxonomy" | "classes" | "properties">("taxonomy");
  return (
    <>
      <h1>Ontology &amp; taxonomy</h1>
      <div className="sub">The ontology graph loaded at harvest: <code>ontology/core.ttl</code>, the SKOS schemes in <code>ontology/schemes/</code>, and the accepted extensions.</div>
      {error && <ErrBox msg={error} />}
      {!data && !error && <Spin />}
      {data && (
        <>
          <div className="tabs">
            <button className={`tab ${tab === "taxonomy" ? "on" : ""}`} onClick={() => setTab("taxonomy")}>Taxonomies ({data.schemes.length})</button>
            <button className={`tab ${tab === "classes" ? "on" : ""}`} onClick={() => setTab("classes")}>Classes ({data.classes.length})</button>
            <button className={`tab ${tab === "properties" ? "on" : ""}`} onClick={() => setTab("properties")}>Properties ({data.properties.length})</button>
          </div>
          {tab === "taxonomy" && (
            <div className="grid g2">
              {data.schemes.map((s) => (
                <div className="card" key={s.uri}>
                  <h2>{s.label} <span className="mut mono" style={{ fontWeight: 400 }}>{tail(s.uri)}</span></h2>
                  <table><tbody>{s.concepts.map((c) => (
                    <tr key={c.uri}><td style={{ width: "38%" }}><Chip>{c.notation ?? tail(c.uri)}</Chip>{c.broader && <div className="mut mono" style={{ fontSize: 11 }}>⊂ {tail(c.broader)}</div>}</td>
                      <td><b>{c.label}</b>{c.definition && <div className="mut">{c.definition}</div>}</td></tr>
                  ))}</tbody></table>
                </div>
              ))}
            </div>
          )}
          {tab === "classes" && (
            <div className="card"><table>
              <thead><tr><th>class</th><th>subclass of</th><th>comment</th></tr></thead>
              <tbody>{data.classes.map((c) => (
                <tr key={c.uri}><td><b>{c.label}</b><div className="mut mono" style={{ fontSize: 11 }}>{c.uri}</div></td>
                  <td className="mono">{c.parents.map(tail).join(", ")}</td><td>{c.comment}</td></tr>
              ))}</tbody>
            </table></div>
          )}
          {tab === "properties" && (
            <div className="card"><table>
              <thead><tr><th>property</th><th>kind</th><th>domain</th><th>range</th></tr></thead>
              <tbody>{data.properties.map((p) => (
                <tr key={p.uri}><td><b>{p.label ?? tail(p.uri)}</b><div className="mut mono" style={{ fontSize: 11 }}>{p.uri}</div></td>
                  <td><Chip>{p.kind === "ObjectProperty" ? "object" : "datatype"}</Chip></td>
                  <td className="mono">{tail(p.domain)}</td><td className="mono">{tail(p.range)}</td></tr>
              ))}</tbody>
            </table></div>
          )}
        </>
      )}
    </>
  );
}
