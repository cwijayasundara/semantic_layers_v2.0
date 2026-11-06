"""logfmt: `key=value` pairs separated by spaces; a value with spaces, quotes,
`=` or control characters is double-quoted with backslash escapes."""
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, decode, row, split_known, split_lines

_ESC = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def parse_pairs(text: str) -> dict[str, str]:
    """Strict logfmt: raises Unparseable on a bare word or an unterminated
    quote, so a foreign line is never half-read into fields."""
    out: dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        if text[i] == " ":
            i += 1
            continue
        eq = text.find("=", i)
        sp = text.find(" ", i)
        if eq < 0 or (0 <= sp < eq):
            raise Unparseable(f"logfmt: bare token at {i}")
        key = text[i:eq]
        i = eq + 1
        if i < n and text[i] == '"':
            buf, i = [], i + 1
            while True:
                if i >= n:
                    raise Unparseable("logfmt: unterminated quote")
                c = text[i]
                if c == "\\" and i + 1 < n:
                    buf.append(_ESC.get(text[i + 1], "\\" + text[i + 1]))
                    i += 2
                elif c == '"':
                    i += 1
                    break
                else:
                    buf.append(c)
                    i += 1
            val = "".join(buf)
        else:
            j = text.find(" ", i)
            j = n if j < 0 else j
            val, i = text[i:j], j
        if not key:
            raise Unparseable("logfmt: empty key")
        out[key] = val
    return out


class LogfmtParser:
    name = "logfmt"
    source = "logs"
    known = frozenset({"ts", "level", "app", "caller", "msg", "request_id", "error"})

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        d = parse_pairs(decode(payload))
        if "ts" not in d:
            raise Unparseable("logfmt: no ts")
        fields, residual = split_known(d, self.known)
        level = d.get("level")
        return row(d["ts"], d.get("app") or None, fields, residual,
                   level=level.upper() if level else None, logger=d.get("caller"), thread=None,
                   msg=d.get("msg"), request_id=d.get("request_id"))


LOGFMT = LogfmtParser()
