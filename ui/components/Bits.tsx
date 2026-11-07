"use client";
import { ReactNode } from "react";

export const Spin = ({ what = "Loading" }: { what?: string }) => <div className="spin">{what}…</div>;
export const ErrBox = ({ msg }: { msg: string }) => <div className="err"><b>Error:</b> {msg}</div>;
export const Stat = ({ v, l, tone }: { v: ReactNode; l: string; tone?: "ok" | "bad" }) => (
  <div className="card stat"><div className={`v ${tone ?? ""}`}>{v}</div><div className="l">{l}</div></div>
);
export const Mark = ({ ok }: { ok: boolean }) => <span className={ok ? "ok" : "bad"}>{ok ? "✔" : "✘"}</span>;
export const Chip = ({ children, tone }: { children: ReactNode; tone?: "ok" | "bad" | "warn" | "acc" }) => <span className={`chip ${tone ?? ""}`}>{children}</span>;

export function ResultTable({ rows }: { rows: Record<string, string>[] }) {
  if (!rows.length) return <div className="mut" style={{ padding: 10 }}>0 rows</div>;
  const cols = Object.keys(rows[0]);
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c} className="mono">{r[c]}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}
