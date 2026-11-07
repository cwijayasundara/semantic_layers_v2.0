"""Archaius fast-property change log:
`<iso ts> <LEVEL>  [<thread>] PropertyChanged k=v ...` (the tail is logfmt)."""
import re
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, decode, row, split_known, split_lines
from e2ekg.preprocess.parsers.logfmt import parse_pairs

_LINE = re.compile(
    r"(?P<ts>\S+) (?P<level>[A-Z]+) +\[(?P<thread>[^\]]*)\] (?P<event>\w+) ?(?P<rest>.*)")
_KEYS = frozenset({"app", "property", "old", "new", "user", "source", "id"})


class PropertiesParser:
    name = "properties"
    source = "events"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        m = _LINE.fullmatch(decode(payload))
        if not m:
            raise Unparseable("properties: not a PropertyChanged line")
        pairs = parse_pairs(m["rest"])
        fields, residual = split_known(pairs, _KEYS)
        head = {k: m[k] for k in ("ts", "level", "thread", "event")}
        return row(m["ts"], pairs.get("app") or None, {**head, **fields}, residual,
                   event_kind="properties", row_kind="record", external_id=pairs.get("id"),
                   event_type=m["event"], status=None)


PROPERTIES = PropertiesParser()
