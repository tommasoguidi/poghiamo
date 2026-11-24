"""Artists: search proxy, add/dedup, unfollow persistence, regions, enrichment."""

from datetime import datetime, timezone

import pytest
import requests
from conftest import login, make_user

from poghiamo.database.models import Artist, Follow, User
from poghiamo.services import artist_search


class _MBResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never hit Deezer or MusicBrainz; retry bookkeeping starts clean
    (artist ids restart per test, the module-level dict would not)."""
    artist_search._last_attempt.clear()
    monkeypatch.setattr(
        artist_search,
        "search_deezer",
        lambda q, limit=8: [
            artist_search.ArtistSuggestion(
                deezer_id=42, name="Faccianuvola", image_url="https://img.example/x.jpg"
            )
        ],
    )
    monkeypatch.setattr(artist_search, "search_musicbrainz", lambda name: None)


def _login_user(client, db, username="alice"):
    user = make_user(db, username=username, password="testpass123")
    login(client, username, "testpass123")
    return user


def test_api_search_requires_login(client):
    resp = client.get("/api/artists/search?q=fac", follow_redirects=False)
    assert resp.status_code == 303


def test_api_search_returns_suggestions(client, db):
    _login_user(client, db)
    resp = client.get("/api/artists/search?q=fac")
    assert resp.status_code == 200
    assert resp.json()["results"][0]["name"] == "Faccianuvola"


def test_api_search_short_query_returns_empty(client, db):
    _login_user(client, db)
    assert client.get("/api/artists/search?q=f").json() == {"results": []}


def test_add_artist_creates_follow_and_enriches_async(client, db):
    _login_user(client, db)
    resp = client.post(
        "/artists/add",
        data={"name": "Faccianuvola", "deezer_id": "42", "image_url": "https://img.example/x.jpg"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    artist = db.query(Artist).one()
    assert artist.deezer_id == 42
    assert artist.name_normalized == "faccianuvola"
    # Background enrichment ran after the response (search_musicbrainz → None → unmatched)
    assert artist.resolution_status == "unmatched"

    follow = db.query(Follow).one()
    assert follow.state == "active" and follow.source == "manual"
    # Back on the list, pointed at the artist just added.
    assert resp.headers["location"] == f"/artists?added={artist.id}"
    assert "Faccianuvola" in client.get("/artists").text


def test_artists_page_has_one_search_box_for_filtering_and_adding(client, db):
    _login_user(client, db)
    page = client.get("/artists").text
    assert page.count('type="search"') == 1
    assert "Non ami ancora nessun artista" in page  # empty state points at the box
    assert 'id="add-section"' in page  # "Da aggiungere" results, below the list

    client.post("/artists/add", data={"name": "Faccianuvola"}, follow_redirects=False)
    page = client.get("/artists").text
    assert page.count('type="search"') == 1
    assert "I tuoi artisti (1)" in page


def test_add_dedupes_across_users_and_free_text(client, db):
    _login_user(client, db)
    client.post(
        "/artists/add", data={"name": "Faccianuvola", "deezer_id": "42"}, follow_redirects=False
    )
    client.post("/logout", follow_redirects=False)

    make_user(db, username="bob", password="testpass123")
    login(client, "bob", "testpass123")
    # Free-text add, different casing: must reuse the same canonical artist
    client.post("/artists/add", data={"name": "  faccianuvola "}, follow_redirects=False)

    assert db.query(Artist).count() == 1
    assert db.query(Follow).count() == 2


def test_adding_an_artist_never_scans_from_the_web(client, db, monkeypatch):
    """The web only records the artist; the scheduler's new-artist lane does
    the first scan, so all source traffic leaves a single process."""
    from poghiamo.services import pipeline

    def must_not_scan(*args, **kwargs):
        raise AssertionError("the web app must not scan")

    monkeypatch.setattr(pipeline, "scan_artist", must_not_scan)
    _login_user(client, db)
    client.post(
        "/artists/add", data={"name": "Faccianuvola", "deezer_id": "42"}, follow_redirects=False
    )
    client.post(
        "/events/create",
        data={"artist_name": "Rares", "date": "2030-05-01", "city": "Torino"},
        follow_redirects=False,
    )

    # Both new artists wait, never scanned, for the scheduler's new lane.
    pending = {a.name for a in pipeline.new_artists_to_scan(db, limit=10)}
    assert pending == {"Faccianuvola", "Rares"}


def test_artist_page_says_the_search_is_running_until_the_first_scan(client, db):
    _login_user(client, db)
    client.post("/artists/add", data={"name": "Faccianuvola"}, follow_redirects=False)
    artist = db.query(Artist).one()

    assert "Sto cercando le date" in client.get(f"/artists/{artist.id}").text

    artist.last_scanned_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    page = client.get(f"/artists/{artist.id}").text
    assert "Sto cercando le date" not in page
    assert "Nessuna data in programma" in page


def test_unfollow_persists_and_refollow_reactivates(client, db):
    _login_user(client, db)
    client.post(
        "/artists/add", data={"name": "Faccianuvola", "deezer_id": "42"}, follow_redirects=False
    )
    artist = db.query(Artist).one()

    client.post(f"/artists/{artist.id}/unfollow", follow_redirects=False)
    follow = db.query(Follow).one()
    db.refresh(follow)
    assert follow.state == "removed" and follow.removed_at is not None
    assert "Faccianuvola" not in client.get("/artists").text

    # Re-adding reactivates the SAME row: no duplicate follows ever
    client.post(
        "/artists/add", data={"name": "Faccianuvola", "deezer_id": "42"}, follow_redirects=False
    )
    assert db.query(Follow).count() == 1
    db.refresh(follow)
    assert follow.state == "active" and follow.removed_at is None


def test_unfollow_of_not_followed_artist_is_noop(client, db):
    _login_user(client, db)
    resp = client.post("/artists/999/unfollow", follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(Follow).count() == 0


def test_settings_page_embeds_area_picker_data(client, db):
    _login_user(client, db)
    page = client.get("/settings").text
    assert "Zone di interesse" in page
    # The picker dataset is embedded as JSON: regions and provinces searchable
    assert "Firenze (FI)" in page
    assert "Sud Sardegna (SU)" in page
    assert "Toscana" in page


def test_areas_roundtrip_filters_unknown(client, db):
    user = _login_user(client, db)
    resp = client.post(
        "/settings/regions",
        data={"regions": ["Toscana", "Lazio", "Atlantide"], "provinces": ["MI", "XX"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(user)
    assert user.regions == ["Toscana", "Lazio"]
    assert user.provinces == ["MI"]

    # Clearing the selection means "all of Italy"
    client.post("/settings/regions", data={}, follow_redirects=False)
    db.refresh(user)
    assert user.regions == []
    assert user.provinces == []


def test_status_endpoint_reports_and_heals_stuck_pending(client, db, monkeypatch):
    """A 'pending' artist whose enrichment task was lost gets retried when the
    status endpoint is polled, and resolves."""
    _login_user(client, db)
    client.post("/artists/add", data={"name": "Tamango"}, follow_redirects=False)
    artist = db.query(Artist).one()
    artist.resolution_status = "pending"  # simulate a lost background task
    db.commit()

    monkeypatch.setattr(
        artist_search,
        "search_musicbrainz",
        lambda name: artist_search.MusicBrainzMatch(
            mbid="mb-tamango", name="Tamango", country="FR", score=100
        ),
    )
    artist_search._last_attempt.clear()  # bypass the once-a-minute retry guard

    resp = client.get("/api/artists/status")
    payload = resp.json()["artists"]
    assert payload[0]["id"] == artist.id and payload[0]["status"] == "pending"

    # The re-scheduled background task ran after the response
    db.refresh(artist)
    assert artist.resolution_status == "resolved"
    assert artist.mbid == "mb-tamango"

    # Next poll reports the settled status
    assert client.get("/api/artists/status").json()["artists"][0]["status"] == "resolved"


def test_status_endpoint_retry_is_rate_limited(client, db, monkeypatch):
    _login_user(client, db)
    client.post("/artists/add", data={"name": "Tamango"}, follow_redirects=False)
    artist = db.query(Artist).one()
    artist.resolution_status = "pending"
    db.commit()

    calls = []
    monkeypatch.setattr(
        artist_search, "enrich_artist", lambda artist_id: calls.append(artist_id)
    )
    artist_search._last_attempt.clear()
    client.get("/api/artists/status")
    client.get("/api/artists/status")  # within the retry interval: no second attempt
    assert calls == [artist.id]


def test_enrich_artist_sets_mbid(db, monkeypatch):
    artist = Artist(name="Faccianuvola", name_normalized="faccianuvola")
    db.add(artist)
    db.commit()
    db.refresh(artist)

    monkeypatch.setattr(
        artist_search,
        "search_musicbrainz",
        lambda name: artist_search.MusicBrainzMatch(
            mbid="abc-123", name="faccianuvola", country="IT", score=100
        ),
    )
    artist_search.enrich_artist(artist.id)

    db.refresh(artist)
    assert artist.mbid == "abc-123"
    assert artist.country == "IT"
    assert artist.resolution_status == "resolved"


def test_enrich_artist_duplicate_mbid_left_unmatched(db, monkeypatch):
    db.add(Artist(name="Original", name_normalized="original", mbid="abc-123"))
    dup = Artist(name="Duplicate", name_normalized="duplicate")
    db.add(dup)
    db.commit()
    db.refresh(dup)

    monkeypatch.setattr(
        artist_search,
        "search_musicbrainz",
        lambda name: artist_search.MusicBrainzMatch(
            mbid="abc-123", name="Duplicate", country="IT", score=100
        ),
    )
    artist_search.enrich_artist(dup.id)

    db.refresh(dup)
    assert dup.mbid is None
    assert dup.resolution_status == "unmatched"


def test_musicbrainz_retries_on_503_then_succeeds(monkeypatch):
    # MusicBrainz search 503s under load; a retry with backoff clears it instead
    # of leaving the artist stuck 'pending'. (_mb_request is tested directly: the
    # autouse fixture stubs out search_musicbrainz.)
    monkeypatch.setattr(artist_search.time, "sleep", lambda *a, **k: None)
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _MBResp(503) if calls["n"] == 1 else _MBResp(200, {"artists": []})

    monkeypatch.setattr(artist_search.requests, "get", fake_get)
    resp = artist_search._mb_request("X")
    assert calls["n"] == 2  # first 503, retried, then 200
    assert resp.status_code == 200


def test_musicbrainz_request_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(artist_search.time, "sleep", lambda *a, **k: None)
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _MBResp(503)

    monkeypatch.setattr(artist_search.requests, "get", fake_get)
    resp = artist_search._mb_request("X")
    assert calls["n"] == 1 + artist_search._MB_RETRIES  # bounded number of tries
    assert resp.status_code == 503  # caller's raise_for_status then leaves it pending
