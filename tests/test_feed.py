"""Feed, calendar, saving, artist detail, and the 'new since last visit' marker."""

import datetime as dt

import pytest
from conftest import login, make_user

from poghiamo.database.models import Artist, Event, EventSource, Follow, SavedEvent
from poghiamo.services import feed

FUTURE = dt.date.today() + dt.timedelta(days=20)
PAST = dt.date.today() - dt.timedelta(days=20)


def _artist(db, name="Faccianuvola"):
    a = Artist(name=name, name_normalized=name.lower())
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _event(db, artist, *, date=FUTURE, city="Milano", province="MI", region="Lombardia",
           first_seen=None, last_seen=None, source="dice", ticket="https://t/x"):
    e = Event(
        artist_id=artist.id, date=date, city=city, city_normalized=city.lower(),
        province=province, region=region,
        first_seen_at=first_seen or dt.datetime(2026, 8, 1, 12, 0),
        last_seen_at=last_seen or dt.datetime(2026, 8, 1, 12, 0),
    )
    db.add(e)
    db.flush()
    db.add(EventSource(event_id=e.id, source=source, ticket_url=ticket))
    db.commit()
    db.refresh(e)
    return e


def _follow(db, user, artist, state="active"):
    db.add(Follow(user_id=user.id, artist_id=artist.id, state=state))
    db.commit()


# --- feed query + area filter ---


def test_feed_shows_followed_upcoming_only(db):
    user = make_user(db, username="alice")
    a = _artist(db)
    _follow(db, user, a)
    _event(db, a, date=FUTURE, city="Milano")
    _event(db, a, date=PAST, city="Milano")  # past: excluded
    other = _artist(db, "NotFollowed")
    _event(db, other, date=FUTURE, city="Milano")  # not followed: excluded

    events = feed.feed_events(db, user, all_italy=True)
    assert len(events) == 1
    assert events[0].date == FUTURE


def test_feed_area_filter_and_toggle(db):
    user = make_user(db, username="alice")
    user.regions = ["Lombardia"]
    user.provinces = []
    a = _artist(db)
    _follow(db, user, a)
    _event(db, a, date=FUTURE, city="Milano", province="MI", region="Lombardia")
    _event(db, a, date=FUTURE + dt.timedelta(days=1), city="Roma", province="RM", region="Lazio")
    db.commit()

    filtered = feed.feed_events(db, user, all_italy=False)
    assert {e.city for e in filtered} == {"Milano"}  # Lazio excluded

    everything = feed.feed_events(db, user, all_italy=True)
    assert {e.city for e in everything} == {"Milano", "Roma"}


def test_removed_follow_hides_events(db):
    user = make_user(db, username="alice")
    a = _artist(db)
    _follow(db, user, a, state="removed")
    _event(db, a)
    assert feed.feed_events(db, user, all_italy=True) == []


# --- new / stale annotation ---


def test_annotate_new_and_stale():
    class E:
        def __init__(self, fs, ls):
            self.first_seen_at, self.last_seen_at, self.id = fs, ls, 1
            self.added_by_user_id = None

    last_visit = dt.datetime(2026, 8, 10, 12, 0)
    fresh = E(dt.datetime(2026, 8, 11), feed.now_utc_naive())  # seen after last visit, seen now
    old = E(dt.datetime(2026, 8, 1), dt.datetime(2026, 8, 1))  # before visit, long unseen
    feed.annotate([fresh, old], last_seen=last_visit, saved_ids=set())
    assert fresh.is_new and not fresh.is_stale
    assert not old.is_new and old.is_stale


# --- routes: save / unsave / calendar / new-marker ---


def test_save_and_unsave_via_routes(client, db):
    user = make_user(db, username="alice", password="testpass123")
    a = _artist(db)
    _follow(db, user, a)
    e = _event(db, a)
    login(client, "alice", "testpass123")

    client.post(f"/events/{e.id}/save", follow_redirects=False)
    assert db.query(SavedEvent).count() == 1
    assert e.city in client.get("/calendario").text

    client.post(f"/events/{e.id}/unsave", follow_redirects=False)
    assert db.query(SavedEvent).count() == 0


def test_calendar_splits_upcoming_and_past(db):
    user = make_user(db, username="alice")
    a = _artist(db)
    up = _event(db, a, date=FUTURE, city="Milano")
    pa = _event(db, a, date=PAST, city="Roma")
    db.add(SavedEvent(user_id=user.id, event_id=up.id))
    db.add(SavedEvent(user_id=user.id, event_id=pa.id))
    db.commit()

    upcoming, past = feed.calendar_events(db, user)
    assert [e.city for e in upcoming] == ["Milano"]
    assert [e.city for e in past] == ["Roma"]


