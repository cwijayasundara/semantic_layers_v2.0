import pytest
from pydantic import ValidationError

from e2ekg.scenario.model import CHANGE_SIGNAL, SIGNAL_TYPES, ChainLink, Scenario
from e2ekg.timeutil import pt
from helpers import minimal_scenario


def test_scenario_round_trips_json():
    s = minimal_scenario()
    assert Scenario.model_validate_json(s.model_dump_json()) == s


def test_scenario_is_frozen():
    with pytest.raises(ValidationError):
        minimal_scenario().name = "x"


def test_edge_to_unknown_app_rejected():
    with pytest.raises(ValidationError, match="unknown app"):
        minimal_scenario(extra_edge=("a", "ghost"))


def test_pt_helper_is_utc():
    assert pt(2026, 10, 14, 15, 30).isoformat() == "2026-10-14T22:30:00+00:00"


def test_chain_link_rejects_unknown_signal():
    with pytest.raises(ValidationError):
        ChainLink(signal="vibes", app="a", at_utc=pt(2026, 10, 14, 15, 0))


def test_change_signal_maps_into_signal_types():
    assert len(SIGNAL_TYPES) == 13
    assert set(CHANGE_SIGNAL.values()) <= set(SIGNAL_TYPES)


def _with(**kw):
    d = minimal_scenario().model_dump()
    d.update(kw)
    return Scenario.model_validate(d)


def _past(**kw):
    d = dict(id="i1", opened_utc=pt(2026, 10, 1, 1, 0), severity="SEV2", affected="a",
             cause_change_kind=None, cause_app=None, resolver_handle="h", summary="s")
    d.update(kw)
    return d


def _change(cid="c1"):
    return dict(id=cid, kind="deployment", app="a", at_utc=pt(2026, 10, 14, 13, 0), attrs={}, is_root_cause=False)


def _fault(**kw):
    d = dict(kind="bad_deploy", target="a", start_utc=pt(2026, 10, 14, 13, 0), trigger_change_id=None)
    d.update(kw)
    return d


def _ak(**kw):
    d = dict(root_cause_change_id=None, causal_chain=(), red_herring_change_ids=(), evidence_tags=())
    d.update(kw)
    return d


@pytest.mark.parametrize("kw", [
    dict(fault=_fault(target="ghost")),
    dict(history=(_past(affected="ghost"),)),
    dict(history=(_past(cause_app="ghost"),)),
    dict(answer_key=_ak(causal_chain=({"signal": "alert", "app": "ghost", "at_utc": pt(2026, 10, 14, 13, 0)},))),
    dict(fault=_fault(trigger_change_id="nope"), changes=(_change(),)),
    dict(answer_key=_ak(root_cause_change_id="nope"), changes=(_change(),)),
    dict(answer_key=_ak(red_herring_change_ids=("nope",)), changes=(_change(),)),
])
def test_dangling_references_rejected(kw):
    with pytest.raises(ValidationError):
        _with(**kw)


def test_valid_references_accepted():
    _with(fault=_fault(trigger_change_id="c1"), changes=(_change(),), history=(_past(cause_app="b"),),
          answer_key=_ak(root_cause_change_id="c1", red_herring_change_ids=("c1",)))
