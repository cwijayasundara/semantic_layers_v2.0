"""Parse the agent's free-text `refuted_alternatives` lines into structured entries."""
from __future__ import annotations

import re
from dataclasses import dataclass

# <uri> [(parenthetical)] refuted[ as <what>]: <reason>
_LINE = re.compile(
    r"^(?P<uri>https?://\S+)"
    r"(?:\s*\((?P<paren>[^)]*)\))?"
    r"\s+refuted(?P<as>\s+as\b[^:]*)?:\s*(?P<reason>.*)$",
    re.DOTALL,
)
_HEX_PREFIX = re.compile(r"\b[0-9a-fA-F]{6,}\b")


@dataclass(frozen=True)
class Refuted:
    uris: tuple[str, ...]
    reason: str
    as_root: bool


def _resolve_prefixes(paren: str, known_uris: set[str]) -> list[str]:
    out: list[str] = []
    for prefix in _HEX_PREFIX.findall(paren):
        for uri in sorted(known_uris):
            if uri.rsplit("/", 1)[-1].lower().startswith(prefix.lower()) and uri not in out:
                out.append(uri)
                break
    return out


def parse_refuted(lines: list[str], known_uris: set[str] | None = None) -> list[Refuted]:
    known = known_uris or set()
    parsed: list[Refuted] = []
    for line in lines:
        m = _LINE.match(line.strip())
        if not m:
            parsed.append(Refuted((), line.strip(), False))
            continue
        uris = [m["uri"]]
        if m["paren"]:
            uris += [u for u in _resolve_prefixes(m["paren"], known) if u not in uris]
        parsed.append(Refuted(tuple(uris), m["reason"].strip(), "root" in (m["as"] or "")))
    return parsed
