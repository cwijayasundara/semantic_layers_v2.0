"use client";
import { useEffect, useRef, useState } from "react";
import cytoscape from "cytoscape";
// @ts-expect-error cytoscape-fcose publishes no type declarations
import fcose from "cytoscape-fcose";
import type { CaseEdge, CaseNode, CasePayload } from "@/lib/api";
import { LABEL_X, caseT0, ms, swimFrame } from "./swimlane";

const g = globalThis as { __caseFcose?: boolean };
if (!g.__caseFcose) { cytoscape.use(fcose); g.__caseFcose = true; }   // once, even across hot reloads

export type Layers = { suspects: boolean; raw: boolean; chat: boolean; topology: boolean };
export type CaseGraphProps = {
  data: CasePayload; layout: "swim" | "force"; layers: Layers;
  time: number | null; trace: number | null; highlight?: ReadonlySet<string> | null; onSelect(n: CaseNode): void;
  onVisible?(ids: ReadonlySet<string>): void;   // the ids of the case nodes now drawn
};

const WIDTH = 1000, LANE = 44, MIN = 60_000;
const hm = (t: number) => new Date(t).toISOString().slice(11, 16);
const short = (s: string, n = 26) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const has = (n: CaseNode, ...r: string[]) => r.some((x) => n.roles.includes(x));

// in the swimlane the lane already says which service a node is on, so these would only add clutter
const SWIM_HIDDEN = new Set(["onApp", "caller", "mentions"]);

/** The nodes and edges the current layer toggles leave on screen. */
export function visibleElements(data: CasePayload, layers: Layers, layout: "swim" | "force") {
  const keep = (n: CaseNode) => {
    if (n.cat === "raw") return layers.raw;
    if (n.cat === "chat") return layers.chat;
    if (n.cat === "app") return true;     // filtered below
    if (!layers.suspects && has(n, "suspect") && !has(n, "chain", "root", "best_candidate", "evidence", "symptom")) return false;
    return true;
  };
  const nonApp = data.nodes.filter((n) => n.cat !== "app" && keep(n));
  const ids = new Set(nonApp.map((n) => n.id));
  const relevant = new Set<string>([data.meta.alert.app, ...(data.meta.incident?.affects ?? [])]);
  for (const n of nonApp) if (n.app) relevant.add(n.app);
  for (const e of data.edges) if (ids.has(e.source) && (e.kind === "caller" || e.kind === "mentions")) relevant.add(e.target.split("/").pop()!);
  const apps = data.nodes.filter((n) => n.cat === "app" && (layers.topology || relevant.has(n.label) || n.first_degraded));
  const nodes = [...apps, ...nonApp];
  const all = new Set(nodes.map((n) => n.id));
  const edges = data.edges.filter((e) => all.has(e.source) && all.has(e.target)
    && (e.kind !== "dependsOn" || layers.topology)
    && (layout === "force" || !SWIM_HIDDEN.has(e.kind)));
  return { nodes, edges };
}

function display(n: CaseNode): string {
  const t = n.at ? hm(ms(n.at)) : "";
  let s: string;
  if (n.cat === "app" || n.cat === "raw") s = n.label;
  else if (n.cat === "chat") s = String(n.props.kind ?? "claim");
  else if (has(n, "root")) s = `★ ${short(n.label, 30)} · ${t}`;
  else if (has(n, "best_candidate")) s = `best candidate, not accepted · confidence ${(n.confidence ?? 0).toFixed(2)}\n${short(n.label, 30)} · ${t}`;
  else if (n.cat === "change") s = `${short(n.label)}\n${t}`;
  else if (n.cat === "alert" || n.cat === "incident") s = `${n.label} · ${t}`;
  else if (n.cat === "finding") s = `${n.missing ? "finding" : "agent finding"} ${n.label.slice(0, 8)}`;
  else if (n.cat === "other") s = `${n.cat} ${n.label.slice(0, 8)}`;
  else s = `${n.cat} ${t}`;
  if (n.hop != null && !has(n, "root", "best_candidate")) s = `#${n.hop} ${s}`;
  return n.missing ? `${s}\n(not in as-of store)` : s;
}

function palette() {
  const css = getComputedStyle(document.documentElement);
  const v = (k: string, d: string) => css.getPropertyValue(k).trim() || d;
  return {
    bg: v("--bg", "#0e1116"), panel: v("--panel", "#151a22"), panel2: v("--panel2", "#1b222d"), line: v("--line", "#262f3d"),
    text: v("--text", "#e6e9ef"), mut: v("--mut", "#8b95a7"), accent: v("--accent", "#6ea8ff"), ok: v("--ok", "#3ecf8e"),
    bad: v("--bad", "#ff6b6b"), warn: v("--warn", "#f5b84b"), chat: "#b48cff", evid: "#f39c6b",
  };
}

