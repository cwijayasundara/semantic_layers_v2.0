"""catalog/apps.yaml: one YAML document per application.

The alias lists are deliberately incomplete (a real catalog lags its
emitters): every third named app loses its last alias, and the Slack-only
nickname `TMS` is never listed."""
import numpy as np

from e2ekg.generate.records import RawRecord
from e2ekg.scenario.model import Scenario
from e2ekg.scenario.propagation import Degradation

REL_PATH = "catalog/apps.yaml"
DROP_ALIAS_EVERY = 3


def render_catalog(scenario: Scenario, degradations: tuple[Degradation, ...],
                   rng: np.random.Generator) -> list[RawRecord]:
    deps: dict[str, list[dict]] = {}
    for e in scenario.edges:
        dep = {"app": e.callee, "protocol": e.protocol}
        if e.pool_size is not None:
            dep["pool_size"] = e.pool_size
        deps.setdefault(e.caller, []).append(dep)
    out = []
    named = 0
    for a in scenario.catalog:
        aliases = list(a.aliases)
        if aliases:
            named += 1
            if named % DROP_ALIAS_EVERY == 0:
                aliases = aliases[:-1]
        doc = {"id": a.id, "aliases": aliases, "owner_team": a.owner_team, "tier": a.tier,
               "regions": list(a.regions), "kind": a.kind, "dependencies": deps.get(a.id, [])}
        out.append(RawRecord(source="catalog", rel_path=REL_PATH, fmt="catalog_yaml",
                             fields=doc))
    return out
