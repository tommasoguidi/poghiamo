"""Events pipeline: scan one artist across all enabled sources in parallel,
normalize and dedup the results, and upsert them into Event/EventSource.

The unit of work is the ARTIST (shared across every user's active follows), so
one scan serves everyone who follows that artist. Each adapter runs in its own
thread with a hard timeout; a source that errors or times out is logged and
skipped, never breaking the scan.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select

from poghiamo.config import SOURCE_TIMEOUT_SECONDS
from poghiamo.database.models import Artist, Event, EventSource, Follow, ScanLog
from poghiamo.sources import enabled_adapters
from poghiamo.sources.base import ArtistRef, normalize_city, resolve_area
from poghiamo.sources.ticketmaster import TicketmasterAdapter

logger = logging.getLogger(__name__)


def _run_adapter(adapter, ref: ArtistRef, tm_attraction_id):
    # Ticketmaster reuses a cached attractionId when we have one.
    if isinstance(adapter, TicketmasterAdapter):
        return adapter.fetch(ref, attraction_id=tm_attraction_id)
    return adapter.fetch(ref)


def scan_artist(db, artist: Artist) -> dict:
    """Scan one artist across all enabled sources, persist results, return a
    per-source summary. Caller owns the transaction commit."""
    ref = ArtistRef(id=artist.id, name=artist.name, name_normalized=artist.name_normalized)
    summary: dict[str, dict] = {}
    all_events = []

    # A self-governing adapter may opt out of this run (e.g. a budget-limited
    # source that has spent its monthly quota); record it and don't call it.
    adapters = []
    for a in enabled_adapters():
        try:
            run = a.should_run(db, artist)
        except Exception:
            run = True
        if run:
            adapters.append(a)
        else:
            summary[a.name] = {"status": "skipped", "found": 0}

    with ThreadPoolExecutor(max_workers=max(1, len(adapters or [1]))) as pool:
        futures = {
            pool.submit(_run_adapter, a, ref, artist.tm_attraction_id): a for a in adapters
        }
        for fut in as_completed(futures, timeout=None):
            adapter = futures[fut]
            try:
                events = fut.result(timeout=SOURCE_TIMEOUT_SECONDS)
                summary[adapter.name] = {"status": "ok", "found": len(events)}
                all_events.extend(events)
            except TimeoutError:
                summary[adapter.name] = {"status": "timeout", "found": 0}
                logger.warning(f"{adapter.name}: timeout scanning '{artist.name}'")
            except Exception as e:
                summary[adapter.name] = {"status": "error", "found": 0, "message": str(e)[:200]}
                logger.warning(f"{adapter.name}: error scanning '{artist.name}': {e}")

    # Cache Ticketmaster's attractionId once resolved (best-effort, non-fatal).
    _maybe_cache_tm_id(db, artist, adapters)

    _upsert_events(db, artist, all_events)

    for source, info in summary.items():
        db.add(
            ScanLog(
                artist_id=artist.id,
                source=source,
                status=info["status"],
                found=info.get("found", 0),
                message=info.get("message"),
            )
        )
    artist.last_scanned_at = dt.datetime.now(dt.timezone.utc)
    return summary


def _maybe_cache_tm_id(db, artist, adapters):
    if artist.tm_attraction_id:
        return
    tm = next((a for a in adapters if isinstance(a, TicketmasterAdapter)), None)
    if tm is None:
        return
    try:
        ref = ArtistRef(id=artist.id, name=artist.name, name_normalized=artist.name_normalized)
        aid = tm.resolve_attraction_id(ref)
        if aid:
            artist.tm_attraction_id = aid
    except Exception:
        pass  # non-fatal; retried next scan


def _upsert_events(db, artist: Artist, scraped: list) -> None:
    """Collapse scraped events onto canonical Events (dedup key artist+date+city)
    and record one EventSource per source. Only future events are stored; past
    dates are ignored at ingestion."""
    today = dt.date.today()
    now = dt.datetime.now(dt.timezone.utc)

    for ev in scraped:
        if ev.date < today:
            continue
        city_norm = normalize_city(ev.city)
        province, region = resolve_area(ev.city, ev.province)

        event = db.execute(
            select(Event).where(
                Event.artist_id == artist.id,
                Event.date == ev.date,
                Event.city_normalized == city_norm,
            )
        ).scalar_one_or_none()

        if event is None:
            event = Event(
                artist_id=artist.id,
                date=ev.date,
                time=ev.time,
                venue=ev.venue,
                city=ev.city,
                city_normalized=city_norm,
                province=province,
                region=region,
                lat=ev.lat,
                lon=ev.lon,
                title=ev.title,
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(event)
            db.flush()
        else:
            event.last_seen_at = now
            # Backfill fields a later, richer source provides.
            event.venue = event.venue or ev.venue
            event.province = event.province or province
            event.region = event.region or region
            event.lat = event.lat if event.lat is not None else ev.lat
            event.lon = event.lon if event.lon is not None else ev.lon
            event.time = event.time or ev.time

        src = db.execute(
            select(EventSource).where(
                EventSource.event_id == event.id, EventSource.source == ev.source
            )
        ).scalar_one_or_none()
        if src is None:
            db.add(
                EventSource(
                    event_id=event.id,
                    source=ev.source,
                    source_event_id=ev.source_event_id,
                    ticket_url=ev.ticket_url,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            src.last_seen_at = now
            src.ticket_url = ev.ticket_url or src.ticket_url


def artists_due_for_scan(db, interval_days: int, limit: int) -> list[Artist]:
    """Followed artists (by at least one active follow) never scanned or scanned
    longer ago than the interval, oldest first."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=interval_days)
    followed_ids = select(Follow.artist_id).where(Follow.state == "active").distinct()
    rows = db.execute(
        select(Artist)
        .where(
            Artist.id.in_(followed_ids),
            (Artist.last_scanned_at.is_(None)) | (Artist.last_scanned_at < cutoff),
        )
        .order_by(Artist.last_scanned_at.is_(None).desc(), Artist.last_scanned_at.asc())
        .limit(limit)
    ).scalars().all()
    return list(rows)
