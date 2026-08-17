"""Adapter interface, the ScrapedEvent boundary schema, and shared helpers."""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
import unicodedata
from dataclasses import dataclass

from pydantic import BaseModel, field_validator

from poghiamo import geo

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


@dataclass(frozen=True)
class ArtistRef:
    """The minimal artist identity an adapter needs, decoupled from the ORM."""

    id: int
    name: str
    name_normalized: str


class ScrapedEvent(BaseModel):
    """Validated output of an adapter: one Italian concert of one artist.

    Adapters keep only Italian events, so `country` is always IT here. The
    pipeline resolves region from province/city and dedups across sources.
    """

    source: str
    source_event_id: str | None = None
    date: dt.date
    time: dt.time | None = None
    venue: str | None = None
    city: str | None = None
    province: str | None = None  # sigla; may be filled by the pipeline from city
    province_raw: str | None = None  # raw area text the source used (for review if unresolved)
    lat: float | None = None
    lon: float | None = None
    ticket_url: str | None = None
    title: str | None = None

    @field_validator("province")
    @classmethod
    def _upper_sigla(cls, v):
        return v.upper() if v else v


def normalize_city(name: str | None) -> str:
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", name).strip().casefold()


def resolve_area(city: str | None, province: str | None) -> tuple[str | None, str | None]:
    """Return (province_sigla, region). Trust an explicit province; otherwise
    look the city up in the ISTAT table (only when unambiguous)."""
    region_of = geo.region_of_province()
    if province and province in region_of:
        return province, region_of[province]
    if city:
        candidates = geo.provinces_of_comune(city)
        if len(candidates) == 1:
            return candidates[0], region_of.get(candidates[0])
    return None, None


class RateLimiter:
    """Process-wide minimum spacing between calls (adapters run in threads)."""

    def __init__(self, min_interval: float):
        self._min = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            gap = self._min - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


class SourceAdapter:
    """Base adapter. Subclasses set `name` and implement `fetch`.

    `fetch` MUST be self-contained: honor the adapter's own rate limit, keep
    only Italian events, and raise on failure (the pipeline records it and
    moves on). It must never return foreign dates.
    """

    name: str = "base"

    @property
    def enabled(self) -> bool:
        return True

    def should_run(self, db, artist) -> bool:
        """Called by the pipeline (with a DB session) before each scan, so a
        self-governing adapter can skip this run, e.g. a budget-limited source
        that has spent its monthly quota. Default: always run when enabled."""
        return True

    def fetch(self, artist: ArtistRef) -> list[ScrapedEvent]:
        raise NotImplementedError
