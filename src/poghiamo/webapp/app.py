# webapp/app.py
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from poghiamo.config import SECRET_KEY, SESSION_HTTPS_ONLY, WEBAPP_URL
from poghiamo.database.engine import get_db, init_db
from poghiamo.database.models import InviteToken, User
from poghiamo.webapp.auth import (
    RedirectToLogin,
    generate_invite_token,
    get_current_user,
    hash_password,
    require_admin,
    require_user,
    verify_password,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_WEBAPP_DIR = Path(__file__).parent

# Fixed hash used to equalize login timing when the username does not exist.
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB setup at startup, not at import: importing this module has no side effects.
    init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=30 * 24 * 3600,
    https_only=SESSION_HTTPS_ONLY,  # Traefik terminates TLS; False only for local http dev
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(_WEBAPP_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(_WEBAPP_DIR / "templates"))
# Use CDN in dev (no built tailwind.css), pre-built CSS in production (Docker)
_tailwind_css = _WEBAPP_DIR / "static" / "tailwind.css"
templates.env.globals.update(
    tailwind_dev=not _tailwind_css.exists(),
    webapp_url=WEBAPP_URL,
)


@app.exception_handler(RedirectToLogin)
async def handle_redirect_to_login(request: Request, exc: RedirectToLogin):
    return RedirectResponse("/login", status_code=303)


# --- AUTH ROUTES ---


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User = Depends(get_current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")


@app.post("/login")
def login(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
):
    user = db.query(User).filter(User.username == username.strip()).first()
    if user:
        password_ok = verify_password(password, user.hashed_password)
    else:
        # Burn the same bcrypt cost for unknown usernames: no timing oracle.
        verify_password(password, _DUMMY_HASH)
        password_ok = False
    if not password_ok:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid username or password"},
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, user: User = Depends(get_current_user), invite: str = None):
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="signup.html", context={"invite": invite}
    )


@app.post("/signup")
def signup(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    invite_code: str = Form(...),
):
    ctx = {"invite": invite_code}
    username = username.strip()

    # Validate invite token (single-use, 7-day lifetime, revocable)
    token_obj = (
        db.query(InviteToken)
        .filter(
            InviteToken.token == invite_code.strip(),
            InviteToken.used_by.is_(None),
            ~InviteToken.revoked,
            InviteToken.created_at >= datetime.now(timezone.utc) - timedelta(days=7),
        )
        .first()
    )
    if not token_obj:
        ctx["error"] = "Invalid or already used invite code"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if password != confirm_password:
        ctx["error"] = "Passwords do not match"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if len(password) < 8:
        ctx["error"] = "Password must be at least 8 characters"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if len(password.encode("utf-8")) > 72:
        # bcrypt silently ignores bytes past 72: reject instead of pretending.
        ctx["error"] = "Password is too long (max 72 bytes)"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if len(username) < 3:
        ctx["error"] = "Username must be at least 3 characters"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if len(username) > 32:
        ctx["error"] = "Username must be at most 32 characters"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if db.query(User).filter(User.username == username).first():
        ctx["error"] = "Username already taken"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)

    # Create the user and consume the token in ONE transaction, with a guarded
    # UPDATE re-checking used_by IS NULL: two concurrent signups on the same
    # token cannot both succeed (the loser's update matches 0 rows).
    user = User(username=username, hashed_password=hash_password(password))
    db.add(user)
    db.flush()
    consumed = (
        db.query(InviteToken)
        .filter(InviteToken.id == token_obj.id, InviteToken.used_by.is_(None))
        .update({"used_by": user.id, "used_at": datetime.now(timezone.utc)})
    )
    if consumed != 1:
        db.rollback()
        ctx["error"] = "Invalid or already used invite code"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    db.commit()

    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- PAGES ---


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request=request, name="index.html", context={"user": user}
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, user: User = Depends(get_current_user)):
    # Public page: reachable without login, as a privacy notice must be.
    return templates.TemplateResponse(
        request=request, name="privacy.html", context={"user": user}
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    error: str = None,
):
    invite_tokens = []
    if user.is_admin:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        invite_tokens = (
            db.query(InviteToken)
            .filter(~InviteToken.revoked, InviteToken.created_at >= cutoff)
            .order_by(InviteToken.created_at.desc())
            .all()
        )
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"user": user, "invite_tokens": invite_tokens, "error": error},
    )


# --- ADMIN: INVITE TOKENS ---


@app.post("/admin/tokens/generate")
def generate_token(
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    token = InviteToken(token=generate_invite_token(), created_by=user.id)
    db.add(token)
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/admin/tokens/{token_id}/revoke")
def revoke_token(
    token_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    token = db.get(InviteToken, token_id)
    if token and not token.used_by:
        token.revoked = True
        db.commit()
    return RedirectResponse("/settings", status_code=303)


# --- ACCOUNT DELETION (GDPR Art. 17, self-serve) ---


@app.post("/account/delete")
def delete_account(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    password: str = Form(...),
):
    if not verify_password(password, user.hashed_password):
        return RedirectResponse("/settings?error=Wrong+password", status_code=303)

    # An invite-only app with zero admins can never mint invites again:
    # refuse to delete the last admin.
    if user.is_admin:
        admin_count = db.query(User).filter(User.is_admin).count()
        if admin_count <= 1:
            return RedirectResponse(
                "/settings?error=You+are+the+last+admin:+promote+someone+first",
                status_code=303,
            )

    # Capture before delete: the ORM instance expires at commit.
    username = user.username

    # Unused tokens the user created: delete. Used ones: keep for the audit
    # trail, detached from the creator. The token the user consumed to join is
    # retired for good (never resurrect a spent single-use invite).
    db.query(InviteToken).filter(
        InviteToken.created_by == user.id, InviteToken.used_by.is_(None)
    ).delete()
    db.query(InviteToken).filter(InviteToken.created_by == user.id).update(
        {"created_by": None}
    )
    db.query(InviteToken).filter(InviteToken.used_by == user.id).update(
        {"used_by": None, "revoked": True}
    )
    db.delete(user)
    db.commit()

    request.session.clear()
    logger.info(f"Account '{username}' deleted at the user's request.")
    return RedirectResponse("/login", status_code=303)


def main():
    """Entry point for the web application."""
    import os

    import uvicorn

    uvicorn.run(
        "poghiamo.webapp.app:app",
        host=os.getenv("UVICORN_HOST", "127.0.0.1"),
        port=int(os.getenv("UVICORN_PORT", "8000")),
        reload=os.getenv("UVICORN_RELOAD", "true").lower() == "true",
    )


if __name__ == "__main__":
    main()
