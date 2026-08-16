"""Ticketmaster Discovery API adapter (official, free key).

Resolve the artist to an attractionId once (the pipeline caches it on the
Artist), then list its Italian events. Disabled when TICKETMASTER_API_KEY is
unset, so local dev and key-less deploys simply skip it.
"""

from __future__ import annotations

import datetime as dt
import logging

import requests

from poghiamo.config import TICKETMASTER_API_KEY
from poghiamo.sources.base import ArtistRef, RateLimiter, ScrapedEvent, SourceAdapter, normalize_city

logger = logging.getLogger(__name__)

_ATTRACTIONS = "https://app.ticketmaster.com/discovery/v2/attractions.json"
_EVENTS = "https://app.ticketmaster.com/discovery/v2/events.json"
_rl = RateLimiter(0.5)  # docs say 5/s, FAQ says 2/s: stay well under


class TicketmasterAdapter(SourceAdapter):
    name = "ticketmaster"

    @property
    def enabled(self) -> bool:
        return bool(TICKETMASTER_API_KEY)

    def resolve_attraction_id(self, artist: ArtistRef) -> str | None:
        """Public so the pipeline can cache the id on the Artist row."""
        _rl.wait()
        r = requests.get(
            _ATTRACTIONS,
            params={"keyword": artist.name, "apikey": TICKETMASTER_API_KEY, "classificationName": "music"},
            timeout=10,
        )
        r.raise_for_status()
        attractions = (r.json().get("_embedded") or {}).get("attractions") or []
        target = artist.name_normalized
        for a in attractions:
            if normalize_city(a.get("name", "")) == target:
                return a.get("id")
        return None

    def fetch(self, artist: ArtistRef, attraction_id: str | None = None) -> list[ScrapedEvent]:
        aid = attraction_id or self.resolve_attraction_id(artist)
        if not aid:
            return []

        _rl.wait()
        r = requests.get(
            _EVENTS,
            params={
                "attractionId": aid,
                "countryCode": "IT",
                "apikey": TICKETMASTER_API_KEY,
                "sort": "date,asc",
                "size": 100,
            },
            timeout=10,
        )
        r.raise_for_status()
        items = (r.json().get("_embedded") or {}).get("events") or []

        events: list[ScrapedEvent] = []
        for ev in items:
            start = (ev.get("dates") or {}).get("start") or {}
            date = _parse_date(start.get("localDate"))
            if date is None:
                continue
            venue = (((ev.get("_embedded") or {}).get("venues") or [{}]) or [{}])[0]
            city = (venue.get("city") or {}).get("name")
            loc = venue.get("location") or {}
            url = ev.get("url")
            events.append(
                ScrapedEvent(
                    source=self.name,
                    source_event_id=str(ev.get("id") or ""),
                    date=date,
                    time=_parse_time(start.get("localTime")),
                    venue=venue.get("name"),
                    city=city,
                    lat=_f(loc.get("latitude")),
                    lon=_f(loc.get("longitude")),
                    ticket_url=url,
                    title=ev.get("name"),
                )
            )
        return events


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_time(value: str | None) -> dt.time | None:
    if not value:
        return None
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        return None


def _f(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
