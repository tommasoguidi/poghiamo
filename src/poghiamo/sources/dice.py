"""DICE adapter: artist slugs from DICE's published sitemap, events from the
artist page's __NEXT_DATA__ JSON. Keep only IT venues.

DICE's search API refuses everything but its own web client (403 from every
network since 2026-09), so names are matched offline against the artist sitemap
that DICE publishes for crawlers. `resolve_slugs` runs from the scheduler, at
most once a day and only while some followed artist has no slug yet, and stores
the matches in Artist.dice_slug. A scan then needs one page per slug.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator

import requests

from poghiamo.sources.base import _UA, ArtistRef, RateLimiter, ScrapedEvent, SourceAdapter

logger = logging.getLogger(__name__)

_ARTIST = "https://dice.fm/artist/{slug}"
_SITEMAP_INDEX = "https://dice.fm/sitemaps/artists/sitemap.xml"
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_ARTIST_URL = re.compile(r"^https://dice\.fm/artist/([a-z0-9-]+)$")
# A slug is the slugified name plus a short random id: faccianuvola-8yaw5.
_SLUG = re.compile(r"^(.+)-[a-z0-9]{4,5}$")
_rl = RateLimiter(1.0)
_sitemap_rl = RateLimiter(2.0)

SITEMAP_SOURCE = "dice_sitemap"  # ScanLog.source of the slug-resolution crawls
RECRAWL_DAYS = 30  # artists still unmatched are retried this often
MIN_GAP_HOURS = 20  # a newly followed artist waits at most for the next night
MAX_CANDIDATES = 3  # homonyms on DICE: keep a few, the page check sorts them out


class DiceAdapter(SourceAdapter):
    name = "dice"

    def __init__(self):
        # Slugs handed from should_run to fetch, keyed by artist id. A plain
        # attribute would race: the web process can scan several artists at once.
        self._slugs: dict[int, list[str]] = {}

    def should_run(self, db, artist) -> bool:
        """Only artists already matched to DICE are fetched; the others wait
        for the next sitemap crawl."""
        slugs = [s for s in (artist.dice_slug or "").split(",") if s]
        self._slugs[artist.id] = slugs
        return bool(slugs)

    def fetch(self, artist: ArtistRef) -> list[ScrapedEvent]:
        events: dict[str, ScrapedEvent] = {}
        for slug in self._slugs.pop(artist.id, []):
            _rl.wait()
            r = requests.get(_ARTIST.format(slug=slug), headers={"User-Agent": _UA}, timeout=15)
            if r.status_code == 404:
                continue  # profile gone; the artist simply has nothing here now
            r.raise_for_status()
            for ev in parse_artist_page(r.text, artist.name_normalized):
                events.setdefault(ev.source_event_id or f"{ev.date}-{ev.city}", ev)
        return list(events.values())


def parse_artist_page(html: str, expected_name: str) -> list[ScrapedEvent]:
    """Italian events from a DICE artist page. Returns nothing when the name on
    the page cannot have produced the same slug as ours (a wrong sitemap match);
    spelling differences that slugify alike (43.Nove, 43 Nove) are accepted."""
    m = _NEXT_DATA.search(html)
    if not m:
        return []
    page = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    profile = page.get("initialProfile") or page.get("profile") or {}
    shown = (profile.get("artist") or {}).get("name")
    if shown and not (slug_keys(shown) & slug_keys(expected_name)):
        logger.warning(f"dice: page is for '{shown}', expected '{expected_name}'; ignored")
        return []

    events: list[ScrapedEvent] = []
    for section in profile.get("sections", []) or []:
        # The web client reads both shapes: section.events, or section.items[].event.
        section_events = section.get("events") or [
            item["event"] for item in section.get("items") or [] if item.get("event")
        ]
        for ev in section_events:
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
                    source="dice",
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


# --- slug resolution from the sitemap ---


def slug_keys(name: str) -> set[str]:
    """The slug bases DICE could have built from this name. Accented letters
    are tried both transliterated and dropped (DICE has Dov'è Liana as
    dov-liana), apostrophes both dropped and as separators."""
    lower = name.lower()
    plain = unicodedata.normalize("NFKD", lower)
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    variants = {
        plain,
        re.sub(r"['’`]", "", plain),
        plain.replace("&", " and "),
        re.sub(r"[^\x00-\x7f]", "", lower),
    }
    keys = {re.sub(r"[^a-z0-9]+", "-", v).strip("-") for v in variants}
    return {k for k in keys if k}


def match_slugs(slugs: Iterable[str], wanted: dict[str, set[int]]) -> dict[int, list[str]]:
    """Artist id -> matching slugs, at most MAX_CANDIDATES each."""
    found: dict[int, list[str]] = defaultdict(list)
    for slug in slugs:
        m = _SLUG.match(slug)
        if not m:
            continue
        for artist_id in wanted.get(m.group(1), ()):
            if len(found[artist_id]) < MAX_CANDIDATES and slug not in found[artist_id]:
                found[artist_id].append(slug)
    return dict(found)


def crawl_sitemap() -> Iterator[str]:
    """Every artist slug in DICE's sitemap, one file at a time (~700 files of
    200 URLs, spaced by _sitemap_rl). A file that fails is skipped; failing
    to read the index raises."""
    session = requests.Session()
    session.headers["User-Agent"] = _UA
    _sitemap_rl.wait()
    index = session.get(_SITEMAP_INDEX, timeout=30)
    index.raise_for_status()
    files = _LOC.findall(index.text)
    if not files:
        raise RuntimeError("DICE sitemap index lists no files")
    failed = 0
    for url in files:
        _sitemap_rl.wait()
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException:
            failed += 1
            continue
        for loc in _LOC.findall(r.text):
            m = _ARTIST_URL.match(loc)
            if m:
                yield m.group(1)
    if failed:
        logger.warning(f"dice sitemap: {failed} of {len(files)} files failed")


def resolve_slugs(
    db, now: dt.datetime | None = None, crawl: Callable[[], Iterable[str]] = crawl_sitemap
) -> dict | None:
    """Match followed artists that have no DICE slug against the sitemap, when
    a crawl is due. Returns a summary, or None when nothing was due. Commits."""
    from sqlalchemy import func, select

    from poghiamo.database.models import Artist, Follow, ScanLog

    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    followed = select(Follow.artist_id).where(Follow.state == "active")
    pending = (
        db.execute(select(Artist).where(Artist.id.in_(followed), Artist.dice_slug.is_(None)))
        .scalars()
        .all()
    )
    if not pending:
        return None
    last = db.execute(
        select(func.max(ScanLog.ran_at)).where(ScanLog.source == SITEMAP_SOURCE)
    ).scalar_one()
    if last is not None:
        age = now - last
        newcomer = any(a.created_at and a.created_at > last for a in pending)
        if age < dt.timedelta(days=RECRAWL_DAYS) and not (
            newcomer and age >= dt.timedelta(hours=MIN_GAP_HOURS)
        ):
            return None

    wanted: dict[str, set[int]] = defaultdict(set)
    for a in pending:
        for key in slug_keys(a.name):
            wanted[key].add(a.id)
    pending_ids = [a.id for a in pending]
    # End the read transaction before a crawl that takes the best part of an
    # hour, so the SQLite WAL can keep checkpointing meanwhile.
    db.commit()

    try:
        matches = match_slugs(crawl(), wanted)
    except Exception as e:
        db.add(ScanLog(source=SITEMAP_SOURCE, status="error", message=str(e)[:200], ran_at=now))
        db.commit()
        raise

    for artist_id, slugs in matches.items():
        artist = db.get(Artist, artist_id)
        if artist is not None and artist.dice_slug is None:
            artist.dice_slug = ",".join(slugs)
            # Due right away, so its DICE dates do not wait for the weekly cadence.
            artist.last_scanned_at = None
    db.add(ScanLog(source=SITEMAP_SOURCE, status="ok", found=len(matches), ran_at=now))
    db.commit()
    return {"pending": len(pending_ids), "matched": len(matches)}


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
