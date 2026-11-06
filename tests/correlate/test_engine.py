"""The correlation engine on the as-of stores the live RCA reads (R34), and the rules
that decide it, each on a small hand-built signal set."""
from datetime import UTC, datetime, timedelta

import pytest

from e2ekg.correlate.engine import (
    CHANGE_TYPES, PRIORS, Signal, candidate_links, correlate, pair_score,
)
from e2ekg.graph.enrichers.correlation import priors_with_history, severity, signals_from_store
from e2ekg.graph.uris import change_uri
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.topology import upstream_closure

HERO = hero_scenario()
KIND = {c.id: c.kind for c in HERO.changes}
HERRINGS = HERO.answer_key.red_herring_change_ids


@pytest.fixture(scope="module")
def uri_of(hero_answers):
    external = {v: k for k, v in hero_answers["external_ids"].items()}
    return lambda cid: str(change_uri("hero", KIND[cid], external[cid]))


# --- the hero, as of its cutoff -------------------------------------------------------

def test_true_chain_ranks_first(harvested_hero_signals, uri_of):
    chains = correlate(*harvested_hero_signals, gate=True)
    assert chains[0].links[0].uri == uri_of("chg-cfg-0001")
    assert chains[0].confidence >= 0.6


def test_the_true_chain_is_the_scale_in_travelling_up_the_tms_callers(harvested_hero_signals,
                                                                       uri_of):
    links = correlate(*harvested_hero_signals)[0].links
    asg = {uri_of(c.id) for c in HERO.changes if c.kind == "scaling"}
    assert links[1].uri in asg
    path = upstream_closure(HERO.edges, HERO.fault.target) | {HERO.fault.target}
    assert {s.app for s in links[:-1]} <= path
    apps = [s.app for s in links]
    assert apps.index("title-metadata-svc") < apps.index("title-batch-loader") \
        < apps.index("lolomo-svc")
    assert (links[-2].type, links[-2].app) == ("alert", "homepage-graphql")
    assert links[-1].type == "incident"


def test_red_herrings_not_top_with_gate(harvested_hero_signals, uri_of):
    roots = [c.links[0].uri for c in correlate(*harvested_hero_signals, gate=True)[:3]]
    assert not {uri_of(h) for h in HERRINGS} & set(roots)


def test_without_gate_api_gateway_trap_survives(harvested_hero_signals, uri_of):
    roots = [c.links[0].uri for c in correlate(*harvested_hero_signals, gate=False)]
    assert uri_of("chg-dep-0001") in roots


def test_skewed_host_does_not_reorder_chain(harvested_hero_signals):
    links = [link.type for link in correlate(*harvested_hero_signals)[0].links]
    assert links.index("metric_anomaly") < links.index("log_error")


def test_history_priors_do_not_flip_the_hero(harvested_hero_signals, harvested_hero_as_of, uri_of):
    pri = priors_with_history(harvested_hero_as_of.store)
    assert pri[("deployment", "log_error")] > PRIORS[("deployment", "log_error")]
    assert correlate(*harvested_hero_signals, priors=pri)[0].links[0].uri == uri_of("chg-cfg-0001")


def test_control_variant_yields_no_chain_to_a_change(harvested_control_signals):
    assert all(c.links[0].type not in CHANGE_TYPES for c in correlate(*harvested_control_signals))


def test_the_control_investigates_an_alert_of_its_own_window(harvested_control_as_of,
                                                             harvested_control_signals):
    r = harvested_control_as_of.report
    assert all(v.success for v in r.results.values()), r.results
    signals, _, symptom = harvested_control_signals
    assert "/alrt/alert/v-control/" in symptom
    assert symptom in {s.uri for s in signals if s.type == "alert"}
    assert not [s for s in signals if s.type == "incident"]       # history incidents are not live


def test_the_only_incident_signal_is_the_live_symptom(harvested_hero_signals):
    signals, _, symptom = harvested_hero_signals
    assert symptom.endswith("/inc/incident/hero/INC-5377")
    assert [s.uri for s in signals if s.type == "incident"] == [symptom]


# --- signals drawn from the store -----------------------------------------------------

def test_one_signal_per_type_app_and_onset_minute(harvested_hero_as_of):
    signals = signals_from_store(harvested_hero_as_of.store)
    keys = [(s.type, s.app, s.at_utc.replace(second=0)) for s in signals
            if s.type not in CHANGE_TYPES | {"alert", "incident"}]
    assert len(keys) == len(set(keys))
    cited = [e for s in signals for e in s.evidence]
    assert len(cited) == len(set(cited))
    tbl = [s for s in signals if s.type == "metric_anomaly" and s.app == "title-batch-loader"]
    assert sum(len(s.evidence) for s in tbl) > len(tbl)          # per-series rows folded


def test_severity_is_half_at_the_detector_threshold_and_below_one_for_huge_magnitudes():
    assert severity("metric_anomaly", 5.0) == 0.5
    assert severity("log_error", 6.0) == 0.5
    assert 0.99 < severity("metric_anomaly", 1780.0) < 1.0
    assert severity("slow_trace", 2.0) < severity("slow_trace", 20.0)


