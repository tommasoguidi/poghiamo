"""Auth flows: login, invite-only signup, admin tokens, account deletion."""

from conftest import login, make_invite, make_user, signup

from poghiamo.database.models import InviteToken, User


def test_login_page_is_public(client):
    assert client.get("/login").status_code == 200


def test_root_redirects_anonymous_to_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_privacy_is_public(client):
    assert client.get("/privacy").status_code == 200


def test_login_success_sets_session(client, db):
    make_user(db, username="alice", password="testpass123")
    resp = login(client, "alice", "testpass123")
    assert resp.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_login_wrong_password(client, db):
    make_user(db, username="alice", password="testpass123")
    resp = login(client, "alice", "wrongwrong")
    assert resp.status_code == 200
    assert "Invalid username or password" in resp.text


def test_signup_with_valid_invite(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin)

    resp = signup(client, invite_code=invite.token)
    assert resp.status_code == 303

    user = db.query(User).filter(User.username == "newuser").first()
    assert user is not None
    db.refresh(invite)
    assert invite.used_by == user.id
    # Session is live right away
    assert client.get("/", follow_redirects=False).status_code == 200


def test_signup_invalid_invite(client, db):
    resp = signup(client, invite_code="not-a-real-token")
    assert resp.status_code == 200
    assert "Invalid or already used invite code" in resp.text


def test_signup_used_invite(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    other = make_user(db, username="other")
    invite = make_invite(db, creator=admin, used_by=other.id)
    resp = signup(client, invite_code=invite.token)
    assert "Invalid or already used invite code" in resp.text


def test_signup_expired_invite(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin, age_days=8)
    resp = signup(client, invite_code=invite.token)
    assert "Invalid or already used invite code" in resp.text


def test_signup_revoked_invite(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin, revoked=True)
    resp = signup(client, invite_code=invite.token)
    assert "Invalid or already used invite code" in resp.text


def test_signup_password_mismatch(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin)
    resp = signup(client, invite_code=invite.token, confirm="different1")
    assert "Passwords do not match" in resp.text


def test_signup_short_password(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin)
    resp = signup(client, invite_code=invite.token, password="short")
    assert "at least 8 characters" in resp.text


def test_signup_taken_username(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin)
    resp = signup(client, invite_code=invite.token, username="admin")
    assert "Username already taken" in resp.text


def test_settings_requires_login(client):
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_admin_generates_and_revokes_token(client, db):
    make_user(db, username="admin", password="testpass123", is_admin=True)
    login(client, "admin", "testpass123")

    resp = client.post("/admin/tokens/generate", follow_redirects=False)
    assert resp.status_code == 303
    token = db.query(InviteToken).first()
    assert token is not None and not token.revoked

    resp = client.post(f"/admin/tokens/{token.id}/revoke", follow_redirects=False)
    assert resp.status_code == 303
    db.refresh(token)
    assert token.revoked


def test_non_admin_cannot_generate_tokens(client, db):
    make_user(db, username="bob", password="testpass123", is_admin=False)
    login(client, "bob", "testpass123")
    resp = client.post("/admin/tokens/generate", follow_redirects=False)
    assert resp.status_code == 403


def test_delete_account_wrong_password(client, db):
    make_user(db, username="bob", password="testpass123")
    login(client, "bob", "testpass123")
    resp = client.post("/account/delete", data={"password": "nope-nope"}, follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(User).filter(User.username == "bob").first() is not None


def test_delete_account(client, db):
    make_user(db, username="admin", password="testpass123", is_admin=True)
    make_user(db, username="bob", password="testpass123")
    login(client, "bob", "testpass123")

    resp = client.post(
        "/account/delete", data={"password": "testpass123"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert db.query(User).filter(User.username == "bob").first() is None
    # Session is dead
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


def test_cannot_delete_last_admin(client, db):
    make_user(db, username="admin", password="testpass123", is_admin=True)
    login(client, "admin", "testpass123")
    resp = client.post(
        "/account/delete", data={"password": "testpass123"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert db.query(User).filter(User.username == "admin").first() is not None


def test_admin_with_peer_can_delete_and_unused_tokens_are_cleaned(client, db):
    admin = make_user(db, username="admin", password="testpass123", is_admin=True)
    make_user(db, username="admin2", password="testpass123", is_admin=True)
    make_invite(db, creator=admin)  # unused token, should be cleaned up
    login(client, "admin", "testpass123")

    resp = client.post(
        "/account/delete", data={"password": "testpass123"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert db.query(User).filter(User.username == "admin").first() is None
    assert db.query(InviteToken).count() == 0


def test_used_invite_stays_dead_after_consumer_deletes_account(client, db):
    """Regression: deleting an account must not resurrect the invite it consumed."""
    from fastapi.testclient import TestClient

    from poghiamo.webapp.app import app

    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin)

    # Consume the invite, then delete the freshly created account
    assert signup(client, invite_code=invite.token, username="guest").status_code == 303
    resp = client.post(
        "/account/delete", data={"password": "longenough1"}, follow_redirects=False
    )
    assert resp.status_code == 303

    db.refresh(invite)
    assert invite.revoked and invite.used_by is None

    # A brand-new visitor cannot reuse the same token
    with TestClient(app) as second_client:
        resp = signup(second_client, invite_code=invite.token, username="guest2")
        assert resp.status_code == 200
        assert "Invalid or already used invite code" in resp.text


def test_used_tokens_survive_creator_deletion_for_audit(client, db):
    admin = make_user(db, username="admin", password="testpass123", is_admin=True)
    make_user(db, username="admin2", password="testpass123", is_admin=True)
    member = make_user(db, username="member")
    used = make_invite(db, creator=admin, used_by=member.id)

    login(client, "admin", "testpass123")
    client.post("/account/delete", data={"password": "testpass123"}, follow_redirects=False)

    db.refresh(used)
    assert used.created_by is None and used.used_by == member.id


def test_non_admin_cannot_revoke_tokens(client, db):
    admin = make_user(db, username="admin", is_admin=True)
    invite = make_invite(db, creator=admin)
    make_user(db, username="bob", password="testpass123")
    login(client, "bob", "testpass123")
    resp = client.post(f"/admin/tokens/{invite.id}/revoke", follow_redirects=False)
    assert resp.status_code == 403


def test_session_cookie_flags(client, db):
    make_user(db, username="alice", password="testpass123")
    resp = login(client, "alice", "testpass123")
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    # SESSION_HTTPS_ONLY=false in tests, so no Secure here; production adds it.
