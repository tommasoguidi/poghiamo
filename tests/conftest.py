"""Shared test fixtures and factory helpers.

The environment MUST be configured before any poghiamo import: config.py reads
env at import time. DATABASE_URL=sqlite:// gives a shared in-memory DB
(StaticPool in engine.py); SESSION_HTTPS_ONLY=false because TestClient talks
plain http and the cookie jar would drop Secure cookies.
"""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_HTTPS_ONLY"] = "false"
os.environ["ADMIN_USERNAME"] = ""
os.environ["ADMIN_PASSWORD"] = ""

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from poghiamo.database.engine import Base, SessionLocal, engine  # noqa: E402
from poghiamo.database.models import InviteToken, User  # noqa: E402
from poghiamo.webapp.app import app  # noqa: E402
from poghiamo.webapp.auth import generate_invite_token, hash_password  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """Fresh tables for every test (the in-memory DB is shared via StaticPool)."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def make_user(db, *, username="alice", password="testpass123", is_admin=False):
    user = User(
        username=username,
        hashed_password=hash_password(password),
        is_admin=is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_invite(db, *, creator, token=None, age_days=0, revoked=False, used_by=None):
    invite = InviteToken(
        token=token or generate_invite_token(),
        created_by=creator.id,
        revoked=revoked,
        used_by=used_by,
        # Stored naive-UTC, matching SQLite's datetime('now') format.
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite


def login(client, username, password):
    return client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )


def signup(client, *, invite_code, username="newuser", password="longenough1", confirm=None):
    return client.post(
        "/signup",
        data={
            "username": username,
            "password": password,
            "confirm_password": confirm if confirm is not None else password,
            "invite_code": invite_code,
        },
        follow_redirects=False,
    )
