"""Timezone-aware manifest timestamps; path identifiers use an explicit BJT suffix."""

from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def parse_timestamp(value):
    """Normalize an ISO-8601 timestamp to UTC for ordering and arithmetic."""
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO-8601 string with a timezone")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(timezone.utc)
