from e2ekg.scenario.model import (
    AnswerKey, Application, DependencyEdge, Fault, Scenario, Window,
)
from e2ekg.timeutil import pt


def minimal_scenario(extra_edge: tuple[str, str] | None = None) -> Scenario:
    apps = tuple(
        Application(id=i, aliases=(), owner_team="team-x", tier=1,
                    regions=("us-west-2",), kind=k)
        for i, k in (("a", "edge"), ("b", "service"), ("c", "datastore"))
    )
    edges = [
        DependencyEdge(caller="a", callee="b", protocol="grpc", pool_size=None),
        DependencyEdge(caller="b", callee="c", protocol="cql", pool_size=8),
    ]
    if extra_edge:
        edges.append(DependencyEdge(caller=extra_edge[0], callee=extra_edge[1],
                                    protocol="http", pool_size=None))
    return Scenario(
        name="minimal", seed=7, catalog=apps, edges=tuple(edges),
        window=Window(start_utc=pt(2026, 10, 14, 12, 0),
                      end_utc=pt(2026, 10, 14, 18, 0), metric_step_s=60),
        fault=Fault(kind="none", target=None, start_utc=pt(2026, 10, 14, 14, 0),
                    trigger_change_id=None),
        changes=(), history=(),
        answer_key=AnswerKey(root_cause_change_id=None, causal_chain=(),
                             red_herring_change_ids=(), evidence_tags=()),
    )
