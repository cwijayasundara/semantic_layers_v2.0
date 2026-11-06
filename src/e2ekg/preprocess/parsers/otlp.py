"""OTLP/JSON: one ResourceSpans object per line (R13). The line is one raw
record; each span inside it becomes its own row (`rows`), all sharing the
line's raw id and numbered by `span_index`."""
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, load_json, row, split_known, split_lines

SPAN_KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER",
              5: "CONSUMER"}
STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}
_SPAN_KEYS = frozenset({"traceId", "spanId", "parentSpanId", "name", "kind", "startTimeUnixNano",
                        "endTimeUnixNano", "attributes", "status", "events", "traceState",
                        "droppedAttributesCount", "droppedEventsCount", "links", "flags"})


def any_value(v: dict):
    """OTLP AnyValue -> plain Python (intValue is a decimal string on the wire)."""
    if not isinstance(v, dict) or len(v) != 1:
        return v
    (k, x), = v.items()
    if k == "intValue":
        return int(x)
    if k == "arrayValue":
        return [any_value(e) for e in (x or {}).get("values", [])]
    if k == "kvlistValue":
        return attrs((x or {}).get("values", []))
    return x


def attrs(kvs) -> dict:
    if not isinstance(kvs, list):
        raise Unparseable("otlp: attributes must be a KeyValue list")
    return {kv["key"]: any_value(kv.get("value")) for kv in kvs if isinstance(kv, dict)}


class OtlpParser:
    name = "otlp"
    source = "traces"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        d = load_json(payload)
        try:
            resource = attrs(d["resource"].get("attributes", []))
            scopes = d["scopeSpans"]
        except (KeyError, TypeError, AttributeError) as e:
            raise Unparseable(f"otlp: not a ResourceSpans object ({e})") from e
        # Envelope keys beyond resource.attributes / scope.name / spans
        # (schemaUrl, scope.version, ...) go to every span's residual.
        _, envelope = split_known(d, frozenset({"resource", "scopeSpans"}))
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
                r["_residual"] = extra | r["_residual"]
                rows.append(r)
        return {**row(None, service, {"resource": resource}, envelope), "rows": rows}

    @staticmethod
    def _span(s: dict, resource: dict, scope, service, host, index: int) -> dict:
        try:
            start, end = int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"])
        except (KeyError, TypeError, ValueError) as e:
            raise Unparseable(f"otlp: bad span times ({e})") from e
        fields, residual = split_known(s, _SPAN_KEYS)
        a = attrs(s.get("attributes", []))
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
