"""Artists: search proxy, add/dedup, unfollow persistence, regions, enrichment."""

import pytest
from conftest import login, make_user

from poghiamo.database.models import Artist, Follow, User
from poghiamo.services import artist_search


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
    assert "Faccianuvola" in client.get("/artists").text


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


def test_new_artist_add_triggers_background_scan(client, db, monkeypatch):
    """Adding a brand-new artist fires a background scan with its id; following
    an already-known artist does not."""
    import poghiamo.webapp.app as app_mod

    scanned = []
    monkeypatch.setattr(app_mod, "scan_artist_by_id", lambda artist_id: scanned.append(artist_id))
    _login_user(client, db)

    client.post(
        "/artists/add", data={"name": "Faccianuvola", "deezer_id": "42"}, follow_redirects=False
    )
    artist = db.query(Artist).one()
    assert scanned == [artist.id]  # scheduled once, ran after the response

    # A second user follows the same (now-existing) artist: no new scan.
    client.post("/logout", follow_redirects=False)
    make_user(db, username="bob", password="testpass123")
    login(client, "bob", "testpass123")
    client.post("/artists/add", data={"name": "  faccianuvola "}, follow_redirects=False)
    assert scanned == [artist.id]  # unchanged


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
