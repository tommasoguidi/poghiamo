# services/artist_search.py
"""Deezer autocomplete + MusicBrainz enrichment.

The browser never talks to these APIs: the backend proxies (keeping the
privacy page's no-third-party promise) and caches. Deezer answers the
as-you-type suggestions (names + images); MusicBrainz resolves the canonical
identity (MBID, country) asynchronously after an artist is added.
"""

import logging
import re
import threading
import time
import unicodedata

import requests
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEEZER_SEARCH_URL = "https://api.deezer.com/search/artist"
MUSICBRAINZ_SEARCH_URL = "https://musicbrainz.org/ws/2/artist"
_MB_USER_AGENT = "poghiamo/0.1 (https://poghiamo.quest)"
# Below this Lucene score the top MusicBrainz hit is too fuzzy to trust.
_MB_MIN_SCORE = 85


class ArtistSuggestion(BaseModel):
    deezer_id: int | None = None
    name: str
    image_url: str | None = None


class MusicBrainzMatch(BaseModel):
    mbid: str
    name: str
    country: str | None = None
    score: int


def normalize_name(name: str) -> str:
    """Accent-stripped, casefolded, whitespace-collapsed form used for dedup."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", name).strip().casefold()


# Tiny TTL cache for suggestion queries (per-process; plenty at this scale).
_cache: dict[str, tuple[float, list[ArtistSuggestion]]] = {}
_CACHE_TTL = 300.0
_CACHE_MAX = 256


def search_deezer(query: str, limit: int = 8) -> list[ArtistSuggestion]:
    key = normalize_name(query)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]

    resp = requests.get(DEEZER_SEARCH_URL, params={"q": query, "limit": limit}, timeout=6)
    resp.raise_for_status()
    results = [
        ArtistSuggestion(
            deezer_id=item.get("id"),
            name=item["name"],
            image_url=item.get("picture_medium") or item.get("picture") or None,
        )
        for item in resp.json().get("data", [])
        if item.get("name")
    ]

    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (now, results)
    return results


# Enrichment retry bookkeeping: background tasks can be lost (server restart,
# network failure), so pending artists are re-scheduled opportunistically, at
# most once a minute each, whenever someone looks at them.
_attempt_lock = threading.Lock()
_last_attempt: dict[int, float] = {}
_RETRY_INTERVAL = 60.0


def maybe_retry_enrichment(artist_ids: list[int], background_tasks) -> None:
    """Schedule enrichment for these artists unless attempted recently."""
    now = time.monotonic()
    with _attempt_lock:
        due = [i for i in artist_ids if now - _last_attempt.get(i, 0.0) > _RETRY_INTERVAL]
        for i in due:
            _last_attempt[i] = now
    for i in due:
        background_tasks.add_task(enrich_artist, i)


# MusicBrainz asks for at most 1 request/second: process-wide throttle
# (enrichment runs in background threads, so the lock matters).
_mb_lock = threading.Lock()
_mb_last_call = 0.0


def search_musicbrainz(name: str) -> MusicBrainzMatch | None:
    global _mb_last_call
    with _mb_lock:
        wait = 1.1 - (time.monotonic() - _mb_last_call)
        if wait > 0:
            time.sleep(wait)
        _mb_last_call = time.monotonic()

    resp = requests.get(
        MUSICBRAINZ_SEARCH_URL,
        params={"query": f'artist:"{name}"', "fmt": "json", "limit": 3},
        headers={"User-Agent": _MB_USER_AGENT},
        timeout=8,
    )
    resp.raise_for_status()
    artists = resp.json().get("artists", [])
    if not artists:
        return None
    top = artists[0]
    score = int(top.get("score", 0))
    if score < _MB_MIN_SCORE:
        return None
    return MusicBrainzMatch(
        mbid=top["id"], name=top.get("name", name), country=top.get("country"), score=score
    )


def enrich_artist(artist_id: int):
    """Background task: resolve MBID/country for a pending artist.

    Opens its own session (never reuse the request's). Network failures leave
    the artist 'pending' so a later add or a future maintenance job can retry.
    """
    from poghiamo.database.engine import SessionLocal
    from poghiamo.database.models import Artist

    db = SessionLocal()
    try:
        artist = db.get(Artist, artist_id)
        if artist is None or artist.resolution_status != "pending":
            return
        try:
            match = search_musicbrainz(artist.name)
        except requests.RequestException as e:
            logger.warning(f"MusicBrainz lookup failed for '{artist.name}': {e}")
            return
        if match is None:
            artist.resolution_status = "unmatched"
        elif (
            db.query(Artist).filter(Artist.mbid == match.mbid, Artist.id != artist.id).first()
            is not None
        ):
            # Another row already owns this MBID: likely a duplicate to merge later.
            artist.resolution_status = "unmatched"
            logger.warning(f"MBID {match.mbid} already taken; '{artist.name}' left unmatched.")
        else:
            artist.mbid = match.mbid
            artist.country = match.country
            artist.resolution_status = "resolved"
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Artist enrichment failed for id={artist_id}: {e}", exc_info=True)
    finally:
        db.close()
