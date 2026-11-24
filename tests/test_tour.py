"""Guided tour: starts at login until finished or skipped, replayable from settings."""

from sqlalchemy import inspect, text

from conftest import login, make_invite, make_user, signup
from poghiamo.database.models import Artist, Follow, User


def test_signup_starts_the_tour(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin)
    resp = signup(client, invite_code=invite.token)
    assert resp.headers["location"] == "/settings?tour=1"


def test_tour_keeps_starting_until_done_even_after_setting_zones(client, db):
    # Setting zones half-way through used to count as "onboarded"; only
    # finishing or skipping the tour does now.
    user = make_user(db)
    user.regions = ["Toscana"]
    db.commit()
    assert login(client, "alice", "testpass123").headers["location"] == "/settings?tour=1"


def test_finishing_the_tour_stops_it_and_keeps_the_first_date(client, db):
    make_user(db)
    login(client, "alice", "testpass123")

    assert client.post("/tour/done").status_code == 204
    first = db.query(User).one().tour_done_at
    assert first is not None

    client.post("/tour/done")  # replayed from Impostazioni
    db.expire_all()
    assert db.query(User).one().tour_done_at == first

    client.post("/logout")
    assert login(client, "alice", "testpass123").headers["location"] == "/calendario"


def test_marking_the_tour_done_needs_a_login(client):
    resp = client.post("/tour/done", follow_redirects=False)
    assert resp.headers["location"] == "/login"


def test_pages_carry_the_tour_and_settings_can_replay_it(client, db):
    make_user(db)
    login(client, "alice", "testpass123")
    page = client.get("/settings").text
    assert 'href="/settings?tour=1"' in page  # Rivedi
    assert 'fetch("/tour/done"' in page
    assert '"/calendario?tour=4"' in page  # last step, with the install hint


def test_migration_marks_set_up_accounts_as_done():
    from poghiamo.database.engine import SessionLocal, _run_migrations, engine

    with SessionLocal() as s:
        zones, follows, fresh = (User(username=n, hashed_password="x") for n in ("z", "f", "n"))
        zones.regions = ["Lazio"]
        artist = Artist(name="Rares", name_normalized="rares")
        s.add_all([zones, follows, fresh, artist])
        s.flush()
        s.add(Follow(user_id=follows.id, artist_id=artist.id, state="active"))
        s.commit()

    # Put users back the way it was before the column existed.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users DROP COLUMN tour_done_at"))

    _run_migrations()

    assert "tour_done_at" in [c["name"] for c in inspect(engine).get_columns("users")]
    with engine.begin() as conn:
        rows = dict(conn.execute(text("SELECT username, tour_done_at IS NOT NULL FROM users")).all())
    assert rows == {"z": 1, "f": 1, "n": 0}
    _run_migrations()  # idempotent
