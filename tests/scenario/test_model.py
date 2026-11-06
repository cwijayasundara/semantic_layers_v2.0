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
