// Swimlane layout for the case graph: x = time, y = one lane per service ordered by call depth
// from the alert app. Pure functions only (no DOM, no Cytoscape) so the layout is easy to reason about.
import type { CaseEdge, CaseMeta, CaseNode } from "@/lib/api";

export type SwimOpts = { t0: number; t1: number; width: number; lane: number; edges?: CaseEdge[] };
export type Pos = { x: number; y: number };
export type SwimRow = { app: string | null; y: number; band: "lane" | "nopath" | "unplaced" };
export type SwimFrame = {
  pos: Record<string, Pos>;
  rows: SwimRow[];
  /** y of each band's top edge (only bands that have content). */
  bands: { chat?: number; nopath?: number; unplaced?: number; raw?: number };
  bottom: number;
  x: (t: number) => number;
  untimedX: number;
  /** nodes stacked above their lane line; their labels read better above them */
  above: Set<string>;
};

export const EARLY = 40;      // items before t0 are squeezed into the left 40 px
export const LABEL_X = -95;   // lane label (the app node) sits left of the time axis
const NEAR = 22, STACK = 17, CHAT_GAP = 26, RAW_GAP = 36, LOOSE_GAP = 105;
const SUB = 14, WRAP = 3 * SUB + 16;   // stagger within a band row; one wrapped band row
const MIN = 60_000;

export const ms = (iso: string) => Date.parse(iso);

/** Start of the time axis: onset − 20 min; with no onset (inconclusive), the earliest
 * chain/evidence/best-candidate item (else alert − 60 min), minus 20 min. */
export function caseT0(meta: CaseMeta, nodes: CaseNode[]): number {
  if (meta.onset_at) return ms(meta.onset_at) - 20 * MIN;
  const keyed = nodes.filter((n) => n.at && n.roles.some((r) => r === "chain" || r === "evidence" || r === "best_candidate"));
  const first = keyed.length ? Math.min(...keyed.map((n) => ms(n.at!))) : ms(meta.alert.at) - 60 * MIN;
  return first - 20 * MIN;
}

const STACK_OFFSETS = (k: number) => Array.from({ length: k }, (_, i) => (i === 0 ? 0 : (i % 2 ? -1 : 1) * STACK * Math.ceil(i / 2)));

/** How many band rows `n` items `gap` apart need to fit in [lo, hi]. */
const bandRows = (n: number, gap: number, lo: number, hi: number) =>
  Math.max(1, Math.ceil(n / (Math.floor((hi - lo) / gap) + 1)));

/** Spread items along x in key order, at least `gap` apart and inside [lo, hi], staggered over
 * three sub-rows; items that cannot fit wrap onto further rows WRAP px apart (dealt in key
 * order, so each row stays ordered by key). */
function spread(items: { id: string; key: number }[], y: number, gap: number, lo: number, hi: number, pos: Record<string, Pos>) {
  const sorted = [...items].sort((a, b) => a.key - b.key || a.id.localeCompare(b.id));
  const rows = bandRows(sorted.length, gap, lo, hi);
  for (let r = 0; r < rows; r++) {
    const row = sorted.filter((_, i) => i % rows === r);
    const xs: number[] = [];
    row.forEach((it, i) => { xs.push(Math.max(Math.min(Math.max(it.key, lo), hi), i ? xs[i - 1] + gap : lo)); });
    for (let i = row.length - 1; i >= 0; i--) xs[i] = Math.min(xs[i], hi - (row.length - 1 - i) * gap);   // pull back inside hi
    row.forEach((it, i) => { pos[it.id] = { x: xs[i], y: y + r * WRAP + (i % 3) * SUB }; });
  }
}

