"""Pipeline: cross-source dedup, upsert/backfill, due-artist selection."""

import datetime as dt

import pytest
from conftest import make_user

from poghiamo.database.models import Artist, Event, EventSource, Follow, ScanLog
from poghiamo.services import pipeline
from poghiamo.sources.base import FetchResult, ScrapedEvent

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


def test_dedup_rockol_and_ticketmaster_same_gig(db, artist, monkeypatch):
    # The real okgiorgio case from the on-the-wire check: rockol spells the city
    # "Segrate", Ticketmaster returns "Segrate (Milano)" which its adapter has
    # already cleaned to city="Segrate" + province="MI". Same artist+date must
    # collapse to a single Lombardia event carrying both source links.
    rockol = ScrapedEvent(source="rockol", source_event_id="r1", date=FUTURE, city="Segrate",
                          province="MI", province_raw="Milano", venue="Circolo Magnolia",
                          ticket_url="https://rockol/r1")
    tm = ScrapedEvent(source="ticketmaster", source_event_id="t1", date=FUTURE, city="Segrate",
                      province="MI", province_raw="Milano", ticket_url="https://tm/t1")
    _fake_adapters(monkeypatch, {"rockol": [rockol], "ticketmaster": [tm]})

    pipeline.scan_artist(db, artist)
    db.commit()

    assert db.query(Event).count() == 1
    e = db.query(Event).one()
    assert e.city == "Segrate" and e.province == "MI" and e.region == "Lombardia"
    assert {s.source for s in e.sources} == {"rockol", "ticketmaster"}
    assert {s.ticket_url for s in e.sources} == {"https://rockol/r1", "https://tm/t1"}


def test_one_source_two_events_same_date_does_not_crash(db, artist, monkeypatch):
    # Real Rares case: DICE sells one festival as two ticket products, so it
    # returns two events on the same (artist, date). With autoflush off, the
    # in-loop EventSource dedup must collapse them instead of inserting a
    # duplicate and tripping UNIQUE(event_id, source), which used to roll back
    # the artist's whole scan. rockol reports the same date once.
    dice_pass = ScrapedEvent(source="dice", source_event_id="d-pass", date=FUTURE, city="Piacenza",
                             venue="Palazzo Farnese", ticket_url="https://dice/pass")
    dice_day1 = ScrapedEvent(source="dice", source_event_id="d-day1", date=FUTURE, city="Piacenza",
                             venue="Palazzo Farnese", ticket_url="https://dice/day1")
    rockol = ScrapedEvent(source="rockol", source_event_id="r1", date=FUTURE, city="Piacenza",
                          province="PC", ticket_url="https://rockol/r1")
    _fake_adapters(monkeypatch, {"dice": [dice_pass, dice_day1], "rockol": [rockol]})

    pipeline.scan_artist(db, artist)
    db.commit()  # would raise IntegrityError before the fix

    assert db.query(Event).count() == 1
    e = db.query(Event).one()
    # DICE collapses to a single source row; rockol adds its own.
    assert sorted(s.source for s in e.sources) == ["dice", "rockol"]
    assert db.query(EventSource).filter(EventSource.source == "dice").count() == 1


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


def test_scan_artist_by_id_scans_and_commits(db, artist, monkeypatch):
    ev = ScrapedEvent(source="dice", date=FUTURE, city="Torino", province="TO")
    _fake_adapters(monkeypatch, {"dice": [ev]})

    summary = pipeline.scan_artist_by_id(artist.id)
    assert summary["dice"]["status"] == "ok"

    db.rollback()  # drop this session's read snapshot; the scan committed its own
    assert db.query(Event).count() == 1
    assert db.get(Artist, artist.id).last_scanned_at is not None


def test_scan_artist_by_id_missing_artist_returns_none(db):
    assert pipeline.scan_artist_by_id(999999) is None


def test_scan_artist_by_id_swallows_failure_and_keeps_last_scanned_null(db, artist, monkeypatch):
    def boom(_db, _artist):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pipeline, "scan_artist", boom)
    assert pipeline.scan_artist_by_id(artist.id) is None  # error swallowed

    db.rollback()
    # Rolled back, so the periodic sweep will retry (last_scanned_at still NULL).
    assert db.get(Artist, artist.id).last_scanned_at is None


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


