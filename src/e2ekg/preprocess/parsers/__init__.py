"""`PARSERS` keyed by format name and `detect_format` (Ruling R6).

Formats are sniffed from content. The path prefix only narrows the choice
for non-log sources (a file under `traces/otlp/` must still look like OTLP);
log files are sniffed purely by content, since the pipeline must not know
which app logs in which format. A file nobody recognises is `unknown`, and
the runner quarantines it whole as `unknown_format`."""
import json
import re

from e2ekg.preprocess.parsers._base import Parser, Truncated, Unparseable
from e2ekg.preprocess.parsers.csv_events import CSV_EVENTS
from e2ekg.preprocess.parsers.gitlog import GITLOG
from e2ekg.preprocess.parsers.gitlog import HEADER as GIT_HEADER
from e2ekg.preprocess.parsers.json_lines import (
    ASG,
    ATLAS,
    INCIDENT,
    JSON_LOG,
    PAGERDUTY,
    RADAR,
    SPINNAKER,
)
from e2ekg.preprocess.parsers.log4j import HEADER as LOG4J_HEADER
from e2ekg.preprocess.parsers.log4j import LOG4J
from e2ekg.preprocess.parsers.logfmt import LOGFMT
from e2ekg.preprocess.parsers.nginx import NGINX
from e2ekg.preprocess.parsers.otlp import OTLP
from e2ekg.preprocess.parsers.prom import PROM
from e2ekg.preprocess.parsers.properties import PROPERTIES
from e2ekg.preprocess.parsers.slack import SLACK
from e2ekg.preprocess.parsers.yaml_catalog import YAML_CATALOG
from e2ekg.preprocess.parsers.zipkin import ZIPKIN

__all__ = ["PARSERS", "UNKNOWN", "Parser", "Truncated", "Unparseable", "detect_format"]

UNKNOWN = "unknown"

PARSERS: dict[str, Parser] = {
    "json_lines": JSON_LOG, "logfmt": LOGFMT, "log4j": LOG4J, "nginx": NGINX,
    "atlas": ATLAS, "prom": PROM, "otlp": OTLP, "zipkin": ZIPKIN,
    "spinnaker": SPINNAKER, "asg": ASG, "properties": PROPERTIES, "csv_events": CSV_EVENTS,
    "gitlog": GITLOG, "radar": RADAR, "pagerduty": PAGERDUTY, "incident": INCIDENT,
    "slack": SLACK, "yaml_catalog": YAML_CATALOG,
}

_NGINX_LINE = re.compile(rb'^\S+ - .*? \[\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\] "')
_LOGFMT_LINE = re.compile(rb"^[A-Za-z_][\w.]*=\S")
_PROPS_LINE = re.compile(rb"^\d{4}-\d{2}-\d{2}T\S+ [A-Z]+ +\[[^\]]*\] \w+")
_CSV_HEADER = re.compile(rb"^[A-Za-z_]\w*(,[A-Za-z_]\w*)+\r?$")
_PROM_LINE = re.compile(rb"^(# (HELP|TYPE) |[a-zA-Z_:][a-zA-Z0-9_:]*(\{| ))")

# JSON dialects told apart by the keys of their records.
_JSON_SIGNATURES: tuple[tuple[str, frozenset[str]], ...] = (
    ("otlp", frozenset({"resource", "scopeSpans"})),
    ("zipkin", frozenset({"traceId", "id"})),
    ("atlas", frozenset({"name", "tags", "t"})),
    ("spinnaker", frozenset({"pipeline"})),
    ("asg", frozenset({"activity_id"})),
    ("asg", frozenset({"desired_capacity"})),
    ("radar", frozenset({"alert_id"})),
    ("incident", frozenset({"affected_services"})),
    ("incident", frozenset({"event", "id"})),
    ("pagerduty", frozenset({"urgency"})),
    ("pagerduty", frozenset({"assignee"})),
    ("slack", frozenset({"type", "ts"})),
)

# Which formats a path prefix admits (non-log sources). Longest prefix wins.
_PREFIX = (
    ("catalog/", ("yaml_catalog",)),
    ("metrics/prom/", ("prom",)),
    ("metrics/", ("atlas",)),
    ("traces/otlp/", ("otlp",)),
    ("traces/zipkin/", ("zipkin",)),
    ("traces/", ("otlp", "zipkin")),
    ("events/", ("spinnaker", "asg", "properties", "csv_events", "gitlog")),
    ("alerts/", ("radar",)),
    ("pagerduty/", ("pagerduty",)),
    ("incidents/", ("incident",)),
    ("slack/", ("slack",)),
    ("logs/", ("json_lines", "logfmt", "log4j", "nginx")),
)

_SNIFF_LINES = 50


def _json_dialect(line: bytes) -> str | None:
    try:
        d = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    keys = set(d)
    for name, sig in _JSON_SIGNATURES:
        if sig <= keys:
            return name
    return "json_lines"


def _line_votes(line: bytes, allowed: tuple[str, ...]) -> str | None:
    s = line.lstrip()
    if s[:1] == b"{":
        name = _json_dialect(line)
        if name in allowed:
            return name
        return "json_lines" if name and "json_lines" in allowed else None
    checks = (("log4j", LOG4J_HEADER), ("gitlog", GIT_HEADER), ("nginx", _NGINX_LINE),
              ("properties", _PROPS_LINE), ("logfmt", _LOGFMT_LINE), ("prom", _PROM_LINE))
    for name, rx in checks:
        if name in allowed and rx.match(line):
            return name
    if "yaml_catalog" in allowed and line.rstrip() == b"---":
        return "yaml_catalog"
    return None


def detect_format(rel_path: str, head: bytes) -> str:
    """Majority vote over the first lines of `head`, so one corrupt line (a
    latin-1 or truncated record) does not decide the file's format."""
    allowed = next((fmts for prefix, fmts in _PREFIX if rel_path.startswith(prefix)), ())
    if not allowed:
        return UNKNOWN
    lines = head.split(b"\n")[:_SNIFF_LINES]
    if "csv_events" in allowed and _CSV_HEADER.match(lines[0]):
        return "csv_events"  # a CSV file is recognised by its header row
    votes: dict[str, int] = {}
    for line in lines:
        if not line.strip():
            continue
        name = _line_votes(line, allowed)
        if name:
            votes[name] = votes.get(name, 0) + 1
    if not votes:
        return UNKNOWN
    return max(votes.items(), key=lambda kv: (kv[1], -allowed.index(kv[0])))[0]
