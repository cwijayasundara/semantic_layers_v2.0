"""`git log` (medium format): a record starts at `commit <40 hex>` and runs to
the next one. Header lines (`Author:`, `Date:`, any `Key: value`) become
fields; the message is the 4-space-indented block after the blank line."""
import re
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, decode, row, split_on_header

HEADER = re.compile(rb"^commit [0-9a-f]{40}$", re.M)
_SUBJECT_APP = re.compile(r"\[([A-Za-z0-9._-]+)\]")


class GitlogParser:
    name = "gitlog"
    source = "events"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_on_header(data, HEADER)

    def parse(self, payload: bytes) -> dict:
        first, *lines = decode(payload).split("\n")
        if not re.fullmatch(r"commit [0-9a-f]{40}", first):
            raise Unparseable("gitlog: record must start with `commit <sha>`")
        fields: dict[str, str] = {"sha": first[7:]}
        residual: dict[str, str] = {}
        i = 0
        while i < len(lines) and lines[i]:
            key, sep, val = lines[i].partition(":")
            if not sep:
                raise Unparseable(f"gitlog: bad header line {lines[i]!r}")
            k = key.strip().lower()
            (fields if k in ("author", "date") else residual)[k] = val.strip()
            i += 1
        body = lines[i + 1:]
        if any(ln[:4] != "    " for ln in body):
            raise Unparseable("gitlog: message lines must be indented 4 spaces")
        fields["message"] = "\n".join(ln[4:] for ln in body)
        subject = body[0][4:] if body else ""
        m = _SUBJECT_APP.match(subject)
        return row(fields.get("date"), m.group(1) if m else None, fields, residual,
                   event_kind="commit", row_kind="record", external_id=fields["sha"],
                   event_type=None, status=None)


GITLOG = GitlogParser()
