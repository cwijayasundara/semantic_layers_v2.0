"""Topology-gated correlation: causal chains of signals that end at a symptom.

A Signal is one causal event: a change, an alert, an incident, or an onset of a
telemetry type on one application (the enricher folds the detections behind it).

The candidate-link graph has an edge a -> b ("a may have caused b") when
  * a precedes b on the detectors' minute grid (R41), within WINDOW: telemetry
    onsets are floored to their minute, so a change, alert or incident (stamped to the
    second) may precede an onset in its own minute, while a same-minute telemetry pair
    or a telemetry onset and a change in one minute have no knowable order (the
    change is taken first, so the graph stays acyclic). Exact-time pairs need a to be
    strictly earlier. No admissibility rests on a sub-minute coincidence;
  * the roles allow it: people and pipelines make deployments, config changes,
    experiments and commits, so nothing causes them (they are roots only); an alert
    causes only an incident, and an incident is caused only by the alert its opening
    page names (R40); an incident causes nothing; and two onsets of one telemetry
    type on one app are one condition developing, not cause and effect;
  * with the topology gate on (R9, R40), b is on a.app, or b.app calls a.app
    transitively (an effect travels up the callers), alerts included; and a change a
    may cause b only if b's cascade had not begun before a: no telemetry ancestor of b
    (through the gated telemetry links) is older than a. An effect whose degradation
    was already under way when the change landed is explained by what came before it
    (explaining away); a red herring is a change that lands while the incident is
    already unfolding. Reachability alone cannot reject it: the edge and the clients
    call almost everything, so a late change anywhere below them reaches the symptom.
Each edge scores pair_score(a, b). A chain is a path of this graph from a change with
no admissible cause (its root) to the symptom; a telemetry signal with no admissible
cause is an unexplained onset, not a cause, and roots no chain. Its confidence is the
geometric mean of its pair scores. A backward beam search from the symptom finds them; the
best chain per root is kept, those under MIN_CONFIDENCE are dropped, and the rest
are ranked by confidence, then by earliest root."""
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import exp, inf, log

from e2ekg.scenario.model import CHANGE_SIGNAL, SIGNAL_TYPES, AppId

WINDOW_S = 600
MIN_CONFIDENCE = 0.6
BEAM_WIDTH, MAX_LENGTH = 20, 10
DEFAULT_PRIOR = 0.5
TELEMETRY = ("metric_anomaly", "log_error", "slow_trace", "failed_trace", "e2e_bottleneck",
             "error_pattern")
# The brief's table, plus every telemetry -> telemetry pair at the 0.75 the brief gives
# each telemetry link it names: metric, log and trace views of one latency or error
# cascade fire in an order set by detector sensitivity (hero: slow traces before the
# error logs), so no telemetry pair is a weaker link than another.
PRIORS: dict[tuple[str, str], float] = {
    **{(a, b): 0.75 for a in TELEMETRY for b in TELEMETRY},
    ("deployment", "log_error"): 0.9,
    ("config_change", "metric_anomaly"): 0.9,
    ("config_change", "autoscaling"): 0.95,
    ("autoscaling", "metric_anomaly"): 0.85,
    ("metric_anomaly", "log_error"): 0.75,
    ("log_error", "slow_trace"): 0.75,
    ("slow_trace", "metric_anomaly"): 0.75,
    ("failed_trace", "incident"): 0.85,
    ("metric_anomaly", "alert"): 0.9,
    ("alert", "incident"): 0.95,
}
CHANGE_TYPES = frozenset(CHANGE_SIGNAL.values())
ROOT_ONLY = CHANGE_TYPES - {"autoscaling"}      # autoscaling reacts to config and load


@dataclass(frozen=True)
class Signal:
    uri: str
    type: str
    app: AppId | None
    at_utc: datetime
    severity: float                  # [0, 1]
    evidence: tuple[str, ...] = field(default=(), compare=False)   # node URIs it aggregates
    opened_by: tuple[str, ...] = field(default=(), compare=False)  # an incident: its page's alerts

    def __post_init__(self):
        if self.type not in SIGNAL_TYPES:
            raise ValueError(f"unknown signal type {self.type!r}")
        if not 0.0 <= self.severity <= 1.0:
            raise ValueError(f"severity {self.severity} outside [0, 1]")


@dataclass(frozen=True)
class Chain:
    links: tuple[Signal, ...]        # root first, symptom last
    confidence: float


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def precedes(a: Signal, b: Signal) -> bool:
    """a may have happened before b, on the minute grid, within the window."""
    dt = (b.at_utc - a.at_utc).total_seconds()
    if dt > WINDOW_S:
        return False
    if a.type in TELEMETRY and b.type in CHANGE_TYPES:
        return _minute(a.at_utc) < _minute(b.at_utc)
    if a.type in CHANGE_TYPES and b.type in TELEMETRY:
        return _minute(a.at_utc) <= _minute(b.at_utc)
    return dt > 0


def _order(s: Signal) -> tuple:
    """A topological order of every admissible link: minute, then changes before onsets
    before alerts and incidents, then time."""
    rank = 0 if s.type in CHANGE_TYPES else 1 if s.type in TELEMETRY else 2
    return _minute(s.at_utc), rank, s.at_utc, s.uri


