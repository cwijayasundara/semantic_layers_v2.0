"""The raw-record contract (Ruling R12) and the deterministic writer.

A renderer builds RawRecords with `fields` and `payload=None`; `serialize`
fills `payload` from FORMATTERS[fmt]. `at_utc`, `host` and `tags` are ground
truth kept in memory only and are never written to disk."""
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from e2ekg.generate.formats import FORMATTERS
from e2ekg.rawid import raw_id

SOURCES = ("catalog", "atlas", "prom", "logs", "traces", "events", "chat")


@dataclass(frozen=True)
class RawRecord:
    source: str
    rel_path: str
    fmt: str
    fields: dict[str, Any] = field(default_factory=dict)
    payload: bytes | None = None
    at_utc: datetime | None = None
    host: str | None = None
    tags: frozenset[str] = frozenset()


def serialize(rec: RawRecord) -> RawRecord:
    if rec.payload is not None:
        return rec
    return replace(rec, payload=FORMATTERS[rec.fmt](rec.fields))


def write_source(records: Sequence[RawRecord], root: Path) -> list[tuple[str, frozenset[str]]]:
    """Write each payload + b"\\n" to root/rel_path in the given order and
    return (raw_id, tags) per record. Each file is created once per call; an
    already-existing target raises FileExistsError (nothing is written), so
    stale output can never leak into a deterministic raw tree."""
    root = Path(root)
    chunks: dict[str, list[bytes]] = {}
    sizes: dict[str, int] = {}
    out: list[tuple[str, frozenset[str]]] = []
    for rec in records:
        payload = serialize(rec).payload
        assert payload is not None
        if rec.rel_path not in sizes:
            if (root / rec.rel_path).exists():
                raise FileExistsError(root / rec.rel_path)
            sizes[rec.rel_path] = 0
            chunks[rec.rel_path] = []
        out.append((raw_id(rec.source, rec.rel_path, sizes[rec.rel_path], payload), rec.tags))
        chunks[rec.rel_path] += (payload, b"\n")
        sizes[rec.rel_path] += len(payload) + 1
    for rel, parts in chunks.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("xb") as fh:
            fh.write(b"".join(parts))
    return out
