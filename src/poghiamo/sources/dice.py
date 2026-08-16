"""DICE adapter: keyless unified_search to resolve the artist slug, then parse
the events embedded as __NEXT_DATA__ JSON on the artist page. Keep only IT venues.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

import requests

from poghiamo.sources.base import _UA, ArtistRef, RateLimiter, ScrapedEvent, SourceAdapter

logger = logging.getLogger(__name__)

_SEARCH = "https://api.dice.fm/unified_search"
_ARTIST = "https://dice.fm/artist/{slug}"
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
_rl = RateLimiter(1.0)


class DiceAdapter(SourceAdapter):
    name = "dice"

    def _resolve_slug(self, artist: ArtistRef) -> str | None:
        _rl.wait()
        r = requests.post(
            _SEARCH,
            json={"q": artist.name},
            headers={"User-Agent": _UA, "Content-Type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        target = artist.name_normalized
        for section in r.json().get("sections", []):
            for item in section.get("items", []):
                if item.get("type") != "artist":
                    continue
                a = item.get("artist") or {}
                name = a.get("name") or ""
                slug = a.get("slug") or a.get("perm_name")
                if slug and _norm(name) == target:
                    return slug
        return None

    def fetch(self, artist: ArtistRef) -> list[ScrapedEvent]:
        slug = self._resolve_slug(artist)
        if not slug:
            return []

        _rl.wait()
        r = requests.get(_ARTIST.format(slug=slug), headers={"User-Agent": _UA}, timeout=15)
        r.raise_for_status()
        m = _NEXT_DATA.search(r.text)
        if not m:
            return []
        data = json.loads(m.group(1))

        profile = (
            data.get("props", {}).get("pageProps", {}).get("initialProfile")
            or data.get("props", {}).get("pageProps", {}).get("profile")
            or {}
        )
        events: list[ScrapedEvent] = []
        for section in profile.get("sections", []) or []:
            for ev in section.get("events", []) or []:
                venues = ev.get("venues") or []
                venue = venues[0] if venues else {}
                city = venue.get("city") or {}
                if (city.get("country_code") or "").upper() != "IT":
                    continue
                date = _parse_date(ev.get("dates", {}).get("event_start_date"))
                if date is None:
                    continue
                loc = venue.get("location") or {}
                perm = ev.get("perm_name")
                events.append(
                    ScrapedEvent(
                        source=self.name,
                        source_event_id=str(ev.get("id") or perm or ""),
                        date=date,
                        time=_parse_time(ev.get("dates", {}).get("event_start_date")),
                        venue=venue.get("name"),
                        city=city.get("name"),
                        lat=_f(loc.get("lat")),
                        lon=_f(loc.get("lng")),
                        ticket_url=f"https://dice.fm/event/{perm}" if perm else None,
                        title=ev.get("name"),
                    )
                )
        return events


def _norm(name: str) -> str:
    from poghiamo.sources.base import normalize_city

    return normalize_city(name)


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_time(value: str | None) -> dt.time | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).time()
    except ValueError:
        return None


def _f(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
