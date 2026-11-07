"""evt.* injectors over change/alert/incident event records."""
from dataclasses import replace

from e2ekg.generate.mess._base import injector, local_index, pick, with_inserts

MISSING_RATE, DUP_WEBHOOK_RATE = 0.10, 0.05
# Structured field -> it vanishes and survives only inside the free text.
MISSABLE = {"spinnaker_jsonl": ("version", "strategy"), "asg_jsonl": ("previous_capacity",),
            "radar_jsonl": ("metric",), "pagerduty_jsonl": ("urgency",),
            "incident_jsonl": ("severity",)}
WEBHOOK_FMTS = frozenset({"spinnaker_jsonl", "radar_jsonl", "pagerduty_jsonl"})


@injector("evt.missing_field", ("events",))
def missing_field(self, recs, rng, scenario):
    """A field is missing from the structured payload; its value appears only
    in the free-text `description` (nothing is lost, it just needs reading)."""
    cands = [i for i, r in enumerate(recs) if self.eligible(r, MISSABLE)
             and any(k in r.fields for k in MISSABLE[r.fmt])]
    out, inj, loc = list(recs), [], local_index(recs)
    for k in pick(rng, len(cands), MISSING_RATE):
        i = cands[k]
        r = recs[i]
        present = [f for f in MISSABLE[r.fmt] if f in r.fields]
        field = present[int(rng.integers(len(present)))]
        value = r.fields[field]
        desc = f"{r.fields.get('description', '')} ({field}: {value})".strip()
        fields = {k2: v for k2, v in r.fields.items() if k2 != field} | {"description": desc}
        out[i] = replace(r, fields=fields)
        inj.append(self.record(r.rel_path, idx=loc[i], at_utc=r.at_utc, field=field, value=value,
                               moved_to="description"))
    return out, inj


@injector("evt.duplicate_webhook", ("events",))
def duplicate_webhook(self, recs, rng, scenario):
    """At-least-once webhook delivery: an identical event right after."""
    cands = [i for i, r in enumerate(recs) if self.eligible(r, WEBHOOK_FMTS)]
    loc = local_index(recs)
    after, inj = {}, []
    for k in pick(rng, len(cands), DUP_WEBHOOK_RATE):
        i = cands[k]
        after[i] = [recs[i]]
        inj.append(self.record(recs[i].rel_path, idx=loc[i], at_utc=recs[i].at_utc))
    return with_inserts(recs, after), inj


INJECTORS = (missing_field, duplicate_webhook)
