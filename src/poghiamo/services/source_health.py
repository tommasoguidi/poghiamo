"""Per-source health for the admin settings page, read from the scan log.

A dead source has to be visible at a glance: DICE failed on every scan for two
months before anyone noticed, and the rockol home path adds a machine that can
go down on its own.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from poghiamo.database.models import ScanLog
from poghiamo.services.feed import now_utc_naive

WINDOW_DAYS = 30
# Consecutive failed fetches after which a source counts as broken rather than
# just flaky (one scan every few days per artist, so three is already a pattern).
BROKEN_AFTER = 3
_FETCHED = ("ok", "error", "timeout")
_LABELS = {"dice": "DICE", "ticketmaster": "Ticketmaster", "rockol": "rockol"}


def source_health(db, adapters=None, now: dt.datetime | None = None) -> list[dict]:
    """One summary per registered adapter, in registry order. `state` is one of
    ok | failing | broken | off | never."""
    from poghiamo.sources import ALL_ADAPTERS

    now = now or now_utc_naive()
    since = now - dt.timedelta(days=WINDOW_DAYS)
    return [_summary(db, a, since, now) for a in (ALL_ADAPTERS if adapters is None else adapters)]


def _summary(db, adapter, since: dt.datetime, now: dt.datetime) -> dict:
    name = adapter.name
    counts = dict(
        db.execute(
            select(ScanLog.status, func.count())
            .where(ScanLog.source == name, ScanLog.ran_at >= since)
            .group_by(ScanLog.status)
        ).all()
    )
    found = db.execute(
        select(func.coalesce(func.sum(ScanLog.found), 0)).where(
            ScanLog.source == name, ScanLog.ran_at >= since, ScanLog.status == "ok"
        )
    ).scalar_one()
    # Skipped runs never reached the source, so they say nothing about its health.
    fetched = (ScanLog.source == name) & ScanLog.status.in_(_FETCHED)
    last_run = db.execute(select(func.max(ScanLog.ran_at)).where(fetched)).scalar_one()
    last_ok = db.execute(
        select(func.max(ScanLog.ran_at)).where(ScanLog.source == name, ScanLog.status == "ok")
    ).scalar_one()
    since_ok = fetched & (ScanLog.ran_at > last_ok) if last_ok else fetched
    streak = db.execute(select(func.count()).where(since_ok)).scalar_one()
    last_error = db.execute(
        select(ScanLog.message, ScanLog.status)
        .where(fetched, ScanLog.status != "ok")
        .order_by(ScanLog.ran_at.desc(), ScanLog.id.desc())
        .limit(1)
    ).first()

    if not adapter.enabled:
        state = "off"
    elif last_run is None:
        state = "never"
    elif streak >= BROKEN_AFTER:
        state = "broken"
    elif streak:
        state = "failing"
    else:
        state = "ok"

    return {
        "name": name,
        "label": _LABELS.get(name, name),
        "state": state,
        "last_run": last_run,
        "last_ok": last_ok,
        "failures_in_a_row": streak,
        "last_error": (last_error[0] or last_error[1]) if last_error else None,
        "ok": counts.get("ok", 0),
        "failed": counts.get("error", 0) + counts.get("timeout", 0),
        "skipped": counts.get("skipped", 0),
        "found": found,
        "notes": _NOTES[name](db, since, now) if name in _NOTES else [],
    }


def _dice_notes(db, since: dt.datetime, now: dt.datetime) -> list[str]:
    """How many followed artists DICE knows, and when the sitemap was last read."""
    from poghiamo.database.models import Artist, Follow
    from poghiamo.sources.dice import SITEMAP_SOURCE

    followed = select(Follow.artist_id).where(Follow.state == "active")
    total, matched = db.execute(
        select(func.count(), func.count(Artist.dice_slug)).where(Artist.id.in_(followed))
    ).one()
    crawl = db.execute(
        select(ScanLog.ran_at, ScanLog.status)
        .where(ScanLog.source == SITEMAP_SOURCE)
        .order_by(ScanLog.ran_at.desc(), ScanLog.id.desc())
        .limit(1)
    ).first()
    if crawl is None:
        sitemap = "Sitemap: mai letta"
    else:
        outcome = "" if crawl.status == "ok" else ", fallita"
        sitemap = f"Sitemap: letta {ago_it(crawl.ran_at, now)}{outcome}"
    return [f"Artisti seguiti trovati su DICE: {matched} su {total}", sitemap]


def _rockol_notes(db, since: dt.datetime, now: dt.datetime) -> list[str]:
    """Which transport served rockol, and how much ZenRows budget is left."""
    from poghiamo.sources import rockol

    via = dict(
        db.execute(
            select(ScanLog.via, func.count())
            .where(
                ScanLog.source == "rockol",
                ScanLog.ran_at >= since,
                ScanLog.status.in_(_FETCHED),
            )
            .group_by(ScanLog.via)
        ).all()
    )
    notes = [
        "Proxy di casa: " + ("configurato" if rockol.ROCKOL_PROXY_URL else "non configurato"),
        f"Ultimi {WINDOW_DAYS} giorni: {via.get('home', 0)} via casa, "
        f"{via.get('zenrows', 0)} via ZenRows",
    ]
    if rockol.ZENROWS_API_KEY:
        spent = rockol.zenrows_spent_this_month(db)
        notes.append(f"ZenRows questo mese: {spent} su {rockol.ROCKOL_MONTHLY_BUDGET}")
    else:
        notes.append("ZenRows: non configurato")
    return notes


_NOTES = {"dice": _dice_notes, "rockol": _rockol_notes}


def ago_it(when: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """Italian relative time for naive-UTC timestamps, e.g. '3 giorni fa'."""
    if when is None:
        return "mai"
    seconds = ((now or now_utc_naive()) - when).total_seconds()
    if seconds < 60:
        return "adesso"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min fa"
    hours = minutes // 60
    if hours < 24:
        return "1 ora fa" if hours == 1 else f"{hours} ore fa"
    days = hours // 24
    return "ieri" if days == 1 else f"{days} giorni fa"
