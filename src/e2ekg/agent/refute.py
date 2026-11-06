"""Refutation: the tests a candidate root cause must survive, computed from the signals and
topology the correlation engine reads, never from a model's judgment.

    inv = Investigation(signals, edges, app_kinds, alert_uri, filed_against, chains)
    for v in inv.judge(inv.candidates(extra_uris)): v.survives, v.checks

The symptom is the ALERT's application degrading: what was observed. The apps an incident
was filed against are a person's belief, so they only add candidates. A candidate is a
change; it survives when every check in CHECKS passes. Times are the graph's skew-corrected
UTC stamps, compared on the minute grid the engine uses."""
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from e2ekg.correlate.engine import CHANGE_TYPES, TELEMETRY, WINDOW_S, Chain, Signal, Topology

LOOKBACK = timedelta(hours=1)          # candidates: changes this long before the onset
EFFECT = timedelta(seconds=2 * WINDOW_S)
EPISODE_GAP = timedelta(seconds=WINDOW_S)


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _z(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ" if t.second == t.microsecond == 0 else "%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    why: str


@dataclass(frozen=True)
class Verdict:
    change: Signal
    checks: tuple[Check, ...]
    chain_conf: float | None       # the best causal chain the change is on

    @property
    def survives(self) -> bool:
        return all(c.ok for c in self.checks)


class Investigation:
    def __init__(self, signals: Iterable[Signal], edges: Iterable[tuple[str, str]],
                 app_kinds: dict[str, str], alert_uri: str, filed_against: Sequence[str],
                 chains: Sequence[Chain]):
        self.signals = sorted(signals, key=lambda s: (s.at_utc, s.uri))
        self.topo = Topology(edges)
        self.chains = chains
        self.filed_against = tuple(sorted(filed_against))
        alert = next((s for s in self.signals if s.uri == alert_uri), None)
        if alert is None or alert.app is None:
            raise ValueError(f"{alert_uri} is not an alert on an application")
        self.alert = alert
        self.onset = self._onset()
        self.clients = tuple(sorted(a for a in self.topo.upstream(alert.app)
                                    if app_kinds.get(a) == "client"))

    def depends(self, caller: str | None, callee: str | None) -> bool:
        """`caller` calls `callee`, directly or through others, or is it."""
        return caller is not None and callee is not None and (
            caller == callee or caller in self.topo.upstream(callee))

    def _onset(self) -> Signal:
        """The first telemetry of the alert app's degradation episode: walking back from the
        alert, onsets on its app no more than EPISODE_GAP apart."""
        first = self.alert
        for s in reversed(self.signals):
            if s.at_utc > self.alert.at_utc or s.type not in TELEMETRY or s.app != self.alert.app:
                continue
            if first.at_utc - s.at_utc > EPISODE_GAP:
                break
            first = s
        return first

    def telemetry(self, apps: Callable[[str | None], bool], lo: datetime, hi: datetime):
        return [s for s in self.signals
                if s.type in TELEMETRY and lo <= s.at_utc <= hi and apps(s.app)]

    def candidates(self, extra: Iterable[str] = ()) -> list[Signal]:
        """Every change in the hour before the onset on an app the alert app depends on, a
        client that reaches it, or an app the incident was filed against; plus `extra`.
        Autoscaling only when named: it reacts to load or configuration, never originates."""
        lo, hi = self.onset.at_utc - LOOKBACK, self.alert.at_utc
        near = {*self.clients, *self.filed_against}
        wanted = set(extra)
        return [s for s in self.signals if s.type in CHANGE_TYPES and (
            s.uri in wanted or (s.type != "autoscaling" and lo <= s.at_utc <= hi and (
                s.app in near or self.depends(self.alert.app, s.app))))]

    def judge(self, changes: Iterable[Signal]) -> list[Verdict]:
        """Survivors first (best chain, then earliest), then the refuted in time order."""
        out = [Verdict(c, tuple(check(self, c) for check in CHECKS), self._chain_conf(c))
               for c in changes]
        return sorted(out, key=lambda v: (not v.survives, -(v.chain_conf or 0.0),
                                          v.change.at_utc, v.change.uri))

    def _chain_conf(self, c: Signal) -> float | None:
        confs = [ch.confidence for ch in self.chains if any(s.uri == c.uri for s in ch.links)]
        return round(max(confs), 3) if confs else None


def closure(inv: Investigation, c: Signal) -> Check:
    a = inv.alert.app
    if inv.depends(a, c.app):
        return Check("closure", True, f"{a} depends on {c.app}")
    return Check("closure", False, f"{c.app} is not a dependency of {a}, where the alert fired")


def timing(inv: Investigation, c: Signal) -> Check:
    a, onset = inv.alert.app, inv.onset.at_utc
    if _minute(c.at_utc) <= _minute(onset):
        return Check("timing", True, f"landed {_z(c.at_utc)}, before {a} began degrading at "
                                     f"{_z(onset)}")
    return Check("timing", False, f"landed {_z(c.at_utc)}, after {a} began degrading at "
                                  f"{_z(onset)}")


def clients(inv: Investigation, c: Signal) -> Check:
    missing = [k for k in inv.clients if not inv.depends(k, c.app)]
    if missing:
        return Check("clients", False, f"does not explain {', '.join(missing)}, which reach "
                                       f"{inv.alert.app} without {c.app}")
    return Check("clients", True, f"every client of {inv.alert.app} depends on {c.app}")


def dependencies(inv: Investigation, c: Signal) -> Check:
    below = [s for s in inv.telemetry(lambda app: app != c.app and inv.depends(c.app, app),
                                      c.at_utc - EPISODE_GAP, c.at_utc)
             if _minute(s.at_utc) < _minute(c.at_utc)]
    if below:
        s = below[0]
        return Check("dependencies", False, f"{s.app}, which {c.app} depends on, was already "
                                            f"degrading ({s.type}) at {_z(s.at_utc)}")
    return Check("dependencies", True, f"nothing {c.app} depends on was degrading when it landed")


def origin(inv: Investigation, c: Signal) -> Check:
    if c.type == "autoscaling":
        return Check("origin", False, "autoscaling reacts to load or configuration")
    if c.type == "commit":
        shipped = [s for s in inv.signals if s.type == "deployment" and s.app == c.app
                   and c.at_utc < s.at_utc <= inv.onset.at_utc]
        if shipped:
            return Check("origin", False, f"a commit takes effect when deployed; "
                                          f"{shipped[0].uri} shipped it")
    return Check("origin", True, "an originating change")


def effect(inv: Investigation, c: Signal) -> Check:
    seen = [s for s in inv.signals if s.app == c.app and c.at_utc <= s.at_utc <= c.at_utc + EFFECT
            and (s.type in TELEMETRY or (s.type == "autoscaling" and s.uri != c.uri))]
    if seen:
        s = seen[0]
        return Check("effect", True, f"{c.app} shows {s.type} at {_z(s.at_utc)}")
    return Check("effect", False, f"nothing on {c.app} changed within "
                                  f"{EFFECT.seconds // 60} min of it")


CHECKS: tuple[Callable[[Investigation, Signal], Check], ...] = (
    closure, timing, clients, dependencies, origin, effect)
