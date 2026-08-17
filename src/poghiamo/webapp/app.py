# webapp/app.py
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from poghiamo.config import SECRET_KEY, SESSION_HTTPS_ONLY, WEBAPP_URL
from poghiamo.database.engine import get_db, init_db
from poghiamo.database.models import Artist, Event, Follow, InviteToken, SavedEvent, User
from poghiamo import geo
from poghiamo.services import artist_search, feed
from poghiamo.services.pipeline import scan_artist_by_id
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

_IT_MONTHS = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]


def _it_date(d):
    return f"{d.day} {_IT_MONTHS[d.month - 1]} {d.year}" if d else ""


def _it_month(d):
    return f"{_IT_MONTHS[d.month - 1].capitalize()} {d.year}" if d else ""


templates.env.filters["it_date"] = _it_date
templates.env.filters["it_month"] = _it_month


@app.exception_handler(RedirectToLogin)
async def handle_redirect_to_login(request: Request, exc: RedirectToLogin):
    return RedirectResponse("/login", status_code=303)


def _landing_url(db, user) -> str:
    """Where a just-logged-in user lands: onboarding for a brand-new account
    (no zones, no follows), otherwise the Calendario home."""
    has_follows = (
        db.query(Follow)
        .filter(Follow.user_id == user.id, Follow.state == "active")
        .first()
        is not None
    )
    if not has_follows and not (user.regions or user.provinces):
        return "/settings?welcome=1"
    return "/calendario"


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
            context={"error": "Nome utente o password non validi"},
        )
    request.session["user_id"] = user.id
    return RedirectResponse(_landing_url(db, user), status_code=303)


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
        ctx["error"] = "Codice di invito non valido o già usato"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if password != confirm_password:
        ctx["error"] = "Le password non coincidono"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if len(password) < 8:
        ctx["error"] = "La password deve avere almeno 8 caratteri"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if len(password.encode("utf-8")) > 72:
        # bcrypt silently ignores bytes past 72: reject instead of pretending.
        ctx["error"] = "Password troppo lunga (massimo 72 byte)"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if len(username) < 3:
        ctx["error"] = "Il nome utente deve avere almeno 3 caratteri"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if len(username) > 32:
        ctx["error"] = "Il nome utente può avere al massimo 32 caratteri"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    if db.query(User).filter(User.username == username).first():
        ctx["error"] = "Nome utente già in uso"
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
        ctx["error"] = "Codice di invito non valido o già usato"
        return templates.TemplateResponse(request=request, name="signup.html", context=ctx)
    db.commit()

    request.session["user_id"] = user.id
    # Brand-new account: start the onboarding (set zones, then add artists).
    return RedirectResponse("/settings?welcome=1", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- PAGES ---


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    in_zona, altre = feed.feed_split(db, user)
    saved_ids = feed.saved_event_ids(db, user)
    feed.annotate(in_zona, last_seen=user.last_seen_events_at, saved_ids=saved_ids, user=user)
    feed.annotate(altre, last_seen=user.last_seen_events_at, saved_ids=saved_ids, user=user)
    # Mark this visit AFTER computing "new since last visit".
    user.last_seen_events_at = feed.now_utc_naive()
    db.commit()
    return templates.TemplateResponse(
        request=request,
        name="feed.html",
        context={
            "user": user,
            "in_zona": in_zona,
            "altre": altre,
            "has_areas": bool(user.regions or user.provinces),
        },
    )


@app.get("/api/comuni/search")
def api_comuni_search(q: str = "", user: User = Depends(require_user)):
    return JSONResponse({"results": geo.search_comuni(q)})


@app.get("/calendario", response_class=HTMLResponse)
def calendario(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    error: str = None,
):
    groups = feed.calendar_month_groups(db, user)
    saved_ids = feed.saved_event_ids(db, user)
    for g in groups:
        feed.annotate(g["events"], last_seen=user.last_seen_events_at, saved_ids=saved_ids, user=user)
    return templates.TemplateResponse(
        request=request,
        name="calendario.html",
        context={"user": user, "error": error, "groups": groups},
    )


@app.post("/events/create")
def add_custom_event(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    artist_name: str = Form(...),
    date: str = Form(...),
    city: str = Form(...),
    venue: str = Form(None),
    deezer_id: int = Form(None),
    image_url: str = Form(None),
):
    from datetime import date as _date

    artist_name = artist_name.strip()
    city = city.strip()
    try:
        when = _date.fromisoformat(date)
    except ValueError:
        return RedirectResponse("/calendario?error=Data+non+valida", status_code=303)
    if not artist_name or not city:
        return RedirectResponse("/calendario?error=Artista,+data+e+città+sono+obbligatori", status_code=303)

    # Find or create the artist, and follow it (reuse the manual-add path's rules).
    normalized = artist_search.normalize_name(artist_name)
    artist = None
    if deezer_id is not None:
        artist = db.query(Artist).filter(Artist.deezer_id == deezer_id).first()
    if artist is None:
        artist = db.query(Artist).filter(Artist.name_normalized == normalized).first()
    if artist is None:
        artist = Artist(
            name=artist_name, name_normalized=normalized, deezer_id=deezer_id,
            image_url=(image_url or None),
        )
        db.add(artist)
        db.flush()
        artist_search.maybe_retry_enrichment([artist.id], background_tasks)
        # Scan the freshly-created artist too: the custom event covers one date,
        # the sources may know others.
        background_tasks.add_task(scan_artist_by_id, artist.id)
    follow = (
        db.query(Follow).filter(Follow.user_id == user.id, Follow.artist_id == artist.id).first()
    )
    if follow is None:
        db.add(Follow(user_id=user.id, artist_id=artist.id, source="manual"))
    elif follow.state != "active":
        follow.state = "active"
        follow.removed_at = None

    # Reuse an existing event for that (artist, date), else create a custom one.
    event = (
        db.query(Event).filter(Event.artist_id == artist.id, Event.date == when).first()
    )
    if event is None:
        from poghiamo.sources.base import normalize_city, resolve_area

        province, region = resolve_area((city or "").strip() or None, None)
        event = Event(
            artist_id=artist.id,
            date=when,
            city=(city or "").strip() or None,
            city_normalized=normalize_city(city),
            province=province,
            region=region,
            venue=(venue or "").strip() or None,
            title=f"{artist.name} live",
            added_by_user_id=user.id,
        )
        db.add(event)
        db.flush()

    # Save it for the creator (co-followers see the shared event on their own feed).
    if (
        db.query(SavedEvent)
        .filter(SavedEvent.user_id == user.id, SavedEvent.event_id == event.id)
        .first()
        is None
    ):
        db.add(SavedEvent(user_id=user.id, event_id=event.id))
    db.commit()
    return RedirectResponse("/calendario", status_code=303)


@app.post("/events/{event_id}/custom-delete")
def delete_custom_event(
    event_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    event = db.get(Event, event_id)
    # Only a custom event, and only its creator or an admin, may be deleted.
    if event and event.added_by_user_id is not None and (
        user.is_admin or event.added_by_user_id == user.id
    ):
        db.query(SavedEvent).filter(SavedEvent.event_id == event_id).delete()
        db.delete(event)
        db.commit()
    return RedirectResponse("/calendario", status_code=303)


@app.post("/events/{event_id}/save")
def save_event(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    event = db.get(Event, event_id)
    if event is not None:
        exists = (
            db.query(SavedEvent)
            .filter(SavedEvent.user_id == user.id, SavedEvent.event_id == event_id)
            .first()
        )
        if exists is None:
            db.add(SavedEvent(user_id=user.id, event_id=event_id))
            db.commit()
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.post("/events/{event_id}/unsave")
def unsave_event(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    db.query(SavedEvent).filter(
        SavedEvent.user_id == user.id, SavedEvent.event_id == event_id
    ).delete()
    db.commit()
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, user: User = Depends(get_current_user)):
    # Public page: reachable without login, as a privacy notice must be.
    return templates.TemplateResponse(
        request=request, name="privacy.html", context={"user": user}
    )


# --- ARTISTS ---


@app.get("/artists", response_class=HTMLResponse)
def artists_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    follows = (
        db.query(Follow)
        .options(joinedload(Follow.artist))
        .join(Artist, Follow.artist_id == Artist.id)
        .filter(Follow.user_id == user.id, Follow.state == "active")
        .order_by(Artist.name_normalized)
        .all()
    )
    # Identity of already-followed artists, so the add-search can flag them.
    followed = {
        "deezer": [f.artist.deezer_id for f in follows if f.artist.deezer_id is not None],
        "names": [f.artist.name_normalized for f in follows],
    }
    return templates.TemplateResponse(
        request=request,
        name="artists.html",
        context={"user": user, "follows": follows, "followed": followed},
    )


@app.get("/api/artists/search")
def api_artists_search(
    q: str = "",
    user: User = Depends(require_user),
):
    q = q.strip()
    if len(q) < 2:
        return JSONResponse({"results": []})
    try:
        suggestions = artist_search.search_deezer(q)
    except requests.RequestException as e:
        logger.warning(f"Deezer search failed for '{q}': {e}")
        return JSONResponse({"results": [], "degraded": True})
    results = [{**s.model_dump(), "label": s.name} for s in suggestions]
    return JSONResponse({"results": results})


@app.get("/api/artists/status")
def api_artists_status(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Statuses of the user's followed artists, polled by the artists page.
    Also self-heals: stuck 'pending' artists get their enrichment re-scheduled."""
    follows = (
        db.query(Follow)
        .options(joinedload(Follow.artist))
        .filter(Follow.user_id == user.id, Follow.state == "active")
        .all()
    )
    pending = [f.artist.id for f in follows if f.artist.resolution_status == "pending"]
    if pending:
        artist_search.maybe_retry_enrichment(pending, background_tasks)
    return JSONResponse(
        {
            "artists": [
                {
                    "id": f.artist.id,
                    "status": f.artist.resolution_status,
                    "country": f.artist.country,
                }
                for f in follows
            ]
        }
    )


@app.post("/artists/add")
def add_artist(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    name: str = Form(...),
    deezer_id: int = Form(None),
    image_url: str = Form(None),
):
    name = name.strip()
    if not name:
        return RedirectResponse("/artists", status_code=303)
    normalized = artist_search.normalize_name(name)

    # Find-or-create the shared canonical artist: by Deezer id first, then by
    # normalized name (covers free-text adds and cross-user dedup).
    artist = None
    if deezer_id is not None:
        artist = db.query(Artist).filter(Artist.deezer_id == deezer_id).first()
    if artist is None:
        artist = db.query(Artist).filter(Artist.name_normalized == normalized).first()
    if artist is None:
        artist = Artist(
            name=name,
            name_normalized=normalized,
            deezer_id=deezer_id,
            image_url=image_url or None,
        )
        db.add(artist)
        db.flush()
        # Instant add, async enrichment: MusicBrainz resolution happens after
        # the response, at its own 1 req/s pace (retry-tracked).
        artist_search.maybe_retry_enrichment([artist.id], background_tasks)
        # A never-seen artist gets scanned right away (after the response), so
        # its events show up without waiting for the periodic sweep. The sweep
        # remains the fallback: on failure last_scanned_at stays NULL and it
        # retries. Only new artists fire this; following a known one inherits
        # the shared events already stored.
        background_tasks.add_task(scan_artist_by_id, artist.id)
    elif artist.deezer_id is None and deezer_id is not None:
        artist.deezer_id = deezer_id
        if not artist.image_url and image_url:
            artist.image_url = image_url

    follow = (
        db.query(Follow)
        .filter(Follow.user_id == user.id, Follow.artist_id == artist.id)
        .first()
    )
    if follow is None:
        db.add(Follow(user_id=user.id, artist_id=artist.id, source="manual"))
    elif follow.state != "active":
        # Explicit re-follow reactivates the same row.
        follow.state = "active"
        follow.removed_at = None
    db.commit()
    return RedirectResponse("/artists", status_code=303)


@app.post("/artists/{artist_id}/unfollow")
def unfollow_artist(
    artist_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    follow = (
        db.query(Follow)
        .filter(Follow.user_id == user.id, Follow.artist_id == artist_id)
        .first()
    )
    if follow and follow.state == "active":
        # Persistent removal: future Spotify syncs must not resurrect this.
        follow.state = "removed"
        follow.removed_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse("/artists", status_code=303)


@app.get("/artists/{artist_id}", response_class=HTMLResponse)
def artist_detail(
    artist_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    artist = db.get(Artist, artist_id)
    if artist is None:
        return RedirectResponse("/artists", status_code=303)
    events = feed.artist_events(db, artist_id)
    feed.annotate(events, last_seen=user.last_seen_events_at, saved_ids=feed.saved_event_ids(db, user), user=user)
    following = (
        db.query(Follow)
        .filter(Follow.user_id == user.id, Follow.artist_id == artist_id, Follow.state == "active")
        .first()
        is not None
    )
    return templates.TemplateResponse(
        request=request,
        name="artist_detail.html",
        context={"user": user, "artist": artist, "events": events, "following": following},
    )


# --- SETTINGS ---


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    error: str = None,
    saved: str = None,
    hint: str = None,
    welcome: str = None,
):
    invite_tokens = []
    all_users = []
    if user.is_admin:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        invite_tokens = (
            db.query(InviteToken)
            .filter(~InviteToken.revoked, InviteToken.created_at >= cutoff)
            .order_by(InviteToken.created_at.desc())
            .all()
        )
        # Everyone but the admin themselves (they manage their own account below).
        all_users = (
            db.query(User).filter(User.id != user.id).order_by(User.username).all()
        )
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "user": user,
            "invite_tokens": invite_tokens,
            "all_users": all_users,
            "error": error,
            "saved": saved,
            "hint": hint,
            "welcome": welcome,
            # Flat searchable list for the area picker: regions first, then provinces.
            "areas": (
                [{"type": "region", "value": r, "label": r, "region": r} for r in geo.regions()]
                + [
                    {"type": "province", "value": sigla, "label": f"{name} ({sigla})", "region": region}
                    for region, provs in geo.provinces_by_region().items()
                    for sigla, name in provs
                ]
            ),
        },
    )


@app.post("/settings/regions")
def save_regions(
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    regions: list[str] = Form(default=[]),
    provinces: list[str] = Form(default=[]),
):
    # Keep only real names/sigle; empty selection means "all of Italy".
    known_provinces = geo.region_of_province()
    user.regions = [r for r in regions if r in geo.regions()]
    user.provinces = [p for p in provinces if p in known_provinces]
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


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
        return RedirectResponse("/settings?error=Password+errata", status_code=303)

    # An invite-only app with zero admins can never mint invites again:
    # refuse to delete the last admin.
    if user.is_admin:
        admin_count = db.query(User).filter(User.is_admin).count()
        if admin_count <= 1:
            return RedirectResponse(
                "/settings?error=Sei+l'ultimo+admin:+promuovi+prima+qualcun+altro",
                status_code=303,
            )

    _purge_user(db, user)
    db.commit()

    request.session.clear()
    logger.info(f"Account '{user.username}' deleted at the user's request.")
    return RedirectResponse("/login", status_code=303)


def _purge_user(db, target: User) -> None:
    """Remove a user and everything attached to them, keeping shared data intact:
    their bookmarks and follows go; the custom events they created stay (shared
    with co-followers) but are detached; invite tokens keep their audit trail."""
    db.query(SavedEvent).filter(SavedEvent.user_id == target.id).delete()
    db.query(Follow).filter(Follow.user_id == target.id).delete()
    # Custom events they created remain visible to co-followers, just orphaned.
    db.query(Event).filter(Event.added_by_user_id == target.id).update(
        {"added_by_user_id": None}
    )
    db.query(InviteToken).filter(
        InviteToken.created_by == target.id, InviteToken.used_by.is_(None)
    ).delete()
    db.query(InviteToken).filter(InviteToken.created_by == target.id).update(
        {"created_by": None}
    )
    db.query(InviteToken).filter(InviteToken.used_by == target.id).update(
        {"used_by": None, "revoked": True}
    )
    db.delete(target)


@app.post("/admin/users/{user_id}/delete")
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    password: str = Form(...),
):
    # Deleting someone else's account is destructive: confirm the admin's password.
    if not verify_password(password, admin.hashed_password):
        return RedirectResponse("/settings?error=Password+errata", status_code=303)
    target = db.get(User, user_id)
    if target is None or target.id == admin.id:
        # Admins remove their own account from the danger zone, not here.
        return RedirectResponse("/settings", status_code=303)
    if target.is_admin and db.query(User).filter(User.is_admin).count() <= 1:
        return RedirectResponse(
            "/settings?error=Non+puoi+eliminare+l'ultimo+admin", status_code=303
        )
    username = target.username
    _purge_user(db, target)
    db.commit()
    logger.info(f"Admin '{admin.username}' deleted account '{username}'.")
    return RedirectResponse("/settings", status_code=303)


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