export function swimFrame(nodes: CaseNode[], opts: SwimOpts): SwimFrame {
  const { t0, t1, width, lane } = opts;
  const timed = nodes.filter((n) => n.at).map((n) => ms(n.at!));
  const tmin = Math.min(t0, ...timed);
  const x = (t: number) =>
    t >= t0 ? EARLY + ((t - t0) / Math.max(t1 - t0, 1)) * (width - EARLY)
      : tmin === t0 ? 0 : ((t - tmin) / (t0 - tmin)) * (EARLY - 8);
  const untimedX = width + 50;

  const apps = nodes.filter((n) => n.cat === "app");
  const laneApps = apps.filter((a) => a.hops != null)
    .sort((a, b) => (a.hops! - b.hops!) || a.label.localeCompare(b.label));
  const nullApps = apps.filter((a) => a.hops == null).sort((a, b) => a.label.localeCompare(b.label));
  const chats = nodes.filter((n) => n.cat === "chat");
  const raws = nodes.filter((n) => n.cat === "raw");
  const signals = nodes.filter((n) => n.cat !== "app" && n.cat !== "chat" && n.cat !== "raw");
  const appNames = new Set(apps.map((a) => a.label));
  const unplaced = signals.filter((n) => !n.app || !appNames.has(n.app));

  const pos: Record<string, Pos> = {};
  const above = new Set<string>();
  const rows: SwimRow[] = [];
  const bands: SwimFrame["bands"] = {};
  let cursor = lane * 0.9;                       // the axis labels sit above this
  const extra = (n: number, gap: number) => (bandRows(n, gap, EARLY, width) - 1) * WRAP;
  if (chats.length) { bands.chat = cursor; cursor += lane * 1.6 + extra(chats.length, CHAT_GAP); }
  for (const a of laneApps) { rows.push({ app: a.label, y: cursor + lane / 2, band: "lane" }); cursor += lane; }
  if (nullApps.length) {
    cursor += lane * 1.1;   // room for labels hanging below the last lane
    bands.nopath = cursor;
    cursor += lane * 0.3;
    for (const a of nullApps) { rows.push({ app: a.label, y: cursor + lane / 2, band: "nopath" }); cursor += lane; }
  }
  if (unplaced.length) {
    cursor += lane * 0.4;
    bands.unplaced = cursor;
    cursor += lane * 0.3;
    rows.push({ app: null, y: cursor + lane / 2, band: "unplaced" });
    cursor += lane;
  }
  if (raws.length) { cursor += lane * 0.6; bands.raw = cursor; cursor += lane * 1.6 + extra(raws.length, RAW_GAP); }

  const rowY = new Map(rows.map((r) => [r.app, r.y]));
  for (const a of apps) pos[a.id] = { x: LABEL_X, y: rowY.get(a.label)! };

  // signals in their app's lane; same-lane items within NEAR px stack at ±STACK px
  const links = new Map<string, string[]>();
  for (const e of opts.edges ?? []) {
    links.set(e.target, [...(links.get(e.target) ?? []), e.source]);
    links.set(e.source, [...(links.get(e.source) ?? []), e.target]);
  }
  const atOf = new Map(nodes.filter((n) => n.at).map((n) => [n.id, x(ms(n.at!))]));
  const cutX = x(t1);
  // a finding the agent recorded during the run has no data time: it sits just after the
  // latest item it cites, never past the cut-off (the agent wrote it from what it saw)
  const runtimeX = (id: string) => {
    const xs = (links.get(id) ?? []).map((o) => atOf.get(o)).filter((v): v is number => v != null);
    return Math.min(xs.length ? Math.max(...xs) + NEAR : cutX, cutX - 6);
  };
  const byRow = new Map<number, { id: string; x: number }[]>();
  const loose: { id: string; key: number }[] = [];
  for (const n of signals) {
    const y = (n.app ? rowY.get(n.app) : undefined) ?? rowY.get(null)!;
    const runtime = !n.at && n.cat === "finding" && !n.missing;
    if (!n.at && !runtime && y === rowY.get(null)) { loose.push({ id: n.id, key: EARLY }); continue; }   // no lane, no time
    const it = { id: n.id, x: n.at ? x(ms(n.at)) : runtime ? runtimeX(n.id) : untimedX };
    byRow.set(y, [...(byRow.get(y) ?? []), it]);
  }
  for (const [y, items] of byRow) {
    items.sort((a, b) => a.x - b.x || a.id.localeCompare(b.id));
    let cluster: typeof items = [];
    const flush = () => { const off = STACK_OFFSETS(cluster.length); cluster.forEach((it, i) => { pos[it.id] = { x: it.x, y: y + off[i] }; if (off[i] < 0) above.add(it.id); }); cluster = []; };
    for (const it of items) {
      if (cluster.length && it.x - cluster[cluster.length - 1].x > NEAR) flush();
      cluster.push(it);
    }
    flush();
  }

  const unplacedY = rowY.get(null);
  if (unplacedY !== undefined) {
    let prev = -Infinity;
    for (const it of loose.sort((a, b) => a.id.localeCompare(b.id))) {
      const px = Math.max(prev + LOOSE_GAP, EARLY);
      prev = px;
      pos[it.id] = { x: px, y: unplacedY + STACK };   // below any timed nodes in this row
    }
  }

  // bands: chat on top, raw at the bottom, each ordered by the mean x of the nodes linked to it
  const meanX = (id: string) => {
    const xs = (links.get(id) ?? []).map((o) => pos[o]).filter((p) => p && p.x !== LABEL_X).map((p) => p.x);
    return xs.length ? xs.reduce((s, v) => s + v, 0) / xs.length : untimedX;
  };
  if (bands.chat !== undefined)
    spread(chats.map((n) => ({ id: n.id, key: n.at ? x(ms(n.at)) : meanX(n.id) })), bands.chat + lane * 0.4, CHAT_GAP, EARLY, width, pos);
  if (bands.raw !== undefined)
    // raw files cited directly by the agent (no graph node read from them) start at the left
    spread(raws.map((n) => ({ id: n.id, key: links.has(n.id) ? meanX(n.id) : EARLY })), bands.raw + lane * 0.5, RAW_GAP, EARLY, width, pos);

  return { pos, rows, bands, bottom: cursor, x, untimedX, above };
}

/** Node id -> preset position for the swimlane view. */
export function swimlane(nodes: CaseNode[], opts: SwimOpts): Record<string, Pos> {
  return swimFrame(nodes, opts).pos;
}