function style(): cytoscape.StylesheetStyle[] {
  const p = palette();
  const s: Record<string, Record<string, unknown>> = {
    node: {
      label: "data(label)", "font-size": 10, color: p.text, "text-wrap": "wrap", "text-max-width": "190px",
      "text-valign": "bottom", "text-margin-y": 2, "background-color": p.mut, width: 12, height: 12,
      "text-background-color": p.bg, "text-background-opacity": 0.75, "text-background-padding": "1px",
      "z-index-compare": "manual", "z-index": 10, "border-width": 0,
    },
    edge: { width: 1, "line-color": p.line, "curve-style": "bezier", "z-index-compare": "manual", "z-index": 5, opacity: 0.7 },
    ".app": {
      shape: "round-rectangle", width: 150, height: 24, "background-color": p.accent, "background-opacity": 0.22,
      "border-width": 1, "border-color": p.accent, "text-valign": "center", "text-margin-y": 0, "font-size": 12,
      "text-background-opacity": 0, "text-max-width": "146px",
    },
    ".app.degraded": { "background-color": p.bad, "background-opacity": 0.55, "border-color": p.bad },
    ".role-evidence": { "background-color": p.evid, width: 13, height: 13 },
    ".role-suspect": { shape: "diamond", width: 20, height: 20, "background-color": p.panel2, "border-width": 1.5, "border-style": "dashed", "border-color": p.mut },
    ".role-chain": { "background-color": p.bad, width: 16, height: 16 },
    ".role-symptom": { width: 22, height: 22, "background-color": p.bad, "border-width": 3, "border-color": p.text, "font-weight": "bold" },
    ".role-root": { shape: "star", width: 38, height: 38, "background-color": p.warn, "border-width": 1.5, "border-style": "solid", "border-color": p.text, "font-size": 13, "font-weight": "bold", color: p.warn },
    ".role-best_candidate": { shape: "diamond", width: 26, height: 26, "background-color": p.panel2, "border-width": 2.5, "border-style": "dashed", "border-color": p.warn, color: p.warn, "font-size": 12 },
    ".cat-chat": { shape: "round-rectangle", width: 12, height: 12, "background-color": p.chat, color: p.chat, "font-size": 8 },
    ".cat-raw": { shape: "rectangle", width: 11, height: 11, "background-color": p.ok, color: p.ok, "font-size": 8 },
    ".lbl-top": { "text-valign": "top", "text-margin-y": -2 },
    ".missing": { "background-opacity": 0.15, "border-width": 1.5, "border-style": "dashed", "border-color": p.bad },
    ":selected": { "border-width": 3, "border-color": p.accent, "border-style": "solid" },
    "edge.k-causal": { width: 2.5, "line-color": p.bad, "target-arrow-color": p.bad, "target-arrow-shape": "triangle", opacity: 0.9, "z-index": 6 },
    "edge.k-causal.tentative": { "line-style": "dashed", "line-color": p.warn, "target-arrow-color": p.warn, width: 2 },
    "edge.k-dependsOn": { "line-color": p.accent, opacity: 0.25, "target-arrow-shape": "vee", "target-arrow-color": p.accent, "arrow-scale": 0.6 },
    "edge.k-prov": { "line-color": p.ok, opacity: 0.2, width: 0.8 },
    "edge.k-mentions": { "line-color": p.chat, opacity: 0.35, width: 0.8 },
    "edge.k-cites": { "line-color": p.accent, "line-style": "dashed" },
    "edge.k-opened": { "line-color": p.mut, "target-arrow-shape": "triangle", "target-arrow-color": p.mut },
    "edge.k-onApp, edge.k-caller": { opacity: 0.3 },
    // swimlane decoration (never interactive)
    ".decor": { events: "no", "z-index": 0, label: "data(label)", "text-background-opacity": 0 },
    ".stripe": { shape: "rectangle", "background-color": p.accent, "background-opacity": 0.05, "border-width": 0, label: "" },
    ".stripe.odd": { "background-opacity": 0.09 },
    ".stripe.nopath": { "background-color": p.mut, "background-opacity": 0.07 },
    ".mark": { width: 1, height: 1, "background-opacity": 0, "text-valign": "top", "font-size": 12, "font-weight": "bold", "text-max-width": "300px" },
    ".tick": { width: 1, height: 1, "background-opacity": 0, "text-valign": "top", "font-size": 10, color: p.mut },
    ".bandlabel": { width: 1, height: 1, "background-opacity": 0, "text-valign": "center", "text-halign": "right", "font-size": 13, color: p.mut, "text-max-width": "900px" },
    "edge.mline": { "line-style": "dashed", width: 1.5, opacity: 0.85, "z-index": 1 },
    "edge.tline": { width: 0.6, opacity: 0.12, "line-color": p.mut, "z-index": 1 },
    ".mk-root": { color: p.warn }, "edge.mk-root": { "line-color": p.warn },
    ".mk-onset": { color: p.bad }, "edge.mk-onset": { "line-color": p.bad },
    ".mk-alert": { color: p.bad }, "edge.mk-alert": { "line-color": p.bad, "line-style": "solid", opacity: 0.5 },
    ".mk-cutoff": { color: p.mut }, "edge.mk-cutoff": { "line-color": p.mut },
    // replay and trace
    ".faded": { opacity: 0.06 },
    ".dim": { opacity: 0.18 },
    ".hl": { "border-width": 4, "border-color": p.text, "border-style": "solid", "font-size": 12, "z-index": 20 },
  };
  return Object.entries(s).map(([selector, st]) => ({ selector, style: st })) as unknown as cytoscape.StylesheetStyle[];
}

