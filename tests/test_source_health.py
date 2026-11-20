"""Source health summary for the admin settings page."""

import datetime as dt

from conftest import login, make_user

from poghiamo.database.models import ScanLog
from poghiamo.services.source_health import ago_it, source_health

NOW = dt.datetime(2026, 11, 20, 12, 0)


class FakeAdapter:
    def __init__(self, name, enabled=True):
        self.name = name
        self.enabled = enabled


def _log(db, source, status, days_ago=0.0, found=0, message=None, via=None):
    db.add(
        ScanLog(
            source=source,
            status=status,
            found=found,
            message=message,
            via=via,
            ran_at=NOW - dt.timedelta(days=days_ago),
        )
    )


def test_states_follow_the_scan_log(db):
    _log(db, "steady", "ok", days_ago=3, found=2)
    _log(db, "steady", "ok", days_ago=1, found=3)

    _log(db, "flaky", "ok", days_ago=2)
    _log(db, "flaky", "timeout", days_ago=1)

    # Last success outside the window, then three failures: broken.
    _log(db, "dead", "ok", days_ago=60)
    _log(db, "dead", "error", days_ago=3, message="403 Client Error")
    _log(db, "dead", "error", days_ago=2, message="403 Client Error")
    _log(db, "dead", "error", days_ago=1, message="403 Forbidden, latest")

    # Skipped runs never reached the source: they must not hide a success.
    _log(db, "budget", "ok", days_ago=5, found=1)
    _log(db, "budget", "skipped", days_ago=2)
    _log(db, "budget", "skipped", days_ago=1)

    _log(db, "off", "error", days_ago=1)

    # A long outage is counted in full, not capped.
    for i in range(25):
        _log(db, "outage", "error", days_ago=20 - i * 0.5)
    db.commit()

    adapters = [FakeAdapter(n) for n in ("steady", "flaky", "dead", "budget", "quiet", "outage")]
    adapters.append(FakeAdapter("off", enabled=False))
    health = {h["name"]: h for h in source_health(db, adapters=adapters, now=NOW)}

    steady = health["steady"]
    assert steady["state"] == "ok"
    assert (steady["ok"], steady["failed"], steady["found"]) == (2, 0, 5)
    assert steady["last_ok"] == NOW - dt.timedelta(days=1)

    assert health["flaky"]["state"] == "failing"
    assert health["flaky"]["failures_in_a_row"] == 1

    dead = health["dead"]
    assert dead["state"] == "broken"
    assert dead["failures_in_a_row"] == 3
    assert (dead["ok"], dead["failed"]) == (0, 3)  # the old success is outside the window
    assert dead["last_ok"] == NOW - dt.timedelta(days=60)
    assert dead["last_error"] == "403 Forbidden, latest"

    budget = health["budget"]
    assert budget["state"] == "ok"
    assert budget["skipped"] == 2
    assert budget["last_run"] == NOW - dt.timedelta(days=5)

    outage = health["outage"]
    assert (outage["state"], outage["failures_in_a_row"], outage["last_ok"]) == ("broken", 25, None)

    assert health["quiet"]["state"] == "never"
    assert health["quiet"]["last_ok"] is None
    assert health["off"]["state"] == "off"
    # Registry order is kept.
    assert [h["name"] for h in source_health(db, adapters=adapters, now=NOW)][:2] == [
        "steady",
        "flaky",
    ]


def test_rockol_notes_show_transport_split_and_budget(db, monkeypatch):
    import poghiamo.sources.rockol as rockol

    monkeypatch.setattr(rockol, "ROCKOL_PROXY_URL", "http://10.77.0.2:8888")
    monkeypatch.setattr(rockol, "ZENROWS_API_KEY", "key")
    monkeypatch.setattr(rockol, "ROCKOL_MONTHLY_BUDGET", 180)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for via in ("home", "home", "zenrows", "zenrows", "zenrows"):
        db.add(ScanLog(source="rockol", status="ok", found=1, via=via, ran_at=now))
    db.commit()

    notes = source_health(db, adapters=[FakeAdapter("rockol")], now=now)[0]["notes"]
    assert "Proxy di casa: configurato" in notes
    assert "Ultimi 30 giorni: 2 via casa, 3 via ZenRows" in notes
    assert "ZenRows questo mese: 3 su 180" in notes


def test_dice_notes_show_matches_and_last_crawl(db):
    from poghiamo.database.models import Artist, Follow

    user = make_user(db)
    for name, slug in (("Rares", "rares-4bwm"), ("Nobody", None), ("Unfollowed", "unf-1234")):
        a = Artist(name=name, name_normalized=name.lower(), dice_slug=slug)
        db.add(a)
        db.flush()
        if name != "Unfollowed":
            db.add(Follow(user_id=user.id, artist_id=a.id, state="active"))
    db.commit()

    notes = source_health(db, adapters=[FakeAdapter("dice")], now=NOW)[0]["notes"]
    assert notes == ["Artisti seguiti trovati su DICE: 1 su 2", "Sitemap: mai letta"]

    _log(db, "dice_sitemap", "ok", days_ago=3, found=1)
    db.commit()
    notes = source_health(db, adapters=[FakeAdapter("dice")], now=NOW)[0]["notes"]
    assert notes[1] == "Sitemap: letta 3 giorni fa"


def test_ago_it():
    assert ago_it(None, NOW) == "mai"
    assert ago_it(NOW - dt.timedelta(seconds=20), NOW) == "adesso"
    assert ago_it(NOW - dt.timedelta(minutes=5), NOW) == "5 min fa"
    assert ago_it(NOW - dt.timedelta(hours=1), NOW) == "1 ora fa"
    assert ago_it(NOW - dt.timedelta(hours=7), NOW) == "7 ore fa"
    assert ago_it(NOW - dt.timedelta(hours=30), NOW) == "ieri"
    assert ago_it(NOW - dt.timedelta(days=12), NOW) == "12 giorni fa"


def test_settings_shows_source_health_to_admins_only(client, db):
    make_user(db, username="admin", password="testpass123", is_admin=True)
    make_user(db, username="bob", password="testpass123")
    db.add(ScanLog(source="dice", status="error", message="403 Client Error"))
    db.commit()

    login(client, "admin", "testpass123")
    page = client.get("/settings").text
    assert "Stato delle sorgenti" in page
    assert "DICE" in page and "Ticketmaster" in page and "rockol" in page

    client.post("/logout")
    login(client, "bob", "testpass123")
    assert "Stato delle sorgenti" not in client.get("/settings").text
