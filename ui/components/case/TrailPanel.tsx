"use client";
import type { Trail, TrailFinding } from "@/lib/api";
import { tail } from "@/lib/api";

const hms = (iso: string) => iso.slice(11, 19);
const PREDATES = "This run predates trail recording.";

/** The agent's investigation, in order: every tool call, and every finding a specialist recorded. */
export default function TrailPanel({ trail, active, onFinding, onScreen }: {
  trail: Trail | null; active: string | null; onFinding(f: TrailFinding | null): void;
  onScreen(uri: string): boolean;
}) {
  if (!trail) {
    return (
      <div className="card" style={{ marginTop: 10 }}>
        <b>Investigation trail</b>
        <div className="mut" style={{ marginTop: 6 }}>{PREDATES}</div>
      </div>
    );
  }
  const { steps, findings } = trail;
  return (
    <div className="card" style={{ marginTop: 10 }}>
      <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
        <b>Investigation trail</b>
        <span className="mut">{steps.length} tool call{steps.length === 1 ? "" : "s"} · {findings.length} finding{findings.length === 1 ? "" : "s"}</span>
        {active && <button className="ghost" style={{ padding: "2px 8px" }} onClick={() => onFinding(null)}>clear highlight</button>}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 12, marginTop: 8 }}>
        <div>
          <div className="mut" style={{ fontSize: 12, marginBottom: 4 }}>Specialist findings (click one to highlight what it cites on the graph)</div>
          {!findings.length && <div className="mut">No findings were recorded.</div>}
          <div style={{ maxHeight: 420, overflow: "auto", display: "flex", flexDirection: "column", gap: 6 }}>
            {findings.map((f) => {
              const on = active === f.uri;
              const shown = f.cites.filter(onScreen).length;
              return (
                <button key={f.uri} className="ghost" onClick={() => onFinding(on ? null : f)} aria-pressed={on}
                  style={{ textAlign: "left", padding: "8px 10px", display: "block", width: "100%",
                    border: `1px solid ${on ? "var(--accent)" : "var(--line)"}`, background: on ? "var(--panel2)" : undefined }}>
                  <div className="row" style={{ gap: 6 }}>
                    <span className="chip acc">{f.specialist}</span>
                    {f.app && <b>{f.app}</b>}
                    <span className="sp" />
                    <span className="mono">confidence {f.confidence == null ? "—" : f.confidence.toFixed(2)}</span>
                  </div>
                  <div style={{ marginTop: 4 }}>{f.claim ?? <span className="mut">(no claim text)</span>}</div>
                  <div className="mut" style={{ fontSize: 12, marginTop: 2 }}>
                    cites {f.cites.length} item{f.cites.length === 1 ? "" : "s"}{f.cites.length > 0 && `, ${shown} on screen`}
                    {on && f.cites.length > 0 && <span className="mono">: {f.cites.map(tail).join(", ")}</span>}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
        <div>
          <div className="mut" style={{ fontSize: 12, marginBottom: 4 }}>Tool calls, in order</div>
          {!steps.length && <div className="mut">No tool calls were recorded.</div>}
          <ol style={{ maxHeight: 420, overflow: "auto", margin: 0, paddingLeft: 0, listStyle: "none" }}>
            {steps.map((s) => (
              <li key={s.i} style={{ borderBottom: "1px solid var(--line)", padding: "4px 0" }}>
                <div className="row" style={{ gap: 8 }}>
                  <span className="mut mono" style={{ minWidth: 28, textAlign: "right" }}>{s.i + 1}</span>
                  <b className="mono">{s.tool}</b>
                  <span className="sp" />
                  <span className="mut mono">{hms(s.at)}</span>
                </div>
                <details style={{ marginLeft: 36 }}>
                  <summary className="mut" style={{ fontSize: 12 }}>args</summary>
                  <pre>{JSON.stringify(s.args, null, 2)}</pre>
                </details>
                <details style={{ marginLeft: 36 }}>
                  <summary className="mut" style={{ fontSize: 12 }}>output{s.output ? ` (${s.output.length} chars)` : " (none)"}</summary>
                  <pre>{s.output}</pre>
                </details>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </div>
  );
}
