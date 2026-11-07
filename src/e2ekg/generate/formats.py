"""FORMATTERS[fmt](fields) -> bytes for every raw format (Ruling R12).

Timestamp convention. A timestamp value inside `fields` is one of:
  * an aware `datetime`: rendered in the fmt's canonical style (DEFAULT_STYLE);
  * a `Stamp(utc, style)`: rendered in that explicit style. Structured
    injectors such as log.tz_mix swap a datetime for a Stamp, and shifters
    such as log.host_skew move `Stamp.utc`, so the instant stays machine-
    readable until serialization;
  * a `str` or `int`: an already-rendered value, emitted verbatim.

Payloads never end with b"\\n" and never contain one, except in
MULTILINE_FMTS where the record is legitimately several lines (Task 6 splits
those by content: log4j header regex, `commit <sha>` lines, `---` lines).
Fixed-layout formatters reject unknown keys, so no field is silently dropped.
"""
import csv
import io
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from e2ekg.timeutil import PT

MULTILINE_FMTS = frozenset({"log4j", "gitlog", "catalog_yaml"})
JSON_FMTS = frozenset({"json", "atlas_jsonl", "otlp_jsonl", "zipkin_jsonl", "spinnaker_jsonl",
                       "asg_jsonl", "radar_jsonl", "pagerduty_jsonl", "incident_jsonl",
                       "slack_jsonl"})
AB_COLUMNS = ("ts", "experiment", "cell", "old_allocation_pct", "new_allocation_pct",
              "population", "app", "actor", "allocation_id")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Stamp:
    utc: datetime
    style: str


def _micros(dt: datetime) -> int:
    return (dt - _EPOCH) // timedelta(microseconds=1)


def render_ts(dt: datetime, style: str) -> str | int:
    """Styles ending in _pt render America/Los_Angeles wall clock; log4j_pt has
    no offset at all (the classic naive-local log line)."""
    d = dt.astimezone(PT if style.endswith("_pt") else UTC)
    ms = f"{d.microsecond // 1000:03d}"
    us = _micros(dt)
    match style:
        case "iso_z":
            return f"{d:%Y-%m-%dT%H:%M:%S}.{ms}Z"
        case "iso_pt":
            return d.isoformat(timespec="milliseconds")
        case "epoch_ms":
            return us // 1000
        case "epoch_us":
            return us
        case "epoch_ns_str":
            return str(us * 1000)
        case "slack_ts":
            return f"{us // 1_000_000}.{us % 1_000_000:06d}"
        case "log4j_utc" | "log4j_pt":
            return f"{d:%Y-%m-%d %H:%M:%S},{ms}"
        case "nginx_utc" | "nginx_pt":
            return f"{d.day:02d}/{_MON[d.month - 1]}/{d.year}:{d:%H:%M:%S} {d:%z}"
        case "git_utc" | "git_pt":
            return f"{_DOW[d.weekday()]} {_MON[d.month - 1]} {d.day} {d:%H:%M:%S} {d.year} {d:%z}"
    raise ValueError(f"unknown timestamp style {style!r}")


def _ts(v: Any, style: str) -> Any:
    if isinstance(v, Stamp):
        return render_ts(v.utc, v.style)
    if isinstance(v, datetime):
        return render_ts(v, style)
    return v


def _walk(obj: Any, style: str) -> Any:
    if isinstance(obj, dict):
        return {k: _walk(v, style) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_walk(v, style) for v in obj]
    return _ts(obj, style)


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _jsonl(style: str) -> Callable[[dict], bytes]:
    return lambda f: _dumps(_walk(f, style))


def _one_line(s: Any) -> str:
    return str(s).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _check(f: dict, allowed: set[str]) -> None:
    extra = set(f) - allowed
    if extra:
        raise ValueError(f"unknown fields {sorted(extra)}")


def _lf_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    s = "" if v is None else str(_ts(v, "iso_z"))
    if s and not any(c in s for c in ' "=\\\n\r\t'):
        return s
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace(
        "\r", "\\r").replace("\t", "\\t")
    return f'"{esc}"'


def _logfmt(f: dict) -> bytes:
    return " ".join(f"{k}={_lf_value(v)}" for k, v in f.items()).encode()


_LOG4J = {"ts", "level", "thread", "logger", "msg", "stack"}


def _log4j(f: dict) -> bytes:
    _check(f, _LOG4J)
    head = (f"{_ts(f['ts'], 'log4j_utc')} {f['level']} [{f['thread']}] {f['logger']} - "
            f"{_one_line(f['msg'])}")
    return "\n".join([head, *(_one_line(s) for s in f.get("stack") or ())]).encode()


_NGINX = {"remote_addr", "remote_user", "ts", "method", "path", "protocol", "status", "bytes",
          "referer", "user_agent", "request_time"}


def _nq(v: Any) -> str:
    return _one_line(v).replace('"', "\\x22")


def _nginx(f: dict) -> bytes:
    _check(f, _NGINX)
    line = (f'{f["remote_addr"]} - {_nq(f["remote_user"])} [{_ts(f["ts"], "nginx_utc")}] '
            f'"{f["method"]} {_nq(f["path"])} {f["protocol"]}" {f["status"]} {f["bytes"]} '
            f'"{_nq(f["referer"])}" "{_nq(f["user_agent"])}"')
    if "request_time" in f:
        line += f" rt={f['request_time']:.3f}"
    return line.encode()


