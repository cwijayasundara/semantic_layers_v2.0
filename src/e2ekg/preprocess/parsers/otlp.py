"""OTLP/JSON: one ResourceSpans object per line (R13). The line is one raw
record; each span inside it becomes its own row (`rows`), all sharing the
line's raw id and numbered by `span_index`."""
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import (
    Unparseable,
    dup_key,
    load_json,
    row,
    split_known,
    split_lines,
)

SPAN_KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER",
              5: "CONSUMER"}
STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}
_SPAN_KEYS = frozenset({"traceId", "spanId", "parentSpanId", "name", "kind", "startTimeUnixNano",
                        "endTimeUnixNano", "attributes", "status", "events", "traceState",
                        "droppedAttributesCount", "droppedEventsCount", "links", "flags"})


_PLAIN = {"stringValue", "boolValue", "doubleValue"}


def any_value(v: dict):
    """OTLP AnyValue -> plain Python where that loses nothing: string, bool,
    double, int (decimal string on the wire), array and kvlist. Any other
    AnyValue (bytesValue, an unknown tag, a malformed one) stays as the tagged
    dict, so its type is never merged with a plain string."""
    if not isinstance(v, dict) or len(v) != 1:
        return v
    (k, x), = v.items()
    if k in _PLAIN:
        return x
    if k == "intValue":
        try:
            return int(x) if str(int(x)) == str(x) else v  # "007" keeps its text
        except (TypeError, ValueError):
            return v
    if k == "arrayValue" and isinstance(x, dict | type(None)):
        return [any_value(e) for e in (x or {}).get("values", [])]
    if k == "kvlistValue" and isinstance(x, dict | type(None)):
        side: dict = {}
        out = attrs((x or {}).get("values", []), side)
        return out if not side else v  # entries attrs() would set aside: keep the tagged value
    return v


def attrs(kvs, residual: dict | None = None, prefix: str = "attributes") -> dict:
    """KeyValue list -> {key: value}. A repeated key keeps its first value and the
    rest become `key#n`. Entries that are not a KeyValue object with a string
    key go to `residual` as `<prefix>[<index>]`, verbatim (or are dropped only if
    no `residual` was given, which callers must then not do)."""
    if not isinstance(kvs, list):
        raise Unparseable("otlp: attributes must be a KeyValue list")
    out: dict = {}
    for i, kv in enumerate(kvs):
        if not isinstance(kv, dict) or not isinstance(kv.get("key"), str):
            if residual is not None:
                residual[f"{prefix}[{i}]"] = kv
            continue
        if residual is not None and kv.keys() - {"key", "value"}:
            residual[f"{prefix}[{i}]"] = kv  # extra members of the KeyValue stay visible
        k = kv["key"]
        out[dup_key(out, k) if k in out else k] = any_value(kv.get("value"))
    return out


class OtlpParser:
    name = "otlp"
    source = "traces"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        d = load_json(payload)
        try:
            side: dict = {}
            resource = attrs(d["resource"].get("attributes", []), side, "resource.attributes")
            scopes = d["scopeSpans"]
        except (KeyError, TypeError, AttributeError) as e:
            raise Unparseable(f"otlp: not a ResourceSpans object ({e})") from e
        # Envelope keys beyond resource.attributes / scope.name / spans
        # (schemaUrl, scope.version, ...) go to every span's residual.
        # Every envelope key is prefixed so a span key of the same name cannot
        # overwrite it.
        _, top = split_known(d, frozenset({"resource", "scopeSpans"}))
        envelope = {f"resourceSpans.{k}": v for k, v in top.items()} | side
        envelope |= {f"resource.{k}": v for k, v in d["resource"].items() if k != "attributes"}
        service, host = resource.get("service.name"), resource.get("host.name")
        rows = []
        for ss in scopes:
            scope_obj = ss.get("scope") or {}
            scope = scope_obj.get("name")
            extra = envelope | {f"scopeSpans.{k}": v for k, v in ss.items()
                                if k not in ("scope", "spans")}
            extra |= {f"scope.{k}": v for k, v in scope_obj.items() if k != "name"}
            for s in ss.get("spans", []):
                r = self._span(s, resource, scope, service, host, len(rows))
                merged = dict(extra)
                for k, v in r["_residual"].items():  # a colliding span key becomes `key#n`
                    merged[dup_key(merged, k) if k in merged else k] = v
                r["_residual"] = merged
                rows.append(r)
        return {**row(None, service, {"resource": resource}, envelope), "rows": rows}

    @staticmethod
    def _span(s: dict, resource: dict, scope, service, host, index: int) -> dict:
        try:
            start, end = int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"])
        except (KeyError, TypeError, ValueError) as e:
            raise Unparseable(f"otlp: bad span times ({e})") from e
        fields, residual = split_known(s, _SPAN_KEYS)
        a = attrs(s.get("attributes", []), residual, "attributes")
        status = (s.get("status") or {}).get("code", 0)
        return row(str(s["startTimeUnixNano"]), service,
                   {"resource": resource, "scope": scope, **fields}, residual,
                   span_index=index, span_format="otlp", host=host,
                   trace_id=s.get("traceId"), span_id=s.get("spanId"),
                   parent_span_id=s.get("parentSpanId") or None, name=s.get("name"),
                   span_kind=SPAN_KINDS.get(s.get("kind"), str(s.get("kind"))),
                   duration_us=(end - start) // 1000,
                   status_code=STATUS.get(status, str(status)), attributes=a)


OTLP = OtlpParser()
