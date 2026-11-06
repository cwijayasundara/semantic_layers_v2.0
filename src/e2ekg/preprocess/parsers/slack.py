"""Slack export messages, one JSON object per line (R13). `ts` is the Slack
`"<epoch s>.<µs>"` id; `message_changed` events carry the edited message."""
from collections.abc import Iterator

from e2ekg.preprocess.parsers._base import Unparseable, load_json, row, split_known, split_lines

_KEYS = frozenset({"type", "subtype", "user", "text", "ts", "thread_ts", "user_profile",
                   "message", "previous_message", "edited", "reactions", "replies",
                   "reply_count", "parent_user_id", "bot_id", "files", "attachments", "blocks"})


class SlackParser:
    name = "slack"
    source = "chat"

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        d = load_json(payload)
        if not isinstance(d, dict) or "ts" not in d:
            raise Unparseable("slack: message without ts")
        fields, residual = split_known(d, _KEYS)
        inner = d.get("message") if isinstance(d.get("message"), dict) else {}
        return row(str(d["ts"]), None, fields, residual,
                   user=d.get("user") or inner.get("user"),
                   text=d.get("text") if "text" in d else inner.get("text"),
                   subtype=d.get("subtype"),
                   thread_ts=d.get("thread_ts") or inner.get("thread_ts"))


SLACK = SlackParser()
