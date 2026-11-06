"""Facts about candidate root-cause changes, computed from the signals, topology and degraded
calls the correlation engine reads. Facts only, never a verdict: the commander weighs them.

    case = Case(signals, edges, calls, observed, alert_uri, symptom_uri, gate=True)
    case.onset                       # the symptom's onset Signal, or None
    case.facts(change) -> Facts

The onset is the earliest ROOTED telemetry onset (one some change reaches through the engine's
admissible links, R42) on the alert's application, among the symptom's causes: where the
engine says the cascade began. Background noise no change explains never moves it.

A hop is one dependency edge caller -> callee on the path from the alert's application down to
the change's application. It is `degraded` when a slow or failed trace edge on it began at or
after the change's minute, `not_degraded` when spans on that call were observed then and none
degraded, and `unknown` when no span on it was observed then: an untraced application or call
tells nothing either way.

Times are the graph's skew-corrected UTC; "before" compares on the engine's minute grid."""
import heapq
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from e2ekg.correlate.engine import (CHANGE_TYPES, TELEMETRY, WINDOW_S, Chain, Signal, Topology,
                                    _order, candidate_links, correlate)

BEFORE = timedelta(seconds=WINDOW_S)     # telemetry this long before a change is "before" it
SAME_APP = timedelta(hours=1)            # other changes on its app from this long before it

HopStatus = Literal["degraded", "not_degraded", "unknown"]
_COST = {"degraded": (0, 0), "unknown": (0, 1), "not_degraded": (1, 0)}


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


@dataclass(frozen=True)
class Call:
    """A caller -> callee trace edge that turned slow or failed at `at`."""
    caller: str
    callee: str
    at: datetime
    uri: str


@dataclass(frozen=True)
class Observed:
    """Spans on caller -> callee were seen from `start` to `end`."""
    caller: str
    callee: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Hop:
    caller: str
    callee: str
    status: HopStatus
    signal: Call | None              # the degraded edge, when status is degraded


@dataclass(frozen=True)
class Before:
    """Telemetry on the change's app or below it, before it landed, with the changes whose
    cascade reaches it (none: no change explains it)."""
    signal: Signal
    roots: tuple[str, ...]


@dataclass(frozen=True)
class Facts:
    change: Signal
    call_path: tuple[Hop, ...] | None        # None: the alert app does not depend on its app
    minutes_before_onset: float | None       # negative: it landed after the onset
    minutes_before_alert: float
    rooted_before: tuple[Before, ...]
    unrooted_before: tuple[Signal, ...]
    same_app_changes: tuple[Signal, ...]     # an hour before it to the alert, time order
    chain: Chain | None                      # the engine's best chain through it


