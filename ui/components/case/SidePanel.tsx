"use client";
import { CSSProperties, Fragment, ReactNode, useState } from "react";
import { api, CaseEdge, CaseNode, CasePayload, ExpandPayload, HypothesisGraph, Lineage, LineageRecord, Rows, hhmm, tail, useApi } from "@/lib/api";
import { Chip, ErrBox, Spin } from "@/components/Bits";

const box: CSSProperties = { border: "1px solid var(--line)", borderRadius: 8, padding: "8px 10px", margin: "8px 0", background: "var(--panel2)" };
const quote: CSSProperties = { ...box, borderLeft: "3px solid var(--accent)", fontStyle: "italic", whiteSpace: "pre-wrap" };
const kv: CSSProperties = { display: "grid", gridTemplateColumns: "max-content 1fr", gap: "2px 10px", fontSize: 12 };

const ROLE: Record<string, { text: (n: CaseNode) => string; color: string }> = {
  root: { text: (n) => `★ root cause (agent's confidence ${(n.confidence ?? 0).toFixed(2)})`, color: "var(--warn)" },
  best_candidate: { text: (n) => `best candidate, not accepted (confidence ${(n.confidence ?? 0).toFixed(2)})`, color: "var(--warn)" },
  chain: { text: (n) => `causal chain hop #${n.hop}`, color: "var(--bad)" },
  evidence: { text: () => "cited by the agent as evidence", color: "#f39c6b" },
  suspect: { text: () => "suspect: a change in the window", color: "var(--mut)" },
  refuted_as_root: { text: () => "refuted as root by the agent", color: "var(--mut)" },
  symptom: { text: () => "symptom", color: "var(--bad)" },
  chat: { text: (n) => `Slack claim (${String(n.props.kind ?? "claim")})`, color: "#b48cff" },
  raw: { text: () => "raw file", color: "var(--ok)" },
  app: { text: () => "service", color: "var(--accent)" },
};

const show = (v: unknown): ReactNode => {
  if (Array.isArray(v)) return v.map((x, i) => <div key={i}>{show(x)}</div>);
  const s = String(v ?? "");
  return s.startsWith("http") ? <span title={s}>{tail(s)}</span> : s;
};

function timing(m: number | null): string {
  if (m == null) return "no onset was found, so timing against onset cannot be computed";
  if (m === 0) return "landed at the onset";
  return m > 0 ? `landed ${m} min before the onset` : `landed ${-m} min after the onset`;
}

function Facts({ n, alertApp }: { n: CaseNode; alertApp: string }) {
  if (!n.facts) return null;
  const f = n.facts;
  return (
    <div style={box}>
      <h3>Facts (computed from the graph)</h3>
      <div>• {timing(f.minutes_before_onset)}</div>
      <div>• dependency path to {alertApp}: {f.has_path ? <b className="ok">yes</b> : <b className="bad">no</b>}</div>
      {f.has_path && f.path.length > 0 && <div className="mono mut" style={{ marginLeft: 12 }}>{f.path.join(", ")}</div>}
    </div>
  );
}

function Record_({ r }: { r: LineageRecord }) {
  const p = r.processed;
  if (r.integrity_error) return (
    <div style={{ ...box, borderColor: "var(--bad)" }}>
      <div className="err" style={{ fontSize: 12, marginBottom: 4 }}>raw bytes failed verification: {r.integrity_error}</div>
      <div style={kv}>
        <span className="mut">raw file</span><span className="mono">{r.file}</span>
        <span className="mut">byte offset</span><span className="mono">{r.offset} (+{r.length} bytes)</span>
        <span className="mut">sha</span><span className="mono">{r.sha}</span>
      </div>
    </div>
  );
  return (
    <div style={box}>
      <div style={kv}>
        <span className="mut">raw file</span><span className="mono">{r.file}</span>
        <span className="mut">byte offset</span><span className="mono">{r.offset} (+{r.length} bytes)</span>
        <span className="mut">sha</span><span className="mono">{r.sha}</span>
      </div>
      <div className="mut" style={{ fontSize: 12, marginTop: 6 }}>original line, as written</div>
      {r.line ? <pre style={{ maxHeight: 140 }}>{r.line}</pre> : <div className="warn" style={{ fontSize: 12 }}>this record has no raw line (the file span could not be read)</div>}
      <div className="mut" style={{ fontSize: 12, marginTop: 6 }}>processed row</div>
      {p ? (
        <div style={kv}>
          <span className="mut">table</span><span className="mono">{p.table}</span>
          <span className="mut">time</span><span className="mono">{p.ts_raw ?? "—"} → {r.ts_utc ?? "—"}</span>
          <span className="mut">skew</span><span className="mono">{p.skew_ms ?? 0} ms{p.tz_hint ? ` (tz hint ${p.tz_hint})` : ""}</span>
          <span className="mut">app resolution</span><span className="mono">{p.app_method ?? "—"}{p.app_confidence != null ? ` (confidence ${p.app_confidence})` : ""}</span>
          {p.template && <><span className="mut">template</span><span className="mono">{p.template}</span></>}
        </div>
      ) : <div className="mut" style={{ fontSize: 12 }}>no processed row matched this raw id (ts_utc {r.ts_utc ?? "—"})</div>}
    </div>
  );
}