type El = cytoscape.ElementDefinition;

function nodeEl(n: CaseNode, position?: { x: number; y: number }, labelAbove = false): El {
  const classes = [labelAbove ? "lbl-top" : "", n.cat === "app" ? "app" : `cat-${n.cat}`, ...n.roles.map((r) => `role-${r}`), n.missing ? "missing" : ""];
  return {
    group: "nodes", data: { id: n.id, label: display(n), atMs: n.at ? ms(n.at) : null, hop: n.hop, fd: n.first_degraded ? ms(n.first_degraded) : null },
    classes: classes.filter(Boolean).join(" "), position,
  };
}

const edgeEl = (e: CaseEdge, tentative: boolean): El =>
  ({ group: "edges", data: { id: e.id, source: e.source, target: e.target }, classes: `k-${e.kind}${tentative ? " tentative" : ""}` });

/** Lane stripes, band labels, time markers and 5-minute ticks for the swimlane view. */
function decor(data: CasePayload, frame: ReturnType<typeof swimFrame>, t0: number, t1: number): El[] {
  const out: El[] = [];
  const left = LABEL_X - 85, right = frame.untimedX + 30, top = 6, bottom = frame.bottom;
  const add = (id: string, classes: string, x: number, y: number, label = "", extra: Record<string, unknown> = {}) =>
    out.push({ group: "nodes", data: { id, label, ...extra }, classes: `decor ${classes}`, position: { x, y }, selectable: false, grabbable: false });
  frame.rows.forEach((r, i) => {
    const id = `stripe:${i}`;
    out.push({ group: "nodes", data: { id, label: "" }, classes: `decor stripe ${i % 2 ? "odd" : ""} ${r.band === "nopath" ? "nopath" : ""}`,
      position: { x: (left + right) / 2, y: r.y }, selectable: false, grabbable: false,
      style: { width: right - left, height: LANE - 3 } });
  });
  let marks = 0;   // alternate label heights so close markers (root vs onset) stay readable
  const line = (id: string, cls: string, x: number, label: string) => {
    add(`${id}:a`, `mark ${cls}`, x, top + 16 + (marks++ % 2) * 14, label);
    add(`${id}:b`, cls, x, bottom, "", {});
    out.push({ group: "edges", data: { id: `${id}:l`, source: `${id}:a`, target: `${id}:b` }, classes: `decor mline ${cls}`, selectable: false });
  };
  for (let t = Math.ceil(t0 / (5 * MIN)) * 5 * MIN; t <= t1; t += 5 * MIN) {
    const x = frame.x(t);
    add(`tick:${t}:a`, "tick", x, top, hm(t));
    add(`tick:${t}:b`, "tick", x, bottom);
    out.push({ group: "edges", data: { id: `tick:${t}:l`, source: `tick:${t}:a`, target: `tick:${t}:b` }, classes: "decor tline", selectable: false });
  }
  add("tick:early", "tick", 18, top, "earlier");
  const root = data.nodes.find((n) => n.roles.includes("root") && n.at);
  if (root) line("mk-root", "mk-root", frame.x(ms(root.at!)), `★ root ${root.cls ?? root.cat} ${hm(ms(root.at!))}`);
  if (data.meta.onset_at) line("mk-onset", "mk-onset", frame.x(ms(data.meta.onset_at)), `onset ${hm(ms(data.meta.onset_at))}`);
  line("mk-alert", "mk-alert", frame.x(ms(data.meta.alert.at)), `alert ${hm(ms(data.meta.alert.at))}`);
  line("mk-cutoff", "mk-cutoff", frame.x(ms(data.meta.as_of)), `cut-off ${hm(ms(data.meta.as_of))} (agent's data ends)`);
  const band = (id: string, y: number | undefined, label: string) => { if (y !== undefined) add(id, "bandlabel", left, y + 6, label); };
  band("band:chat", frame.bands.chat, "Slack claims (what the humans said)");
  band("band:nopath", frame.bands.nopath, `services with NO dependency path to ${data.meta.alert.app}: changes here cannot explain the symptom`);
  band("band:unplaced", frame.bands.unplaced, "no service lane (not in the as-of store, or no app)");
  band("band:raw", frame.bands.raw, "raw files (lineage)");
  return out;
}

