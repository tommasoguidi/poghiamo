"""Scheduler lanes: only the scheduler scans, one artist per tick."""

import datetime as dt

import pytest
from conftest import make_user

from poghiamo.database.models import Artist, Follow
from poghiamo.scheduler import runner
from poghiamo.services import pipeline

NOW = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


@pytest.fixture
def scans(monkeypatch):
    """Record which artists get scanned; `results` maps an id to the value the
    fake scan returns (None = failure), default a summary dict."""
    calls, results = [], {}

    def fake_scan(artist_id):
        calls.append(artist_id)
        return results.get(artist_id, {"rockol": {"status": "ok"}})

    monkeypatch.setattr(pipeline, "scan_artist_by_id", fake_scan)
    monkeypatch.setattr(runner, "_new_artist_retry_at", {})
    return calls, results


def _artist(db, user, name, *, created_days_ago, scanned_days_ago=None, followed=True):
    a = Artist(
        name=name,
        name_normalized=name.lower(),
        created_at=NOW - dt.timedelta(days=created_days_ago),
        last_scanned_at=None if scanned_days_ago is None else NOW - dt.timedelta(days=scanned_days_ago),
    )
    db.add(a)
    db.flush()
    if followed:
        db.add(Follow(user_id=user.id, artist_id=a.id, state="active"))
    db.commit()
    return a


def test_new_lane_scans_one_never_scanned_artist_per_tick(db, scans):
    calls, _ = scans
    user = make_user(db)
    older = _artist(db, user, "Older", created_days_ago=2)
    newer = _artist(db, user, "Newer", created_days_ago=1)
    _artist(db, user, "Scanned", created_days_ago=3, scanned_days_ago=1)
    _artist(db, user, "Unfollowed", created_days_ago=5, followed=False)

    runner.run_new_artist()
    assert calls == [older.id]  # one per tick, longest waiting first

    calls.clear()
    db.get(Artist, older.id).last_scanned_at = NOW
    db.commit()
    runner.run_new_artist()
    assert calls == [newer.id]


def test_new_lane_keeps_a_failing_artist_out_for_a_while(db, scans, monkeypatch):
    calls, results = scans
    user = make_user(db)
    broken = _artist(db, user, "Broken", created_days_ago=2)
    other = _artist(db, user, "Other", created_days_ago=1)
    results[broken.id] = None  # scan_artist_by_id swallowed an internal error

    clock = [1000.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    runner.run_new_artist()
    runner.run_new_artist()
    assert calls == [broken.id, other.id]  # not retried in a tight loop

    clock[0] += runner.NEW_ARTIST_RETRY_SECONDS + 1
    calls.clear()
    runner.run_new_artist()
    assert calls == [broken.id]  # retried once the pause is over


def test_refresh_lane_rescans_one_due_artist_oldest_first(db, scans, monkeypatch):
    calls, _ = scans
    monkeypatch.setattr(runner, "SCAN_INTERVAL_DAYS", 7)
    user = make_user(db)
    _artist(db, user, "Fresh", created_days_ago=30, scanned_days_ago=2)
    oldest = _artist(db, user, "Oldest", created_days_ago=30, scanned_days_ago=20)
    _artist(db, user, "Older", created_days_ago=30, scanned_days_ago=9)

    runner.run_refresh()
    assert calls == [oldest.id]


def test_refresh_lane_idles_when_nothing_is_due(db, scans):
    calls, _ = scans
    user = make_user(db)
    _artist(db, user, "Fresh", created_days_ago=30, scanned_days_ago=1)
    runner.run_refresh()
    assert calls == []


def test_a_failing_job_does_not_stop_the_loop(caplog):
    def boom():
        raise RuntimeError("db is locked")

    runner._guarded(boom)  # must not raise
    assert "boom failed" in caplog.text