def test_the_enricher_writes_the_chain_that_explains_the_live_incident(harvested_hero_as_of,
                                                                       uri_of):
    rows = harvested_hero_as_of.store.query("""
        PREFIX ops: <https://kg.local/ops#> PREFIX prov: <http://www.w3.org/ns/prov#>
        PREFIX inc: <https://kg.local/inc#>
        SELECT ?c ?conf ?i ?src WHERE {
          ?c a ops:CausalChain ; ops:explains/inc:incidentId "INC-5377" ; ops:confidence ?conf ;
             ops:hasLink ?l . ?l ops:linkOrder ?i ; prov:wasDerivedFrom ?src }""")
    best = max({r["c"] for r in rows}, key=lambda c: float(next(r["conf"] for r in rows
                                                              if r["c"] == c)))
    links = sorted((int(r["i"]), r["src"]) for r in rows if r["c"] == best)
    assert [i for i, _ in links] == list(range(len(links)))
    assert links[0][1] == uri_of("chg-cfg-0001")
    assert links[-1][1].endswith("/INC-5377")


# --- the rules, on hand-built signals ---------------------------------------------------

T0 = datetime(2026, 10, 14, 22, 0, tzinfo=UTC)


def sig(name: str, kind: str, app: str | None, minute: float, sev: float = 1.0) -> Signal:
    return Signal(uri=f"urn:{name}", type=kind, app=app, at_utc=T0 + timedelta(minutes=minute),
                  severity=sev)


# edge -> mid -> svc (caller -> callee); the edge is every request's ancestor
EDGES = [("edge", "mid"), ("mid", "svc"), ("edge", "other")]


def _linked(signals, a, b, gate=True) -> bool:
    return a in {x for x, _ in candidate_links(signals, EDGES, gate).get(b, ())}


def test_pair_score_follows_the_brief_formula():
    a, b = sig("a", "metric_anomaly", "svc", 0), sig("b", "log_error", "svc", 5, sev=0.6)
    assert pair_score(a, b, 0.75) == pytest.approx(0.75 * (1 - 0.4 * 300 / 600) * (0.5 + 0.3))
    assert pair_score(b, a, 0.75) == 0.0
    assert pair_score(a, sig("c", "log_error", "svc", 10.5), 0.75) == 0.0


def test_an_effect_up_the_callers_passes_the_gate_and_one_off_them_does_not():
    dep = sig("dep", "deployment", "svc", 0)
    up, off = sig("up", "log_error", "edge", 3), sig("off", "log_error", "other", 3)
    assert _linked([dep, up, off], dep, up)
    assert not _linked([dep, up, off], dep, off)
    assert _linked([dep, up, off], dep, off, gate=False)


def test_an_alert_anywhere_passes_the_gate():
    dep, alert = sig("dep", "deployment", "svc", 0), sig("alert", "alert", "other", 3)
    assert _linked([dep, alert], dep, alert)


def test_a_change_landing_after_the_cascade_began_is_explained_away():
    first = sig("first", "metric_anomaly", "svc", 0)
    mid = sig("mid", "metric_anomaly", "mid", 2)
    late = sig("late", "experiment", "mid", 3)
    effect = sig("effect", "log_error", "edge", 6)
    signals = [first, mid, late, effect]
    assert _linked(signals, mid, effect) and not _linked(signals, late, effect)
    assert _linked(signals, late, effect, gate=False)
    early = sig("early", "experiment", "mid", 1)
    assert _linked([early, mid, effect], early, effect)


def test_roles_people_make_changes_alerts_open_incidents_and_a_condition_does_not_cause_itself():
    m1, m2 = sig("m1", "metric_anomaly", "svc", 0), sig("m2", "metric_anomaly", "svc", 2)
    dep = sig("dep", "deployment", "svc", 3)
    alert, inc = sig("alert", "alert", "svc", 4), sig("inc", "incident", "svc", 5)
    later = sig("later", "log_error", "svc", 6)
    signals = [m1, m2, dep, alert, inc, later]
    assert not _linked(signals, m1, m2)
    assert not _linked(signals, m2, dep)
    assert _linked(signals, alert, inc) and not _linked(signals, alert, later)
    assert not _linked(signals, inc, later)


def test_chains_run_from_a_source_one_per_root_ranked_and_cut_at_the_threshold():
    cfg = sig("cfg", "config_change", "svc", 0)
    scale = sig("scale", "autoscaling", "svc", 2)
    lat = sig("lat", "metric_anomaly", "svc", 4)
    alert = sig("alert", "alert", "edge", 6)
    inc = sig("inc", "incident", "edge", 8)
    weak = sig("weak", "commit", None, 1)
    chains = correlate([cfg, scale, lat, alert, inc, weak], EDGES, "urn:inc")
    assert [[s.uri for s in c.links] for c in chains] == [
        ["urn:cfg", "urn:scale", "urn:lat", "urn:alert", "urn:inc"]]
    assert chains[0].confidence == pytest.approx(
        (pair_score(cfg, scale, 0.95) * pair_score(scale, lat, 0.85)
         * pair_score(lat, alert, 0.9) * pair_score(alert, inc, 0.95)) ** 0.25)
