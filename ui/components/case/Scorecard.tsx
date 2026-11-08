"use client";
import type { ArmGroup, ScoreArm, Scorecard as Score, ScoreThis } from "@/lib/api";
import { Mark } from "@/components/Bits";

const ARM_LABEL: Record<string, string> = {
  graph: "the graph agent",
  graph_no_evidence_tool: "the agent without the evidence tool",
  raw_only: "the agent over raw files",
  engine_top: "the correlation engine",
  engine_no_topology: "the engine without topology gating",
  engine_no_explain_away: "the engine without explain-away",
};
const GROUPS: { group: ArmGroup; title: string }[] = [
  { group: "agent", title: "LLM agent arms" },
  { group: "engine", title: "Correlation engine arms (no LLM)" },
];
// arms that do not use the graph's dependency topology
const NO_TOPOLOGY = new Set(["raw_only", "engine_no_topology"]);

const NUM = { textAlign: "right", padding: "4px 8px", whiteSpace: "nowrap" } as const;
const CELL = { padding: "4px 8px", whiteSpace: "nowrap" } as const;
const pct = (v: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const hits = (a: ScoreArm) => Math.round((a.top1 ?? 0) * a.scenarios);
const frac = (a: ScoreArm) => `${hits(a)}/${a.scenarios}`;
const list = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);
const label = (arm: string) => ARM_LABEL[arm] ?? arm;

/** The headline, computed from the run: a clause is dropped when its arm is not in the run. */
export function headline(arms: ScoreArm[]): string | null {
  const by = new Map(arms.map((a) => [a.arm, a]));
  const clauses: string[] = [];
  const topo = arms.filter((a) => !NO_TOPOLOGY.has(a.arm) && a.scenarios > 0);
  const perfect = topo.filter((a) => hits(a) === a.scenarios);
  if (perfect.length) {
    const n = new Set(perfect.map((a) => a.scenarios)).size === 1 ? `${perfect[0].scenarios}/${perfect[0].scenarios}` : "every scenario";
    clauses.push(`With the graph's topology, ${list(perfect.map((a) => label(a.arm)))} ${perfect.length > 1 ? "find" : "finds"} the right root in ${n}`);
  } else if (topo.length) {
    const best = [...topo].sort((a, b) => (b.top1 ?? 0) - (a.top1 ?? 0))[0];
    clauses.push(`With the graph's topology, the best arm, ${label(best.arm)}, finds the right root in ${frac(best)}`);
  }
  const noTopo = by.get("engine_no_topology");
  if (noTopo) clauses.push(`without topology gating the engine gets ${frac(noTopo)}`);
  const raw = by.get("raw_only");
  if (raw) clauses.push(`the agent over raw files gets ${frac(raw)}`);
  if (!clauses.length) return null;
  const s = clauses.join("; ");
  return s[0].toUpperCase() + s.slice(1) + ".";
}

function Verdict({ t }: { t: ScoreThis | undefined }) {
  if (!t) return <span className="mut">not run</span>;
  return (
    <span title={t.root_app ? `root app: ${t.root_app.split("/").pop()}` : undefined}>
      <Mark ok={t.top1} /> <span className="mono">{t.verdict}</span>{t.trap && <span className="bad"> · took the trap</span>}
    </span>
  );
}

export default function Scorecard({ score, scenario }: { score: Score; scenario: string }) {
  const note = score.notes.evidence_precision;
  const head = headline(score.arms);
  const thisBy = new Map(score.this_scenario.map((t) => [t.arm, t]));
  return (
    <div className="card" style={{ marginBottom: 10, padding: "10px 12px" }}>
      <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
        <b>Scorecard</b>
        <span className="mut">eval run <span className="mono">{score.run}</span>, scored against the answer key</span>
      </div>
      {head && <div style={{ margin: "6px 0 8px", fontSize: 15 }}>{head}</div>}
      <table style={{ width: "100%", fontVariantNumeric: "tabular-nums", fontSize: 13 }}>
        <thead>
          <tr>
            <th style={CELL}>arm</th>
            <th style={NUM} title="right root ranked first, across scenarios">top-1</th>
            <th style={NUM} title="right root in the top three, across scenarios">top-3</th>
            <th style={NUM} title={note}>evidence precision¹</th>
            <th style={NUM} title="share of hero runs that blamed the trap change">trap rate</th>
            <th style={CELL}>this scenario ({scenario})</th>
          </tr>
        </thead>
        {GROUPS.map(({ group, title }) => {
          const arms = score.arms.filter((a) => a.group === group);
          if (!arms.length) return null;
          return (
            <tbody key={group}>
              <tr>
                <td colSpan={6} className="mut" style={{ ...CELL, paddingTop: 8, fontSize: 12, textTransform: "uppercase", letterSpacing: 0.4 }}>{title}</td>
              </tr>
              {arms.map((a) => (
                <tr key={a.arm}>
                  <td style={CELL}><span className="mono">{a.arm}</span> <span className="mut">· {label(a.arm)}</span></td>
                  <td style={NUM}>{frac(a)}</td>
                  <td style={NUM}>{pct(a.top3)}</td>
                  <td style={NUM}>{pct(a.evidence_precision)}</td>
                  <td style={NUM}>{pct(a.trap_rate)}</td>
                  <td style={CELL}><Verdict t={thisBy.get(a.arm)} /></td>
                </tr>
              ))}
            </tbody>
          );
        })}
      </table>
      <div className="mut" style={{ fontSize: 12, marginTop: 6 }}>¹ evidence precision: {note}.</div>
    </div>
  );
}