def pair_score(a: Signal, b: Signal, prior: float) -> float:
    """The brief's score; a change and an onset in one minute are dt = 0 apart."""
    if not precedes(a, b):
        return 0.0
    dt = max((b.at_utc - a.at_utc).total_seconds(), 0.0)
    return prior * (1 - 0.4 * dt / WINDOW_S) * (0.5 + 0.5 * b.severity)


def may_cause(a: Signal, b: Signal) -> bool:
    if b.type in ROOT_ONLY or a.type == "incident":
        return False
    if b.type == "incident":
        return a.uri in b.opened_by
    if a.type == b.type and a.app == b.app and a.type not in CHANGE_TYPES:
        return False
    return a.type != "alert" or b.type == "incident"


class Topology:
    """Caller closures over (caller, callee) dependency edges."""

    def __init__(self, edges: Iterable[tuple[str, str]]):
        self._callers: dict[str, set[str]] = defaultdict(set)
        for caller, callee in edges:
            self._callers[callee].add(caller)
        self._up: dict[str, frozenset[str]] = {}

    def upstream(self, app: str) -> frozenset[str]:
        if app not in self._up:
            seen, todo = set(), deque([app])
            while todo:
                for c in self._callers.get(todo.popleft(), ()):
                    if c not in seen:
                        seen.add(c)
                        todo.append(c)
            self._up[app] = frozenset(seen - {app})
        return self._up[app]

    def allows(self, a: Signal, b: Signal) -> bool:
        if b.type == "incident":            # its page names its cause (may_cause)
            return True
        if a.app is None or b.app is None:
            return False
        return a.app == b.app or b.app in self.upstream(a.app)


def candidate_links(signals: Iterable[Signal], edges: Iterable[tuple[str, str]],
                    gate: bool = True, priors: Mapping[tuple[str, str], float] = PRIORS,
                    ) -> dict[Signal, list[tuple[Signal, float]]]:
    """b -> [(a, pair score)] over every admissible cause a of b."""
    ordered = sorted(set(signals), key=_order)
    topo = Topology(edges)
    causes: dict[Signal, list[tuple[Signal, float]]] = defaultdict(list)
    for j, b in enumerate(ordered):
        for a in reversed(ordered[:j]):
            if (_minute(b.at_utc) - _minute(a.at_utc)).total_seconds() > WINDOW_S:
                break
            if not may_cause(a, b) or (gate and not topo.allows(a, b)):
                continue
            s = pair_score(a, b, priors.get((a.type, b.type), DEFAULT_PRIOR))
            if s > 0:
                causes[b].append((a, s))
    if gate:
        began: dict[Signal, datetime] = {}         # b's earliest telemetry ancestor
        for b in ordered:
            lineage = [min(a.at_utc, began.get(a, a.at_utc)) for a, _ in causes.get(b, ())
                       if a.type not in CHANGE_TYPES]
            if lineage:
                began[b] = min(lineage)
                causes[b] = [(a, s) for a, s in causes[b]
                             if a.type not in CHANGE_TYPES
                             or _minute(a.at_utc) <= _minute(began[b])]
    return causes


def _gm(log_scores: tuple[float, ...]) -> float:
    return exp(sum(log_scores) / len(log_scores))


def correlate(signals: Iterable[Signal], edges: Iterable[tuple[str, str]], symptom_uri: str,
              gate: bool = True, priors: Mapping[tuple[str, str], float] = PRIORS,
              ) -> list[Chain]:
    signals = list(signals)
    symptom = next((s for s in signals if s.uri == symptom_uri), None)
    if symptom is None:
        return []
    causes = candidate_links(signals, edges, gate, priors)
    to_root: dict[Signal, float] = {}         # fewest links back to a root change
    for b in sorted(set(signals), key=_order):
        if b not in causes:
            to_root[b] = 0 if b.type in CHANGE_TYPES else inf
        else:
            to_root[b] = 1 + min(to_root[a] for a, _ in causes[b])
    beam: list[tuple[tuple[Signal, ...], tuple[float, ...]]] = [((symptom,), ())]
    best: dict[str, Chain] = {}
    while beam:
        grown = []
        for links, logs in beam:
            preds = causes.get(links[0], ())
            if not preds and logs and links[0].type in CHANGE_TYPES:
                c = Chain(links, _gm(logs))
                if c.confidence > best.get(links[0].uri, Chain((), -1.0)).confidence:
                    best[links[0].uri] = c
            grown += [((a, *links), (log(s), *logs)) for a, s in preds
                      if len(links) + 1 + to_root[a] <= MAX_LENGTH]
        head: dict[Signal, tuple] = {}             # same head, same length: the best dominates
        for x in grown:
            if x[0][0] not in head or sum(x[1]) > sum(head[x[0][0]][1]):
                head[x[0][0]] = x
        beam = sorted(head.values(),
                      key=lambda x: (-_gm(x[1]), [s.uri for s in x[0]]))[:BEAM_WIDTH]
    chains = [c for c in best.values() if c.confidence >= MIN_CONFIDENCE]
    return sorted(chains, key=lambda c: (-c.confidence, c.links[0].at_utc, c.links[0].uri))


__all__ = ["BEAM_WIDTH", "CHANGE_TYPES", "Chain", "DEFAULT_PRIOR", "MAX_LENGTH", "MIN_CONFIDENCE",
           "PRIORS", "Signal", "TELEMETRY", "Topology", "WINDOW_S",
           "candidate_links", "correlate", "may_cause", "pair_score", "precedes"]
