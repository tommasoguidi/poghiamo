"""Read queries for the Concerti feed, the Calendario section and artist pages.

Area filtering (region OR province, empty = all Italy) is done in Python via
geo.area_matches, since its "empty means everything" semantics don't map cleanly
to SQL; volumes here are tiny (a user's followed artists), so this is fine.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from poghiamo import geo
from poghiamo.config import EVENT_STALE_DAYS
from poghiamo.database.models import Event, Follow, SavedEvent


def now_utc_naive() -> dt.datetime:
    """Naive-UTC 'now', matching how timestamps read back from SQLite so that
    comparisons never mix aware and naive datetimes."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def followed_artist_ids(db, user) -> set[int]:
    return set(
        db.execute(
            select(Follow.artist_id).where(Follow.user_id == user.id, Follow.state == "active")
        ).scalars()
    )


def saved_event_ids(db, user) -> set[int]:
    return set(
        db.execute(select(SavedEvent.event_id).where(SavedEvent.user_id == user.id)).scalars()
    )


def _load(db, whereclause, order):
    return (
        db.execute(
            select(Event)
            .options(joinedload(Event.artist), joinedload(Event.sources))
            .where(whereclause)
            .order_by(order)
        )
        .unique()
        .scalars()
        .all()
    )


def annotate(events, *, last_seen, saved_ids):
    """Attach transient display flags used by the templates."""
    stale_before = now_utc_naive() - dt.timedelta(days=EVENT_STALE_DAYS)
    for e in events:
        e.is_new = last_seen is None or (e.first_seen_at is not None and e.first_seen_at > last_seen)
        e.is_stale = e.last_seen_at is not None and e.last_seen_at < stale_before
        e.is_saved = e.id in saved_ids
    return events


def feed_events(db, user, all_italy: bool = False):
    """Upcoming events for the user's followed artists, area-filtered by default."""
    ids = followed_artist_ids(db, user)
    if not ids:
        return []
    events = _load(db, (Event.artist_id.in_(ids)) & (Event.date >= dt.date.today()), Event.date.asc())
    if not all_italy:
        events = [
            e for e in events if geo.area_matches(user.regions, user.provinces, e.region, e.province)
        ]
    return events


def calendar_events(db, user):
    """The user's saved events, split into (upcoming, past-most-recent-first)."""
    ids = saved_event_ids(db, user)
    if not ids:
        return [], []
    events = _load(db, Event.id.in_(ids), Event.date.asc())
    today = dt.date.today()
    upcoming = [e for e in events if e.date >= today]
    past = [e for e in reversed(events) if e.date < today]
    return upcoming, past


def group_by_month(events):
    """Ordered [(date-of-first-in-month, [events]), ...], preserving input order."""
    groups: list[tuple[dt.date, list]] = []
    for e in events:
        key = (e.date.year, e.date.month)
        if groups and (groups[-1][0].year, groups[-1][0].month) == key:
            groups[-1][1].append(e)
        else:
            groups.append((e.date, [e]))
    return groups


def artist_events(db, artist_id: int):
    """All upcoming events for one artist (not area-filtered: it's the artist's page)."""
    return _load(db, (Event.artist_id == artist_id) & (Event.date >= dt.date.today()), Event.date.asc())
