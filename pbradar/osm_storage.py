"""Single-copy OSM cache state and safe cleanup for abandoned downloads."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from .core import BASE_DIR

OSM_FILE = BASE_DIR / "cache" / "ukraine-latest.osm.pbf"
FRESH_HOURS = 24
RECHECK_DAYS = 7


@dataclass(frozen=True)
class OSMState:
    status: str
    description: str
    color: str


def inspect_osm(path: Path = OSM_FILE, now: dt.datetime | None = None) -> OSMState:
    """Check local copy age; this does NOT claim to check the remote release."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        stats = path.stat()
    except OSError:
        return OSMState("Потребує оновлення", "Дані OSM ще не завантажені", "#e6a16d")
    if not path.is_file() or stats.st_size == 0:
        return OSMState("Потребує оновлення", "Файл OSM відсутній або порожній", "#e6a16d")
    modified = dt.datetime.fromtimestamp(stats.st_mtime, dt.timezone.utc)
    age = max(dt.timedelta(0), now - modified)
    size_mb = round(stats.st_size / (1024 * 1024))
    suffix = f"локальний файл від {modified:%d.%m.%Y %H:%M} · {size_mb} МБ"
    if age < dt.timedelta(hours=FRESH_HOURS):
        return OSMState("Оновлений", suffix, "#5fd7a2")
    if age < dt.timedelta(days=RECHECK_DAYS):
        return OSMState("Готовий", suffix, "#a6c8ff")
    return OSMState("Потребує оновлення", f"Понад {RECHECK_DAYS} днів · {suffix}", "#e6a16d")


def cleanup_abandoned_download(path: Path = OSM_FILE,
                               now: dt.datetime | None = None) -> bool:
    """Remove ONLY our own .partial file when abandoned for over 24h.

    Never touches the previous completed OSM dataset. Active downloads may run
    for hours on a slow connection, so a 24h grace period avoids interruptions.
    """
    part = path.with_suffix(path.suffix + ".partial")
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        age = now - dt.datetime.fromtimestamp(part.stat().st_mtime, dt.timezone.utc)
        if age < dt.timedelta(hours=24):
            return False
        part.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
