"use client";
import type { Layers } from "./CaseGraph";

const hm = (t: number) => new Date(t).toISOString().slice(11, 16) + "Z";
const LAYER_LABEL: Record<keyof Layers, string> = { suspects: "suspects", raw: "raw lineage", chat: "Slack claims", topology: "full topology" };

export default function Toolbar({ layout, setLayout, layers, setLayers, t0, t1, time, setTime, playing, onPlay, tracing, onTrace, hasChain }: {
  layout: "swim" | "force"; setLayout(l: "swim" | "force"): void;
  layers: Layers; setLayers(l: Layers): void;
  t0: number; t1: number; time: number | null; setTime(t: number | null): void;
  playing: boolean; onPlay(): void; tracing: boolean; onTrace(): void; hasChain: boolean;
}) {
  return (
    <div className="card" style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 10, padding: "10px 12px" }}>
      <div className="row">
        <div className="row" style={{ gap: 2 }}>
          {(["swim", "force"] as const).map((l) => (
            <button key={l} className={layout === l ? "" : "ghost"} onClick={() => setLayout(l)} style={{ padding: "4px 10px" }}>
              {l === "swim" ? "Swimlane (time × service)" : "Knowledge graph (force)"}
            </button>
          ))}
        </div>
        <span className="sp" />
        {(Object.keys(LAYER_LABEL) as (keyof Layers)[]).map((k) => (
          <span key={k} className={`toggle ${layers[k] ? "" : "off"}`} onClick={() => setLayers({ ...layers, [k]: !layers[k] })}>{LAYER_LABEL[k]}</span>
        ))}
      </div>
      <div className="row">
        <button className="ghost" onClick={onPlay} style={{ padding: "4px 10px" }}>{playing ? "❚❚ pause" : "▶ replay"}</button>
        <input type="range" min={t0} max={t1} step={15_000} value={time ?? t1} style={{ flex: 1, minWidth: 200 }}
          onChange={(e) => setTime(Number(e.target.value))} aria-label="replay time" />
        <span className="mono" style={{ minWidth: 60 }}>{time == null ? "all" : hm(time)}</span>
        <button className="ghost" onClick={() => setTime(null)} disabled={time == null} style={{ padding: "4px 10px" }}>show all</button>
        <button className={tracing ? "" : "ghost"} onClick={onTrace} disabled={!hasChain} style={{ padding: "4px 10px" }}>{tracing ? "■ stop trace" : "Trace the chain"}</button>
      </div>
    </div>
  );
}
