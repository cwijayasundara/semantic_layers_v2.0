"""Catalog enricher: applications, owning teams and regions (the identity gate).

One ops:Application per catalog id, with ops:inCatalog true, rdfs:label (the
id), ops:appKind, ops:tier, ops:ownedBy (team node labelled with the catalog
owner name) and ops:deployedIn (region nodes labelled with the region name).
Each application cites the catalog record(s) it came from (ops:rawSelector), and
each team and region the records of the applications that name it.
Duplicate records of one id are merged:
first non-empty value wins, every raw id is kept."""
from collections import defaultdict

from rdflib import Literal
from rdflib.namespace import RDF

from e2ekg.graph.enricher import (
    NS, Enricher, integer, json_fields, label, raw_selectors, read_table, string,
)
from e2ekg.graph.uris import app_uri, region_uri, team_uri

OPS = NS["ops"]


class CatalogEnricher(Enricher):
    name = "catalog"
    reads = ("catalog",)

    def fetch(self, ctx):
        return read_table(ctx, "catalog", ["raw_id", "fields", "app_id", "app_kind"]
                          ).sort("raw_id").to_dicts()

    def analyze(self, rows):
        apps: dict[str, dict] = {}
        for r in rows:
            f = json_fields(r["fields"])
            aid = f.get("id") or r["app_id"]
            if not aid:
                continue
            a = apps.setdefault(aid, {"id": aid, "kind": None, "tier": None, "team": None,
                                      "regions": [], "raw_ids": []})
            a["raw_ids"].append(r["raw_id"])
            a["kind"] = a["kind"] or f.get("kind") or r["app_kind"]
            if a["tier"] is None and isinstance(f.get("tier"), int):
                a["tier"] = f["tier"]
            a["team"] = a["team"] or f.get("owner_team")
            for reg in f.get("regions") or ():
                if isinstance(reg, str) and reg and reg not in a["regions"]:
                    a["regions"].append(reg)
        teams, regions = defaultdict(list), defaultdict(list)
        for a in apps.values():
            if a["team"]:
                teams[a["team"]] += a["raw_ids"]
            for reg in a["regions"]:
                regions[reg] += a["raw_ids"]
        return [apps[k] for k in sorted(apps)], teams, regions

    def emit(self, result):
        apps, teams, regions = result
        for names, uri_of, cls in ((teams, team_uri, OPS.Team), (regions, region_uri, OPS.Region)):
            for name, raw_ids in sorted(names.items()):
                yield uri_of(name), RDF.type, cls
                yield label(uri_of(name), name)
                yield from raw_selectors(uri_of(name), raw_ids)
        for a in apps:
            s = app_uri(a["id"])
            yield s, RDF.type, OPS.Application
            yield s, OPS.inCatalog, Literal(True)
            yield label(s, a["id"])
            if a["kind"]:
                yield s, OPS.appKind, string(a["kind"])
            if a["tier"] is not None:
                yield s, OPS.tier, integer(a["tier"])
            if a["team"]:
                yield s, OPS.ownedBy, team_uri(a["team"])
            for reg in a["regions"]:
                yield s, OPS.deployedIn, region_uri(reg)
            yield from raw_selectors(s, a["raw_ids"])
