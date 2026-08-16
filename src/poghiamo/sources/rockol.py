"""rockol.it adapter: our parser, pluggable fetch.

rockol is the broadest Italian source but its Cloudflare blocks the VPS IP
(plain HTTP, TLS impersonation and a headless browser all fail from there).
The PARSER is ours and complete; the FETCH is a strategy that stays None until
a residential-IP fetcher (a scraping API) is configured, at which point this
adapter turns itself on. Parsing is fully unit-tested against saved HTML.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

import datetime as _dt

import requests

from poghiamo.config import ROCKOL_MONTHLY_BUDGET, ZENROWS_API_KEY
from poghiamo.sources.base import ArtistRef, RateLimiter, ScrapedEvent, SourceAdapter

logger = logging.getLogger(__name__)

_SEARCH = "https://www.rockol.it/concerti-ricerca?artista={q}"
_ZENROWS = "https://api.zenrows.com/v1/"
_rl = RateLimiter(5.0)  # honor rockol's robots.txt Crawl-Delay: 5

_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

# Validated against real HTML captured via ZenRows 2026-08-16. Each event is an
# anchor whose href is a /concerto-...-c-{id} permalink and whose title reads:
#   "Dettagli evento 22 agosto 2026 - 'Faccianuvola' presso Lungomare Trento - Roseto degli Abruzzi - Teramo"
#   = <prefix> <date> - '<artist>' presso <venue> - <city>[ - <province>]
# Events appear twice (desktop/mobile); dedup by the alphanumeric permalink id.
_TITLE_DATE = re.compile(r"(\d{1,2})\s+([a-zàèéìòù]+)\s+(\d{4})", re.IGNORECASE)
_A_TAG = re.compile(r"<a\s[^>]*>", re.IGNORECASE | re.DOTALL)
_TITLE_ATTR = re.compile(r'title="([^"]*)"', re.IGNORECASE)
_HREF = re.compile(r'href="([^"]*)"', re.IGNORECASE)
_PERMALINK = re.compile(r"/concerto-[^\"']*-c-([a-z0-9]+)", re.IGNORECASE)
_VENUE_CITY = re.compile(r"presso\s+(.+)$", re.IGNORECASE)


def rockol_search_url(name_normalized: str) -> str:
    return _SEARCH.format(q="+".join(name_normalized.split()))


def parse_rockol_html(html: str) -> list[ScrapedEvent]:
    """Parse a rockol search-results page into ScrapedEvents. Iterate the event
    anchors (concerto permalinks), dedup the desktop/mobile duplicates by the
    permalink id."""
    events: list[ScrapedEvent] = []
    seen: set[str] = set()
    for tag in _A_TAG.findall(html):
        href_m = _HREF.search(tag)
        if not href_m:
            continue
        perm_m = _PERMALINK.search(href_m.group(1))
        if not perm_m:
            continue  # not an event anchor
        pid = perm_m.group(1)
        if pid in seen:
            continue

        title_m = _TITLE_ATTR.search(tag)
        title = title_m.group(1) if title_m else ""
        date = _parse_date(title)
        if date is None:
            continue
        seen.add(pid)

        venue, city = _venue_city(title)
        events.append(
            ScrapedEvent(
                source="rockol",
                source_event_id=pid,
                date=date,
                city=city,
                province=None,  # region resolved from city downstream
                venue=venue,
                ticket_url=_abs(href_m.group(1)),
                title=title.strip(),
            )
        )
    return events


def _parse_date(title: str) -> dt.date | None:
    dm = _TITLE_DATE.search(title)
    if not dm:
        return None
    month = _MONTHS.get(dm.group(2).lower())
    if not month:
        return None
    try:
        return dt.date(int(dm.group(3)), month, int(dm.group(1)))
    except ValueError:
        return None


def _venue_city(title: str) -> tuple[str | None, str | None]:
    # After "presso": "venue - city[ - province]". Venue is the first segment,
    # city the second; the trailing province (when present) is ignored since the
    # region is resolved from the city name via the ISTAT table.
    m = _VENUE_CITY.search(title)
    if not m:
        return None, None
    segs = [s.strip() for s in m.group(1).split(" - ") if s.strip()]
    venue = segs[0] if segs else None
    city = segs[1] if len(segs) >= 2 else None
    return venue, city


class RockolAdapter(SourceAdapter):
    name = "rockol"

    @property
    def enabled(self) -> bool:
        # Off until ZenRows (residential proxy + JS render) is configured.
        return bool(ZENROWS_API_KEY)

    def should_run(self, db, artist) -> bool:
        """Skip once this calendar month's request budget is spent, to stay
        inside the ScraperAPI free tier. Counts rockol runs that actually hit
        the API (ok/error/timeout) this month from the scan log."""
        from sqlalchemy import func as _func
        from sqlalchemy import select

        from poghiamo.database.models import ScanLog

        month_start = _dt.datetime.now(_dt.timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        spent = db.execute(
            select(_func.count())
            .select_from(ScanLog)
            .where(
                ScanLog.source == self.name,
                ScanLog.status.in_(("ok", "error", "timeout")),
                ScanLog.ran_at >= month_start,
            )
        ).scalar_one()
        return spent < ROCKOL_MONTHLY_BUDGET

    def fetch(self, artist: ArtistRef) -> list[ScrapedEvent]:
        if not self.enabled:
            return []
        _rl.wait()
        # ZenRows: js_render clears the JS challenge, premium_proxy gives a
        # residential IP, proxy_country=it keeps it Italian.
        params = {
            "apikey": ZENROWS_API_KEY,
            "url": rockol_search_url(artist.name_normalized),
            "js_render": "true",
            "premium_proxy": "true",
            "proxy_country": "it",
        }
        r = requests.get(_ZENROWS, params=params, timeout=70)
        r.raise_for_status()
        return parse_rockol_html(r.text)


def _abs(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return "https://www.rockol.it" + href
