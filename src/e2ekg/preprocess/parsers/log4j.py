"""log4j PatternLayout `%d{yyyy-MM-dd HH:mm:ss,SSS} %p [%t] %c - %m%n%throwable`.

A record starts at a header line and owns every following line up to the
next header: the unindented exception line, `\\tat ...` frames and `Caused
by:` all belong to it (Carry to T6). The timestamp has no zone; the runner
infers one per file (timefix.infer_tz)."""
import re
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import (
    REQUEST_ID,
    Unparseable,
    decode,
    row,
    split_on_header,
)

HEADER = re.compile(rb"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ", re.M)
_LINE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<level>\S+) \[(?P<thread>[^\]]*)\] "
    r"(?P<logger>\S+) - (?P<msg>.*)")


class Log4jParser:
    name = "log4j"
    source = "logs"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_on_header(data, HEADER)

    def parse(self, payload: bytes) -> dict:
        head, *stack = decode(payload).split("\n")
        m = _LINE.fullmatch(head)
        if not m:
            raise Unparseable("log4j: header does not match the layout")
        f = m.groupdict()
        rid = REQUEST_ID.search(f["msg"])
        return row(f["ts"], None, {**f, "stack": stack}, {},
                   level=f["level"].upper(), logger=f["logger"], thread=f["thread"],
                   msg=f["msg"], request_id=rid.group(1) if rid else None)


LOG4J = Log4jParser()
