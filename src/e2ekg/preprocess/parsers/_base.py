"""Parser contract, record splitting and strict decoding shared by the parsers.

A parser turns raw bytes into byte spans (`split`) and one span's payload into
a row dict (`parse`). A row dict has:

  ts_raw       the timestamp exactly as written (str) or None
  surface_app  the application name as written (unresolved) or None
  fields       every recognised key, values as written
  _residual    every key the parser does not know, so nothing is dropped
  cols         table-specific promoted (normalised) columns
  rows         optional: several rows from one record (an OTLP ResourceSpans
               line holds many spans). They share the record's raw id.

Failures raise `Truncated`, `Unparseable` or `UnicodeDecodeError` (payloads
are decoded with errors="strict"); the runner turns those into quarantine
rows. Nothing here imports the generator (R6)."""
import json
import re
from collections.abc import Iterator
from typing import Any, Protocol


class Truncated(ValueError):
    """The record is a prefix of a well-formed record (cut short)."""


class Unparseable(ValueError):
    """The record is complete but not in the parser's format."""


class Parser(Protocol):
    """A parser may also define `for_file(data) -> Parser` to bind per-file
    state (the CSV header); the runner calls it when it exists."""

    source: str  # the raw source family whose files this parser reads

    def split(self, data: bytes) -> Iterator[tuple[int, int]]: ...

    def parse(self, payload: bytes) -> dict: ...


def split_lines(data: bytes) -> Iterator[tuple[int, int]]:
    """One span per line, without its b"\\n". A blank line is a 0-length span
    (quarantined, so the ledger still accounts for its newline); a final line
    without a newline is still a span."""
    pos, end = 0, len(data)
    while pos < end:
        nl = data.find(b"\n", pos)
        if nl < 0:
            yield pos, end - pos
            return
        yield pos, nl - pos
        pos = nl + 1


def split_on_header(data: bytes, header: re.Pattern[bytes]) -> Iterator[tuple[int, int]]:
    """A record starts at a line matching `header` and runs to the line before
    the next header line (Carry to T6: never split on continuation lines).
    Lines before the first header are emitted one per line, so they reach
    quarantine instead of vanishing."""
    starts = [m.start() for m in header.finditer(data)]
    first = starts[0] if starts else len(data)
    yield from split_lines(data[:first])
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(data)
        if e > s and data[e - 1:e] == b"\n":
            e -= 1
        yield s, e - s


def decode(payload: bytes) -> str:
    return payload.decode("utf-8", errors="strict")


def _json_incomplete(text: str) -> bool:
    """True when `text` ends inside a string or an open object/array, i.e. it
    is a prefix of some JSON value rather than malformed JSON."""
    depth, in_str, esc = 0, False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                return False
    return in_str or depth > 0


def dup_key(d: dict, key: str) -> str:
    """The next free `key#n` (n >= 2) in `d`."""
    n = 2
    while f"{key}#{n}" in d:
        n += 1
    return f"{key}#{n}"


def put_first(d: dict, key: str, value: Any) -> None:
    """First-wins insert: a repeated key keeps its first value in place and the
    later ones are stored as `key#2`, `key#3`, ... so no value is lost. Parsers
    route the `#n` keys to `_residual`."""
    d[key if key not in d else dup_key(d, key)] = value


def _pairs_first_wins(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        put_first(out, k, v)
    return out


def load_json(payload: bytes) -> Any:
    """Strict UTF-8 + JSON. A cut inside a multi-byte character or inside the
    JSON structure is `Truncated`; other failures are `Unparseable` or the
    original `UnicodeDecodeError` (latin-1 bytes mid-line)."""
    try:
        text = decode(payload)
    except UnicodeDecodeError as e:
        if (e.end == len(payload) and e.reason == "unexpected end of data"
                and payload.lstrip()[:1] in (b"{", b"[")):
            raise Truncated("cut inside a UTF-8 sequence") from e
        raise
    try:
        return json.loads(text, object_pairs_hook=_pairs_first_wins)
    except json.JSONDecodeError as e:
        if text.lstrip()[:1] in ("{", "[") and _json_incomplete(text):
            raise Truncated(str(e)) from e
        raise Unparseable(str(e)) from e


def split_known(d: dict, known: frozenset[str]) -> tuple[dict, dict]:
    fields = {k: v for k, v in d.items() if k in known}
    residual = {k: v for k, v in d.items() if k not in known}
    return fields, residual


def first_str(d: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def ts_str(v: Any) -> str | None:
    """Timestamps as written: ints become their decimal string."""
    if v is None or isinstance(v, bool):
        return None
    return str(v)


def row(ts_raw: str | None, surface_app: str | None, fields: dict, residual: dict | None = None,
        **cols: Any) -> dict:
    return {"ts_raw": ts_raw, "surface_app": surface_app, "fields": fields,
            "_residual": residual or {}, "cols": cols}


REQUEST_ID = re.compile(r"\brequest_id=([0-9A-Za-z_-]+)")
