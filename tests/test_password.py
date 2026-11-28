"""Password change, session versioning, admin reset links and the admin CLI."""

import hashlib
import logging
import re
from datetime import timedelta
from types import SimpleNamespace

from conftest import login, make_user
from fastapi.testclient import TestClient

from poghiamo import cli
from poghiamo.database.models import PasswordResetToken, User
from poghiamo.scheduler import runner
from poghiamo.services import password_reset
from poghiamo.services.feed import now_utc_naive
from poghiamo.webapp.app import _RedactResetToken, app
from poghiamo.webapp.auth import get_current_user, verify_password


def change(client, current, new, confirm=None):
    return client.post(
        "/account/password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": confirm if confirm is not None else new,
        },
        follow_redirects=False,
    )


def logged_in(client) -> bool:
    return client.get("/settings", follow_redirects=False).status_code == 200


def admin_reset_link(client, user_id, password="testpass123"):
    return client.post(
        f"/admin/users/{user_id}/reset-link", data={"password": password}, follow_redirects=False
    )


def link_token(client) -> str:
    """Read the once-shown reset link off the settings page."""
    match = re.search(r"/reset\?token=([A-Za-z0-9_-]+)", client.get("/settings").text)
    assert match, "no reset link on the settings page"
    return match.group(1)


def reset(client, token, new="brandnew1", confirm=None):
    return client.post(
        "/reset",
        data={
            "token": token,
            "new_password": new,
            "confirm_password": confirm if confirm is not None else new,
        },
        follow_redirects=False,
    )


# --- change password ---


def test_change_password(client, db):
    alice = make_user(db, username="alice", password="testpass123")
    login(client, "alice", "testpass123")
    resp = change(client, "testpass123", "brandnew1")
    assert resp.headers["location"] == "/settings?pw=ok#password"
    db.refresh(alice)
    assert verify_password("brandnew1", alice.hashed_password)
    assert "Password cambiata" in client.get("/settings?pw=ok").text


def test_change_password_wrong_current_changes_nothing(client, db):
    alice = make_user(db, username="alice", password="testpass123")
    login(client, "alice", "testpass123")
    resp = change(client, "nope-nope", "brandnew1")
    assert resp.headers["location"] == "/settings?pw=wrong#password"
    db.refresh(alice)
    assert verify_password("testpass123", alice.hashed_password)
    assert "Password attuale errata" in client.get("/settings?pw=wrong").text


def test_change_password_validation(client, db):
    make_user(db, username="alice", password="testpass123")
    login(client, "alice", "testpass123")
    assert change(client, "testpass123", "brandnew1", "different1").headers["location"].endswith(
        "pw=mismatch#password"
    )
    assert change(client, "testpass123", "short").headers["location"].endswith("pw=short#password")
    assert change(client, "testpass123", "é" * 40).headers["location"].endswith("pw=long#password")


def test_change_password_logs_out_other_devices_only(client, db):
    make_user(db, username="alice", password="testpass123")
    login(client, "alice", "testpass123")
    with TestClient(app) as phone:
        login(phone, "alice", "testpass123")
        assert logged_in(phone)
        change(client, "testpass123", "brandnew1")
        assert logged_in(client)
        assert not logged_in(phone)


def test_change_password_requires_login(client):
    resp = change(client, "a", "brandnew1")
    assert resp.headers["location"] == "/login"


# --- session versioning ---


def test_session_without_version_still_valid(db):
    """Cookies issued before session versioning carry no "sv": the deploy must
    not log anybody out."""
    alice = make_user(db, username="alice")
    request = SimpleNamespace(session={"user_id": alice.id})
    assert get_current_user(request, db).id == alice.id


def test_stale_session_version_is_dropped(db):
    alice = make_user(db, username="alice")
    alice.session_version = 2
    db.commit()
    request = SimpleNamespace(session={"user_id": alice.id, "sv": 1})
    assert get_current_user(request, db) is None
    assert request.session == {}


def test_logout_everywhere(client, db):
    make_user(db, username="alice", password="testpass123")
    login(client, "alice", "testpass123")
    with TestClient(app) as phone:
        login(phone, "alice", "testpass123")
        resp = client.post("/logout/all", follow_redirects=False)
        assert resp.headers["location"] == "/login"
        assert not logged_in(client)
        assert not logged_in(phone)
    # Logging in again works as usual.
    login(client, "alice", "testpass123")
    assert logged_in(client)


# --- admin reset links ---


def test_reset_link_flow(client, db):
    make_user(db, username="admin", password="testpass123", is_admin=True)
    bob = make_user(db, username="bob", password="oldpass123")
    with TestClient(app) as bobs_phone:
        login(bobs_phone, "bob", "oldpass123")

        login(client, "admin", "testpass123")
        resp = admin_reset_link(client, bob.id)
        assert resp.headers["location"] == "/settings#utenti"
        token = link_token(client)
        # Shown once: a reload no longer has it.
        assert "/reset?token=" not in client.get("/settings").text
        # Only the hash is stored.
        row = db.query(PasswordResetToken).one()
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in row.token_hash

        with TestClient(app) as browser:
            page = browser.get(f"/reset?token={token}")
            assert "bob" in page.text and "Salva e accedi" in page.text
            resp = reset(browser, token)
            assert resp.status_code == 303
            assert logged_in(browser)

        # The old password is gone, the old session too.
        assert not logged_in(bobs_phone)
        db.refresh(bob)
        assert verify_password("brandnew1", bob.hashed_password)
        assert db.query(PasswordResetToken).count() == 0

    # Single use.
    with TestClient(app) as other:
        assert "Link non valido" in reset(other, token, new="another1").text
    db.refresh(bob)
    assert verify_password("brandnew1", bob.hashed_password)


