# webapp/auth.py
import secrets

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from poghiamo.database.engine import get_db
from poghiamo.database.models import User

# Word list for human-readable invite tokens (entropy comes from secrets.choice:
# 70*70*90 ≈ 440k combinations, plenty for single-use short-lived tokens that are
# also rate-limited at the edge).
_WORDS = [
    "alpine", "autumn", "blaze", "breeze", "bright", "canyon", "cedar",
    "cliff", "cloud", "coral", "crane", "creek", "crystal", "dawn",
    "delta", "drift", "dusk", "ember", "falcon", "fern", "flint",
    "forest", "frost", "glade", "grove", "harbor", "hawk", "haze",
    "island", "jade", "lark", "lunar", "maple", "marsh", "meadow",
    "mist", "north", "oasis", "ocean", "orbit", "peak", "pine",
    "plain", "pond", "prism", "rain", "reef", "ridge", "river",
    "sage", "shore", "sky", "slate", "snow", "solar", "spark",
    "spray", "stone", "storm", "stream", "summit", "swift", "tide",
    "trail", "vale", "wave", "wild", "wind", "winter", "zenith",
]


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def generate_invite_token() -> str:
    """Generate a random invite token like 'bright-forest-42' (CSPRNG-backed)."""
    w1 = secrets.choice(_WORDS)
    w2 = secrets.choice(_WORDS)
    num = 10 + secrets.randbelow(90)
    return f"{w1}-{w2}-{num}"


class RedirectToLogin(Exception):
    pass


def get_current_user(request: Request, db: Session = Depends(get_db)):
    """Return the logged-in User or None."""
    user_id = request.session.get("user_id")
    if user_id:
        user = db.get(User, user_id)
        if user:
            return user
    request.session.clear()
    return None


def require_user(request: Request, user: User = Depends(get_current_user)):
    """Raise RedirectToLogin if no user is logged in."""
    if user is None:
        raise RedirectToLogin()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """Raise HTTP 403 if the current user is not an admin."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
