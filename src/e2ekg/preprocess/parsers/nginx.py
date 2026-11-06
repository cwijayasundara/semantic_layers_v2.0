"""nginx `combined` access log, optionally followed by ` rt=<request_time>`."""
import re
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, decode, row, split_lines

_LINE = re.compile(
    r'(?P<remote_addr>\S+) - (?P<remote_user>.*?) \[(?P<ts>[^\]]+)\] '
    r'"(?P<method>\S+) (?P<path>.*?) (?P<protocol>HTTP/[\d.]+)" (?P<status>\d{3}) '
    r'(?P<bytes>\d+|-) "(?P<referer>.*?)" "(?P<user_agent>.*?)"(?: rt=(?P<request_time>[\d.]+))?')


class NginxParser:
    name = "nginx"
    source = "logs"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        m = _LINE.fullmatch(decode(payload))
        if not m:
            raise Unparseable("nginx: not a combined-format line")
        f = {k: v for k, v in m.groupdict().items() if v is not None}
        status = int(f["status"])
        return row(f["ts"], None, f, {},
                   level="ERROR" if status >= 500 else "WARN" if status >= 400 else "INFO",
                   logger=None, thread=None, msg=f'{f["method"]} {f["path"]} {f["status"]}',
                   request_id=None)


NGINX = NginxParser()