function LineageView({ scenario, n }: { scenario: string; n: CaseNode }) {
  const { data, error, loading } = useApi<Lineage>(`/scenarios/${scenario}/lineage?uri=${encodeURIComponent(n.id)}`);
  if (error) return <ErrBox msg={error} />;
  if (loading || !data) return <Spin what="Reading raw lines" />;
  return (
    <>
      {data.records.length === 0 && <div className="mut" style={{ marginTop: 6 }}>No raw records were found for this node.</div>}
      {data.records.map((r) => <Record_ key={r.raw_id} r={r} />)}
    </>
  );
}

/** The original raw lines behind one raw id — the agent's raw_evidence tool in id mode. */
function RawLines({ scenario, id }: { scenario: string; id: string }) {
  const { data, error, loading } = useApi<Rows>(`/scenarios/${scenario}/raw?id=${encodeURIComponent(id)}&limit=5`);
  if (error) return <ErrBox msg={error} />;
  if (loading || !data) return <Spin what="Reading raw lines" />;
  return (
    <div style={{ ...box, borderColor: "var(--ok)" }}>
      {data.rows.length === 0 && <div className="mut" style={{ fontSize: 12 }}>No raw records matched this id.</div>}
      {data.rows.map((r, i) => (
        <div key={r.raw_id ?? i} style={{ margin: "6px 0" }}>
          <div className="mono mut" style={{ fontSize: 10, wordBreak: "break-all" }}>{r.raw_id}{r.ts ? ` · ${hhmm(r.ts)}` : ""}{r.len ? ` · cut (${r.len} bytes)` : ""}</div>
          {r.line ? <pre style={{ maxHeight: 140 }}>{r.line}</pre> : <div className="warn" style={{ fontSize: 12 }}>{r.error ?? "no raw line"}</div>}
          {r.truncated != null && <div className="mut" style={{ fontSize: 11 }}>… {r.truncated} more lines not shown</div>}
        </div>
      ))}
    </div>
  );
}

/** The evidence the hypothesis cites: URIs select their node; raw ids (not graph nodes) load
    the original lines. */
