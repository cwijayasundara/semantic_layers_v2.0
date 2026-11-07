"""Scoring an RCAReport against a scenario's answer key.

The answer key names changes by scenario id (`chg-cfg-0001`); every artifact an agent or the
pipeline sees names them by an opaque external id (R22). `answers["external_ids"]` maps one to
the other, and this module is the only place that does.

  * A report names a root as `https://kg.local/chg/<kind>/<scenario>/<external id>` (graph
    arms) or as the bare external id (a raw-files agent has no URIs). Anything else names no
    change.
  * top1 / top3: the verdict is root_cause_found and the true root is the first / among the
    first three hypotheses. In the control (no root) both mean the report abstained; a run
    that failed (FailedRCA) abstained from nothing and scores a miss there too.
  * trap: the report found a root and its first is one of the answer key's red herrings.
  * control_false_positive: the scenario has no root and the verdict is root_cause_found.
  * evidence_precision: the share of the first hypothesis's distinct evidence items that
    reach a record of the answer key's evidence. What "reach" means is the caller's
    `cites_evidence`; `evidence_checker` builds it for graph URIs, raw record ids and
    `path:line` citations of raw files."""
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from e2ekg.agent.autorca import FailedRCA
from e2ekg.agent.report import RCAReport

_CHANGE_URI = re.compile(r"https://kg\.local/chg/[a-z_]+/[^/]+/(?P<ext>.+)")
_CITATION = re.compile(r"^(?:/?raw/)?(?P<path>[^:\s]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")


@dataclass(frozen=True)
class Score:
    top1: bool
    top3: bool
    evidence_precision: float
    trap: bool
    control_false_positive: bool


def scenario_change(text: str, answers: dict) -> str | None:
    """The scenario change id (`chg-...`) a report's root names, or None."""
    external = answers["external_ids"]
    text = text.strip()
    m = _CHANGE_URI.fullmatch(text)
    if m:
        return external.get(m["ext"])
    if text in external:
        return external[text]
    named = {external[e] for e in external if e in text}
    return named.pop() if len(named) == 1 else None


def roots(report: RCAReport, answers: dict) -> list[str | None]:
    return [scenario_change(h.root_cause_uri, answers) for h in report.hypotheses]


def true_root_rank(report: RCAReport, answers: dict) -> int | None:
    """1-based position of the true root among the report's hypotheses, None if absent (or
    the scenario has no root)."""
    key = answers["answer_key"]["root_cause_change_id"]
    found = roots(report, answers)
    return found.index(key) + 1 if key in found else None


def score(report: RCAReport, answers: dict,
          cites_evidence: Callable[[str], bool] | None = None) -> Score:
    key = answers["answer_key"]["root_cause_change_id"]
    herrings = set(answers["answer_key"]["red_herring_change_ids"])
    found = report.verdict == "root_cause_found"
    named = roots(report, answers)
    if key is None:
        top1 = top3 = not found and not isinstance(report, FailedRCA)
    else:
        top1 = found and named[:1] == [key]
        top3 = found and key in named[:3]
    if cites_evidence is None:
        known = set(answers["evidence_raw_ids"])
        cites_evidence = known.__contains__
    cited = list(dict.fromkeys(report.hypotheses[0].evidence)) if report.hypotheses else []
    precision = sum(map(cites_evidence, cited)) / len(cited) if cited else 0.0
    return Score(top1=top1, top3=top3, evidence_precision=precision,
                 trap=found and bool(named) and named[0] in herrings,
                 control_false_positive=key is None and found)


def evidence_checker(answers: dict, *, store=None, proc: Path | None = None,
                     raw: Path | None = None, view: dict | None = None
                     ) -> Callable[[str], bool]:
    """Does one cited evidence item reach a record of the answer key's evidence?
      * a raw record id of the key: yes;
      * a kg.local URI, with `store` and `proc` (the as-of store and its processed tables):
        yes if it, or what it derives from or cites, has a raw selector or raw id of the key;
      * `path:line` or `path:a-b`, with `raw` (the raw tree the agent read, or its as-of view)
        and `view` (the view's segment map; None when `raw` is the original tree): yes if
        those lines overlap a record of the key, by byte range in the original file."""
    wanted = set(answers["evidence_raw_ids"])
    spans: dict[str, list[tuple[int, int]]] = {}
    for rid in wanted:
        _, path, offset, length, _ = rid.split("|")
        spans.setdefault(path, []).append((int(offset), int(offset) + int(length)))
    for v in spans.values():
        v.sort()
    memo: dict[str, bool] = {}
    lines: dict[str, np.ndarray] = {}

    def line_starts(path: str) -> np.ndarray:
        if path not in lines:
            data = np.fromfile(raw / path, dtype=np.uint8)
            lines[path] = np.concatenate(([0], np.flatnonzero(data == 10) + 1))
        return lines[path]

    def overlaps(path: str, lo: int, hi: int) -> bool:
        return any(a < hi and lo < b for a, b in spans.get(path, ()))

    def cited_lines(path: str, first: int, last: int) -> bool:
        if raw is None or not (raw / path).is_file() or (view is not None and path not in view):
            return False
        starts = line_starts(path)
        if not 1 <= first <= last <= len(starts):
            return False
        lo = int(starts[first - 1])
        hi = int(starts[last]) if last < len(starts) else (raw / path).stat().st_size
        if view is None:
            return overlaps(path, lo, hi)
        return any(overlaps(path, o0 + (max(lo, v0) - v0), o0 + (min(hi, v0 + n) - v0))
                   for v0, o0, n in view[path] if max(lo, v0) < min(hi, v0 + n))

    def graph(uri: str) -> bool:
        from e2ekg.preprocess.selector import selected_raw_ids
        vals = [r["v"] for r in store.query(
            "PREFIX ops: <https://kg.local/ops#> PREFIX prov: <http://www.w3.org/ns/prov#> "
            f"SELECT DISTINCT ?v WHERE {{ <{uri}> (prov:wasDerivedFrom|ops:cites)* ?x . "
            "?x ops:rawSelector ?v }")]
        ids = {v for v in vals if not v.startswith("{")}
        ids |= selected_raw_ids(proc, [v for v in vals if v.startswith("{")])
        return bool(ids & wanted)

    def check(item: str) -> bool:
        item = item.strip()
        if item not in memo:
            m = _CITATION.match(item)
            if item in wanted:
                memo[item] = True
            elif item.startswith("https://kg.local/") and store is not None:
                memo[item] = graph(item)
            elif m and not item.startswith("http"):
                a = int(m["a"])
                memo[item] = cited_lines(m["path"], a, int(m["b"] or a))
            else:
                memo[item] = False
        return memo[item]
    return check


def load_answers(root: Path, scenario: str) -> dict:
    return json.loads((Path(root) / "raw" / "_answers" / f"{scenario}.json").read_text())
