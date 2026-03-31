from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CHILE_TZ = ZoneInfo("America/Santiago")
DATETIME_DISPLAY_FORMAT = "%d-%m-%Y %H:%M"


def _parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Accept ISO timestamps with optional trailing Z.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def utc_to_chile(dt_value):
    dt = _parse_datetime(dt_value)
    if dt is None:
        return None

    # Database values are stored in UTC; naive values are treated as UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(CHILE_TZ)


def format_utc_to_chile(dt_value, default="-"):
    dt_local = utc_to_chile(dt_value)
    if dt_local is None:
        return default
    return dt_local.strftime(DATETIME_DISPLAY_FORMAT)


def chile_datetime_filter(value):
    dt_local = utc_to_chile(value)
    if dt_local is None:
        return value if value not in (None, "") else "-"
    return dt_local.strftime(DATETIME_DISPLAY_FORMAT)
