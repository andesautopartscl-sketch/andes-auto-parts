"""Zona horaria oficial de Chile para todo Andes Auto Parts.

Fuente de verdad: IANA ``America/Santiago`` (la misma que usa Google Calendar,
Windows/macOS “Santiago” y tzdata). Incluye automáticamente el adelanto de
primavera (típicamente primer sábado de septiembre) y el atraso de otoño
(abril); no hay que hardcodear fechas.

Convención del sistema
----------------------
* **Almacenamiento en DB:** UTC naive (``datetime.utcnow`` / ``utcnow_naive()``).
* **Pantallas / PDFs / auditoría visible:** siempre convertir con
  ``chile_datetime`` / ``format_utc_to_chile`` / ``utc_to_chile``.
* **“Hoy” de negocio** (fecha de documento, cortes diarios): ``chile_today()``
  o ``now_chile()``, nunca ``datetime.now()`` sin zona.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHILE_TZ_NAME = "America/Santiago"

try:
    CHILE_TZ = ZoneInfo(CHILE_TZ_NAME)
except ZoneInfoNotFoundError as exc:  # pragma: no cover - Windows sin tzdata
    raise RuntimeError(
        "No está disponible la zona America/Santiago. "
        "Instalá el paquete 'tzdata' (pip install tzdata)."
    ) from exc

DATETIME_DISPLAY_FORMAT = "%d-%m-%Y %H:%M"
DATETIME_DISPLAY_FORMAT_SECONDS = "%d-%m-%Y %H:%M:%S"
DATE_DISPLAY_FORMAT = "%d-%m-%Y"


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
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        # Python 3.11+ may return date for YYYY-MM-DD; promote to midnight UTC
        # only when used as timestamp path — callers of chile_date handle dates.
        if isinstance(parsed, date) and not isinstance(parsed, datetime):
            return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
        return parsed
    return None


def now_chile() -> datetime:
    """Instante actual en horario de Chile (aware)."""
    return datetime.now(CHILE_TZ)


def chile_today() -> date:
    """Fecha de calendario 'hoy' en Chile (útil para documentos / cortes)."""
    return now_chile().date()


def chile_today_str(fmt: str = "%Y-%m-%d") -> str:
    return chile_today().strftime(fmt)


def chile_day_start_utc(d: date) -> datetime:
    """Inicio del día civil Chile (00:00) como UTC naive para filtrar columnas DB."""
    local = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=CHILE_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def chile_day_end_exclusive_utc(d: date) -> datetime:
    """Fin exclusivo del día civil Chile (00:00 del día siguiente) como UTC naive."""
    from datetime import timedelta

    return chile_day_start_utc(d + timedelta(days=1))


def utcnow_naive() -> datetime:
    """UTC naive para columnas DateTime de la DB (misma convención histórica)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def chile_to_utc_naive(dt_value) -> datetime | None:
    """Convierte un instante (o naive interpretado como Chile) a UTC naive."""
    dt = _parse_datetime(dt_value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CHILE_TZ)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_chile(dt_value):
    """Convierte un valor de DB (UTC naive / ISO) a datetime aware en Chile."""
    if isinstance(dt_value, date) and not isinstance(dt_value, datetime):
        # Fecha civil sin hora: mostrar el mismo día (sin shift UTC).
        return datetime(dt_value.year, dt_value.month, dt_value.day, tzinfo=CHILE_TZ)

    dt = _parse_datetime(dt_value)
    if dt is None:
        return None

    # Database timestamps are stored in UTC; naive datetimes are treated as UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(CHILE_TZ)


def format_utc_to_chile(dt_value, default="-", with_seconds: bool = False):
    dt_local = utc_to_chile(dt_value)
    if dt_local is None:
        return default
    fmt = DATETIME_DISPLAY_FORMAT_SECONDS if with_seconds else DATETIME_DISPLAY_FORMAT
    return dt_local.strftime(fmt)


def chile_datetime_filter(value):
    """Filtro Jinja: instante UTC → texto en horario de Chile."""
    dt_local = utc_to_chile(value)
    if dt_local is None:
        return value if value not in (None, "") else "-"
    return dt_local.strftime(DATETIME_DISPLAY_FORMAT)


def chile_date_filter(value):
    """Filtro Jinja: solo fecha (dd-mm-yyyy) en Chile."""
    dt_local = utc_to_chile(value)
    if dt_local is None:
        return value if value not in (None, "") else "-"
    return dt_local.strftime(DATE_DISPLAY_FORMAT)


def chile_tz_status() -> dict:
    """Diagnóstico: offset actual y nombre de zona (DST incluido)."""
    now = now_chile()
    offset = now.utcoffset()
    offset_hours = (offset.total_seconds() / 3600.0) if offset else None
    return {
        "tz": CHILE_TZ_NAME,
        "now_chile": now.isoformat(),
        "tzname": now.tzname(),
        "utc_offset_hours": offset_hours,
        "is_dst": bool(now.dst()),
    }
