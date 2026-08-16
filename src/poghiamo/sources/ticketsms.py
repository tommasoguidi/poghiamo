"""TicketSms adapter: open JSON API at backend.ticketsms.it/api/v4, no auth.

Resolve the performer by slug (normalized artist name), list its event codes,
then fetch each event and its location. TicketSms is an Italian ticketer, so
everything it returns is domestic.
"""

from __future__ import annotations

import datetime as dt
import logging

import requests

from poghiamo.sources.base import _UA, ArtistRef, RateLimiter, ScrapedEvent, SourceAdapter

logger = logging.getLogger(__name__)

_BASE = "https://backend.ticketsms.it/api/v4"
_rl = RateLimiter(0.5)


def _slug(name_normalized: str) -> str:
    # TicketSms slugs look like "nerissima-serpe": spaces to hyphens on the
    # already accent-stripped, casefolded normalized name.
    return "-".join(name_normalized.split())


class TicketSmsAdapter(SourceAdapter):
    name = "ticketsms"

    def _get(self, path: str) -> requests.Response:
        _rl.wait()
        return requests.get(f"{_BASE}{path}", headers={"User-Agent": _UA}, timeout=10)

    def fetch(self, artist: ArtistRef) -> list[ScrapedEvent]:
        slug = _slug(artist.name_normalized)
        r = self._get(f"/performers/{slug}")
        if r.status_code == 404:
            return []
        r.raise_for_status()

        r = self._get(f"/performers/{slug}/events?perPage=50")
        r.raise_for_status()
        items = ((r.json().get("data") or {}).get("items")) or []
        codes = [e.get("code") for e in items if e.get("code")]

        events: list[ScrapedEvent] = []
        locations: dict[int, dict] = {}
        for code in codes:
            er = self._get(f"/events/{code}")
            if er.status_code != 200:
                continue
            ev = er.json().get("data") or {}
            # dateEvent is {"date": "2026-10-15T19:00:00+00:00", ...}; the offset
            # is nominal, the value is local wall-clock, so keep date/time as-is.
            raw_date = (ev.get("dateEvent") or {}).get("date")
            date = _parse_date(raw_date)
            if date is None:
                continue

            loc_id = ev.get("locationId")
            loc = locations.get(loc_id)
            if loc is None and loc_id:
                lr = self._get(f"/locations/{loc_id}")
                loc = (lr.json().get("data") or {}) if lr.status_code == 200 else {}
                locations[loc_id] = loc
            loc = loc or {}

            events.append(
                ScrapedEvent(
                    source=self.name,
                    source_event_id=str(code),
                    date=date,
                    time=_parse_time(raw_date),
                    venue=loc.get("name"),
                    city=loc.get("city"),
                    province=(loc.get("province") or None),
                    lat=_f(loc.get("latitude")),
                    lon=_f(loc.get("longitude")),
                    ticket_url=f"https://www.ticketsms.it/event/{code}",
                    title=ev.get("name"),
                )
            )
        return events


def _parse_date(value: str | None) -> dt.date | None:
    # dateEvent carries a +00:00 offset but is really local wall-clock time;
    # take the calendar date as-is.
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
