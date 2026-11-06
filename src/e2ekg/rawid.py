"""Byte-exact raw record ids: "{source}|{rel_path}|{offset}|{length}|{sha256(payload)[:16]}".

`raw_id` is the only producer of these strings (Global Constraints)."""
import hashlib
import re
from dataclasses import dataclass

_DIGEST = re.compile(r"[0-9a-f]{16}")


def raw_id(source: str, rel_path: str, offset: int, payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"{source}|{rel_path}|{offset}|{len(payload)}|{digest}"


@dataclass(frozen=True)
class RawSpan:
    raw_id: str
    source: str
    rel_path: str
    offset: int
    length: int

    @classmethod
    def parse(cls, rid: str) -> "RawSpan":
        """Source is the first field and offset/length/digest the last three,
        so a rel_path containing '|' still parses."""
        head, sep, tail = rid.partition("|")
        parts = tail.rsplit("|", 3) if sep else []
        if len(parts) != 4 or not head or not parts[0]:
            raise ValueError(f"malformed raw id {rid!r}")
        rel_path, off, length, digest = parts
        if not (off.isdigit() and length.isdigit() and _DIGEST.fullmatch(digest)):
            raise ValueError(f"malformed raw id {rid!r}")
        return cls(raw_id=rid, source=head, rel_path=rel_path, offset=int(off),
                   length=int(length))
