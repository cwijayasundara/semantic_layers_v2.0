from dataclasses import dataclass

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


# ---- Task 5 helpers: read the raw tree (and _answers/), never generator state ----
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import yaml  # noqa: E402

from e2ekg.timeutil import PT  # noqa: E402

DEFAULT_RAW = Path("data/raw")


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def load_answers(name: str, root: Path = DEFAULT_RAW) -> dict:
    return json.loads((Path(root) / "_answers" / f"{name}.json").read_text())


def manifest(raw_root: Path) -> SimpleNamespace:
    raw_root = Path(raw_root)
    d = json.loads((raw_root.parent / "_answers" / f"{raw_root.name}.manifest.json").read_text())
    return SimpleNamespace(**d)


def jsonl(path: Path) -> list[dict]:
    """Parse a JSONL raw file, skipping lines the mess made unparseable."""
    out = []
    for line in Path(path).read_bytes().split(b"\n"):
        try:
            out.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            continue
    return out


def _onset(raw_root: Path, client: bool) -> datetime:
    ans = load_answers(raw_root.name, raw_root.parent)
    for link in ans["answer_key"]["causal_chain"]:
        if link["signal"] == "metric_anomaly" and link["app"].startswith("client-") == client:
            return datetime.fromisoformat(link["at_utc"])
    raise LookupError("no metric_anomaly link")


def atlas_points(raw_root: Path, name: str, **tag_filter: str) -> dict[int, float]:
    """t(ms) -> mean v over distinct series points matching the tag filter
    (met.duplicate_series copies are deduplicated, met.gap nulls skipped)."""
    seen: dict[tuple, float] = {}
    for p in jsonl(Path(raw_root) / "metrics" / "atlas.jsonl"):
        tags = p.get("tags") or {}
        if p.get("name") != name or p.get("v") is None:
            continue
        if any(tags.get(k) != v for k, v in tag_filter.items()):
            continue
        seen[(p["t"], tuple(sorted(tags.items())))] = p["v"]
    by_t: dict[int, list[float]] = {}
    for (t, _), v in seen.items():
        by_t.setdefault(t, []).append(v)
    return {t: sum(vs) / len(vs) for t, vs in by_t.items()}


def ttr_delta(raw_root: Path, platform: str, cell: str | None = None) -> float:
    """Mean client.ttr.p90 in [onset+5m, onset+15m) minus [onset-10m, onset)."""
    onset = _onset(Path(raw_root), client=True)
    filt = {"platform": platform} | ({"ab.cell": cell} if cell else {})
    pts = atlas_points(raw_root, "client.ttr.p90", **filt)

    def mean(a: timedelta, b: timedelta) -> float:
        lo, hi = (int((onset + x).timestamp() * 1000) for x in (a, b))
        vs = [v for t, v in pts.items() if lo <= t < hi]
        assert vs, (platform, cell, a, b)
        return sum(vs) / len(vs)
    return (mean(timedelta(minutes=5), timedelta(minutes=15))
            - mean(timedelta(minutes=-10), timedelta(0)))


_TS_JSON = re.compile(rb'"ts":(?:"([^"]+)"|(\d+))')


def json_log_ts(line: bytes) -> datetime | None:
    m = _TS_JSON.search(line)
    if not m:
        return None
    if m.group(2):
        return datetime.fromtimestamp(int(m.group(2)) / 1000, UTC)
    return datetime.fromisoformat(m.group(1).decode().replace("Z", "+00:00"))


def hero_local(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, 14, h, m, tzinfo=PT).astimezone(UTC)


def npe_rate(raw_root: Path, start: str, end: str) -> float:
    """NullPointerException lines per hour in artwork-svc logs, [start, end) PT."""
    lo, hi = hero_local(start), hero_local(end)
    n = 0
    for f in (Path(raw_root) / "logs" / "artwork-svc").glob("*.log"):
        for line in f.read_bytes().split(b"\n"):
            if b"NullPointerException" in line:
                ts = json_log_ts(line)
                n += ts is not None and lo <= ts < hi
    return n / ((hi - lo).total_seconds() / 3600)


def _slack_ts(iso: str) -> str:
    us = (datetime.fromisoformat(iso) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(
        microseconds=1)
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


def service_mentions(raw_root: Path, texts: list[str]) -> set[str]:
    """Catalog ids mentioned by id or by a known chat nickname."""
    from e2ekg.generate.mess.chat import NICKNAMES
    from e2ekg.generate.mess.identity import SLACK_ONLY
    cat = yaml.safe_load_all((Path(raw_root) / "catalog" / "apps.yaml").read_text())
    names = {a["id"]: a["id"] for a in cat}
    names |= {nick: app for app, nick in (NICKNAMES | SLACK_ONLY).items()}
    pats = {n: re.compile(rf"(?<![\w-]){re.escape(n)}(?![\w-])") for n in names}
    return {names[n] for n, p in pats.items() if any(p.search(t) for t in texts)}


def slack_messages(raw_root: Path, channel: str) -> list[dict]:
    """Authored messages: message_changed events and injected retractions
    (listed in the manifest) are mess, not messages."""
    rel = f"slack/{channel}/messages.jsonl"
    injected = {_slack_ts(json.loads(i["detail"])["retracted_at"])
                for i in manifest(raw_root).injections
                if i["injector_id"] == "chat.retraction" and i["rel_path"] == rel}
    return [m for m in jsonl(Path(raw_root) / rel)
            if not m.get("subtype") and m.get("ts") not in injected]


def slack_stats(raw_root: Path, channel: str) -> tuple[int, int, int]:
    msgs = slack_messages(raw_root, channel)
    return (len(msgs), len({m["user"] for m in msgs}),
            len(service_mentions(raw_root, [m["text"] for m in msgs])))


@dataclass(frozen=True)
class Preprocessed:
    raw: Path        # <tmp>/raw/hero
    proc: Path       # <tmp>/processed/hero
    answers: Path    # <tmp>/raw/_answers
    report: object   # PreprocessReport


@dataclass(frozen=True)
class Harvested:
    store: object    # e2ekg.graph.store.Store
    report: object   # HarvestReport
    proc: Path
    answers: Path
    out: Path        # the harvest output directory (store/ and harvest.ttl)
    as_of: object = None   # the cutoff of an as-of store (datetime), None for the full store
    symptom: str | None = None   # the live symptom's URI (HarvestContext.symptom)


def asof_only_pattern(proc: Path, as_of) -> tuple[str, str, str, list[str]]:
    """(app_uri, template_id, template, raw ids earliest first) of a log pattern that exists
    only in the as-of view: mined over the lines before `as_of`, its template id names no
    full-window template (the lines after the cutoff reshaped it there)."""
    import polars as pl

    from e2ekg.preprocess.logtemplates import mine_templates
    seen = pl.read_parquet(proc / "logs.parquet", columns=["raw_id", "app_uri", "ts_utc", "msg"]
                           ).filter(pl.col("ts_utc") < as_of)
    mined = mine_templates(seen)
    full = set(pl.read_parquet(proc / "derived" / "rows_logs.parquet", columns=["key"])["key"])
    only = mined.filter(~pl.col("template_id").is_in(list(full)))
    assert only.height, "the fixture has no template that the later lines reshape"
    app, tid = only.select("app_uri", "template_id").unique().sort("app_uri", "template_id").row(0)
    rows = only.filter((pl.col("app_uri") == app) & (pl.col("template_id") == tid)) \
        .sort("ts_utc", "raw_id")
    return app, tid, rows["template"][0], rows["raw_id"].to_list()