def _prom_num(v: Any) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "+Inf" if v > 0 else "-Inf"
        return repr(v)
    return str(v)


def _prom_label(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _prom(f: dict) -> bytes:
    kind = f["kind"]
    if kind == "help":
        _check(f, {"kind", "name", "text"})
        return f"# HELP {f['name']} {_one_line(f['text'])}".encode()
    if kind == "type":
        _check(f, {"kind", "name", "type"})
        return f"# TYPE {f['name']} {f['type']}".encode()
    _check(f, {"kind", "name", "labels", "value", "t"})
    labels = f.get("labels") or {}
    lab = ",".join(f'{k}="{_prom_label(v)}"' for k, v in labels.items())
    lab = f"{{{lab}}}" if labels else ""
    out = f"{f['name']}{lab} {_prom_num(f['value'])}"
    if f.get("t") is not None:
        out += f" {_ts(f['t'], 'epoch_ms')}"
    return out.encode()


def _any_value(v: Any) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_any_value(x) for x in v]}}
    return {"stringValue": str(_ts(v, "iso_z"))}


def _kv(attrs: dict) -> list[dict]:
    return [{"key": k, "value": _any_value(v)} for k, v in attrs.items()]


def _otlp_span(s: dict) -> dict:
    out = {"traceId": s["traceId"], "spanId": s["spanId"],
           "parentSpanId": s.get("parentSpanId", ""), "name": s["name"], "kind": s["kind"],
           "startTimeUnixNano": _ts(s["start"], "epoch_ns_str"),
           "endTimeUnixNano": _ts(s["end"], "epoch_ns_str"),
           "attributes": _kv(s.get("attributes", {})), "status": s.get("status", {"code": 0})}
    if "events" in s:
        out["events"] = [{"timeUnixNano": _ts(e["time"], "epoch_ns_str"), "name": e["name"],
                          "attributes": _kv(e.get("attributes", {}))} for e in s["events"]]
    known = {"traceId", "spanId", "parentSpanId", "name", "kind", "start", "end", "attributes",
             "status", "events"}
    out |= {k: _walk(v, "epoch_ns_str") for k, v in s.items() if k not in known}
    return out


def _otlp(f: dict) -> bytes:
    """One OTLP/JSON ResourceSpans object per line (Ruling R13)."""
    _check(f, {"resource", "scope", "spans"})
    return _dumps({"resource": {"attributes": _kv(f["resource"])},
                   "scopeSpans": [{"scope": {"name": f.get("scope", "e2ekg")},
                                   "spans": [_otlp_span(s) for s in f["spans"]]}]})


def _properties(f: dict) -> bytes:
    rest = {k: v for k, v in f.items() if k != "ts"}
    return (f"{_ts(f['ts'], 'iso_z')} INFO  [archaius-poller] PropertyChanged ".encode()
            + _logfmt(rest))


def _ab_csv(f: dict) -> bytes:
    if f.get("header"):
        _check(f, {"header"})
        return ",".join(AB_COLUMNS).encode()
    _check(f, set(AB_COLUMNS))
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(
        [_one_line(_ts(f[c], "iso_z")) if c in f else "" for c in AB_COLUMNS])
    return buf.getvalue().encode()


def _gitlog(f: dict) -> bytes:
    _check(f, {"sha", "author", "date", "message"})
    lines = [f"commit {f['sha']}", f"Author: {f['author']}", f"Date:   {_ts(f['date'], 'git_utc')}",
             ""]
    # split("\n") only: splitlines() would collapse \r\n, drop a trailing
    # newline and turn \x0b, \x1c, ... into real newlines. Every message line,
    # blank ones too, is indented, so the payload never ends with "\n".
    lines += [f"    {ln}" for ln in str(f["message"]).replace("\r", "\\r").split("\n")]
    return "\n".join(lines).encode()


def _catalog_yaml(f: dict) -> bytes:
    body = yaml.safe_dump(f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return ("---\n" + body.rstrip("\n")).encode()


FORMATTERS: dict[str, Callable[[dict], bytes]] = {
    "json": _jsonl("iso_z"),
    "logfmt": _logfmt,
    "log4j": _log4j,
    "nginx": _nginx,
    "atlas_jsonl": _jsonl("epoch_ms"),
    "prom": _prom,
    "otlp_jsonl": _otlp,
    "zipkin_jsonl": _jsonl("epoch_us"),
    "spinnaker_jsonl": _jsonl("epoch_ms"),
    "properties_log": _properties,
    "asg_jsonl": _jsonl("iso_z"),
    "ab_csv": _ab_csv,
    "gitlog": _gitlog,
    "radar_jsonl": _jsonl("iso_z"),
    "pagerduty_jsonl": _jsonl("iso_z"),
    "incident_jsonl": _jsonl("iso_z"),
    "slack_jsonl": _jsonl("slack_ts"),
    "catalog_yaml": _catalog_yaml,
}
