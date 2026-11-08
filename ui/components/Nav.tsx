"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Status, useApi } from "@/lib/api";

const ITEMS = [["/", "Scenarios"], ["/sparql", "SPARQL"], ["/ontology", "Ontology & taxonomy"], ["/evals", "Evals"]] as const;

export default function Nav() {
  const path = usePathname();
  const { data, error } = useApi<Status>("/status");
  const on = (href: string) => (href === "/" ? path === "/" || path.startsWith("/scenarios") : path.startsWith(href));
  return (
    <nav className="side">
      <div className="brand">e2ekg<small>knowledge graph + AutoRCA</small></div>
      {ITEMS.map(([href, label]) => (
        <Link key={href} href={href} className={`nav ${on(href) ? "on" : ""}`}>{label}</Link>
      ))}
      <div className="foot">
        {data ? (<>
          {data.scenarios.length} scenarios<br />
          AutoRCA: {data.fireworks_key ? <span className="ok">enabled</span> : <span className="warn">no Fireworks key</span>}<br />
          {data.jobs_running ? `${data.jobs_running} RCA running` : null}
        </>) : error ? <span className="bad">API unreachable</span> : "connecting to API…"}
      </div>
    </nav>
  );
}
