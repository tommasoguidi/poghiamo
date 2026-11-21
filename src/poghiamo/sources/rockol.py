"""rockol.it adapter: our parser, two ways to fetch.

rockol is the broadest Italian source but its Cloudflare blocks the VPS IP
(plain HTTP, TLS impersonation and a headless browser all fail from there),
while a plain GET with a browser UA passes from a non-datacenter line. So the
fetch goes out through a forward proxy on the owner's home PC when one is
configured (ROCKOL_PROXY_URL, reached over WireGuard), and falls back to ZenRows
(rented residential proxy + JS render, on a monthly budget) when the home path
is unreachable or blocked. With neither configured the adapter is off. The
parser is ours and fully unit-tested against saved HTML.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time

import requests

from poghiamo.config import (
    ROCKOL_HOME_COOLDOWN_HOURS,
    ROCKOL_MONTHLY_BUDGET,
    ROCKOL_PROXY_URL,
    ZENROWS_API_KEY,
)
from poghiamo.sources.base import (
    _UA,
    ArtistRef,
    FetchResult,
    RateLimiter,
    ScrapedEvent,
    SourceAdapter,
)

logger = logging.getLogger(__name__)

_SEARCH = "https://www.rockol.it/concerti-ricerca?artista={q}"
_ZENROWS = "https://api.zenrows.com/v1/"
# robots.txt asks for Crawl-Delay: 5; the jitter makes it 5 to 8 s, never a fixed beat.
_rl = RateLimiter(5.0, jitter=3.0)
# Connect and read timeouts for the home path: a dead tunnel must fail fast.
_HOME_TIMEOUT = (5, 20)
_HOME_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
}

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

        venue, city, province, province_raw = _venue_city(title)
        events.append(
            ScrapedEvent(
                source="rockol",
                source_event_id=pid,
                date=date,
                city=city,
                province=province,  # resolved from the province NAME (last title segment)
                province_raw=province_raw,
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


def _venue_city(title: str) -> tuple[str | None, str | None, str | None, str | None]:
    # After "presso": "venue - [address -] city - province". The last segment is
    # the province NAME (a reliable signal); the city is the segment before it;
    # the venue is the first. Returns (venue, city, province_sigla, province_raw).
    from poghiamo import geo

    m = _VENUE_CITY.search(title)
    if not m:
        return None, None, None, None
    segs = [s.strip() for s in m.group(1).split(" - ") if s.strip()]
    if not segs:
        return None, None, None, None
    venue = segs[0]
    raw = segs[-1] if len(segs) >= 2 else None  # the province name as written
    sigla = geo.province_name_to_sigla(raw)
    if sigla:
        # last = province; city is the segment before it (or the province name
        # itself when the title is just "venue - province", e.g. "Arena - Verona").
        city = segs[-2] if len(segs) >= 3 else segs[-1]
        return venue, city, sigla, raw
    # Province name did not resolve: keep the real city (second-to-last when
    # there is an address/province tail) and hand the raw province name upward.
    city = segs[-2] if len(segs) >= 3 else (segs[-1] if len(segs) >= 2 else None)
    return venue, city, None, raw


def zenrows_spent_this_month(db) -> int:
    """rockol fetches served by ZenRows since the start of this UTC month: the
    budget counter, read from the scan log."""
    from sqlalchemy import func, select

    from poghiamo.database.models import ScanLog

    month_start = dt.datetime.now(dt.timezone.utc).replace(
        tzinfo=None, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return db.execute(
        select(func.count())
        .select_from(ScanLog)
        .where(
            ScanLog.source == "rockol",
            ScanLog.via == "zenrows",
            ScanLog.ran_at >= month_start,
        )
    ).scalar_one()


class RockolFetchError(RuntimeError):
    """A failed rockol fetch, tagged with the transport that failed so the scan
    log can attribute it."""

    def __init__(self, message: str, via: str | None):
        super().__init__(message)
        self.via = via


class _HomeUnavailable(Exception):
    """The home path could not serve this fetch and ZenRows should be tried:
    the tunnel or proxy is down, or (blocked=True) rockol refused the line."""

    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


class RockolAdapter(SourceAdapter):
    name = "rockol"

    def __init__(self):
        # Monotonic deadline until which the home path is skipped after a block.
        self._home_blocked_until = 0.0
        # ZenRows fetches still allowed this month. should_run refreshes it from
        # the scan log before every scan; a soft counter is enough for a budget
        # that already keeps a margin under the real quota.
        self._zenrows_left = ROCKOL_MONTHLY_BUDGET

    @property
    def enabled(self) -> bool:
        return bool(ROCKOL_PROXY_URL or ZENROWS_API_KEY)

    def _home_available(self) -> bool:
        return bool(ROCKOL_PROXY_URL) and time.monotonic() >= self._home_blocked_until

    def should_run(self, db, artist) -> bool:
        """Run when either transport can serve: the home path (unmetered), or
        ZenRows while this calendar month's budget lasts."""
        left = 0
        if ZENROWS_API_KEY:
            left = max(0, ROCKOL_MONTHLY_BUDGET - zenrows_spent_this_month(db))
        self._zenrows_left = left
        return self._home_available() or left > 0

    def fetch(self, artist: ArtistRef) -> FetchResult:
        if not self.enabled:
            return FetchResult([])
        url = rockol_search_url(artist.name_normalized)
        _rl.wait()

        home_failure = None
        if self._home_available():
            try:
                return FetchResult(parse_rockol_html(self._get_via_home(url)), via="home")
            except _HomeUnavailable as e:
                if e.blocked:
                    # Back off instead of insisting from the home line.
                    self._home_blocked_until = (
                        time.monotonic() + ROCKOL_HOME_COOLDOWN_HOURS * 3600
                    )
                home_failure = str(e)
                logger.warning(f"rockol: {home_failure}")

        if ZENROWS_API_KEY and self._zenrows_left > 0:
            # Counted before the call: a failed ZenRows request is spent budget too.
            self._zenrows_left -= 1
            return FetchResult(parse_rockol_html(self._get_via_zenrows(url)), via="zenrows")
        if home_failure:
            raise RockolFetchError(f"{home_failure}; no ZenRows fallback left", via="home")
        raise RockolFetchError("no transport available", via=None)

    def _get_via_home(self, url: str) -> str:
        """Plain GET through the home proxy. Raises _HomeUnavailable when ZenRows
        should take over, RockolFetchError when the path works and rockol itself
        answers with an error (ZenRows would get the same)."""
        proxies = {"http": ROCKOL_PROXY_URL, "https": ROCKOL_PROXY_URL}
        try:
            r = requests.get(url, headers=_HOME_HEADERS, proxies=proxies, timeout=_HOME_TIMEOUT)
        except requests.RequestException as e:
            raise _HomeUnavailable(f"home proxy unreachable ({type(e).__name__})") from None
        # Cloudflare marks its own refusals with cf-mitigated; 403/429 are blocks
        # whoever sends them.
        if r.status_code in (403, 429) or "cf-mitigated" in r.headers:
            raise _HomeUnavailable(
                f"rockol blocked the home line (HTTP {r.status_code})", blocked=True
            )
        if r.status_code != 200:
            raise RockolFetchError(f"rockol answered HTTP {r.status_code}", via="home")
        return r.text

    def _get_via_zenrows(self, url: str) -> str:
        # js_render clears the JS challenge, premium_proxy gives a residential
        # IP, proxy_country=it keeps it Italian.
        params = {
            "apikey": ZENROWS_API_KEY,
            "url": url,
            "js_render": "true",
            "premium_proxy": "true",
            "proxy_country": "it",
        }
        # requests' own error text embeds the request URL, API key included, and
        # it would end up in the scan log: raise our own message instead.
        try:
            r = requests.get(_ZENROWS, params=params, timeout=70)
        except requests.RequestException as e:
            raise RockolFetchError(
                f"ZenRows request failed ({type(e).__name__})", via="zenrows"
            ) from None
        if r.status_code != 200:
            raise RockolFetchError(f"ZenRows answered HTTP {r.status_code}", via="zenrows")
        return r.text


def _abs(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return "https://www.rockol.it" + href
