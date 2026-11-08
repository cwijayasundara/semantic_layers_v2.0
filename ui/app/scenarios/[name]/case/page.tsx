"use client";
import { use, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { After, CaseNode, CasePayload, Scorecard as Score, TrailFinding, hhmm, useApi } from "@/lib/api";
import { Chip, ErrBox, Spin } from "@/components/Bits";
import type { Layers } from "@/components/case/CaseGraph";
import { caseT0, ms } from "@/components/case/swimlane";
import SidePanel from "@/components/case/SidePanel";
import Toolbar from "@/components/case/Toolbar";
import Scorecard from "@/components/case/Scorecard";
import TrailPanel from "@/components/case/TrailPanel";
import AfterPanel from "@/components/case/AfterPanel";

// Cytoscape touches the DOM on import, so the graph renders on the client only.
const CaseGraph = dynamic(() => import("@/components/case/CaseGraph"), { ssr: false, loading: () => <Spin what="Drawing" /> });

const PLAY_STEPS = 150, PLAY_MS = 100, TRACE_MS = 1100;

export default function CasePage({ params, searchParams }: { params: Promise<{ name: string }>; searchParams: Promise<{ source?: string }> }) {
  const { name } = use(params);
  const { source } = use(searchParams);
  const { data, error, loading } = useApi<CasePayload>(`/scenarios/${name}/case${source ? `?source=${encodeURIComponent(source)}` : ""}`);
  return (
    <>
      <div className="row"><Link href={`/scenarios/${name}`}>← {name}</Link></div>
      <h1>Case: {name}</h1>
      {error && <ErrBox msg={error} />}
      {loading && !data && <Spin what="Assembling the case from the as-of store" />}
      {data && <Case name={name} data={data} />}
    </>
  );
}

function Case({ name, data }: { name: string; data: CasePayload }) {
  const m = data.meta;
  const [layout, setLayout] = useState<"swim" | "force">("swim");
  const [layers, setLayers] = useState<Layers>({ suspects: true, raw: true, chat: true, topology: false });
  const [time, setTime] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [traceIdx, setTraceIdx] = useState<number | null>(null);
  const [tracing, setTracing] = useState(false);
  const [selected, setSelected] = useState<CaseNode | null>(null);
  const [finding, setFinding] = useState<TrailFinding | null>(null);
  const [onScreen, setOnScreen] = useState<ReadonlySet<string>>(new Set());
  const highlight = useMemo(() => (finding ? new Set([finding.uri, ...finding.cites]) : null), [finding]);
  // the scorecard is of the eval run the report came from; a job's case falls back to the backend default run
  const run = m.source?.startsWith("eval:") ? m.source.slice(5) : null;
  const score = useApi<Score>(`/scenarios/${name}/scorecard${run ? `?run=${encodeURIComponent(run)}` : ""}`);
  const after = useApi<After>(`/scenarios/${name}/after${m.source ? `?source=${encodeURIComponent(m.source)}` : ""}`);
  const t0 = useMemo(() => caseT0(m, data.nodes), [m, data.nodes]);
  const t1 = ms(m.as_of);
  const hops = useMemo(() => data.nodes.filter((n) => n.hop != null).sort((a, b) => a.hop! - b.hop!), [data.nodes]);

  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => setTime((t) => {
      const next = (t == null || t >= t1 ? t0 : t) + (t1 - t0) / PLAY_STEPS;
      if (next >= t1) { setPlaying(false); return t1; }
      return next;
    }), PLAY_MS);
    return () => clearInterval(id);
  }, [playing, t0, t1]);

  useEffect(() => {
    if (!tracing) return;
    const id = setInterval(() => setTraceIdx((i) => {
      const next = i == null ? 0 : i + 1;
      if (next >= hops.length) { setTracing(false); return i; }
      return next;
    }), TRACE_MS);
    return () => clearInterval(id);
  }, [tracing, hops.length]);
  useEffect(() => { if (traceIdx != null && hops[traceIdx]) setSelected(hops[traceIdx]); }, [traceIdx, hops]);

  const onTrace = () => {
    if (tracing) { setTracing(false); setTraceIdx(null); return; }
    setTraceIdx(0); setTracing(true);
  };
  const onPlay = () => { if (!playing && (time == null || time >= t1)) setTime(t0); setPlaying(!playing); };
  const select = (n: CaseNode) => { setTracing(false); setTraceIdx(null); setSelected(n); };

  return (
    <>
      <div className="sub row" style={{ gap: 8 }}>
        <span>alert <span className="mono">{m.alert.id}</span> on <b>{m.alert.app}</b> at {hhmm(m.alert.at)}</span>
        <Chip tone={m.verdict === "root_cause_found" ? "ok" : "warn"}>{m.verdict}</Chip>
        <span>· as-of cut-off {hhmm(m.as_of)}</span>
        <span className="mut">· report: {m.source ?? "latest eval"}{m.arm ? ` (${m.arm} arm)` : ""}</span>
      </div>
      {score.data && <Scorecard score={score.data} scenario={name} />}
      {score.error && <div className="mut" style={{ marginBottom: 10 }}>Scorecard unavailable: {score.error}</div>}
      <Toolbar layout={layout} setLayout={setLayout} layers={layers} setLayers={setLayers} t0={t0} t1={t1}
        time={time} setTime={(t) => { setPlaying(false); setTime(t); }} playing={playing} onPlay={onPlay}
        tracing={tracing} onTrace={onTrace} hasChain={hops.length > 0} />
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 360px", gap: 12 }}>
        <div className="card" style={{ padding: 4, height: "72vh", minHeight: 560 }}>
          <CaseGraph data={data} layout={layout} layers={layers} time={time} trace={traceIdx == null ? null : hops[traceIdx]?.hop ?? null}
            highlight={highlight} onSelect={select} onVisible={setOnScreen} />
        </div>
        <div className="card" style={{ height: "72vh", minHeight: 560, overflow: "auto" }}>
          <SidePanel scenario={name} data={data} node={selected} chatOn={layers.chat} onClear={() => setSelected(null)} onSelect={select} />
        </div>
      </div>
      <div className="legend">
        <span><i style={{ background: "var(--warn)" }} />★ root (agent)</span>
        <span><i style={{ background: "var(--bad)" }} />causal chain</span>
        <span><i style={{ background: "#f39c6b" }} />other evidence</span>
        <span><i style={{ background: "var(--panel2)", border: "1px dashed var(--mut)", borderRadius: 0, transform: "rotate(45deg)" }} />suspect</span>
        <span><i style={{ background: "var(--accent)", borderRadius: 2 }} />service lane (red once degraded)</span>
        <span><i style={{ background: "var(--ok)", borderRadius: 0 }} />raw file</span>
        <span><i style={{ background: "#b48cff", borderRadius: 2 }} />Slack claim</span>
        {layers.chat && m.chat_note && <span>· Slack layer: {m.chat_note}</span>}
      </div>
      <TrailPanel trail={data.trail} active={finding?.uri ?? null} onFinding={setFinding} onScreen={(u) => onScreen.has(u)} />
      <AfterPanel after={after.data} error={after.error} />
    </>
  );
}
