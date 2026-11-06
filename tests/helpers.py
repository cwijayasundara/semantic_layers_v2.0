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


# ---- Task 2 helpers ----
from collections import deque  # noqa: E402

import numpy as np  # noqa: E402


def rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def hero_fault() -> Fault:
    return Fault(kind="capacity_reduction", target="title-metadata-svc",
                 start_utc=pt(2026, 10, 14, 14, 52),
                 trigger_change_id="chg-hero-capacity-reduction")


def hero_window() -> Window:
    return Window(start_utc=pt(2026, 10, 14, 12, 0),
                  end_utc=pt(2026, 10, 14, 18, 0), metric_step_s=60)


def shortest_hops(edges, src: str, dst: str) -> int | None:
    """BFS hop count following caller->callee, or None if unreachable."""
    out: dict[str, list[str]] = {}
    for e in edges:
        out.setdefault(e.caller, []).append(e.callee)
    seen, q = {src}, deque([(src, 0)])
    while q:
        n, d = q.popleft()
        if n == dst:
            return d
        for m in out.get(n, ()):
            if m not in seen:
                seen.add(m)
                q.append((m, d + 1))
    return None