def test_scan_log_records_the_transport(db, artist, monkeypatch):
    class ViaHome:
        name = "rockol"
        enabled = True

        def fetch(self, ref):
            ev = ScrapedEvent(source="rockol", date=FUTURE, city="Torino", province="TO")
            return FetchResult([ev], via="home")

    class FailingViaZenrows:
        name = "other"
        enabled = True

        def fetch(self, ref):
            err = RuntimeError("down")
            err.via = "zenrows"
            raise err

    monkeypatch.setattr(pipeline, "enabled_adapters", lambda: [ViaHome(), FailingViaZenrows()])
    summary = pipeline.scan_artist(db, artist)
    db.commit()

    assert summary["rockol"] == {"status": "ok", "found": 1, "via": "home"}
    assert db.query(Event).count() == 1  # a FetchResult's events are stored like a plain list
    logs = {log.source: log for log in db.query(ScanLog).all()}
    assert logs["rockol"].via == "home"
    assert (logs["other"].status, logs["other"].via) == ("error", "zenrows")


def test_error_messages_never_store_api_keys(db, artist, monkeypatch):
    # requests' HTTPError text carries the full URL, query string included, and
    # Ticketmaster takes its key as the apikey parameter.
    import requests

    url = (
        "https://app.ticketmaster.com/discovery/v2/events.json"
        "?keyword=x&apikey=SECRET123&classificationName=music"
    )
    _fake_adapters(
        monkeypatch,
        {"ticketmaster": requests.HTTPError(f"401 Client Error: Unauthorized for url: {url}")},
    )
    pipeline.scan_artist(db, artist)
    db.commit()

    message = db.query(ScanLog).one().message
    assert "SECRET123" not in message
    assert "apikey=***" in message
    assert "keyword=x" in message  # the rest of the URL stays readable


@pytest.fixture
def rockol_adapter(monkeypatch):
    """ZenRows-only rockol adapter with a budget of 3, isolated from the local .env."""
    import poghiamo.sources.rockol as mod

    monkeypatch.setattr(mod, "ROCKOL_PROXY_URL", "")
    monkeypatch.setattr(mod, "ZENROWS_API_KEY", "key")
    monkeypatch.setattr(mod, "ROCKOL_MONTHLY_BUDGET", 3)
    return mod.RockolAdapter()


def _log_rockol(db, artist, via, times):
    for _ in range(times):
        db.add(ScanLog(artist_id=artist.id, source="rockol", status="ok", found=0, via=via))
    db.commit()


def test_rockol_budget_counts_only_zenrows_fetches(db, artist, rockol_adapter):
    assert rockol_adapter.should_run(db, artist) is True  # no history
    _log_rockol(db, artist, "home", 5)  # home-served fetches are free
    assert rockol_adapter.should_run(db, artist) is True
    _log_rockol(db, artist, "zenrows", 3)
    assert rockol_adapter.should_run(db, artist) is False


def test_rockol_home_path_outlives_the_zenrows_budget(db, artist, rockol_adapter, monkeypatch):
    import requests

    import poghiamo.sources.rockol as mod
    from poghiamo.sources.base import ArtistRef, RateLimiter

    monkeypatch.setattr(mod, "ROCKOL_PROXY_URL", "http://10.77.0.2:8888")
    monkeypatch.setattr(mod, "_rl", RateLimiter(0))
    _log_rockol(db, artist, "zenrows", 3)
    assert rockol_adapter.should_run(db, artist) is True  # home needs no budget

    # With the budget spent, a home failure has nothing to fall back on.
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        raise requests.ConnectionError("tunnel down")

    monkeypatch.setattr(mod.requests, "get", fake_get)
    ref = ArtistRef(id=artist.id, name=artist.name, name_normalized=artist.name_normalized)
    with pytest.raises(mod.RockolFetchError):
        rockol_adapter.fetch(ref)
    assert calls == [mod.rockol_search_url("faccianuvola")]  # ZenRows never called


def test_migration_adds_scan_log_transport_and_backfills_rockol():
    from sqlalchemy import inspect, text

    from poghiamo.database.engine import _run_migrations, engine

    # Put scan_logs back the way it was before the column existed.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE scan_logs DROP COLUMN via"))
        conn.execute(
            text(
                "INSERT INTO scan_logs (source, status, found) VALUES "
                "('rockol', 'ok', 2), ('rockol', 'skipped', 0), ('dice', 'ok', 1)"
            )
        )

    _run_migrations()

    assert "via" in [c["name"] for c in inspect(engine).get_columns("scan_logs")]
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT source, status, via FROM scan_logs ORDER BY id")).all()
    # Only fetches that really hit ZenRows are attributed to it.
    assert [tuple(r) for r in rows] == [
        ("rockol", "ok", "zenrows"),
        ("rockol", "skipped", None),
        ("dice", "ok", None),
    ]
    _run_migrations()  # idempotent