class Case:
    def __init__(self, signals: Iterable[Signal], edges: Iterable[tuple[str, str]],
                 calls: Iterable[Call], observed: Iterable[Observed], alert_uri: str,
                 symptom_uri: str, gate: bool = True):
        edges = list(edges)
        self.signals = sorted(set(signals), key=lambda s: (s.at_utc, s.uri))
        by_uri = {s.uri: s for s in self.signals}
        alert = by_uri.get(alert_uri)
        if alert is None or alert.app is None:
            raise ValueError(f"{alert_uri} is not an alert on an application")
        self.alert = alert
        self.topo = Topology(edges)
        self.callees: dict[str, set[str]] = defaultdict(set)
        for caller, callee in edges:
            self.callees[caller].add(callee)
        self.calls: dict[tuple[str, str], list[Call]] = defaultdict(list)
        for k in sorted(calls, key=lambda k: (k.at, k.uri)):
            self.calls[(k.caller, k.callee)].append(k)
        self.observed: dict[tuple[str, str], list[Observed]] = defaultdict(list)
        for o in observed:
            self.observed[(o.caller, o.callee)].append(o)
        symptom = symptom_uri if symptom_uri in by_uri else alert_uri
        causes = candidate_links(self.signals, edges, gate)
        self.chains = correlate(self.signals, edges, symptom, gate)
        self.roots = _roots(self.signals, causes)
        ancestry, todo = {by_uri[symptom]}, [by_uri[symptom]]
        while todo:
            for a, _ in causes.get(todo.pop(), ()):
                if a not in ancestry:
                    ancestry.add(a)
                    todo.append(a)
        self.onset = min((s for s in ancestry if s.type in TELEMETRY and s.app == alert.app
                          and self.roots[s]), key=lambda s: (s.at_utc, s.uri), default=None)

    def depends(self, caller: str | None, callee: str | None) -> bool:
        return caller is not None and callee is not None and (
            caller == callee or caller in self.topo.upstream(callee))

    def facts(self, c: Signal) -> Facts:
        def minutes(t: datetime) -> float:
            return round((t - c.at_utc).total_seconds() / 60, 1)

        before = [s for s in self.signals
                  if s.type in TELEMETRY and c.at_utc - BEFORE <= s.at_utc
                  and _minute(s.at_utc) < _minute(c.at_utc) and self.depends(c.app, s.app)]
        return Facts(
            change=c, call_path=self._call_path(c),
            minutes_before_onset=minutes(self.onset.at_utc) if self.onset else None,
            minutes_before_alert=minutes(self.alert.at_utc),
            rooted_before=tuple(Before(s, tuple(sorted(self.roots[s])))
                                for s in before if self.roots[s]),
            unrooted_before=tuple(s for s in before if not self.roots[s]),
            same_app_changes=tuple(s for s in self.signals
                                   if s.type in CHANGE_TYPES and s.app == c.app and s != c
                                   and c.at_utc - SAME_APP <= s.at_utc <= self.alert.at_utc),
            chain=max((ch for ch in self.chains if c in ch.links),
                      key=lambda ch: ch.confidence, default=None))

    def _hop(self, caller: str, callee: str, lo: datetime) -> Hop:
        hi = self.alert.at_utc
        degraded = [k for k in self.calls.get((caller, callee), ()) if lo <= k.at <= hi]
        if degraded:
            return Hop(caller, callee, "degraded", degraded[0])
        seen = any(o.start <= hi and o.end >= lo for o in self.observed.get((caller, callee), ()))
        return Hop(caller, callee, "not_degraded" if seen else "unknown", None)

    def _call_path(self, c: Signal) -> tuple[Hop, ...] | None:
        """The dependency path from the alert app down to the change's app with the fewest
        not_degraded hops, then the fewest unknown hops, then the fewest hops."""
        if not self.depends(self.alert.app, c.app):
            return None
        lo, goal = _minute(c.at_utc), c.app
        best: dict[str, tuple] = {self.alert.app: (0, 0, 0)}
        via: dict[str, Hop] = {}
        heap = [((0, 0, 0), self.alert.app)]
        while heap:
            cost, app = heapq.heappop(heap)
            if app == goal:
                break
            if cost > best[app]:
                continue
            for nxt in sorted(self.callees.get(app, ())):
                if not self.depends(nxt, goal):
                    continue
                hop = self._hop(app, nxt, lo)
                bad, unknown = _COST[hop.status]
                new = (cost[0] + bad, cost[1] + unknown, cost[2] + 1)
                if nxt not in best or new < best[nxt]:
                    best[nxt], via[nxt] = new, hop
                    heapq.heappush(heap, (new, nxt))
        path, app = [], goal
        while app != self.alert.app:
            path.append(via[app])
            app = via[app].caller
        return tuple(reversed(path))


def _roots(signals: list[Signal], causes) -> dict[Signal, frozenset[str]]:
    """The changes that reach each signal through admissible links (R42's "rooted")."""
    out: dict[Signal, frozenset[str]] = {}
    for b in sorted(signals, key=_order):
        out[b] = frozenset().union(*(out[a] | ({a.uri} if a.type in CHANGE_TYPES else set())
                                     for a, _ in causes.get(b, ())))
    return out