function Arguments({ scenario, hyp, onSelect }: { scenario: string; hyp: HypothesisGraph; onSelect(n: CaseNode): void }) {
  const [openRaw, setOpenRaw] = useState<string | null>(null);
  const rawIds = hyp.evidence.filter((e) => !e.startsWith("http"));
  return (
    <>
      <h3 style={{ marginTop: 12 }}>Hypothesis #{hyp.i + 1} arguments</h3>
      {hyp.confidence != null && <div style={{ marginBottom: 4 }}><Chip tone="acc">confidence {hyp.confidence.toFixed(2)}</Chip></div>}
      {rawIds.length > 0 && <div className="mut" style={{ fontSize: 12 }}>{rawIds.length} raw-id evidence item{rawIds.length === 1 ? "" : "s"} — not graph nodes; click one to read the original lines.</div>}
      {rawIds.map((id) => (
        <div key={id} style={{ margin: "4px 0" }}>
          <a href="#" className="mono" style={{ fontSize: 11, wordBreak: "break-all" }} onClick={(e) => { e.preventDefault(); setOpenRaw(openRaw === id ? null : id); }}>{openRaw === id ? "▾ " : "▸ "}{id}</a>
          {openRaw === id && <RawLines key={id} scenario={scenario} id={id} />}
        </div>
      ))}
      {hyp.refuted.length > 0 && (
        <div style={{ ...box, borderColor: "var(--warn)" }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--warn)" }}>✗ {hyp.refuted.length} alternative{hyp.refuted.length === 1 ? "" : "s"} refuted as root</div>
          {hyp.refuted.map((r, i) => (
            <div key={i} style={{ margin: "4px 0", fontSize: 12 }}>
              {r.uris.map(tail).join(", ")} — <span className="mut">{r.reason}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function Overview({ data, chatOn }: { data: CasePayload; chatOn: boolean }) {
  const m = data.meta;
  return (
    <>
      <div className="row" style={{ marginBottom: 6 }}>
        <Chip tone={m.verdict === "root_cause_found" ? "ok" : "warn"}>{m.verdict}</Chip>
        {m.confidence != null && <Chip tone="acc">confidence {m.confidence.toFixed(2)}</Chip>}
      </div>
      <div className="mut" style={{ fontSize: 12 }}>Click a node to see its roles, facts, the agent&apos;s reasoning and its raw lineage.</div>
      {m.summary && <><h3 style={{ marginTop: 12 }}>Agent&apos;s summary</h3><div style={{ ...quote, maxHeight: 360, overflow: "auto" }}>{m.summary}</div></>}
      {m.unattributed_notes.length > 0 && <h3 style={{ marginTop: 12 }}>Notes the agent did not tie to a node</h3>}
      {m.unattributed_notes.map((t, i) => (
        <div key={i} style={quote}><div className="mut" style={{ fontStyle: "normal", fontSize: 11 }}>unattributed agent note</div>{t}</div>
      ))}
      {chatOn && m.chat_note && <div className="mut" style={{ fontSize: 12, marginTop: 8 }}>Slack layer: {m.chat_note}</div>}
      {(m.tool_issues?.length ?? 0) > 0 && (
        <div style={{ ...box, borderColor: "var(--warn)" }}>
          <div className="warn" style={{ fontSize: 12, fontWeight: 600 }}>⚠ this case may be incomplete: some tool calls failed or were cut to fit</div>
          {m.tool_issues!.map((t, i) => <div key={i} className="mono" style={{ fontSize: 11 }}>{t.tool}: {t.issue}</div>)}
        </div>
      )}
    </>
  );
}

/** Drill-through: one hop of the as-of graph around `n`, merged into the case view. */
function ExpandButton({ scenario, n, expanded, onExpand }: {
  scenario: string; n: CaseNode; expanded: boolean; onExpand(nodes: CaseNode[], edges: CaseEdge[]): void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async () => {
    setBusy(true); setErr(null);
    try {
      const p = await api<ExpandPayload>(`/scenarios/${scenario}/case/expand?uri=${encodeURIComponent(n.id)}`);
      onExpand(p.nodes, p.edges);
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
  return (
    <div style={{ marginTop: 10 }}>
      <button className="ghost" onClick={run} disabled={busy} style={{ padding: "3px 10px" }}>
        {busy ? "expanding…" : expanded ? "⊕ re-expand one hop" : "⊕ expand one hop in the as-of graph"}
      </button>
      {expanded && <span className="mut" style={{ fontSize: 11, marginLeft: 8 }}>neighbors drawn on the graph</span>}
      {err && <ErrBox msg={err} />}
    </div>
  );
}

export default function SidePanel({ scenario, data, node, chatOn, hyp, expandedUris, onClear, onSelect, onExpand }: {
  scenario: string; data: CasePayload; node: CaseNode | null; chatOn: boolean; hyp: HypothesisGraph | null;
  expandedUris: ReadonlySet<string>; onClear(): void; onSelect(n: CaseNode): void; onExpand(nodes: CaseNode[], edges: CaseEdge[]): void;
}) {
  if (!node) return (
    <>
      <Overview data={data} chatOn={chatOn} />
      {hyp && <Arguments scenario={scenario} hyp={hyp} onSelect={onSelect} />}
    </>
  );
  const n = node;
  const refutations = (data.hypotheses ?? []).flatMap((h) => h.refuted.filter((r) => r.uris.includes(n.id)).map((r) => ({ h: h.i, ...r })));
  const expandable = n.id.startsWith("http") && !n.missing && n.cat !== "finding";
  const sources = n.cat === "raw" ? data.edges.filter((e) => e.target === n.id).map((e) => data.nodes.find((x) => x.id === e.source)).filter((x): x is CaseNode => !!x) : [];
  return (
    <>
      <div className="row" style={{ marginBottom: 6 }}>
        <button className="ghost" onClick={onClear} style={{ padding: "3px 10px" }}>← case overview</button>
      </div>
      <h2 style={{ wordBreak: "break-word" }}>{n.label}</h2>
      <div className="mono mut" style={{ fontSize: 11, wordBreak: "break-all" }}>{n.id}</div>
      <div style={{ margin: "8px 0" }}>
        {n.roles.map((r) => {
          const def = ROLE[r] ?? { text: () => r, color: "var(--mut)" };
          return <div key={r} style={{ borderLeft: `4px solid ${def.color}`, padding: "3px 8px", margin: "3px 0", background: "var(--panel2)", borderRadius: 4, color: def.color, fontWeight: 600, fontSize: 12 }}>{def.text(n)}</div>;
        })}
      </div>
      {n.missing && <div className="err" style={{ fontSize: 12 }}>Not in the as-of store: drawn from the agent&apos;s report so nothing is hidden.</div>}
      {n.cat === "finding" && !n.missing && <div className="mut" style={{ fontSize: 12 }}>Recorded by the agent during the run (from its trail), so it is not in the as-of store and has no data time; drawn just after the latest item it cites.</div>}
      <Facts n={n} alertApp={data.meta.alert.app} />
      {n.agent_reason && <><h3 style={{ marginTop: 10 }}>Agent&apos;s reasoning</h3><div style={quote}>“{n.agent_reason}”</div></>}
      {refutations.length > 0 && (
        <div style={{ ...box, borderColor: "var(--warn)" }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--warn)" }}>✗ refuted by the agent</div>
          {refutations.map((r, i) => <div key={i} className="mut" style={{ fontSize: 12 }}>hypothesis #{r.h + 1}: {r.reason}</div>)}
        </div>
      )}
      {expandable && onExpand && <ExpandButton scenario={scenario} n={n} expanded={expandedUris.has(n.id)} onExpand={onExpand} />}
      {n.discussed === false && <div style={{ ...box, color: "var(--mut)" }}>not discussed by the agent</div>}
      {n.cat === "chat" && typeof n.props["chat:quote"] === "string" && <div style={{ ...quote, borderLeftColor: "#b48cff" }}>“{n.props["chat:quote"]}”</div>}

      <h3 style={{ marginTop: 12 }}>Properties</h3>
      <div style={kv}>
        <span className="mut">class</span><span className="mono">{n.cls ?? "—"}</span>
        <span className="mut">named graph</span><span className="mono">{n.graph ?? "—"}</span>
        {n.app && <><span className="mut">service</span><span className="mono">{n.app}</span></>}
        {n.at && <><span className="mut">at</span><span className="mono">{hhmm(n.at)}</span></>}
        {n.cat === "app" && <><span className="mut">call depth</span><span className="mono">{n.hops == null ? `no dependency path to ${data.meta.alert.app}` : `${n.hops} (negative = caller of ${data.meta.alert.app})`}</span></>}
        {n.cat === "app" && <><span className="mut">first degraded</span><span className="mono">{n.first_degraded ? hhmm(n.first_degraded) : "—"}</span></>}
        {Object.entries(n.props).map(([k, v]) => <Fragment key={k}><span className="mut mono">{k}</span><span className="mono" style={{ wordBreak: "break-word" }}>{show(v)}</span></Fragment>)}
      </div>

      {n.cat === "raw" ? (
        <>
          <h3 style={{ marginTop: 12 }}>Graph nodes read from this file</h3>
          {sources.length === 0 && <div className="mut">Cited directly by the agent as raw evidence.</div>}
          {sources.map((s) => <div key={s.id}><a href="#" onClick={(e) => { e.preventDefault(); onSelect(s); }}>{s.cat} · {s.label}</a></div>)}
        </>
      ) : n.cat !== "app" && n.cat !== "finding" && !n.missing && n.id.startsWith("http") ? (
        <><h3 style={{ marginTop: 12 }}>Lineage: raw → processed → graph</h3><LineageView key={n.id} scenario={scenario} n={n} /></>
      ) : null}
      {hyp && <Arguments scenario={scenario} hyp={hyp} onSelect={onSelect} />}
    </>
  );
}