export default function CaseGraph({ data, layout, layers, time, trace, highlight, onSelect, onVisible }: CaseGraphProps) {
  const box = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const select = useRef(onSelect);
  const visible = useRef(onVisible);
  useEffect(() => { select.current = onSelect; visible.current = onVisible; }, [onSelect, onVisible]);
  const [built, setBuilt] = useState(0);

  useEffect(() => {
    if (!box.current) return;
    const { nodes, edges } = visibleElements(data, layers, layout);
    const byId = new Map(data.nodes.map((n) => [n.id, n]));
    const t0 = caseT0(data.meta, data.nodes), t1 = ms(data.meta.as_of);
    const tentative = data.meta.verdict !== "root_cause_found";   // the chain of a hypothesis the agent did not accept
    let elements: El[];
    if (layout === "swim") {
      const frame = swimFrame(nodes, { t0, t1, width: WIDTH, lane: LANE, edges });
      elements = [...decor(data, frame, t0, t1), ...nodes.map((n) => nodeEl(n, frame.pos[n.id], frame.above.has(n.id))), ...edges.map((e) => edgeEl(e, tentative))];
    } else {
      elements = [...nodes.map((n) => nodeEl(n)), ...edges.map((e) => edgeEl(e, tentative))];
    }
    const cy = cytoscape({
      container: box.current, elements, style: style(), minZoom: 0.15, maxZoom: 4, boxSelectionEnabled: false,
      layout: layout === "swim" ? { name: "preset", fit: true, padding: 16 }
        : ({ name: "fcose", animate: false, randomize: true, quality: "default", nodeRepulsion: 6000, idealEdgeLength: 70, padding: 20 } as cytoscape.LayoutOptions),
    });
    cy.on("tap", "node", (ev) => { const n = byId.get(ev.target.id()); if (n) select.current(n); });
    cyRef.current = cy;
    visible.current?.(new Set(nodes.map((n) => n.id)));
    setBuilt((b) => b + 1);
    return () => { cy.destroy(); cyRef.current = null; };
  }, [data, layout, layers]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      const real = cy.nodes().not(".decor");
      real.removeClass("faded dim hl degraded");
      cy.edges().removeClass("faded dim");
      real.filter(".app").forEach((a) => {
        const fd = a.data("fd") as number | null;
        if (fd != null && (time == null || fd <= time)) a.addClass("degraded");
      });
      if (time != null) {
        real.forEach((n) => { const at = n.data("atMs") as number | null; if (at != null && at > time) n.addClass("faded"); });
        real.filter(".cat-raw").forEach((r) => {
          const src = r.incomers("node");
          if (src.length && src.filter(".faded").length === src.length) r.addClass("faded");
        });
      }
      if (trace != null) {
        real.forEach((n) => {
          const hop = n.data("hop") as number | null;
          if (hop === trace) n.addClass("hl");
          else if (hop == null || hop > trace) { if (!n.hasClass("app")) n.addClass("dim"); }
        });
      }
      if (highlight?.size) {   // a trail finding's cited items
        real.forEach((n) => {
          if (highlight.has(n.id())) { n.removeClass("dim"); n.addClass("hl"); }
          else if (!n.hasClass("app")) n.addClass("dim");
        });
      }
      cy.edges().not(".decor").forEach((e) => {
        if (e.source().hasClass("faded") || e.target().hasClass("faded")) e.addClass("faded");
        else if (e.source().hasClass("dim") || e.target().hasClass("dim")) e.addClass("dim");
      });
    });
  }, [time, trace, highlight, built]);

  return <div ref={box} style={{ width: "100%", height: "100%", minHeight: 560 }} />;
}
