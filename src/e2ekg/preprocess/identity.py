"""Identity resolution: messy surface names -> catalog apps. The catalog is the gate.

Order of attempts (first hit wins):
  1. exact catalog id or alias, case-folded.
  2. pattern strip, one step at a time with a catalog lookup after each step:
     `-prod`, `-v\\d+`, pod hashes (`-<10>-<5>` k8s names and `-[a-f0-9]{5,10}`),
     ASG `-asg-\\d+`; plus CamelCase -> kebab and separator-free ("squashed")
     comparison against ids and aliases.
  3. rapidfuzz token_set_ratio >= 92 with one best app and a runner-up >= 5 lower.
  4. the LLM picks from a closed list (acronym matches, apps named in the
     context, top fuzzy candidates; at most 5) plus `none`. An answer outside
     the list, an error or `none` is unresolved.
  5. unresolved. Team names are never apps.

A strip can only land on a catalog id or alias, so no unknown name is invented."""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import polars as pl
from pydantic import BaseModel
from rapidfuzz import fuzz

from e2ekg.llm import LLMClient
from e2ekg.scenario.model import AppId, Application

Method = Literal["exact", "alias", "pattern", "fuzzy", "llm", "unresolved"]
FUZZY_MIN, FUZZY_GAP, N_CANDIDATES = 92, 5, 5
APP_URI_PREFIX = "https://kg.local/app/application/"
CONF = {"exact": 1.0, "alias": 0.98, "pattern": 0.95, "fuzzy": 0.85, "llm": 0.7,
        "unresolved": 0.0}

_STRIPS = (
    re.compile(r"-prod$", re.I),
    re.compile(r"-asg-\d+$", re.I),
    re.compile(r"-v\d+$", re.I),
    re.compile(r"-[a-z0-9]{10}-[a-z0-9]{5}$", re.I),   # k8s pod name: <deploy>-<rs hash>-<pod>
    re.compile(r"-[a-f0-9]{5,10}$", re.I),             # single pod / build hash
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEP = re.compile(r"[\s_./-]+")


class Pick(BaseModel):
    choice: str


@dataclass(frozen=True)
class Resolution:
    app: AppId | None
    surface: str
    method: Method
    confidence: float

    @property
    def app_uri(self) -> str | None:
        return APP_URI_PREFIX + self.app if self.app else None


def app_uri(app_id: str) -> str:
    return APP_URI_PREFIX + app_id


def _kebab(s: str) -> str:
    return _CAMEL.sub("-", s).lower()


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _tokens(s: str) -> str:
    return " ".join(t for t in _SEP.split(_kebab(s)) if t)


SYSTEM = (
    "You resolve a service name written in logs, metrics or chat to exactly one application "
    "of a fixed catalog. Choose ONLY from the candidate list given, or answer none if no "
    "candidate is clearly the same service. Never answer with a team name or a new name."
)


class Resolver:
    def __init__(self, catalog: tuple[Application, ...], llm: LLMClient | None = None) -> None:
        self.llm = llm
        self.ids = tuple(a.id for a in catalog)
        self._by_name: dict[str, set[str]] = {}
        self._by_squash: dict[str, set[str]] = {}
        for a in catalog:
            for name in (a.id, *a.aliases):
                self._by_name.setdefault(name.casefold(), set()).add(a.id)
                self._by_squash.setdefault(_squash(name), set()).add(a.id)
        self._is_id = {a.id.casefold() for a in catalog}
        self._teams = {a.owner_team.casefold() for a in catalog}
        # every searchable name -> app, for fuzzy scoring
        self._names = [(_tokens(n), a.id) for a in catalog for n in (a.id, *a.aliases)]
        self._acronym: dict[str, set[str]] = {}
        for a in catalog:
            parts = [p for p in a.id.split("-") if not p.isdigit()]
            if len(parts) >= 2:
                self._acronym.setdefault("".join(p[0] for p in parts), set()).add(a.id)
        self._memo: dict[tuple[str, str], Resolution] = {}

    # -- lookups ---------------------------------------------------------
    def _lookup(self, s: str) -> tuple[str, Method] | None:
        hit = self._by_name.get(s.casefold())
        if hit and len(hit) == 1:
            (app,) = hit
            return app, ("exact" if s.casefold() in self._is_id else "alias")
        return None

    def _lookup_loose(self, s: str) -> str | None:
        for cand in (_kebab(s), s):
            hit = self._by_name.get(cand.casefold())
            if hit and len(hit) == 1:
                return next(iter(hit))
        hit = self._by_squash.get(_squash(s))
        if hit and len(hit) == 1:
            return next(iter(hit))
        return None

    def _strip_chain(self, s: str):
        yield s
        seen = {s}
        while True:
            for pat in _STRIPS:
                nxt = pat.sub("", s)
                if nxt != s and nxt and nxt not in seen:
                    s = nxt
                    seen.add(s)
                    yield s
                    break
            else:
                return

    def _fuzzy(self, s: str) -> list[tuple[str, float, float]]:
        """(app, token_set score, ranking score), best ranking first. Only the token_set
        score gates a fuzzy match; the ranking score also looks at the squashed form so
        truncated nicknames ('artworkmeta') still make the LLM's closed candidate list."""
        q, qs = _tokens(s), _squash(s)
        best: dict[str, tuple[float, float]] = {}
        for name, app in self._names:
            ts = fuzz.token_set_ratio(q, name)
            rk = max(ts, fuzz.partial_ratio(qs, _squash(name)) * 0.9 if len(qs) >= 4 else 0)
            old = best.get(app, (-1.0, -1.0))
            best[app] = (max(old[0], ts), max(old[1], rk))
        return sorted(((a, ts, rk) for a, (ts, rk) in best.items()),
                      key=lambda x: (-x[2], -x[1], x[0]))

    # -- resolution ------------------------------------------------------
    def resolve(self, surface: str, context: str = "") -> Resolution:
        key = (surface, context)
        if key not in self._memo:
            self._memo[key] = self._resolve(surface, context)
        return self._memo[key]

    def _resolve(self, surface: str, context: str) -> Resolution:
        def done(app: str | None, method: Method) -> Resolution:
            return Resolution(AppId(app) if app else None, surface, method, CONF[method])

        s = (surface or "").strip()
        if not s or s.casefold() in self._teams:
            return done(None, "unresolved")
        if (hit := self._lookup(s)):
            return done(*hit)
        for step in self._strip_chain(s):
            if (hit := self._lookup(step)):
                return done(hit[0], "pattern")
            if (app := self._lookup_loose(step)):
                return done(app, "pattern")
        core = list(self._strip_chain(s))[-1]
        ranked = self._fuzzy(core)
        by_ts = sorted(ranked, key=lambda x: -x[1])
        if by_ts and by_ts[0][1] >= FUZZY_MIN and (
                len(by_ts) == 1 or by_ts[0][1] - by_ts[1][1] >= FUZZY_GAP):
            return done(by_ts[0][0], "fuzzy")
        if self.llm is not None:
            if (app := self._ask(s, core, context, ranked)):
                return done(app, "llm")
        return done(None, "unresolved")

    def candidates(self, core: str, context: str,
                   ranked: list[tuple[str, float, float]]) -> list[str]:
        out: list[str] = []
        pools = [sorted(self._acronym.get(_squash(core), ())),
                 [i for i in self.ids if re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])",
                                                   context, re.I)],
                 [a for a, _, _ in ranked]]
        for pool in pools:
            for a in pool:
                if a not in out and len(out) < N_CANDIDATES:
                    out.append(a)
        return out

    def _ask(self, surface: str, core: str, context: str, ranked) -> str | None:
        cands = self.candidates(core, context, ranked)
        if not cands:
            return None
        user = json.dumps({"surface": surface, "context": context, "candidates": [*cands, "none"]})
        try:
            pick = self.llm.structured(SYSTEM, user, Pick)
        except Exception:  # contract error or transport failure: stay unresolved
            return None
        return pick.choice if pick.choice in cands else None


