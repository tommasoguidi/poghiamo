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

from poghiamo.config import (
    ROCKOL_MONTHLY_BUDGET,
    ROCKOL_PROXY_TIER,
    SCRAPERAPI_KEY,
)
from poghiamo.sources.base import ArtistRef, RateLimiter, ScrapedEvent, SourceAdapter

logger = logging.getLogger(__name__)

_SEARCH = "https://www.rockol.it/concerti-ricerca?artista={q}"
_SCRAPERAPI = "https://api.scraperapi.com"
_rl = RateLimiter(5.0)  # honor rockol's robots.txt Crawl-Delay: 5

_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

# Full date lives in the <a title="..."> attribute in a dash-separated structure:
#   "22 agosto 2026 - Faccianuvola presso Lungomare Trento - Roseto degli Abruzzi - Teramo"
#    = date            - artist presso venue                - city                - province
# NOTE: parsing is UNVALIDATED against live HTML (rockol blocks the VPS). It
# encodes the documented structure and its fixture; revisit the moment a
# scraping-API fetcher is wired and real HTML can be captured.
_TITLE_DATE = re.compile(r"(\d{1,2})\s+([a-zàèéìòù]+)\s+(\d{4})", re.IGNORECASE)
_ITEM = re.compile(r'<[^>]*data-cest="show-listing".*?</a>', re.DOTALL)
_TITLE_ATTR = re.compile(r'title="([^"]+)"')
_HREF = re.compile(r'href="([^"]+)"')


def rockol_search_url(name_normalized: str) -> str:
    return _SEARCH.format(q="+".join(name_normalized.split()))


def parse_rockol_html(html: str) -> list[ScrapedEvent]:
    """Parse a rockol search-results page into ScrapedEvents. Deduplicate the
    desktop/mobile duplicate markup by the event permalink id in the URL."""
    events: list[ScrapedEvent] = []
    seen: set[str] = set()
    for block in _ITEM.findall(html):
        tm = _TITLE_ATTR.search(block)
        title = tm.group(1) if tm else ""
        dm = _TITLE_DATE.search(title)
        if not dm:
            continue
        month = _MONTHS.get(dm.group(2).lower())
        if not month:
            continue
        try:
            date = dt.date(int(dm.group(3)), month, int(dm.group(1)))
        except ValueError:
            continue

        href_m = _HREF.search(block)
        href = href_m.group(1) if href_m else None
        pid = _permalink_id(href)
        if pid and pid in seen:
            continue
        if pid:
            seen.add(pid)

        city = _city(title)
        events.append(
            ScrapedEvent(
                source="rockol",
                source_event_id=pid,
                date=date,
                city=city,
                province=None,  # rockol gives the province NAME; region resolved from city downstream
                venue=_venue(title),
                ticket_url=_abs(href),
                title=title.strip(),
            )
        )
    return events


class RockolAdapter(SourceAdapter):
    name = "rockol"

    @property
    def enabled(self) -> bool:
        # Off until ScraperAPI (residential IP + JS render) is configured.
        return bool(SCRAPERAPI_KEY)

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
        params = {
            "api_key": SCRAPERAPI_KEY,
            "render": "true",
            "country_code": "it",
        }
        if ROCKOL_PROXY_TIER in ("premium", "ultra_premium"):
            params[ROCKOL_PROXY_TIER] = "true"
        # ScraperAPI wants its own params before the target url.
        params["url"] = rockol_search_url(artist.name_normalized)
        r = requests.get(_SCRAPERAPI, params=params, timeout=70)
        r.raise_for_status()
        return parse_rockol_html(r.text)


def _permalink_id(href: str | None) -> str | None:
    if not href:
        return None
    m = re.search(r"-c-(\d+)", href)  # event permalink /concerto-...-c-{id}
    return m.group(1) if m else None


def _city(title: str) -> str | None:
    # Structure: "date - artist presso venue - city - province". The city is the
    # second-to-last dash-separated segment; resolve_area later maps it to a
    # province/region via the ISTAT table.
    parts = [p.strip() for p in title.split(" - ") if p.strip()]
    if len(parts) >= 3:
        return parts[-2]
    return None


def _venue(title: str) -> str | None:
    m = re.search(r"presso\s+(.+?)\s+-\s+", title)
    return m.group(1).strip() if m else None


def _abs(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return "https://www.rockol.it" + href