def test_reset_link_needs_admin_password(client, db):
    make_user(db, username="admin", password="testpass123", is_admin=True)
    bob = make_user(db, username="bob")
    login(client, "admin", "testpass123")
    resp = admin_reset_link(client, bob.id, password="nope")
    assert resp.headers["location"] == "/settings?error=Password+errata"
    assert db.query(PasswordResetToken).count() == 0


def test_non_admin_cannot_issue_reset_link(client, db):
    make_user(db, username="admin", is_admin=True)
    make_user(db, username="bob", password="testpass123")
    victim = make_user(db, username="victim")
    login(client, "bob", "testpass123")
    assert admin_reset_link(client, victim.id).status_code == 403
    assert db.query(PasswordResetToken).count() == 0


def test_admin_cannot_issue_reset_link_for_self(client, db):
    admin = make_user(db, username="admin", password="testpass123", is_admin=True)
    login(client, "admin", "testpass123")
    admin_reset_link(client, admin.id)
    assert db.query(PasswordResetToken).count() == 0


def test_new_reset_link_replaces_the_old_one(client, db):
    make_user(db, username="admin", password="testpass123", is_admin=True)
    bob = make_user(db, username="bob")
    login(client, "admin", "testpass123")
    admin_reset_link(client, bob.id)
    first = link_token(client)
    admin_reset_link(client, bob.id)
    second = link_token(client)
    assert first != second
    assert "Link non valido" in client.get(f"/reset?token={first}").text
    assert "Salva e accedi" in client.get(f"/reset?token={second}").text


def test_expired_reset_link(client, db):
    bob = make_user(db, username="bob")
    token = password_reset.issue(db, bob)
    db.commit()
    row = db.query(PasswordResetToken).one()
    row.created_at = now_utc_naive() - password_reset.RESET_TOKEN_TTL - timedelta(minutes=1)
    db.commit()
    assert "Link non valido" in client.get(f"/reset?token={token}").text
    assert "Link non valido" in reset(client, token).text


def test_unknown_reset_link(client):
    assert "Link non valido" in client.get("/reset?token=made-up").text
    assert "Link non valido" in client.get("/reset").text


def test_reset_validation_keeps_the_link_usable(client, db):
    bob = make_user(db, username="bob")
    token = password_reset.issue(db, bob)
    db.commit()
    resp = reset(client, token, new="brandnew1", confirm="different1")
    assert "Le password non coincidono" in resp.text
    assert db.query(PasswordResetToken).count() == 1
    assert reset(client, token).status_code == 303


def test_deleting_a_user_removes_their_reset_link(client, db):
    make_user(db, username="admin", password="testpass123", is_admin=True)
    bob = make_user(db, username="bob")
    login(client, "admin", "testpass123")
    admin_reset_link(client, bob.id)
    client.post(f"/admin/users/{bob.id}/delete", data={"password": "testpass123"})
    assert db.query(User).filter(User.username == "bob").first() is None
    assert db.query(PasswordResetToken).count() == 0


def test_nightly_purge_removes_expired_links(db):
    bob = make_user(db, username="bob")
    carol = make_user(db, username="carol")
    password_reset.issue(db, bob)
    password_reset.issue(db, carol)
    db.commit()
    old = db.query(PasswordResetToken).filter(PasswordResetToken.user_id == bob.id).one()
    old.created_at = now_utc_naive() - password_reset.RESET_TOKEN_TTL - timedelta(hours=1)
    db.commit()
    runner.run_purge_reset_links()
    db.expire_all()
    assert [t.user_id for t in db.query(PasswordResetToken).all()] == [carol.id]


def test_access_log_redacts_reset_token():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/reset?token=secret-value", "1.1", 200), None,
    )
    _RedactResetToken().filter(record)
    assert "secret-value" not in record.getMessage()
    assert "/reset?[redacted]" in record.getMessage()


def test_login_page_points_to_reset(client):
    assert "Password dimenticata?" in client.get("/login").text


# --- CLI ---


def test_cli_reset_link(client, db, capsys):
    make_user(db, username="admin", password="forgotten1", is_admin=True)
    assert cli.main(["reset-link", "admin"]) == 0
    token = re.search(r"/reset\?token=([A-Za-z0-9_-]+)", capsys.readouterr().out).group(1)
    assert reset(client, token).status_code == 303
    assert logged_in(client)


def test_cli_unknown_user(db, capsys):
    assert cli.main(["reset-link", "nobody"]) == 1
    assert "No user named" in capsys.readouterr().err
