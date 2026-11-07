"""log.* injectors. All structured except log.truncate and log.encoding,
which corrupt serialized bytes."""
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise

from e2ekg.generate.formats import Stamp
from e2ekg.generate.mess._base import (
    base_keys,
    by_path,
    injector,
    local_index,
    pick,
    set_fields,
    shift_ts,
    with_inserts,
)

LOG_FMTS = frozenset({"json", "logfmt", "log4j", "nginx"})
TZ_RATE, TRUNC_RATE, DUP_RATE, INTERLEAVE_RATE, PII_RATE = 0.3, 0.001, 0.002, 0.001, 0.001
HOST_SKEW = timedelta(seconds=90)
HOST_SKEW_APP_DIR = "logs/title-batch-loader/"
# Per format, the local-time rendering a misconfigured host uses.
TZ_STYLE = {"json": "epoch_ms", "logfmt": "iso_pt", "log4j": "log4j_pt", "nginx": "nginx_pt"}
_FIRST = ("jane", "omar", "li", "sofia", "raj", "maria", "kenji", "ada")
_LAST = ("doe", "haddad", "wei", "rossi", "patel", "garcia", "sato", "obi")
_DOMAINS = ("gmail.com", "yahoo.com", "example.org")


def _is_log(self, r) -> bool:
    return self.eligible(r, LOG_FMTS)


@injector("log.tz_mix", ("logs",))
def tz_mix(self, recs, rng, scenario):
    """Some hosts render timestamps in PT (log4j_pt has no offset) or epoch ms."""
    files = by_path(recs, lambda r: _is_log(self, r))
    paths = base_keys(recs, files)
    out, inj = list(recs), []
    for k in pick(rng, len(paths), TZ_RATE):
        p = paths[k]
        style = TZ_STYLE[recs[files[p][0]].fmt]
        n = 0
        for i in files[p]:
            ts = out[i].fields.get("ts")
            if isinstance(ts, datetime):
                out[i] = set_fields(out[i], ts=Stamp(ts, style))
                n += 1
        inj.append(self.record(p, style=style, count=n))
    return out, inj


@injector("log.host_skew", ("logs",))
def host_skew(self, recs, rng, scenario):
    files = by_path(recs, lambda r: _is_log(self, r) and r.rel_path.startswith(HOST_SKEW_APP_DIR))
    paths = sorted(base_keys(recs, files))
    if not paths:
        return list(recs), []
    p = paths[int(rng.integers(len(paths)))]
    out = list(recs)
    for i in files[p]:
        out[i] = set_fields(out[i], ts=shift_ts(out[i].fields["ts"], HOST_SKEW))
    return out, [self.record(p, host=recs[files[p][0]].host, skew_s=90, count=len(files[p]))]


@injector("log.truncate", ("logs",), phase="bytes")
def truncate(self, recs, rng, scenario):
    """Cut a JSON line mid-object. The removed bytes are lost from the raw
    file by design and kept only in the detail."""
    cands = [i for i, r in enumerate(recs) if self.eligible(r, {"json"}) and len(r.payload) > 2]
    out, inj, loc = list(recs), [], local_index(recs)
    for k in pick(rng, len(cands), TRUNC_RATE):
        i = cands[k]
        pl = recs[i].payload
        cut = int(rng.integers(1, len(pl) - 1))
        out[i] = replace(recs[i], payload=pl[:cut])
        inj.append(self.record(recs[i].rel_path, idx=loc[i], at_utc=recs[i].at_utc, cut_at=cut,
                               original_len=len(pl), removed_hex=pl[cut:].hex()))
    return out, inj


@injector("log.duplicate", ("logs",))
def duplicate(self, recs, rng, scenario):
    """At-least-once shipping: an identical copy (tags included) right after."""
    cands = [i for i, r in enumerate(recs) if _is_log(self, r)]
    loc = local_index(recs)
    after, inj = {}, []
    for k in pick(rng, len(cands), DUP_RATE):
        i = cands[k]
        after[i] = [recs[i]]
        inj.append(self.record(recs[i].rel_path, idx=loc[i], at_utc=recs[i].at_utc))
    return with_inserts(recs, after), inj


