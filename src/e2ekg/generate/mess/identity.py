"""id.* injectors: rewrite catalog app ids into the surface forms real systems
use. Machine sources rewrite whole streams (one rel_path x app), because a
given emitter uses one surface form consistently; one MessInjection per
stream. Only canonical ids are rewritten, so surface forms never stack."""
from dataclasses import replace

from e2ekg.generate.mess._base import (
    FnInjector,
    app_ref,
    injector,
    local_index,
    pick,
    rename_key,
    set_path,
    word_re,
)

_MACHINE = ("atlas", "prom", "logs", "traces", "events")
_POD_ALPHABET = "bcdfghjklmnpqrstvwxz2456789"  # k8s pod-hash alphabet

ALIAS_RATE, CASING_RATE, SUFFIX_RATE, STALE_RATE, SLACK_RENAME_RATE = 0.15, 0.05, 0.10, 0.05, 0.7
# Spinnaker names the cluster, Radar and PagerDuty the service (spec section 3).
KIND_KEY = {"spinnaker_jsonl": ("cluster", "-prod"), "radar_jsonl": ("service", ""),
            "pagerduty_jsonl": ("service", "")}
SLACK_ONLY = {"title-metadata-svc": "TMS"}  # never in the catalog; Slack remembers it


def _rewrite_streams(self: FnInjector, recs, rng, scenario, rate, surface, want=None):
    apps = {a.id: a for a in scenario.catalog}
    streams: dict[tuple[str, str], list[int]] = {}
    for i, r in enumerate(recs):
        ref = app_ref(r) if self.eligible(r) else None
        if ref and ref[1] in apps and (want is None or want(apps[ref[1]])):
            streams.setdefault((r.rel_path, ref[1]), []).append(i)
    keys = list(streams)
    out, inj = list(recs), []
    for k in pick(rng, len(keys), rate):
        rel, app = keys[k]
        new = surface(apps[app], rng)
        for i in streams[keys[k]]:
            path, _ = app_ref(out[i])
            out[i] = replace(out[i], fields=set_path(out[i].fields, path, new))
        inj.append(self.record(rel, app=app, surface=new, field=".".join(path),
                               count=len(streams[keys[k]])))
    return out, inj


@injector("id.alias_surface", _MACHINE)
def alias_surface(self, recs, rng, scenario):
    return _rewrite_streams(self, recs, rng, scenario, ALIAS_RATE,
                            lambda a, g: a.aliases[int(g.integers(len(a.aliases)))],
                            want=lambda a: bool(a.aliases))


def _casing(a, g):
    return a.id.upper() if g.integers(2) else "-".join(p.capitalize() for p in a.id.split("-"))


@injector("id.casing", ("traces", "events", "logs"))
def casing(self, recs, rng, scenario):
    return _rewrite_streams(self, recs, rng, scenario, CASING_RATE, _casing)


def _suffix(a, g):
    match int(g.integers(3)):
        case 0:
            return f"{a.id}-prod"
        case 1:
            return f"{a.id}-v{int(g.integers(1, 100)):03d}"  # ASG / Spinnaker server group

    def chars(n: int) -> str:
        return "".join(_POD_ALPHABET[int(i)] for i in g.integers(len(_POD_ALPHABET), size=n))
    return f"{a.id}-{chars(10)}-{chars(5)}"  # k8s pod name


@injector("id.suffix", _MACHINE)
def suffix(self, recs, rng, scenario):
    return _rewrite_streams(self, recs, rng, scenario, SUFFIX_RATE, _suffix)


@injector("id.kind_confusion", ("events",))
def kind_confusion(self, recs, rng, scenario):
    """Source convention, so every record of the fmt; one injection per file."""
    out, counts = list(recs), {}
    for i, r in enumerate(recs):
        if self.eligible(r, KIND_KEY) and "app" in r.fields:
            key, sfx = KIND_KEY[r.fmt]
            val = r.fields["app"] + sfx if sfx else None
            out[i] = replace(r, fields=rename_key(r.fields, "app", key, val))
            counts.setdefault(r.rel_path, [r.fmt, key, sfx, 0])[3] += 1
    return out, [self.record(p, fmt=f, from_key="app", to_key=k, value_suffix=s, count=n)
                 for p, (f, k, s, n) in counts.items()]


@injector("id.stale_catalog_alias", ("atlas", "events", "logs"))
def stale_catalog_alias(self, recs, rng, scenario):
    """The emitter moved to a newer name; the catalog alias list is stale."""
    return _rewrite_streams(self, recs, rng, scenario, STALE_RATE,
                            lambda a, g: f"{a.aliases[-1]}-v2", want=lambda a: bool(a.aliases))


@injector("id.slack_only_rename", ("chat",))
def slack_only_rename(self, recs, rng, scenario):
    pats = {app: word_re(app) for app in SLACK_ONLY}
    cands = [i for i, r in enumerate(recs) if self.eligible(r)
             and any(p.search(str(r.fields.get("text", ""))) for p in pats.values())]
    out, inj, loc = list(recs), [], local_index(recs)
    for k in pick(rng, len(cands), SLACK_RENAME_RATE):
        i = cands[k]
        r, before = out[i], out[i].fields["text"]
        after = before
        for app, p in pats.items():
            after = p.sub(SLACK_ONLY[app], after)
        out[i] = replace(r, fields={**r.fields, "text": after})
        inj.append(self.record(r.rel_path, idx=loc[i], at_utc=r.at_utc, before=before, after=after))
    return out, inj


INJECTORS = (alias_surface, casing, suffix, kind_confusion, stale_catalog_alias, slack_only_rename)
