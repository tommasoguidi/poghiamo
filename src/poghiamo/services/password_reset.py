"""Password reset links, the recovery path of an app with no email.

An admin (from Impostazioni) or the poghiamo-admin CLI issues a link for a user
and sends it by hand. Only the SHA-256 of the token is stored.
"""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from poghiamo.database.models import PasswordResetToken, User
from poghiamo.services.feed import now_utc_naive

RESET_TOKEN_TTL = timedelta(hours=72)


def _hash(token: str) -> str:
    # A plain SHA-256 is enough: the token is random and long, so there is
    # nothing to brute-force, unlike a password.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reset_url(base_url: str, token: str) -> str:
    return f"{base_url}/reset?token={token}"


def purge_expired(db: Session) -> int:
    """Delete expired tokens; returns how many. The caller commits."""
    cutoff = now_utc_naive() - RESET_TOKEN_TTL
    return (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.created_at < cutoff)
        .delete(synchronize_session=False)
    )


def issue(db: Session, user: User, created_by: User | None = None) -> str:
    """Create a reset token for user and return it in clear, the only time it
    exists outside the link. The user's earlier tokens stop working. The caller
    commits."""
    purge_expired(db)
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.id).delete(
        synchronize_session=False
    )
    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash(token),
            created_by=created_by.id if created_by else None,
        )
    )
    return token


def find(db: Session, token: str) -> PasswordResetToken | None:
    """The live reset token matching this link, or None if unknown or expired."""
    if not token:
        return None
    return (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.token_hash == _hash(token),
            PasswordResetToken.created_at >= now_utc_naive() - RESET_TOKEN_TTL,
        )
        .first()
    )
