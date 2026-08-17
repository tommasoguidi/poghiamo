"""Pipeline: cross-source dedup, upsert/backfill, due-artist selection."""

import datetime as dt

import pytest
from conftest import make_user

from poghiamo.database.models import Artist, Event, EventSource, Follow, ScanLog
from poghiamo.services import pipeline
from poghiamo.sources.base import ScrapedEvent

FUTURE = dt.date.today() + dt.timedelta(days=30)
PAST = dt.date.today() - dt.timedelta(days=5)


@pytest.fixture
def artist(db):
    a = Artist(name="Faccianuvola", name_normalized="faccianuvola")
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _fake_adapters(monkeypatch, mapping):
    """mapping: source name -> list[ScrapedEvent] (or Exception to raise)."""

    class Fake:
        def __init__(self, name, payload):
            self.name = name
            self._payload = payload

        @property
        def enabled(self):
            return True

        def fetch(self, ref):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    adapters = [Fake(n, p) for n, p in mapping.items()]
    monkeypatch.setattr(pipeline, "enabled_adapters", lambda: adapters)


def test_dedup_across_sources(db, artist, monkeypatch):
    # Same gig from two sources: same date + city, different venue spelling
    ev_a = ScrapedEvent(source="ticketsms", source_event_id="A", date=FUTURE, city="Milano",
                        province="MI", venue="Circolo Magnolia", ticket_url="https://tsms/A")
    ev_b = ScrapedEvent(source="dice", source_event_id="B", date=FUTURE, city="Milano",
                        venue="Magnolia", ticket_url="https://dice/B")
    _fake_adapters(monkeypatch, {"ticketsms": [ev_a], "dice": [ev_b]})

    pipeline.scan_artist(db, artist)
    db.commit()

    assert db.query(Event).count() == 1
    event = db.query(Event).one()
    assert event.city == "Milano" and event.province == "MI" and event.region == "Lombardia"
    assert {s.source for s in event.sources} == {"ticketsms", "dice"}
    assert {s.ticket_url for s in event.sources} == {"https://tsms/A", "https://dice/B"}


def test_dedup_merges_different_cities_prefers_resolved(db, artist, monkeypatch):
    # Same artist+date from two sources, different city; the one that resolves a
    # region wins, and both source links are kept.
    rockol = ScrapedEvent(source="rockol", date=FUTURE, city="Nubistan", province_raw="Nubistan")
    tsms = ScrapedEvent(source="ticketsms", date=FUTURE, city="Milano", province="MI")
    _fake_adapters(monkeypatch, {"rockol": [rockol], "ticketsms": [tsms]})

    pipeline.scan_artist(db, artist)
    db.commit()

    assert db.query(Event).count() == 1
    e = db.query(Event).one()
    assert e.city == "Milano" and e.region == "Lombardia"  # resolved source wins
    assert e.province_raw == "Nubistan"  # unmatched raw text retained for review
    assert {s.source for s in e.sources} == {"rockol", "ticketsms"}


def test_past_events_are_not_stored(db, artist, monkeypatch):
    _fake_adapters(monkeypatch, {
        "dice": [ScrapedEvent(source="dice", date=PAST, city="Roma")],
    })
    pipeline.scan_artist(db, artist)
    db.commit()
    assert db.query(Event).count() == 0


def test_failing_source_does_not_break_scan(db, artist, monkeypatch):
    good = ScrapedEvent(source="dice", date=FUTURE, city="Torino", province="TO")
    _fake_adapters(monkeypatch, {"dice": [good], "ticketsms": RuntimeError("boom")})

    summary = pipeline.scan_artist(db, artist)
    db.commit()

    assert db.query(Event).count() == 1
    assert summary["dice"]["status"] == "ok"
    assert summary["ticketsms"]["status"] == "error"
    # Health logged for both
    statuses = {s.source: s.status for s in db.query(ScanLog).all()}
    assert statuses == {"dice": "ok", "ticketsms": "error"}
    assert artist.last_scanned_at is not None


def test_rescan_backfills_and_updates_last_seen(db, artist, monkeypatch):
    thin = ScrapedEvent(source="dice", date=FUTURE, city="Bologna")  # no venue
    _fake_adapters(monkeypatch, {"dice": [thin]})
    pipeline.scan_artist(db, artist)
    db.commit()
    event = db.query(Event).one()
    first_seen = event.first_seen_at
    # Province/region are resolved from the city even without an explicit field
    assert event.province == "BO" and event.region == "Emilia-Romagna"
    assert event.venue is None

    rich = ScrapedEvent(source="ticketsms", date=FUTURE, city="Bologna", province="BO",
                        venue="Estragon")
    _fake_adapters(monkeypatch, {"ticketsms": [rich]})
    pipeline.scan_artist(db, artist)
    db.commit()

    db.refresh(event)
    assert db.query(Event).count() == 1
    assert event.venue == "Estragon"  # backfilled on rescan
    assert event.first_seen_at == first_seen  # unchanged
    assert db.query(EventSource).count() == 2


def test_due_selection_respects_interval_and_follows(db, artist, monkeypatch):
    user = make_user(db, username="alice")
    # Unfollowed artist is never due
    other = Artist(name="Other", name_normalized="other")
    db.add(other)
    db.add(Follow(user_id=user.id, artist_id=artist.id, state="active"))
    db.commit()

    due = pipeline.artists_due_for_scan(db, interval_days=7, limit=10)
    assert [a.id for a in due] == [artist.id]  # only the followed one, never scanned

    # After a recent scan it drops out
    artist.last_scanned_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    assert pipeline.artists_due_for_scan(db, interval_days=7, limit=10) == []

    # An old scan makes it due again
    artist.last_scanned_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    db.commit()
    assert [a.id for a in pipeline.artists_due_for_scan(db, 7, 10)] == [artist.id]


def test_removed_follow_excludes_artist(db, artist, monkeypatch):
    user = make_user(db, username="bob")
    db.add(Follow(user_id=user.id, artist_id=artist.id, state="removed"))
    db.commit()
    assert pipeline.artists_due_for_scan(db, 7, 10) == []


def test_adapter_can_opt_out_of_a_run(db, artist, monkeypatch):
    """A budget-limited adapter that returns should_run=False is recorded as
    'skipped' and never fetched."""

    class Budgeted:
        name = "rockol"
        enabled = True

        def should_run(self, db, artist):
            return False

        def fetch(self, ref):
            raise AssertionError("must not be called when should_run is False")

    monkeypatch.setattr(pipeline, "enabled_adapters", lambda: [Budgeted()])
    summary = pipeline.scan_artist(db, artist)
    db.commit()
    assert summary["rockol"]["status"] == "skipped"
    assert db.query(ScanLog).one().status == "skipped"


def test_rockol_budget_guard_counts_month(db, artist):
    from poghiamo.config import ROCKOL_MONTHLY_BUDGET
    from poghiamo.sources.rockol import RockolAdapter

    adapter = RockolAdapter()
    # Under budget with no history
    assert adapter.should_run(db, artist) is True
    # Fill the month's budget with spent runs (ok/error/timeout count)
    for _ in range(ROCKOL_MONTHLY_BUDGET):
        db.add(ScanLog(artist_id=artist.id, source="rockol", status="ok", found=0))
    db.commit()
    assert adapter.should_run(db, artist) is False
