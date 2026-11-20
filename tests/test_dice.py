"""DICE: artist-page parsing, sitemap slug matching, and when a crawl is due."""

import datetime as dt
from pathlib import Path

import pytest
from conftest import make_user

import poghiamo.sources.dice as dice
from poghiamo.database.models import Artist, Follow, ScanLog
from poghiamo.sources.base import ArtistRef

_FIXTURE = (Path(__file__).parent / "fixtures" / "dice_artist.html").read_text()
NOW = dt.datetime(2026, 11, 20, 3, 0)


def _page(artist_name, sections):
    import json

    data = {"props": {"pageProps": {"initialProfile": {"artist": {"name": artist_name}, "sections": sections}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def _event(eid, date, city, country="IT"):
    return {
        "id": eid,
        "perm_name": f"show-{eid}",
        "name": "Show",
        "dates": {"event_start_date": date},
        "venues": [{"name": "Venue", "city": {"name": city, "country_code": country}}],
    }


# --- page parsing ---


def test_parse_fixture_keeps_only_italy():
    events = dice.parse_artist_page(_FIXTURE, "faccianuvola")
    assert len(events) == 1
    e = events[0]
    assert (e.source, e.city, e.venue, e.date) == ("dice", "Milano", "Circolo Magnolia", dt.date(2026, 9, 17))
    assert (e.lat, e.lon) == (45.45, 9.28)
    assert e.ticket_url == "https://dice.fm/event/test-show-it1"


def test_parse_reads_the_items_shape_too():
    html = _page("Rares", [{"items": [{"event": _event("e1", "2026-12-05T21:00:00+01:00", "Torino")}, {"text": "x"}]}])
    events = dice.parse_artist_page(html, "rares")
    assert [(e.city, e.date) for e in events] == [("Torino", dt.date(2026, 12, 5))]


def test_parse_rejects_a_page_for_another_artist():
    html = _page("Somebody Else", [{"events": [_event("e1", "2026-12-05T21:00:00+01:00", "Torino")]}])
    assert dice.parse_artist_page(html, "rares") == []


def test_parse_accepts_spellings_that_slugify_alike():
    html = _page("43 Nove", [{"events": [_event("e1", "2026-12-05T21:00:00+01:00", "Roma")]}])
    assert len(dice.parse_artist_page(html, "43.nove")) == 1


# --- slug matching ---


def test_slug_keys_cover_dice_spellings():
    assert {"dove-liana", "dov-e-liana"} <= dice.slug_keys("Dov'è Liana")
    assert dice.slug_keys("Ex-Otago") == {"ex-otago"}
    assert dice.slug_keys("43.Nove") == {"43-nove"}
    assert {"simon-and-garfunkel", "simon-garfunkel"} <= dice.slug_keys("Simon & Garfunkel")


def test_match_slugs_strips_the_id_and_caps_homonyms():
    slugs = [
        "faccianuvola-8yaw5",
        "faccianuvola-band-x1y2z",  # a different base, must not match
        "rares-4bwm",
        "rares-aaaa1",
        "rares-bbbb2",
        "rares-cccc3",  # fourth homonym, over the cap
        "rares",  # no id suffix, not a slug shape we know
    ]
    found = dice.match_slugs(slugs, {"faccianuvola": {1}, "rares": {2}, "nobody": {3}})
    assert found == {1: ["faccianuvola-8yaw5"], 2: ["rares-4bwm", "rares-aaaa1", "rares-bbbb2"]}


# --- adapter ---


class _Resp:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise dice.requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(dice, "_rl", dice.RateLimiter(0))
    monkeypatch.setattr(dice, "_sitemap_rl", dice.RateLimiter(0))


def test_adapter_fetches_only_matched_artists(db, monkeypatch, no_wait):
    matched = Artist(name="Faccianuvola", name_normalized="faccianuvola", dice_slug="faccianuvola-8yaw5,faccianuvola-zzzz9")
    unmatched = Artist(name="Rares", name_normalized="rares")
    db.add_all([matched, unmatched])
    db.commit()
    pages = {
        "https://dice.fm/artist/faccianuvola-8yaw5": _Resp(text=_FIXTURE),
        "https://dice.fm/artist/faccianuvola-zzzz9": _Resp(status=404),  # gone: ignored
    }
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return pages[url]

    monkeypatch.setattr(dice.requests, "get", fake_get)
    adapter = dice.DiceAdapter()

    assert adapter.should_run(db, unmatched) is False
    assert adapter.should_run(db, matched) is True
    events = adapter.fetch(ArtistRef(id=matched.id, name=matched.name, name_normalized=matched.name_normalized))
    assert [e.city for e in events] == ["Milano"]
    assert calls == list(pages)


def test_pipeline_skips_dice_for_unmatched_artists(db, monkeypatch):
    from poghiamo.services import pipeline

    artist = Artist(name="Rares", name_normalized="rares")
    db.add(artist)
    db.commit()
    monkeypatch.setattr(pipeline, "enabled_adapters", lambda: [dice.DiceAdapter()])
    summary = pipeline.scan_artist(db, artist)
    assert summary["dice"]["status"] == "skipped"


# --- when a crawl is due ---


def _follow(db, user, name, created_days_ago, dice_slug=None):
    a = Artist(
        name=name,
        name_normalized=name.lower(),
        dice_slug=dice_slug,
        created_at=NOW - dt.timedelta(days=created_days_ago),
        last_scanned_at=NOW - dt.timedelta(days=1),
    )
    db.add(a)
    db.flush()
    db.add(Follow(user_id=user.id, artist_id=a.id, state="active"))
    db.commit()
    return a


def _crawl(*slugs):
    calls = []

    def crawl():
        calls.append(1)
        return iter(slugs)

    return crawl, calls


def test_nothing_to_do_without_unmatched_artists(db):
    user = make_user(db)
    _follow(db, user, "Rares", 10, dice_slug="rares-4bwm")
    crawl, calls = _crawl()
    assert dice.resolve_slugs(db, now=NOW, crawl=crawl) is None
    assert calls == []


def test_first_crawl_matches_and_makes_the_artist_due(db):
    user = make_user(db)
    rares = _follow(db, user, "Rares", 10)
    crawl, calls = _crawl("rares-4bwm", "someone-else-1234")

    assert dice.resolve_slugs(db, now=NOW, crawl=crawl) == {"pending": 1, "matched": 1}
    db.refresh(rares)
    assert rares.dice_slug == "rares-4bwm"
    assert rares.last_scanned_at is None  # picked up by the next sweep
    log = db.query(ScanLog).one()
    assert (log.source, log.status, log.found) == ("dice_sitemap", "ok", 1)


def test_crawl_cadence(db):
    user = make_user(db)
    _follow(db, user, "Nobody On Dice", 40)
    db.add(ScanLog(source="dice_sitemap", status="ok", found=0, ran_at=NOW - dt.timedelta(days=2)))
    db.commit()
    crawl, calls = _crawl()

    # Unmatched artist that was already looked for two days ago: wait for the monthly retry.
    assert dice.resolve_slugs(db, now=NOW, crawl=crawl) is None
    # A newcomer followed after the last crawl is looked up at the next night...
    newcomer = _follow(db, user, "Rares", 0.5)
    assert dice.resolve_slugs(db, now=NOW, crawl=crawl) == {"pending": 2, "matched": 0}
    assert len(calls) == 1
    # ...but not twice in one day, however many newcomers arrive.
    later = NOW + dt.timedelta(hours=3)
    newcomer.created_at = later - dt.timedelta(minutes=5)
    db.commit()
    assert dice.resolve_slugs(db, now=later, crawl=crawl) is None
    # And everything unmatched is retried after RECRAWL_DAYS.
    month = NOW + dt.timedelta(days=dice.RECRAWL_DAYS)
    assert dice.resolve_slugs(db, now=month, crawl=crawl) is not None
    assert len(calls) == 2


def test_failed_crawl_is_logged_and_changes_nothing(db):
    user = make_user(db)
    rares = _follow(db, user, "Rares", 10)

    def broken():
        raise RuntimeError("sitemap index unreachable")

    with pytest.raises(RuntimeError):
        dice.resolve_slugs(db, now=NOW, crawl=broken)
    db.refresh(rares)
    assert rares.dice_slug is None
    log = db.query(ScanLog).one()
    assert (log.source, log.status, log.message) == ("dice_sitemap", "error", "sitemap index unreachable")


def test_crawl_sitemap_reads_index_and_files(monkeypatch, no_wait):
    index = (
        "<sitemapindex><sitemap><loc>https://dice.fm/sitemaps/artists/sitemap1.xml</loc></sitemap>"
        "<sitemap><loc>https://dice.fm/sitemaps/artists/sitemap2.xml</loc></sitemap></sitemapindex>"
    )
    files = {
        "https://dice.fm/sitemaps/artists/sitemap.xml": _Resp(text=index),
        "https://dice.fm/sitemaps/artists/sitemap1.xml": _Resp(
            text="<urlset><url><loc>https://dice.fm/artist/rares-4bwm</loc></url>"
            "<url><loc>https://dice.fm/venue/not-an-artist-1234</loc></url></urlset>"
        ),
        "https://dice.fm/sitemaps/artists/sitemap2.xml": _Resp(status=500),  # skipped
    }

    class FakeSession:
        headers = {}

        def get(self, url, **kwargs):
            return files[url]

    monkeypatch.setattr(dice.requests, "Session", FakeSession)
    assert list(dice.crawl_sitemap()) == ["rares-4bwm"]
