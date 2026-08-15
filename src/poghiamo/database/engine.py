# database/engine.py
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import StaticPool

from poghiamo.config import DATABASE_URL

# SQLite needs check_same_thread=False when sessions cross threads (uvicorn workers,
# scheduler thread pool). A pure in-memory URL ("sqlite://", used by tests) additionally
# needs StaticPool so every session shares the single connection that holds the data.
connect_args = {}
engine_kwargs = {}
if "sqlite" in DATABASE_URL:
    connect_args["check_same_thread"] = False
    if DATABASE_URL in ("sqlite://", "sqlite:///:memory:"):
        engine_kwargs["poolclass"] = StaticPool

engine = create_engine(DATABASE_URL, connect_args=connect_args, echo=False, **engine_kwargs)

# WAL journal mode: needed because web and scheduler containers share one SQLite file.
if "sqlite" in DATABASE_URL:
    @event.listens_for(engine, "connect")
    def _set_sqlite_wal(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def init_db():
    """Create tables, apply migrations, seed the admin. Called at webapp import."""
    import poghiamo.database.models  # noqa: F401  (register models with Base.metadata)

    Base.metadata.create_all(bind=engine)
    _run_migrations()
    seed_admin()


def _run_migrations():
    """Hand-rolled idempotent migrations, lesgoski-style: inspect the schema and
    ALTER/CREATE what is missing. Add numbered blocks here as the schema evolves."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)

    # Migration 1 (phase 2): area preferences on users (regions + provinces).
    if "users" in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns("users")]
        if "regions" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN regions VARCHAR"))
        if "provinces" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN provinces VARCHAR"))


def seed_admin():
    """Bootstrap the admin account from ADMIN_USERNAME / ADMIN_PASSWORD.

    Unlike lesgoski, the password is applied only at CREATION: re-applying it on
    every boot silently reverts in-app password changes. Rotation, if ever needed,
    happens by deleting the user or via a future in-app change-password flow.
    """
    import logging

    from poghiamo.config import ADMIN_PASSWORD, ADMIN_USERNAME
    from poghiamo.database.models import User
    from poghiamo.webapp.auth import hash_password

    logger = logging.getLogger(__name__)
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if admin is None:
            admin = User(
                username=ADMIN_USERNAME,
                hashed_password=hash_password(ADMIN_PASSWORD),
                is_admin=True,
            )
            db.add(admin)
            logger.info(f"Admin user '{ADMIN_USERNAME}' created.")
        elif not admin.is_admin:
            admin.is_admin = True
            logger.info(f"User '{ADMIN_USERNAME}' promoted to admin.")
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Admin seeding failed: {e}", exc_info=True)
    finally:
        db.close()


def get_db():
    """Yield a session and always close it (FastAPI dependency)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