@injector("log.interleave", ("logs",))
def interleave(self, recs, rng, scenario):
    """Two buffered writers flush out of order: adjacent records of one file
    swap places. Records stay contiguous byte spans (Ruling R13)."""
    pairs = []
    for idxs in by_path(recs, lambda r: _is_log(self, r)).values():
        pairs += list(pairwise(idxs))
    out, inj, loc = list(recs), [], local_index(recs)
    used: set[int] = set()
    for k in pick(rng, len(pairs), INTERLEAVE_RATE):
        a, b = pairs[k]
        if a in used or b in used:
            continue
        used |= {a, b}
        out[a], out[b] = recs[b], recs[a]
        inj.append(self.record(recs[a].rel_path, idx_a=loc[a], idx_b=loc[b],
                               at_utc_a=recs[a].at_utc, at_utc_b=recs[b].at_utc))
    return out, inj


def _choice(rng, xs):
    return xs[int(rng.integers(len(xs)))]


def _email(rng) -> str:
    return (f"{_choice(rng, _FIRST)}.{_choice(rng, _LAST)}{int(rng.integers(10, 99))}"
            f"@{_choice(rng, _DOMAINS)}")


@injector("log.pii", ("logs",))
def pii(self, recs, rng, scenario):
    """A member email leaks into a log line (added, nothing removed)."""
    cands = [i for i, r in enumerate(recs) if _is_log(self, r)
             and (r.fmt != "nginx" or r.fields.get("remote_user") == "-")]
    out, inj, loc = list(recs), [], local_index(recs)
    for k in pick(rng, len(cands), PII_RATE):
        i = cands[k]
        r, email = recs[i], _email(rng)
        match r.fmt:
            case "json" | "logfmt":
                field, out[i] = "user_email", set_fields(r, user_email=email)
            case "log4j":
                field, out[i] = "msg", set_fields(r, msg=f"{r.fields['msg']} user={email}")
            case _:
                field, out[i] = "remote_user", set_fields(r, remote_user=email)
        inj.append(self.record(r.rel_path, idx=loc[i], at_utc=r.at_utc, field=field, value=email))
    return out, inj


def latin1_undecodable(pl: bytes) -> bytes | None:
    """The latin-1 re-encoding of a UTF-8 line, but only if the result really
    fails UTF-8 decoding (e.g. "Ã©" re-encodes to C3 A9, which is valid "é")."""
    if pl.isascii():
        return None
    try:
        out = pl.decode("utf-8").encode("latin-1")
        out.decode("utf-8")
    except UnicodeEncodeError:
        return None
    except UnicodeDecodeError:
        return out
    return None


@injector("log.encoding", ("logs",), phase="bytes")
def encoding(self, recs, rng, scenario):
    """One line per non-ASCII log file is written latin-1 instead of UTF-8.
    Only lines whose latin-1 bytes are undecodable as UTF-8 qualify (the
    others are passed over for another line of the file), and truncated JSON
    lines are skipped so each corruption stays separable."""
    enc: dict[int, bytes] = {}
    for i, r in enumerate(recs):
        if _is_log(self, r) and (r.fmt != "json" or r.payload.endswith(b"}")):
            b = latin1_undecodable(r.payload)
            if b is not None:
                enc[i] = b
    files: dict[str, list[int]] = {}
    for i in enc:
        files.setdefault(recs[i].rel_path, []).append(i)
    out, inj, loc = list(recs), [], local_index(recs)
    for p, idxs in files.items():
        i = idxs[int(rng.integers(len(idxs)))]
        try:
            enc[i].decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:  # pragma: no cover - guarded by latin1_undecodable
            raise AssertionError(f"log.encoding produced decodable bytes in {p}")
        out[i] = replace(recs[i], payload=enc[i])
        inj.append(self.record(p, idx=loc[i], at_utc=recs[i].at_utc, from_enc="utf-8",
                               to_enc="latin-1"))
    return out, inj


INJECTORS = (tz_mix, host_skew, truncate, duplicate, interleave, pii, encoding)
