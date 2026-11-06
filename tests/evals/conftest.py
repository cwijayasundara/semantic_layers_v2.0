"""A data root laid out like data/ (raw, _answers, processed, as-of graphs) over the fixture
hero and control, for the eval harness."""
import json
import shutil

import pytest

from e2ekg.graph.uris import change_uri
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.variants import variant_scenarios


@pytest.fixture(scope="session")
def eval_root(tmp_path_factory, preprocessed, harvested):
    root = tmp_path_factory.mktemp("evalroot")
    (root / "raw/_answers").mkdir(parents=True)
    for name in ("hero", "v-control"):
        pp, h = preprocessed(name), harvested(name, as_of=True)
        shutil.copy(pp.answers / f"{name}.json", root / "raw/_answers" / f"{name}.json")
        for link, target in {f"raw/{name}": pp.raw, f"processed/{name}": pp.proc,
                             f"graph/{name}/as_of": h.out}.items():
            (root / link).parent.mkdir(parents=True, exist_ok=True)
            (root / link).symlink_to(target)
    return root


@pytest.fixture(scope="session")
def uri_of(eval_root):
    """(scenario, scenario change id) -> the change's URI, mapped through the answer key."""
    scenarios = {s.name: s for s in (hero_scenario(), *variant_scenarios())}

    def get(scenario: str, cid: str) -> str:
        answers = json.loads((eval_root / "raw/_answers" / f"{scenario}.json").read_text())
        ext = {v: k for k, v in answers["external_ids"].items()}
        kind = next(c.kind for c in scenarios[scenario].changes if c.id == cid)
        return str(change_uri(scenario, kind, ext[cid]))
    return get
