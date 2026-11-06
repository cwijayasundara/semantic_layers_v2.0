"""`git log` (medium format): a record starts at `commit <40 hex>` and runs to
the next one. Header lines (`Author:`, `Date:`, any `Key: value`) become
fields; the message is the 4-space-indented block after the blank line."""
import re
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, decode, dup_key, row, split_on_header

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
        dates: list[str] = []
        i = 0
        while i < len(lines) and lines[i]:
            key, sep, val = lines[i].partition(":")
            if not sep:
                raise Unparseable(f"gitlog: bad header line {lines[i]!r}")
            # Key casing and value text are kept verbatim (padding included); only
            # the recognition of author/date is case-insensitive.
            known = key.strip().lower() in ("author", "date")
            if known and key not in fields:
                fields[key] = val
                if key.strip().lower() == "date":
                    dates.append(val.strip())
            else:  # unknown header, or a repeat of one (first wins, rest are `key#n`)
                taken = fields | residual | {"message": None}  # `sha`/`message` are parser-owned
                residual[dup_key(taken, key) if key in taken or known else key] = val
            i += 1
        body = lines[i + 1:]
        if any(ln[:4] != "    " for ln in body):
            raise Unparseable("gitlog: message lines must be indented 4 spaces")
        fields["message"] = "\n".join(ln[4:] for ln in body)
        subject = body[0][4:] if body else ""
        m = _SUBJECT_APP.match(subject)
        return row(dates[0] if dates else None, m.group(1) if m else None, fields, residual,
                   event_kind="commit", row_kind="record", external_id=fields["sha"],
                   event_type=None, status=None)


GITLOG = GitlogParser()
