"""Zipkin v2 JSON: one span object per line (timestamps in epoch µs)."""
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, load_json, row, split_known, split_lines

_KEYS = frozenset({"traceId", "id", "parentId", "name", "kind", "timestamp", "duration",
                   "localEndpoint", "remoteEndpoint", "tags", "annotations", "shared", "debug"})


class ZipkinParser:
    name = "zipkin"
    source = "traces"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        d = load_json(payload)
        if not isinstance(d, dict) or "traceId" not in d or "id" not in d:
            raise Unparseable("zipkin: not a v2 span")
        fields, residual = split_known(d, _KEYS)
        tags = d.get("tags") or {}
        service = (d.get("localEndpoint") or {}).get("serviceName")
        code = tags.get("otel.status_code")
        status = "ERROR" if "error" in tags else code if code else "UNSET"
        ts = d.get("timestamp")
        return row(None if ts is None else str(ts), service, fields, residual,
                   span_index=0, span_format="zipkin", host=tags.get("host.name"),
                   trace_id=d["traceId"], span_id=d["id"], parent_span_id=d.get("parentId"),
                   name=d.get("name"), span_kind=d.get("kind") or tags.get("otel.span_kind"),
                   duration_us=d.get("duration"), status_code=status, attributes=tags)


ZIPKIN = ZipkinParser()
