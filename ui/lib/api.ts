"use client";
import { useCallback, useEffect, useState } from "react";

export type Status = { data_root: string; scenarios: string[]; fireworks_key: boolean; jobs_running: number };
export type Live = { alert: string | null; symptom: string; symptom_kind: string; alert_at: string; as_of: string };
export type ScenarioRow = {
  name: string; quads: number | null; shacl_ok: boolean | null; as_of_quads: number | null; as_of: string | null;
  failed_enrichers: string[]; live: Live | null;
};
export type Link = { type: string; app: string; at: string; uri: string };
export type Chain = { confidence: number; links: Link[] };
export type Change = { id: string; uri: string; kind: string; app: string; at: string; what: string };
export type Symptom = {
  symptom_uri: string; as_of: string;
  alert: { uri: string; id: string; app: string; at: string; severity: string | null; metric: string | null; summary: string | null };
  incident: { uri: string; id: string; at: string; severity: string; affects: string[] } | null;
};
export type ScenarioDetail = {
  name: string; live: Live | null; symptom: Symptom | null; chains: Chain[]; closure?: string[]; changes: Change[];
};
export type Alert = { id: string; uri: string; at: string; app: string | null };
export type Topology = { nodes: { id: string; kind: string | null; tier: string | null }[]; edges: { source: string; target: string }[] };
export type Rows = { rows: Record<string, string | null>[]; count: number; truncated?: number; capped?: number; ms: number };
export type Ontology = {
  classes: { uri: string; label: string; comment: string | null; parents: string[] }[];
  properties: { uri: string; kind: string; label: string | null; domain: string | null; range: string | null }[];
  schemes: { uri: string; label: string; concepts: { uri: string; notation: string | null; label: string | null; definition: string | null; broader: string | null }[] }[];
};
export type Hypothesis = { root_cause_uri: string; chain: string[]; confidence: number; evidence: string[]; refuted_alternatives: string[] };
export type Report = { verdict: string; hypotheses: Hypothesis[]; summary: string; failure?: string };
export type Job = {
  id: string; scenario: string; alert: string; state: "queued" | "running" | "done" | "failed";
  created: number; started?: number; finished?: number; report?: Report; error?: string;
};
export type EvalRun = { name: string; has_results: boolean; meta: Record<string, unknown> };
export type EvalDetail = { name: string; summary_md: string; results: Record<string, unknown>[] };

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method: body !== undefined ? "POST" : "GET",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* not json */ }
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

/** GET with loading / error state and manual reload. */
export function useApi<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!path);
  const reload = useCallback(() => {
    if (!path) return;
    setLoading(true);
    api<T>(path).then((d) => { setData(d); setError(null); }).catch((e: Error) => setError(e.message)).finally(() => setLoading(false));
  }, [path]);
  useEffect(() => { reload(); }, [reload]);
  return { data, error, loading, reload };
}

export const tail = (u: string | null | undefined) => (u ? u.split(/[/#]/).pop() ?? u : "");
export const hhmm = (iso: string) => iso.replace("T", " ").replace(/:\d\d(\.\d+)?(Z|\+00:00)$/, "Z");
export const CHANGE_TYPES = new Set(["commit", "deployment", "config_change", "experiment", "autoscaling", "traffic_shift"]);
