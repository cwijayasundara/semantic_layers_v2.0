"""CSV change events (A/B allocations): the first line is the header and names
the columns; the header is itself a row (`row_kind="header"`) so its bytes
are covered. `for_file` binds a parser to one file's header."""
import csv
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import (
    Unparseable,
    decode,
    dup_key,
    first_str,
    row,
    split_lines,
)


def _cells(text: str) -> list[str]:
    try:
        rows = list(csv.reader([text], strict=True))
    except csv.Error as e:
        raise Unparseable(f"csv: {e}") from e
    if len(rows) != 1:
        raise Unparseable("csv: expected one row")
    return rows[0]


class CsvEventsParser:
    name = "csv_events"
    source = "events"
    ts_columns = ("ts", "timestamp", "time")
    id_columns = ("allocation_id", "id")

    def __init__(self, header: bytes | None = None) -> None:
        self.header = header
        self.columns = _cells(decode(header)) if header is not None else None

    def for_file(self, data: bytes) -> "CsvEventsParser":
        nl = data.find(b"\n")
        try:
            return CsvEventsParser(data[:nl if nl >= 0 else len(data)])
        except (UnicodeDecodeError, Unparseable):
            return CsvEventsParser(None)  # every row then quarantines as unparseable

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        if self.columns is None:
            raise Unparseable("csv: no readable header")
        cells = _cells(decode(payload))
        base = {"event_kind": "ab_allocation", "event_type": None, "status": None}
        if payload == self.header:
            return row(None, None, {"columns": cells}, {}, row_kind="header", external_id=None,
                       **base)
        if len(cells) > len(self.columns):
            raise Unparseable("csv: more cells than header columns")
        fields: dict[str, str] = {}
        residual: dict[str, str] = {}
        for col, cell in zip(self.columns, cells, strict=False):
            if col in fields:  # repeated header column: first wins, the rest are `col#n`
                residual[dup_key(residual, col)] = cell
            else:
                fields[col] = cell
        ts = next((fields[c] for c in self.ts_columns if fields.get(c)), None)
        return row(ts, first_str(fields, ("app",)), fields, residual, row_kind="record",
                   external_id=first_str(fields, self.id_columns), **base)


CSV_EVENTS = CsvEventsParser()
