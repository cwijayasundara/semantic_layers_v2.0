"use client";
import { useMemo, useState } from "react";
import type { After, AfterChange, AfterSeries } from "@/lib/api";
import { ErrBox } from "@/components/Bits";

const W = 560, H = 170, M = { l: 48, r: 12, t: 26, b: 22 };
const ms = (iso: string) => Date.parse(iso);
const hm = (t: number) => new Date(t).toISOString().slice(11, 16);
const hms = (t: number) => new Date(t).toISOString().slice(11, 19) + "Z";
const fmt = (v: number) => {
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toFixed(a >= 1e7 ? 0 : 1)}M`;
  if (a >= 1e4) return `${(v / 1e3).toFixed(0)}K`;
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  return v.toFixed(2);
};

function niceTicks(lo: number, hi: number, n = 4): number[] {
  if (hi <= lo) { hi = lo + 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  const top = Math.ceil(hi / step - 1e-9) * step;
  for (let v = Math.floor(lo / step) * step; v <= top + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

const markLabel = (m: { from: number; to: number }) => (m.from === m.to ? `${m.from + 1}` : `${m.from + 1}–${m.to + 1}`);
const LABEL_W = (s: string) => 2 + s.length * 6.2 + 4;   // 10 px text from x + 2, then a gap

/** Changes whose marker label would run into the previous label share it ("1–3"). */
function clusters(changes: AfterChange[], x: (t: number) => number) {
  const out: { x: number; from: number; to: number }[] = [];
  changes.forEach((c, i) => {
    const px = x(ms(c.at)), last = out[out.length - 1];
    if (last && px - last.x < Math.max(14, LABEL_W(markLabel(last)))) last.to = i; else out.push({ x: px, from: i, to: i });
  });
  return out;
}

function Chart({ s, asOf, changes, t0, t1 }: { s: AfterSeries; asOf: number; changes: AfterChange[]; t0: number; t1: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const pts = s.points.map(([t, v]) => [ms(t), v] as const);
  const vals = pts.map((p) => p[1]);
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const x = (t: number) => M.l + ((t - t0) / (t1 - t0 || 1)) * (W - M.l - M.r);
  const y = (v: number) => H - M.b - ((v - y0) / (y1 - y0 || 1)) * (H - M.t - M.b);
  const path = pts.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const xTicks: number[] = [];
  const step = (t1 - t0) > 3 * 3600e3 ? 60 * 60e3 : 30 * 60e3;
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) xTicks.push(t);
  const cx = x(asOf), marks = clusters(changes, x);
  const near = hover == null ? null : pts.reduce((b, p) => (Math.abs(p[0] - hover) < Math.abs(b[0] - hover) ? p : b), pts[0]);

  const move = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const vx = ((e.clientX - r.left) / r.width) * W;
    if (vx < M.l || vx > W - M.r) { setHover(null); return; }
    setHover(t0 + ((vx - M.l) / (W - M.l - M.r)) * (t1 - t0));
  };
  if (!pts.length) return <div className="mut">{s.name} on {s.app}: no points.</div>;
  return (
    <figure style={{ margin: 0 }}>
      <figcaption style={{ fontSize: 13, marginBottom: 2 }}><b className="mono">{s.name}</b> <span className="mut">on {s.app} · 1-minute mean{s.aggregate === "mean" && s.apps?.length ? `, averaged over ${s.apps.length} reporting apps (${s.apps.join(", ")})` : ""}</span></figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`${s.name} on ${s.app}, ${hm(t0)} to ${hm(t1)} UTC`}
        onMouseMove={move} onMouseLeave={() => setHover(null)} style={{ display: "block", overflow: "visible" }}>
        <rect x={cx} y={M.t} width={Math.max(0, W - M.r - cx)} height={H - M.t - M.b} fill="var(--mut)" opacity={0.12} />
        <text x={W - M.r - 4} y={H - M.b - 6} fontSize={11} textAnchor="end" fill="var(--mut)">after the agent&apos;s data cut-off</text>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={M.l} x2={W - M.r} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeWidth={1} />
            <text x={M.l - 6} y={y(v) + 3.5} fontSize={10} textAnchor="end" fill="var(--mut)" style={{ fontVariantNumeric: "tabular-nums" }}>{fmt(v)}</text>
          </g>
        ))}
        {xTicks.map((t) => (
          <text key={t} x={x(t)} y={H - 6} fontSize={10} textAnchor="middle" fill="var(--mut)">{hm(t)}</text>
        ))}
        <line x1={cx} x2={cx} y1={M.t - 4} y2={H - M.b} stroke="var(--mut)" strokeWidth={1.5} strokeDasharray="4 3" />
        <text x={cx - 4} y={M.t - 8} fontSize={10} textAnchor="end" fill="var(--mut)">cut-off {hm(asOf)}</text>
        {changes.map((c) => (
          <line key={c.uri} x1={x(ms(c.at))} x2={x(ms(c.at))} y1={M.t} y2={H - M.b} stroke="var(--change)" strokeWidth={1} opacity={0.9} />
        ))}
        {marks.map((m) => (
          <text key={m.from} x={m.x + 2} y={M.t - 8} fontSize={10} textAnchor="start" fill="var(--text)">
            {markLabel(m)}
          </text>
        ))}
        <path d={path} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        {near && (
          <g pointerEvents="none">
            <line x1={x(near[0])} x2={x(near[0])} y1={M.t} y2={H - M.b} stroke="var(--text)" strokeWidth={1} opacity={0.4} />
            <circle cx={x(near[0])} cy={y(near[1])} r={4} fill="var(--accent)" stroke="var(--panel)" strokeWidth={2} />
          </g>
        )}
      </svg>
      <div className="mono" style={{ fontSize: 11, minHeight: 16, color: "var(--text)" }}>
        {near ? <>{hms(near[0])} · {fmt(near[1])}{near[0] > asOf && <span className="mut"> · after the cut-off</span>}</> : <span className="mut">hover for values</span>}
      </div>
      <details>
        <summary className="mut" style={{ fontSize: 12 }}>data table ({pts.length} points)</summary>
        <div style={{ maxHeight: 180, overflow: "auto" }}>
          <table><tbody>{pts.map(([t, v]) => <tr key={t}><td className="mono">{hms(t)}</td><td className="mono" style={{ textAlign: "right" }}>{v}</td></tr>)}</tbody></table>
        </div>
      </details>
    </figure>
  );
}

/** What happened after the agent stopped looking: hidden when nothing changed after the cut-off. */
export default function AfterPanel({ after, error }: { after: After | null; error: string | null }) {
  const [open, setOpen] = useState(false);
  const changes = useMemo(() => [...(after?.changes ?? [])].sort((a, b) => ms(a.at) - ms(b.at)), [after]);
  if (error) return <div style={{ marginTop: 10 }}><ErrBox msg={`after the cut-off: ${error}`} /></div>;
  if (!after || !changes.length) return null;
  const asOf = ms(after.as_of);
  const times = [...after.series.flatMap((s) => s.points.map((p) => ms(p[0]))), ...changes.map((c) => ms(c.at)), asOf];
  const t0 = Math.min(...times), t1 = Math.max(...times);
  return (
    <div className="card" style={{ marginTop: 10, borderStyle: open ? "dashed" : undefined }}>
      <div className="row" style={{ gap: 8 }}>
        <button className={open ? "" : "ghost"} onClick={() => setOpen(!open)} aria-expanded={open} style={{ padding: "4px 10px" }}>
          {open ? "Hide" : "Show"} what happened after the agent&apos;s cut-off
        </button>
        <span className="mut">{changes.length} change{changes.length === 1 ? "" : "s"} after {hm(asOf)}Z, from the full store: the agent never saw any of this</span>
      </div>
      {open && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: 16, marginTop: 10 }}>
            {after.series.map((s) => <Chart key={`${s.name}|${s.app}`} s={s} asOf={asOf} changes={changes} t0={t0} t1={t1} />)}
          </div>
          <div className="mut" style={{ fontSize: 12, margin: "10px 0 4px" }}>Changes {after.label} (numbered as on the charts)</div>
          <table>
            <thead><tr><th>#</th><th>at</th><th>app</th><th>kind</th><th>what</th></tr></thead>
            <tbody>
              {changes.map((c, i) => (
                <tr key={c.uri}>
                  <td className="mono">{i + 1}</td><td className="mono">{hms(ms(c.at))}</td><td>{c.app}</td><td>{c.kind}</td><td>{c.what}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