def llm_user_prompt(resolver: Resolver, surface: str, context: str = "") -> str:
    """The exact user prompt `resolve` would send (tests key a FakeClient with it)."""
    core = list(resolver._strip_chain(surface.strip()))[-1]
    cands = resolver.candidates(core, context, resolver._fuzzy(core))
    return json.dumps({"surface": surface.strip(), "context": context,
                       "candidates": [*cands, "none"]})


# -- applying the resolver to the processed tables --------------------------
_TAG_KEYS = ("nf.app", "app", "service")
TABLES_WITH_APP = ("metrics_atlas", "metrics_prom", "logs", "spans", "events", "alerts",
                   "pages", "incidents", "catalog")


def catalog_from_table(df: pl.DataFrame) -> tuple[Application, ...]:
    apps = []
    for f in df.sort("raw_id")["fields"]:
        d = json.loads(f)
        if "id" in d:
            apps.append(Application(
                id=d["id"], aliases=tuple(d.get("aliases") or ()),
                owner_team=d.get("owner_team") or "", tier=d.get("tier") or 0,
                regions=tuple(d.get("regions") or ()), kind=d.get("kind") or "service"))
    return tuple(apps)


def _effective_surface(table: str) -> pl.Expr:
    """surface_app, else the app tag/label for metric tables (their parsers leave it null)."""
    col = {"metrics_atlas": "tags", "metrics_prom": "labels"}.get(table)
    if col is None:
        return pl.col("surface_app")
    return pl.coalesce(pl.col("surface_app"),
                       *(pl.col(col).str.json_path_match(f'$["{k}"]') for k in _TAG_KEYS))


def resolve_tables(proc: Path, resolver: Resolver) -> dict[str, dict[str, int]]:
    """Add app_uri, app_method, app_confidence to each table; returns rows per method."""
    breakdown: dict[str, dict[str, int]] = {}
    for table in TABLES_WITH_APP:
        path = proc / f"{table}.parquet"
        lf = pl.scan_parquet(path).with_columns(_surf=_effective_surface(table))
        surfaces = lf.select("_surf").unique().collect()["_surf"].drop_nulls().to_list()
        res = [resolver.resolve(s) for s in sorted(surfaces)]
        m = pl.DataFrame({"_surf": [r.surface for r in res],
                          "app_uri": [r.app_uri for r in res],
                          "app_method": [r.method for r in res],
                          "app_confidence": [r.confidence for r in res]},
                         schema={"_surf": pl.String, "app_uri": pl.String,
                                 "app_method": pl.String, "app_confidence": pl.Float64})
        out = (lf.join(m.lazy(), on="_surf", how="left")
               .with_columns(app_method=pl.col("app_method").fill_null("unresolved"),
                             app_confidence=pl.col("app_confidence").fill_null(0.0))
               .drop("_surf"))
        if table == "logs":
            # log4j / nginx lines carry no app: surface_app is the log directory (Task 6),
            # so the resolution is of the directory name, not of a name in the record.
            out = out.with_columns(app_from_dir=pl.col("log_format").is_in(["log4j", "nginx"])
                                   & (pl.col("surface_app") == pl.col("app_dir")))
        tmp = path.with_suffix(".tmp")
        out.sink_parquet(tmp, compression="zstd")
        tmp.replace(path)
        counts = pl.scan_parquet(path).group_by("app_method").len().collect()
        breakdown[table] = dict(zip(counts["app_method"], counts["len"], strict=True))
    return breakdown
