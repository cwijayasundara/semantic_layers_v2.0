"""Prometheus text exposition format: `# HELP`, `# TYPE` and sample lines
`name{label="v",...} value [timestamp_ms]`. Every line is its own row, so the
HELP/TYPE comments are kept as rows too."""
import math
import re
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, decode, first_str, row, split_lines

_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_ESC = {"\\": "\\", '"': '"', "n": "\n"}
_SPECIAL = {"NaN": math.nan, "+Inf": math.inf, "-Inf": -math.inf, "Inf": math.inf}


def _labels(text: str, i: int) -> tuple[dict[str, str], int]:
    """Parse `{k="v",...}` starting at text[i] == "{"; returns (labels, index after })."""
    out: dict[str, str] = {}
    i += 1
    while True:
        while i < len(text) and text[i] in " ,":
            i += 1
        if i < len(text) and text[i] == "}":
            return out, i + 1
        m = _NAME.match(text, i)
        if not m or text[m.end():m.end() + 2] != '="':
            raise Unparseable("prom: bad label")
        key, i, buf = m.group(), m.end() + 2, []
        while True:
            if i >= len(text):
                raise Unparseable("prom: unterminated label value")
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                buf.append(_LABEL_ESC.get(text[i + 1], "\\" + text[i + 1]))
                i += 2
            elif c == '"':
                i += 1
                break
            else:
                buf.append(c)
                i += 1
        out[key] = "".join(buf)


def _value(s: str) -> float:
    if s in _SPECIAL:
        return _SPECIAL[s]
    try:
        return float(s)
    except ValueError as e:
        raise Unparseable(f"prom: bad value {s!r}") from e


class PromParser:
    name = "prom"
    source = "prom"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        text = decode(payload)
        none = {"labels": {}, "value": None, "value_raw": None, "metric_type": None,
                "help": None, "instance": None}
        if text.startswith("# HELP ") or text.startswith("# TYPE "):
            kind, rest = text[2:6].lower(), text[7:]
            name, _, tail = rest.partition(" ")
            if not _NAME.fullmatch(name):
                raise Unparseable("prom: bad metric name")
            fields = {"kind": kind, "name": name, ("text" if kind == "help" else "type"): tail}
            cols = none | {"prom_kind": kind, "metric": name,
                           ("help" if kind == "help" else "metric_type"): tail}
            return row(None, None, fields, {}, **cols)
        m = _NAME.match(text)
        if not m:
            raise Unparseable("prom: not a sample line")
        name, i, labels = m.group(), m.end(), {}
        if text[i:i + 1] == "{":
            labels, i = _labels(text, i)
        parts = text[i:].split()
        if len(parts) not in (1, 2) or (len(parts) == 2 and not parts[1].lstrip("-").isdigit()):
            raise Unparseable("prom: expected `value [timestamp]`")
        t = parts[1] if len(parts) == 2 else None
        fields = {"kind": "sample", "name": name, "labels": labels, "value": parts[0]}
        if t is not None:
            fields["t"] = t
        return row(t, first_str(labels, ("app", "service")), fields, {},
                   **none | {"prom_kind": "sample", "metric": name, "labels": labels,
                             "value": _value(parts[0]), "value_raw": parts[0],
                             "instance": labels.get("instance")})


PROM = PromParser()
