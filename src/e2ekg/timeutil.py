from datetime import UTC, datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def pt(y: int, m: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    """Build a Pacific-time wall-clock instant and return it as aware UTC."""
    return datetime(y, m, d, h, mi, s, tzinfo=PT).astimezone(UTC)
