"""Every scenario end to end through the graph: the answer key's root ranks first in the as-of
store the live RCA reads (R34), and the ledgers and SHACL are clean on both stores."""
import json

import pytest

from e2ekg.correlate.engine import correlate
from e2ekg.graph.enrichers.correlation import chain_inputs
from e2ekg.graph.harvest import unreachable_rows
from e2ekg.graph.uris import change_uri
from e2ekg.preprocess.fingerprints import load_fingerprints
from e2ekg.preprocess.ledger import check_coverage, check_reachability
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.variants import variant_scenarios

FAULTS = [s.name for s in variant_scenarios() if s.name != "v-control"]
ALL_SCENARIOS = ["hero", *FAULTS, "v-control"]


@pytest.mark.parametrize("name", ["hero", *FAULTS])
def test_variant_root_cause_top1(name, harvested):
    s = harvested(name, as_of=True)
    answers = json.loads((s.answers / f"{name}.json").read_text())
    scenario = next(x for x in (hero_scenario(), *variant_scenarios()) if x.name == name)
    root = answers["answer_key"]["root_cause_change_id"]
    kind = next(c.kind for c in scenario.changes if c.id == root)
    external = next(k for k, v in answers["external_ids"].items() if v == root)
    chains = correlate(*chain_inputs(s.store, s.symptom))
    assert chains and chains[0].links[0].uri == str(change_uri(name, kind, external))


def test_the_control_has_nothing_to_blame(harvested):
    s = harvested("v-control", as_of=True)
    assert correlate(*chain_inputs(s.store, s.symptom)) == []


@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_ledgers_and_shacl(name, harvested, preprocessed):
    pp = preprocessed(name)
    cov = check_coverage(pp.raw, pp.proc)
    assert cov.gaps == [] and cov.overlaps == [] and cov.mismatches == []
    assert cov.bytes_covered == cov.bytes_total
    reach = check_reachability(pp.proc, load_fingerprints(pp.proc))
    assert reach.ok
    full = harvested(name)
    assert full.report.shacl_ok, full.report.shacl_text
    assert not any(not r.success for r in full.report.results.values())
    assert {t: n for t, n in unreachable_rows(full.store, full.proc).items() if n} == {}
    live = harvested(name, as_of=True)
    assert live.report.shacl_ok, live.report.shacl_text