def test_visiting_feed_updates_last_seen(client, db):
    user = make_user(db, username="alice", password="testpass123")
    user.regions = ["Lombardia"]  # in-zona needs an area, else it redirects to settings
    db.commit()
    a = _artist(db)
    _follow(db, user, a)
    _event(db, a)  # Milano -> Lombardia
    assert user.last_seen_events_at is None
    login(client, "alice", "testpass123")
    client.get("/")
    db.refresh(user)
    assert user.last_seen_events_at is not None


def test_feed_redirects_to_settings_when_no_area(client, db):
    make_user(db, username="alice", password="testpass123")
    login(client, "alice", "testpass123")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?hint=zone"


def test_feed_split(db):
    user = make_user(db, username="alice")
    user.regions = ["Lombardia"]
    db.commit()
    a = _artist(db)
    _follow(db, user, a)
    _event(db, a, city="Milano", province="MI", region="Lombardia")
    _event(db, a, date=FUTURE + dt.timedelta(days=1), city="Roma", province="RM", region="Lazio")
    in_zona, altre = feed.feed_split(db, user)
    assert [e.city for e in in_zona] == ["Milano"]
    assert [e.city for e in altre] == ["Roma"]


def test_add_custom_event_follows_and_saves(client, db):
    user = make_user(db, username="alice", password="testpass123")
    login(client, "alice", "testpass123")
    resp = client.post(
        "/calendario/add",
        data={"artist_name": "Nuovo Artista", "date": str(FUTURE), "city": "Bologna", "venue": "Locale X"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    artist = db.query(Artist).filter(Artist.name_normalized == "nuovo artista").one()
    # Auto-followed
    assert db.query(Follow).filter(Follow.user_id == user.id, Follow.artist_id == artist.id,
                                   Follow.state == "active").count() == 1
    ev = db.query(Event).filter(Event.artist_id == artist.id).one()
    assert ev.added_by_user_id == user.id
    assert ev.region == "Emilia-Romagna"  # resolved from Bologna
    # Auto-saved
    assert db.query(SavedEvent).filter(SavedEvent.event_id == ev.id).count() == 1


def test_custom_event_shared_and_deletable_by_creator_only(client, db):
    creator = make_user(db, username="alice", password="testpass123")
    other = make_user(db, username="bob", password="testpass123")
    login(client, "alice", "testpass123")
    client.post("/calendario/add", data={"artist_name": "X", "date": str(FUTURE), "city": "Milano"},
                follow_redirects=False)
    ev = db.query(Event).one()

    # Another user (not creator, not admin) cannot delete it
    client.post("/logout", follow_redirects=False)
    login(client, "bob", "testpass123")
    client.post(f"/events/{ev.id}/custom-delete", follow_redirects=False)
    assert db.query(Event).count() == 1  # still there

    # The creator can
    client.post("/logout", follow_redirects=False)
    login(client, "alice", "testpass123")
    client.post(f"/events/{ev.id}/custom-delete", follow_redirects=False)
    assert db.query(Event).count() == 0


def test_custom_event_reuses_existing_scraped_event(client, db):
    user = make_user(db, username="alice", password="testpass123")
    a = _artist(db, "Faccianuvola")
    scraped = _event(db, a, date=FUTURE, city="Milano")  # a scraped (added_by NULL) event
    login(client, "alice", "testpass123")
    client.post("/calendario/add", data={"artist_name": "Faccianuvola", "date": str(FUTURE), "city": "Milano"},
                follow_redirects=False)
    # No duplicate event; the scraped one is just saved
    assert db.query(Event).count() == 1
    assert db.query(SavedEvent).filter(SavedEvent.event_id == scraped.id).count() == 1


def test_artist_detail_page(client, db):
    user = make_user(db, username="alice", password="testpass123")
    a = _artist(db)
    a.ticketsms_slug = "faccianuvola"
    _follow(db, user, a)
    _event(db, a, city="Milano")
    db.commit()
    login(client, "alice", "testpass123")

    page = client.get(f"/artists/{a.id}")
    assert page.status_code == 200
    assert "Faccianuvola" in page.text
    assert "Milano" in page.text


def test_group_by_month_preserves_order(db):
    a = _artist(db)
    e1 = _event(db, a, date=dt.date(2026, 8, 22), city="Milano")
    e2 = _event(db, a, date=dt.date(2026, 8, 28), city="Roma")
    e3 = _event(db, a, date=dt.date(2026, 9, 2), city="Torino")
    groups = feed.group_by_month([e1, e2, e3])
    assert len(groups) == 2
    assert len(groups[0][1]) == 2 and len(groups[1][1]) == 1
